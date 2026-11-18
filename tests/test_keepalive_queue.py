"""Keepalive probe'u ile istek sırası / sağlık kaydı arasındaki kurallar.

* Gerçek trafik varken probe atılmaz (kota boşa harcanmaz).
* Sıra doluyken probe bekler; sırada beklerken süresi dolarsa arıza sayılmaz.
* Cevap gecikmesi `upstream_timeout`'tur; `wrapper_unreachable` yalnız bağlantı
  hiç kurulamadığında kullanılır (18 Kasım'daki yanlış etiketin düzeltmesi).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

import app.services.keepalive as keepalive_module
from app.services.alerting import ProbeResult
from app.services.keepalive import (
    _never_served,
    _report_health,
    run_upstream_keepalive,
    transport_failure_code,
    wait_until_due,
)
from app.services.request_queue import RequestQueue, max_queue_wait
from app.services.upstream_health import UpstreamHealth
from tests.conftest import make_settings


async def timed(awaitable) -> float:
    started = time.monotonic()
    await awaitable
    return time.monotonic() - started


# ------------------------------------------------------- arıza etiketi
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("yavaş"), "upstream_timeout"),
        (httpx.WriteTimeout("yavaş"), "upstream_timeout"),
        (httpx.PoolTimeout("yavaş"), "upstream_timeout"),
        (httpx.ConnectTimeout("bağlanamadı"), "wrapper_unreachable"),
        (httpx.ConnectError("reddedildi"), "wrapper_unreachable"),
        (httpx.RemoteProtocolError("koptu"), "wrapper_unreachable"),
        (RuntimeError("beklenmeyen"), "wrapper_unreachable"),
    ],
)
def test_a_slow_answer_is_not_reported_as_an_unreachable_wrapper(error, code):
    assert transport_failure_code(error) == code


# -------------------------------------------------- gerçek trafik varken
async def test_probe_waits_only_its_delay_when_there_is_no_traffic():
    health = UpstreamHealth(300.0)
    elapsed = await timed(
        wait_until_due(0.05, interval=5.0, jitter=0.0, health=health, queue=RequestQueue())
    )
    assert 0.04 <= elapsed < 1.0


async def test_probe_is_deferred_when_a_real_request_succeeded_meanwhile():
    """Gerçek istek oturumu tazelediyse probe o istekten bir aralık sonraya kalır."""
    health = UpstreamHealth(300.0)

    async def traffic() -> None:
        await asyncio.sleep(0.02)
        health.record_success()

    task = asyncio.create_task(traffic())
    elapsed = await timed(wait_until_due(0.05, interval=0.3, jitter=0.0, health=health))
    await task

    assert elapsed >= 0.3  # 0,05 sn'de değil; başarıdan (0,02) bir aralık (0,3) sonra


async def test_steady_traffic_keeps_pushing_the_probe_back():
    health = UpstreamHealth(300.0)

    async def traffic() -> None:
        await asyncio.sleep(0.02)
        health.record_success()  # 0,02 sn
        for _ in range(2):
            await asyncio.sleep(0.1)
            health.record_success()  # 0,12 sn ve 0,22 sn

    task = asyncio.create_task(traffic())
    elapsed = await timed(wait_until_due(0.05, interval=0.25, jitter=0.0, health=health))
    await task

    # İlk erteleme 0,27 sn'ye; o sırada 0,22'deki başarı görülür → 0,47 sn'ye.
    assert elapsed >= 0.45


async def test_success_older_than_this_wait_does_not_defer():
    health = UpstreamHealth(300.0)
    health.record_success()  # önceki turdan kalma
    await asyncio.sleep(0.01)

    elapsed = await timed(wait_until_due(0.05, interval=5.0, jitter=0.0, health=health))

    assert elapsed < 1.0


async def test_deferral_can_be_switched_off():
    health = UpstreamHealth(300.0)

    async def traffic() -> None:
        await asyncio.sleep(0.02)
        health.record_success()

    task = asyncio.create_task(traffic())
    elapsed = await timed(
        wait_until_due(0.05, interval=5.0, jitter=0.0, health=health, skip_when_active=False)
    )
    await task

    assert elapsed < 1.0


async def test_a_failed_request_does_not_defer_the_probe():
    """Yalnız başarı oturumun taze olduğunu gösterir; arıza probe'u ertelemez."""
    health = UpstreamHealth(300.0)

    async def traffic() -> None:
        await asyncio.sleep(0.02)
        health.record_failure("upstream_timeout")

    task = asyncio.create_task(traffic())
    elapsed = await timed(wait_until_due(0.05, interval=5.0, jitter=0.0, health=health))
    await task

    assert elapsed < 1.0


