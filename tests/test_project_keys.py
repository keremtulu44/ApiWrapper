"""Proje anahtarı + kullanım muhasebesi sözleşme testleri.

İki şey kilitlenir:
1. "Hangi proje ne kadar kullandı" sorusu cevaplanabilir (etiket + sayaçlar).
2. Anahtar **değeri** hiçbir yüzeye sızmaz: log'a değil, liste yanıtına değil,
   durum dosyasına etiket olarak değil. Yalnız `API_KEYS` listesinde düz metin
   durur (operatörün seçtiği mevcut düzen).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.core.errors import InvalidRequestError
from app.core.logging import mask_value
from app.core.metrics import metrics
from app.core.security import client_fingerprint
from app.services.project_keys import (
    ProjectRegistry,
    format_labels,
    normalize_label,
    parse_labels,
)
from app.upstream.state_store import (
    _ENV_API_KEYS,
    remember_seed,
    restore_runtime_state,
    save_runtime_state,
    sync_render_env,
)
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


@pytest.fixture(autouse=True)
def _fresh_api_key_seed():
    """`id()` yeniden kullanımı testler arasında açılış env'ini karıştırmasın."""
    _ENV_API_KEYS.clear()
    yield
    _ENV_API_KEYS.clear()


# ------------------------------------------------------------------ etiket biçimi
def test_label_normalization_protects_the_separators():
    """Virgül ve iki nokta eşlemenin ayıracı: etikette kalırlarsa eşleme kayar."""
    assert normalize_label("  Proje A  ") == "Proje-A"
    assert normalize_label("a,b:c") == "a-b-c"
    assert normalize_label("x" * 80) == "x" * 40
    assert normalize_label(" ,: ") == ""


def test_labels_round_trip_and_bad_parts_are_dropped():
    labels = {"cli_a": "proje-a", "cli_b": "diger"}
    assert parse_labels(format_labels(labels)) == labels
    assert parse_labels("cli_a:proje,bozuk,cli_b,:bos") == {"cli_a": "proje"}


# ------------------------------------------------------------------- muhasebe
def test_record_counts_requests_tokens_and_errors():
    settings = make_settings()
    registry = ProjectRegistry(settings)
    key_id = client_fingerprint(TEST_API_KEY)
    registry.bind_label(key_id, "projem")

    registry.record(key_id, tokens=120)
    registry.record(key_id, error=True)

    (row,) = registry.snapshot(settings)
    assert row["key_id"] == key_id
    assert row["label"] == "projem"
    assert row["masked_key"] == mask_value(TEST_API_KEY)
    assert (row["requests"], row["tokens"], row["errors"]) == (2, 120, 1)
    assert row["last_used"] is not None
    # Anahtar değeri listede yok.
    assert TEST_API_KEY not in json.dumps(row)


def test_metrics_carry_the_label_never_the_key():
    metrics.reset()
    settings = make_settings()
    registry = ProjectRegistry(settings)
    registry.bind_label(client_fingerprint(TEST_API_KEY), "projem")
    registry.record(client_fingerprint(TEST_API_KEY), tokens=7)

    rendered = metrics.render()
    assert 'apiwrapper_project_requests_total{project="projem"}' in rendered
    assert 'apiwrapper_project_tokens_total{project="projem"}' in rendered
    assert TEST_API_KEY not in rendered


def test_unknown_and_anonymous_identities_get_placeholders():
    registry = ProjectRegistry(make_settings())
    assert registry.label_for(None) == "(kimliksiz)"
    assert registry.label_for("anonymous") == "(kimliksiz)"
    assert registry.label_for("cli_yok") == "(etiketsiz)"


# --------------------------------------------------------------- üretim / iptal
def test_mint_adds_a_unique_key_and_binds_the_label():
    settings = make_settings()
    registry = ProjectRegistry(settings)
    key, key_id = registry.mint(settings, "proje a")

    assert key.startswith("sk-") and len(key) > 24
    assert settings.api_keys == [TEST_API_KEY, key]
    assert key_id == client_fingerprint(key)
    assert registry.label_for(key_id) == "proje-a"
    # Etiket alanı parmak izi taşır, anahtar değeri değil.
    assert f"{key_id}:proje-a" in settings.api_key_labels
    assert key not in settings.api_key_labels


def test_mint_rejects_an_empty_label():
    settings = make_settings()
    with pytest.raises(InvalidRequestError) as info:
        ProjectRegistry(settings).mint(settings, "  ,: ")
    assert info.value.code == "invalid_label"
    assert settings.api_keys == [TEST_API_KEY]


def test_revoke_drops_the_key_and_its_admin_right():
    settings = make_settings()
    registry = ProjectRegistry(settings)
    key, key_id = registry.mint(settings, "gecici")
    settings.admin_api_keys = [key]

    masked = registry.revoke(settings, key_id)

    assert key not in settings.api_keys
    assert settings.admin_api_keys == []
    assert key_id not in settings.api_key_labels
    assert key not in masked  # yanıt/log maskeli değeri taşır


