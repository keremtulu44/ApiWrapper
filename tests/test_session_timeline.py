"""`app/services/session_timeline.py` — ölçüm katmanının birim testleri.

Burada doğrulanan şey bir onarım değil, **kanıt zinciri**: 401'in bizden mi
(sabitlenmiş bayat token) yoksa hedeften mi (ölü oturum) kaynaklandığını ayırt
eden ölçümler doğru hesaplanıyor mu, ve olay defteri ham token sızdırıyor mu.
"""

from __future__ import annotations

import base64
import json
import time as time_module

import pytest

from app.services import session_timeline


class _Clock:
    """Deterministik saat: seyreltme ve aralık ölçümleri beklenmeden test edilir."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _jwt(*, tag: str = "t", ttl: int = 1200, age: int = 0) -> str:
    """`iat`/`exp` taşıyan JWT: `ttl` tasarlanmış ömür, `age` ne kadar süredir elimizde."""
    issued = int(_CLOCK.time()) - age

    def part(payload: dict[str, object]) -> str:
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return (
        f"{part({'alg': 'HS256'})}."
        f"{part({'sub': 'u', 'tag': tag, 'iat': issued, 'exp': issued + ttl})}."
        "signature"
    )


_CLOCK = _Clock()


@pytest.fixture(autouse=True)
def _clean_timeline(monkeypatch: pytest.MonkeyPatch):
    """Her test boş bir defter ve sabit bir saatle başlar."""
    monkeypatch.setattr(session_timeline, "time", _CLOCK)
    session_timeline.configure(maxlen=400, utc_offset_hours=0.0)
    session_timeline.reset()
    yield
    session_timeline.reset()
    monkeypatch.setattr(session_timeline, "time", time_module)


def _kinds() -> list[str]:
    return [str(event["kind"]) for event in session_timeline.snapshot(0)]


# ------------------------------------------------------------------- token betimi


def test_token_id_is_stable_and_hides_the_value():
    token = _jwt(tag="a")

    first = session_timeline.token_id(token)
    second = session_timeline.token_id(token)

    assert first == second and first is not None
    assert len(first) == 10
    assert token not in first
    assert session_timeline.token_id("") is None
    assert session_timeline.token_id(_jwt(tag="b")) != first


def test_describe_token_computes_ttl_age_and_expiry():
    """`ttl` tasarlanmış ömür, `age` elde kalma süresi — H1 testi bu ikisine bakar."""
    fresh = _jwt(ttl=1200, age=0)
    old = _jwt(tag="eski", ttl=1200, age=3600)

    described_fresh = session_timeline.describe_token(fresh)
    described_old = session_timeline.describe_token(old)

    assert described_fresh["is_jwt"] is True
    assert described_fresh["ttl_s"] == 1200
    assert described_fresh["age_s"] == pytest.approx(0, abs=1)
    assert described_fresh["expired"] is False
    assert described_old["expired"] is True
    assert described_old["exp_in_s"] == pytest.approx(-2400, abs=2)
    assert described_old["age_s"] == pytest.approx(3600, abs=1)


def test_describe_token_handles_opaque_and_missing_values():
    opaque = session_timeline.describe_token("düz-bir-değer")
    assert opaque["found"] is True and opaque["is_jwt"] is False
    assert "exp_utc" not in opaque
    assert session_timeline.describe_token(None) == {"found": False}


# ---------------------------------------------------------------------- halka


def test_record_drops_none_fields_and_keeps_order():
    session_timeline.record("ilk", deger=1, bos=None)
    session_timeline.record("ikinci", deger=2)

    events = session_timeline.snapshot(0)

    assert [event["kind"] for event in events] == ["ilk", "ikinci"]
    assert "bos" not in events[0]
    assert events[0]["utc"] == time_module.strftime(
        "%Y-%m-%d %H:%M:%S", time_module.gmtime(_CLOCK.time())
    )


def test_snapshot_limit_returns_the_newest():
    for index in range(5):
        session_timeline.record(f"olay-{index}")

    assert [event["kind"] for event in session_timeline.snapshot(2)] == ["olay-3", "olay-4"]


def test_configure_caps_the_ring_but_totals_survive():
    session_timeline.configure(maxlen=10)
    session_timeline.note_upstream_success(status=200, token=_jwt(ttl=1200, age=54000))
    for index in range(30):
        session_timeline.record(f"gurultu-{index}")

    events = session_timeline.snapshot(0)
    upstream = session_timeline.analysis()["upstream"]

    assert len(events) == 10, "halka kapasiteyi aşmamalı"
    assert all(event["kind"].startswith("gurultu") for event in events)
    # Halka doldu ama kritik ölçüm kaybolmadı: amaç tam olarak bu.
    assert upstream["successes"] == 1
    assert upstream["max_working_age_s"] == pytest.approx(54000, abs=2)


# ------------------------------------------------------------------- rotasyon


def test_rotation_counts_and_interval_mean():
    session_timeline.note_token_rotation(new_token=_jwt(tag="1"), source="set_cookie")
    _CLOCK.advance(600)
    session_timeline.note_token_rotation(new_token=_jwt(tag="2"), source="set_cookie")
    _CLOCK.advance(1200)
    session_timeline.note_token_rotation(new_token=_jwt(tag="3"), source="set_cookie")

    token = session_timeline.analysis()["token"]

    assert token["rotations"] == 3
    assert token["distinct_ids"] == 3
    assert token["rotation_interval_mean_s"] == 900.0
    assert token["seconds_since_last_rotation"] == pytest.approx(0, abs=1)
    assert _kinds().count("token_rotated") == 3


def test_pinned_token_shadowing_is_recorded_when_env_value_differs():
    """Canlı arızanın imzası: çerez rotasyona girer, sabitlenmiş token yerinde kalır."""
    pinned = _jwt(tag="sabitlenmis", ttl=1200, age=3600)
    session_timeline.note_token_rotation(
        new_token=_jwt(tag="taze"),
        old_token=_jwt(tag="cerezi-eski"),
        pinned_token=pinned,
        replaced_pinned=False,
    )

    assert "pinned_token_shadowing" in _kinds()
    shadow = [e for e in session_timeline.snapshot(0) if e["kind"] == "pinned_token_shadowing"][0]

    assert shadow["pinned_id"] == session_timeline.token_id(pinned)
    assert shadow["pinned_expired"] is True
    assert "Render env'den anahtarı SİLMEK" in str(shadow["hint"])
    # Ham token hiçbir olaya sızmamalı.
    assert pinned not in json.dumps(session_timeline.snapshot(0))


def test_no_shadowing_event_when_the_pinned_value_was_replaced():
    session_timeline.note_token_rotation(
        new_token=_jwt(tag="taze"),
        old_token=_jwt(tag="eski"),
        pinned_token=_jwt(tag="eski"),
        replaced_pinned=True,
    )

    assert "pinned_token_shadowing" not in _kinds()
    rotated = [e for e in session_timeline.snapshot(0) if e["kind"] == "token_rotated"][0]
    assert rotated["replaced_pinned"] is True


def test_cookie_unchanged_is_throttled_but_totals_are_not():
    token = _jwt(tag="ayni")

    session_timeline.note_cookie_unchanged(token=token)
    _CLOCK.advance(30)
    session_timeline.note_cookie_unchanged(token=token)
    _CLOCK.advance(300)
    session_timeline.note_cookie_unchanged(token=token)

    assert _kinds().count("token_unchanged") == 2, "5 dakikadan sık yazılmamalı"
    assert session_timeline.analysis()["token"]["current"]["id"] == session_timeline.token_id(token)


# --------------------------------------------------------------------- kimlik


def test_identity_source_is_detected():
    pinned = _jwt(tag="pinned")
    cookie_token = _jwt(tag="cookie")

    session_timeline.note_request_identity(
        token=pinned, cookie_token=cookie_token, pinned_token=pinned, account="hesap-1"
    )
    identity = session_timeline.analysis()["identity_last_used"]

    assert identity["source"] == "pinned"
    assert identity["account"] == "hesap-1"
    assert _kinds() == ["identity_used"]


def test_cookie_only_identity_is_labelled_cookie():
    cookie_token = _jwt(tag="cookie")

    session_timeline.note_request_identity(token=cookie_token, cookie_token=cookie_token)

    assert session_timeline.analysis()["identity_last_used"]["source"] == "cookie"


def test_missing_token_is_labelled_none():
    session_timeline.note_request_identity(token=None)

    identity = session_timeline.analysis()["identity_last_used"]
    assert identity["source"] == "none"
    assert identity.get("id") is None


def test_identity_event_is_deduplicated_until_it_changes():
    """Aynı kimlikle giden istekler satır üretmemeli; değişim ve 15 dk'lık kalp atışı üretmeli."""
    token = _jwt(tag="pinned")
    for _ in range(5):
        session_timeline.note_request_identity(token=token, pinned_token=token)
    assert _kinds().count("identity_used") == 1

    _CLOCK.advance(300)
    session_timeline.note_request_identity(token=token, pinned_token=token)
    assert _kinds().count("identity_used") == 1, "pencere içinde aynı kimlik yazılmaz"

    _CLOCK.advance(700)  # toplam 1000 sn > 900 sn pencere
    session_timeline.note_request_identity(token=token, pinned_token=token)
    assert _kinds().count("identity_used") == 2, "kimlik aynı kalsa da 15 dk'da bir tazelenir"

    _CLOCK.advance(1)
    other = _jwt(tag="baska")
    session_timeline.note_request_identity(token=other, pinned_token=other)
    events = session_timeline.snapshot(0)
    assert _kinds().count("identity_used") == 3, "kimlik değişince hemen yazılmalı"
    assert events[-1]["changed"] is True
    assert events[-2].get("changed") is None, "kalp atışı 'değişti' diye işaretlenmez"


