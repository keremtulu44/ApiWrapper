"""Playwright ile headless tarayıcıda reCAPTCHA token'ı üreten sağlayıcı.

Strateji (basit→gelişmiş):
1. **Canlı sayfada Send'e tıkla (ana yol):** Referer URL'si (örn. /text/direct)
   rota hilesi olmadan açılır, Cloudflare çerezleri ve oturum çerezleriyle
   normal şekilde yüklenir. Sayfanın kendi JS'i kendi reCAPTCHA
   bootstrap'ını yapar (doğru site-key, doğru action, doğru badge).
   "Send" butonuna scriptsel tıklama yapılır; tarayıcının ürettiği ağ
   isteği route interception ile yakalanır ve gövdedeki
   ``recaptchaV3Token`` (ya da ayarlanmış alan adı) okunur. Bu, elle
   execute() çağırmaktan çok daha güvenilirdir çünkü aksiyon adı,
   zamanlama, skor ve diğer parametreler Arena'nın kendi kodu tarafından
   belirlenir.
2. **Canlı sayfada doğrudan execute() (ikinci seçenek):** Tıklama akışı
   çalışmazsa veya form tam olarak o sayfada bulunmazsa, grecaptcha
   hazır olduktan sonra ayarlardaki site-key/action ile doğrudan
   execute() çağrılır.
3. **Bootstrap fallback (son çare):** Canlı sayfa Cloudflare/403 ile
   bloklanırsa veya hiç grecaptcha yüklenmezse, sentetik HTML + kendi
   reCAPTCHA script'imiz devreye girer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.errors import RecaptchaError
from app.core.logging import get_logger
from app.services.recaptcha.base import RecaptchaProvider

logger = get_logger(__name__)

#: Sayfa içinde çalıştırılacak betik: seçilen v3/Enterprise API hazır olunca
#: bizim belirlediğimiz site-key/action ile token üretir (ikinci seçenek).
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

#: Navigatorde otomasyon izlerini silen init script.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => false});
Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR','tr','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = { runtime: {} };
try {
  const origGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p){
    if (p === 37445) return 'Google Inc. (Intel)';
    if (p === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630)';
    return origGetParameter.call(this, p);
  };
} catch(e) {}
"""

#: Bootstrap fallback HTML: sentetik bir chat sayfası, kendi reCAPTCHA
#: script'imiz yüklenir. Son çare olarak kullanılır.
_BOOTSTRAP_HTML_TEMPLATE = """<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chat - __ORIGIN_HOST__</title>
<link rel="icon" href="data:,">
<style>
  html,body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,
  "Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:#fff;color:#0d0d0d}
  #root{min-height:100vh;padding:24px}
  .wrap{max-width:768px;margin:0 auto}
  textarea{width:100%;min-height:120px;border:1px solid #ddd;border-radius:8px;
  padding:12px;font:inherit}
  button{margin-top:8px;padding:10px 18px;border-radius:8px;border:0;
  background:#10a37f;color:#fff;font:inherit;cursor:pointer}
</style>
<script>
Object.defineProperty(navigator,'webdriver',{get:()=>false});
window.__wrapperRecaptchaScriptFailed=false;
window.__wrapperRecaptchaError=null;
window.addEventListener('error',function(e){
  window.__wrapperRecaptchaError=String(e.message||e.error||e);
});
</script>
<script
  src="https://www.google.com/recaptcha/__SCRIPT_NAME__?render=__SITE_KEY__"
  onerror="window.__wrapperRecaptchaScriptFailed=true">
</script>
</head>
<body>
<div id="root"><div class="wrap">
<textarea id="prompt" placeholder="Message…"></textarea>
<button id="send">Send</button>
</div></div>
</body></html>
"""

