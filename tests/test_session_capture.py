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
    inspect_cookie,
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
        self.visited_urls: list[str] = []
        self._listeners: list[Any] = []

    def on(self, _event: str, callback: Any) -> None:
        self._listeners.append(callback)
        for request in self._requests:
            callback(request)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.visited_urls.append(url)

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

    async def add_cookies(self, _cookies: list[dict[str, Any]]) -> None:
        # Gerçek bağlamda yüklenen çerezler daha sonra sayfa tarafından
        # değiştirilebilir; bu sahte, önceden verilen son jar görünümünü korur.
        return None


def _jwt(*, expired: bool = False) -> str:
    """Testler için biçimsel olarak geçerli bir JWT (imza önemsiz)."""
    import base64
    import time

    def seg(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    exp = int(time.time()) + (-3600 if expired else 3600)
    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'sub': 'u1', 'exp': exp})}.sig"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_try_send_message_prefers_visible_submit_button():
    from app.upstream.session_capture import _try_send_message

    class Element:
        def __init__(self, selector: str, clicked: list[str]) -> None:
            self.selector = selector
            self.clicked = clicked
            self.first = self

        async def count(self) -> int:
            return int(self.selector in {"textarea:not([disabled])", 'button[type="submit"]'})

        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            return True

        async def click(self, **_kwargs: Any) -> None:
            self.clicked.append(self.selector)

        async def fill(self, _value: str, **_kwargs: Any) -> None:
            return None

    class Page:
        def __init__(self) -> None:
            self.clicked: list[str] = []
            self.keyboard = FakeKeyboard()

        def locator(self, selector: str) -> Element:
            return Element(selector, self.clicked)

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

    page = Page()
    assert _run(_try_send_message(page, "merhaba", 0)) is True
    assert page.clicked == ['textarea:not([disabled])', 'button[type="submit"]']
    assert page.keyboard.pressed == []


def test_try_send_message_searches_rich_text_editor_inside_frame():
    from app.upstream.session_capture import _try_send_message

    class Element:
        def __init__(self, frame: Frame, selector: str) -> None:
            self.frame = frame
            self.selector = selector
            self.first = self

        async def count(self) -> int:
            return int(self.selector == ".ProseMirror")

        async def is_visible(self) -> bool:
            return True

        async def is_enabled(self) -> bool:
            return True

        async def click(self, **_kwargs: Any) -> None:
            self.frame.clicked = self.selector

        async def fill(self, value: str, **_kwargs: Any) -> None:
            self.frame.filled = value

    class Frame:
        def __init__(self) -> None:
            self.clicked = ""
            self.filled = ""

        def locator(self, selector: str) -> Element:
            return Element(self, selector)

    class Page:
        def __init__(self, frame: Frame) -> None:
            self.frames = [frame]
            self.keyboard = FakeKeyboard()

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator(False)

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

    frame = Frame()
    page = Page(frame)
    assert _run(_try_send_message(page, "merhaba", 0)) is True
    assert frame.clicked == ".ProseMirror"
    assert frame.filled == "merhaba"
    assert page.keyboard.pressed == ["Enter"]


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
    assert page.visited_urls[0].startswith("https://upstream.test/c/")
    assert result.deployment_id == STAMP
    assert result.cx == "cx-value-123"
    assert result.stream_path == "/nextjs-api/stream/create-evaluation"
    assert result.logged_in is True
    assert result.referer_path == "/text/direct"
    assert "gerçek sohbet isteği yakalandı" in result.notes
    assert page.closed is True, "sayfa her durumda kapatılmalı"


def test_capture_navigates_to_configured_chat_page_not_site_root():
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
    )

    _run(
        capture_session(
            context,
            make_settings(upstream_referer_path="/text/direct"),
            wait_ms=0,
        )
    )

    assert page.visited_urls == ["https://upstream.test/text/direct"]


def test_capture_templates_dynamic_id_in_the_stream_path():
    chat_id = "01a199f6-5355-7fdb-a8d3-f651a68db7fa"
    page = FakePage(
        requests=[
            FakeRequest(
                f"https://upstream.test/api/stream/{chat_id}?mode=direct",
                {},
            )
        ],
        url="https://upstream.test/text/direct",
    )
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
    )

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.stream_path == "/api/stream/{chat_id}?mode=direct"


