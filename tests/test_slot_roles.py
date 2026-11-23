"""Yuva rolleri: üç hesap **açık kalır**, yalnız iş bölümü değişir.

Kullanıcı itirazı (haklı): "nasıl yani, hesabı verme artık olmayacak ki; 3 farklı
hesap kullanılacak". Doğrusu bu: hiçbir cURL/çerez silinmez, hiçbir yuva
kapatılmaz. Rol yalnız şunu belirler: bu hesap gündüz `max` trafiği mi alıyor,
sıcak yedek mi bekliyor, yoksa Agent projesinin mi?
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.main import create_app
from app.services.account import Account, AccountPool
from app.services.lanes import Lane, LaneSet
from app.services.request_queue import RequestQueue
from app.services.slot_roles import (
    ROLE_AGENT,
    ROLE_MAX,
    ROLE_SPARE,
    SlotRoles,
    normalize_role,
)
from app.services.upstream_health import UpstreamHealth
from tests.conftest import TEST_API_KEY, make_settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_lanes(count: int = 3) -> list[Lane]:
    clock = Clock()
    return [
        Lane(
            index,
            Account(slot=index + 1, name=f"hesap-{index + 1}", cookie="x"),
            RequestQueue(min_gap=0, max_gap=0, clock=clock, name=f"hesap-{index + 1}"),
            UpstreamHealth(300.0, clock=clock),
        )
        for index in range(count)
    ]


# ------------------------------------------------------------------ rol modeli
def test_roles_are_off_by_default() -> None:
    """Rol verilmezse tüm yuvalar `max`: davranış eskisiyle aynı."""
    roles = SlotRoles.from_settings(make_settings())
    assert roles.enabled is False
    assert roles.role_of(1) == ROLE_MAX
    assert roles.role_of(3) == ROLE_MAX
    assert "tanımsız" in roles.describe_line()


def test_env_roles_are_parsed() -> None:
    settings = make_settings(account_slot_roles="1:max,2:max,3:agent")
    roles = SlotRoles.from_settings(settings)
    assert roles.enabled is True
    assert roles.role_of(1) == ROLE_MAX
    assert roles.role_of(3) == ROLE_AGENT
    assert roles.keepalive_allowed(3) is False
    assert roles.keepalive_allowed(1) is True


def test_turkish_role_aliases_and_bad_values() -> None:
    """Türkçe adlar kabul edilir; bozuk değer **rolü patlatmaz**, ayıklanır."""
    assert normalize_role("Yedek") == ROLE_SPARE
    assert normalize_role("ajan") == ROLE_AGENT
    assert normalize_role("sacma") is None

    settings = make_settings(account_slot_roles="1:max,2:bozuk,uc:agent")
    roles = SlotRoles.from_settings(settings)
    assert roles.roles == {1: ROLE_MAX}  # geçersiz parçalar atıldı


# ------------------------------------------------------- üç hesap birlikte açık
def test_agent_slot_stays_open_but_takes_no_max_traffic() -> None:
    """Agent yuvası **açık kalır** (cURL/çerez yerinde) ama `max` işi almaz."""
    roles = SlotRoles(roles={1: ROLE_MAX, 2: ROLE_MAX, 3: ROLE_AGENT}, enabled=True)
    lanes = LaneSet(build_lanes(3), failover=True, cooldown=lambda s: 0.0, roles=roles)

    # Hesap hâlâ havuzda/hatta duruyor:
    assert lanes.get(3) is not None
    assert lanes.get(3).account is not None

    chosen = {lanes.choose("o").slot for _ in range(9)}
    assert chosen == {1, 2}, "agent yuvasına gündüz max isteği gitmemeli"
    # Keepalive: agent yuvasında yok, diğerlerinde var.
    assert roles.keepalive_allowed(3) is False
    assert roles.keepalive_allowed(1) is True


def test_spare_slot_is_open_and_warm_but_takes_no_day_traffic() -> None:
    """Yedek: trafik almaz, keepalive **yapılır** (acil durumda sıcak olsun)."""
    roles = SlotRoles(roles={1: ROLE_MAX, 2: ROLE_MAX, 3: ROLE_SPARE}, enabled=True)
    lanes = LaneSet(build_lanes(3), failover=True, cooldown=lambda s: 0.0, roles=roles)

    chosen = {lanes.choose("o").slot for _ in range(8)}
    assert chosen == {1, 2}
    assert roles.keepalive_allowed(3) is True  # sıcak bekliyor

    snapshot = {row["slot"]: row for row in lanes.snapshot()}
    assert snapshot[3]["role"] == ROLE_SPARE
    assert snapshot[3]["keepalive"] is True


def test_swap_roles_without_closing_any_account() -> None:
    """Rol değişimi: hesap kapanmaz, trafik yeni role göre akar."""
    roles = SlotRoles(roles={1: ROLE_MAX, 2: ROLE_MAX, 3: ROLE_AGENT}, enabled=True)
    lanes = LaneSet(build_lanes(3), failover=True, cooldown=lambda s: 0.0, roles=roles)
    assert {lanes.choose("o").slot for _ in range(6)} == {1, 2}

    # 3. hesabı Max trafiğine al, 2. hesabı Agent'a ver — hiçbiri kapanmıyor.
    roles.set_role(3, "max")
    roles.set_role(2, "agent")
    assert lanes.get(2).account is not None
    assert lanes.get(3).account is not None
    assert {lanes.choose("o").slot for _ in range(8)} == {1, 3}


def test_all_accounts_blocked_by_roles_does_not_crash() -> None:
    """Tüm yuvalar `agent` olsa bile dağıtıcı çökmez (güvenli geri düşüş)."""
    roles = SlotRoles(roles={1: ROLE_AGENT, 2: ROLE_AGENT}, enabled=True)
    lanes = LaneSet(build_lanes(2), failover=True, cooldown=lambda s: 0.0, roles=roles)
    lane = lanes.choose("o")  # istisna yok; seçim yine bir hat döndürür
    assert lane.slot in {1, 2}


# ------------------------------------------------------------------ canlı uç
@pytest.mark.asyncio
async def test_role_endpoint_keeps_account_configured() -> None:
    """`POST .../{slot}/role`: rol değişir, hesap **konfigüre** kalır."""
    settings = make_settings(
        upstream_access_token="TOKEN-ONE",
        upstream_account_name="bir",
        upstream_access_token_2="TOKEN-TWO",
        upstream_account_2_name="iki",
        upstream_access_token_3="TOKEN-THREE",
        upstream_account_3_name="uc",
        upstream_keepalive_interval_seconds=0.0,
        account_slot_roles="1:max,2:max,3:max",
    )
    app = create_app(settings)
    import httpx

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.internal",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        ) as client,
    ):
        resp = await client.post(
            "/v1/autopilot/upstream-slots/3/role", json={"role": "agent"}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["slot"] == 3
        assert data["role"] == "agent"
        assert data["keepalive_allowed"] is False
        assert "açık kaldı" in data["note"]

        status = (await client.get("/v1/autopilot/upstream-slots")).json()
        slot3 = next(s for s in status["slots"] if s["slot"] == 3)
        # Hesap hâlâ yapılandırılmış ve açık; yalnız rolü değişti.
        assert slot3["configured"] is True
        assert slot3["role"] == "agent"
        assert status["slot_roles"]["roles"]["3"] == "agent"


@pytest.mark.asyncio
async def test_role_on_empty_slot_is_rejected_clearly() -> None:
    """Rol vermek hesap yaratmaz: boş yuvada açık hata döner."""
    settings = make_settings(
        upstream_access_token="TOKEN-ONE",
        upstream_account_name="bir",
        upstream_keepalive_interval_seconds=0.0,
    )
    app = create_app(settings)
    import httpx

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.internal",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        ) as client,
    ):
        resp = await client.post(
            "/v1/autopilot/upstream-slots/2/role", json={"role": "agent"}
        )
        assert resp.status_code in (400, 422)
        assert "yapılandırılmış hesap yok" in resp.text


def test_settings_default_has_no_roles() -> None:
    """Varsayılan `Settings` rol vermez (üç yuva da `max`)."""
    settings = Settings(_env_file=None, api_keys=["k"])
    assert settings.account_slot_roles == ""
    assert SlotRoles.from_settings(settings).enabled is False


def test_lane_snapshot_exposes_role_and_keepalive() -> None:
    """`/hesaplar` için: her hattın rolü ve keepalive izni görünür."""
    roles = SlotRoles(roles={1: ROLE_MAX, 2: ROLE_SPARE}, enabled=True)
    lanes = LaneSet(build_lanes(2), failover=True, cooldown=lambda s: 0.0, roles=roles)
    rows = lanes.snapshot()
    assert [row["role"] for row in rows] == [ROLE_MAX, ROLE_SPARE]
    assert [row["keepalive"] for row in rows] == [True, True]


def test_account_pool_still_reports_three_accounts() -> None:
    """Rol vermek hesabı havuzdan düşürmez: üç hesap üç hesap kalır."""
    settings = make_settings(
        upstream_access_token="TOKEN-ONE",
        upstream_account_name="bir",
        upstream_access_token_2="TOKEN-TWO",
        upstream_account_2_name="iki",
        upstream_access_token_3="TOKEN-THREE",
        upstream_account_3_name="uc",
        account_slot_roles="1:max,2:max,3:agent",
    )
    pool = AccountPool(
        settings,
        [
            Account(slot=1, name="bir", cookie="x"),
            Account(slot=2, name="iki", cookie="x"),
            Account(slot=3, name="uc", cookie="x"),
        ],
    )
    assert pool.size == 3


# --------------------------------------------------------------------------
# 2. Max cURL'ü yerleştirme aracı (scripts/put_curl2.py)
# --------------------------------------------------------------------------
def test_put_curl2_accepts_valid_second_account_curl() -> None:
    """Geçerli 2. hesap cURL'ü kabul edilir; model kimliği kendi gövdesinden okunur."""
    import importlib.util
    from pathlib import Path

    from tests.test_upstream_slots import _build_realistic_max_curl

    spec = importlib.util.spec_from_file_location(
        "put_curl2", Path(__file__).resolve().parent.parent / "scripts" / "put_curl2.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    curl = _build_realistic_max_curl(sub="ikinci-hesap")
    ok, problems, values = mod.check(curl, env={})
    assert ok, problems
    assert values["identity"] == "ikinci-hesap"
    assert values["model_id"] == "019c-max-model-v1"  # 1. yuva boş olsa bile
    assert values["has_cookie"] == "evet"


def test_put_curl2_rejects_agent_curl_for_max_slot() -> None:
    """Agent cURL'ü Max yuvasına konmaz (karışırsa oturumlar çakışır)."""
    import importlib.util
    from pathlib import Path

    from tests.test_autopilot import _build_windows_cmd_input_curl

    spec = importlib.util.spec_from_file_location(
        "put_curl2", Path(__file__).resolve().parent.parent / "scripts" / "put_curl2.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ok, problems, _ = mod.check(_build_windows_cmd_input_curl(sub="ajan"), env={})
    assert ok is False
    assert any("Agent Mode" in item for item in problems)


def test_put_curl2_writes_only_slot_two_lines(tmp_path) -> None:
    """Yazma yalnız 2. yuvaya dokunur; 1. hesap ve diğer satırlar aynen kalır."""
    import importlib.util
    from pathlib import Path


    spec = importlib.util.spec_from_file_location(
        "put_curl2", Path(__file__).resolve().parent.parent / "scripts" / "put_curl2.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_KEYS=sk-test\nTARGET_DOMAIN=eski.test\nUPSTREAM_CURL_2=eski-deger\n",
        encoding="utf-8",
    )
    values = {
        "curl_env": "UPSTREAM_CURL_2=curl 'https://yeni.test/x' \\\n  -b 'a=b'",
        "target_domain": "yeni.test",
    }
    backup, replaced = mod.write_env(env_file, values)
    text = env_file.read_text(encoding="utf-8")

    assert backup.exists()  # yedek alındı
    assert "API_KEYS=sk-test" in text  # dokunulmadı
    assert "TARGET_DOMAIN=eski.test" in text  # 1. hesap alanı korundu
    assert "TARGET_DOMAIN_2=yeni.test" in text
    assert "eski-deger" not in text  # 2. yuva güncellendi
    assert set(replaced) == {"UPSTREAM_CURL_2", "TARGET_DOMAIN_2"}