#: Isınma davranışı: ufak fare + scroll.
_WARMUP_SCRIPT = """
() => {
  try {
    const w = window.innerWidth, h = window.innerHeight;
    const evt = (type, x, y) => document.dispatchEvent(new MouseEvent(type, {
      clientX:x, clientY:y, bubbles:true, cancelable:true, view:window
    }));
    const pts = [
      [Math.floor(w*0.15), Math.floor(h*0.2)],
      [Math.floor(w*0.45+Math.random()*20), Math.floor(h*0.5+Math.random()*15)],
      [Math.floor(w*0.5+Math.random()*30), Math.floor(h*0.65+Math.random()*10)],
    ];
    let i=0;
    const step=()=>{ if(i>=pts.length) return; const[x,y]=pts[i++];
      evt('mousemove',x,y); setTimeout(step, 120+Math.random()*100); };
    evt('mousemove',10,10); setTimeout(step,50);
    window.scrollBy({top:20,behavior:'instant'});
    setTimeout(()=>window.scrollBy({top:-10,behavior:'instant'}),300);
  } catch(e){}
}
"""


class BrowserRecaptchaProvider(RecaptchaProvider):
    """Playwright Chromium örneğini canlı tutar ve token üretir."""

    name = "browser"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._warm_page: Any = None
        self._captured_live_token: str | None = None
        self._lock = asyncio.Lock()
        self._reset_warm_page = False
        self.stats["last_token_source"] = None

    def _record_token_source(self, source: str) -> None:
        # Yalnız yol adı tutulur; token veya sayfa içeriği loglanmaz.
        self.stats["last_token_source"] = source
        logger.info("recaptcha_browser_token_source", source=source)

    async def startup(self) -> None:
        if self._context is not None:
            return
        if not self.settings.recaptcha_site_key:
            raise RecaptchaError(
                "RECAPTCHA_SITE_KEY must be set when using the 'browser' provider.",
                code="recaptcha_not_configured",
            )
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
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
            locale = (
                self.settings.upstream_accept_language.split(",", 1)[0].strip()
                or "en-US"
            )
            self._context = await self._browser.new_context(
                user_agent=self.settings.upstream_user_agent,
                locale=locale,
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": (
                        self.settings.upstream_accept_language or "en-US,en;q=0.9"
                    ),
                },
            )
            await self._context.add_init_script(_STEALTH_INIT)
        except Exception as exc:
            await self.shutdown()
            raise RecaptchaError(f"Browser startup failed: {exc}") from exc
        logger.info("recaptcha_browser_started")

    def _is_alive(self) -> bool:
        if self._context is None:
            return False
        if self._browser is not None and hasattr(self._browser, "is_connected"):
            try:
                return bool(self._browser.is_connected())
            except Exception:
                return False
        return True

    async def browser_context(self) -> Any:
        async with self._lock:
            if not self._is_alive():
                await self.shutdown()
                await self.startup()
            return self._context

    def invalidate(self) -> None:
        """Sonraki üretimde sıcak sayfayı kilit altında yenile.

        Eşzamanlı bir üretimin kullandığı sayfayı burada kapatmak onu bozardı.
        """
        super().invalidate()
        self._reset_warm_page = True

    async def shutdown(self) -> None:
        for closer in (self._warm_page, self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._warm_page = None
        self._captured_live_token = None
        self._context = self._browser = self._playwright = None
        logger.info("recaptcha_browser_stopped")

    def _warm_target_url(self) -> str:
        referer_path = self.settings.upstream_referer_path or "/"
        placeholder = "captcha-warmup"
        if "{chat_id}" in referer_path:
            referer_path = referer_path.replace("{chat_id}", placeholder)
        if not referer_path.startswith("/"):
            referer_path = "/" + referer_path
        return self.settings.base_url.rstrip("/") + referer_path

    def _stream_url(self) -> str:
        """Gerçek akış isteğinin URL'si (ağ yakalama için)."""
        # upstream_stream_path içinde {chat_id} varsa sıcak bir chat id ile;
        # yoksa olduğu gibi kullan (create-evaluation gibi).
        path = self.settings.upstream_stream_path
        if "{chat_id}" in path:
            # Rastgele bir chat id; Arena kimliği gövdede beklemiyorsa
            # (örn. create-evaluation) bunu kullanmayacağız, sadece URL
            # route eşleşmesi için.
            path = path.replace("{chat_id}", "warmup-chat-id")
        return self.settings.base_url.rstrip("/") + (
            path if path.startswith("/") else "/" + path
        )

    async def _set_cookies_from_settings(self) -> None:
        assert self._context is not None
        parsed = urlparse(self.settings.base_url)
        if not parsed.hostname:
            return
        cookie_header = self.settings.upstream_cookie or ""
        if not cookie_header:
            return
        to_add: list[dict[str, Any]] = []
        for piece in cookie_header.split(";"):
            piece = piece.strip()
            if "=" not in piece:
                continue
            name, _, val = piece.partition("=")
            name = name.strip()
            val = val.strip()
            if not name or not val:
                continue
            # Güvenlik açısından hassas oturum çerezlerini enjekte etme:
            # onlar yalnızca upstream isteklerinde kullanılır, reCAPTCHA
            # bootstrap için gereksiz ve tarayıcının cookie kotasını
            # şişirir.
            if name.lower().startswith("arena-auth"):
                continue
            to_add.append({
                "name": name,
                "value": val,
                "domain": parsed.hostname,
                "path": "/",
                "httpOnly": False,
                "secure": parsed.scheme == "https",
                "sameSite": "Lax",
            })
        if to_add:
            with contextlib.suppress(Exception):
                await self._context.add_cookies(to_add)  # type: ignore[arg-type]

    async def _wait_for_grecaptcha(self, page: Any, timeout_ms: int) -> bool:
        try:
            await page.wait_for_function(
                """(version) => {
                  if (typeof grecaptcha === 'undefined') return false;
                  const api = version === 'enterprise'
                    ? grecaptcha.enterprise : grecaptcha;
                  return !!(api && (api.execute || api.ready));
                }""",
                arg=self.settings.recaptcha_version,
                timeout=timeout_ms,
            )
            return True
        except Exception:
            return False

    async def _extract_token_from_click(self, page: Any) -> str | None:
        """Canlı sayfada 'Send' tıklamasından doğan akış isteğini yakala.

        Sayfanın kendi JS'i form submit akışında reCAPTCHA üretir ve
        stream endpoint'ine POST atar; biz o POST'u route ile yakalayıp
        gövdeden captcha alanını okuruz ve isteği iptal ederiz (gerçek
        mesaj gönderilmez).
        """
        captured: dict[str, str] = {}
        field = (self.settings.upstream_recaptcha_field or "recaptchaV3Token").strip()
        stream_url_prefix = self._stream_url().split("?")[0]

        async def intercept(route: Any) -> None:
            try:
                req = route.request
                post_data = req.post_data or ""
                if post_data:
                    try:
                        body = json.loads(post_data)
                        tok = body.get(field)
                        if isinstance(tok, str) and tok.strip():
                            captured["token"] = tok.strip()
                    except Exception:
                        pass
            except Exception:
                pass
            # İsteği her zaman abort et: gerçek bir mesaj göndermesin.
            with contextlib.suppress(Exception):
                await route.abort("aborted")

        await page.route(stream_url_prefix + "**", intercept)
        try:
            # Send butonunu bul ve tıkla. Birden fazla seçiciyi dene;
            # Arenanın gerçek butonunu olabildiğince çok biçimde yakala.
            clicked = False
            for selector in (
                'button[type="submit"]',
                'button[aria-label*="end" i]',
                'button[aria-label*="send" i]',
                'button[data-testid*="send" i]',
                '#send',
                'form button:last-of-type',
                'textarea + button',
            ):
                with contextlib.suppress(Exception):
                    handle = await page.query_selector(selector)
                    if handle:
                        await handle.click(timeout=1500)
                        clicked = True
                        break
            if not clicked:
                # Son çare: formu submit et
                with contextlib.suppress(Exception):
                    await page.evaluate(
                        """() => {
                          const f = document.querySelector('form');
                          if (f) f.requestSubmit ? f.requestSubmit() : f.submit();
                        }"""
                    )
                    clicked = True
            # 10 sn içinde token yakalanmazsa None dön.
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                if captured.get("token"):
                    return captured["token"]
                await asyncio.sleep(0.2)
            return captured.get("token")
        finally:
            with contextlib.suppress(Exception):
                await page.unroute(stream_url_prefix + "**")

    async def _install_bootstrap(self, page: Any) -> tuple[str, str]:
        script_name = (
            "enterprise.js"
            if self.settings.recaptcha_version == "enterprise"
            else "api.js"
        )
        parsed = urlparse(self.settings.base_url)
        origin_host = parsed.hostname or (parsed.netloc or "localhost")
        html = (
            _BOOTSTRAP_HTML_TEMPLATE.replace("__SCRIPT_NAME__", script_name)
            .replace("__SITE_KEY__", self.settings.recaptcha_site_key)
            .replace("__ORIGIN_HOST__", origin_host)
            .replace(
                "__LANG__",
                (self.settings.upstream_accept_language or "en-US").split(",", 1)[0],
            )
        )

        async def serve(route: Any) -> None:
            try:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await route.abort("failed")

        route_pattern = f"{self.settings.base_url.rstrip('/')}/**/*"
        root_pattern = f"{self.settings.base_url}/"
        await page.route(route_pattern, serve)
        await page.route(root_pattern, serve)
        return root_pattern, html

    async def _ensure_warm_page(self) -> tuple[Any, str]:
        """(page, mode) çifti döndürür. mode 'live'|'bootstrap'."""
        assert self._context is not None
        page = self._warm_page
        needs_boot = False
        if page is None:
            needs_boot = True
        else:
            with contextlib.suppress(Exception):
                closed = bool(getattr(page, "is_closed", lambda: False)())
                if closed:
                    needs_boot = True
                    self._warm_page = None
        if needs_boot:
            await self._set_cookies_from_settings()
            page = await self._context.new_page()
            # Hazırlık başarısız olsa bile shutdown sayfayı kapatabilsin.
            self._warm_page = page

            target_url = self._warm_target_url()
            mode = "bootstrap"

            # --- Aşama 1: canlı sayfa ---
            live_ok = False
            try:
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=15000,
                    referer=self.settings.base_url + "/",
                )
                live_ok = await self._wait_for_grecaptcha(page, timeout_ms=8000)
            except Exception as exc:
                logger.info(
                    "recaptcha_browser_live_page_failed",
                    url=target_url,
                    error=str(exc)[:200],
                )

            if live_ok:
                # Hafif ısınma (canlı sayfa üstünde)
                with contextlib.suppress(Exception):
                    await page.evaluate(_WARMUP_SCRIPT)
                await asyncio.sleep(1.5 + random.random() * 0.5)
                # Tıklama yakalamasını dene; token dönerse o live sayfayı
                # ısıtılmış sayfa olarak tut ve 'live-click' modunda kullan.
                tok = await self._extract_token_from_click(page)
                if tok:
                    self._captured_live_token = tok
                    mode = "live-click"
                    logger.info(
                        "recaptcha_browser_live_token_captured",
                        token_len=len(tok),
                    )
                else:
                    mode = "live"
                    logger.info(
                        "recaptcha_browser_using_live_page",
                        url=target_url,
                        hint="click did not capture a token; will retry on"
                             " next produce call",
                    )
            else:
                # --- Aşama 2: bootstrap fallback ---
                logger.warning(
                    "recaptcha_browser_falling_back_to_bootstrap",
                    url=target_url,
                )
                with contextlib.suppress(Exception):
                    await page.goto("about:blank")
                await self._install_bootstrap(page)
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=int(
                        self.settings.recaptcha_browser_timeout * 1000
                    ),
                    referer=self.settings.base_url + "/",
                )
                ready = await self._wait_for_grecaptcha(page, timeout_ms=15000)
                if not ready:
                    diag = ""
                    with contextlib.suppress(Exception):
                        diag = str(
                            await page.evaluate(
                                """() => JSON.stringify({
                                  has_grecaptcha: typeof grecaptcha!=='undefined',
                                  script_failed: !!window.__wrapperRecaptchaScriptFailed,
                                  page_error: window.__wrapperRecaptchaError,
                                  readyState: document.readyState,
                                  location: location.href
                                })"""
                            )
                        )[:300]
                    raise RecaptchaError(
                        "reCAPTCHA API did not become ready."
                        + (f" Diagnostics: {diag}" if diag else "")
                    )
                if await page.evaluate(
                    "() => window.__wrapperRecaptchaScriptFailed === true"
                ):
                    raise RecaptchaError(
                        "Google reCAPTCHA script failed to load; "
                        "check outbound access to www.google.com."
                    )
                with contextlib.suppress(Exception):
                    await page.evaluate(_WARMUP_SCRIPT)
                await asyncio.sleep(1.5 + random.random() * 0.3)
                mode = "bootstrap"

            self._warm_page = page
            self._warm_mode: str = mode
            logger.info("recaptcha_browser_page_warmed", mode=mode)
        return page, getattr(self, "_warm_mode", "bootstrap")  # type: ignore[attr-defined]

    async def _produce_token(self) -> str:
        async with self._lock:
            self.stats["last_token_source"] = None
            if self._reset_warm_page:
                self._reset_warm_page = False
                self._captured_live_token = None
                if self._warm_page is not None:
                    with contextlib.suppress(Exception):
                        await self._warm_page.close()
                    self._warm_page = None
            if not self._is_alive():
                await self.shutdown()
                await self.startup()
            assert self._context is not None
            try:
                page, mode = await self._ensure_warm_page()
            except Exception:
                await self.shutdown()
                await self.startup()
                assert self._context is not None
                try:
                    page, mode = await self._ensure_warm_page()
                except Exception:
                    await self.shutdown()
                    raise

            # Live-click modunda token'ı zaten yakaladık: bir kez
            # kullanırız, sonra sayfayı sıfırlarız (bir sonraki istek
            # yeniden tıklayıp yeni token yakalasın).
            cached = getattr(self, "_captured_live_token", None)
            if cached:
                self._captured_live_token = None  # type: ignore[attr-defined]
                # Sayfayı da kapat: bir sonraki istek yeniden tıklasın.
                with contextlib.suppress(Exception):
                    await page.close()
                self._warm_page = None
                self._record_token_source("live_click")
                return cached

            try:
                if mode == "live":
                    # Tıklama ilk seferde yakalayamadıysak bir daha dene;
                    # olmazsa doğrudan execute et.
                    tok = await self._extract_token_from_click(page)
                    if tok:
                        with contextlib.suppress(Exception):
                            await page.close()
                        self._warm_page = None
                        self._record_token_source("live_click")
                        return tok
                # Doğrudan execute() ile üret (live/bootstrap):
                with contextlib.suppress(Exception):
                    await page.evaluate(_WARMUP_SCRIPT)
                    await asyncio.sleep(random.uniform(0.25, 0.5))
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
                diag = ""
                with contextlib.suppress(Exception):
                    diag = str(
                        await page.evaluate(
                            """() => JSON.stringify({
                              has_grecaptcha: typeof grecaptcha!=='undefined',
                              has_enterprise: !!(typeof grecaptcha!=='undefined'
                                && grecaptcha.enterprise),
                              script_failed: !!window.__wrapperRecaptchaScriptFailed,
                              page_error: window.__wrapperRecaptchaError,
                              readyState: document.readyState,
                              location: location.href
                            })"""
                        )
                    )[:500]
                with contextlib.suppress(Exception):
                    await page.close()
                self._warm_page = None
                raise RecaptchaError(
                    "Timed out while generating a reCAPTCHA token."
                    + (f" Diagnostics: {diag}" if diag else "")
                ) from exc
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await page.close()
                self._warm_page = None
                raise RecaptchaError(
                    f"Browser captcha generation failed: {exc}"
                ) from exc

            if not isinstance(token, str) or not token:
                raise RecaptchaError("Browser returned an empty reCAPTCHA token.")
            self._record_token_source("live_execute" if mode == "live" else "bootstrap_execute")
            return token
