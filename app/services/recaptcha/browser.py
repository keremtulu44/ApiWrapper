"""Playwright ile headless tarayıcıda `grecaptcha.execute()` çağıran sağlayıcı."""

from __future__ import annotations

import asyncio
import contextlib
import random
from typing import Any
from urllib.parse import urlparse

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

#: Boot betiği: navigator.webdriver'ı kaldır, küçük kullanıcı hareketleri yap,
#: script yükleme hatalarını yakala. _WARMUP_SCRIPT sayfa açıldıktan ~1.5 sn
#: sonra çağrılır ve hafif imleç kayması + scroll ile insan benzeri davranış
#: ekler. Bu eylemler reCAPTCHA v3/Enterprise skorunu ciddi ölçüde etkiliyor;
#: boş sayfada anında execute() düşük skorla reddediliyordu.
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
  h1{font-size:20px;margin:0 0 12px}
  p{color:#555;line-height:1.5;margin:0 0 8px}
  textarea{width:100%;min-height:120px;border:1px solid #ddd;border-radius:8px;
  padding:12px;font:inherit;resize:vertical}
  button{margin-top:8px;padding:10px 18px;border-radius:8px;border:0;
  background:#10a37f;color:#fff;font:inherit;cursor:pointer}
  .status{margin-top:12px;color:#888;font-size:13px}
</style>
<script>
// Stealth: navigator.webdriver otomasyon imzasını kaldır.
Object.defineProperty(navigator, 'webdriver', {get: () => false});
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
<div id="root">
  <div class="wrap">
    <h1>New chat</h1>
    <p>How can I help you today?</p>
    <textarea placeholder="Message ChatGPT…" id="prompt"></textarea>
    <button id="send">Send</button>
    <div class="status" id="status">Ready.</div>
  </div>
</div>
</body></html>
"""

#: Isınma: hafif rastgele imleç hareketi ve küçük scroll. Gerçek bir kullanıcı
#: chat formuna bakmadan anında göndermez; 1–2 sn gezinir. Bu tarz küçük
#: dokunuşlar reCAPTCHA skorunu 0.1'den 0.7+ seviyesine çekiyor.
_WARMUP_SCRIPT = """
() => {
  try {
    const root = document.getElementById('root');
    if (!root || !window.innerWidth) return;
    const w = window.innerWidth, h = window.innerHeight;
    const evt = (type, x, y) => {
      const ev = new MouseEvent(type, {
        clientX: x, clientY: y, bubbles: true, cancelable: true, view: window
      });
      document.dispatchEvent(ev);
    };
    // Birkaç fare konumu: sol üst → orta → form üstü.
    const pts = [
      [Math.floor(w*0.15), Math.floor(h*0.2)],
      [Math.floor(w*0.45+Math.random()*20), Math.floor(h*0.5+Math.random()*15)],
      [Math.floor(w*0.5+Math.random()*30), Math.floor(h*0.65+Math.random()*10)],
    ];
    let i = 0;
    const step = () => {
      if (i >= pts.length) return;
      const [x,y] = pts[i++];
      evt('mousemove', x, y);
      setTimeout(step, 120 + Math.random()*100);
    };
    evt('mousemove', 10, 10);
    setTimeout(step, 50);
    // Küçük scroll.
    window.scrollBy({top: 20, behavior: 'instant'});
    setTimeout(() => window.scrollBy({top: -10, behavior: 'instant'}), 300);
    // Form alanına tıklama benzeri (gerçek odaklanma olmasa da event akışı oluşur).
    const prompt = document.getElementById('prompt');
    if (prompt) {
      setTimeout(() => {
        evt('mousemove', Math.floor(w*0.3), Math.floor(h*0.7));
        prompt.dispatchEvent(new MouseEvent('mouseover', {bubbles:true}));
      }, 600);
    }
  } catch (e) { /* ısınma hatası token üretimini engellemesin */ }
}
"""

#: İlk ısınma sonrası kısa gecikme (ms). 1500–2200 arası, üstel değil sabit
#: aralık seçiyoruz: insan benzeri ama kontrollü.
_WARMUP_WAIT_MS = 1800


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
        self._warm_page: Any = None  # yeniden kullanılan ısınmış sayfa
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
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
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
            ]
            # --single-process Render 512MB için gerekli ama normalde hata
            # ayıklama için kaldırılabilir:
            launch_args.insert(2, "--single-process")
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=launch_args,
            )
            locale = (
                self.settings.upstream_accept_language.split(",", 1)[0].strip() or "en-US"
            )
            self._context = await self._browser.new_context(
                user_agent=self.settings.upstream_user_agent,
                locale=locale,
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": self.settings.upstream_accept_language or "en-US,en;q=0.9",
                },
            )
            # Navigator otomasyon izlerini CDP üzerinden de kaldır
            await self._context.add_init_script(
                """
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                Object.defineProperty(navigator, 'languages', {
                  get: () => ['en-US','en']
                });
                Object.defineProperty(navigator, 'plugins', {
                  get: () => [1, 2, 3, 4, 5]
                });
                // chrome.runtime'ın varlığı bot tespitini azaltır.
                window.chrome = { runtime: {} };
                const origGetParameter =
                  WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p){
                  // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL
                  if (p === 37445) return 'Google Inc. (Intel)';
                  if (p === 37446) {
                    return 'ANGLE (Intel, Intel(R) UHD Graphics 630 '
                      + 'Direct3D11 vs_5_0 ps_5_0, D3D11)';
                  }
                  return origGetParameter.call(this, p);
                };
                """
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
        for closer in (self._warm_page, self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):  # pragma: no cover
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                await self._playwright.stop()
        self._warm_page = None
        self._context = self._browser = self._playwright = None
        logger.info("recaptcha_browser_stopped")

    async def _set_cookies_from_settings(self) -> None:
        """Context çerezlerine Cloudflare/oturum çerezlerini ekle.

        Tarayıcının ilk isteğinde cf_clearance/__cf_bm gibi çerezler yoksa
        Google script'i bazen farklı risk skorluyor olabilir; ayrıca
        _vcrr_ec… çerezi arena.ai kendi ziyaretçi tanımlaması için.
        Sadece mevcut runtime kayıtlı çerezler enjekte edilir; yoksa sessizce
        geçilir.
        """
        assert self._context is not None
        if not self.settings.base_url:
            return
        parsed = urlparse(self.settings.base_url)
        if not parsed.hostname:
            return
        try:
            from app.upstream.state_store import load_runtime_state
        except Exception:  # pragma: no cover
            return
        try:
            state = load_runtime_state(self.settings)
        except Exception:
            return
        # Cookie header'ı "name=value; name2=value2" biçiminde saklanıyor;
        # bakılacak yer upstream session store olabilir ama en kolay kaynak
        # settings'de birikmiş son kullanılan cookie_header.
        cookie_header: str = ""
        if isinstance(state, dict):
            cookie_header = str(state.get("last_cookie_header") or "")
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
            if name.lower() in {"__cf_bm", "_dd_s", "cf_clearance", "_vcrrc",
                                "_vcrr_ec68a2456e920849"}:
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

    async def _ensure_warm_page(self) -> Any:
        """Isınmış bir sayfa döndürür; yoksa oluşturur.

        Sayfanın yaşı ve sürekli açık kalması reCAPTCHA skorunu yükseltir
        (ziyaretçi başına ilk dakika riskli, sonra skoru oturur). Her token
        için yeni sayfa açmak yerine aynı sayfayı kullanıyoruz; sayfa
        kapanırsa (crash/navigation) yeniden açılır.
        """
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
            await self._load_page(page)
            # Isınma: 1.5–2 sn insan benzeri hareket, grecaptcha hazır olsun.
            await page.wait_for_function(
                """(version) => {
                  if (typeof grecaptcha === 'undefined') return false;
                  const api = version === 'enterprise'
                    ? grecaptcha.enterprise : grecaptcha;
                  return !!(api && (api.execute || api.ready));
                }""",
                arg=self.settings.recaptcha_version,
                timeout=20000,
            )
            with contextlib.suppress(Exception):
                await page.evaluate(_WARMUP_SCRIPT)
            await asyncio.sleep(_WARMUP_WAIT_MS / 1000.0 + random.uniform(0, 0.3))
            self._warm_page = page
            logger.info("recaptcha_browser_page_warmed")
        return self._warm_page

    async def _produce_token(self) -> str:
        async with self._lock:
            if not self._is_alive():
                await self.shutdown()
                await self.startup()
            assert self._context is not None

            try:
                page = await self._ensure_warm_page()
            except Exception:
                # Isınmış sayfa oluşturma başarısız olduysa tarayıcıyı temiz
                # yeniden başlatıp bir kez daha dene (Chromium renderer
                # çökmüş olabilir).
                await self.shutdown()
                await self.startup()
                assert self._context is not None
                page = await self._ensure_warm_page()

            try:
                # Hafif tekrar ısıt: imleci biraz kıpırdat, sonra token üret.
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
                # Zaman aşımı ısınmış sayfayı da kırmış olabilir; sıfırla.
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
            return token

    async def _load_page(self, page: Any) -> None:
        """CAPTCHA'yı hedef origin'de, sohbeti andıran izole bir belgeyle yükle.

        - Gerçek upstream_chat_url (veya base_url + /c/...) üzerinde
          açılırız; böylece token'ın üretildiği path, upstream'in beklediği
          sohbet path'iyle eşleşir (Enterprise bazı konfigürasyonlarda
          path/sayfa bağlılığı denetliyor).
        - Tüm aynı-origin istekleri (favicon, CSS, img, script) bootstrap
          HTML ile cevaplanır; hiçbir istek askıda kalmaz. Google
          reCAPTCHA script'i www.google.com'da; o route'a dokunmayız.
        - Gerçek bir chat sayfasına benzeyen başlık/metin/alan/buton
          içeren HTML; navigator.webdriver override; sonra ısınma
          script'inde hafif kullanıcı hareketleri.
        """
        script_name = (
            "enterprise.js" if self.settings.recaptcha_version == "enterprise" else "api.js"
        )
        # Hedef URL: gerçek referer path'i (sohbet/anlık sayfa). Path içinde
        # {chat_id} placeholder'ı varsa ısınma için uygun bir kimlikle doldur,
        # yoksa path'i olduğu gibi kullan (örn. "/text/direct").
        referer_path = self.settings.upstream_referer_path or "/"
        placeholder_chat_id = "captcha-warmup"
        if "{chat_id}" in referer_path:
            referer_path = referer_path.replace("{chat_id}", placeholder_chat_id)
        target_origin = self.settings.base_url.rstrip("/") + (
            referer_path if referer_path.startswith("/") else "/" + referer_path
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

        async def serve_bootstrap(route: Any) -> None:
            # Tüm aynı-origin istekleri bootstrap HTML'i ile cevapla.
            # Sadece Google reCAPTCHA ve www.gstatic.com istekleri normal
            # akışta kalsın (onları route'a dahil etmedik, serbest).
            try:
                await route.fulfill(
                    status=200, content_type="text/html; charset=utf-8", body=html,
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await route.abort("failed")

        # Wildcard route: origin içindeki HER şeyi bootstrap'le cevapla.
        # Yalnızca Google/gstatic harici istekler normal ağa çıkar (onlar
        # zaten farklı origin'de, route'umuz onları yakalamaz).
        route_pattern = f"{self.settings.base_url.rstrip('/')}/**/*"
        # Ek olarak kök URL'yi de kapsayacak şekilde: route pattern'ı
        # "**/*" kullandığı için "/" için eşleşmeyebilir; o yüzden ikinci
        # route da ekleyelim.
        root_pattern = f"{self.settings.base_url}/"

        try:
            await page.route(route_pattern, serve_bootstrap)
            await page.route(root_pattern, serve_bootstrap)
            # Veri: URL'ler boş favicon'a data: ile gidiyor, harici istek yok.
            await page.goto(
                target_origin,
                wait_until="domcontentloaded",
                timeout=self.settings.recaptcha_browser_timeout * 1000,
                referer=self.settings.base_url + "/",
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
                "Could not initialize the isolated reCAPTCHA page or load"
                " the Google script."
                + (f" Diagnostics: {diag}" if diag else "")
            ) from exc

        if await page.evaluate("() => window.__wrapperRecaptchaScriptFailed === true"):
            raise RecaptchaError(
                "Google reCAPTCHA script failed to load; "
                "check outbound access to www.google.com."
            )
