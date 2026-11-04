"""Arka planda erişim token'ını tazeleyen döngü ve süre bitimi uyarısı.

Erişim token'ı ~1 saat yaşıyor; süre dolduğunda her istek 401 döner ve servis
sabaha kadar ölü kalır. Bu döngü dakikada bir bakar, yalnız **süresi yaklaşmışsa**
yenileme isteği atar; başarılı olursa hem `Settings` hem `AccountPool` hem de
`UpstreamClient` çerez kavanozlarını (cookie jar) tazeler. Başarısız olursa
mevcut token'a dokunmaz ve token bitmesine ≤10 dk kaldığında **tek seferlik**
(spam olmayan) uyarı gönderir.

`/v1/admin/upstream/refresh` ucu da aynı fonksiyonu elle tetikler.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import extract_access_token, token_seconds_remaining
from app.upstream.refresh import (
    configured_refresh_slots,
    needs_refresh,
    refresh_upstream_token,
)

logger = get_logger(__name__)

#: Son deneme/başarı zamanı (Telegram `/tani` ve `/ping` için).
_LAST_ATTEMPT: float | None = None
_LAST_SUCCESS: float | None = None
_LAST_ERROR: str = ""
#: Aynı token için tekrar tekrar "10 dk kaldı" uyarısı atmamak için özet.
_WARNED_TOKEN_HASH: str = ""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def record_refresh(*, ok: bool, detail: str = "") -> None:
    """Yenileme sonucunu süreç içinde saklar (yanıtlarda gizli değer yok)."""
    global _LAST_ATTEMPT, _LAST_SUCCESS, _LAST_ERROR, _WARNED_TOKEN_HASH
    _LAST_ATTEMPT = time.time()
    if ok:
        _LAST_SUCCESS = _LAST_ATTEMPT
        _LAST_ERROR = ""
        _WARNED_TOKEN_HASH = ""
    else:
        _LAST_ERROR = detail


def refresh_state() -> dict[str, object]:
    """Son yenileme durumu (zaman damgaları + son hata cümlesi)."""
    return {
        "last_attempt_at": _LAST_ATTEMPT,
        "last_success_at": _LAST_SUCCESS,
        "last_error": _LAST_ERROR,
    }


def reset_refresh_state() -> None:
    """Testlerde durumu sıfırlar."""
    global _LAST_ATTEMPT, _LAST_SUCCESS, _LAST_ERROR, _WARNED_TOKEN_HASH
    _LAST_ATTEMPT = _LAST_SUCCESS = None
    _LAST_ERROR = ""
    _WARNED_TOKEN_HASH = ""


def describe_refresh(now: float | None = None) -> str:
    """İnsan okunur tek satır: `✓ 12 dk önce yenilendi` / `⚠ ...`."""
    current = time.time() if now is None else now
    if _LAST_SUCCESS is not None:
        age = max(0.0, current - _LAST_SUCCESS)
        if age < 90:
            return "Token yenilendi (az önce)"
        if age < 3600:
            return f"Token {int(age // 60)} dk önce yenilendi"
        return f"Token {int(age // 3600)} sa önce yenilendi"
    if _LAST_ATTEMPT is not None and _LAST_ERROR:
        return "Token yenilenemedi: " + _LAST_ERROR
    return "Token yenileme bu süreçte henüz çalışmadı"


async def maybe_warn_expiring_token(
    settings: Settings,
    alerter: Any | None = None,
    *,
    reason: str = "",
) -> str | None:
    """Token'ın bitmesine ≤leeway (vars. 10 dk) kaldıysa **tek bir** uyarı üretir.

    Otomatik yenileme başarılı olduğunda bu fonksiyon çağrılmaz; yalnız refresh
    token yoksa veya yenileme denemesi başarısız olduysa çalışır. Aynı token
    için ikinci kez bildirim gönderilmez (spam koruması).
    """
    global _WARNED_TOKEN_HASH
    token = extract_access_token(
        settings.upstream_cookie or "",
        cookie_names=settings.upstream_token_cookie_names,
    ) or (settings.upstream_access_token or "").strip()
    if not token:
        return None

    remaining = token_seconds_remaining(token)
    if remaining is None or remaining > settings.upstream_refresh_leeway_seconds:
        return None

    digest = _token_hash(token)
    if digest == _WARNED_TOKEN_HASH:
        return None
    _WARNED_TOKEN_HASH = digest

    if remaining <= 0:
        time_part = "süresi doldu"
    else:
        minutes = max(1, int(round(remaining / 60)))
        time_part = f"bitmesine ≈{minutes} dk kaldı"

    why = reason or (
        "otomatik yenileme kapalı"
        if not settings.upstream_refresh_enabled
        else "çerezde refresh_token yok"
    )
    message = (
        f"⚠️ ApiWrapper — erişim token'ının {time_part} ve otomatik yenilenemedi "
        f"({why}).\n"
        "Yapılacak: Oturum 401'e düşmeden önce /cookie veya /yenile ile tazeleyin."
    )
    logger.warning(
        "access_token_expiring_soon",
        expires_in_seconds=int(remaining),
        reason=why,
    )
    if alerter is not None and getattr(alerter, "enabled", False):
        await alerter.send(message)
    return message


async def _propagate_credentials(
    settings: Settings,
    *,
    accounts: Any | None = None,
    upstream: Any | None = None,
) -> None:
    """Yenilenen kimliği `AccountPool` ve `UpstreamClient` jar'larına aktarır."""
    if accounts is not None:
        accounts.reload_credentials(settings)
    if upstream is not None:
        await upstream.reset_account_clients()


