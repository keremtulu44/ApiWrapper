"""Sunucu tarafındaki Chromium ile taze upstream oturumu yakalama.

**Neden var?**

Oturum düştüğünde ya da sürüm damgası eskidiğinde tek gereken şey taze bir
cURL'dir. Ama telefonda DevTools yok: "Copy as cURL" yapılamıyor, Render Shell
de yok. Bu modül yakalamayı **telefonun yerine servisteki tarayıcıya** yaptırır:

1. Zaten var olan cookie/token bağlama yüklenir,
2. hedef sayfa açılır (uygulama kendi oturum yenilemesini tetikleyebilir),
3. **gerçek** bir `create-evaluation` isteği yakalanır: cookie'ler, `dpl_...`
   sürüm damgası, `cx`, referer yolu ve akış endpoint yolu,
4. sonuç çalışan ayarlara uygulanır; yakalanan sohbet yolu eski
   `UPSTREAM_STREAM_PATH` değerini düzeltebilir.

Burada hiçbir DOM ayrıntısı zorunlu değildir: sohbet kutusu bulunamazsa bile
sayfa yüklemesi sırasında giden herhangi bir API isteğinden başlıklar okunur.
Bulunamazsa uç nokta açıkça "login gerekli" der — sessizce başarısız olmaz.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import (
    extract_access_token,
    extract_refresh_token,
    is_token_expired,
    parse_cookie_header,
    token_expiry,
    token_seconds_remaining,
)
from app.upstream.curl_import import looks_like_stream_path, stream_path_template
from app.upstream.header_store import format_extra_headers
from app.upstream.headers import DEPLOYMENT_HEADERS
from app.upstream.state_store import save_runtime_state
from app.utils.ids import new_chat_id

logger = get_logger(__name__)

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
    #: Gerçek sohbet isteğinden alınan, `{chat_id}`'si normalize edilmiş yol.
    stream_path: str | None = None
    logged_in: bool = False
    triggered_request: bool = False
    final_url: str = ""
    final_path: str = ""
    #: Mevcut cookie bağlama yüklendi mi (0 = yüklenemedi).
    injected_cookies: int = 0
    #: Yükleme başarısızsa gizli değer içermeyen neden/kademe bilgisi.
    cookie_injection_error: str = ""
    #: Sayfa HTTP durumu ve bot koruması göstergesi (içerik/token taşımaz).
    navigation_status: int | None = None
    bot_challenge_detected: bool = False
    access_token_found: bool = False
    access_token_expired: bool | None = None
    #: Oturum yakalanamadıysa nedeni (Türkçe, gizli değer içermez).
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    #: Tarayıcı jar'ında bulunan çerez **adları** (değer yok). Teşhis için:
    #: "çerez var" demek yetmiyor — oturumu taşıyan adın gelip gelmediği önemli.
    cookie_names: list[str] = field(default_factory=list)
    #: Adında auth/session/token geçen bir çerez var mı?
    session_cookie_found: bool = False
    #: Sayfa giriş ekranına yönlendi mi (oturum çerezle birlikte ölmüş)?
    login_redirect: bool = False
    #: Yakalanan çerezdeki token mevcut ayarlardakinden daha mı taze?
    fresher_token_found: bool = False
    #: Ağ isteklerinden veya sayfadan yakalanan Supabase `apikey` (anon anahtar).
    refresh_api_key: str | None = None

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookies.strip())

    @property
    def cookie_application_safe(self) -> bool:
        """Tarayıcı çerezleri gerçek oturum kanıtı taşıyor mu?

        Yalnız ``logged_in`` bayrağına güvenmek yeterli değil: auth *adlı*
        bayat bir çerez, hiç çerez yüklenemeyen anonim bir sekmede bile yanlış
        pozitif üretebilirdi. Geçerli JWT, gerçek akış isteği veya bu çalışma
        sırasında yüklenmiş oturum çerezi olmadan mevcut ayar ezilmez.
        """
        if not self.logged_in:
            return False
        valid_token = self.access_token_found and self.access_token_expired is False
        return bool(self.injected_cookies > 0 or self.stream_path or valid_token)


def cookie_header_from(items: list[dict[str, Any]]) -> str:
    """Playwright cookie listesini `name=value; name2=value2` başlığına çevirir."""
    parts: list[str] = []
    for item in items or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _path_of(url: str) -> str:
    """URL'den yol kısmını döndürür (domain loglanmasın diye)."""
    if not url:
        return ""
    without_scheme = url.split("://", 1)[-1]
    path = without_scheme.split("/", 1)[-1] if "/" in without_scheme else ""
    return "/" + path.split("?", 1)[0].split("#", 1)[0].lstrip("/")


