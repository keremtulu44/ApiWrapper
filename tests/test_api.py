"""Uçtan uca API testleri (upstream respx ile mock'lanır)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


# ------------------------------------------------------------------ system
def test_health_is_public(client):
    response = client.get("/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_endpoint(client):
    client.get("/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "apiwrapper_requests_total" in response.text


def test_request_id_header_echoed(client):
    response = client.get("/health", headers={"x-request-id": "my-id"})
    assert response.headers["x-request-id"] == "my-id"


# -------------------------------------------------------------------- auth
def test_missing_api_key_rejected(client):
    response = client.get("/v1/models", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_wrong_api_key_rejected(client):
    response = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# ------------------------------------------------------------------ models
def test_list_models(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {"test-model", "second-model"}


def test_retrieve_model_by_alias(client):
    response = client.get("/v1/models/alias-model")
    assert response.status_code == 200
    assert response.json()["id"] == "test-model"


def test_unknown_model_returns_404(client):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_unprefixed_openai_routes_are_aliased(client):
    """OpenAI SDK base_url '/v1' olmadan verilirse /chat/completions 404 olmamalı."""
    models = client.get("/models")
    assert models.status_code == 200
    assert {m["id"] for m in models.json()["data"]} == {"test-model", "second-model"}

    missing = client.post(
        "/chat/completions",
        json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"


def test_unknown_local_route_has_helpful_404(client):
    response = client.get("/v1/this-does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert "/v1/chat/completions" in error["message"]


# ------------------------------------------------------------- completions
@respx.mock
def test_non_streaming_completion(client):
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("Merhaba", " dünya"))
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Merhaba dünya"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 11
    assert body["usage"]["completion_tokens"] == 7
    assert body["model"] == "test-model"

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["modelAId"] == "upstream-model-a"
    assert sent["userMessage"]["content"] == "selam"
    assert sent["modality"] == "chat"
    request_headers = route.calls[0].request.headers
    assert request_headers["content-type"] == "text/plain;charset=UTF-8"
    assert request_headers["origin"] == f"https://{UPSTREAM_DOMAIN}"
    assert request_headers["referer"].startswith(f"https://{UPSTREAM_DOMAIN}/c/")


@respx.mock
def test_reasoning_deltas_are_not_returned_as_content(client):
    """Düşünme (thinking) modelleri: `g:` delta'ları yanıta girmez.

    Upstream düşünme adımlarını ayrı olay türüyle gönderir; istemciye yalnızca
    metin delta'ları iletilir. Continue gibi istemciler ham düşünme zincirini
    değil, nihai cevabı görür.
    """
    body = (
        'f:{"messageId":"msg-think"}\n'
        'g:"önce sorunu parçalara ayırıyorum"\n'
        'ag:"ikinci adım"\n'
    ).encode() + ai_stream("cevap ", "burada")
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )

    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert content == "cevap burada"
    assert "parçalara" not in content
    assert "ikinci adım" not in content


@respx.mock
def test_streaming_completion_sse_shape(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("Bir", " iki"))
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "say"}],
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())

    payloads = [
        line[len("data: ") :]
        for line in raw.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(
        c["choices"][0]["delta"].get("content") or ""
        for c in chunks
        if c.get("choices")
    )
    assert text == "Bir iki"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@respx.mock
def test_streaming_include_usage(client):
    stream_route(respx).mock(return_value=httpx.Response(200, content=ai_stream("x")))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "say"}],
        },
    ) as response:
        raw = "".join(response.iter_text())

    chunks = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    usage_chunks = [c for c in chunks if c.get("usage")]
    assert usage_chunks
    assert usage_chunks[-1]["usage"]["total_tokens"] > 0


@respx.mock
def test_stop_sequence_truncates_output(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("hello ", "STOP", " tail"))
    )
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["STOP"],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello "


@respx.mock
def test_upstream_error_becomes_502(client):
    stream_route(respx).mock(return_value=httpx.Response(500, content=b"boom"))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


@respx.mock
def test_upstream_404_is_actionable(client):
    stream_route(respx).mock(return_value=httpx.Response(404, content=b"not found"))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_not_found"
    assert "UPSTREAM_STREAM_PATH" in error["message"]
    assert "curl_to_env" in error["message"]


@respx.mock
def test_upstream_redirect_is_not_followed_silently(client):
    stream_route(respx).mock(
        return_value=httpx.Response(
            301, headers={"location": "/nextjs-api/stream/create-evaluation"}
        )
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_redirect"
    assert "create-evaluation" in error["message"]


@respx.mock
def test_stream_path_without_chat_id_placeholder_is_used():
    """Yer tutucusuz yol (create-evaluation) chat_id eklemeden çağrılmalı."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    url = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/create-evaluation"
    route = respx.post(url).mock(return_value=httpx.Response(200, content=ai_stream("ok")))
    app = create_app(
        make_settings(upstream_stream_path="/nextjs-api/stream/create-evaluation")
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert str(route.calls[0].request.url) == url


@respx.mock
def test_upstream_captcha_rejection_surfaces_clear_error(client):
    stream_route(respx).mock(
        return_value=httpx.Response(403, content=b'{"error":"invalid recaptcha token"}')
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "recaptcha_rejected"


@respx.mock
def test_stream_error_is_delivered_inside_sse(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=b'3:"upstream exploded"\n')
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as response:
        raw = "".join(response.iter_text())
    assert "upstream exploded" in raw
    assert raw.rstrip().endswith("[DONE]")


@respx.mock
def test_legacy_completions_endpoint(client):
    stream_route(respx).mock(return_value=httpx.Response(200, content=ai_stream("ok")))
    response = client.post(
        "/v1/completions", json={"model": "test-model", "prompt": "merhaba"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "ok"


# ------------------------------------------------------------- validation
def test_empty_messages_rejected(client):
    response = client.post("/v1/chat/completions", json={"model": "test-model", "messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "messages"


def test_n_greater_than_one_rejected(client):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 3,
        },
    )
    assert response.status_code == 400


def test_malformed_body_returns_422(client):
    response = client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request_error"


# ------------------------------------------------------------------ admin
def test_admin_config_endpoint(client):
    response = client.get("/v1/admin/config")
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == f"https://{UPSTREAM_DOMAIN}"
    assert "recaptcha_provider" in body
    # Hassas alanlar sızmamalı
    assert "cookie" not in json.dumps(body).lower() or body["cookie_configured"] is False


def test_admin_auth_reports_refresh_token_without_exposing_it():
    from urllib.parse import quote

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    session = json.dumps({"access_token": "A" * 30, "refresh_token": "secret-refresh"})
    second = json.dumps({"access_token": "B" * 30, "refresh_token": "second-refresh"})
    app = create_app(
        make_settings(
            upstream_cookie=f"sb-auth-token={quote(session)}",
            upstream_cookie_2=f"sb-auth-token={quote(second)}",
            upstream_auth_from_cookie=True,
            upstream_auth_from_cookie_2=True,
            upstream_account_2_name="ikinci",
        )
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = local.get("/v1/admin/auth")
        second_response = local.get("/v1/admin/auth?slot=2")
        missing_response = local.get("/v1/admin/auth?slot=4")
    assert response.status_code == 200
    refresh = response.json()["refresh_token"]
    assert refresh["found"] is True
    assert refresh["length"] == len("secret-refresh")
    assert "secret-refresh" not in response.text
    assert second_response.status_code == 200
    assert second_response.json()["slot"] == 2
    assert second_response.json()["account"] == "ikinci"
    assert second_response.json()["source"] == "cookie"
    assert second_response.json()["refresh_token"]["length"] == len("second-refresh")
    assert "second-refresh" not in second_response.text
    assert missing_response.status_code == 400


@respx.mock
def test_total_request_timeout_limits_the_whole_upstream_stream():
    import asyncio

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ai_stream("first")
            await asyncio.sleep(0.1)
            yield ai_stream("second")

    stream_route(respx).mock(return_value=httpx.Response(200, stream=SlowStream()))
    app = create_app(
        make_settings(total_request_timeout=0.01, retry_max_attempts=1, read_timeout=5.0)
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 504


def test_admin_breaker_reset(client):
    assert client.post("/v1/admin/breaker/reset").json()["status"] == "closed"


def test_admin_recaptcha_invalidate(client):
    assert client.post("/v1/admin/recaptcha/invalidate").json()["status"] == "invalidated"


# -------------------------------------------------------------- rate limit
@pytest.mark.parametrize("limit", [2])
def test_rate_limit_returns_429(limit):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(
        make_settings(rate_limit_enabled=True, rate_limit_rpm=limit, rate_limit_burst=limit)
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        statuses = [local.get("/v1/models").status_code for _ in range(limit + 3)]
    assert 429 in statuses


# ------------------------------------------------- upstream auth (401) yolu
@respx.mock
def test_upstream_401_reported_as_credentials_problem(client):
    stream_route(respx).mock(
        return_value=httpx.Response(401, content=b'{"error":"Unauthorized: invalid token"}')
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "upstream_unauthorized"
    assert "UPSTREAM_COOKIE" in error["message"]


@respx.mock
def test_empty_401_body_treated_as_auth_not_captcha(client):
    stream_route(respx).mock(return_value=httpx.Response(401, content=b""))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.json()["error"]["code"] == "upstream_unauthorized"


@respx.mock
def test_401_mentioning_captcha_still_routes_to_recaptcha(client):
    stream_route(respx).mock(
        return_value=httpx.Response(401, content=b'{"error":"recaptcha verification failed"}')
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.json()["error"]["code"] == "recaptcha_rejected"


@respx.mock
def test_authorization_header_sent_to_upstream():
    """Cookie'den ayıklanan token upstream isteğine Bearer olarak eklenmeli."""
    import base64
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    def seg(d):
        return base64.urlsafe_b64encode(_json.dumps(d).encode()).decode().rstrip("=")

    token = f"{seg({'alg': 'HS256'})}.{seg({'sub': 'u'})}.sig"

    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("ok"))
    )
    app = create_app(
        make_settings(
            upstream_cookie=f"access_token={token}; theme=dark",
            upstream_auth_from_cookie=True,
        )
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        assert local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code == 200

    sent = route.calls[0].request.headers
    assert sent["authorization"] == f"Bearer {token}"
    assert sent["cookie"] == f"access_token={token}; theme=dark"


# ---------------------------------------------- captcha alan adı (V2 / V3)
@respx.mock
def test_recaptcha_field_name_is_configurable():
    """Hedef `recaptchaV2Token` bekliyorsa gövde alan adı ona göre yazılmalı."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("ok"))
    )
    app = create_app(
        make_settings(
            recaptcha_provider="static",
            recaptcha_static_token="tok-abc",
            upstream_recaptcha_field="recaptchaV2Token",
        )
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        assert local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code == 200

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["recaptchaV2Token"] == "tok-abc"
    assert "recaptchaV3Token" not in sent


@respx.mock
def test_accept_language_is_configurable():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("ok"))
    )
    app = create_app(make_settings(upstream_accept_language="tr-TR,tr;q=0.9,en-US;q=0.8"))
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert route.calls[0].request.headers["accept-language"] == "tr-TR,tr;q=0.9,en-US;q=0.8"


@respx.mock
def test_no_authorization_header_by_default():
    """Tarayıcı cURL'ünde authorization yoksa biz de göndermemeliyiz."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("ok"))
    )
    app = create_app(make_settings(upstream_cookie="arena-auth-prod-v1.0=base64-abc"))
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        local.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert "authorization" not in route.calls[0].request.headers
