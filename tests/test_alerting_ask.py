"""Bildirim politikası: dış sistemin (`/v1/ask`) istekleri ve yerel olaylar.

Kural: karşı sisteme yalnız "problem" denir, ayrıntı operatöre gider — ama olay
başına **tek** mesajla. Süren arızada gelen istekler sayılır, düzelince tek bir
özet yazılır.
"""

from __future__ import annotations

from app.services.alerting import (
    AlertPolicy,
    ProbeResult,
    TelegramAlerter,
    format_event,
    human_duration,
)


def ask_failure(code: str = "upstream_quota_reached", status: int | None = 429, **kwargs):
    return ProbeResult(ok=False, status_code=status, code=code, source="ask", **kwargs)


def probe_failure(code: str = "upstream_quota_reached", status: int | None = 429):
    return ProbeResult(ok=False, status_code=status, code=code)


def ok(source: str = "ask") -> ProbeResult:
    return ProbeResult(ok=True, status_code=200, source=source)


# ------------------------------------------------------------ ilk mesaj
def test_ask_failure_message_tells_the_operator_what_the_caller_was_not_told():
    policy = AlertPolicy(repeat_seconds=21600)
    text = policy.evaluate(
        ask_failure(
            details=(
                'Upstream cevabı: HTTP 429 · {"error":"daily spend limit reached"}',
                "İstek req_abc · sırada 12 sn · işlem 3 sn",
            )
        ),
        now=0.0,
    )

    assert text is not None
    lines = text.splitlines()
    assert lines[0].endswith('dış istek cevaplanamadı; karşı sisteme "problem" döndü.')
    assert lines[1].startswith("Ne oldu: ") and lines[1].endswith("[upstream_quota_reached]")
    assert lines[2] == "Tür: GEÇİCİ sayılır — önce bekleyin, dokunmayın."
    assert lines[3].startswith("Yapılacak: ")
    assert 'Upstream cevabı: HTTP 429 · {"error":"daily spend limit reached"}' in lines
    assert "İstek req_abc · sırada 12 sn · işlem 3 sn" in lines
    # Son satır yalnız zaman damgası: wrapper'ın iç durum kodu burada yazılmaz
    # (upstream'in gerçek cevabı zaten ayrıntı satırında).
    assert lines[-1].endswith(" UTC") and "HTTP" not in lines[-1]


def test_probe_failure_keeps_its_own_headline():
    text = AlertPolicy().evaluate(probe_failure(), now=0.0)
    assert text is not None
    assert "upstream isteği başarısız." in text.splitlines()[0]
    assert "karşı sisteme" not in text
    assert text.splitlines()[-1].startswith("HTTP 429 · ")  # probe'da durum kodu kalır


def test_session_death_is_marked_permanent_and_quota_temporary():
    permanent = AlertPolicy().evaluate(ask_failure("upstream_unauthorized", 502), now=0.0)
    temporary = AlertPolicy().evaluate(ask_failure("recaptcha_rejected", 503), now=0.0)

    assert permanent is not None and temporary is not None
    assert "Tür: KALICI — elle müdahale gerekir." in permanent
    assert permanent.startswith("🔴")
    assert "Tür: GEÇİCİ" in temporary
    assert temporary.startswith("⚠️")


def test_captcha_rejection_tells_the_operator_to_wait_not_to_touch():
    """17 Kasım dersi: reCAPTCHA reddi geçicidir; taze cURL almak oturumu öldürür."""
    text = AlertPolicy().evaluate(ask_failure("recaptcha_rejected", 503), now=0.0)
    assert text is not None
    assert "bekleyin" in text
    assert "öldürür" in text


# ------------------------------------------------- olay başına tek mesaj
def test_requests_during_an_incident_are_counted_not_announced():
    policy = AlertPolicy(repeat_seconds=21600)
    assert policy.evaluate(ask_failure(), now=0.0) is not None

    for second in range(1, 40):
        assert policy.evaluate(ask_failure(), now=float(second * 60)) is None

    assert policy.state.ask_problems == 40
    assert policy.state.total_alerts == 1


def test_recovery_reports_how_many_callers_fell_back():
    policy = AlertPolicy(repeat_seconds=21600)
    policy.evaluate(ask_failure(), now=0.0)
    policy.evaluate(ask_failure(), now=60.0)
    policy.evaluate(ask_failure(), now=120.0)

    text = policy.evaluate(ok(), now=900.0)

    assert text is not None
    assert "yeniden çalışıyor" in text
    assert "Önceki arıza: upstream_quota_reached" in text
    assert "Süre: 15 dk" in text
    assert 'Arıza boyunca "problem" cevabı alan dış istek: 3' in text
    # Sayaç sıfırlandı; sağlıklı istekler sessiz.
    assert policy.state.ask_problems == 0
    assert policy.evaluate(ok(), now=960.0) is None