def _explain_not_logged_in(result: CaptureResult, login_redirect: bool) -> str:
    """Yakalanamama nedenini tek cümlede, eyleme dönük biçimde yazar.

    Üç ayrı durum var ve üçü farklı eylem gerektirir:

    1. Tarayıcıya hiç çerez yüklenmedi → `UPSTREAM_COOKIE` boş/bozuk.
    2. Çerezler geldi ama içinde oturum taşıyan ad yok → eksik çerez seti
       (telefonda en sık görülen durum: yalnız birkaç çerez kopyalanmış).
    3. Çerezlerde oturum adı var ama sayfa giriş ekranına düştü → oturum ölmüş,
       yeni giriş şart.
    """
    if login_redirect:
        return (
            f"sayfa giriş ekranına yönlendi ({result.final_path or '/'}): "
            "oturum çerezi ölü, yeni giriş gerekiyor."
        )
    if result.bot_challenge_detected:
        return (
            "sayfa bot koruması/doğrulama ekranında kaldı; sohbet arayüzü "
            "yüklenemedi. Birkaç dakika sonra tekrar deneyin veya /cookie kullanın."
        )
    if not result.cookie_names:
        detail = (
            f" ({result.cookie_injection_error})" if result.cookie_injection_error else ""
        )
        return (
            "tarayıcıya hiç çerez yüklenemedi: UPSTREAM_COOKIE boş, geçersiz "
            f"veya Chromium reddetti{detail}."
        )
    if result.access_token_found and result.access_token_expired:
        return "cookie'deki access_token süresi dolmuş; site oturumu yenilemedi."
    if not result.access_token_found and not result.session_cookie_found:
        names = ", ".join(result.cookie_names[:8])
        return (
            "gelen çerezlerde oturum taşıyan ad yok "
            f"(gelenler: {names}). Giriş yapmış tarayıcıdan **tüm** çerezleri aktarın."
        )
    if result.cookie_injection_error:
        return (
            "mevcut oturum çerezleri tarayıcıya yüklenemedi "
            f"({result.cookie_injection_error}); sayfa anonim/bot korumalı "
            "açılmış olabilir. Önce /cookie ile taze auth çerezlerini uygulayın."
        )
    return "sayfa oturum açmadı (gerçek sohbet isteği görülmedi)."


