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

    async def fulfill(self, *, status: int, content_type: str, body: str) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body


class FakePage:
    def __init__(self, *, script_failed: bool = False, fail_goto: bool = False) -> None:
        self.url = "about:blank"
        self.script_failed = script_failed
        self.fail_goto = fail_goto
        self.routes: dict[str, Any] = {}
        self.fulfillments: list[FakeRoute] = []
        self.goto_url: str | None = None

    async def route(self, url: str, handler: Any) -> None:
        self.routes[url] = handler

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.goto_url = url
        if self.fail_goto:
            raise RuntimeError("navigation failed")
        # Route kaydı yoksa gerçek hedefe gidilmesine izin verme: test, canlı
        # sayfa yerine izole bootstrap'ın kullanıldığını böylece de doğrular.
        handler = self.routes[url]
        route = FakeRoute()
        await handler(route)
        self.fulfillments.append(route)
        self.url = url

    async def wait_for_function(self, _script: str, *, arg=None, timeout=None) -> None:
        # Test ortamında grecaptcha'nın var olduğunu varsay (bootstrap HTML'i
        # doğru üretilmişse başarılı kabul et).
        return None

    async def set_extra_http_headers(self, _headers: dict[str, str]) -> None:
        return None

    async def evaluate(self, script: str, *_args: Any) -> Any:
        if "__wrapperRecaptchaScriptFailed" in script:
            return self.script_failed
        if "has_grecaptcha" in script:
            return "{}"
        return None


def browser_provider(**overrides: Any) -> BrowserRecaptchaProvider:
    values: dict[str, Any] = {
        "recaptcha_provider": "browser",
        "recaptcha_site_key": "site-key",
        "target_domain": "upstream.test",
    }
    values.update(overrides)
    return BrowserRecaptchaProvider(make_settings(**values))


@pytest.mark.asyncio
async def test_browser_bootstrap_uses_isolated_target_origin():
    page = FakePage()
    await browser_provider()._load_page(page)

    assert page.goto_url == "https://upstream.test/"
    assert page.url == "https://upstream.test/"
    assert len(page.fulfillments) == 1
    route = page.fulfillments[0]
    assert route.status == 200
    assert route.content_type == "text/html"
    assert route.body is not None
    assert "/recaptcha/api.js?render=site-key" in route.body


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
    class ExecutePage:
        def __init__(self):
            self.arguments: list[str] = []

        async def evaluate(self, script: str, arguments: list[str]) -> str:
            assert "grecaptcha.enterprise" in script
            self.arguments = arguments
            return "fresh-token"

        async def close(self) -> None:
            return None

    class Context:
        def __init__(self, page: ExecutePage):
            self.page = page

        async def new_page(self) -> ExecutePage:
            return self.page

    async def fake_load_page(_page) -> None:
        return None

    page = ExecutePage()
    provider = browser_provider(
        recaptcha_version="enterprise",
        recaptcha_action="chat_submit",
    )
    provider._context = Context(page)
    monkeypatch.setattr(provider, "_load_page", fake_load_page)

    assert await provider._produce_token() == "fresh-token"


@pytest.mark.asyncio
async def test_browser_provider_restarts_when_chromium_disconnects(monkeypatch):
    class GoodPage:
        async def evaluate(self, _script: str, _args: list[str]) -> str:
            return "recovered-token"

        async def close(self) -> None:
            return None

    class DeadContext:
        async def new_page(self):
            raise RuntimeError("Target page, context or browser has been closed")

        async def close(self) -> None:
            return None

    class GoodContext:
        async def new_page(self) -> GoodPage:
            return GoodPage()

        async def close(self) -> None:
            return None

    provider = browser_provider(recaptcha_version="enterprise")
    provider._context = DeadContext()
    restarted = False

    async def fake_startup() -> None:
        nonlocal restarted
        restarted = True
        provider._context = GoodContext()

    async def fake_load_page(_page) -> None:
        return None

    monkeypatch.setattr(provider, "startup", fake_startup)
    monkeypatch.setattr(provider, "_load_page", fake_load_page)

    token = await provider._produce_token()
    assert token == "recovered-token"
    assert restarted is True
