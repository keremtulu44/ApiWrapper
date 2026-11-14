"""reCAPTCHA sağlayıcılarının hedeften bağımsız yaşam döngüsü testleri."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.recaptcha.browser import (
    _BOOTSTRAP_HTML_TEMPLATE,
    BrowserRecaptchaProvider,
)
from tests.conftest import make_settings


class _FakeRoute:
    def __init__(self) -> None:
        self.status: int | None = None
        self.content_type: str | None = None
        self.body: Any = None
        self.aborted: str | None = None

    async def fulfill(self, *, status: int, content_type: str, body: Any) -> None:
        self.status = status
        self.content_type = content_type
        self.body = body

    async def abort(self, reason: str = "failed") -> None:
        self.aborted = reason


class _FakePageForBootstrap:
    """_install_bootstrap için yalın sahte sayfa."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.fulfillments: list[_FakeRoute] = []

    async def route(self, url: str, handler: Any) -> None:
        self.routes[url] = handler

    async def unroute(self, url: str) -> None:
        self.routes.pop(url, None)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        if url in self.routes:
            r = _FakeRoute()
            await self.routes[url](r)
            self.fulfillments.append(r)
            return
        for pattern, cand in list(self.routes.items()):
            # Playwright glob: **/* tüm alt yolları eşleştirir.
            if pattern.endswith("/**/*"):
                prefix = pattern[: -len("/**/*")]
                if url.startswith(prefix + "/"):
                    r = _FakeRoute()
                    await cand(r)
                    self.fulfillments.append(r)
                    return
            else:
                # basit fnmatch
                import fnmatch
                if fnmatch.fnmatch(url, pattern):
                    r = _FakeRoute()
                    await cand(r)
                    self.fulfillments.append(r)
                    return


def browser_provider(**overrides: Any) -> BrowserRecaptchaProvider:
    values: dict[str, Any] = {
        "recaptcha_provider": "browser",
        "recaptcha_site_key": "site-key",
        "target_domain": "upstream.test",
        "upstream_referer_path": "/c/{chat_id}",
        "upstream_stream_path": "/nextjs-api/stream/create-evaluation",
        "upstream_scheme": "https",
        "upstream_accept_language": "en-US,en;q=0.9",
        "upstream_user_agent": "Mozilla/5.0 Chrome/154.0.0.0",
        "upstream_recaptcha_field": "recaptchaV3Token",
    }
    values.update(overrides)
    return BrowserRecaptchaProvider(make_settings(**values))


def _render_bootstrap(provider: BrowserRecaptchaProvider) -> str:
    """_install_bootstrap tarafından route handler'a verilen HTML'i döndürür."""
    script_name = (
        "enterprise.js"
        if provider.settings.recaptcha_version == "enterprise"
        else "api.js"
    )
    host = "upstream.test"
    lang = (provider.settings.upstream_accept_language or "en-US").split(",", 1)[0]
    return (
        _BOOTSTRAP_HTML_TEMPLATE.replace("__SCRIPT_NAME__", script_name)
        .replace("__SITE_KEY__", provider.settings.recaptcha_site_key)
        .replace("__ORIGIN_HOST__", host)
        .replace("__LANG__", lang)
    )


def test_browser_bootstrap_html_uses_v3_script():
    html = _render_bootstrap(browser_provider(recaptcha_version="v3"))
    assert "/recaptcha/api.js?render=site-key" in html
    assert "/recaptcha/enterprise.js" not in html


def test_browser_bootstrap_html_uses_enterprise_script_when_configured():
    html = _render_bootstrap(browser_provider(recaptcha_version="enterprise"))
    assert "/recaptcha/enterprise.js?render=site-key" in html
    assert "/recaptcha/api.js" not in html


def test_warm_target_url_resolves_chat_id_placeholder():
    p = browser_provider(upstream_referer_path="/c/{chat_id}")
    assert p._warm_target_url() == "https://upstream.test/c/captcha-warmup"


def test_warm_target_url_passthrough_for_direct_paths():
    p = browser_provider(upstream_referer_path="/text/direct")
    assert p._warm_target_url() == "https://upstream.test/text/direct"


def test_stream_url_drops_chat_id_for_new_create_endpoint():
    p = browser_provider(
        upstream_stream_path="/nextjs-api/stream/create-evaluation"
    )
    assert p._stream_url() == (
        "https://upstream.test/nextjs-api/stream/create-evaluation"
    )


def test_stream_url_templates_chat_id_segment():
    p = browser_provider(
        upstream_stream_path="/nextjs-api/stream/post-to-evaluation/{chat_id}"
    )
    assert "warmup-chat-id" in p._stream_url()