async def _inject_cookies(context: Any, settings: Settings) -> tuple[int, str]:
    """Mevcut çerezleri temiz bağlama yükle; sayı ve güvenli hata kodu döndür.

    Paylaşılan Chromium context'inde önceki yakalamanın auth parçaları kalabilir.
    Onları yeni oturumla karıştırmamak için önce jar temizlenir. Domain biçimi
    reddedilirse aynı çerezler ``url`` biçimiyle bir kez daha denenir. Hata
    metinleri cookie/token taşıyabileceğinden yalnız sınıf adı döndürülür.
    """
    clearer = getattr(context, "clear_cookies", None)
    if clearer is not None:
        try:
            await clearer()
        except Exception as exc:  # pragma: no cover - tarayıcıya bağlı
            logger.warning(
                "upstream_capture_cookie_clear_failed", error_type=type(exc).__name__
            )

    raw_cookie = (settings.upstream_cookie or "").strip()
    cookies = parse_cookie_header(raw_cookie)
    if not cookies:
        return 0, "upstream_cookie_empty" if not raw_cookie else "upstream_cookie_invalid"
    domain = (settings.target_domain or "").split(":", 1)[0].strip()
    if not domain:
        return 0, "target_domain_missing"
    payload = [
        {
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "secure": settings.upstream_scheme == "https",
        }
        for name, value in cookies.items()
    ]
    adder = getattr(context, "add_cookies", None)
    if adder is None:
        return 0, "browser_cookie_api_unavailable"
    try:
        await adder(payload)
        return len(payload), ""
    except Exception as exc:
        first_error = type(exc).__name__

    # Playwright bazı sürümler/domain biçimlerinde domain yerine URL ister.
    url_payload = [
        {
            "name": name,
            "value": value,
            "url": settings.base_url,
            "secure": settings.upstream_scheme == "https",
        }
        for name, value in cookies.items()
    ]
    try:
        await adder(url_payload)
        logger.info("upstream_capture_cookies_injected_with_url_fallback")
        return len(url_payload), ""
    except Exception as exc:
        reason = f"domain:{first_error};url:{type(exc).__name__}"
        logger.warning("upstream_capture_cookie_injection_failed", reason=reason)
        return 0, reason


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
        if lowered.get("apikey"):
            observed.setdefault("refresh_api_key", str(lowered["apikey"]))
        url = str(getattr(request, "url", ""))
        parsed_url = urlsplit(url)
        target_host = urlsplit("//" + settings.target_domain).hostname
        request_host = parsed_url.hostname
        configured_path = urlsplit(settings.upstream_stream_path).path
        same_host = bool(
            target_host
            and request_host
            and request_host.lower() == target_host.lower()
        )
        stream_like = looks_like_stream_path(parsed_url.path) or (
            bool(configured_path) and parsed_url.path == configured_path
        )
        if same_host and stream_like:
            observed["stream_request"] = "1"
            full_path = parsed_url.path or "/"
            if parsed_url.query:
                full_path += "?" + parsed_url.query
            body: dict[str, Any] = {}
            post_data = getattr(request, "post_data", None)
            if isinstance(post_data, str):
                try:
                    parsed_body = json.loads(post_data)
                except (TypeError, ValueError):
                    parsed_body = None
                if isinstance(parsed_body, dict):
                    body = parsed_body
            observed.setdefault(
                "stream_path", stream_path_template(full_path, body)
            )

    try:
        # Sayfa yüklenirken giden tüm istekleri dinle: damga başlıkları buradan gelir.
        with contextlib.suppress(Exception):  # sahte sayfalarda `on` olmayabilir
            page.on("request", on_request)

        # Eldeki oturumu bağlama yükle. Bu olmadan tarayıcı anonim açılır ve
        # her site giriş ekranına düşer (eski sürümdeki hata buydu).
        result.injected_cookies, result.cookie_injection_error = await _inject_cookies(
            context, settings
        )

        # Kök sayfa bazı sağlayıcılarda pazarlama/oturum açma sayfasıdır ve
        # sohbet alanı içermez. Gerçek sohbet arayüzünü açmak için cURL'den
        # gelen referer yolu kullanılır; gerekiyorsa geçici bir chat ID üretilir.
        url = settings.referer_url(new_chat_id())
        try:
            navigation = await page.goto(
                url, wait_until="domcontentloaded", timeout=navigation_timeout_ms
            )
            navigation_status = getattr(navigation, "status", None)
            result.navigation_status = navigation_status
            if (
                navigation_status is not None
                and navigation_status >= 400
                and url != settings.base_url
            ):
                result.notes.append(f"sohbet sayfası HTTP {navigation_status}")
                fallback_navigation = await page.goto(
                    settings.base_url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms,
                )
                result.navigation_status = getattr(
                    fallback_navigation, "status", navigation_status
                )
        except Exception as exc:
            result.notes.append(f"sohbet sayfası açılamadı: {type(exc).__name__}")
            if url != settings.base_url:
                try:
                    fallback_navigation = await page.goto(
                        settings.base_url,
                        wait_until="domcontentloaded",
                        timeout=navigation_timeout_ms,
                    )
                    result.navigation_status = getattr(
                        fallback_navigation, "status", None
                    )
                except Exception as fallback_exc:
                    result.notes.append(
                        f"ana sayfa açılamadı: {type(fallback_exc).__name__}"
                    )

        await _wait(page, wait_ms)
        result.final_url = str(getattr(page, "url", "") or "")

        # SPA'nın editörü veya Cloudflare'in otomatik doğrulaması ilk bekleme
        # sonunda hazır olmayabilir. Henüz istem gönderilmediyse bir kez daha
        # bekle; başarılı gönderimleri asla tekrarlayarak kota harcama.
        result.triggered_request = await _try_send_message(page, prompt, wait_ms)
        html = await _content(page)
        if not result.triggered_request and wait_ms > 0:
            await _wait(page, min(max(wait_ms, 3000), 6000))
            result.triggered_request = await _try_send_message(page, prompt, wait_ms)
            html = await _content(page)
            result.notes.append("sohbet editörü için ikinci bekleme denemesi yapıldı")

        # HTML'de damga ve olası bot doğrulaması ara. Sayfa metni/loglanmaz.
        if "deployment_id" not in observed and html:
            from app.upstream.deployment import _from_body  # tek kaynak

            found = _from_body(html)
            if found:
                observed["deployment_id"] = found
                result.notes.append("damga HTML'den okundu")
        result.bot_challenge_detected = _is_bot_challenge(html)
        if result.bot_challenge_detected:
            result.notes.append("sayfada bot doğrulaması/challenge algılandı")

        try:
            cookies = list(await context.cookies(url))
        except Exception:  # pragma: no cover - savunmacı
            cookies = []
        result.cookies = cookie_header_from(cookies)
        if "deployment_id" not in observed:
            from app.upstream.deployment import _from_body

            for item in cookies:
                name = str(item.get("name", "")).lower()
                if not name.startswith("_vcrr"):
                    continue
                found = _from_body(str(item.get("value", "")))
                if found:
                    observed["deployment_id"] = found
                    result.notes.append("damga tarayıcı çerezinden okundu")
                    break
        result.cookie_names = sorted(
            {str(item.get("name", "")).strip() for item in cookies if item.get("name")}
        )[:20]
        result.final_path = _path_of(result.final_url)
        token = extract_access_token(result.cookies)
        result.access_token_found = bool(token)
        if token:
            result.access_token_expired = is_token_expired(token)
        login_redirect = any(hint in result.final_path.lower() for hint in _LOGIN_HINTS)
        session_cookie = _looks_logged_in(cookies)
        result.session_cookie_found = session_cookie
        result.login_redirect = login_redirect
        result.fresher_token_found = _has_fresher_token(
            settings, result.cookies, login_redirect=login_redirect
        )
        result.refresh_api_key = observed.get("refresh_api_key")
        if login_redirect:
            result.logged_in = False
        elif token:
            # Token varsa tek ölçüt odur: süresi dolmuş token "oturum açık"
            # sayılmamalı, yoksa bayat çerez çalışan oturumun üzerine yazılır.
            result.logged_in = not result.access_token_expired
        else:
            # Auth *adı* tek başına oturum kanıtı değildir. Bu yakalama
            # sırasında çerez yüklenemediyse anonim sayfadaki bayat auth adı
            # "logged_in" ve cookie güncellemesi üretmemeli.
            result.logged_in = (
                session_cookie and result.injected_cookies > 0
            ) or "stream_request" in observed
        if not result.logged_in:
            result.reason = _explain_not_logged_in(result, login_redirect)
        result.deployment_id = observed.get("deployment_id")
        result.stream_path = observed.get("stream_path")
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
            result.notes.append(result.reason)
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


