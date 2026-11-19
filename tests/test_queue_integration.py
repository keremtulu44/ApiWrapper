"""Sıranın, sağlık kaydının ve yardımcıların servise bağlanışı."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import app.telegram.bot as bot_module
from app.api.disconnect import cancel_on_disconnect, wait_for_disconnect
from app.core.config import Settings
from app.core.diagnostics import explain_failure
from app.core.errors import ClientClosedRequestError
from app.main import create_app
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.completion_service import Admission
from app.telegram.bot import TelegramSettings, run_from_settings
from app.upstream.client import UpstreamErrorNote, error_excerpt
from app.utils.tasks import TaskRegistry
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"
CHAT = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


def stream_route():
    return respx.post(url__startswith=STREAM_URL)


# --------------------------------------------------------------- ayarlar
def test_defaults_match_what_was_agreed_with_the_operator():
    settings = Settings(_env_file=None, target_domain="example.test")
    # 30 sn sonra ilk "bekle", sonra 15 sn'de bir.
    assert settings.ask_first_wait_seconds == 30.0
    assert settings.ask_wait_interval_seconds == 15.0
    # İstek başına 180 sn.
    assert settings.ask_request_timeout_seconds == 180.0
    assert settings.ask_max_queue_wait_seconds == 600.0
    assert settings.ask_problem_hold_seconds == 300.0
    # Tek sıra + rastgele 5–10 sn.
    assert settings.request_queue_enabled is True
    assert settings.request_gap_min_seconds == 5.0
    assert settings.request_gap_max_seconds == 10.0
    assert settings.request_queue_max_waiting == 20
    assert settings.upstream_keepalive_skip_when_active is True


#: Canlı sürümün (`03aca2c`) varsayılan kota işaretleri. Liste, upstream'e bir
#: isteğin kaç kez gönderileceğini belirlediği için **gönderme tarafının**
#: parçasıdır; operatörün kararı olmadan değişmez (docs/LESSONS §20).
LIVE_QUOTA_MARKERS = [
    "upstream limit reached",
    "message limit reached",
    "conversation limit reached",
    "daily limit reached",
    "usage limit reached",
    "quota exceeded",
    "rate limit exceeded",
    "too many requests",
]
SPEND_LIMIT_BODY = b'{"error":"daily spend limit reached"}'


def _documented_opt_in() -> str:
    """`.env.example`'daki yorum satırı: 18 Kasım gövdesini kota sayan tam liste."""
    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    lines = [
        line[2:].split("=", 1)[1]
        for line in template.splitlines()
        if line.startswith("# UPSTREAM_LIMIT_MARKERS=")
    ]
    assert len(lines) == 1, "şablonda tek bir isteğe bağlı UPSTREAM_LIMIT_MARKERS satırı olmalı"
    return lines[0]


def test_default_quota_markers_are_exactly_the_live_list():
    """Varsayılan liste canlıdakiyle aynı: `spend limit reached` kendiliğinden eklenmez."""
    settings = Settings(_env_file=None, target_domain="example.test")
    assert settings.upstream_limit_markers == LIVE_QUOTA_MARKERS


@respx.mock
def test_daily_spend_limit_is_retried_by_default_like_the_live_version():
    """Canlıdaki davranış korunur: bu gövde kota sayılmaz, 429 tekrar denenir.

    Canlı ağaçta aynı girdiyle ölçüldü: istek başına 3 POST, kod
    `upstream_rate_limited` (docs/LESSONS §20).
    """
    route = stream_route().mock(return_value=httpx.Response(429, content=SPEND_LIMIT_BODY))
    app = create_app(
        make_settings(retry_max_attempts=3, retry_base_delay=0.001, retry_max_delay=0.002)
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )
        cooling = client.app.state.accounts.cooldown_remaining(1)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_rate_limited"
    assert route.call_count == 3  # canlıdaki gibi: aynı istek üç kez gönderildi
    assert cooling == 0  # hesap dinlenmeye alınmadı


@respx.mock
def test_daily_spend_limit_is_answered_at_once_when_the_operator_lists_it():
    """18 Kasım koruması isteğe bağlıdır: `.env.example`'daki satırla açılır."""
    value = _documented_opt_in()
    settings = Settings(_env_file=None, target_domain="example.test", upstream_limit_markers=value)
    assert settings.upstream_limit_markers == [*LIVE_QUOTA_MARKERS, "spend limit reached"]

    route = stream_route().mock(return_value=httpx.Response(429, content=SPEND_LIMIT_BODY))
    app = create_app(make_settings(retry_max_attempts=3, upstream_limit_markers=value))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"
    assert route.call_count == 1  # tekrar denenmedi