@pytest.mark.asyncio
async def test_install_bootstrap_registers_wildcard_and_root_routes():
    p = browser_provider()
    page = _FakePageForBootstrap()
    await p._install_bootstrap(page)
    patterns = sorted(page.routes.keys())
    assert any("/**/*" in ptn for ptn in patterns)
    assert any(ptn.endswith("upstream.test/") for ptn in patterns)
    # Wildcard'a giden bir istekte text/html cevap üretilsin:
    await page.goto("https://upstream.test/text/direct")
    assert page.fulfillments
    last = page.fulfillments[-1]
    assert last.status == 200
    assert "text/html" in (last.content_type or "")
    assert "recaptcha/api.js?render=site-key" in (last.body or "")


@pytest.mark.asyncio
async def test_produce_token_restarts_when_chromium_disconnects(monkeypatch):
    """İlk context ölüyorsa shutdown+startup yapıp tekrar dener."""

    class LivePage:
        closed = False

        async def goto(self, *_a, **_k):
            return None

        async def wait_for_function(self, *_a, **_k):
            return None

        async def evaluate(self, script, *_a):
            if "wrapperRecaptchaScriptFailed" in script:
                return False
            if "new Promise" in script:
                return "recovered-token"
            return None

        async def query_selector(self, *_a):
            return None

        async def route(self, *_a):
            return None

        async def unroute(self, *_a):
            return None

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class DeadContext:
        async def new_page(self):
            raise RuntimeError("Target page, context or browser has been closed")

        async def add_init_script(self, *_a):
            return None

        async def add_cookies(self, *_a):
            return None

        async def close(self):
            return None

    class GoodContext:
        def __init__(self, page):
            self.page = page
            self.init_scripts = []
            self.cookies_added = []

        async def new_page(self):
            return self.page

        async def add_init_script(self, s):
            self.init_scripts.append(s)

        async def add_cookies(self, c):
            self.cookies_added.extend(c)

        async def close(self):
            return None

    page = LivePage()
    provider = browser_provider(recaptcha_version="enterprise")
    provider._context = DeadContext()
    provider._browser = type("B", (), {"is_connected": lambda self: False})()
    restarted = False

    async def fake_startup():
        nonlocal restarted
        restarted = True
        provider._context = GoodContext(page)
        provider._browser = type("B", (), {"is_connected": lambda self: True})()

    monkeypatch.setattr(provider, "startup", fake_startup)
    # _ensure_warm_page'ı atla, doğrudan bootstrap modda sıcak sayfa ver:
    provider._warm_page = page
    provider._warm_mode = "bootstrap"  # type: ignore[attr-defined]

    token = await provider._produce_token()
    assert token == "recovered-token"
    assert restarted is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 500])
async def test_external_failure_does_not_expose_response_secrets(status):
    import httpx

    from app.core.errors import RecaptchaError
    from app.services.recaptcha.external import ExternalRecaptchaProvider

    secret = "sensitive-opaque-secret"
    provider = ExternalRecaptchaProvider(
        make_settings(recaptcha_external_url="https://solver.test/token")
    )

    async def handler(_request):
        return httpx.Response(status, json={"error": secret})

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RecaptchaError) as exc:
            await provider.get_token()
        assert secret not in str(exc.value)
        assert secret not in str(provider.stats)
    finally:
        await provider.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "captured", "clicked", "expected"),
    [
        ("live", "cached-secret", None, "live_click"),
        ("live", None, "clicked-secret", "live_click"),
        ("live", None, None, "live_execute"),
        ("bootstrap", None, None, "bootstrap_execute"),
    ],
)
async def test_browser_reports_token_source_without_token(
    monkeypatch, mode, captured, clicked, expected
):
    provider = browser_provider()
    provider._context = object()
    provider._captured_live_token = captured

    class Page:
        async def close(self):
            pass

        async def evaluate(self, script, *_args):
            return "execute-secret" if "new Promise" in script else None

    async def warm():
        return Page(), mode

    async def click(_page):
        return clicked

    async def no_sleep(_duration):
        pass

    monkeypatch.setattr(provider, "_ensure_warm_page", warm)
    monkeypatch.setattr(provider, "_extract_token_from_click", click)
    monkeypatch.setattr("app.services.recaptcha.browser.asyncio.sleep", no_sleep)
    token = await provider.get_token()
    assert token in {captured, clicked, "execute-secret"}
    assert provider.stats["last_token_source"] == expected
    assert token not in str(provider.stats)
