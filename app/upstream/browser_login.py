"""Kalıcı giriş yapmış tarayıcıyla taze oturum çerezi üretir.

Neden
-----
Hedefin erişim token'ı ~20 dakikada ölüyor ve yenileme ucu backend'de
`apikey` istediği için sunucu kendi yenileyemiyor. Ama **sitede açık bir
tarayıcı oturumu kendi token'ını kendiliğinden yeniliyor**: sayfa her
etkileşimde taze `access_token` üretiyor. Bu modül o oturumu kalıcı tutar ve
taze çerezi bizim `Settings`'imize aktarır.

Nasıl
-----
1. Playwright Chromium açılır (tek örnek, süreç ömrü boyunca yaşar).
2. İlk açılışta siteye **mail + şifre** ile giriş yapılır (2FA/captcha yok).
3. Sıcak sayfa açık tutulur. Döngü her ``interval`` saniyede bir:
   * çerezleri okur, erişim token'ı eskimeye başladıysa sayfayı dürtüp
     (isteğe bağlı yenileme) taze token üretir,
   * taze çerezi `Settings`'e ve kalıcı duruma yazar.
4. Çerez ölürse (401) otomatik yeniden giriş yapılır.

Güvenlik
--------
* Şifre yalnız ``UPSTREAM_LOGIN_PASSWORD`` env'inden okunur; loglanmaz,
  diske yazılmaz, yanıtlarda dönmez.
* Çerez/token değerleri loglanmaz; yalnız "döndü/dönmedi" ve kalan saniye.
* Kalıcı profil (``UPSTREAM_BROWSER_PROFILE_DIR``) yalnız çerez saklar; şifre
  oraya yazılmaz.

Bu modül **varsayılan olarak kapalıdır**: ``UPSTREAM_LOGIN_ENABLED=true``
değilse hiçbir tarayıcı açılmaz.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import (
    extract_access_token,
    extract_refresh_token,
    is_token_expired,
    token_seconds_remaining,
)

#: Çerezde oturum çerezi aramak için kullanılan ipuçları.
SESSION_COOKIE_HINTS: tuple[str, ...] = ("auth", "session", "token", "sb-")


def cookie_header_from(items: list[dict[str, Any]]) -> str:
    """Playwright çerez listesini `name=value; name2=value2` başlığına çevirir."""
    parts: list[str] = []
    for item in items or []:
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if name:
            parts.append(f"{name}={value}")
    return "; ".join(parts)

logger = get_logger(__name__)

#: Playwright'ın kaynak tüketimini 512 MB sınırında tutan argümanlar.
#: `browser.py` ile aynı: tek süreç, GPU yok, JS heap sınırlı.
_CHROMIUM_ARGS: tuple[str, ...] = (
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
)

#: Giriş formunda "e-posta" alanı olarak denenecek seçiciler (sırayla).
_EMAIL_SELECTORS: tuple[str, ...] = (
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input[placeholder*="mail" i]',
    'input[placeholder*="e-posta" i]',
    'input[id*="email" i]',
)

#: "Devam / İleri" düğmesi olarak denenecek seçiciler.
_NEXT_SELECTORS: tuple[str, ...] = (
    'button[type="submit"]',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Devam")',
    'button:has-text("İleri")',
    'button:has-text("Giriş")',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
)

#: "Şifre" alanı olarak denenecek seçiciler.
_PASSWORD_SELECTORS: tuple[str, ...] = (
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="current-password"]',
)


@dataclass(slots=True)
class LoginStatus:
    """Son giriş/tazeleme denemesinin gizli değer içermeyen özeti."""

    ok: bool = False
    reason: str = ""
    at: float = 0.0
    #: Sayfa giriş ekranına düştü mü (oturum ölmüş demektir).
    login_redirect: bool = False
    #: Çerezde oturum çerezi bulundu mu.
    session_cookie: bool = False
    #: Erişim token'ı var mı ve kaç saniye kaldı.
    token_seconds: float | None = None
    #: Bu turda çerez gerçekten değişti mi (gereksiz Render yazımını önler).
    changed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "at": self.at,
            "login_redirect": self.login_redirect,
            "session_cookie": self.session_cookie,
            "token_seconds": (
                None if self.token_seconds is None else round(self.token_seconds)
            ),
            "changed": self.changed,
            "notes": list(self.notes),
        }


def login_configured(settings: Settings) -> bool:
    """Giriş için gereken asgari yapılandırma tam mı?"""
    return bool(
        settings.upstream_login_enabled
        and (settings.upstream_login_email or "").strip()
        and (settings.upstream_login_password or "").strip()
    )


def missing_login_config(settings: Settings) -> list[str]:
    """Eksik env adlarını döndürür (değer döndürmez)."""
    missing: list[str] = []
    if not (settings.upstream_login_email or "").strip():
        missing.append("UPSTREAM_LOGIN_EMAIL")
    if not (settings.upstream_login_password or "").strip():
        missing.append("UPSTREAM_LOGIN_PASSWORD")
    return missing


def _has_session_cookie(cookies: list[dict[str, Any]]) -> bool:
    return any(
        any(hint in str(item.get("name", "")).lower() for hint in SESSION_COOKIE_HINTS)
        for item in cookies or []
    )


class SessionKeeper:
    """Kalıcı giriş yapmış tarayıcıyı yönetir ve taze çerezi ayarlara yazar."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self.status = LoginStatus()
        self.stats: dict[str, Any] = {
            "logins": 0,
            "refreshes": 0,
            "failures": 0,
            "last_error": "",
        }

    # ------------------------------------------------------------- yaşam
    async def startup(self) -> None:
        """Tarayıcıyı hazırlar ve ilk girişi yapar (hata fırlatmaz)."""
        if not login_configured(self.settings):
            return
        try:
            await self._ensure_browser()
            await self.refresh(reason="startup")
        except Exception as exc:  # pragma: no cover - tarayıcı hatası
            self.stats["failures"] += 1
            self.stats["last_error"] = type(exc).__name__
            logger.warning("upstream_login_startup_failed", error_type=type(exc).__name__)

    async def shutdown(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                with contextlib.suppress(Exception):
                    await closer.close()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
        self._page = self._context = self._browser = self._playwright = None

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Playwright kurulu değil: pip install 'apiwrapper[browser]'"
            ) from exc

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=list(_CHROMIUM_ARGS)
        )
        profile_dir = (self.settings.upstream_browser_profile_dir or "").strip()
        locale = (
            self.settings.upstream_accept_language.split(",", 1)[0].strip() or "en-US"
        )
        context_kwargs: dict[str, Any] = {
            "user_agent": self.settings.upstream_user_agent,
            "locale": locale,
        }
        if profile_dir:
            # Kalıcı profil: süreç yeniden başlasa da oturum durur. Render'da
            # disk kalıcı olmadığı için varsayılan kapalı; yerelde işe yarar.
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir, headless=True, args=list(_CHROMIUM_ARGS),
                **context_kwargs,
            )
        else:
            self._context = await self._browser.new_context(**context_kwargs)
        self._page = await self._context.new_page()
        logger.info("upstream_login_browser_ready", persistent=bool(profile_dir))

    # ------------------------------------------------------------- eylem
    async def refresh(self, *, reason: str = "manual") -> LoginStatus:
        """Çerezi okur; gerekiyorsa giriş yapar; taze çerezi ayarlara yazar."""
        async with self._lock:
            if not login_configured(self.settings):
                self.status = LoginStatus(
                    ok=False,
                    reason="yapılandırma eksik: " + ", ".join(missing_login_config(self.settings)),
                    at=time.time(),
                )
                return self.status
            await self._ensure_browser()
            notes: list[str] = []

            page = self._page
            cookies = await self._context.cookies()
            if not _has_session_cookie(cookies):
                notes.append("oturum çerezi yok; giriş denenecek")
                await self._login(page, notes)

            # Girişten sonra tekrar oku.
            cookies = await self._context.cookies()
            cookie_header = cookie_header_from(cookies)
            token = extract_access_token(cookie_header)
            remaining = token_seconds_remaining(token) if token else None

            if token and not is_token_expired(token) and (remaining or 0) > 120:
                if self._apply(cookie_header, token):
                    notes.append("çerez tazelendi")
            else:
                # Token bitmiş ya da yok: önce sayfayı dürt, olmazsa yeniden giriş.
                notes.append("token eskimiş; sayfa dürtülecek")
                await self._nudge(page, notes)
                cookies = await self._context.cookies()
                cookie_header = cookie_header_from(cookies)
                token = extract_access_token(cookie_header)
                remaining = token_seconds_remaining(token) if token else None
                if not token or is_token_expired(token):
                    notes.append("sayfa yetmedi; yeniden giriş")
                    await self._login(page, notes)
                    cookies = await self._context.cookies()
                    cookie_header = cookie_header_from(cookies)
                    token = extract_access_token(cookie_header)
                    remaining = token_seconds_remaining(token) if token else None
                if self._apply(cookie_header, token):
                    notes.append("çerez tazelendi")

            ok = bool(token and not is_token_expired(token))
            if not ok:
                self.stats["failures"] += 1
            self.status = LoginStatus(
                ok=ok,
                reason="" if ok else "taze token alınamadı",
                at=time.time(),
                login_redirect=any("giris" in note for note in notes),
                session_cookie=_has_session_cookie(cookies),
                token_seconds=remaining,
                changed=any("tazelendi" in note for note in notes),
                notes=notes,
            )
            logger.info(
                "upstream_login_refresh",
                reason=reason,
                ok=ok,
                changed=self.status.changed,
                token_seconds=(
                    None if remaining is None else int(remaining)
                ),
                notes=len(notes),
            )
            return self.status

    async def _login(self, page: Any, notes: list[str]) -> None:
        """Siteye mail + şifre ile girer (iki adımlı akış)."""
        email = (self.settings.upstream_login_email or "").strip()
        password = (self.settings.upstream_login_password or "").strip()
        url = self.settings.login_url()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            notes.append(f"giris sayfasi acilamadi: {type(exc).__name__}")
            return
        await page.wait_for_timeout(1500)

        # 1. adım: e-posta
        if not await _fill_first(page, _EMAIL_SELECTORS, email):
            notes.append("e-posta alani bulunamadi")
            return
        await _click_first(page, _NEXT_SELECTORS)
        await page.wait_for_timeout(2000)

        # 2. adım: şifre
        if not await _fill_first(page, _PASSWORD_SELECTORS, password):
            notes.append("sifre alani bulunamadi")
            return
        await _click_first(page, _NEXT_SELECTORS)
        await page.wait_for_timeout(4000)

        final_path = _path_of(str(getattr(page, "url", "") or ""))
        if any(hint in final_path.lower() for hint in ("login", "signin", "auth")):
            notes.append(f"giris sonrasi hala kimlik ekraninda: {final_path}")
        else:
            self.stats["logins"] += 1
            notes.append("giris yapildi")

    async def _nudge(self, page: Any, notes: list[str]) -> None:
        """Sayfayı dürtüp kendi token yenilemesini tetiklemeyi dener."""
        try:
            await page.goto(
                self.settings.referer_url("keepalive"),
                wait_until="domcontentloaded",
                timeout=25000,
            )
            await page.wait_for_timeout(2500)
            notes.append("sayfa durtuldu")
        except Exception as exc:
            notes.append(f"durtme basarisiz: {type(exc).__name__}")

    def _apply(self, cookie_header: str, token: str | None) -> bool:
        """Taze çerezi `Settings`'e yazar; değiştiyse True döner."""
        text = (cookie_header or "").strip()
        if not text:
            return False
        changed = False
        if self.settings.upstream_cookie != text:
            self.settings.upstream_cookie = text
            changed = True
        if token and self.settings.upstream_access_token != token:
            self.settings.upstream_access_token = token
            changed = True
        refresh = extract_refresh_token(text)
        if refresh and self.settings.upstream_refresh_token != refresh:
            self.settings.upstream_refresh_token = refresh
            changed = True
        if changed:
            self.stats["refreshes"] += 1
        return changed