async def refresh_if_needed(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    accounts: Any | None = None,
    upstream: Any | None = None,
    alerter: Any | None = None,
) -> dict[str, object] | None:
    """Süre yaklaşmışsa yeniler; aksi hâlde `None` döner (istek atılmaz)."""
    if not settings.upstream_refresh_enabled:
        await maybe_warn_expiring_token(settings, alerter)
        return None

    slots = configured_refresh_slots(settings)
    due_slots = [slot for slot in slots if needs_refresh(settings, slot=slot)]
    if not due_slots:
        # Refresh token yoksa bile erişim token'ı bitmek üzereyse tek uyarı ver.
        if 1 not in slots:
            await maybe_warn_expiring_token(settings, alerter)
        return None

    primary_result: dict[str, object] | None = None
    any_refreshed = False

    for slot in due_slots:
        outcome = await refresh_upstream_token(settings, client, slot=slot)
        if slot == 1 or primary_result is None:
            record_refresh(ok=outcome.ok, detail=outcome.detail)
            primary_result = {
                "ok": outcome.ok,
                "detail": outcome.detail,
                "expires_in": outcome.expires_in,
            }
        if outcome.ok:
            any_refreshed = True
        elif slot == 1:
            await maybe_warn_expiring_token(
                settings, alerter, reason=outcome.detail
            )

    if any_refreshed:
        await _propagate_credentials(settings, accounts=accounts, upstream=upstream)

    return primary_result


async def run_token_refresher(
    settings: Settings,
    *,
    interval_seconds: float | None = None,
    accounts: Any | None = None,
    upstream: Any | None = None,
    alerter: Any | None = None,
) -> None:
    """Süreç ömrü boyunca çalışan yenileme döngüsü (iptal edilebilir)."""
    interval = interval_seconds or settings.upstream_refresh_check_interval_seconds
    interval = max(15.0, float(interval))
    logger.info("token_refresher_started", interval_seconds=interval)
    async with httpx.AsyncClient() as client:
        while True:
            # İlk kontrolü hemen yap: restart sonrası süresi dolmak üzere olan
            # token için kullanıcıyı gereksiz yere bir aralık bekletme. Token
            # tazeyse `refresh_if_needed` ağ isteği atmadan döner.
            try:
                result = await refresh_if_needed(
                    settings,
                    client,
                    accounts=accounts,
                    upstream=upstream,
                    alerter=alerter,
                )
                if result is not None:
                    logger.info(
                        "token_refresher_cycle",
                        ok=result["ok"],
                        expires_in=result["expires_in"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - savunmacı
                logger.warning("token_refresher_error", error_type=type(exc).__name__)
            # `CancelledError` sleep'te de yutulmaz; uygulama kapanışı
            # bir sonraki aralığı beklemeden tamamlanır.
            await asyncio.sleep(interval)
