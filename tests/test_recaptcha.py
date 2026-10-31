"""reCAPTCHA sağlayıcılarının hedeften bağımsız yaşam döngüsü testleri."""

from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import RecaptchaError
from app.services.recaptcha.browser import BrowserRecaptchaProvider
from tests.conftest import make_settings


class FakePage:
    def __init__(self, url: str, *, has_grecaptcha: bool = False, fail_goto: bool = False):
        self.url = url
        self.has_grecaptcha = has_grecaptcha
        self.fail_goto = fail_goto
        self.content: str | None = None

    async def goto(self, *_args, **_kwargs) -> None:
        if self.fail_goto:
            raise RuntimeError("navigation failed")

    async def evaluate(self, script: str, *_args) -> Any:
        if "typeof grecaptcha" in script:
            return self.has_grecaptcha
        return None

    async def set_content(self, content: str, **_kwargs) -> None:
        self.content = content


def browser_provider(**overrides: Any) -> BrowserRecaptchaProvider:
    values: dict[str, Any] = {
        "recaptcha_provider": "browser",
        "recaptcha_site_key": "site-key",
        "target_domain": "upstream.test",
    }
    values.update(overrides)
    return BrowserRecaptchaProvider(make_settings(**values))


@pytest.mark.asyncio
async def test_browser_uses_existing_target_page_script():
    page = FakePage("https://upstream.test/", has_grecaptcha=True)
    await browser_provider()._load_page(page)
    assert page.content is None


@pytest.mark.asyncio
async def test_browser_bootstrap_keeps_the_target_origin():
    page = FakePage("https://upstream.test/", fail_goto=True)
    await browser_provider()._load_page(page)
    assert page.content is not None
    assert "render=site-key" in page.content


@pytest.mark.asyncio
async def test_browser_bootstrap_uses_enterprise_script_when_configured():
    page = FakePage("https://upstream.test/", fail_goto=True)
    provider = browser_provider(recaptcha_version="enterprise")
    await provider._load_page(page)
    assert page.content is not None
    assert "/recaptcha/enterprise.js?render=site-key" in page.content
    assert "/recaptcha/api.js" not in page.content


@pytest.mark.asyncio
async def test_browser_does_not_generate_a_token_on_about_blank():
    page = FakePage("about:blank", fail_goto=True)
    with pytest.raises(RecaptchaError, match="original origin"):
        await browser_provider()._load_page(page)
    assert page.content is None


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
    assert page.arguments == ["site-key", "chat_submit", "enterprise"]
