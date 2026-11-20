"""Hesap hatları: isteklerin hesaplar arasında bölüşümü ve arıza kuralları.

Burada yalnız dağıtıcının kararı sınanır (saat sahte, upstream yok). Aynı
kuralların HTTP üzerinden ve gerçek soketlerdeki sonuçları
`tests/test_two_accounts.py` ve `tests/test_two_accounts_live.py` içindedir.
"""

from __future__ import annotations

import pytest

from app.services.account import Account, AccountPool
from app.services.lanes import (
    RETRY_FAILING_WITHOUT_PROBE_SECONDS,
    Lane,
    LaneSet,
    LaneUnavailable,
)
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


def build(
    count: int = 2,
    *,
    failover: bool = True,
    retry_failing_after: float = 0.0,
    hold: float = 300.0,
) -> tuple[LaneSet, Clock, dict[int, float]]:
    """`count` hesaplı bir küme; saat ve kota dinlenmeleri testin elinde."""
    clock = Clock()
    cooling: dict[int, float] = {}
    lanes = [
        Lane(
            index,
            Account(slot=index + 1, name=f"hesap-{index + 1}", cookie="x"),
            RequestQueue(min_gap=0, max_gap=0, clock=clock, name=f"hesap-{index + 1}"),
            UpstreamHealth(hold, clock=clock),
        )
        for index in range(count)
    ]
    lane_set = LaneSet(
        lanes,
        failover=failover,
        cooldown=lambda slot: cooling.get(slot, 0.0),
        retry_failing_after=retry_failing_after,
    )
    return lane_set, clock, cooling


def slots(lane_set: LaneSet, count: int, **kwargs) -> list[int]:
    return [lane_set.choose(object(), **kwargs).slot for _ in range(count)]


# ------------------------------------------------------------------ bölüşüm
def test_requests_alternate_between_two_idle_accounts():
    """Tek tek gelen istekler sırayla değişir: trafik yarı yarıya bölünür."""
    lanes, _, _ = build()
    assert slots(lanes, 6) == [1, 2, 1, 2, 1, 2]
    assert [lane.assigned for lane in lanes.lanes] == [3, 3]


def test_a_hundred_requests_split_fifty_fifty():
    lanes, _, _ = build()
    slots(lanes, 100)
    assert [lane.assigned for lane in lanes.lanes] == [50, 50]


def test_three_accounts_share_evenly():
    lanes, _, _ = build(3)
    assert slots(lanes, 6) == [1, 2, 3, 1, 2, 3]


async def test_a_burst_goes_to_the_less_busy_account():
    """Bir hesap meşgulken gelen istek boştaki hesaba gider (yük, sıradan önce gelir)."""
    lanes, _, _ = build()
    first = lanes.choose(object())
    ticket = await first.queue.acquire("ask")  # 1. hesap bir isteği işliyor
    assert first.slot == 1

    assert slots(lanes, 1) == [2]  # sıra 2. hesapta
    # Sıra yine 1. hesapta olsa da o meşgul; 2. hesap boşta.
    assert slots(lanes, 1) == [2]

    first.queue.release(ticket)
    assert slots(lanes, 1) == [1]  # yük eşitlendi: en uzun süredir iş almayan


def test_single_account_always_gets_the_request():
    lanes, _, _ = build(1)
    assert slots(lanes, 3) == [1, 1, 1]
    assert lanes.multi is False
    assert lanes.scope(lanes.primary) == ""  # tek hesapta bildirimler hesap adı taşımaz


# -------------------------------------------------------------------- arıza
def test_a_failing_account_gets_no_new_work():
    lanes, _, _ = build()
    lanes.get(1).health.record_failure("upstream_unauthorized")

    assert slots(lanes, 4) == [2, 2, 2, 2]
    assert slots(lanes, 4, respect_hold=True) == [2, 2, 2, 2]
    assert lanes.usable(lanes.get(1)) is False
    assert lanes.others_usable(lanes.get(1)) is True


def test_the_account_returns_to_rotation_after_a_success():
    """Arızalı hesabı kendi probe'u (ya da sabitlenmiş bir istek) geri getirir."""
    lanes, _, _ = build()
    lanes.get(1).health.record_failure("upstream_timeout")
    assert slots(lanes, 2) == [2, 2]

    lanes.get(1).health.record_success()

    assert slots(lanes, 4) == [1, 2, 1, 2]