def test_recovery_after_a_probe_only_incident_has_no_caller_count():
    policy = AlertPolicy()
    policy.evaluate(probe_failure(), now=0.0)
    text = policy.evaluate(ok("probe"), now=600.0)
    assert text is not None
    assert "dış istek" not in text


def test_probe_and_ask_share_one_incident():
    """Aynı arızayı önce probe, sonra dış istek görürse ikinci bir mesaj çıkmaz."""
    policy = AlertPolicy(repeat_seconds=21600)
    assert policy.evaluate(probe_failure(), now=0.0) is not None
    assert policy.evaluate(ask_failure(), now=30.0) is None
    assert policy.evaluate(probe_failure(), now=3600.0) is None

    recovered = policy.evaluate(ok("probe"), now=7200.0)

    assert recovered is not None
    assert 'Arıza boyunca "problem" cevabı alan dış istek: 1' in recovered


def test_reminder_after_the_repeat_interval_carries_the_running_count():
    policy = AlertPolicy(repeat_seconds=3600)
    policy.evaluate(ask_failure(), now=0.0)
    policy.evaluate(ask_failure(), now=600.0)

    reminder = policy.evaluate(ask_failure(), now=3600.0)

    assert reminder is not None
    assert "arıza sürüyor" in reminder
    assert 'Şimdiye dek "problem" cevabı alan dış istek: 3' in reminder


def test_a_different_failure_is_announced_and_the_count_carries_on():
    policy = AlertPolicy(repeat_seconds=21600)
    policy.evaluate(ask_failure("upstream_quota_reached", 429), now=0.0)
    changed = policy.evaluate(ask_failure("upstream_unauthorized", 502), now=60.0)

    assert changed is not None and "[upstream_unauthorized]" in changed
    recovered = policy.evaluate(ok(), now=120.0)
    assert recovered is not None
    assert 'dış istek: 2' in recovered


def test_probe_result_defaults_stay_backwards_compatible():
    result = ProbeResult(ok=False, status_code=503, code="x")
    assert result.source == "probe"
    assert result.details == ()
    assert result.key == "x"


# ------------------------------------------------------------ yerel olay
class Recorder(TelegramAlerter):
    def __init__(self) -> None:
        super().__init__("123:test-token", 42)
        self.sent: list[tuple[str, str | None]] = []

    async def send(self, text: str, *, label: str | None = None) -> bool:
        self.sent.append((text, label))
        return True


def test_format_event_explains_a_local_problem():
    text = format_event("queue_full", 429, ("İstek req_abc · sırada 0 sn",))
    lines = text.splitlines()
    assert 'karşı sisteme "problem" döndü' in lines[0]
    assert lines[1].endswith("[queue_full]")
    assert lines[2].startswith("Yapılacak: ")
    assert "İstek req_abc · sırada 0 sn" in lines
    assert lines[-1].endswith(" UTC")


async def test_local_events_are_throttled_per_kind():
    alerter = Recorder()

    first = await alerter.notify_event("queue_full", "sıra dolu", now=0.0)
    second = await alerter.notify_event("queue_full", "sıra dolu", now=10.0)
    third = await alerter.notify_event("queue_full", "sıra dolu", now=3599.0)
    other = await alerter.notify_event("queue_timeout", "sırada bekledi", now=20.0)
    later = await alerter.notify_event("queue_full", "sıra dolu", now=3600.0)

    assert first == "sıra dolu"
    assert second is None and third is None
    assert other == "sırada bekledi"  # başka tür kendi sayacını kullanır
    assert later is not None
    assert "aynı türden 2 olay daha oldu" in later
    assert [label for _, label in alerter.sent] == ["queue_full", "queue_timeout", "queue_full"]


async def test_local_events_do_not_open_an_incident():
    """Sıra dolması arıza kaydı açmaz: ardından gelen sağlıklı istek "düzeldi" demez."""
    alerter = Recorder()
    await alerter.notify_event("queue_full", "sıra dolu", now=0.0)

    assert alerter.policy.state.active_key is None
    assert await alerter.notify(ok()) is None
    assert len(alerter.sent) == 1


async def test_notify_sends_only_when_the_policy_says_so():
    alerter = Recorder()
    assert await alerter.notify(ask_failure()) is not None
    assert await alerter.notify(ask_failure()) is None
    assert await alerter.notify(ok()) is not None
    assert len(alerter.sent) == 2


def test_human_duration():
    assert human_duration(0) == "0 sn"
    assert human_duration(45.9) == "45 sn"
    assert human_duration(300) == "5 dk"
    assert human_duration(3 * 3600) == "3 sa"