def test_last_key_cannot_be_revoked():
    """Son anahtarı silmek wrapper'ı herkese kapatırdı — reddedilir."""
    settings = make_settings()
    with pytest.raises(InvalidRequestError) as info:
        ProjectRegistry(settings).revoke(settings, client_fingerprint(TEST_API_KEY))
    assert info.value.code == "last_key_cannot_be_revoked"
    assert settings.api_keys == [TEST_API_KEY]


def test_unknown_key_id_is_reported():
    settings = make_settings()
    registry = ProjectRegistry(settings)
    registry.mint(settings, "birinci")
    with pytest.raises(InvalidRequestError) as info:
        registry.revoke(settings, "cli_yokboyle")
    assert info.value.code == "key_not_found"


# ------------------------------------------------------------------- API yüzeyi
def test_key_list_never_contains_a_plaintext_key(client):
    response = client.get("/v1/admin/keys")
    assert response.status_code == 200
    assert TEST_API_KEY not in response.text
    (row,) = response.json()["data"]
    assert row["label"] == "(etiketsiz)"
    assert row["masked_key"].startswith("sk-")


def test_minted_key_works_and_revocation_is_immediate(client):
    payload = client.post("/v1/admin/keys", json={"label": "proje x"}).json()
    key = payload["api_key"]
    assert payload["key_id"] == client_fingerprint(key)
    headers = {"Authorization": f"Bearer {key}"}

    # Redeploy yok: yeni anahtar anında geçerli.
    assert client.get("/v1/models", headers=headers).status_code == 200

    revoked = client.delete(f"/v1/admin/keys/{payload['key_id']}")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert key not in revoked.text

    assert client.get("/v1/models", headers=headers).status_code == 401


def test_mint_requires_a_label(client):
    response = client.post("/v1/admin/keys", json={})
    assert response.status_code == 422


@respx.mock
def test_completion_usage_is_attributed_to_the_project(client):
    """Tamamlama isteği proje hanesine yazılır: istek + token + son kullanım."""
    route = respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=ai_stream("Merhaba"))
    )
    payload = client.post("/v1/admin/keys", json={"label": "proje x"}).json()

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {payload['api_key']}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert response.status_code == 200
    assert route.called

    rows = client.get("/v1/admin/keys").json()["data"]
    row = next(item for item in rows if item["key_id"] == payload["key_id"])
    assert row["label"] == "proje-x"
    assert row["requests"] == 1
    assert row["tokens"] > 0
    assert row["errors"] == 0
    assert row["last_used"] is not None

    # Diğer projenin hanesi etkilenmez.
    other = next(item for item in rows if item["masked_key"] != row["masked_key"])
    assert other["requests"] == 0


@respx.mock
def test_failed_completion_is_counted_as_project_error(client):
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(502, text="bad gateway")
    )
    payload = client.post("/v1/admin/keys", json={"label": "kirilgan"}).json()

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {payload['api_key']}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert response.status_code >= 400

    rows = client.get("/v1/admin/keys").json()["data"]
    row = next(item for item in rows if item["key_id"] == payload["key_id"])
    assert row["requests"] == 1
    assert row["errors"] == 1


# ------------------------------------------------------------------ kalıcılık
def test_minted_keys_and_labels_survive_restart(tmp_path):
    """Free Render'da disk yok: anahtarlar durum dosyası + env ile taşınır."""
    state_file = tmp_path / "state.json"
    settings = make_settings(upstream_state_file=str(state_file))
    remember_seed(settings)
    registry = ProjectRegistry(settings)
    key, key_id = registry.mint(settings, "proje a")

    fresh = make_settings(upstream_state_file=str(state_file))
    remember_seed(fresh)
    changed = restore_runtime_state(fresh)

    assert "api_keys" in changed and "api_key_labels" in changed
    assert key in fresh.api_keys
    assert ProjectRegistry(fresh).label_for(key_id) == "proje-a"


def _write_state(
    state_file, *, api_keys: list[str], env_keys: list[str], cookie: str = ""
) -> None:
    """Durum dosyasını elle kurar (kimlik kararı atlanmış senaryolar için)."""
    from app.upstream.state_store import _STATE_VERSION

    state_file.write_text(
        json.dumps(
            {
                "version": _STATE_VERSION,
                "snapshot_version": 1,
                "target_domain": UPSTREAM_DOMAIN,
                "seed_hash": "farkli-tohum",
                "updated_at": 1_800_000_000.0,
                "source": "test",
                "upstream_cookie": cookie,
                "api_keys": ",".join(api_keys),
                "api_keys_env": ",".join(env_keys),
                "api_key_labels": "",
            }
        ),
        encoding="utf-8",
    )