def _is_bot_challenge(html: str) -> bool:
    """Gerçek doğrulama ekranını, normal sayfadaki Cloudflare betiklerinden ayır.

    ``cf-chl`` veya ``challenge-platform`` gibi teknik adlar uygulamanın normal
    JS paketinde de bulunabilir; bunlar tek başına kanıt değildir. Görünür metin
    için script/style içeriği çıkarılır. Hiçbir sayfa içeriği saklanmaz/loglanmaz.
    """
    sample = html[:200_000]
    lowered = sample.lower()
    if any(
        marker in lowered
        for marker in (
            "window._cf_chl_opt",
            'id="challenge-form"',
            "id='challenge-form'",
            'id="challenge-stage"',
            "id='challenge-stage'",
            'name="cf-turnstile-response"',
            "name='cf-turnstile-response'",
        )
    ):
        return True

    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>",
        " ",
        sample,
        flags=re.IGNORECASE | re.DOTALL,
    )
    visible_text = re.sub(r"<[^>]+>", " ", without_scripts).lower()
    return any(
        marker in visible_text
        for marker in (
            "checking your browser",
            "verify you are human",
            "verify your humanity",
            "just a moment...",
        )
    )


async def _try_send_message(page: Any, prompt: str, wait_ms: int) -> bool:
    """Ana sayfa ve iframe'lerde editörü bulup gerçek akışı tetiklemeyi dene.

    Arena'nın kullandığı editör textarea, contenteditable, ProseMirror, Slate
    veya Lexical olabilir. Gönder düğmesi bulunamazsa Enter kullanılır; DOM
    tanınmazsa hiçbir ayar değiştirilmeden False döner.
    """
    editors = (
        "textarea:not([disabled])",
        '[contenteditable="true"]',
        ".ProseMirror",
        '[data-slate-editor="true"]',
        '[data-lexical-editor="true"]',
        '[role="textbox"]',
        'input[type="text"]:not([disabled])',
    )
    scopes = [page]
    for frame in getattr(page, "frames", []) or []:
        if frame is not page:
            scopes.append(frame)

    for scope in scopes:
        for selector in editors:
            try:
                locator = scope.locator(selector).first
                if not await locator.count():
                    continue
                is_visible = getattr(locator, "is_visible", None)
                if is_visible is not None and not await is_visible():
                    continue
                is_enabled = getattr(locator, "is_enabled", None)
                if is_enabled is not None and not await is_enabled():
                    continue
                await locator.click(timeout=3000)
                await locator.fill(prompt, timeout=3000)

                submitted = False
                for button_selector in (
                    'button[type="submit"]',
                    'button[aria-label*="send" i]',
                    'button[aria-label*="gönder" i]',
                    'button[title*="send" i]',
                    'button[title*="gönder" i]',
                ):
                    try:
                        button = scope.locator(button_selector).first
                        if not await button.count():
                            continue
                        button_visible = getattr(button, "is_visible", None)
                        if button_visible is not None and not await button_visible():
                            continue
                        button_enabled = getattr(button, "is_enabled", None)
                        if button_enabled is not None and not await button_enabled():
                            continue
                        await button.click(timeout=2000)
                        submitted = True
                        break
                    except Exception:
                        continue

                if not submitted:
                    keyboard = getattr(scope, "keyboard", page.keyboard)
                    await keyboard.press("Enter")
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