def test_the_env_line_in_the_runbook_is_the_tested_one_and_shows_in_admin_config(monkeypatch):
    """Operatör korumayı env ile açar (karar: 19 Kasım 2026).

    Canlıya alma adımlarında (`ROLLBACK §13.3`) kopyalanacak değer şablondaki
    sınanmış satırla aynıdır; ortam değişkeni olarak girildiğinde uygulama onu
    dokuz ifade olarak okur ve belgede söylenen doğrulama ucu bunu gösterir.
    """
    runbook = (Path(__file__).resolve().parents[1] / "docs" / "ROLLBACK_2026_11_16.md").read_text(
        "utf-8"
    )
    lines = runbook.splitlines()
    at = lines.index("Key:    UPSTREAM_LIMIT_MARKERS")
    assert lines[at + 1].startswith("Value:  ")
    value = lines[at + 1].removeprefix("Value:  ")
    assert value == _documented_opt_in()

    monkeypatch.setenv("UPSTREAM_LIMIT_MARKERS", value)
    expected = [*LIVE_QUOTA_MARKERS, "spend limit reached"]
    with TestClient(create_app(make_settings())) as client:
        shown = client.get(
            "/v1/admin/config", headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        ).json()["quota"]["markers"]
    assert shown == expected

    # Belgedeki iki uyarının gerekçesi: tırnak değerin parçası olur; yalnız yeni
    # ifadeyi yazmak öbür sekizini siler (env listesi varsayılanın YERİNE geçer).
    monkeypatch.setenv("UPSTREAM_LIMIT_MARKERS", f'"{value}"')
    quoted = make_settings().upstream_limit_markers
    assert quoted[0] == '"upstream limit reached' and quoted[-1] == 'spend limit reached"'
    monkeypatch.setenv("UPSTREAM_LIMIT_MARKERS", "spend limit reached")
    assert make_settings().upstream_limit_markers == ["spend limit reached"]


# ---------------------------------------------------------------- bağlantı
def test_one_queue_health_and_alerter_are_shared_by_everything(client):
    state = client.app.state
    assert state.completion_service.queue is state.queue
    assert state.completion_service.health is state.upstream_health
    assert isinstance(state.tasks, TaskRegistry)
    assert state.alerter is None  # testlerde Telegram yapılandırılmamış


def test_ask_route_is_listed_and_documented(client):
    assert "/v1/ask" in client.get("/").json()["endpoints"]
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/v1/ask"]
    assert "get" in paths["/v1/admin/queue"]


@respx.mock
def test_every_completion_goes_through_the_queue_and_reports_its_outcome(client):
    stream_route().mock(return_value=httpx.Response(200, content=ai_stream("tamam")))
    state = client.app.state
    assert state.upstream_health.last_success_at is None

    assert client.post("/v1/chat/completions", json=CHAT).status_code == 200

    assert state.queue.snapshot()["served_total"] == 1
    assert state.queue.snapshot()["active"] is None
    assert state.upstream_health.last_success_at is not None


@respx.mock
def test_streaming_completion_holds_and_releases_the_queue(client):
    stream_route().mock(return_value=httpx.Response(200, content=ai_stream("par", "ça")))
    state = client.app.state

    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True})

    assert response.status_code == 200
    assert "[DONE]" in response.text
    assert state.queue.snapshot()["served_total"] == 1
    assert state.queue.snapshot()["active"] is None
    assert state.upstream_health.last_success_at is not None


@respx.mock
def test_streaming_failure_is_reported_to_health(client):
    stream_route().mock(return_value=httpx.Response(500, content=b"internal error"))

    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True})

    assert response.status_code == 200  # akışta hata gövdede bildirilir
    assert client.app.state.upstream_health.snapshot()["code"] == "upstream_failure"
    assert client.app.state.queue.snapshot()["active"] is None


@respx.mock
def test_invalid_request_never_enters_the_queue_or_the_health_record(client):
    route = stream_route().mock(return_value=httpx.Response(200, content=ai_stream("x")))
    state = client.app.state

    missing_model = client.post("/v1/chat/completions", json={**CHAT, "model": "ghost"})
    no_messages = client.post("/v1/chat/completions", json={"model": "test-model", "messages": []})

    assert missing_model.status_code == 404
    assert no_messages.status_code == 400
    assert route.call_count == 0
    assert state.queue.snapshot()["served_total"] == 0
    assert state.upstream_health.failing is False
    assert state.upstream_health.last_success_at is None