def test_keys_survive_when_the_identity_state_is_skipped(tmp_path):
    """Yeni cURL deploy'u üretilmiş anahtarı düşürmemeli.

    Kimlik kararı (`_should_restore`) "env daha taze" deyip disk kaydını
    atlayabilir; anahtar listesi o karara bağlı değil, çünkü upstream kimliği
    değildir. Bağlı olsaydı her cURL yenilemesinde tüm tüketiciler 401 alırdı.
    """
    state_file = tmp_path / "state.json"
    _write_state(state_file, api_keys=[TEST_API_KEY, "sk-minted"], env_keys=[TEST_API_KEY])

    fresh = make_settings(upstream_state_file=str(state_file))
    changed = restore_runtime_state(fresh)

    assert "api_keys" in changed
    assert fresh.api_keys == [TEST_API_KEY, "sk-minted"]


def test_revoked_key_is_not_resurrected(tmp_path):
    """İptal niyeti env'den güçlüdür: env sync gecikse bile anahtar dönmez."""
    state_file = tmp_path / "state.json"
    _write_state(
        state_file,
        api_keys=[TEST_API_KEY],  # iptal sonrası kayıt
        env_keys=[TEST_API_KEY, "sk-revoked"],  # env sync yetişmemiş
    )

    fresh = make_settings(
        upstream_state_file=str(state_file), api_keys=[TEST_API_KEY, "sk-revoked"]
    )
    restore_runtime_state(fresh)

    assert fresh.api_keys == [TEST_API_KEY]


def test_operator_added_env_key_is_kept(tmp_path):
    """Operatörün env'e eklediği anahtar kayıt yüzünden kaybolmaz."""
    state_file = tmp_path / "state.json"
    _write_state(state_file, api_keys=[TEST_API_KEY, "sk-minted"], env_keys=[TEST_API_KEY])

    fresh = make_settings(
        upstream_state_file=str(state_file),
        api_keys=[TEST_API_KEY, "sk-operator"],
    )
    restore_runtime_state(fresh)

    assert fresh.api_keys == [TEST_API_KEY, "sk-minted", "sk-operator"]


def test_empty_api_keys_in_the_state_file_never_wipes_keys(tmp_path):
    """Güvenlik kilidi: bozuk/eksik dosya tüm anahtarları silemez."""
    state_file = tmp_path / "state.json"
    settings = make_settings(upstream_state_file=str(state_file))
    remember_seed(settings)
    assert save_runtime_state(settings, source="test") is True

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    payload["api_keys"] = ""
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    fresh = make_settings(upstream_state_file=str(state_file))
    remember_seed(fresh)
    changed = restore_runtime_state(fresh)

    assert "api_keys" not in changed
    assert fresh.api_keys == [TEST_API_KEY]


@pytest.mark.asyncio
async def test_render_env_sync_persists_keys_and_labels():
    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-test",
        api_key_labels="cli_x:proje-a",
    )
    updates: dict[str, tuple[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        body = request.read()
        # DELETE boş gövde taşır: `json.loads("")` patlar (bkz. PR #54 dersi).
        value = json.loads(body)["value"] if request.method != "DELETE" else None
        updates[key] = (request.method, value)
        return httpx.Response(200, json={"value": "stored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is True

    assert updates["API_KEYS"] == ("PUT", TEST_API_KEY)
    assert updates["API_KEY_LABELS"] == ("PUT", "cli_x:proje-a")


# ------------------------------------------------------- admin ayrımı (güvenlik)
def test_mint_warns_when_admin_keys_are_not_separated(capsys):
    """`ADMIN_API_KEYS` boşken tüketici anahtarı admin yetkisi de taşır."""
    settings = make_settings()
    key, _ = ProjectRegistry(settings).mint(settings, "proje")
    out = capsys.readouterr().out

    assert "project_key_minted_without_admin_separation" in out
    assert key not in out  # uyarı da anahtar değerini taşımaz


def test_mint_is_silent_and_consumer_key_is_not_admin(capsys):
    settings = make_settings(admin_api_keys=[TEST_API_KEY])
    registry = ProjectRegistry(settings)
    key, key_id = registry.mint(settings, "proje")
    out = capsys.readouterr().out

    assert "project_key_minted_without_admin_separation" not in out
    rows = {row["key_id"]: row for row in registry.snapshot(settings)}
    assert rows[key_id]["is_admin"] is False
    assert rows[client_fingerprint(TEST_API_KEY)]["is_admin"] is True
    assert key not in json.dumps(rows)


def test_key_list_tells_the_truth_about_admin_rights(client):
    payload = client.get("/v1/admin/keys").json()
    # conftest `admin_api_keys` vermez → ayrım yok → `require_admin` herkesi
    # geçirir. Liste bunu gizlememeli, açıkça söylemeli.
    assert payload["admin_separation"] is False
    assert all(row["is_admin"] is True for row in payload["data"])
    assert "ADMIN_API_KEYS" in payload["hint"]