# ------------------------------------------------------------------ başarı/ölüm


def test_expired_token_that_works_refutes_h1():
    """Süresi 2400 sn önce dolmuş bir token'la istek geçti ⇒ hedef yaşı denetlemiyor."""
    stale = _jwt(tag="bayat", ttl=1200, age=3600)

    session_timeline.note_upstream_success(status=200, token=stale)
    data = session_timeline.analysis()

    assert data["upstream"]["max_exp_overrun_s"] == pytest.approx(2400, abs=2)
    assert data["upstream"]["max_working_age_s"] == pytest.approx(3600, abs=2)
    assert "H1 ÇÜRÜTÜLDÜ" in data["verdict"]


def test_success_keeps_the_oldest_working_token():
    session_timeline.note_upstream_success(status=200, token=_jwt(tag="genc", ttl=1200, age=100))
    _CLOCK.advance(10)
    session_timeline.note_upstream_success(status=200, token=_jwt(tag="yasli", ttl=1200, age=9000))
    _CLOCK.advance(10)
    session_timeline.note_upstream_success(status=200, token=_jwt(tag="orta", ttl=1200, age=500))

    upstream = session_timeline.analysis()["upstream"]

    assert upstream["successes"] == 3, "toplamlar seyreltmeden etkilenmez"
    assert upstream["max_working_age_s"] == pytest.approx(9000, abs=3)
    # `upstream_ok` olayı seyreltilir: 3 başarılı istek 3 satır üretmez.
    assert _kinds().count("upstream_ok") == 1