def test_capture_does_not_take_a_stream_path_from_another_host():
    page = FakePage(
        requests=[
            FakeRequest("https://analytics.test/stream/create-evaluation", {})
        ],
        url="https://upstream.test/text/direct",
    )
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
    )

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.stream_path is None
    assert "gerçek sohbet isteği yakalandı" not in result.notes


def test_capture_reports_missing_session_instead_of_failing():
    page = FakePage(url="https://upstream.test/login", has_textarea=False)
    context = FakeContext(page, [{"name": "__cf_bm", "value": "bm"}])
    settings = make_settings()

    result = _run(capture_session(context, settings, wait_ms=0))

    assert result.logged_in is False
    assert any("giriş" in note for note in result.notes)
    assert result.deployment_id is None


def test_capture_records_http_fallback_and_bot_challenge_without_html_leak():
    class Navigation:
        def __init__(self, status: int) -> None:
            self.status = status

    class ChallengePage(FakePage):
        def __init__(self) -> None:
            super().__init__(
                url="https://upstream.test/",
                html="<html>Verify you are human — private-page-marker</html>",
                has_textarea=False,
            )
            self.statuses = iter((503, 200))

        async def goto(self, url: str, **_kwargs: Any) -> Navigation:
            self.visited_urls.append(url)
            return Navigation(next(self.statuses))

    page = ChallengePage()
    result = _run(
        capture_session(FakeContext(page, []), make_settings(), wait_ms=0)
    )

    assert len(page.visited_urls) == 2
    assert result.navigation_status == 200
    assert result.bot_challenge_detected is True
    assert "bot koruması" in result.reason
    assert "private-page-marker" not in " ".join(result.notes)


def test_bot_challenge_detection_ignores_normal_cloudflare_script_names():
    from app.upstream.session_capture import _is_bot_challenge

    normal_page = """
    <html><head><script src="/cdn-cgi/challenge-platform/h/b/orchestrate.js">
    </script><script>const cf-chl = 'verify you are human';</script></head>
    <body><textarea placeholder="Mesaj"></textarea></body></html>
    """
    assert _is_bot_challenge(normal_page) is False
    assert _is_bot_challenge('<form id="challenge-form"></form>') is True
    assert _is_bot_challenge("<html>Verify you are human</html>") is True


def test_capture_retries_editor_once_before_reporting_challenge(
    monkeypatch: pytest.MonkeyPatch,
):
    import app.upstream.session_capture as capture_module

    calls = 0

    async def delayed_editor(_page: Any, _prompt: str, _wait_ms: int) -> bool:
        nonlocal calls
        calls += 1
        return calls == 2

    monkeypatch.setattr(capture_module, "_try_send_message", delayed_editor)
    page = FakePage(url="https://upstream.test/text/direct", has_textarea=False)
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
    )

    result = _run(capture_session(context, make_settings(), wait_ms=1))

    assert calls == 2
    assert result.triggered_request is True
    assert "ikinci bekleme denemesi" in " ".join(result.notes)


def test_capture_falls_back_to_html_stamp():
    page = FakePage(html=f'<script>window.__dpl="{STAMP}"</script>')
    context = FakeContext(page, [{"name": "sb-token", "value": "x"}])

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.deployment_id == STAMP
    assert "damga HTML'den okundu" in result.notes


def test_capture_falls_back_to_deployment_stamp_in_vcrr_cookie():
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(
        page,
        [
            {"name": "arena-auth-prod-v1.0", "value": "fresh-session"},
            {"name": "_vcrr", "value": f"vercel-cache;deployment={STAMP};other=1"},
        ],
    )

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.deployment_id == STAMP
    assert "damga tarayıcı çerezinden okundu" in result.notes


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
        stream_path="/nextjs-api/stream/create-evaluation",
        logged_in=True,
    )

    changed = apply_capture_to_settings(settings, result)

    assert settings.upstream_cookie == FRESH_COOKIE
    assert settings.upstream_referer_path == "/text/direct"
    assert settings.upstream_stream_path == "/nextjs-api/stream/create-evaluation"
    headers = settings.parsed_extra_headers()
    assert headers["x-deployment-id"] == STAMP
    assert headers["cx"] == "cx-value-123"
    assert headers["accept"] == "text/plain", "diğer başlıklar korunmalı"
    assert set(changed) >= {
        "upstream_cookie",
        "upstream_extra_headers",
        "upstream_referer_path",
        "upstream_stream_path",
    }