def cookie_text_from_export(raw: str) -> str:
    """Çerez dışa aktarımını (JSON veya başlık satırı) `ad=değer; ...` hâline çevirir.

    Mobilde iki biçimle karşılaşılıyor:

    * `ad=değer; ad2=değer2` — tarayıcı başlığı veya eklentinin "Header String"i
    * `[{"name": "ad", "value": "değer", ...}, ...]` — Cookie-Editor "Export → JSON"

    JSON biçimi satır kaydırmaları ve tırnaklar yüzünden kopyalanırken bozulabiliyor;
    ikisini de kabul etmek telefonda tek deneme şansını boşa harcamamak için.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Bozuk JSON: başlık satırı gibi değerlendirilsin (aşağıda reddedilir).
            return text
        if isinstance(parsed, list):
            items = [item for item in parsed if isinstance(item, dict)]
            return cookie_header_from(items)
    return text


def inspect_cookie(raw: str) -> dict[str, object]:
    """Çerez metnini **uygulamadan** inceler: hangi adlar var, oturum çerezi var mı?

    Telefonda çerez göndermeden önce doğrulama imkânı: yanlış/eksik seti
    uygulamak, çalışan oturumun üzerine yazma riski taşır. Bu fonksiyon hiçbir
    şeyi değiştirmez ve yalnızca **adları** (değer değil) döndürür.
    """
    text = cookie_text_from_export(raw).strip().strip("'\"")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    cookies = parse_cookie_header(text)
    normalized = cookie_header_from(
        [{"name": name, "value": value} for name, value in cookies.items()]
    )
    token = extract_access_token(normalized) if normalized else None
    session_names = [
        name
        for name in cookies
        if any(hint in name.lower() for hint in _SESSION_COOKIE_HINTS)
    ]
    refresh = extract_refresh_token(normalized) if normalized else None
    remaining = token_seconds_remaining(token) if token else None
    problems = _cookie_problems(cookies, token_found=bool(token), refresh_found=bool(refresh))
    return {
        "cookie_count": len(cookies),
        "cookie_names": list(cookies)[:20],
        "session_cookie_names": session_names[:10],
        "session_cookie_found": bool(session_names),
        "access_token_found": bool(token),
        "access_token_expired": is_token_expired(token) if token else None,
        "access_token_expires_in_seconds": (
            int(remaining) if remaining is not None else None
        ),
        "refresh_token_found": bool(refresh),
        "chunked_families": _chunked_families(cookies),
        "problems": problems,
        # Oturum taşımayan bir set uygulanırsa çalışan oturum ezilebilir.
        "safe_to_apply": bool(session_names) or bool(token),
    }


def _chunked_families(cookies: dict[str, str]) -> dict[str, int]:
    """`ad.0`, `ad.1` … parçalarını aile başına sayar (ör. `arena-auth-prod-v1`: 2)."""
    families: dict[str, int] = {}
    for name in cookies:
        match = re.match(r"^(?P<base>.+)\.(?P<index>\d+)$", name)
        if match:
            base = match.group("base")
            families[base] = families.get(base, 0) + 1
    return families


def _cookie_problems(
    cookies: dict[str, str], *, token_found: bool, refresh_found: bool
) -> list[str]:
    """Kullanıcıya söylenecek somut eksikler (gizli değer içermez).

    Telefonda tek şans olduğu için "uygulandı" demek yetmiyor: parça eksikse,
    base64 bozuksa ya da token okunamıyorsa bunu açıkça yazmak gerekiyor.
    """
    problems: list[str] = []
    families = _chunked_families(cookies)
    for base, count in sorted(families.items()):
        indices = sorted(
            int(m.group("index"))
            for name in cookies
            if (m := re.match(r"^(?P<base>.+)\.(?P<index>\d+)$", name))
            and m.group("base") == base
        )
        expected = list(range(len(indices)))
        if indices != expected:
            missing = [str(i) for i in expected if i not in indices]
            problems.append(
                f"`{base}` parçaları eksik: {', '.join(missing)} numaralı parça "
                "gelmedi. Çerezi yeniden, TÜM parçalarıyla kopyalayın."
            )
        merged = "".join(cookies.get(f"{base}.{i}", "") for i in range(count))
        if len(merged) > 64 and not _decodes_to_json(merged):
            problems.append(
                f"`{base}` parçalarının birleşimi çözülemedi (base64 bozuk): "
                "kopyalama sırasında karakter kaybolmuş olabilir. JSON dışa "
                "aktarımı yerine 'Header String' deneyin ya da tekrar kopyalayın."
            )
    if not token_found and families:
        problems.append(
            "Parçalı oturum çerezi var ama içinden erişim token'ı okunamadı; "
            "parçalar eksik/bozuk olabilir."
        )
    elif token_found and not refresh_found:
        problems.append(
            "Erişim token'ı okundu ama refresh token yok: token ~1 saatte ölür, "
            "elle tazelemek gerekecek."
        )
    return problems


def _decodes_to_json(value: str) -> bool:
    """`base64-…` gövdesini çözmeyi dener; JSON'a dönüşüyor mu?"""
    body = value.strip()
    for prefix in ("base64-", "base64_", "b64-"):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    body = body.replace("-", "+").replace("_", "/")
    body += "=" * (-len(body) % 4)
    try:
        decoded = base64.b64decode(body, validate=False).decode("utf-8", "ignore")
    except (ValueError, binascii.Error):
        return False
    stripped = decoded.strip()
    return stripped.startswith(("{", "["))