def test_death_is_counted_only_for_user_not_found():
    token = _jwt(tag="oluyor", ttl=1200, age=100)
    session_timeline.note_token_rotation(new_token=token)
    _CLOCK.advance(120)

    session_timeline.note_identity_rejected(
        status=401, body_class="user_not_found", token=token, account="hesap-1"
    )
    session_timeline.note_identity_rejected(status=403, body_class="captcha", token=token)
    session_timeline.note_identity_rejected(status=401, body_class="auth_body", token=token)

    data = session_timeline.analysis()

    assert data["upstream"]["deaths"] == 1, "yalnızca user_not_found oturum ölümüdür"
    assert data["upstream"]["body_classes"] == {
        "user_not_found": 1,
        "captcha": 1,
        "auth_body": 1,
    }
    assert data["upstream"]["death_rotation_gap_mean_s"] == pytest.approx(120, abs=2)
    rejected = [e for e in session_timeline.snapshot(0) if e["kind"] == "identity_rejected"][0]
    assert rejected["seconds_since_last_rotation"] == pytest.approx(120, abs=2)


# --------------------------------------------------------------- forensic karar


def test_forensic_recovered_proves_h3():
    session_timeline.note_identity_rejected(status=401, body_class="user_not_found", token=None)
    session_timeline.note_forensic_retry(
        outcome="recovered", stale_id="aaa", fresh_id="bbb", first_status=401, status=200
    )

    verdict = session_timeline.analysis()["verdict"]

    assert "H3 DOĞRULANDI" in verdict
    assert "SİL" in verdict, "çözüm eylemi net olmalı: boşaltmak değil silmek"


def test_forensic_rejected_proves_h2():
    session_timeline.note_forensic_retry(outcome="rejected", first_status=401, status=401)

    verdict = session_timeline.analysis()["verdict"]

    assert "H2 DOĞRULANDI" in verdict
    assert "/yenile diriltmez" in verdict


def test_forensic_skipped_no_fresh_token_supports_h2():
    session_timeline.note_forensic_retry(outcome="skipped_no_fresh_token", set_cookie_count=1)

    assert "H2 DESTEKLENDİ" in session_timeline.analysis()["verdict"]


