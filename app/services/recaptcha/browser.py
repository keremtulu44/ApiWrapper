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
  const timer = setTimeout(() => {
    const current = api();
    let reason = 'ready/execute did not complete';
    if (typeof grecaptcha === 'undefined') {
      reason = 'script unavailable';
    } else if (!current) {
      reason = version + ' API unavailable';
    } else if (!current.execute) {
      reason = 'execute API unavailable';
    }
    reject(new Error('grecaptcha timeout: ' + reason));
  }, 30000);
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

_BOOTSTRAP_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>recaptcha</title>
<script>
window.__wrapperRecaptchaScriptFailed = false;
window.__wrapperRecaptchaError = null;
window.addEventListener('error', function(e){
  window.__wrapperRecaptchaError = String(e.message || e.error || e);
});
</script>
<script
  src="https://www.google.com/recaptcha/__SCRIPT_NAME__?render=__SITE_KEY__"
  onerror="window.__wrapperRecaptchaScriptFailed = true">
</script>
</head>
<body>
<div id="root" style="min-height:80vh"></div>
</body></html>
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
            # Free Render 512 MB limiti için mümkün olan en hafif Chromium ayarı:
            # - single-process (renderer/subprocess ayırmaz; ~50-80 MB tasarruf)
            # - disable-dev-shm-usage (/dev/shm küçük olduğundan disk tabanlı
            #   paylaşıma düşer; aksi hâlde /dev/shm dolunca Chromium çöker)
            # - no-sandbox (container içinde zaten root/yek kullanıcı gerekli)
            # - memory-pressure, js-flags ile GC sıkılaştırılır.
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--single-process",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-dev-tools",
                    "--no-zygote",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-default-apps",
                    "--mute-audio",
                    "--hide-scrollbars",
                    "--disable-blink-features=AutomationControlled",
                    "--js-flags=--max-old-space-size=128",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=self.settings.upstream_user_agent,
                locale=self.settings.upstream_accept_language.split(",", 1)[0] or "en-US",
                viewport={"width": 1024, "height": 768},
            )
        except Exception as exc:
            # Playwright başladıktan sonra Chromium/context açılamazsa yarım
            # kalan kaynaklar birikmesin; sonraki istek temiz startup denesin.
            await self.shutdown()
            raise RecaptchaError(f"Browser startup failed: {exc}") from exc
        logger.info("recaptcha_browser_started")

    def _is_alive(self) -> bool:
        """Tarayıcı bağlamı ve varsa Chromium alt süreci hâlâ bağlı mı?"""
        if self._context is None:
            return False
        if self._browser is not None and hasattr(self._browser, "is_connected"):
            try:
                return bool(self._browser.is_connected())
            except Exception:
                return False
        return True

    async def browser_context(self) -> Any:
        """Yakalama (oturum tazeleme) için mevcut tarayıcı bağlamını döndürür.

        Aynı Chromium kullanılır: Render'da ikinci bir tarayıcı belleğe sığmaz.
        Chromium alt süreci çöktüyse otomatik yeniden başlatılır.
        """
        async with self._lock:
            if not self._is_alive():
                await self.shutdown()
                await self.startup()
            return self._context

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
            if not self._is_alive():
                await self.shutdown()
                await self.startup()
            assert self._context is not None

            try:
                page = await self._context.new_page()
            except Exception:
                # Render'da uzun süreli çalışmada Chromium renderer/browser
                # kapanmış olabilir: temiz yeniden başlatıp bir kez daha dene.
                await self.shutdown()
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
            except (TimeoutError, asyncio.TimeoutError) as exc:
                # Zaman aşımında sayfadaki teşhis bilgisini topla; hangi aşamada
                # takıldığını (script yüklenmedi mi, API hazır değil mi, execute
                # reddedildi mi) operatörün görmesi sorunu kökten çözer.
                diag = ""
                with contextlib.suppress(Exception):
                    diag = str(
                        await page.evaluate(
                            """() => JSON.stringify({
                              has_grecaptcha: typeof grecaptcha !== 'undefined',
                              has_enterprise: !!(typeof grecaptcha !== 'undefined'
                                && grecaptcha.enterprise),
                              script_failed: !!window.__wrapperRecaptchaScriptFailed,
                              page_error: window.__wrapperRecaptchaError,
                              readyState: document.readyState,
                              location: location.href,
                              scripts: Array.from(document.scripts)
                                .map(function(s){ return s.src; })
                                .filter(Boolean)
                            })"""
                        )
                    )[:500]
                raise RecaptchaError(
                    "Timed out while generating a reCAPTCHA token."
                    + (f" Diagnostics: {diag}" if diag else "")
                ) from exc
            except Exception as exc:
                raise RecaptchaError(f"Browser captcha generation failed: {exc}") from exc
            finally:
                with contextlib.suppress(Exception):  # pragma: no cover
                    await page.close()

            if not isinstance(token, str) or not token:
                raise RecaptchaError("Browser returned an empty reCAPTCHA token.")
            return token

    async def _load_page(self, page: Any) -> None:
        """CAPTCHA'yı hedef origin'de, canlı sayfadan izole bir belgeyle yükle.

        Gerçek hedef HTML'i Cloudflare/CSP veya kendi script'leriyle Google
        bootstrap'ını engelleyebilir. Route fulfillment URL origin'ini korur;
        böylece site-key domain kontrolü bozulmadan yalnız reCAPTCHA yüklenir.
        """
        script_name = (
            "enterprise.js" if self.settings.recaptcha_version == "enterprise" else "api.js"
        )
        target_url = f"{self.settings.base_url}/"
        html = (
            _BOOTSTRAP_HTML_TEMPLATE.replace("__SCRIPT_NAME__", script_name).replace(
                "__SITE_KEY__", self.settings.recaptcha_site_key
            )
        )

        async def serve_bootstrap(route: Any) -> None:
            await route.fulfill(status=200, content_type="text/html", body=html)

        # Canlı tarayıcıyla aynı Accept-Language başlığı bootstrap sayfasına
        # da ekleniyor; bazı reCAPTCHA bot-risk skorlamalarında buna bakılıyor.
        if self.settings.upstream_accept_language:
            await page.set_extra_http_headers({
                "Accept-Language": self.settings.upstream_accept_language,
            })

        try:
            await page.route(target_url, serve_bootstrap)
            await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=self.settings.recaptcha_browser_timeout * 1000,
            )
            # Script yüklenip grecaptcha hazır olana kadar kısa süre bekle:
            # wait_until="load" statik varlıklar yüzünden takılabiliyordu.
            await page.wait_for_function(
                """(version) => {
                  if (typeof grecaptcha === 'undefined') return false;
                  const api = version === 'enterprise' ? grecaptcha.enterprise : grecaptcha;
                  return !!(api && (api.execute || api.ready));
                }""",
                arg=self.settings.recaptcha_version,
                timeout=min(15000, int(self.settings.recaptcha_browser_timeout * 1000)),
            )
        except Exception as exc:
            diag = ""
            with contextlib.suppress(Exception):
                diag = str(
                    await page.evaluate(
                        """() => JSON.stringify({
                          has_grecaptcha: typeof grecaptcha !== 'undefined',
                          script_failed: !!window.__wrapperRecaptchaScriptFailed,
                          page_error: window.__wrapperRecaptchaError,
                          readyState: document.readyState
                        })"""
                    )
                )[:300]
            raise RecaptchaError(
                "Could not initialize the isolated reCAPTCHA page or load the Google script."
                + (f" Diagnostics: {diag}" if diag else "")
            ) from exc

        if await page.evaluate("() => window.__wrapperRecaptchaScriptFailed === true"):
            raise RecaptchaError(
                "Google reCAPTCHA script failed to load; "
                "check outbound access to www.google.com."
            )
