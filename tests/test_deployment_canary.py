"""Sürüm damgası kanaryası, 409 kurtarma ve keepalive hijyeni testleri."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.services.keepalive import (
    jittered_delay,
    pick_prompt,
    stream_smoke,
)
from app.upstream.client import UpstreamClient
from app.upstream.deployment import DeploymentIdResolver
from app.upstream.exceptions import UpstreamRefreshRequired
from app.upstream.headers import build_stream_headers
from tests.conftest import make_settings

STAMP_HEADER = "x-deployment-id"
OLD_STAMP = "dpl_old0000000000"
NEW_STAMP = "dpl_new1111111111"
REFRESH_BODY = '{"error":"refresh_required","reason":"forced_update"}'
PAYLOAD: dict[str, object] = {"id": "chat-1", "userMessage": "selam"}


def _resolver(**overrides: Any) -> DeploymentIdResolver:
    overrides.setdefault("upstream_extra_headers", f"{STAMP_HEADER}={OLD_STAMP}")
    return DeploymentIdResolver(make_settings(**overrides))


def _page(value: str | None, *, status: int = 200, body: str = "<html></html>") -> httpx.Response:
    headers = {STAMP_HEADER: value} if value else {}
    return httpx.Response(status, headers=headers, text=body)


# --------------------------------------------------------------- kanarya okuma
def test_detects_deployment_stamp_from_response_header():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _page(OLD_STAMP)

    async def run() -> None:
        resolver = _resolver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.value == OLD_STAMP
        assert reading.source == "header"

    asyncio.run(run())
    assert seen == ["https://upstream.test/"]


def test_detects_deployment_stamp_from_page_body_when_header_missing():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _page(None, body=f'<script>window.__dpl="{NEW_STAMP}"</script>')

    async def run() -> None:
        resolver = _resolver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.value == NEW_STAMP
        assert reading.source == "body"

    asyncio.run(run())


def test_cached_stamp_avoids_extra_page_reads_within_ttl():
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _page(OLD_STAMP)

    async def run() -> None:
        resolver = _resolver(deployment_id_ttl_seconds=3600.0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await resolver.refresh(client, "https://upstream.test")
            second = await resolver.refresh(client, "https://upstream.test")
            forced = await resolver.refresh(client, "https://upstream.test", force=True)
        assert first.source == "header"
        assert second.source == "cache"
        assert forced.source == "header"

    asyncio.run(run())
    assert calls["n"] == 2


def test_probe_failure_falls_back_to_configured_stamp_without_raising():
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async def run() -> None:
        resolver = _resolver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.value == OLD_STAMP
        assert reading.source == "configured"

    asyncio.run(run())


def test_probe_rejected_by_bot_protection_keeps_configured_stamp():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Just a moment...")

    async def run() -> None:
        resolver = _resolver()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.value == OLD_STAMP
        assert reading.source == "configured"

    asyncio.run(run())


def test_resolver_is_inactive_without_a_deployment_header():
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _page(NEW_STAMP)

    async def run() -> None:
        resolver = DeploymentIdResolver(make_settings(upstream_extra_headers=""))
        assert resolver.active is False
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reading = await resolver.refresh(client, "https://upstream.test")
        assert reading.value is None
        assert reading.source == "none"

    asyncio.run(run())
    assert calls["n"] == 0, "kanarya pasifken ağa çıkılmamalı"


# ------------------------------------------------------------------- başlıklar
def test_deployment_id_replaces_existing_header_only():
    settings = make_settings(
        upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP};x-arena-web-deployment={OLD_STAMP}"
    )
    headers = build_stream_headers(settings, "chat-1", deployment_id=NEW_STAMP)
    assert headers[STAMP_HEADER] == NEW_STAMP
    assert headers["x-arena-web-deployment"] == NEW_STAMP

    # Takip edilmeyen upstream'lere yeni başlık uydurulmaz.
    plain = build_stream_headers(make_settings(upstream_extra_headers=""), "chat-1",
                                 deployment_id=NEW_STAMP)
    assert STAMP_HEADER not in plain


# ------------------------------------------------------------- 409 kurtarma
def _install_client(client: UpstreamClient, handler: Any) -> httpx.AsyncClient:
    """Testin gerçek transport yerine MockTransport kullanmasını sağlar."""
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._client = transport_client
    return transport_client


def _stamp_of(request: httpx.Request) -> str:
    return request.headers.get(STAMP_HEADER, "")


def test_409_retries_once_with_fresh_stamp_and_streams():
    posts: list[str] = []
    gets = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            # İlk okuma eski damgayı görür (deploy henüz yayınlanmadı varsayımı),
            # 409'dan sonraki zorunlu okuma yenisini bulur.
            return _page(OLD_STAMP if gets["n"] == 1 else NEW_STAMP)
        posts.append(_stamp_of(request))
        if _stamp_of(request) == OLD_STAMP:
            return httpx.Response(409, text=REFRESH_BODY)
        return httpx.Response(200, content=b"0:ok")

    async def run() -> list[bytes]:
        settings = make_settings(
            upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP}",
            retry_max_attempts=1,
        )
        client = UpstreamClient(settings)
        transport_client = _install_client(client, handler)
        try:
            async with client.stream_completion("chat-1", PAYLOAD) as stream:
                return [chunk async for chunk in stream]
        finally:
            await transport_client.aclose()

    chunks = asyncio.run(run())
    assert chunks == [b"0:ok"]
    assert posts == [OLD_STAMP, NEW_STAMP], "409 sonrası taze damgayla tekrar denemeli"
    assert gets["n"] == 2, "ilk okuma + zorunlu tazeleme okuması"


def test_409_without_new_stamp_raises_refresh_required_and_refreshes_cookie():
    posts = 0
    page_gets: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            page_gets.append(request.url.path)
            return _page(OLD_STAMP)
        posts += 1
        return httpx.Response(409, text=REFRESH_BODY)

    async def run() -> None:
        settings = make_settings(
            upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP}",
            retry_max_attempts=1,
        )
        client = UpstreamClient(settings)
        transport_client = _install_client(client, handler)
        try:
            with pytest.raises(UpstreamRefreshRequired):
                async with client.stream_completion("chat-1", PAYLOAD):
                    pass  # pragma: no cover - akış hiç başlamamalı
        finally:
            await transport_client.aclose()

    asyncio.run(run())
    assert posts == 1, "damga değişmediyse boşuna tekrar denememeli"
    # Kanarya okuması + 409 sonrası oturum tazeleme GET'i.
    assert len(page_gets) >= 2


def test_409_auto_retry_can_be_disabled():
    posts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET":
            return _page(OLD_STAMP)
        posts += 1
        return httpx.Response(409, text=REFRESH_BODY)

    async def run() -> None:
        settings = make_settings(
            upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP}",
            retry_max_attempts=1,
            deployment_id_auto_retry=False,
        )
        client = UpstreamClient(settings)
        transport_client = _install_client(client, handler)
        try:
            with pytest.raises(UpstreamRefreshRequired):
                async with client.stream_completion("chat-1", PAYLOAD):
                    pass  # pragma: no cover
        finally:
            await transport_client.aclose()

    asyncio.run(run())
    assert posts == 1


def test_canary_probe_failure_does_not_break_the_stream():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ConnectTimeout("slow")
        assert _stamp_of(request) == OLD_STAMP
        return httpx.Response(200, content=b"0:ok")

    async def run() -> list[bytes]:
        settings = make_settings(upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP}")
        client = UpstreamClient(settings)
        transport_client = _install_client(client, handler)
        try:
            async with client.stream_completion("chat-1", PAYLOAD) as stream:
                return [chunk async for chunk in stream]
        finally:
            await transport_client.aclose()

    assert asyncio.run(run()) == [b"0:ok"]


# ------------------------------------------------------------------ keepalive
def test_jitter_stays_within_bounds_and_scales_down():
    for _ in range(50):
        # 3600 ± 450: tam saatte atılan öngörülebilir desen kırılır.
        delay = jittered_delay(3600.0, 450.0)
        assert 3150.0 <= delay <= 4050.0
    # Küçük aralıklarda sapma orantılı küçülür (testler yavaşlamasın).
    small = jittered_delay(0.01, 450.0)
    assert 0.001 <= small <= 0.02
    assert jittered_delay(0.0, 450.0) == 0.0


def test_pick_prompt_rotates_and_differs():
    prompts = [pick_prompt(i) for i in range(6)]
    assert len(set(prompts)) == 6
    assert pick_prompt(6) == prompts[0]
    assert all(prompt.strip() for prompt in prompts)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_stream_smoke_reports_success_when_sse_data_arrives():
    fake = _FakeClient(
        _FakeResponse(200, 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
    )

    async def run() -> Any:
        return await stream_smoke(fake, {"Authorization": "Bearer x"}, make_settings())

    result = asyncio.run(run())
    assert result.ok is True
    assert fake.requests[0]["json"]["stream"] is True


def test_stream_smoke_flags_empty_stream():
    fake = _FakeClient(_FakeResponse(200, ""))

    async def run() -> Any:
        return await stream_smoke(fake, {}, make_settings())

    result = asyncio.run(run())
    assert result.ok is False
    assert result.code == "stream_empty"


def test_stream_smoke_propagates_http_error_classification():
    fake = _FakeClient(
        _FakeResponse(503, '{"error": {"code": "upstream_refresh_required"}}')
    )

    async def run() -> Any:
        return await stream_smoke(fake, {}, make_settings())

    result = asyncio.run(run())
    assert result.ok is False
    assert result.code == "upstream_refresh_required"


# ------------------------------------------------------------------ gözlem
def test_cx_flag_detects_only_client_identity_references():
    assert UpstreamClient._cx_flag(400, '{"error":"missing cx"}') is True
    assert UpstreamClient._cx_flag(409, '{"cx":"abc"}') is True
    assert UpstreamClient._cx_flag(400, '{"note":"cx=1 required"}') is True
    # Sunucu hatası ve alakasız gövde işaretlenmez.
    assert UpstreamClient._cx_flag(500, '{"cx":"abc"}') is False
    assert UpstreamClient._cx_flag(400, '{"error":"quota"}') is False
    assert UpstreamClient._cx_flag(400, "") is False


def test_keepalive_runs_daily_stream_smoke_when_due(monkeypatch):
    import app.services.keepalive as keepalive_module

    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        text = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            calls.append(kwargs["json"])
            if len(calls) >= 2:
                raise asyncio.CancelledError
            return FakeResponse()

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", FakeClient)
    settings = make_settings(
        upstream_keepalive_interval_seconds=0.01,
        upstream_keepalive_initial_delay_seconds=0.0,
        upstream_keepalive_stream_smoke_interval_seconds=0.0,
    )

    async def run() -> None:
        # smoke_interval=0 => kapalı: iki probe da non-stream kalmalı.
        task = asyncio.create_task(keepalive_module.run_upstream_keepalive(settings))
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls and all(payload["stream"] is False for payload in calls)


def test_keepalive_stream_smoke_can_be_scheduled_instead_of_plain_probe(monkeypatch):
    import app.services.keepalive as keepalive_module

    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200
        text = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> FakeResponse:
            calls.append(kwargs["json"])
            if len(calls) >= 2:
                raise asyncio.CancelledError
            return FakeResponse()

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", FakeClient)
    settings = make_settings(
        upstream_keepalive_interval_seconds=0.01,
        upstream_keepalive_initial_delay_seconds=0.0,
        # İlk probe normal, ikincisi zamanı geldiği için akış kontrolü olur.
        upstream_keepalive_stream_smoke_interval_seconds=0.001,
    )

    async def run() -> None:
        task = asyncio.create_task(keepalive_module.run_upstream_keepalive(settings))
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls[0]["stream"] is False
    assert calls[1]["stream"] is True


def test_health_exposes_stamp_status_but_not_the_value():
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(upstream_extra_headers=f"{STAMP_HEADER}={OLD_STAMP}")
    app = create_app(settings)
    with TestClient(app) as test_client:
        body = test_client.get("/health").json()

    stamp = body["upstream"]["deployment_stamp"]
    assert stamp["managed"] is True
    assert stamp["source"] in {"configured", "none", "header", "cache"}
    assert OLD_STAMP not in str(body), "ham damga sağlık yanıtına sızmamalı"


def test_snapshot_reports_configured_source_before_first_read():
    resolver = _resolver()
    snapshot = resolver.snapshot("https://upstream.test")
    assert snapshot["managed"] is True
    assert snapshot["source"] == "configured"
    assert snapshot["age_seconds"] is None