def apply_cookie_to_settings(settings: Settings, raw: str) -> list[str]:
    """Ham `Cookie` başlığını doğrulayıp çalışan ayarlara uygular.

    Mobilde cURL üretmek zor olduğu için kısa yol: yalnızca çerez satırı
    gönderilir. Oturum çerezi gibi görünmeyen metin reddedilir ki yanlışlıkla
    çalışan oturum silinmesin. Eklentinin JSON dışa aktarımı da kabul edilir.
    """
    text = cookie_text_from_export(raw).strip().strip("'\"")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    cookies = parse_cookie_header(text)
    if not cookies:
        raise ValueError(
            "Çerez başlığı ayrıştırılamadı. Biçim: 'ad=değer; ad2=değer2'."
        )
    normalized = cookie_header_from(
        [{"name": name, "value": value} for name, value in cookies.items()]
    )
    token = extract_access_token(normalized)
    has_session_name = any(
        hint in name.lower()
        for name in cookies
        for hint in _SESSION_COOKIE_HINTS
    )
    if not token and not has_session_name:
        raise ValueError(
            "Bu metin bir oturum çerezi gibi görünmüyor (access_token ya da "
            "'auth/session/token' adlı çerez yok)."
        )
    changed: list[str] = []
    if settings.upstream_cookie != normalized:
        settings.upstream_cookie = normalized
        changed.append("upstream_cookie")
    # Bearer token'ı da hizala: `UPSTREAM_ACCESS_TOKEN` doluysa
    # `build_authorization` onu çerezden **önce** tercih eder. Bu yüzden yalnızca
    # çerez güncellendiğinde eski token header'da gönderilmeye devam ediyor ve
    # taze oturum yok sayılıyordu (telefondan /cookie akışının sessiz hatası).
    if token and settings.upstream_access_token != token:
        settings.upstream_access_token = token
        changed.append("upstream_access_token")
    refresh = extract_refresh_token(normalized)
    if refresh and settings.upstream_refresh_token != refresh:
        settings.upstream_refresh_token = refresh
        changed.append("upstream_refresh_token")
    if changed:
        save_runtime_state(settings, source="cookie_import")
    return changed