def test_apply_capture_keeps_existing_stamp_when_capture_has_none():
    settings = make_settings(
        upstream_cookie=OLD_COOKIE,
        upstream_extra_headers=f"x-deployment-id={STAMP}",
    )
    changed = apply_capture_to_settings(
        settings,
        CaptureResult(cookies=FRESH_COOKIE, logged_in=True, injected_cookies=2),
    )

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
    assert body["stream_path_found"] is True
    assert app.state.settings.upstream_stream_path == "/nextjs-api/stream/create-evaluation"
    # Teşhis alanları: "cookie var" demek yetmiyor, hangi çerezler geldiği önemli.
    assert "arena-auth-prod-v1.0" in body["cookie_names"]
    assert body["session_cookie_found"] is True
    assert body["login_redirect"] is False
    assert body["cookie_injection_error"] == ""
    assert "navigation_status" in body
    assert body["bot_challenge_detected"] is False
    assert "upstream_cookie" in body["fields"]
    assert "fresh-session" not in response.text, "çerez yanıta sızmamalı"
    assert settings.upstream_cookie.startswith("arena-auth-prod-v1.0=fresh-session")
    assert app.state.settings.parsed_extra_headers()["x-deployment-id"] == STAMP
    assert provider.invalidated is True


def test_admin_capture_reports_when_it_did_not_observe_the_stream_path():
    from fastapi.testclient import TestClient

    from app.main import create_app

    original_path = "/old/stream/path"
    settings = make_settings(
        upstream_cookie=OLD_COOKIE,
        upstream_stream_path=original_path,
    )
    app = create_app(settings)

    class FakeProvider:
        async def browser_context(self) -> FakeContext:
            page = FakePage(url="https://upstream.test/text/direct", requests=[])
            return FakeContext(
                page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
            )

        def invalidate(self) -> None:
            return None

    with TestClient(app) as client:
        app.state.recaptcha = FakeProvider()  # type: ignore[assignment]
        response = client.post(
            "/v1/admin/upstream/capture",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["logged_in"] is True
    assert body["stream_path_found"] is False
    assert "UPSTREAM_STREAM_PATH değişmedi" in body["next"]
    assert settings.upstream_stream_path == original_path


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
                "stream_path_found": True,
                "injected_cookies": 2,
                "cookie_injection_error": "",
                "navigation_status": 200,
                "bot_challenge_detected": False,
                "fields": ["upstream_cookie", "upstream_stream_path"],
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
    assert "Akış yolu: yakalandı" in sent[-1]["text"]
    assert "Tarayıcıya yüklenen çerez: 2" in sent[-1]["text"]
    assert "Sayfa HTTP: 200" in sent[-1]["text"]
    assert "cx: yok (isteğe bağlı)" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_telegram_yenile_shows_why_zero_cookie_capture_failed():
    from app.telegram.bot import TelegramBot, TelegramSettings

    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "not_captured",
                "logged_in": False,
                "cookie_found": True,
                "deployment_found": False,
                "cx_found": False,
                "stream_path_found": False,
                "triggered_request": False,
                "injected_cookies": 0,
                "cookie_injection_error": "domain:RuntimeError;url:ValueError",
                "navigation_status": 403,
                "bot_challenge_detected": True,
                "cookie_names": ["arena-auth-prod-v1.0"],
                "session_cookie_found": True,
                "fields": [],
                "next": "Taze çerezleri /cookie ile gönderin.",
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
        await bot.capture_upstream_session(42)

    message = sent[-1]["text"]
    assert "Oturum yakalanamadı" in message
    assert "Tarayıcıya yüklenen çerez: 0" in message
    assert "domain:RuntimeError;url:ValueError" in message
    assert "Sayfa HTTP: 403" in message
    assert "Bot doğrulaması: algılandı" in message


# ------------------------------------- yakalama hatası çalışan oturumu bozmamalı
def test_anonymous_capture_does_not_overwrite_the_working_cookie():
    # Canlı hata: oturum yakalanamadı ama anonim çerez seti mevcut oturumun
    # üzerine yazıldı. Artık yalnızca gerçek oturum uygulanır.
    settings = make_settings(upstream_cookie=OLD_COOKIE)
    result = CaptureResult(
        cookies="__cf_bm=anonim",
        deployment_id=STAMP,
        deployment_headers={"x-deployment-id": STAMP},
        logged_in=False,
        reason="sayfa giriş ekranına yönlendi (/login).",
    )

    changed = apply_capture_to_settings(settings, result)

    assert settings.upstream_cookie == OLD_COOKIE, "çalışan çerez korunmalı"
    assert "upstream_cookie" not in changed
    assert settings.parsed_extra_headers()["x-deployment-id"] == STAMP


def test_capture_injects_the_existing_cookie_before_navigation():
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(page, [{"name": "arena-auth-prod-v1.0", "value": "x"}])
    added: list[list[dict[str, Any]]] = []

    async def add_cookies(cookies: list[dict[str, Any]]) -> None:
        added.append(cookies)

    context.add_cookies = add_cookies  # type: ignore[attr-defined]
    settings = make_settings(upstream_cookie="arena-auth-prod-v1.0=abc; other=1")

    result = _run(capture_session(context, settings, wait_ms=0))

    assert result.injected_cookies == 2
    assert {c["name"] for c in added[0]} == {"arena-auth-prod-v1.0", "other"}
    assert all(c["domain"] == "upstream.test" for c in added[0])


def test_zero_injection_and_stale_auth_name_are_not_a_captured_session():
    """Canlı yanlış-pozitif: 0 yükleme + istek yok, ama auth adı jar'da kaldı."""
    page = FakePage(url="https://upstream.test/", has_textarea=False)
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.0", "value": "stale-session"}]
    )
    settings = make_settings(upstream_cookie="")

    result = _run(capture_session(context, settings, wait_ms=0))

    assert result.injected_cookies == 0
    assert result.stream_path is None
    assert result.triggered_request is False
    assert result.logged_in is False
    assert result.cookie_application_safe is False
    assert result.cookie_injection_error == "upstream_cookie_empty"

    settings.upstream_cookie = OLD_COOKIE
    changed = apply_capture_to_settings(settings, result)
    assert settings.upstream_cookie == OLD_COOKIE
    assert "upstream_cookie" not in changed


