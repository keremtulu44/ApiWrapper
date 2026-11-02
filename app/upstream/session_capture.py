"""Sunucu tarafındaki Chromium ile taze upstream oturumu yakalama.

**Neden var?**

Oturum düştüğünde ya da sürüm damgası eskidiğinde tek gereken şey taze bir
cURL'dir. Ama telefonda DevTools yok: "Copy as cURL" yapılamıyor, Render Shell
de yok. Bu modül yakalamayı **telefonun yerine servisteki tarayıcıya** yaptırır:

1. Zaten var olan cookie/token bağlama yüklenir,
2. hedef sayfa açılır (uygulama kendi oturum yenilemesini tetikleyebilir),
3. **gerçek** bir `create-evaluation` isteği yakalanır: cookie'ler, `dpl_...`
   sürüm damgası, `cx` ve referer yolu,
4. sonuç çalışan ayarlara uygulanır.

Burada hiçbir DOM ayrıntısı zorunlu değildir: sohbet kutusu bulunamazsa bile
sayfa yüklemesi sırasında giden herhangi bir API isteğinden başlıklar okunur.
Bulunamazsa uç nokta açıkça "login gerekli" der — sessizce başarısız olmaz.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.headers import DEPLOYMENT_HEADERS

logger = get_logger(__name__)

#: Sohbet isteği sayılacak URL parçaları (uç yolu ayarlardan da eklenir).
_STREAM_HINTS = ("create-evaluation", "post-to-evaluation", "stream")

#: "Giriş yapılmamış" sayılan URL parçaları.
_LOGIN_HINTS = ("login", "signin", "sign-in", "auth/", "welcome")

#: Oturum çerezi olduğunu gösteren ad parçaları.
_SESSION_COOKIE_HINTS = ("auth", "session", "token", "sb-")


@dataclass(slots=True)
class CaptureResult:
    """Yakalama sonucu (gizli değerler loglanmaz, yalnız döndürülür)."""

    cookies: str = ""
    #: Yakalanan deployment başlıkları (küçük harf ad → değer).
    deployment_headers: dict[str, str] = field(default_factory=dict)
    deployment_id: str | None = None
    cx: str | None = None
    referer_path: str | None = None
    logged_in: bool = False
    triggered_request: bool = False
    final_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookies.strip())


def cookie_header_from(items: list[dict[str, Any]]) -> str:
    """Playwright cookie listesini `name=value; name2=value2` başlığına çevirir."""
    parts: list[str] = []
    for item in items or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _looks_logged_in(cookies: list[dict[str, Any]]) -> bool:
    return any(
        any(hint in str(item.get("name", "")).lower() for hint in _SESSION_COOKIE_HINTS)
        for item in cookies or []
    )


def _looks_like_id(segment: str) -> bool:
    """Sohbet kimliği gibi görünen yol parçası mı? (uuid/uzun karma)"""
    if len(segment) >= 16:
        return True
    return len(segment) >= 8 and "-" in segment


def _referer_path(url: str, settings: Settings) -> str | None:
    """Mutlak URL'den `{chat_id}` şablonlu referer yolu üretir."""
    if not url:
        return None
    host = settings.target_domain
    path = url.split(f"//{host}", 1)[-1] if host in url else url
    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and _looks_like_id(parts[-1]):
        parts[-1] = "{chat_id}"
    return "/" + "/".join(parts) if parts else "/"


