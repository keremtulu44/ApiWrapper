"""Olay sonrası sertleştirme testleri.

Kapsam:
* HTTP 409 `refresh_required` sınıflandırması ve devre kesiciye sayılmaması,
* kota/401 hatalarının devre kesiciyi açmaması, 401'de hesap devri,
* `chunked` gövde sınırı (413),
* stream jeneratörünün iptal/kapanışta bozulmaması ve `[DONE]` garantisi,
* anlamlı + spam olmayan Telegram bildirimleri,
* `/health` ve `/metrics` bilgi sızıntısı sertleştirmesi.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.diagnostics import format_failure
from app.main import create_app
from app.services.alerting import AlertPolicy, ProbeResult
from app.services.keepalive import classify_response
from app.services.model_registry import ModelEntry, ModelRegistry
from app.upstream.breaker import BreakerState
from tests.conftest import (
    TEST_API_KEY,
    UPSTREAM_DOMAIN,
    ai_stream,
    make_settings,
)

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"
REFRESH_BODY = '{"error":"refresh_required","reason":"forced_update"}'


def _stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


def _registry() -> ModelRegistry:
    return ModelRegistry([ModelEntry(id="test-model", upstream_id="model-a")])


# --------------------------------------------------------------- 409 refresh
@respx.mock
def test_refresh_required_is_actionable_and_skips_breaker():
    """409 refresh_required: net kod + mesaj, retry yok, devre kesici kapalı kalır."""
    app = create_app(make_settings(breaker_enabled=True, breaker_failure_threshold=1))
    route = _stream_route(respx).mock(
        return_value=httpx.Response(409, text=REFRESH_BODY)
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        # threshold=1: tek bir "arıza" bile olsaydı devre açılırdı.
        for _ in range(3):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert response.status_code == 503
            error = response.json()["error"]
            assert error["code"] == "upstream_refresh_required"
            assert "UPSTREAM_CURL" in error["message"]
            # Sağlayıcı host'u istemciye sızmaz.
            assert UPSTREAM_DOMAIN not in error["message"]
            assert response.headers["retry-after"] == "300"

        assert route.call_count == 3  # retry edilmedi
        assert app.state.upstream.breaker.state is BreakerState.CLOSED


def test_refresh_marker_detection_is_specific():
    """Genel 'update' sözü refresh sayılmamalı; gerçek işaretler sayılmalı."""
    from app.upstream.client import UpstreamClient

    assert UpstreamClient._refresh_marker(409, REFRESH_BODY) == "refresh_required"
    assert UpstreamClient._refresh_marker(409, "") == "http_409"
    assert UpstreamClient._refresh_marker(409, '{"error":"please update later"}') is None
    assert UpstreamClient._refresh_marker(400, REFRESH_BODY) == "refresh_required"


# ------------------------------------------------------------ breaker policy
@respx.mock
def test_quota_errors_do_not_open_the_breaker():
    """Kota bir erişilebilirlik arızası değildir; sağlıklı hesapları bloklamamalı."""
    app = create_app(make_settings(breaker_enabled=True, breaker_failure_threshold=1))
    _stream_route(respx).mock(
        return_value=httpx.Response(429, text='{"error":"upstream limit reached"}')
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(3):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "upstream_quota_reached"
        assert app.state.upstream.breaker.state is BreakerState.CLOSED


@respx.mock
def test_server_errors_still_open_the_breaker():
    """Gerçek erişilebilirlik arızası (5xx) devre kesiciyi açmaya devam etmeli."""
    app = create_app(
        make_settings(
            breaker_enabled=True,
            breaker_failure_threshold=2,
            retry_max_attempts=1,
        )
    )
    _stream_route(respx).mock(return_value=httpx.Response(500, text="boom"))
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(2):
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert app.state.upstream.breaker.state is BreakerState.OPEN


# --------------------------------------------------------- hesap devri (401)
@respx.mock
def test_auth_rejection_switches_to_the_next_account():
    """İlk hesabın oturumu düşmüşse istek ikinci hesapla tamamlanır."""
    settings = make_settings(
        upstream_cookie="session=first",
        upstream_cookie_2="session=second",
        breaker_enabled=True,
        breaker_failure_threshold=1,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "session=first" in request.headers.get("cookie", ""):
            return httpx.Response(401, text="")
        return httpx.Response(200, content=ai_stream("merhaba"))

    route = _stream_route(respx).mock(side_effect=handler)
    app = create_app(settings)
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        assert app.state.accounts.size == 2
        response = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert "merhaba" in response.json()["choices"][0]["message"]["content"]
    assert route.call_count == 2
    assert app.state.upstream.breaker.state is BreakerState.CLOSED


# ------------------------------------------------------------- body limiting
def test_chunked_body_over_limit_is_rejected():
    """`Content-Length` yoksa da akan gövde sayılır ve 413 döner."""
    from fastapi.testclient import TestClient

    app = create_app(make_settings(max_body_bytes=1024))
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})

        def chunks():
            for _ in range(4):
                yield b"x" * 400

        response = client.post("/v1/chat/completions", content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_declared_content_length_is_rejected_early():
    from fastapi.testclient import TestClient

    app = create_app(make_settings(max_body_bytes=16))
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = client.post("/v1/chat/completions", content=b"y" * 64)
    assert response.status_code == 413


# ------------------------------------------------------------------- streams
@respx.mock
def test_closing_stream_generator_midway_does_not_raise():
    """İstemci koptuğunda jeneratör RuntimeError üretmeden kapanmalı."""
    import asyncio

    from fastapi.testclient import TestClient

    _stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("bir", "iki"))
    )
    app = create_app(make_settings())
    with TestClient(app):
        service = app.state.completion_service
        request = _chat_request()
        metrics_before = _active_streams()

        async def scenario() -> None:
            agen = service.stream_completion(request)
            first = await agen.__anext__()
            assert "chat.completion.chunk" in first
            # Starlette'te istemci kopması bu noktaya denk gelir.
            await agen.aclose()

        asyncio.run(scenario())
        assert _active_streams() == metrics_before


@respx.mock
def test_stream_always_ends_with_done_sentinel():
    from fastapi.testclient import TestClient

    _stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("tek"))
    )
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as response:
            body = "".join(response.iter_text())
    assert body.rstrip().endswith("data: [DONE]")


@respx.mock
def test_stream_error_still_sends_done_after_error_event():
    from fastapi.testclient import TestClient

    _stream_route(respx).mock(return_value=httpx.Response(500, text="boom"))
    app = create_app(make_settings(retry_max_attempts=1))
    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as response:
            body = "".join(response.iter_text())
    assert '"error"' in body
    assert body.rstrip().endswith("data: [DONE]")


def _chat_request():
    from app.schemas.openai import ChatCompletionRequest

    return ChatCompletionRequest(
        model="test-model", messages=[{"role": "user", "content": "hi"}]
    )


def _active_streams() -> float:
    from app.core.metrics import metrics

    return metrics._counters.get("apiwrapper_active_streams", {}).get((), 0.0)


# ------------------------------------------------------------------- alerts
def test_alert_policy_reports_first_failure_with_action():
    policy = AlertPolicy(repeat_seconds=3600)
    text = policy.evaluate(ProbeResult(ok=False, status_code=503, code="upstream_refresh_required"))
    assert text is not None
    assert "upstream_refresh_required" in text
    assert "UPSTREAM_CURL" in text  # eylem maddesi
    assert "503" in text


def test_alert_policy_does_not_spam_on_repeated_same_failure():
    policy = AlertPolicy(repeat_seconds=3600)
    result = ProbeResult(ok=False, status_code=503, code="upstream_refresh_required")
    assert policy.evaluate(result, now=0.0) is not None
    assert policy.evaluate(result, now=60.0) is None
    assert policy.evaluate(result, now=3599.0) is None
    reminder = policy.evaluate(result, now=3600.0)
    assert reminder is not None and "sürüyor" in reminder
    assert policy.evaluate(result, now=3700.0) is None


def test_alert_policy_reports_code_change_and_recovery():
    policy = AlertPolicy(repeat_seconds=3600)
    assert policy.evaluate(
        ProbeResult(ok=False, status_code=503, code="upstream_refresh_required"), now=0.0
    )
    changed = policy.evaluate(
        ProbeResult(ok=False, status_code=429, code="upstream_quota_reached"), now=30.0
    )
    assert changed is not None and "kota" in changed.lower()
    recovered = policy.evaluate(ProbeResult(ok=True, status_code=200), now=120.0)
    assert recovered is not None and "son kontrol başarılı" in recovered
    # Toparlanma sonrası sessizlik.
    assert policy.evaluate(ProbeResult(ok=True, status_code=200), now=180.0) is None


def test_probe_classification_extracts_error_code():
    response = httpx.Response(
        503,
        json={"error": {"message": "x", "code": "upstream_refresh_required"}},
    )
    result = classify_response(response, 12.5)
    assert result.ok is False
    assert result.code == "upstream_refresh_required"
    assert result.status_code == 503
    assert result.duration_ms == 12.5

    ok = classify_response(httpx.Response(200, json={"choices": []}))
    assert ok.ok is True


def test_telegram_failure_text_is_meaningful():
    text = format_failure(502, "upstream_refresh_required")
    assert "eskimiş" in text
    assert "UPSTREAM_CURL" in text
    assert "upstream_failure" not in text
    # Eşleşmeyen kod: durum kodundan anlamlı bir yedek üretilmeli.
    fallback = format_failure(504, None)
    assert "zaman aşımı" in fallback.lower()


@pytest.mark.asyncio
async def test_keepalive_alerts_on_failure_via_fake_alerter(monkeypatch):
    from app.services import keepalive as keepalive_module

    class FakeResponse:
        status_code = 503
        text = '{"error": {"code": "upstream_refresh_required", "message": "x"}}'

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            raise __import__("asyncio").CancelledError

    class RecordingAlerter:
        def __init__(self):
            self.results: list[ProbeResult] = []

        async def notify(self, result: ProbeResult):
            self.results.append(result)

    # İlk probe'yu yakalamak için post'u tek seferlik başarıyla cevaplayan istemci.
    import asyncio

    class OneShotClient(FakeClient):
        def __init__(self, **_kwargs):
            self.calls = 0

        async def post(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse()
            raise asyncio.CancelledError

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", OneShotClient)
    settings = make_settings(
        upstream_keepalive_interval_seconds=0.01,
        upstream_keepalive_initial_delay_seconds=0.0,
    )
    alerter = RecordingAlerter()

    task = asyncio.create_task(
        keepalive_module.run_upstream_keepalive(settings, alerter=alerter)  # type: ignore[arg-type]
    )
    for _ in range(100):
        if alerter.results:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert alerter.results, "keepalive arızayı bildirmedi"
    assert alerter.results[0].code == "upstream_refresh_required"


# ----------------------------------------------------------------- health/ops
def test_health_hides_upstream_host_by_default():
    from fastapi.testclient import TestClient

    app = create_app(make_settings())
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert "base_url" not in body["upstream"]
    assert body["upstream"]["configured"] is True


def test_health_can_expose_upstream_when_explicitly_enabled():
    from fastapi.testclient import TestClient

    app = create_app(make_settings(health_expose_upstream=True))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["upstream"]["base_url"] == f"https://{UPSTREAM_DOMAIN}"


def test_metrics_can_require_an_api_key():
    from fastapi.testclient import TestClient

    app = create_app(make_settings(metrics_require_auth=True))
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get("/metrics", headers={"Authorization": f"Bearer {TEST_API_KEY}"})
        assert ok.status_code == 200
        assert "apiwrapper_requests_total" in ok.text