def test_forensic_disabled_is_recorded_as_its_own_outcome():
    session_timeline.note_forensic_retry(outcome="skipped_disabled", status=401)

    outcomes = session_timeline.analysis()["forensic"]["outcomes"]
    assert outcomes == {"skipped_disabled": 1}


def test_verdict_reports_healthy_and_insufficient_data():
    assert "Yetersiz veri" in session_timeline.analysis()["verdict"]

    session_timeline.note_upstream_success(status=200, token=_jwt(ttl=1200, age=10))
    session_timeline.note_token_rotation(new_token=_jwt(tag="r"))

    verdict = session_timeline.analysis()["verdict"]
    assert "Sağlıklı" in verdict and "1 kez rotasyona" in verdict


def test_death_without_forensic_evidence_stays_honest():
    """Ölüm var ama doğrulama denemesi hiç çalışmadı: rapor kesin konuşmamalı."""
    session_timeline.note_identity_rejected(status=401, body_class="user_not_found", token=None)
    session_timeline.note_upstream_success(status=200, token=_jwt(ttl=1200, age=10))

    verdict = session_timeline.analysis()["verdict"]

    assert "OLASI" in verdict, "kanıt yokken kesin hüküm verilmemeli"
    assert "DOĞRULANDI" not in verdict
    assert "forensic retry" in verdict


def test_opaque_token_death_falls_back_to_needing_more_data():
    """JWT olmayan token'da yaş ölçülemez: yorum veri istemeli, uydurmamalı."""
    session_timeline.note_upstream_success(status=200, token="duz-bir-token")
    session_timeline.note_identity_rejected(
        status=401, body_class="user_not_found", token="duz-bir-token"
    )

    verdict = session_timeline.analysis()["verdict"]

    assert "forensic retry hiç tetiklenmedi" in verdict
    assert "DOĞRULANDI" not in verdict and "ÇÜRÜTÜLDÜ" not in verdict


# ------------------------------------------------------------------------ rapor


def test_report_shows_utc_and_local_time_when_offset_is_set():
    session_timeline.configure(maxlen=50, utc_offset_hours=3.0)
    session_timeline.note_token_rotation(new_token=_jwt(tag="r"))
    session_timeline.note_identity_rejected(status=401, body_class="user_not_found", token=None)

    report = session_timeline.format_report(limit=5)

    assert "OTURUM OLAY DEFTERİ" in report
    assert "yerel)" in report, "operatör UTC+3'te; tek saat dilimi yanıltır"
    assert "identity_rejected" in report and "token_rotated" in report
    assert "YORUM:" in report


def test_report_stays_within_the_telegram_limit():
    for index in range(200):
        session_timeline.record(f"olay-{index}", detay="x" * 200)

    report = session_timeline.format_report(limit=400)

    assert len(report) <= 3800
    assert report.endswith("…")


def test_report_and_events_never_contain_a_raw_token():
    """Olay defteri bir telemetri yüzeyidir: ham kimlik bilgisi sızmamalı."""
    pinned = _jwt(tag="gizli-pinned")
    rotated = _jwt(tag="gizli-taze")

    session_timeline.note_token_rotation(
        new_token=rotated, old_token=pinned, pinned_token=pinned
    )
    session_timeline.note_request_identity(token=pinned, pinned_token=pinned)
    session_timeline.note_upstream_success(status=200, token=pinned)
    session_timeline.note_identity_rejected(status=401, body_class="user_not_found", token=pinned)

    dumped = json.dumps(session_timeline.snapshot(0), ensure_ascii=False)
    report = session_timeline.format_report(limit=50)

    for secret in (pinned, rotated):
        assert secret not in dumped
        assert secret not in report
        assert secret not in json.dumps(session_timeline.analysis(), ensure_ascii=False)


def test_state_save_marks_the_timeline_boundary():
    session_timeline.note_state_saved(source="upstream_curl", token=_jwt(tag="yeni-curl"))
    session_timeline.note_state_saved(source="upstream_set_cookie", token=None)

    assert session_timeline.analysis()["state_saves"] == {
        "upstream_curl": 1,
        "upstream_set_cookie": 1,
    }
    assert _kinds().count("state_saved") == 2


def test_process_started_records_the_restart_boundary():
    session_timeline.note_process_started(version="1.0.0")

    assert _kinds() == ["process_started"]
    assert session_timeline.analysis()["window"]["process_started"]