def test_cookie_injection_clears_shared_jar_and_retries_with_url():
    page = FakePage(url="https://upstream.test/text/direct")

    class FallbackContext(FakeContext):
        def __init__(self) -> None:
            super().__init__(
                page, [{"name": "arena-auth-prod-v1.0", "value": "fresh-session"}]
            )
            self.events: list[str] = []

        async def clear_cookies(self) -> None:
            self.events.append("clear")

        async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
            self.events.append("domain" if "domain" in cookies[0] else "url")
            if "domain" in cookies[0]:
                raise RuntimeError("domain rejected; secret-must-not-leak")

    context = FallbackContext()
    result = _run(
        capture_session(
            context,
            make_settings(upstream_cookie="arena-auth-prod-v1.0=fresh-session"),
            wait_ms=0,
        )
    )

    assert context.events == ["clear", "domain", "url"]
    assert result.injected_cookies == 1
    assert result.cookie_injection_error == ""
    assert result.logged_in is True
    assert "secret-must-not-leak" not in " ".join(result.notes)


def test_failed_injection_never_overwrites_cookie_or_leaks_browser_error():
    page = FakePage(url="https://upstream.test/", has_textarea=False)

    class RejectingContext(FakeContext):
        def __init__(self) -> None:
            super().__init__(
                page, [{"name": "arena-auth-prod-v1.0", "value": "stale-session"}]
            )
            self.cleared = False

        async def clear_cookies(self) -> None:
            self.cleared = True

        async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
            secret = "browser-error-secret-must-not-leak"
            if "domain" in cookies[0]:
                raise RuntimeError(secret)
            raise ValueError(secret)

    context = RejectingContext()
    settings = make_settings(upstream_cookie=OLD_COOKIE)
    result = _run(capture_session(context, settings, wait_ms=0))

    assert context.cleared is True
    assert result.injected_cookies == 0
    assert result.cookie_injection_error == "domain:RuntimeError;url:ValueError"
    assert result.logged_in is False
    assert result.cookie_application_safe is False
    assert "browser-error-secret" not in result.cookie_injection_error
    assert "browser-error-secret" not in " ".join(result.notes)

    changed = apply_capture_to_settings(settings, result)
    assert settings.upstream_cookie == OLD_COOKIE
    assert "upstream_cookie" not in changed


