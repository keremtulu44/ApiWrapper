"""Playwright ile headless tarayıcıda `grecaptcha.execute()` çağıran sağlayıcı."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.config import Settings
from app.core.errors import RecaptchaError
from app.core.logging import get_logger
from app.services.recaptcha.base import RecaptchaProvider

logger = get_logger(__name__)

#: Sayfa içinde çalıştırılacak betik: seçilen v3/Enterprise API hazır olunca
#: token üretir. Enterprise'da global `grecaptcha.execute` değil,
#: `grecaptcha.enterprise.execute` kullanılır.
_EXECUTE_SCRIPT = """
([siteKey, action, version]) => new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('grecaptcha timeout')), 30000);
  const api = () => {
    if (typeof grecaptcha === 'undefined') { return null; }
    return version === 'enterprise' ? grecaptcha.enterprise : grecaptcha;
  };
  const run = () => {
    const current = api();
    if (!current || !current.execute) {
      return setTimeout(run, 250);
    }
    const exec = () => current.execute(siteKey, { action })
      .then((token) => { clearTimeout(timer); resolve(token); })
      .catch((err) => { clearTimeout(timer); reject(err); });
    if (current.ready) { current.ready(exec); } else { exec(); }
  };
  run();
})
"""

_BOOTSTRAP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>recaptcha</title>
<script src="https://www.google.com/recaptcha/{script_name}?render={site_key}"></script>
</head><body><div id="root">loading</div></body></html>
"""


class BrowserRecaptchaProvider(RecaptchaProvider):
    """Playwright Chromium örneğini canlı tutar ve token üretir.

    Kurulum::

        pip install "apiwrapper[browser]"
        playwright install chromium
    """

    name = "browser"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        if self._context is not None:
            return
        if not self.settings.recaptcha_site_key:
            raise RecaptchaError(
                "RECAPTCHA_SITE_KEY must be set when using the 'browser' provider.",
                code="recaptcha_not_configured",
            )
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - opsiyonel bağımlılık
            raise RecaptchaError(
                "Playwright is not installed. Install with: "
                'pip install "apiwrapper[browser]" && playwright install chromium'
            ) from exc

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=self.settings.upstream_user_agent,
                locale=self.settings.upstream_accept_language.split(",", 1)[0] or "en-US",
                viewport={"width": 1440, "height": 900},
            )
        except Exception as exc:
            # Playwright başladıktan sonra Chromium/context açılamazsa yarım
            # kalan kaynaklar birikmesin; sonraki istek temiz startup denesin.
            await self.shutdown()
            raise RecaptchaError(f"Browser startup failed: {exc}") from exc
        logger.info("recaptcha_browser_started")

    async def shutdown(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):  # pragma: no cover
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await self._playwright.stop()
        self._context = self._browser = self._playwright = None
        logger.info("recaptcha_browser_stopped")

    async def _produce_token(self) -> str:
        async with self._lock:
            if self._context is None:
                await self.startup()
            assert self._context is not None

            page = await self._context.new_page()
            try:
                await self._load_page(page)
                token = await asyncio.wait_for(
                    page.evaluate(
                        _EXECUTE_SCRIPT,
                        [
                            self.settings.recaptcha_site_key,
                            self.settings.recaptcha_action,
                            self.settings.recaptcha_version,
                        ],
                    ),
                    timeout=self.settings.recaptcha_browser_timeout,
                )
            except TimeoutError as exc:
                raise RecaptchaError("Timed out while generating a reCAPTCHA token.") from exc
            except Exception as exc:
                raise RecaptchaError(f"Browser captcha generation failed: {exc}") from exc
            finally:
                with contextlib.suppress(Exception):  # pragma: no cover
                    await page.close()

            if not isinstance(token, str) or not token:
                raise RecaptchaError("Browser returned an empty reCAPTCHA token.")
            return token

    async def _load_page(self, page: Any) -> None:
        """reCAPTCHA'yı hedef origin'de yükle; about:blank token üretme."""
        try:
            await page.goto(
                self.settings.base_url,
                wait_until="domcontentloaded",
                timeout=self.settings.recaptcha_browser_timeout * 1000,
            )
        except Exception as exc:
            logger.debug("recaptcha_target_page_failed", error=str(exc))
            # page.goto bazı hatalardan sonra hedef URL'de kalabilir; bu durumda
            # set_content origin'i korur. about:blank'ta ise site-key domain
            # doğrulaması geçemeyeceği için bootstrap denemek yalnızca hatayı gizler.
            if not page.url.startswith(self.settings.base_url):
                raise RecaptchaError(
                    "Could not load the target page on its original origin for reCAPTCHA."
                ) from exc

        if self.settings.recaptcha_version == "enterprise":
            has_grecaptcha = await page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && "
                "!!grecaptcha.enterprise && typeof grecaptcha.enterprise.execute === 'function'"
            )
            script_name = "enterprise.js"
        else:
            has_grecaptcha = await page.evaluate(
                "() => typeof grecaptcha !== 'undefined' && "
                "typeof grecaptcha.execute === 'function'"
            )
            script_name = "api.js"
        if has_grecaptcha:
            return
        await page.set_content(
            _BOOTSTRAP_HTML.format(
                script_name=script_name,
                site_key=self.settings.recaptcha_site_key,
            ),
            wait_until="domcontentloaded",
        )
