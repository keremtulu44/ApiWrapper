"""Hesap rotasyonu: "hangi hesap" kararının sıra kuralı.

Kullanıcı sözleşmesi: iki Max hesabı ile bir Agent modu aynı anda aktiftir;
dışarıdan gelen istek bir hesaba, Agent işi kendi hesabına gider; "40 dk önce
verilen metni 1. Max yazdıysa şimdi 2. Max yazsın". **Upstream istek biçimine
(gövde, başlık, akış) dokunulmaz** — bu dosya yalnız hesap seçimini sınar.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.account import Account, AccountPool
from app.services.account_rotation import AccountRotator, logical_message_key
from app.services.lanes import Lane, LaneSet
from app.services.request_queue import RequestQueue
from app.services.upstream_health import UpstreamHealth
from tests.conftest import make_settings


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def build_set(rotator: AccountRotator | None, count: int = 3) -> LaneSet:
    return LaneSet(build_lanes(count), failover=True, cooldown=lambda slot: 0.0, rotation=rotator)


def _settings(**kwargs: object):
    return make_settings(**kwargs)


# --------------------------------------------------------------------- kurulum
def test_rotation_is_off_by_default_and_behaviour_is_unchanged() -> None:
    """Rotasyon varsayılan KAPALI: karar yine yük dengesiyle verilir."""
    settings = _settings()
    assert settings.account_rotation_enabled is False
    rotator = AccountRotator.from_settings(settings)
    assert rotator.enabled is False  # motor kurulsa bile karar vermez

    lane_set = build_set(None)
    first = lane_set.choose("owner")
    second = lane_set.choose("owner")
    # Eski davranış: yük dengesi en boş hattı seçer (1 → 2 → 3 sırayla).
    assert (first.slot, second.slot) == (1, 2)
    assert lane_set.last_decision == {}  # rotasyon kararı yok


def test_setting_parsing_is_forgiving() -> None:
    """Bozuk ayar rotasyonu sessizce öldürmemeli; geçerli kısım ayıklanır."""
    settings = _settings(
        account_rotation_enabled=True,
        account_rotation_strict_slots="1, 2",
        account_rotation_excluded_slots="",
        account_rotation_weights="1:2,2:1",
        account_rotation_agent_slot=3,
    )
    rotator = AccountRotator.from_settings(settings)
    assert rotator.enabled is True
    assert rotator.strict_slots == (1, 2)
    assert rotator.weights == {1: 2, 2: 1}
    assert rotator.agent_slot == 3


# ------------------------------------------------------------------ sıra kuralı
def test_round_robin_alternates_between_max_accounts() -> None:
    """Art arda gelen istekler 1 → 2 → 1 … döner (yük dengesinden bağımsız)."""
    rotator = AccountRotator(enabled=True, mode="round_robin", strict_slots=(1, 2))
    lanes = build_set(rotator)
    sequence = [lanes.choose("o", rotation_message_key=f"m{i}").slot for i in range(4)]
    assert sequence == [1, 2, 1, 2]
    assert lanes.last_decision["rule"] == "round_robin"


def test_same_message_never_goes_to_same_account_twice() -> None:
    """Kullanıcının örneği: aynı metin ikinci kez gelirse **öbür** Max yazar."""
    rotator = AccountRotator(enabled=True, mode="round_robin", strict_slots=(1, 2))
    lanes = build_set(rotator)
    key = logical_message_key(
        messages=[{"role": "user", "content": "Aynı metin"}], model="max"
    )
    first = lanes.choose("o", rotation_message_key=key).slot
    second = lanes.choose("o", rotation_message_key=key).slot
    third = lanes.choose("o", rotation_message_key=key).slot
    assert first != second
    assert third == first  # sıra korunuyor, yalnız üst üste aynı hesap yok


def test_strict_slots_keep_spare_account_out_of_day_traffic() -> None:
    """"Birinci hesap tamamen bitmesin": yedek yuva gündüz rotasyonuna girmez."""
    rotator = AccountRotator(enabled=True, mode="round_robin", strict_slots=(1, 2))
    lanes = build_set(rotator)
    slots = {lanes.choose("o", rotation_message_key=f"k{i}").slot for i in range(10)}
    assert slots == {1, 2}
    assert 3 not in slots


def test_excluded_slot_never_receives_requests() -> None:
    """Gece Agent hesabı Max isteği almamalı."""
    rotator = AccountRotator(
        enabled=True, mode="round_robin", excluded_slots=(3,)
    )
    lanes = build_set(rotator)
    slots = {lanes.choose("o", rotation_message_key=f"e{i}").slot for i in range(8)}
    assert slots == {1, 2}


def test_all_configured_slots_rotate_when_no_restriction() -> None:
    """Kısıt yoksa üç hesap birlikte döner (yedek de trafiğe girer)."""
    rotator = AccountRotator(enabled=True, mode="round_robin")
    lanes = build_set(rotator)
    sequence = [lanes.choose("o", rotation_message_key=f"r{i}").slot for i in range(6)]
    assert sequence == [1, 2, 3, 1, 2, 3]


def test_agent_source_excludes_agent_slot() -> None:
    """Agent işi kendi yuvasını Max tarafında kullanmaz (roller çakışmasın)."""
    rotator = AccountRotator(enabled=True, mode="round_robin", agent_slot=3)
    lanes = build_set(rotator)
    slots = {
        lanes.choose("o", rotation_source="agent", rotation_message_key=f"a{i}").slot
        for i in range(6)
    }
    assert 3 not in slots


def test_weights_give_primary_account_more_work() -> None:
    """`1:2,2:1` → 1. hesap iki katı istek alır (deterministik)."""
    rotator = AccountRotator(enabled=True, mode="weighted", weights={1: 2, 2: 1})
    lanes = build_set(rotator, count=2)
    slots = [lanes.choose("o", rotation_message_key=f"w{i}").slot for i in range(9)]
    assert slots.count(1) == 6
    assert slots.count(2) == 3


def test_sticky_conversation_keeps_its_account() -> None:
    """Aynı sohbet iki hesaba bölünmez: geçmişin yarısını görmesin."""
    rotator = AccountRotator(enabled=True, mode="round_robin")
    lanes = build_set(rotator)
    first = lanes.choose("o", sticky_key="conv-1", rotation_message_key="m1")
    second = lanes.choose("o", sticky_key="conv-1", rotation_message_key="m2")
    assert first.slot == second.slot
    assert lanes.last_decision["rule"] == "sticky"


def test_pinned_slot_bypasses_rotation() -> None:
    """`x-apiwrapper-account` sabitlemesi rotasyonu ezer (probe/elle sınama)."""
    rotator = AccountRotator(enabled=True, mode="round_robin")
    lanes = build_set(rotator)
    lane = lanes.choose("o", pinned_slot=3, rotation_message_key="p1")
    assert lane.slot == 3
    assert lanes.last_decision["rule"] == "pinned"


def test_rotation_respects_health_and_falls_back() -> None:
    """Seçilen hesap arızalıysa rotasyon zorlamaz; yük dengesine döner."""
    rotator = AccountRotator(enabled=True, mode="round_robin")
    lanes = build_set(rotator)
    # 1. hattı arızalı işaretle: rotasyon 1'i istese de 2'ye gidilmeli.
    lanes.get(1).health.record_failure("upstream_refresh_required", 300.0)
    chosen = [lanes.choose("o", rotation_message_key=f"h{i}").slot for i in range(4)]
    assert 1 not in chosen


def test_health_getter_observability() -> None:
    """`/hesaplar` için okunur özet: kural, mod, yuvalar (secret yok)."""
    rotator = AccountRotator(enabled=True, mode="round_robin", strict_slots=(1, 2))
    info = rotator.describe()
    assert info["enabled"] is True
    assert info["mode"] == "round_robin"
    assert info["strict_slots"] == [1, 2]
    assert "cookie" not in str(info).lower()


def test_message_key_ignores_account_and_is_content_based() -> None:
    """Aynı içerik aynı anahtar; farklı içerik farklı anahtar (hash, metin yok)."""
    a = logical_message_key(messages=[{"role": "user", "content": "Merhaba"}], model="max")
    b = logical_message_key(messages=[{"role": "user", "content": "Merhaba"}], model="max")
    c = logical_message_key(messages=[{"role": "user", "content": "Başka"}], model="max")
    assert a == b
    assert a != c
    assert "Merhaba" not in a  # içerik sızmıyor, yalnız özet


@pytest.mark.parametrize("mode", ["round_robin", "weighted", "least_loaded"])
def test_all_modes_never_pick_unconfigured_slot(mode: str) -> None:
    """Yapılandırılmamış yuva asla seçilmez (tek hesapta tek hat)."""
    settings = _settings(account_rotation_enabled=True, account_rotation_mode=mode)
    pool = AccountPool(settings, [Account(slot=1, name="hesap-1", cookie="x")])
    lanes = LaneSet.from_settings(settings, pool)
    slots = {lanes.choose("o", rotation_message_key=f"z{i}").slot for i in range(5)}
    assert slots == {1}
    assert lanes.rotation is not None  # açıkken motor kurulur
    assert lanes.rotation.mode == mode


# --------------------------------------------------------------------------
# Upstream istek formatı: rotasyon açık/kapalı **bayt düzeyinde aynı** olmalı
# --------------------------------------------------------------------------
@respx.mock
def test_rotation_does_not_change_upstream_request_shape() -> None:
    """Rotasyon yalnız hesap seçer; gövde/başlık/akış biçimi değişmez.

    Kullanıcı sözleşmesi: "asla max'ın ve agentin istek gönderme formatına
    dokunma". Bu test, aynı isteğin rotasyon kapalıyken ve açıkken upstream'e
    **birebir aynı** başlık ve gövdeyle gittiğini doğrular; yalnız hangi
    hesabın (Authorization/cookie) cevapladığı değişir.
    """
    import re

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import TEST_API_KEY, ai_stream
    from tests.test_ask import STREAM_URL

    uuid_re = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    captured: list[tuple[dict[str, str], bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            (
                {
                    k.lower(): v
                    for k, v in request.headers.items()
                    if k.lower()
                    in {
                        "content-type",
                        "accept",
                        "accept-language",
                        "user-agent",
                        "referer",
                        "authorization",
                        "cookie",
                    }
                },
                request.content,
            )
        )
        return httpx.Response(200, content=ai_stream("cevap"))

    respx.post(url__startswith=STREAM_URL).mock(side_effect=handler)

    def run(rotation: bool) -> tuple[dict[str, str], bytes]:
        settings = _settings(
            upstream_access_token="TOKEN-ONE",
            upstream_account_name="bir",
            upstream_access_token_2="TOKEN-TWO",
            upstream_account_2_name="iki",
            account_rotation_enabled=rotation,
            account_rotation_strict_slots="1,2",
        )
        with TestClient(create_app(settings)) as client:
            client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
            resp = client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
            )
            assert resp.status_code == 200
        headers, body = captured[-1]
        # Hesap kimliği (hangi hesabın cevapladığı) bu karşılaştırmanın dışında.
        headers = {
            k: v for k, v in headers.items() if k not in {"authorization", "cookie"}
        }
        # Her yeni sohbet yeni bir kimlik (chat/mesaj id) üretir; bu rotasyonla
        # ilgili değildir. Biçimi karşılaştırmak için id'ler maskelenir.
        masked_headers = {k: uuid_re.sub(b"<id>", v.encode()).decode() for k, v in headers.items()}
        masked_body = uuid_re.sub(b"<id>", body)
        return masked_headers, masked_body

    off = run(rotation=False)
    on = run(rotation=True)
    assert on == off, "rotasyon upstream istek biçimini değiştirmemeli"


@respx.mock
def test_rotation_actually_changes_which_account_answers() -> None:
    """Aynı metin iki kez: ikinci isteği **öbür** Max hesabı cevaplar."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import TEST_API_KEY, ai_stream
    from tests.test_ask import STREAM_URL

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        seen.append(token)
        return httpx.Response(200, content=ai_stream("cevap"))

    respx.post(url__startswith=STREAM_URL).mock(side_effect=handler)

    settings = _settings(
        upstream_access_token="TOKEN-ONE",
        upstream_account_name="bir",
        upstream_access_token_2="TOKEN-TWO",
        upstream_account_2_name="iki",
        account_rotation_enabled=True,
        account_rotation_strict_slots="1,2",
    )
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "aynı metin"}],
    }
    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
        assert client.post("/v1/chat/completions", json=payload).status_code == 200

    assert seen == ["TOKEN-ONE", "TOKEN-TWO"]
    decision = client.app.state.lanes.last_decision
    assert decision["rule"] in {"round_robin", "sticky"}