def test_capture_detects_a_working_session_from_the_access_token():
    jwt = _jwt()
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(page, [{"name": "arena-auth-prod-v1.1", "value": jwt}])

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.access_token_found is True
    assert result.access_token_expired is False
    assert result.logged_in is True


def test_capture_explains_an_expired_token():
    page = FakePage(url="https://upstream.test/text/direct")
    context = FakeContext(
        page, [{"name": "arena-auth-prod-v1.1", "value": _jwt(expired=True)}]
    )

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.logged_in is False
    assert result.access_token_expired is True
    assert "süresi dolmuş" in result.reason


# --------------------------------------------------------------- çerez ucu
def test_apply_cookie_to_settings_accepts_a_raw_cookie_header():
    from app.upstream.session_capture import apply_cookie_to_settings

    settings = make_settings(upstream_cookie=OLD_COOKIE)
    changed = apply_cookie_to_settings(
        settings, "cookie: arena-auth-prod-v1.1=" + _jwt() + "; __cf_bm=bm"
    )

    # Çerezdeki token `UPSTREAM_ACCESS_TOKEN` alanına da yazılır: aksi halde
    # header'daki eski token çerezden önce tercih edilir ve taze oturum yok sayılır.
    assert changed == ["upstream_cookie", "upstream_access_token"]
    assert settings.upstream_cookie.endswith("__cf_bm=bm")
    assert "arena-auth-prod-v1.1=" in settings.upstream_cookie


def test_apply_cookie_rejects_a_non_session_string():
    from app.upstream.session_capture import apply_cookie_to_settings

    settings = make_settings(upstream_cookie=OLD_COOKIE)
    with pytest.raises(ValueError):
        apply_cookie_to_settings(settings, "tracking=1; theme=dark")
    assert settings.upstream_cookie == OLD_COOKIE


def test_admin_cookie_endpoint_applies_and_hides_the_value():
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(upstream_cookie=OLD_COOKIE)
    app = create_app(settings)
    token = _jwt()
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/upstream/cookie",
            json={"cookie": f"arena-auth-prod-v1.1={token}"},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        assert response.status_code == 200, response.text
        assert token not in response.text
        assert app.state.settings.upstream_cookie == f"arena-auth-prod-v1.1={token}"
        assert app.state.accounts.accounts[0].cookie.startswith("arena-auth-prod-v1.1=")


@pytest.mark.asyncio
async def test_telegram_cookie_command_posts_the_header():
    from app.telegram.bot import TelegramBot, TelegramSettings

    sent: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {"path": request.url.path, "json": json.loads(request.content)}
        )
        if "dry_run" in request.url.query.decode():
            return httpx.Response(
                200,
                json={
                    "status": "inspected",
                    "safe_to_apply": True,
                    "cookie_names": ["arena-auth-prod-v1.1"],
                    "session_cookie_found": True,
                    "access_token_found": True,
                },
            )
        return httpx.Response(200, json={"status": "applied", "fields": ["upstream_cookie"]})

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
        token = _jwt()
        await bot.handle_message(
            {"chat": {"id": 42}, "text": f"/cookie arena-auth-prod-v1.1={token}"}
        )

    assert calls[0]["path"] == "/v1/admin/upstream/cookie"
    assert token not in sent[-1]["text"], "çerez yanıta yansımamalı"
    assert "Çerez uygulandı" in sent[-1]["text"]