# ------------------------------------------------------ hesap başına kayıt
class SilentAlerter(TelegramAlerter):
    """Gerçek politika; gönderim yerine metni saklar."""

    def __init__(self) -> None:
        super().__init__("123:test-token", 42, repeat_seconds=21600)
        self.sent: list[str] = []

    async def send(self, text: str, *, label: str | None = None) -> bool:
        self.sent.append(text)
        return True


async def test_each_account_has_its_own_incident():
    """Bir hesabın arızası ve düzelmesi öbürününkine karışmaz."""
    alerter = SilentAlerter()

    await alerter.notify(ask_failure("upstream_unauthorized", 502, scope="hesap-1"))
    await alerter.notify(ask_failure("upstream_quota_reached", 429, scope="hesap-2"))
    # Öbür hesabın başarısı bu hesabın arızasını kapatmaz.
    assert await alerter.notify(ProbeResult(ok=True, status_code=200, scope="hesap-2"))
    assert alerter.policy_for("hesap-1").state.active_key == "upstream_unauthorized"
    assert alerter.policy_for("hesap-2").state.active_key is None
    # Tek hesaplı kurulumun ortak kaydı hiç kullanılmadı.
    assert alerter.policy.state.active_key is None

    assert len(alerter.sent) == 3
    assert alerter.sent[0].split("\n")[1] == "Hesap: hesap-1"
    assert alerter.sent[1].split("\n")[1] == "Hesap: hesap-2"
    assert alerter.sent[2].startswith("✅ ApiWrapper — hesap yeniden çalışıyor")
    assert alerter.sent[2].split("\n")[1] == "Hesap: hesap-2"
    assert sorted(alerter.scopes) == ["", "hesap-1", "hesap-2"]


def test_same_failure_on_two_accounts_is_two_incidents():
    """Aynı kod iki hesapta görülürse ikisi de bildirilir (biri öbürünü bastırmaz)."""
    alerter = SilentAlerter()

    first = alerter.evaluate(probe_failure_for("hesap-1"))
    second = alerter.evaluate(probe_failure_for("hesap-2"))
    repeat = alerter.evaluate(probe_failure_for("hesap-1"))

    assert first is not None and "Hesap: hesap-1" in first
    assert second is not None and "Hesap: hesap-2" in second
    assert repeat is None  # aynı hesapta süren arıza: mesaj yok


def probe_failure_for(scope: str) -> ProbeResult:
    return ProbeResult(ok=False, status_code=502, code="upstream_unauthorized", scope=scope)


def test_request_moved_to_another_account_is_not_counted_as_a_problem():
    """Karşı sistem "problem" görmediyse sayaç artmaz; mesaj bunu açıkça söyler."""
    policy = AlertPolicy(repeat_seconds=21600, scope="hesap-1")

    text = policy.evaluate(
        ask_failure(
            "upstream_unauthorized",
            502,
            scope="hesap-1",
            rerouted=True,
            details=("İstek req_1 hesap-2 hesabından cevaplandı.",),
        ),
        now=0.0,
    )

    assert text is not None
    assert text.startswith("🔴 ApiWrapper — bir hesap cevap veremedi; istek diğer hesaba taşındı.")
    assert 'karşı sisteme "problem" döndü' not in text
    assert "HTTP 502" not in text  # iç durum kodu yazılmaz
    assert policy.state.ask_problems == 0
    assert policy.state.failures == 1

    # Aynı arıza sürerken gerçekten "problem" alan istek sayılır.
    again = ask_failure("upstream_unauthorized", 502, scope="hesap-1")
    assert policy.evaluate(again, now=1.0) is None
    assert policy.state.ask_problems == 1

    recovered = policy.evaluate(ProbeResult(ok=True, status_code=200, scope="hesap-1"), now=60.0)
    assert 'Arıza boyunca "problem" cevabı alan dış istek: 1' in recovered


def test_reminder_for_an_account_names_it():
    policy = AlertPolicy(repeat_seconds=100, scope="hesap-2")
    policy.evaluate(probe_failure_for("hesap-2"), now=0.0)

    reminder = policy.evaluate(probe_failure_for("hesap-2"), now=101.0)

    assert reminder is not None
    assert reminder.split("\n")[:2] == ["⏳ ApiWrapper — arıza sürüyor.", "Hesap: hesap-2"]


def test_messages_without_an_account_are_unchanged():
    """Tek hesaplı kurulumun metinlerinde `Hesap:` satırı yoktur."""
    policy = AlertPolicy(repeat_seconds=100)

    failure = policy.evaluate(probe_failure(), now=0.0)
    reminder = policy.evaluate(probe_failure(), now=101.0)
    recovered = policy.evaluate(ok("probe"), now=200.0)

    for text in (failure, reminder, recovered):
        assert text is not None and "Hesap" not in text
    assert recovered.startswith("✅ ApiWrapper — upstream yeniden çalışıyor.")
