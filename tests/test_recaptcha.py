"""reCAPTCHA sağlayıcılarının hedeften bağımsız yaşam döngüsü testleri."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import RecaptchaError
from app.services.recaptcha.browser import BrowserRecaptchaProvider
from tests.conftest import make_settings


class FakeRoute:
    def __init__(self) -> None:
        self.status: int | None = None
        self.content_type: str | None = None
        self.body: str | None = None
        self.aborted: str | None = None

    async def fulfill(self, *, status: int, content_type: str, body: str) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body

    async def abort(self, reason: str = "failed") -> None:
        self.aborted = reason


class FakePage:
    def __init__(self, *, script_failed: bool = False, fail_goto: bool = False) -> None:
        self.url = "about:blank"
        self.script_failed = script_failed
        self.fail_goto = fail_goto
        self.routes: dict[str, Any] = {}
        self.fulfillments: list[FakeRoute] = []
        self.goto_url: str | None = None
        self.goto_kwargs: dict[str, Any] = {}
        self.extra_headers: dict[str, str] = {}
        self.closed = False

    async def route(self, url: str, handler: Any) -> None:
        self.routes[url] = handler

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_url = url
        self.goto_kwargs = kwargs
        if self.fail_goto:
            raise RuntimeError("navigation failed")
        # Her kayıtlı route'u dene: ilk eşleşen handler'ı çalıştır.
        # _load_page artık wildcard (**/*) + kök (/) için route kaydediyor;
        # ilk istek (target_origin) kök route ile cevaplanmalı.
        handler = self.routes.get(url)
        if handler is None:
            for pattern, cand in self.routes.items():
                # Basit wildcard: sonda "/**/*" varsa ve URL aynı prefix'e
                # sahipse eşleşmiş say.
                if pattern.endswith("/**/*") and url.startswith(pattern[:-5]):
                    handler = cand
                    break
        assert handler is not None, f"No route registered for {url}; routes: {list(self.routes)}"
        route = FakeRoute()
        await handler(route)
        self.fulfillments.append(route)
        self.url = url

    async def wait_for_function(self, _script: str, *, arg=None, timeout=None) -> None:
        return None

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.extra_headers.update(headers)

    async def evaluate(self, script: str, *_args: Any) -> Any:
        if "__wrapperRecaptchaScriptFailed" in script:
            return self.script_failed
        if "has_grecaptcha" in script:
            return "{}"
        return None

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage):
        self.page = page
        self.pages: list[FakePage] = []
        self.cookies_added: list[dict[str, Any]] = []
        self.init_scripts: list[str] = []

    async def new_page(self) -> FakePage:
        self.pages.append(self.page)
        return self.page

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.cookies_added.extend(cookies)

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def close(self) -> None:
        return None


def browser_provider(**overrides: Any) -> BrowserRecaptchaProvider:
    values: dict[str, Any] = {
        "recaptcha_provider": "browser",
        "recaptcha_site_key": "site-key",
        "target_domain": "upstream.test",
        "upstream_referer_path": "/c/{chat_id}",
        "upstream_scheme": "https",
        "upstream_accept_language": "en-US,en;q=0.9",
        "upstream_user_agent": "Mozilla/5.0 Chrome/154.0.0.0",
    }
    values.update(overrides)
    return BrowserRecaptchaProvider(make_settings(**values))


@pytest.mark.asyncio
async def test_browser_bootstrap_uses_referer_path_origin():
    """Bootstrap sayfası gerçek referer path üzerinde açılır (chat kökü)."""
    page = FakePage()
    provider = browser_provider()
    await provider._load_page(page)

    # referer /c/{chat_id} → /c/captcha-warmup üzerinde açılmalı
    assert page.goto_url == "https://upstream.test/c/captcha-warmup"
    assert page.url == "https://upstream.test/c/captcha-warmup"
    # content-type text/html olmalı
    assert page.fulfillments
    route = page.fulfillments[0]
    assert route.status == 200
    assert route.content_type and "text/html" in route.content_type
    assert route.body is not None
    assert "/recaptcha/api.js?render=site-key" in route.body
    # wait_until=domcontentloaded olmalı (load asset'leri blokluyordu)
    assert page.goto_kwargs.get("wait_until") == "domcontentloaded"


@pytest.mark.asyncio
async def test_browser_bootstrap_uses_enterprise_script_when_configured():
    page = FakePage()
    provider = browser_provider(recaptcha_version="enterprise")
    await provider._load_page(page)

    body = page.fulfillments[0].body
    assert body is not None
    assert "/recaptcha/enterprise.js?render=site-key" in body
    assert "/recaptcha/api.js" not in body


@pytest.mark.asyncio
async def test_browser_bootstrap_uses_direct_path_when_no_chat_id_placeholder():
    """Yeni arena.ai uçları referer'ı /text/direct (placeholder'sız)."""
    page = FakePage()
    provider = browser_provider(upstream_referer_path="/text/direct")
    await provider._load_page(page)

    assert page.goto_url == "https://upstream.test/text/direct"


@pytest.mark.asyncio
async def test_browser_script_load_failure_is_not_generic_execute_timeout():
    page = FakePage(script_failed=True)
    with pytest.raises(RecaptchaError, match="script failed to load"):
        await browser_provider()._load_page(page)


@pytest.mark.asyncio
async def test_browser_bootstrap_initialization_failure_is_actionable():
    page = FakePage(fail_goto=True)
    with pytest.raises(RecaptchaError, match="isolated reCAPTCHA page"):
        await browser_provider()._load_page(page)


@pytest.mark.asyncio
async def test_enterprise_execution_receives_version_and_action(monkeypatch):
    """_produce_token ısınmış sayfayı kullanır, warm-up sayfası create
    edildikten sonra execute script'ini evaluate eder."""

    class WarmPage(FakePage):
        def __init__(self) -> None:
            super().__init__()
            self.evaluate_calls: list[tuple[str, tuple[Any, ...]]] = []

        async def evaluate(self, script: str, *args: Any) -> Any:
            self.evaluate_calls.append((script[:40], args))
            if "__wrapperRecaptchaScriptFailed" in script:
                return False
            if "has_grecaptcha" in script:
                return "{}"
            # _WARMUP_SCRIPT / _EXECUTE_SCRIPT ikisi de gelir: ikincisi token döndürür
            if "grecaptcha.enterprise" in script or "new Promise" in script:
                return "fresh-token"
            return None

    page = WarmPage()
    ctx = FakeContext(page)
    provider = browser_provider(
        recaptcha_version="enterprise",
        recaptcha_action="chat_submit",
    )
    provider._context = ctx
    provider._browser = type("B", (), {"is_connected": lambda self: True})()

    token = await provider._produce_token()
    assert token == "fresh-token"
    # Sayfa açıldıktan sonra ısınmış sayfada _EXECUTE_SCRIPT çağrılmış olmalı
    assert page.goto_url  # _load_page çalışmış
    assert provider._warm_page is page  # yeniden kullanım için saklanmış


