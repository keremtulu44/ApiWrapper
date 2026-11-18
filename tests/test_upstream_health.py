"""`UpstreamHealth`: bilinen arıza bekletmesi ve yarı-açık deneme."""

from __future__ import annotations

from app.services.upstream_health import UpstreamHealth


class Clock:
    def __init__(self, start: float = 500.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def make(hold: float = 300.0, **kwargs) -> tuple[UpstreamHealth, Clock]:
    clock = Clock()
    return UpstreamHealth(hold, clock=clock, **kwargs), clock


def test_healthy_upstream_admits_everyone():
    health, _ = make()
    assert health.admit(object()) is None
    assert health.blocked() is None
    assert health.failing is False
    assert health.last_success_at is None


def test_known_failure_blocks_requests_until_the_hold_expires():
    health, clock = make(300.0)
    applied = health.record_failure("upstream_quota_reached")
    assert applied == 300.0

    clock.now += 100.0
    hold = health.admit(object())
    assert hold is not None
    assert hold.code == "upstream_quota_reached"
    assert hold.remaining == 200.0
    assert hold.failing_for == 100.0
    assert hold.trial_in_flight is False
    assert health.blocked() is not None


def test_after_the_hold_exactly_one_request_is_tried():
    """Yarı-açık: bekletme dolunca tek istek denenir, diğerleri hemen geri çevrilir."""
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    first, second = object(), object()

    assert health.admit(first) is None  # deneme isteği
    hold = health.admit(second)
    assert hold is not None and hold.trial_in_flight is True
    assert hold.remaining == 0.0
    # Bekletme süresi dolmuştur: sırada bekleyenleri boşaltma koşulu tetiklenmez.
    assert health.blocked() is None
    # Deneme sahibi yeniden sorarsa geri çevrilmez.
    assert health.admit(first) is None


def test_successful_trial_reopens_for_everyone():
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    owner = object()
    assert health.admit(owner) is None

    health.record_success()

    assert health.failing is False
    assert health.admit(object()) is None
    assert health.last_success_at == clock.now
    assert health.snapshot()["failures"] == 0


def test_failed_trial_starts_a_new_hold():
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    owner = object()
    assert health.admit(owner) is None

    health.record_failure("upstream_timeout")

    hold = health.admit(object())
    assert hold is not None and hold.remaining == 300.0
    assert hold.failing_for == 301.0  # arızanın başı korunur
    assert health.snapshot()["failures"] == 2


def test_abandoned_trial_hands_the_turn_to_the_next_request():
    """Deneme isteği sonuç bildirmeden biterse (istemci koptu) hak kaybolmaz."""
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    owner, other = object(), object()
    assert health.admit(owner) is None
    assert health.admit(other) is not None

    health.release_trial(owner)

    assert health.admit(other) is None


def test_release_trial_by_a_non_owner_changes_nothing():
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    owner, other = object(), object()
    assert health.admit(owner) is None

    health.release_trial(other)

    assert health.admit(other) is not None


def test_trial_that_never_reports_expires():
    """Sonucunu hiç bildirmeyen deneme, uç noktayı süresiz kapalı tutamaz."""
    health, clock = make(300.0)
    health.record_failure("upstream_timeout")
    clock.now += 301.0
    assert health.admit(object()) is None

    clock.now += 901.0
    assert health.admit(object()) is None


def test_retry_after_longer_than_the_hold_is_honoured_up_to_the_cap():
    health, _ = make(300.0, max_hold_seconds=1800.0)
    assert health.record_failure("upstream_quota_reached", retry_after=1200.0) == 1200.0
    assert health.record_failure("upstream_quota_reached", retry_after=86400.0) == 1800.0
    # Daha kısa bir Retry-After bekletmeyi kısaltmaz.
    assert health.record_failure("upstream_quota_reached", retry_after=5.0) == 300.0


def test_zero_hold_disables_the_fast_fail():
    health, _ = make(0.0)
    assert health.record_failure("upstream_quota_reached", retry_after=600.0) == 0.0
    assert health.failing is True  # kayıt tutulur...
    assert health.admit(object()) is None  # ...ama kimse geri çevrilmez
    assert health.blocked() is None


def test_success_is_remembered_for_the_keepalive():
    health, clock = make()
    health.record_success()
    stamp = health.last_success_at
    clock.now += 50.0
    assert health.last_success_at == stamp
    assert health.snapshot()["seconds_since_success"] == 50.0


def test_snapshot_describes_a_failure():
    health, clock = make(300.0)
    health.record_failure("recaptcha_rejected")
    clock.now += 60.0
    snapshot = health.snapshot()
    assert snapshot["failing"] is True
    assert snapshot["code"] == "recaptcha_rejected"
    assert snapshot["failing_seconds"] == 60.0
    assert snapshot["hold_remaining_seconds"] == 240.0
    assert snapshot["trial_in_flight"] is False