# ------------------------------------- teşhis: hangi çerezler geldi, oturum var mı
def test_capture_lists_cookie_names_when_session_cookie_is_missing():
    """Canlı şikâyet: "Cookie: var" diyor ama oturum çerezi hiç gelmemiş.

    Kullanıcı yalnız birkaç çerez kopyaladığında mesajın ne olduğunu söylemesi
    gerekiyor; "access_token yok" demek eksik çerez setini fark ettirmiyordu.
    """
    page = FakePage(url="https://upstream.test/", has_textarea=False)
    context = FakeContext(
        page,
        [
            {"name": "__cf_bm", "value": "cf-value-secret"},
            {"name": "_dd_s", "value": "rum-value-secret"},
        ],
    )

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.logged_in is False
    assert result.cookie_names == ["__cf_bm", "_dd_s"]
    assert result.session_cookie_found is False
    assert "__cf_bm" in result.reason and "_dd_s" in result.reason
    assert "tüm" in result.reason.lower()
    # Yalnız adlar raporlanır; değerler hiçbir yere yazılmaz.
    assert "cf-value-secret" not in result.reason
    assert "rum-value-secret" not in " ".join(result.notes)


def test_capture_says_no_cookie_at_all_when_jar_is_empty():
    page = FakePage(url="https://upstream.test/", has_textarea=False)
    result = _run(capture_session(FakeContext(page, []), make_settings(), wait_ms=0))

    assert result.cookie_names == []
    assert result.logged_in is False
    assert "hiç çerez yüklenemedi" in result.reason


def test_capture_flags_login_redirect_separately():
    page = FakePage(url="https://upstream.test/login", has_textarea=False)
    context = FakeContext(page, [{"name": "arena-auth-prod-v1.0", "value": "dead"}])

    result = _run(capture_session(context, make_settings(), wait_ms=0))

    assert result.login_redirect is True
    assert result.session_cookie_found is True
    assert "giriş ekranına yönlendi" in result.reason


# ------------------------------- telefon: eklentinin JSON dışa aktarımı
def test_cookie_export_json_is_accepted():
    """Cookie-Editor "Export → JSON" çıktısı doğrudan uygulanabilmeli."""
    import json as jsonlib

    from app.upstream.session_capture import apply_cookie_to_settings, cookie_text_from_export

    exported = jsonlib.dumps(
        [
            {"name": "arena-auth-prod-v1.0", "value": "abc.def.ghi", "domain": "upstream.test"},
            {"name": "__cf_bm", "value": "bm", "domain": "upstream.test"},
        ]
    )
    assert cookie_text_from_export(exported) == "arena-auth-prod-v1.0=abc.def.ghi; __cf_bm=bm"

    settings = make_settings()
    changed = apply_cookie_to_settings(settings, exported)
    assert "upstream_cookie" in changed
    assert settings.upstream_cookie == "arena-auth-prod-v1.0=abc.def.ghi; __cf_bm=bm"


def test_broken_json_falls_back_to_header_parsing():
    from app.upstream.session_capture import cookie_text_from_export

    # JSON gibi başlayıp bozulmuş metin: başlık satırı gibi ele alınır.
    assert cookie_text_from_export("[bozuk") == "[bozuk"
    # Normal başlık satırı değişmeden geçer.
    assert cookie_text_from_export("Cookie: a=1; b=2") == "Cookie: a=1; b=2"


# ------------------------------- uygulamadan önce doğrulama (dry run)
def test_admin_cookie_dry_run_reports_names_without_applying():
    import base64
    import json as jsonlib
    import time as timelib

    from fastapi.testclient import TestClient

    from app.main import create_app

    def seg(payload: dict) -> str:
        return base64.urlsafe_b64encode(jsonlib.dumps(payload).encode()).decode().rstrip("=")

    token = f"{seg({'alg': 'HS256'})}.{seg({'exp': int(timelib.time()) + 600})}.sig"
    settings = make_settings(upstream_cookie=OLD_COOKIE)

    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = client.post(
            "/v1/admin/upstream/cookie?dry_run=true",
            json={"cookie": f'[{{"name":"arena-auth-prod-v1.0","value":"{token}"}}]'},
        )
        body = response.json()

    assert response.status_code == 200
    assert body["status"] == "inspected" and body["applied"] is False
    assert body["session_cookie_found"] is True
    assert body["access_token_found"] is True
    assert body["safe_to_apply"] is True
    assert token not in response.text, "token yanıta sızmamalı"
    # Uygulanmadı: çalışan çerez değişmedi.
    assert settings.upstream_cookie == OLD_COOKIE