@respx.mock
def test_last_upstream_error_is_kept_for_the_operator(client):
    stream_route().mock(
        return_value=httpx.Response(429, content=b'{"error":"daily spend limit reached"}')
    )

    client.post("/v1/chat/completions", json=CHAT)

    note = client.app.state.upstream.last_error
    assert note is not None
    assert note.status == 429
    assert note.describe() == 'HTTP 429 · {"error":"daily spend limit reached"}'
    assert client.app.state.upstream.error_since(note.at) is note
    assert client.app.state.upstream.error_since(note.at + 1) is None


@respx.mock
async def test_admission_reports_queue_and_processing_time():
    stream_route().mock(return_value=httpx.Response(200, content=ai_stream("tamam")))
    app = create_app(make_settings())
    request = ChatCompletionRequest(
        model="test-model", messages=[ChatMessage(role="user", content="selam")]
    )
    async with app.router.lifespan_context(app):
        admission = Admission(label="ask", deadline=5.0)
        response = await app.state.completion_service.create_completion(
            request, admission=admission
        )

    assert response.choices[0].message.content == "tamam"
    assert admission.started_at > 0
    assert admission.waited >= 0
    assert admission.duration > 0


# ------------------------------------------------------- istemci kopması
class FakeRequest:
    """`request.receive()` ve `request.url.path` sağlayan asgari istek."""

    class _Url:
        path = "/v1/chat/completions"

    url = _Url()

    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def receive(self) -> dict[str, Any]:
        return await self.messages.get()


async def test_work_that_finishes_returns_its_result():
    request = FakeRequest()

    async def work() -> str:
        await asyncio.sleep(0.01)
        return "cevap"

    assert await cancel_on_disconnect(request, work()) == "cevap"  # type: ignore[arg-type]