async def capture_session(
    context: Any,
    settings: Settings,
    *,
    wait_ms: int = 4000,
    navigation_timeout_ms: int = 25000,
    prompt: str = "merhaba",
) -> CaptureResult:
    """Mevcut tarayıcı bağlamıyla taze oturum bilgilerini yakalar.

    `context` Playwright `BrowserContext`'tir; testlerde basit bir sahte nesne de
    kullanılabilir (yalnız `new_page`, `cookies`, `add_cookies` beklenir).
    """
    result = CaptureResult()
    result_headers = result.deployment_headers
    page = await context.new_page()
    observed: dict[str, str] = {}

    def on_request(request: Any) -> None:
        try:
            headers = request.headers
        except Exception:  # pragma: no cover - savunmacı
            return
        lowered = {str(k).lower(): v for k, v in headers.items()}
        for name in DEPLOYMENT_HEADERS:
            if lowered.get(name):
                observed.setdefault("deployment_id", str(lowered[name]))
                result_headers[name] = str(lowered[name])
        if lowered.get("cx"):
            observed.setdefault("cx", str(lowered["cx"]))
        if lowered.get("referer"):
            observed.setdefault("referer", str(lowered["referer"]))
        url = str(getattr(request, "url", ""))
        if any(hint in url for hint in (*_STREAM_HINTS, settings.upstream_stream_path)):
            observed["stream_request"] = "1"

    try:
        # Sayfa yüklenirken giden tüm istekleri dinle: damga başlıkları buradan gelir.
        with contextlib.suppress(Exception):  # sahte sayfalarda `on` olmayabilir
            page.on("request", on_request)

        url = settings.base_url
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        except Exception as exc:
            result.notes.append(f"sayfa açılamadı: {type(exc).__name__}")

        await _wait(page, wait_ms)
        result.final_url = str(getattr(page, "url", "") or "")

        # Sohbet isteğini tetiklemeyi dene (bulunamazsa sessizce devam).
        result.triggered_request = await _try_send_message(page, prompt, wait_ms)

        # HTML'de damga varsa onu da kullan (istek yakalanamadıysa).
        if "deployment_id" not in observed:
            html = await _content(page)
            if html:
                from app.upstream.deployment import _from_body  # tek kaynak

                found = _from_body(html)
                if found:
                    observed["deployment_id"] = found
                    result.notes.append("damga HTML'den okundu")

        try:
            cookies = list(await context.cookies(url))
        except Exception:  # pragma: no cover - savunmacı
            cookies = []
        result.cookies = cookie_header_from(cookies)
        result.logged_in = _looks_logged_in(cookies) and not any(
            hint in result.final_url.lower() for hint in _LOGIN_HINTS
        )
        result.deployment_id = observed.get("deployment_id")
        if result.deployment_id and not result.deployment_headers:
            # Tek bir başlık adı görüldüyse onu kullan; isim bilinmiyorsa
            # varsayılan iki damga başlığı yazılır (canlı cURL'ler ikisini de taşır).
            result.deployment_headers = dict.fromkeys(
                DEPLOYMENT_HEADERS, result.deployment_id
            )
        result.cx = observed.get("cx")
        referer = observed.get("referer")
        result.referer_path = _referer_path(referer, settings) if referer else None
        if not result.logged_in:
            result.notes.append(
                "oturum çerezi bulunamadı ya da sayfa giriş ekranına düştü"
            )
        if "stream_request" in observed:
            result.notes.append("gerçek sohbet isteği yakalandı")
        return result
    finally:
        with contextlib.suppress(Exception):
            await page.close()


async def _wait(page: Any, ms: int) -> None:
    try:
        await page.wait_for_timeout(ms)
    except Exception:  # pragma: no cover - sahte sayfalarda yok
        return


async def _content(page: Any) -> str:
    try:
        return str(await page.content())
    except Exception:  # pragma: no cover
        return ""


async def _try_send_message(page: Any, prompt: str, wait_ms: int) -> bool:
    """Sohbet kutusuna kısa bir istem yazıp Enter'a basmayı dener.

    Başarılı olursa uygulama kendi `create-evaluation` isteğini atar ve
    `on_request` gerçek başlıkları yakalar. DOM tanınmazsa False döner.
    """
    selectors = ("textarea", '[contenteditable="true"]', 'input[type="text"]')
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            await locator.click(timeout=3000)
            await locator.fill(prompt, timeout=3000)
            await page.keyboard.press("Enter")
            await _wait(page, wait_ms)
            return True
        except Exception:
            continue
    return False


async def capture_with_provider(provider: Any, settings: Settings) -> CaptureResult:
    """Var olan tarayıcı bağlamını kullanır; yoksa geçici bir tane açar.

    Serviste reCAPTCHA `browser` sağlayıcısı zaten bir Chromium çalıştırıyor
    (Render Starter'da iki tarayıcı belleğe sığmaz), bu yüzden önce onu
    kullanıyoruz. Sağlayıcı tarayıcı değilse ve Playwright kuruluysa tek
    kullanımlık bir tarayıcı açılır.
    """
    getter = getattr(provider, "browser_context", None)
    if getter is not None:
        context = await getter()
        return await capture_session(context, settings)

    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValueError(
            "Tarayıcı yakalama için Chromium gerekir: RECAPTCHA_PROVIDER=browser "
            "kullanın ya da 'apiwrapper[browser]' ekini kurun."
        ) from exc

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                user_agent=settings.upstream_user_agent,
                locale=(settings.upstream_accept_language.split(",", 1)[0] or "en-US"),
            )
            return await capture_session(context, settings)
        finally:
            await browser.close()
    finally:
        await playwright.stop()


def apply_capture_to_settings(settings: Settings, result: CaptureResult) -> list[str]:
    """Yakalanan oturumu çalışan ayarlara uygular (değerler loglanmaz)."""
    changed: list[str] = []
    if result.has_cookie and result.cookies != settings.upstream_cookie:
        settings.upstream_cookie = result.cookies
        changed.append("upstream_cookie")
    if result.deployment_headers:
        headers = settings.parsed_extra_headers()
        updated = False
        for name, value in result.deployment_headers.items():
            if headers.get(name) != value:
                headers[name] = value
                updated = True
        if updated:
            settings.upstream_extra_headers = ";".join(
                f"{key}={value}" for key, value in headers.items()
            )
            changed.append("upstream_extra_headers")
    if result.cx:
        headers = settings.parsed_extra_headers()
        if headers.get("cx") != result.cx:
            headers["cx"] = result.cx
            settings.upstream_extra_headers = ";".join(
                f"{key}={value}" for key, value in headers.items()
            )
            changed.append("upstream_cx")
    if result.referer_path and settings.upstream_referer_path != result.referer_path:
        settings.upstream_referer_path = result.referer_path
        changed.append("upstream_referer_path")
    return changed