def test_admin_cookie_dry_run_flags_an_anonymous_set():
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = make_settings(upstream_cookie=OLD_COOKIE)
    with TestClient(create_app(settings)) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        body = client.post(
            "/v1/admin/upstream/cookie?dry_run=true",
            json={"cookie": "__cf_bm=bm; _dd_s=rum"},
        ).json()

    assert body["session_cookie_found"] is False
    assert body["safe_to_apply"] is False
    assert "__cf_bm" in body["cookie_names"]
    assert "TÜM çerezleri" in body["next"]
    assert settings.upstream_cookie == OLD_COOKIE


def _chunked_cookie(chunks: int = 2, *, corrupt: bool = False) -> str:
    """Gerçek biçime benzeyen parçalı `base64-` oturum çerezi."""
    import base64 as _b64

    payload = {"access_token": _jwt(), "refresh_token": "test-refresh-token"}
    value = "base64-" + _b64.b64encode(json.dumps(payload).encode()).decode()
    if corrupt:  # kopyalama sırasında karakter kaybı
        value = value[:120] + value[121:]
    size = len(value) // chunks + 1
    parts = [
        f"arena-auth-prod-v1.{index}={value[index * size : (index + 1) * size]}"
        for index in range(chunks)
    ]
    return "; ".join(parts)


def test_inspect_reports_chunked_cookies_and_refresh_token():
    report = inspect_cookie(_chunked_cookie())
    assert report["access_token_found"] is True
    assert report["refresh_token_found"] is True
    assert report["chunked_families"] == {"arena-auth-prod-v1": 2}
    assert report["problems"] == []


def test_apply_cookie_reports_refresh_token_only_change():
    from app.upstream.auth import extract_access_token
    from app.upstream.session_capture import apply_cookie_to_settings

    cookie = _chunked_cookie()
    settings = make_settings(
        upstream_cookie=cookie,
        upstream_access_token=extract_access_token(cookie) or "",
        upstream_refresh_token="",
    )
    changed = apply_cookie_to_settings(settings, cookie)
    assert changed == ["upstream_refresh_token"]
    assert settings.upstream_refresh_token == "test-refresh-token"


def test_inspect_reports_missing_chunk():
    cookie = _chunked_cookie().split("; ")[0]  # yalnız .0 parçası
    report = inspect_cookie(cookie)
    assert report["access_token_found"] is False
    problems = " ".join(report["problems"])
    assert "parçalar eksik" in problems or "okunamadı" in problems


def test_inspect_reports_corrupted_base64():
    report = inspect_cookie(_chunked_cookie(corrupt=True))
    problems = " ".join(report["problems"])
    assert "base64" in problems or "okunamadı" in problems


def test_inspect_rejects_truncated_json_when_only_access_token_is_salvaged():
    # Access token baştaki kısımdan kurtarılabilir; refresh ayıklama ise tam
    # oturum JSON'unu ister. Bütünlük denetimi bu farkı gizlememeli.
    report = inspect_cookie(_chunked_cookie()[:-4])

    assert report["access_token_found"] is True
    assert report["refresh_token_found"] is False
    assert any("base64" in problem for problem in report["problems"])


def test_inspect_never_echoes_cookie_values():
    cookie = _chunked_cookie()
    report = inspect_cookie(cookie)
    serialized = json.dumps(report)
    assert "refresh_token" not in serialized.replace("refresh_token_found", "")
    assert "test-refresh-token" not in serialized
    assert _jwt() not in serialized


def test_apply_capture_applies_fresher_token_even_when_logged_in_is_false():
    expired = _jwt(expired=True)
    fresh = _jwt(expired=False)
    settings = make_settings(
        upstream_cookie=f"arena-auth-prod-v1.0={expired}",
        upstream_access_token=expired,
    )
    result = CaptureResult(
        cookies=f"arena-auth-prod-v1.0={fresh}",
        logged_in=False,
        login_redirect=False,
        refresh_api_key="discovered-anon-key",
    )

    changed = apply_capture_to_settings(settings, result)

    assert "upstream_cookie" in changed
    assert "upstream_access_token" in changed
    assert "upstream_refresh_api_key" in changed
    assert settings.upstream_access_token == fresh
    assert settings.upstream_refresh_api_key == "discovered-anon-key"