async def test_disconnect_cancels_the_work():
    request = FakeRequest()
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "geç"

    task = asyncio.create_task(cancel_on_disconnect(request, work()))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    await request.messages.put({"type": "http.disconnect"})

    with pytest.raises(ClientClosedRequestError) as error:
        await asyncio.wait_for(task, timeout=1)
    assert error.value.status_code == 499
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_disconnect_does_not_cut_work_already_sent_upstream():
    """Upstream'e gönderilmiş akışsız istek, istemcisi kopsa da sonuna kadar sürer.

    Canlı sürümde (`03aca2c`) böyleydi: akışsız uçta kopma izlenmiyordu. Gönderme
    tarafının davranışı değişmesin diye korunur (docs/LESSONS §20).
    """
    request = FakeRequest()
    cancelled = asyncio.Event()
    finished = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finished.set()
        return "cevap"

    task = asyncio.create_task(
        cancel_on_disconnect(request, work(), committed=lambda: True)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    await request.messages.put({"type": "http.disconnect"})

    assert await asyncio.wait_for(task, timeout=2) == "cevap"
    assert finished.is_set()
    assert not cancelled.is_set()


async def test_disconnect_still_cancels_work_that_was_not_sent_yet():
    """Sırada bekleyen (henüz gönderilmemiş) istek, istemcisi koparsa iptal edilir."""
    request = FakeRequest()
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "geç"

    task = asyncio.create_task(
        cancel_on_disconnect(request, work(), committed=lambda: False)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    await request.messages.put({"type": "http.disconnect"})

    with pytest.raises(ClientClosedRequestError):
        await asyncio.wait_for(task, timeout=1)
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_error_of_work_that_outlived_its_client_still_surfaces():
    """Gönderilmiş iş hatayla biterse hata olağan yoldan çıkar (yutulmaz)."""
    request = FakeRequest()

    async def work() -> str:
        await asyncio.sleep(0.05)
        raise ValueError("upstream hata verdi")

    task = asyncio.create_task(
        cancel_on_disconnect(request, work(), committed=lambda: True)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    await request.messages.put({"type": "http.disconnect"})

    with pytest.raises(ValueError, match="upstream hata verdi"):
        await asyncio.wait_for(task, timeout=2)


async def test_cancelling_the_handler_also_stops_work_that_outlived_its_client():
    """Sunucu kapanırken (işleyici iptal edilir) gönderilmiş iş de bırakılır."""
    request = FakeRequest()
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "geç"

    task = asyncio.create_task(
        cancel_on_disconnect(request, work(), committed=lambda: True)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.01)
    await request.messages.put({"type": "http.disconnect"})
    await asyncio.sleep(0.05)
    assert not task.done()  # istemci gitti ama iş sürüyor

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_other_messages_are_not_mistaken_for_a_disconnect():
    request = FakeRequest()
    await request.messages.put({"type": "http.request", "body": b"", "more_body": False})

    async def work() -> str:
        await asyncio.sleep(0.05)
        return "cevap"

    assert await cancel_on_disconnect(request, work()) == "cevap"  # type: ignore[arg-type]


async def test_errors_from_the_work_pass_through_unchanged():
    request = FakeRequest()

    async def work() -> str:
        raise ValueError("bozuk")

    with pytest.raises(ValueError, match="bozuk"):
        await cancel_on_disconnect(request, work())  # type: ignore[arg-type]


async def test_cancelling_the_handler_cancels_the_work_too():
    request = FakeRequest()
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "geç"

    task = asyncio.create_task(cancel_on_disconnect(request, work()))  # type: ignore[arg-type]
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_broken_receive_channel_is_not_treated_as_a_disconnect():
    class Broken(FakeRequest):
        async def receive(self) -> dict[str, Any]:
            raise RuntimeError("kanal bozuldu")

    watcher = asyncio.create_task(wait_for_disconnect(Broken()))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert not watcher.done()  # "koptu" demedi; iptal edilene dek bekliyor
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher


# ---------------------------------------------------------- arka plan işleri
async def test_task_registry_keeps_tasks_until_they_finish():
    registry = TaskRegistry()
    gate = asyncio.Event()

    async def job() -> int:
        await gate.wait()
        return 7

    task = registry.spawn(job(), name="iş")
    assert len(registry) == 1
    gate.set()
    assert await task == 7
    await asyncio.sleep(0)
    assert len(registry) == 0


async def test_task_registry_survives_a_failing_task():
    registry = TaskRegistry()

    async def job() -> None:
        raise RuntimeError("bildirim gönderilemedi")

    task = registry.spawn(job())
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert len(registry) == 0  # hata loglandı, kayıt temizlendi, kimse çökmedi


async def test_task_registry_shutdown_cancels_what_is_left():
    registry = TaskRegistry()
    stopped = asyncio.Event()

    async def job() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    registry.spawn(job())
    await asyncio.sleep(0)
    await registry.shutdown()
    assert stopped.is_set()
    assert len(registry) == 0


# ------------------------------------------------------------- hata özeti
def test_error_excerpt_is_one_line_and_bounded():
    text = '{\n  "error": "daily spend limit reached",\n  "detail":   "x"\n}'
    assert error_excerpt(text) == '{ "error": "daily spend limit reached", "detail": "x" }'
    assert len(error_excerpt("kelime " * 200)) <= 200
    assert error_excerpt("kelime " * 200).endswith("…")
    assert error_excerpt("") == ""


def test_error_excerpt_hides_tokens_but_keeps_error_codes():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123"
    assert error_excerpt(f"bad token {jwt} rejected") == "bad token [gizlendi] rejected"
    code = "daily_spend_limit_reached_for_this_account_today"
    assert error_excerpt(code) == code  # uzun ama okunur hata kodu gizlenmez


def test_error_note_describes_a_missing_answer():
    assert UpstreamErrorNote(1.0, None, "ReadTimeout").describe() == "cevap yok (ReadTimeout)"
    assert UpstreamErrorNote(1.0, 503, "").describe() == "HTTP 503"


# ---------------------------------------------------------------- teşhis
def test_new_failure_codes_have_their_own_explanations():
    for code in ("queue_full", "queue_timeout", "empty_response", "upstream_rate_limited"):
        hint = explain_failure(None, code)
        assert hint.severity == "warning", code
        assert hint.title != explain_failure(None, "bilinmeyen-kod").title, code


def test_captcha_rejection_is_no_longer_treated_as_permanent():
    hint = explain_failure(503, "recaptcha_rejected")
    assert hint.severity == "warning"
    assert "bekleyin" in hint.action


def test_unreachable_wrapper_text_is_only_about_the_connection():
    hint = explain_failure(None, "wrapper_unreachable")
    assert hint.severity == "critical"
    assert "bağlantı" in hint.title.lower()


# -------------------------------------------------------------- Telegram
async def test_telegram_bot_tags_its_requests_for_the_queue(monkeypatch):
    created: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    class FakeBot:
        def __init__(self, *_args: Any) -> None:
            pass

        async def run(self) -> None:
            return None

    monkeypatch.setattr(bot_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(bot_module, "TelegramBot", FakeBot)
    settings = TelegramSettings(
        _env_file=None,
        telegram_bot_token="123:test",
        telegram_allowed_chat_ids="42",
        wrapper_api_key="sk-wrapper",
    )

    await run_from_settings(settings)

    wrapper_client = created[0]
    assert wrapper_client["headers"]["x-apiwrapper-source"] == "telegram"
    assert wrapper_client["headers"]["Authorization"] == "Bearer sk-wrapper"
    assert "x-apiwrapper-source" not in json.dumps(created[1].get("headers", {}))