def test_failing_account_is_not_retried_with_real_requests_while_probes_run():
    """Keepalive açıkken (varsayılan kurulum) bekletme dolsa bile gerçek istek denenmez.

    Yoksa takılı kalan bir hesap, payına düşen her isteği süre sınırına kadar
    bekletip "problem"e düşürürdü; öbür hesap çalışırken bu gereksiz.
    """
    lanes, clock, _ = build(retry_failing_after=0.0)
    lanes.get(1).health.record_failure("upstream_timeout")
    clock.advance(10_000)  # bekletme çoktan doldu

    assert slots(lanes, 4, respect_hold=True) == [2, 2, 2, 2]
    assert slots(lanes, 2) == [2, 2]


def test_without_probes_a_failing_account_is_retried_after_a_long_pause():
    """Keepalive kapalıyken hesabı geri getirecek probe yoktur: seyrek gerçek deneme."""
    lanes, clock, _ = build(retry_failing_after=1800.0)
    lanes.get(1).health.record_failure("upstream_failure")

    clock.advance(1799)
    assert slots(lanes, 2, respect_hold=True) == [2, 2]

    clock.advance(2)
    owner = object()
    assert lanes.choose(owner, respect_hold=True).slot == 1  # deneme isteği
    # Deneme sürerken ikinci bir istek aynı hesaba yığılmaz.
    assert lanes.choose(object(), respect_hold=True).slot == 2
    assert lanes.choose(object(), respect_hold=True).slot == 2

    lanes.get(1).health.record_failure("upstream_failure")  # deneme de başarısız
    assert slots(lanes, 2, respect_hold=True) == [2, 2]  # yine uzun bekleyiş


def test_an_account_resting_on_quota_is_skipped():
    """Kota dinlenmesindeki hesaba istek gönderilmez (kilidi uzatır)."""
    lanes, _, cooling = build()
    cooling[2] = 600.0

    assert slots(lanes, 3) == [1, 1, 1]
    assert lanes.usable(lanes.get(2)) is False

    cooling[2] = 0.0
    assert 2 in slots(lanes, 2)


# ------------------------------------------------- hiçbir hesap çalışmıyorken
def test_ask_is_refused_at_once_when_every_account_is_held():
    lanes, clock, _ = build()
    lanes.get(1).health.record_failure("upstream_unauthorized")
    clock.advance(100)
    lanes.get(2).health.record_failure("upstream_quota_reached")

    with pytest.raises(LaneUnavailable) as caught:
        lanes.choose(object(), respect_hold=True)

    # En erken açılacak hesap bildirilir: 1. hesabın bekletmesi 100 sn önce başladı.
    assert caught.value.lane is lanes.get(1)
    assert caught.value.hold.code == "upstream_unauthorized"
    assert caught.value.hold.remaining == pytest.approx(200.0)
    assert [lane.assigned for lane in lanes.lanes] == [0, 0]  # kimseye iş verilmedi


def test_chat_requests_are_still_attempted_when_every_account_is_failing():
    """Sohbet uçları (Telegram, probe) bekletmeye bakmaz; hesaplar sırayla denenir."""
    lanes, _, _ = build()
    for lane in lanes.lanes:
        lane.health.record_failure("upstream_timeout")

    assert slots(lanes, 4) == [1, 2, 1, 2]


def test_one_trial_per_account_once_the_hold_expires_and_nobody_works():
    """Çalışan hesap kalmadıysa tek hesaptaki kural geçerlidir: bekletme dolunca tek deneme."""
    lanes, clock, _ = build()
    for lane in lanes.lanes:
        lane.health.record_failure("upstream_failure")
    clock.advance(301)

    first, second = object(), object()
    assert lanes.choose(first, respect_hold=True).slot == 1
    assert lanes.choose(second, respect_hold=True).slot == 2
    with pytest.raises(LaneUnavailable) as caught:
        lanes.choose(object(), respect_hold=True)
    assert caught.value.hold.trial_in_flight is True

    # Deneme başarılı olursa hesap yeniden herkese açılır.
    lanes.get(2).health.record_success()
    assert slots(lanes, 3, respect_hold=True) == [2, 2, 2]


def test_resting_account_reports_its_cooldown_as_the_wait():
    """Bekletme dolmuş ama hesap dinleniyorsa kalan süre dinlenme süresidir."""
    lanes, clock, cooling = build(1)
    lanes.primary.health.record_failure("upstream_quota_reached")
    cooling[1] = 900.0
    clock.advance(400)  # bekletme (300 sn) doldu, dinlenme sürüyor

    with pytest.raises(LaneUnavailable) as caught:
        lanes.choose(object(), respect_hold=True)

    assert caught.value.hold.code == "upstream_quota_reached"
    assert caught.value.hold.remaining == pytest.approx(900.0)