def _has_fresher_token(
    settings: Settings, captured_cookie: str, *, login_redirect: bool = False
) -> bool:
    """Yakalanan çerez mevcut ayarlardakinden daha iyi/taze bir token taşıyor mu?"""
    if login_redirect or not captured_cookie.strip():
        return False
    captured_token = extract_access_token(
        captured_cookie, cookie_names=settings.upstream_token_cookie_names
    )
    captured_refresh = extract_refresh_token(
        captured_cookie, cookie_names=settings.upstream_token_cookie_names
    )
    current_token = extract_access_token(
        settings.upstream_cookie or "",
        cookie_names=settings.upstream_token_cookie_names,
    ) or (settings.upstream_access_token or "").strip()
    current_refresh = extract_refresh_token(
        settings.upstream_cookie or "",
        cookie_names=settings.upstream_token_cookie_names,
    ) or (settings.upstream_refresh_token or "").strip()

    if captured_token and not is_token_expired(captured_token):
        if not current_token or is_token_expired(current_token):
            return True
        cap_exp = token_expiry(captured_token)
        cur_exp = token_expiry(current_token)
        if cap_exp is not None and cur_exp is not None and cap_exp > cur_exp:
            return True

    return bool(
        captured_refresh
        and not current_refresh
        and (not captured_token or not is_token_expired(captured_token))
    )


