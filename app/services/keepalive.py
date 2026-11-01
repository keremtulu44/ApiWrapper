"""Opsiyonel, servis içi sentetik completion ve arıza probe'u.

İki işi var:

1. **Canlılık denemesi:** belirli aralıkla kısa bir non-stream completion atar.
   Upstream oturumunu yenileme garantisi vermez (sabit token TTL'si bizim
   elimizde değil), ama uçtan uca yolu sıcak tutar.
2. **Teşhis / bildirim:** dönen cevabı sınıflandırır (sürüm eski, kota, captcha,
   kimlik, ağ...) ve `TelegramAlerter` üzerinden **spam yapmadan** haber verir.

İlk probe `UPSTREAM_KEEPALIVE_INITIAL_DELAY_SECONDS` (vars. 60 sn) sonra çalışır:
deploy sonrası durumu bir saat bekleyip öğrenmemek için.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.alerting import ProbeResult, TelegramAlerter

logger = get_logger(__name__)

_KEEPALIVE_PROMPT = "Reply only with OK."
_REQUEST_TIMEOUT = 180.0


def _error_code(body: str) -> str:
    """Wrapper'ın OpenAI hata zarfından kodu çıkarır (gövde yoksa boş)."""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code") or error.get("type")
    return str(code)[:100] if code else ""


def classify_response(response: Any, duration_ms: float = 0.0) -> ProbeResult:
    """HTTP cevabını `ProbeResult`'a çevirir.

    Okuma yolu savunmacıdır: testlerde/alternatif istemcilerde `text`/`json`
    bulunmayabilir; bu durumda yalnızca durum kodu değerlendirilir.
    """
    status = getattr(response, "status_code", None)
    try:
        status_code = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_code = None

    if status_code is not None and 200 <= status_code < 300:
        return ProbeResult(ok=True, status_code=status_code, duration_ms=duration_ms)

    body = getattr(response, "text", "") or ""
    return ProbeResult(
        ok=False,
        status_code=status_code,
        code=_error_code(body),
        duration_ms=duration_ms,
    )


async def run_upstream_keepalive(
    settings: Settings, alerter: TelegramAlerter | None = None
) -> None:
    """Belirli aralıklarla kısa bir non-stream completion gönder.

    Hata bir sonraki aralığı engellemez; bu task yalnızca deney/ölçüm içindir ve
    sabit token TTL'sini yenileme garantisi vermez. Render süreci uyuduğunda
    task da durur.
    """
    interval = settings.upstream_keepalive_interval_seconds
    if interval <= 0:
        return

    api_key = next((key.strip() for key in settings.api_keys if key.strip()), "")
    if not api_key and not settings.auth_disabled:
        logger.error("upstream_keepalive_configuration_error", missing=["API_KEYS"])
        return

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": settings.upstream_keepalive_model,
        "messages": [{"role": "user", "content": _KEEPALIVE_PROMPT}],
        "stream": False,
    }
    base_url = f"http://127.0.0.1:{settings.port}"
    initial_delay = max(0.0, settings.upstream_keepalive_initial_delay_seconds)
    logger.info(
        "upstream_keepalive_started",
        interval_seconds=interval,
        initial_delay_seconds=initial_delay,
        alerts=alerter is not None,
    )

    first = True
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        trust_env=False,
    ) as client:
        while True:
            # İlk turda en fazla bir aralık beklenir: interval < initial_delay
            # ise (testler/kısa aralıklar) ilk probe yine de hızlı çalışır.
            delay = min(initial_delay, interval) if first else interval
            first = False
            await asyncio.sleep(delay)

            started = time.monotonic()
            try:
                response = await client.post(
                    "/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # URL'de secret yok; yine de hata metni yerine yalnız türü logla.
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                logger.warning(
                    "upstream_keepalive_failed",
                    error_type=type(exc).__name__,
                    duration_ms=duration_ms,
                )
                result = ProbeResult(
                    ok=False,
                    status_code=None,
                    code="wrapper_unreachable",
                    duration_ms=duration_ms,
                )
            else:
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                result = classify_response(response, duration_ms)
                if result.ok:
                    logger.info("upstream_keepalive_completed", duration_ms=duration_ms)
                else:
                    # Kod, logdan tek bakışta teşhis edilebilsin: "status 409"
                    # yerine "code=upstream_refresh_required".
                    logger.warning(
                        "upstream_keepalive_failed",
                        status=result.status_code,
                        code=result.code or None,
                        duration_ms=duration_ms,
                    )

            if alerter is not None:
                await alerter.notify(result)