def test_single_account_keeps_the_half_open_rule():
    lanes, clock, _ = build(1)
    health = lanes.primary.health
    health.record_failure("upstream_timeout")

    with pytest.raises(LaneUnavailable):
        lanes.choose(object(), respect_hold=True)
    assert lanes.choose(object()).slot == 1  # sohbet isteği yine denenir

    clock.advance(301)
    owner = object()
    assert lanes.choose(owner, respect_hold=True).slot == 1
    with pytest.raises(LaneUnavailable) as caught:
        lanes.choose(object(), respect_hold=True)
    assert caught.value.hold.trial_in_flight is True


# ------------------------------------------------------------ katı bölüşüm
def test_strict_split_keeps_alternating_even_when_one_account_fails():
    """`ACCOUNT_FAILOVER=false`: arızalı hesabın payı başka hesaba verilmez."""
    lanes, _, _ = build(failover=False)
    lanes.get(1).health.record_failure("upstream_unauthorized")

    outcomes: list[str] = []
    for _ in range(6):
        try:
            outcomes.append(str(lanes.choose(object(), respect_hold=True).slot))
        except LaneUnavailable as exc:
            assert exc.lane is lanes.get(1)
            outcomes.append("problem")

    assert outcomes == ["problem", "2", "problem", "2", "problem", "2"]
    assert lanes.alternative([1]) is None  # katı bölüşümde istek taşınmaz


# ---------------------------------------------------------- sabitleme/taşıma
def test_pinned_request_goes_to_its_account_whatever_its_health():
    lanes, _, _ = build()
    lanes.get(2).health.record_failure("upstream_unauthorized")

    assert slots(lanes, 3, pinned_slot=2) == [2, 2, 2]
    assert slots(lanes, 1, pinned_slot=2, respect_hold=True) == [2]
    with pytest.raises(LookupError):
        lanes.choose(object(), pinned_slot=7)


def test_pinned_requests_do_not_disturb_the_split():
    """Probe bölüşümün parçası değildir: ne sayılır ne sırayı kaydırır."""
    lanes, _, _ = build()
    assert slots(lanes, 1) == [1]

    slots(lanes, 3, pinned_slot=2)  # 2. hesabın probe'ları

    assert [lane.assigned for lane in lanes.lanes] == [1, 0]
    assert slots(lanes, 3) == [2, 1, 2]  # sıra yine 2. hesaptaydı


def test_alternative_offers_only_a_working_account_not_yet_tried():
    lanes, _, _ = build(3)
    assert lanes.alternative([1]).slot == 2
    lanes.get(2).health.record_failure("upstream_failure")
    assert lanes.alternative([1]).slot == 3
    assert lanes.alternative([1, 3]) is None  # kalan tek hesap arızalı
    assert build(1)[0].alternative([]) is None


# -------------------------------------------------------- sohbet bağlılığı
def test_a_reused_conversation_stays_on_one_account():
    """Upstream sohbeti hesaba aittir: aynı sohbet iki hesaba bölünmez."""
    lanes, _, _ = build()
    assert slots(lanes, 4, sticky_key="client:telegram") == [1, 1, 1, 1]
    # Başka bir sohbet öbür hesaba düşer (yük oraya kaydı için).
    assert slots(lanes, 3, sticky_key="client:other") == [2, 2, 2]
    # Bağımsız istekler bölüşmeye devam eder.
    assert sorted(slots(lanes, 2)) == [1, 2]


def test_a_conversation_moves_when_its_account_fails_and_stays_there():
    lanes, _, _ = build()
    assert slots(lanes, 2, sticky_key="conv:a") == [1, 1]

    lanes.get(1).health.record_failure("upstream_unauthorized")
    assert slots(lanes, 2, sticky_key="conv:a") == [2, 2]

    lanes.get(1).health.record_success()
    assert slots(lanes, 2, sticky_key="conv:a") == [2, 2]  # geri zıplamaz


def test_sticky_map_is_bounded():
    clock = Clock()
    lanes = LaneSet(
        [
            Lane(i, Account(slot=i + 1, name=f"h{i + 1}", cookie="x"), RequestQueue(clock=clock),
                 UpstreamHealth(clock=clock))
            for i in range(2)
        ],
        sticky_size=3,
    )
    for index in range(10):
        lanes.choose(object(), sticky_key=f"conv:{index}")
    assert len(lanes._sticky) == 3


# ----------------------------------------------------------- kurulum/çıktı
def test_one_lane_per_configured_account():
    settings = make_settings(upstream_cookie="A", upstream_cookie_2="B")
    lanes = LaneSet.from_settings(settings, AccountPool(settings))

    assert [lane.label for lane in lanes.lanes] == ["hesap-1", "hesap-2"]
    assert [lane.queue.name for lane in lanes.lanes] == ["hesap-1", "hesap-2"]
    assert lanes.lanes[0].queue is not lanes.lanes[1].queue
    assert lanes.lanes[0].health is not lanes.lanes[1].health
    assert lanes.failover is True


