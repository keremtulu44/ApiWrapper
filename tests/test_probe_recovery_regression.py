"""Probe gerçek akış hatasını HTTP 200 + [DONE] ile karıştırmamalı."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.alerting import AlertPolicy, ProbeResult
from app.services.keepalive import classify_completion_response, stream_smoke
from tests.conftest import TEST_API_KEY, ai_stream, make_settings


class FakeClient:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def post(self, *_args, **_kwargs):
        return self.response


@respx.mock
def test_real_stream_401_does_not_send_false_recovery():
    respx.post(url__startswith="https://upstream.test/nextjs-api/stream/").mock(
        return_value=httpx.Response(401, json={"error": "Unauthorized: invalid token"})
    )
    settings = make_settings(upstream_cookie="test-session=opaque")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )
    assert response.status_code == 200
    assert '"code":"upstream_unauthorized"' in response.text
    assert "[DONE]" in response.text
    result = asyncio.run(stream_smoke(FakeClient(response), {}, settings))
    assert not result.ok
    assert result.code == "upstream_unauthorized"
    policy = AlertPolicy(repeat_seconds=3600)
    assert policy.evaluate(ProbeResult(ok=False, status_code=502, code=result.code), now=0)
    assert policy.evaluate(result, now=1) is None


@respx.mock
def test_real_successful_stream_still_reports_success():
    respx.post(url__startswith="https://upstream.test/nextjs-api/stream/").mock(
        return_value=httpx.Response(200, content=ai_stream("merhaba"))
    )
    settings = make_settings(upstream_cookie="test-session=opaque")
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}],
                  "stream": True},
        )
    result = asyncio.run(stream_smoke(FakeClient(response), {}, settings))
    assert result.ok


def test_unknown_error_code_is_not_echoed_or_logged(capsys):
    data = 'data: {"error":{"code":"SECRET-TOKEN-VALUE"}}\n\ndata: [DONE]\n\n'
    result = asyncio.run(
        stream_smoke(FakeClient(httpx.Response(200, text=data)), {}, make_settings())
    )
    assert not result.ok
    assert result.code == "upstream_failure"
    assert "SECRET-TOKEN-VALUE" not in capsys.readouterr().out


@pytest.mark.parametrize("data", [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
    'data: {bad-json}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    'data: {"error":{"message":"nope","code":"upstream_unauthorized"}}\n\n'
    'data: [DONE]\n\n',
])
def test_bad_or_failed_sse_never_reports_success(data):
    result = asyncio.run(
        stream_smoke(FakeClient(httpx.Response(200, text=data)), {}, make_settings())
    )
    assert not result.ok


def test_nonstream_probe_requires_nonempty_completion_and_recognizes_error():
    valid = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    assert classify_completion_response(httpx.Response(200, json=valid)).ok
    for body in ({"choices": []}, {"choices": [{"message": {"content": ""}}]},
                 {"error": {"code": "upstream_unauthorized"}}):
        result = classify_completion_response(httpx.Response(200, json=body))
        assert not result.ok
    assert classify_completion_response(
        httpx.Response(200, json={"error": {"code": "upstream_unauthorized"}})
    ).code == "upstream_unauthorized"
    assert not classify_completion_response(httpx.Response(200, text="not json")).ok


def test_recovery_message_reports_sample_not_permanent_recovery():
    policy = AlertPolicy()
    policy.evaluate(ProbeResult(ok=False, status_code=502, code="upstream_unauthorized"), now=0)
    text = policy.evaluate(ProbeResult(ok=True, status_code=200), now=1)
    assert text is not None and "son kontrol başarılı" in text
    assert "yeniden çalışıyor" not in text