# --------------------------------------------------------------------------
# Aynı anda üç rol: Max-1, Max-2 ve Agent (roller birbirine karışmaz)
# --------------------------------------------------------------------------
def test_three_roles_share_the_day_without_collision() -> None:
    """Gündüz akışı: dış istekler Max-1/Max-2 arasında döner, Agent kendi
    hesabında kalır; Agent'ın hesabı hiçbir Max isteğini almaz.

    Bu, kullanıcının tarif ettiği tablo: "dışarıdan istek gelince biri siteye,
    biri ajana çalışır" — roller ayrı, çakışma yok.
    """
    rotator = AccountRotator(
        enabled=True,
        mode="round_robin",
        strict_slots=(1, 2),   # Max tarafı: yalnız 1 ve 2
        excluded_slots=(3,),   # 3 = Agent hesabı, Max isteği almaz
        agent_slot=3,
    )
    lanes = build_set(rotator)

    external = [
        lanes.choose("o", rotation_source="api", rotation_message_key=f"d{i}").slot
        for i in range(6)
    ]
    assert external == [1, 2, 1, 2, 1, 2]
    assert 3 not in external

    agent = [
        lanes.choose("o", rotation_source="agent", rotation_message_key=f"g{i}").slot
        for i in range(4)
    ]
    assert 3 not in agent  # Agent işi de Agent yuvasını Max tarafında kullanmaz
    assert set(agent) <= {1, 2}


def test_second_max_answers_repeat_of_same_message_after_40_minutes() -> None:
    """Kullanıcının cümlesi birebir: aynı metni 1. Max yazdıysa sonraki 2. yazar.

    Saat ilerletilir (40 dk); içerik aynı olduğu için rotasyon aynı hesabı
    seçmez.
    """
    clock = Clock()
    rotator = AccountRotator(enabled=True, mode="round_robin", strict_slots=(1, 2))
    lanes = LaneSet(build_lanes(2), failover=True, cooldown=lambda s: 0.0, rotation=rotator)

    message = logical_message_key(
        messages=[{"role": "user", "content": "Aynı uzun metin"}], model="max"
    )
    first = lanes.choose("o", rotation_message_key=message).slot
    clock.advance(40 * 60)
    second = lanes.choose("o", rotation_message_key=message).slot

    assert first == 1
    assert second == 2
