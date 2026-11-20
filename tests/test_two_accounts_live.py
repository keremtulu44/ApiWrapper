"""İki hesap — gerçek soketlerde: eşzamanlılık, hesap başına sıra/aralık ve keepalive.

Sahte upstream hesapları `Authorization` token'ından ayırt eder ve her hesap için
ayrı sayaç tutar (aynı anda kaç istek, hangi anlarda). Burada sınananlar bellek
içi istemciyle görülemez: iki hesabın **aynı anda** çalışması, her hesabın yine
tek tek ve aralıklı gitmesi, probe'un hesap başına zamanlanması.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.services.alerting import TelegramAlerter
from tests.live import (
    TOKEN_ONE,
    TOKEN_TWO,
    LiveWrapper,
    UpstreamScript,
    live_wrapper,
    two_accounts,
    wait_until,
)

CHAT_BODY = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


class RecordingAlerter(TelegramAlerter):
    def __init__(self) -> None:
        super().__init__("123:test-token", 42)
        self.sent: list[str] = []

    async def send(self, text: str, *, label: str | None = None) -> bool:
        self.sent.append(text)
        return True


def ask(wrapper: LiveWrapper, prompt: str = "soru") -> tuple[str, float]:
    """`/v1/ask` çağırır; (son satırın durumu, geçen süre) döndürür."""
    started = time.monotonic()
    lines: list[dict] = []
    with httpx.Client(timeout=30) as client, client.stream(
        "POST", f"{wrapper.base_url}/v1/ask", headers=wrapper.headers, json={"prompt": prompt}
    ) as response:
        for line in response.iter_lines():
            if line.startswith("data:"):
                lines.append(json.loads(line[5:]))
    return (lines[-1]["status"] if lines else "none"), time.monotonic() - started


def chat(wrapper: LiveWrapper, slot: int | None = None) -> httpx.Response:
    headers = dict(wrapper.headers)
    if slot is not None:
        headers["x-apiwrapper-account"] = str(slot)
    return httpx.post(
        f"{wrapper.base_url}/v1/chat/completions", headers=headers, json=CHAT_BODY, timeout=30
    )


def scripts(**kwargs) -> tuple[UpstreamScript, UpstreamScript, UpstreamScript]:
    """(toplam sayaç, 1. hesap, 2. hesap) — hesaplar aynı senaryoyla başlar."""
    return UpstreamScript(), UpstreamScript(**kwargs), UpstreamScript(**kwargs)


def usable(wrapper: LiveWrapper, slot: int) -> bool:
    lanes = wrapper.app.state.lanes
    return lanes.usable(lanes.get(slot))


KEEPALIVE = {
    "upstream_keepalive_model": "test-model",
    "upstream_keepalive_jitter_seconds": 0.0,
    "upstream_keepalive_stream_smoke_interval_seconds": 0.0,
}


# ------------------------------------------------------- bölüşüm ve sıralar
def test_four_simultaneous_requests_are_split_two_and_two():
    """Aynı anda gelen dört istek ikişer ayrılır; hesaplar birbirini beklemez.

    Her hesap yine aynı anda tek istek işler ve iki isteği arasına aralık koyar.
    """
    total, one, two = scripts(delay=0.4)
    gap = 0.3
    with live_wrapper(
        total,
        {TOKEN_ONE: one, TOKEN_TWO: two},
        **two_accounts(request_gap_min_seconds=gap, request_gap_max_seconds=gap),
    ) as wrapper:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: ask(wrapper, f"soru {index}"), range(4)))
        elapsed = time.monotonic() - started

    assert [status for status, _ in results] == ["done"] * 4
    assert (one.calls, two.calls) == (2, 2)
    assert one.max_concurrent == 1 and two.max_concurrent == 1  # hesap başına tek tek
    assert total.max_concurrent == 2  # iki hesap aynı anda çalıştı
    for account in (one, two):
        first, second = account.started_at
        assert second - first >= 0.4 + gap - 0.05  # önceki bitti + aralık
    # Tek sırada dört istek en az 4×0,4 + 3×0,3 = 2,5 sn sürerdi.
    assert elapsed < 2.4


def test_sequential_requests_alternate_between_accounts():
    total, one, two = scripts()
    with live_wrapper(total, {TOKEN_ONE: one, TOKEN_TWO: two}, **two_accounts()) as wrapper:
        order: list[str] = []
        for _ in range(6):
            before = one.calls
            assert ask(wrapper)[0] == "done"
            order.append("bir" if one.calls > before else "iki")

    assert order == ["bir", "iki", "bir", "iki", "bir", "iki"]


def test_request_waiting_behind_a_failing_account_moves_to_the_other():
    """Sırada beklerken hesabı arızalanan istek "problem" almaz: çalışan hesaba geçer."""
    total, one, two = scripts(delay=0.6)
    one.status, one.body = 500, "boom"
    with (
        live_wrapper(total, {TOKEN_ONE: one, TOKEN_TWO: two}, **two_accounts()) as wrapper,
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        futures = []
        for index in range(4):  # 1→bir, 2→iki, 3→bir (bekler), 4→iki (bekler)
            futures.append(pool.submit(ask, wrapper, f"soru {index}"))
            time.sleep(0.1)
        results = [future.result()[0] for future in futures]

    # Arızayı ortaya çıkaran istek "problem" alır; arkasındaki öbür hesaptan cevaplanır.
    assert results == ["problem", "done", "done", "done"]
    assert (one.calls, two.calls) == (1, 3)
    assert two.max_concurrent == 1


def test_moved_request_waits_its_turn_on_the_other_account():
    """Taşınan istek öbür hesabın sırasına girer; o hesap yine tek tek ve aralıklı çalışır."""
    total, one, two = scripts()
    one.status = 401
    two.delay = 0.5
    gap = 0.3
    with (
        live_wrapper(
            total,
            {TOKEN_ONE: one, TOKEN_TWO: two},
            **two_accounts(request_gap_min_seconds=gap, request_gap_max_seconds=gap),
        ) as wrapper,
        ThreadPoolExecutor(max_workers=4) as pool,
    ):
        busy = pool.submit(chat, wrapper, 2)  # 2. hesap yarım saniye meşgul
        assert wait_until(lambda: two.calls == 1)
        status, _ = ask(wrapper)  # 1. hesaba düşer, 401 alır, 2. hesaba taşınır
        assert busy.result().status_code == 200

    assert status == "done"
    assert (one.calls, two.calls) == (1, 2)
    assert two.max_concurrent == 1
    first, second = two.started_at
    assert second - first >= 0.5 + gap - 0.05


# ----------------------------------------------------------------- keepalive
def test_each_account_gets_its_own_probe():
    """Probe hesabına sabitlenir: trafik yokken iki hesap da ayrı ayrı yoklanır."""
    total, one, two = scripts()
    with live_wrapper(
        total,
        {TOKEN_ONE: one, TOKEN_TWO: two},
        **two_accounts(
            upstream_keepalive_interval_seconds=0.6,
            upstream_keepalive_initial_delay_seconds=0.1,
            **KEEPALIVE,
        ),
    ):
        assert wait_until(lambda: one.calls >= 2 and two.calls >= 2, timeout=5)
        # İlk probe'lar aynı anda atılmaz (hesaplar arası kaydırma).
        assert abs(one.started_at[0] - two.started_at[0]) >= 0.2


def test_probe_is_timed_from_each_accounts_own_last_real_request():
    """Bir hesabın gerçek isteği yalnız o hesabın probe'unu erteler.

    Kullanıcının örneğinin küçültülmüşü: aralık 1 sn. 2. hesaba 0,9. ve 1,5.
    saniyelerde gerçek istek gelir; o hesabın bir sonraki probe'u son gerçek
    istekten bir aralık sonra (≈2,5) atılır. 1. hesap hiç istek almadığı için
    kendi probe'ları aralıkla sürer.
    """
    total, one, two = scripts()
    with live_wrapper(
        total,
        {TOKEN_ONE: one, TOKEN_TWO: two},
        **two_accounts(
            upstream_keepalive_interval_seconds=1.0,
            upstream_keepalive_initial_delay_seconds=0.2,
            **KEEPALIVE,
        ),
    ) as wrapper:
        origin = time.monotonic()
        real: list[float] = []
        for at in (0.9, 1.5):
            time.sleep(max(0.0, origin + at - time.monotonic()))
            assert chat(wrapper, 2).status_code == 200
            real.append(time.monotonic())
        time.sleep(max(0.0, origin + 3.3 - time.monotonic()))
        second_calls = list(two.started_at)
        first_calls = [moment - origin for moment in one.started_at]

    # 2. hesap: ilk probe (≈0,7), iki gerçek istek, sonra tek probe.
    after_real = [moment for moment in second_calls if moment > real[-1] + 0.05]
    assert len(after_real) == 1
    assert 0.9 <= after_real[0] - real[-1] <= 1.6  # son gerçek istekten bir aralık sonra
    between = [moment for moment in second_calls if real[0] + 0.05 < moment < real[1] - 0.05]
    assert between == []  # iki gerçek istek arasında probe atılmadı
    # 1. hesap: gerçek istek yok → probe'lar birer aralıkla (0,2 · 1,2 · 2,2 · 3,2).
    assert len(first_calls) >= 3
    gaps = [later - earlier for earlier, later in zip(first_calls, first_calls[1:], strict=False)]
    assert all(0.9 <= gap <= 1.6 for gap in gaps)


def test_failed_account_returns_through_its_own_probe(monkeypatch):
    """Devreden çıkan hesabı gerçek istek değil, kendi probe'u geri getirir."""
    alerter = RecordingAlerter()
    monkeypatch.setattr("app.main.build_alerter", lambda _settings: alerter)
    total, one, two = scripts()
    one.status, one.body = 500, "boom"
    with live_wrapper(
        total,
        {TOKEN_ONE: one, TOKEN_TWO: two},
        **two_accounts(
            upstream_keepalive_interval_seconds=0.8,
            upstream_keepalive_initial_delay_seconds=0.6,
            **KEEPALIVE,
        ),
    ) as wrapper:
        assert ask(wrapper, "ilk-soru")[0] == "problem"  # 1. hesap arızalandı
        assert not usable(wrapper, 1)

        # Hesap arızalıyken gerçek istekler ona hiç gitmez (probe'lar gider).
        assert [ask(wrapper, f"gercek-{index}")[0] for index in range(3)] == ["done"] * 3
        assert not any("gercek-" in body for body in one.bodies)
        assert sum("gercek-" in body for body in two.bodies) == 3

        one.status = 200  # hesap düzeldi; onu fark edecek olan kendi probe'u
        assert wait_until(lambda: usable(wrapper, 1), timeout=5)
        assert not any("gercek-" in body for body in one.bodies)  # geri getiren gerçek istek değil

        assert [ask(wrapper, f"sonra-{index}")[0] for index in range(2)] == ["done", "done"]
        assert sum("sonra-" in body for body in one.bodies) == 1  # yeniden bölüşülüyor
        assert sum("sonra-" in body for body in two.bodies) == 1
        assert wait_until(lambda: any(text.startswith("✅") for text in alerter.sent))

    failure = alerter.sent[0]
    assert 'dış istek cevaplanamadı; karşı sisteme "problem" döndü' in failure
    assert "Hesap: bir" in failure
    assert "kendi keepalive denemesi başarılı olunca geri döner" in failure
    recovered = [text for text in alerter.sent if text.startswith("✅")]
    assert len(recovered) == 1
    assert "Hesap: bir" in recovered[0]
    assert 'Arıza boyunca "problem" cevabı alan dış istek: 1' in recovered[0]
    # Olay başına tek mesaj: bir arıza, bir düzelme.
    assert len(alerter.sent) == 2


