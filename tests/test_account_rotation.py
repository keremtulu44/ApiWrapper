"""Üç hesaplı rotasyon: yuva takası, serbest bırakma ve rol görünümü.

Kullanıcı sözleşmesi: üç ayrı hesap (Max-1, Max-2 ve yedek/Agent) dönüşümlü
kullanılmalı; hiçbir hesap sessizce tamamen tükenmemeli. Bu testler rotasyonun
**veri kaybetmeden** ve **aynı hesabı iki yuvaya düşürmeden** yapıldığını
doğrular.
"""

from __future__ import annotations

import pytest

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.main import create_app


def _curl_for(sub: str, *, model_id: str = "019c-max-model-v1") -> str:
    """Verilen hesap kimliğiyle geçerli bir `create-evaluation` cURL'ü.

    Gerçek biçim kullanılır (parçalı `.0`/`.1` oturum çerezi + JWT `sub`),
    çünkü rol görünümü kimliği bu çerezden türetir.
    """
    from tests.test_upstream_slots import _build_realistic_max_curl

    return _build_realistic_max_curl(sub=sub, model_id=model_id)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "api_keys": ["sk-test"],
        "upstream_keepalive_interval_seconds": 0.0,
        "request_gap_min_seconds": 0.0,
        "request_gap_max_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_three_account_slots_are_supported() -> None:
    """Üç hesap için 3. yuvanın (yedek/Agent) ayarları gerçekten var olmalı.

    Regresyon koruması: alanlar `Settings`'ten düşerse rotasyon sessizce
    tek/iki hesaba iner ve "hesap tamamen bitti" durumu geri gelir.
    """
    assert MAX_ACCOUNT_SLOTS >= 3
    fields = Settings.model_fields
    assert "upstream_curl_3" in fields
    assert "upstream_cookie_3" in fields


@pytest.mark.asyncio
async def test_swap_roles_moves_accounts_without_duplicates() -> None:
    """`swap_roles(1, 2)` iki hesabın **yerini** değiştirmeli, kopyalamamalı."""
    settings = _settings(
        upstream_curl=_curl_for("hesap-a"),
        upstream_curl_2=_curl_for("hesap-b"),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        slot_mgr = app.state.slot_manager
        before = slot_mgr.status_snapshot()
        fp_1_before = before["roles"]["max_primary"]["identity_fingerprint"]
        fp_2_before = before["roles"]["max_secondary"]["identity_fingerprint"]
        assert fp_1_before and fp_2_before
        assert fp_1_before != fp_2_before

        result = await slot_mgr.swap_roles(1, 2)

        assert result["swapped"] == [1, 2]
        roles = result["roles"]
        # Yer değiştirdiler: 1. yuva artık eskiden 2. yuvada olan hesap.
        assert roles["max_primary"]["identity_fingerprint"] == fp_2_before
        assert roles["max_secondary"]["identity_fingerprint"] == fp_1_before
        # Hiçbir hesap iki yuvaya birden düşmedi.
        fingerprints = [
            (roles["max_primary"]["identity_fingerprint"]),
            (roles["max_secondary"]["identity_fingerprint"]),
        ]
        assert len(set(fingerprints)) == 2
        assert roles["configured_count"] == 2


@pytest.mark.asyncio
async def test_swap_roles_is_atomic_and_reversible() -> None:
    """İki kez takas etmek başlangıç durumuna dönmeli (atomik, sır kaybı yok)."""
    settings = _settings(
        upstream_curl=_curl_for("hesap-a"),
        upstream_curl_2=_curl_for("hesap-b"),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        slot_mgr = app.state.slot_manager
        original = slot_mgr.status_snapshot()["roles"]

        await slot_mgr.swap_roles(1, 2)
        await slot_mgr.swap_roles(1, 2)
        restored = slot_mgr.status_snapshot()["roles"]

        assert (
            restored["max_primary"]["identity_fingerprint"]
            == original["max_primary"]["identity_fingerprint"]
        )
        assert (
            restored["max_secondary"]["identity_fingerprint"]
            == original["max_secondary"]["identity_fingerprint"]
        )


@pytest.mark.asyncio
async def test_swap_roles_rejects_empty_slot() -> None:
    """Boş yuva takas edilemez; hata kullanıcıya açıkça söylenmeli."""
    settings = _settings(upstream_curl=_curl_for("hesap-a"))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        with pytest.raises(ValueError, match="iki yuva da dolu"):
            await app.state.slot_manager.swap_roles(1, 2)


@pytest.mark.asyncio
async def test_release_slot_refuses_primary_and_clears_secondary() -> None:
    """3. yuva serbest bırakılabilir; 1. yuva (ana hesap) **asla** bırakılamaz."""
    settings = _settings(
        upstream_curl=_curl_for("hesap-a"),
        upstream_curl_2=_curl_for("hesap-b"),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        slot_mgr = app.state.slot_manager
        with pytest.raises(ValueError, match="1. ana Max yuvası bırakılamaz"):
            await slot_mgr.release_slot(1)

        result = await slot_mgr.release_slot(2)
        assert result["closed_slot"] == 2
        roles = slot_mgr.status_snapshot()["roles"]
        assert roles["max_primary"]["configured"] is True
        assert roles["max_secondary"]["configured"] is False


@pytest.mark.asyncio
async def test_rotation_swap_endpoint_via_api() -> None:
    """`/rotasyon` ucu Telegram'ın kullandığı sözleşmeyi korumalı."""
    import httpx

    settings = _settings(
        upstream_curl=_curl_for("hesap-a"),
        upstream_curl_2=_curl_for("hesap-b"),
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.internal",
            headers={"Authorization": "Bearer sk-test"},
        ) as client,
    ):
            resp = await client.post(
                "/v1/autopilot/upstream-slots/swap",
                json={"slot_a": 1, "slot_b": 2},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["swapped"] == [1, 2]
            assert data["roles"]["configured_count"] == 2

            bad = await client.post(
                "/v1/autopilot/upstream-slots/swap",
                json={"slot_a": 1, "slot_b": 1},
            )
            assert bad.status_code in (400, 422)