def _path_of(url: str) -> str:
    """URL'den yol kısmını döndürür (alan adı loglanmasın diye)."""
    if not url:
        return ""
    without_scheme = url.split("://", 1)[-1]
    return "/" + (without_scheme.split("/", 1)[1] if "/" in without_scheme else "")


async def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            if not await locator.is_visible():
                continue
            await locator.click(timeout=3000)
            await locator.fill(value, timeout=3000)
            return True
        except Exception:
            continue
    return False


async def _click_first(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not await locator.count():
                continue
            if not await locator.is_visible() or not await locator.is_enabled():
                continue
            await locator.click(timeout=3000)
            return True
        except Exception:
            continue
    # Düğme bulunamazsa Enter ile form gönderilir.
    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def run_session_keeper(
    settings: Settings,
    *,
    keeper: SessionKeeper,
    interval_seconds: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Periyodik tazeleme döngüsü; `stop_event` set edilince çıkar."""
    interval = max(30.0, float(interval_seconds))
    logger.info("upstream_login_loop_started", interval_seconds=interval)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        if stop_event is not None and stop_event.is_set():
            return
        try:
            await keeper.refresh(reason="interval")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - savunmacı
            keeper.stats["failures"] += 1
            keeper.stats["last_error"] = type(exc).__name__
            logger.warning(
                "upstream_login_cycle_failed", error_type=type(exc).__name__
            )