def test_probe_failure_alert_names_the_account_and_what_happens_next(monkeypatch):
    """Arızayı probe bulursa mesaj hangi hesap olduğunu ve sonrasını söyler."""
    alerter = RecordingAlerter()
    monkeypatch.setattr("app.main.build_alerter", lambda _settings: alerter)
    total, one, two = scripts()
    two.status, two.body = 401, '{"error":"Unauthorized"}'
    with live_wrapper(
        total,
        {TOKEN_ONE: one, TOKEN_TWO: two},
        **two_accounts(
            upstream_keepalive_interval_seconds=5.0,
            upstream_keepalive_initial_delay_seconds=0.1,
            **KEEPALIVE,
        ),
    ) as wrapper:
        assert wait_until(lambda: len(alerter.sent) == 1, timeout=5)
        assert not usable(wrapper, 2)
        # Probe hesabına sabitlidir: 401 alınca öbür hesaba taşınmaz.
        assert two.calls == 1
        assert [ask(wrapper, f"gercek-{index}")[0] for index in range(2)] == ["done", "done"]
        assert not any("gercek-" in body for body in two.bodies)  # arızalı hesap iş almadı
        assert sum("gercek-" in body for body in one.bodies) == 2

    message = alerter.sent[0]
    assert "upstream isteği başarısız" in message
    assert "Hesap: iki" in message
    assert "[upstream_unauthorized]" in message
    assert "yeni istekler çalışan hesaptan cevaplanır, karşı sistem etkilenmez" in message
    assert "UPSTREAM_CURL_2 değişkenindedir" in message
    assert "Hesaplar: bir çalışıyor · iki ARIZALI [upstream_unauthorized]" in message
    assert len(alerter.sent) == 1  # sonraki gerçek istekler yeni mesaj üretmedi