# ------------------------------------------------------ sıra doluyken
async def test_probe_waits_for_a_busy_queue(monkeypatch):
    monkeypatch.setattr(keepalive_module, "_BUSY_POLL_SECONDS", 0.01)
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    ticket = await queue.acquire("ask")

    async def finish() -> None:
        await asyncio.sleep(0.2)
        queue.release(ticket)

    task = asyncio.create_task(finish())
    elapsed = await timed(wait_until_due(0.01, interval=5.0, jitter=0.0, queue=queue))
    await task

    assert elapsed >= 0.2


async def test_probe_does_not_wait_for_a_busy_queue_forever(monkeypatch):
    monkeypatch.setattr(keepalive_module, "_BUSY_POLL_SECONDS", 0.01)
    monkeypatch.setattr(keepalive_module, "_BUSY_MAX_WAIT_SECONDS", 0.1)
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    ticket = await queue.acquire("ask")

    elapsed = await timed(wait_until_due(0.01, interval=5.0, jitter=0.0, queue=queue))

    assert 0.1 <= elapsed < 2.0
    queue.release(ticket)


async def test_probe_that_timed_out_while_queued_is_not_an_outage():
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    holder = await queue.acquire("ask")
    probe = asyncio.create_task(queue.acquire("keepalive"))
    await asyncio.sleep(0)
    unanswered = ProbeResult(ok=False, status_code=None, code="upstream_timeout")

    assert _never_served(queue, unanswered) is True

    # Probe işlenirken (upstream'e gitmişken) süresi dolduysa bu gerçek bir arızadır.
    queue.release(holder)
    ticket = await probe
    ticket.touched = True
    assert _never_served(queue, unanswered) is False
    queue.release(ticket)


async def test_answered_probes_are_never_treated_as_queued():
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    await queue.acquire("ask")
    waiter = asyncio.create_task(queue.acquire("keepalive"))
    await asyncio.sleep(0)

    assert _never_served(queue, ProbeResult(ok=True, status_code=200)) is False
    assert _never_served(queue, ProbeResult(ok=False, status_code=429, code="x")) is False
    assert _never_served(None, ProbeResult(ok=False, status_code=None, code="x")) is False
    waiter.cancel()


def test_only_unanswered_probes_are_reported_to_health():
    """HTTP cevabı dönen probe'un sonucunu servis zaten kaydetmiştir."""
    health = UpstreamHealth(300.0)

    _report_health(health, ProbeResult(ok=True, status_code=200))
    _report_health(health, ProbeResult(ok=False, status_code=429, code="upstream_quota_reached"))
    assert health.failing is False

    _report_health(health, ProbeResult(ok=False, status_code=None, code="upstream_timeout"))
    assert health.failing is True
    assert health.snapshot()["code"] == "upstream_timeout"
    _report_health(None, ProbeResult(ok=False, status_code=None, code="x"))  # hata vermez


# ------------------------------------------------------ döngünün kendisi
class RecordingAlerter:
    def __init__(self) -> None:
        self.results: list[ProbeResult] = []

    async def notify(self, result: ProbeResult) -> None:
        self.results.append(result)


def fake_client(post):
    """`httpx.AsyncClient` yerine geçen, `post`'u verilen işleve bağlayan sınıf."""

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> Any:
            return await post(url, **kwargs)

    return FakeClient