def apply_capture_to_settings(
    settings: Settings, result: CaptureResult, *, force: bool = False
) -> list[str]:
    """Yakalanan oturumu çalışan ayarlara uygular (değerler loglanmaz).

    **Oturum yakalanamadıysa ve daha taze bir token üretilmediyse cookie
    değiştirilmez**: anonim çerez setinin çalışan oturumun üzerine yazması,
    sorunu büyütmekten başka işe yaramaz. Ancak sayfa daha taze bir token
    (`fresher_token_found`) ürettiyse o da uygulanır. Sürüm damgası ve referer
    yolu ise sayfadan bağımsız olarak doğru bilgidir; onlar yine uygulanır.
    """
    changed: list[str] = []
    fresher = result.fresher_token_found or _has_fresher_token(
        settings, result.cookies, login_redirect=result.login_redirect
    )
    if (
        result.has_cookie
        and (result.cookie_application_safe or force or fresher)
        and result.cookies != settings.upstream_cookie
    ):
        settings.upstream_cookie = result.cookies
        changed.append("upstream_cookie")
        # Çerezdeki token header'dan önce tercih edilmesin diye onu da eşitle.
        token = extract_access_token(result.cookies)
        if token and settings.upstream_access_token != token:
            settings.upstream_access_token = token
            changed.append("upstream_access_token")
        refresh = extract_refresh_token(result.cookies)
        if refresh and settings.upstream_refresh_token != refresh:
            settings.upstream_refresh_token = refresh
            changed.append("upstream_refresh_token")
    if result.deployment_headers:
        headers = settings.parsed_extra_headers()
        updated = False
        for name, value in result.deployment_headers.items():
            if headers.get(name) != value:
                headers[name] = value
                updated = True
        if updated:
            settings.upstream_extra_headers = format_extra_headers(headers)
            changed.append("upstream_extra_headers")
    if result.cx:
        headers = settings.parsed_extra_headers()
        if headers.get("cx") != result.cx:
            headers["cx"] = result.cx
            settings.upstream_extra_headers = format_extra_headers(headers)
            changed.append("upstream_cx")
    if (
        result.stream_path
        and looks_like_stream_path(result.stream_path)
        and settings.upstream_stream_path != result.stream_path
    ):
        settings.upstream_stream_path = result.stream_path
        changed.append("upstream_stream_path")
    if result.referer_path and settings.upstream_referer_path != result.referer_path:
        settings.upstream_referer_path = result.referer_path
        changed.append("upstream_referer_path")
    if (
        result.refresh_api_key
        and settings.upstream_refresh_api_key != result.refresh_api_key
    ):
        settings.upstream_refresh_api_key = result.refresh_api_key
        changed.append("upstream_refresh_api_key")
    if changed:
        save_runtime_state(settings, source="session_capture")
    return changed