def test_request_timing_out_in_the_other_accounts_queue_does_not_blame_that_account():
    """Taşınan istek öbür hesabın sırasında beklerken süresi dolarsa o hesap suçlanmaz.

    O hesap isteği hiç görmedi; arızalı sayılırsa çalışan tek hesap da devreden
    çıkar ve sonraki istekler boş yere "problem" alırdı.
    """
    total, one, two = scripts()
    one.status = 401
    two.delay = 1.5
    with (
        live_wrapper(
            total,
            {TOKEN_ONE: one, TOKEN_TWO: two},
            **two_accounts(ask_request_timeout_seconds=0.4),
        ) as wrapper,
        ThreadPoolExecutor(max_workers=4) as pool,
    ):
        alerter = RecordingAlerter()
        wrapper.app.state.alerter = alerter
        busy = pool.submit(chat, wrapper, 2)  # 2. hesap 1,5 sn meşgul
        assert wait_until(lambda: two.calls == 1)
        # 1. hesaba düşer, 401 alır, 2. hesabın sırasına girer; 0,4 sn'lik süresi orada dolar.
        status, elapsed = ask(wrapper)

        assert status == "problem"
        assert elapsed < 1.2
        # 2. hesabın işi hâlâ sürüyor: durumu o iş bitmeden, şimdi denetlenir.
        assert not busy.done()
        assert not usable(wrapper, 1)
        assert usable(wrapper, 2)  # çalışan hesap devrede kaldı
        assert wait_until(lambda: len(alerter.sent) == 2)

        assert busy.result().status_code == 200
        assert two.calls == 1  # süresi dolan istek 2. hesaba hiç gönderilmedi
        assert wait_until(
            lambda: all(
                row["queue"]["active"] is None and row["queue"]["waiting"] == 0
                for row in wrapper.app.state.lanes.snapshot()
            )
        )

    moved = next(text for text in alerter.sent if "Hesap: bir" in text)
    assert "[upstream_unauthorized]" in moved
    assert "iki hesabına taşındı ama orada da cevaplanamadı" in moved
    # İsteğin kendi sonu yerel bir yoğunluk olayıdır; hiçbir hesaba arıza yazılmaz.
    local = next(text for text in alerter.sent if "Hesap: bir" not in text)
    assert "[queue_timeout]" in local
    assert alerter.policy_for("iki").state.active_key is None