async def run_one_probe(monkeypatch, post, *, queue: RequestQueue, health: UpstreamHealth):
    """Keepalive döngüsünü ilk probe bitene dek çalıştırır; alerter'ı döndürür."""
    posted = asyncio.Event()

    async def wrapped(url: str, **kwargs: Any) -> Any:
        try:
            return await post(url, **kwargs)
        finally:
            posted.set()

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", fake_client(wrapped))
    alerter = RecordingAlerter()
    settings = make_settings(
        upstream_keepalive_interval_seconds=30.0,
        upstream_keepalive_initial_delay_seconds=0.0,
    )
    task = asyncio.create_task(
        run_upstream_keepalive(
            settings,
            alerter=alerter,  # type: ignore[arg-type]
            health=health,
            queue=queue,
        )
    )
    await asyncio.wait_for(posted.wait(), timeout=2)
    await asyncio.sleep(0.05)  # sonucun işlenmesi için
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return alerter


async def test_probe_left_unanswered_is_reported_as_upstream_timeout(monkeypatch):
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    health = UpstreamHealth(300.0)

    async def post(_url: str, **_kwargs: Any) -> Any:
        raise httpx.ReadTimeout("180 sn doldu")

    alerter = await run_one_probe(monkeypatch, post, queue=queue, health=health)

    assert [result.code for result in alerter.results] == ["upstream_timeout"]
    assert alerter.results[0].status_code is None
    # Dış istekler aynı duvara tek tek çarpmasın: arıza sağlık kaydına işlendi.
    assert health.snapshot()["code"] == "upstream_timeout"