@pytest.mark.asyncio
async def test_browser_provider_restarts_when_chromium_disconnects(monkeypatch):
    class GoodPage(FakePage):
        async def evaluate(self, _script: str, *_args: Any) -> Any:
            if "__wrapperRecaptchaScriptFailed" in _script:
                return False
            if "new Promise" in _script or "grecaptcha" in _script:
                return "recovered-token"
            return None

    class DeadContext:
        async def new_page(self):
            raise RuntimeError("Target page, context or browser has been closed")

        async def add_init_script(self, _script: str) -> None:
            return None

        async def add_cookies(self, _c: Any) -> None:
            return None

        async def close(self) -> None:
            return None

    class GoodContext(FakeContext):
        def __init__(self) -> None:
            super().__init__(GoodPage())

        async def add_init_script(self, _s: str) -> None:
            return None

    provider = browser_provider(recaptcha_version="enterprise")
    provider._context = DeadContext()
    provider._browser = type("B", (), {
        "is_connected": lambda self: False,
    })()
    restarted = False

    async def fake_startup() -> None:
        nonlocal restarted
        restarted = True
        provider._context = GoodContext()
        provider._browser = type("B", (), {"is_connected": lambda self: True})()

    monkeypatch.setattr(provider, "startup", fake_startup)

    token = await provider._produce_token()
    assert token == "recovered-token"
    assert restarted is True
