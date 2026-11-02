"""Arka planda erişim token'ını tazeleyen döngü.

Erişim token'ı ~1 saat yaşıyor; süre dolduğunda her istek 401 döner ve servis
sabaha kadar ölü kalır. Bu döngü dakikada bir bakar, yalnız **süresi yaklaşmışsa**
yenileme isteği atar; başarısız olursa mevcut token'a dokunmaz ve loglar.

`/v1/admin/upstream/refresh` ucu da aynı fonksiyonu elle tetikler.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.refresh import needs_refresh, refresh_upstream_token

logger = get_logger(__name__)

#: Son deneme/başarı zamanı (Telegram `/tani` ve `/ping` için).
_LAST_ATTEMPT: float | None = None
_LAST_SUCCESS: float | None = None
_LAST_ERROR: str = ""


def record_refresh(*, ok: bool, detail: str = "") -> None:
    """Yenileme sonucunu süreç içinde saklar (yanıtlarda gizli değer yok)."""
    global _LAST_ATTEMPT, _LAST_SUCCESS, _LAST_ERROR
    _LAST_ATTEMPT = time.time()
    if ok:
        _LAST_SUCCESS = _LAST_ATTEMPT
        _LAST_ERROR = ""
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
    global _LAST_ATTEMPT, _LAST_SUCCESS, _LAST_ERROR
    _LAST_ATTEMPT = _LAST_SUCCESS = None
    _LAST_ERROR = ""


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


async def refresh_if_needed(
    settings: Settings, client: httpx.AsyncClient | None = None
) -> dict[str, object] | None:
    """Süre yaklaşmışsa yeniler; aksi hâlde `None` döner (istek atılmaz)."""
    if not settings.upstream_refresh_enabled:
        return None
    if not needs_refresh(settings):
        return None
    outcome = await refresh_upstream_token(settings, client)
    record_refresh(ok=outcome.ok, detail=outcome.detail)
    return {
        "ok": outcome.ok,
        "detail": outcome.detail,
        "expires_in": outcome.expires_in,
    }


async def run_token_refresher(
    settings: Settings, *, interval_seconds: float | None = None
) -> None:
    """Süreç ömrü boyunca çalışan yenileme döngüsü (iptal edilebilir)."""
    interval = interval_seconds or settings.upstream_refresh_check_interval_seconds
    interval = max(15.0, float(interval))
    logger.info("token_refresher_started", interval_seconds=interval)
    async with httpx.AsyncClient() as client:
        while True:
            try:
                result = await refresh_if_needed(settings, client)
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
            # `CancelledError` burada yutulmaz: yutulursa iptal edilen görev
            # sonsuz döner ve uygulama kapanışı kilitlenir.
            await asyncio.sleep(interval)