async def test_probe_that_cannot_connect_is_wrapper_unreachable(monkeypatch):
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    health = UpstreamHealth(300.0)

    async def post(_url: str, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("bağlantı reddedildi")

    alerter = await run_one_probe(monkeypatch, post, queue=queue, health=health)

    assert [result.code for result in alerter.results] == ["wrapper_unreachable"]


async def test_probe_stuck_behind_real_traffic_raises_no_alarm(monkeypatch):
    """Araya giren istek yüzünden sırada süresi dolan probe: alarm yok, bekletme yok."""
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    health = UpstreamHealth(300.0)
    held: list[Any] = []

    async def post(_url: str, **_kwargs: Any) -> Any:
        # Probe gönderildiği anda sıra doldu: önde gerçek bir istek var, probe bekliyor.
        held.append(await queue.acquire("ask"))
        held.append(asyncio.create_task(queue.acquire("keepalive")))
        await asyncio.sleep(0)
        raise httpx.ReadTimeout("180 sn doldu")

    alerter = await run_one_probe(monkeypatch, post, queue=queue, health=health)

    assert alerter.results == []
    assert health.failing is False
    held[1].cancel()


async def test_probe_identifies_itself_to_the_queue(monkeypatch):
    queue = RequestQueue(min_gap=0.0, max_gap=0.0)
    health = UpstreamHealth(300.0)
    seen: dict[str, Any] = {}

    class Response:
        status_code = 200

    async def post(url: str, **kwargs: Any) -> Any:
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return Response()

    alerter = await run_one_probe(monkeypatch, post, queue=queue, health=health)

    assert seen["url"] == "/v1/chat/completions"
    assert seen["headers"]["x-apiwrapper-source"] == "keepalive"
    assert [result.ok for result in alerter.results] == [True]


# ---------------------------------------------- sıranın reddettiği probe
@pytest.mark.parametrize(("status", "code"), [(429, "queue_full"), (503, "queue_timeout")])
def test_probe_rejected_by_the_wrappers_own_queue_is_not_an_outage(status, code):
    rejected = ProbeResult(ok=False, status_code=status, code=code)
    assert _never_served(RequestQueue(), rejected) is True
    assert _never_served(None, rejected) is True  # sıra nesnesi olmasa da kod yeterli


def test_real_upstream_answers_are_still_reported():
    queue = RequestQueue()
    for status, code in ((429, "upstream_quota_reached"), (502, "upstream_unauthorized")):
        assert _never_served(queue, ProbeResult(ok=False, status_code=status, code=code)) is False


class JsonResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.text = json.dumps(payload)


async def test_probe_skipped_for_the_queue_is_retried_soon_not_an_interval_later(monkeypatch):
    """Atlanan probe tam aralığı (canlıda 60 dk) beklemez; yarım dakika sonra yeniden denenir."""
    monkeypatch.setattr(keepalive_module, "_RETRY_AFTER_SKIP_SECONDS", 0.05)
    calls: list[float] = []
    second = asyncio.Event()

    async def post(_url: str, **_kwargs: Any) -> Any:
        calls.append(time.monotonic())
        if len(calls) == 1:
            return JsonResponse(503, {"error": {"code": "queue_timeout", "message": "x"}})
        second.set()
        return JsonResponse(200, {"choices": []})

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", fake_client(post))
    alerter = RecordingAlerter()
    health = UpstreamHealth(300.0)
    settings = make_settings(
        upstream_keepalive_interval_seconds=30.0,  # tam aralık: test süresinden çok uzun
        upstream_keepalive_initial_delay_seconds=0.0,
    )
    task = asyncio.create_task(
        run_upstream_keepalive(
            settings,
            alerter=alerter,  # type: ignore[arg-type]
            health=health,
            queue=RequestQueue(min_gap=0.0, max_gap=0.0),
        )
    )
    await asyncio.wait_for(second.wait(), timeout=2)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 2
    assert calls[1] - calls[0] < 1.5
    # Atlanan probe ne alarm üretti ne bekletme; ikincisi sağlıklı sonuç bildirdi.
    assert [result.ok for result in alerter.results] == [True]
    assert health.failing is False


def test_only_the_probe_gets_a_short_queue_wait():
    assert max_queue_wait("keepalive") == 60.0
    assert max_queue_wait("api") is None
    assert max_queue_wait("telegram") is None
    assert max_queue_wait("ask") is None


# ------------------------------------------------- akış kontrolü (günlük)
class SseResponse:
    status_code = 200

    def __init__(self, *events: str) -> None:
        self.text = "".join(f"data: {event}\n\n" for event in events)


def sse_client(response: Any):
    class Client:
        async def post(self, *_args: Any, **_kwargs: Any) -> Any:
            return response

    return Client()


ROLE_CHUNK = '{"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}'
TEXT_CHUNK = '{"choices":[{"index":0,"delta":{"content":"merhaba"}}]}'
FINISH_CHUNK = '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'


async def test_stream_check_passes_on_a_real_answer():
    response = SseResponse(ROLE_CHUNK, TEXT_CHUNK, FINISH_CHUNK, "[DONE]")
    result = await keepalive_module.stream_smoke(sse_client(response), {}, make_settings())
    assert result.ok is True


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ('{"error":{"message":"x","type":"upstream_error","code":"upstream_quota_reached"}}',
         "upstream_quota_reached"),
        ('{"error":{"message":"x","type":"upstream_error","code":null}}', "upstream_error"),
        ('{"error":"düz metin hata"}', "upstream_failure"),
    ],
)
async def test_stream_check_sees_an_error_reported_inside_the_stream(error, code):
    """Akışlı uçta hata HTTP 200'ün içinde gelir. Önceden "veri aktı" diye başarı
    sayılıyor, arıza sürerken "yeniden çalışıyor" mesajına yol açabiliyordu."""
    response = SseResponse(ROLE_CHUNK, error, "[DONE]")

    result = await keepalive_module.stream_smoke(sse_client(response), {}, make_settings())

    assert result.ok is False
    assert result.code == code
    assert result.status_code == 200


async def test_stream_check_rejected_by_the_queue_is_skipped():
    error = '{"error":{"message":"x","type":"server_error","code":"queue_timeout"}}'
    response = SseResponse(ROLE_CHUNK, error, "[DONE]")

    result = await keepalive_module.stream_smoke(sse_client(response), {}, make_settings())

    assert result.code == "queue_timeout"
    assert _never_served(RequestQueue(), result) is True


async def test_answer_text_that_mentions_errors_is_not_mistaken_for_one():
    chatty = '{"choices":[{"index":0,"delta":{"content":"the \\"error\\" key is common"}}]}'
    response = SseResponse(ROLE_CHUNK, chatty, FINISH_CHUNK, "[DONE]")

    result = await keepalive_module.stream_smoke(sse_client(response), {}, make_settings())

    assert result.ok is True
