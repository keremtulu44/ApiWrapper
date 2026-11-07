"""CAPTCHA tokenları POST denemeleri ve eşzamanlı istekler arasında paylaşılmaz."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.recaptcha.base import RecaptchaProvider
from app.upstream.client import UpstreamClient
from tests.conftest import TEST_API_KEY, ai_stream, make_settings
from tests.test_runtime_curl_swap import fresh_curl


class CountingProvider(RecaptchaProvider):
    name = "external"

    def __init__(self, settings):
        super().__init__(settings)
        self.count = 0

    async def _produce_token(self):
        self.count += 1
        n = self.count
        await asyncio.sleep(0.001)
        return f"token-{n}"


async def test_concurrent_callers_receive_distinct_tokens():
    provider = CountingProvider(make_settings())
    tokens = await asyncio.gather(*(provider.get_token() for _ in range(20)))
    assert len(set(tokens)) == 20
    assert provider.stats["generated"] == 20


@pytest.mark.parametrize("failure", ["server", "network", "stamp"])
async def test_each_retry_uses_fresh_token(failure):
    settings = make_settings(
        recaptcha_provider="external", upstream_recaptcha_field="recaptchaV2Token",
        retry_max_attempts=2, retry_base_delay=0, retry_max_delay=0,
        upstream_extra_headers="x-deployment-id=dpl_old0000000000" if failure == "stamp" else "",
    )
    provider = CountingProvider(settings)
    posts = []
    gets = 0

    async def handler(request):
        nonlocal gets
        if request.method == "GET":
            gets += 1
            return httpx.Response(200, headers={
                "x-deployment-id": "dpl_old0000000000" if gets == 1 else "dpl_new1111111111"
            })
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            if failure == "network":
                raise httpx.ConnectTimeout("timeout", request=request)
            if failure == "stamp":
                return httpx.Response(409, text='{"error":"refresh_required"}')
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=b"ok")

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        async with upstream.stream_completion("chat", {"id": "chat"},
                                              token_factory=provider.get_token) as chunks:
            assert b"".join([chunk async for chunk in chunks]) == b"ok"
    finally:
        await upstream.shutdown()
    assert [post["recaptchaV2Token"] for post in posts] == ["token-1", "token-2"]
    assert all("recaptchaV3Token" not in post for post in posts)


@respx.mock
def test_api_retries_with_fresh_token(monkeypatch):
    settings = make_settings(recaptcha_provider="external", retry_max_attempts=2,
                             retry_base_delay=0, retry_max_delay=0)
    provider = CountingProvider(settings)
    monkeypatch.setattr("app.main.create_recaptcha_provider", lambda _: provider)
    posts = []

    def handler(request):
        posts.append(json.loads(request.content))
        return httpx.Response(503, text="busy") if len(posts) == 1 else httpx.Response(
            200, content=ai_stream("ok")
        )

    respx.post(url__startswith="https://upstream.test/").mock(side_effect=handler)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200, response.text
    assert [p["recaptchaV3Token"] for p in posts] == ["token-1", "token-2"]


def test_runtime_curl_switches_live_provider_to_static():
    settings = make_settings(recaptcha_provider="noop")
    curl = fresh_curl().replace('"mode": "direct-battle"',
                                '"mode": "direct-battle", "recaptchaV3Token": "new-token"')
    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = client.post("/v1/admin/upstream/curl", json={"curl": curl})
        assert response.status_code == 200, response.text
        assert client.app.state.recaptcha.name == "static"
        assert client.app.state.completion_service.recaptcha is client.app.state.recaptcha


async def test_browser_invalidation_does_not_close_inflight_page(monkeypatch):
    from app.services.recaptcha.browser import BrowserRecaptchaProvider

    provider = BrowserRecaptchaProvider(make_settings(recaptcha_site_key="key"))
    started = asyncio.Event()
    release = asyncio.Event()

    class Page:
        def __init__(self):
            self.closed = False

        async def evaluate(self, script, *_args):
            if "new Promise" in script:
                started.set()
                await release.wait()
                assert not self.closed
                return "token"
            return None

        async def close(self):
            self.closed = True

    first, second = Page(), Page()
    provider._context = object()
    provider._warm_page = first

    async def ensure_page():
        if provider._warm_page is None:
            provider._warm_page = second
        return provider._warm_page, "bootstrap"

    monkeypatch.setattr(provider, "_ensure_warm_page", ensure_page)
    task = asyncio.create_task(provider.get_token())
    await asyncio.wait_for(started.wait(), timeout=2)
    provider.invalidate()
    assert not first.closed
    release.set()
    assert await task == "token"
    assert await provider.get_token() == "token"
    assert first.closed
    assert not second.closed


@pytest.mark.parametrize("error_type", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
    httpx.RemoteProtocolError,
])
async def test_ambiguous_transport_errors_do_not_repeat_post(error_type):
    from app.upstream.exceptions import UpstreamNetworkError, UpstreamTimeout

    settings = make_settings(deployment_id_refresh_enabled=False, retry_max_attempts=3,
                             retry_base_delay=0, retry_max_delay=0)
    posts = 0

    async def handler(request):
        nonlocal posts
        posts += 1
        raise error_type("ambiguous", request=request)

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises((UpstreamNetworkError, UpstreamTimeout)):
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass
        assert posts == 1
    finally:
        await upstream.shutdown()


@pytest.mark.parametrize("status,body", [
    (503, "busy"),
    (409, '{"error":"refresh_required"}'),
])
async def test_static_token_is_not_reused_after_http_response(status, body):
    from app.upstream.exceptions import UpstreamHTTPError

    settings = make_settings(
        deployment_id_refresh_enabled=False,
        upstream_extra_headers="x-deployment-id=dpl_old0000000000",
        retry_max_attempts=3,
        retry_base_delay=0,
        retry_max_delay=0,
    )
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, text=body)

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamHTTPError):
            async with upstream.stream_completion("chat", {"recaptchaV3Token": "single-use"}):
                pass
        assert len(requests) == 1
    finally:
        await upstream.shutdown()


async def test_upstream_error_body_is_not_logged(capsys):
    from app.upstream.exceptions import RecaptchaRejected

    settings = make_settings(deployment_id_refresh_enabled=False, retry_max_attempts=1)
    secret = "opaque-secret-not-a-jwt"

    async def handler(_request):
        return httpx.Response(403, json={"error": "recaptcha invalid", "private": secret})

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RecaptchaRejected):
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass
        assert secret not in capsys.readouterr().out
    finally:
        await upstream.shutdown()


@pytest.mark.parametrize("status,body", [
    (401, '{"error":"unauthorized"}'),
    (429, '{"error":"upstream limit reached"}'),
])
def test_api_does_not_retry_static_token_after_auth_or_quota(status, body, monkeypatch):
    from tests.test_token_refresh import session_cookie

    settings = make_settings(
        upstream_cookie=session_cookie(expires_in=3600),
        upstream_cookie_2="session=other-account",
        recaptcha_provider="static",
        recaptcha_static_token="static-single-use",
        deployment_id_refresh_enabled=False,
        retry_max_attempts=3,
        account_max_switches=1,
    )
    refreshed = []

    async def no_refresh(*_args, **_kwargs):
        refreshed.append(True)
        raise AssertionError("static token must not be resent")

    monkeypatch.setattr("app.services.completion_service.refresh_upstream_token", no_refresh)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith="https://upstream.test/").mock(
            return_value=httpx.Response(status, text=body)
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert response.status_code in (429, 502)
        assert route.call_count == 1
        assert not refreshed
