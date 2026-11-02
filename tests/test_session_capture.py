"""Sunucu tarayıcısıyla oturum yakalama (telefonda DevTools olmadan)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.upstream.session_capture import (
    CaptureResult,
    _referer_path,
    apply_capture_to_settings,
    capture_session,
    capture_with_provider,
    cookie_header_from,
)
from tests.conftest import TEST_API_KEY, make_settings

OLD_COOKIE = "arena-auth-prod-v1.0=old-session"
FRESH_COOKIE = "arena-auth-prod-v1.0=fresh-session; __cf_bm=bm-value"
STAMP = "dpl_DJU19pVoAz4jzELwyLNofxwG17pL"


class FakeRequest:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers


class FakeLocator:
    def __init__(self, exists: bool) -> None:
        self._exists = exists

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        return 1 if self._exists else 0

    async def click(self, **_kwargs: Any) -> None:
        return None

    async def fill(self, _value: str, **_kwargs: Any) -> None:
        return None


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    def __init__(
        self,
        *,
        requests: list[FakeRequest] | None = None,
        url: str = "",
        html: str = "<html></html>",
        has_textarea: bool = True,
    ) -> None:
        self._requests = requests or []
        self.url = url
        self._html = html
        self._has_textarea = has_textarea
        self.keyboard = FakeKeyboard()
        self.closed = False
        self._listeners: list[Any] = []

    def on(self, _event: str, callback: Any) -> None:
        self._listeners.append(callback)
        for request in self._requests:
            callback(request)

    async def goto(self, _url: str, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def content(self) -> str:
        return self._html

    def locator(self, _selector: str) -> FakeLocator:
        return FakeLocator(self._has_textarea)

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage, cookies: list[dict[str, str]]) -> None:
        self.page = page
        self._cookies = cookies

    async def new_page(self) -> FakePage:
        return self.page

    async def cookies(self, _url: str) -> list[dict[str, str]]:
        return self._cookies


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_capture_collects_cookies_stamp_and_referer():
    page = FakePage(
        requests=[
            FakeRequest(
                "https://upstream.test/nextjs-api/stream/create-evaluation",
                {
                    "x-deployment-id": STAMP,
                    "x-arena-web-deployment": STAMP,
                    "cx": "cx-value-123",
                    "referer": "https://upstream.test/text/direct",
                },
            )
        ],
        url="https://upstream.test/text/direct",
    )
    context = FakeContext(
        page,
        [
            {"name": "arena-auth-prod-v1.0", "value": "fresh-session"},
            {"name": "__cf_bm", "value": "bm-value"},
        ],
    )
    settings = make_settings()

    result = _run(capture_session(context, settings, wait_ms=0))

    assert result.cookies == FRESH_COOKIE
    assert result.deployment_id == STAMP
    assert result.cx == "cx-value-123"
    assert result.logged_in is True
    assert result.referer_path == "/text/direct"
    assert "gerçek sohbet isteği yakalandı" in result.notes
    assert page.closed is True, "sayfa her durumda kapatılmalı"


def test_capture_reports_missing_session_instead_of_failing():
    page = FakePage(url="https://upstream.test/login", has_textarea=False)
    context = FakeContext(page, [{"name": "__cf_bm", "value": "bm"}])
    settings = make_settings()

    result = _run(capture_session(context, settings, wait_ms=0))

    assert result.logged_in is False
    assert any("giriş" in note for note in result.notes)
    assert result.deployment_id is None


def test_capture_falls_back_to_html_stamp():
    page = FakePage(html=f'<script>window.__dpl="{STAMP}"</script>')
    context = FakeContext(page, [{"name": "sb-token", "value": "x"}])

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.deployment_id == STAMP
    assert "damga HTML'den okundu" in result.notes


def test_cookie_header_from_playwright_cookie_list():
    cookies = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    assert cookie_header_from(cookies) == "a=1; b=2"
    assert cookie_header_from([]) == ""


def test_referer_path_templates_chat_ids():
    settings = make_settings()
    assert _referer_path("https://upstream.test/text/direct", settings) == "/text/direct"
    assert (
        _referer_path(
            "https://upstream.test/c/01a199f6-5355-7fdb-a8d3-f651a68db7fa", settings
        )
        == "/c/{chat_id}"
    )
    assert _referer_path("", settings) is None


def test_apply_capture_updates_cookie_headers_and_referer():
    settings = make_settings(
        upstream_cookie=OLD_COOKIE,
        upstream_extra_headers="x-deployment-id=old-dpl;accept=text/plain",
    )
    result = CaptureResult(
        cookies=FRESH_COOKIE,
        deployment_id=STAMP,
        deployment_headers={"x-deployment-id": STAMP},
        cx="cx-value-123",
        referer_path="/text/direct",
        logged_in=True,
    )

    changed = apply_capture_to_settings(settings, result)

    assert settings.upstream_cookie == FRESH_COOKIE
    assert settings.upstream_referer_path == "/text/direct"
    headers = settings.parsed_extra_headers()
    assert headers["x-deployment-id"] == STAMP
    assert headers["cx"] == "cx-value-123"
    assert headers["accept"] == "text/plain", "diğer başlıklar korunmalı"
    assert set(changed) >= {"upstream_cookie", "upstream_extra_headers", "upstream_referer_path"}


def test_apply_capture_keeps_existing_stamp_when_capture_has_none():
    settings = make_settings(
        upstream_cookie=OLD_COOKIE,
        upstream_extra_headers=f"x-deployment-id={STAMP}",
    )
    changed = apply_capture_to_settings(settings, CaptureResult(cookies=FRESH_COOKIE))

    assert settings.upstream_cookie == FRESH_COOKIE
    assert settings.parsed_extra_headers()["x-deployment-id"] == STAMP
    assert "upstream_extra_headers" not in changed


def test_capture_with_provider_prefers_the_existing_browser_context():
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}])

    class FakeProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def browser_context(self) -> FakeContext:
            self.calls += 1
            return context

    provider = FakeProvider()
    result = _run(capture_with_provider(provider, make_settings()))

    assert provider.calls == 1
    assert result.cookies == "arena-auth-prod-v1.0=fresh-session"


# ------------------------------------------------------------------ admin ucu
def _fake_context() -> FakeContext:
    page = FakePage(
        requests=[
            FakeRequest(
                "https://upstream.test/nextjs-api/stream/create-evaluation",
                {"x-deployment-id": STAMP, "referer": "https://upstream.test/text/direct"},
            )
        ],
        url="https://upstream.test/text/direct",
    )
    return FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
    )


def test_admin_capture_endpoint_applies_session_without_secrets_in_response():
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(upstream_cookie=OLD_COOKIE)
    app = create_app(settings)

    class FakeProvider:
        def __init__(self) -> None:
            self.invalidated = False

        async def browser_context(self) -> FakeContext:
            return _fake_context()

        def invalidate(self) -> None:
            self.invalidated = True

    provider = FakeProvider()

    with TestClient(app) as client:
        # Startup gerçek sağlayıcıyı kurar; yakalama için sahtesini devreye al.
        app.state.recaptcha = provider  # type: ignore[assignment]
        response = client.post(
            "/v1/admin/upstream/capture",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["logged_in"] is True
    assert body["cookie_found"] is True
    assert body["deployment_found"] is True
    assert "upstream_cookie" in body["fields"]
    assert "fresh-session" not in response.text, "çerez yanıta sızmamalı"
    assert settings.upstream_cookie.startswith("arena-auth-prod-v1.0=fresh-session")
    assert app.state.settings.parsed_extra_headers()["x-deployment-id"] == STAMP
    assert provider.invalidated is True


def test_admin_capture_requires_api_key():
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.post("/v1/admin/upstream/capture")
    assert response.status_code == 401


# --------------------------------------------------------------- telegram /yenile
@pytest.mark.asyncio
async def test_telegram_yenile_reports_capture_result():
    from app.telegram.bot import TelegramBot, TelegramSettings

    sent: list[dict[str, Any]] = []
    paths: list[str] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "captured",
                "logged_in": True,
                "cookie_found": True,
                "deployment_found": True,
                "cx_found": False,
                "fields": ["upstream_cookie"],
                "next": "Bir mesaj atarak test edin.",
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(
            TelegramSettings(
                _env_file=None,
                telegram_bot_token="test-bot-token",
                wrapper_api_key="test-wrapper-key",
                wrapper_base_url="https://wrapper.test",
                telegram_allowed_chat_ids="42",
            ),
            telegram,
            wrapper,
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/yenile"})

    assert paths == ["/v1/admin/upstream/capture"]
    assert "yakalanıyor" in sent[0]["text"]
    assert "Oturum yakalandı" in sent[-1]["text"]
    assert "upstream_cookie" in sent[-1]["text"]