def test_single_and_no_account_setups_get_one_unnamed_lane():
    single = make_settings(upstream_cookie="A")
    lanes = LaneSet.from_settings(single, AccountPool(single))
    assert len(lanes) == 1 and lanes.primary.label == "hesap-1"
    assert lanes.primary.queue.name == ""  # metrik ve log adları tek sıralı kurulumla aynı

    legacy = make_settings()
    lanes = LaneSet.from_settings(legacy, AccountPool(legacy))
    assert len(lanes) == 1 and lanes.primary.account is None and lanes.primary.slot == 0


def test_real_request_retry_is_only_enabled_without_keepalive():
    with_probe = make_settings(
        upstream_cookie="A", upstream_cookie_2="B", upstream_keepalive_interval_seconds=3600
    )
    assert LaneSet.from_settings(with_probe, AccountPool(with_probe)).retry_failing_after == 0.0

    without = make_settings(upstream_cookie="A", upstream_cookie_2="B")
    lanes = LaneSet.from_settings(without, AccountPool(without))
    assert lanes.retry_failing_after == RETRY_FAILING_WITHOUT_PROBE_SECONDS


def test_failover_setting_is_read_from_settings():
    settings = make_settings(upstream_cookie="A", upstream_cookie_2="B", account_failover=False)
    assert LaneSet.from_settings(settings, AccountPool(settings)).failover is False


def test_operator_notes_describe_every_account():
    lanes, _, cooling = build(3)
    lanes.get(1).health.record_failure("upstream_unauthorized")
    cooling[3] = 60.0

    assert lanes.describe() == (
        "Hesaplar: hesap-1 ARIZALI [upstream_unauthorized] · hesap-2 çalışıyor · "
        "hesap-3 dinleniyor (kota)"
    )


def test_outlook_tells_what_happens_after_a_failure():
    lanes, _, _ = build()
    first, second = lanes.get(1), lanes.get(2)
    first.health.record_failure("upstream_timeout")

    text = lanes.outlook(first)
    assert "yeni istekler çalışan hesaptan cevaplanır" in text
    assert "hesap-1 yeni iş almaz" in text
    assert "kendi keepalive denemesi başarılı olunca geri döner" in text

    second.health.record_failure("upstream_timeout")
    assert "çalışan hesap kalmadı" in lanes.outlook(second)
    assert '5 dk boyunca upstream\'e gidilmeden "problem" denir' in lanes.outlook(second)

    assert build(1)[0].outlook(build(1)[0].primary) is None  # tek hesap: metni çağıran yazar


def test_outlook_without_keepalive_and_in_strict_mode():
    lanes, _, _ = build(retry_failing_after=1800.0)
    lanes.get(1).health.record_failure("upstream_failure")
    assert "30 dk sonra gerçek bir istekle yeniden denenir (keepalive kapalı)" in lanes.outlook(
        lanes.get(1)
    )

    strict, _, _ = build(failover=False)
    strict.get(1).health.record_failure("upstream_failure")
    text = strict.outlook(strict.get(1))
    assert "hesap-1 hesabının payına düşen" in text
    assert "ACCOUNT_FAILOVER kapalı" in text


def test_critical_incident_note_names_the_variable_to_refresh():
    lanes, _, _ = build()
    second = lanes.get(2)
    second.health.record_failure("upstream_unauthorized")

    notes = lanes.incident_notes(second, critical=True)
    assert "UPSTREAM_CURL_2" in notes[0]
    assert "bütün hesapların oturumunu sıfırlar" in notes[0]
    assert "Acelesi yok" in notes[0]  # öbür hesap çalışıyor
    assert notes[-1].startswith("Hesaplar:")

    assert lanes.incident_notes(second, critical=False) == (lanes.describe(),)
    assert build(1)[0].incident_notes(build(1)[0].primary, critical=True) == ()


def test_snapshot_lists_each_account():
    lanes, _, _ = build()
    slots(lanes, 3)
    lanes.get(2).health.record_failure("upstream_timeout")

    snapshot = lanes.snapshot()

    assert [row["account"] for row in snapshot] == ["hesap-1", "hesap-2"]
    assert [row["assigned_total"] for row in snapshot] == [2, 1]
    assert [row["usable"] for row in snapshot] == [True, False]
    assert snapshot[1]["upstream_health"]["code"] == "upstream_timeout"


def test_lane_set_needs_at_least_one_lane():
    with pytest.raises(ValueError):
        LaneSet([])
