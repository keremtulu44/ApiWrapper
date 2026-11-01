"""Opsiyonel, servis içi sentetik completion canlı tutma denemesi."""

from __future__ import annotations

import asyncio

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_KEEPALIVE_PROMPT = "Reply only with OK."
_REQUEST_TIMEOUT = 180.0


async def run_upstream_keepalive(settings: Settings) -> None:
    """Belirli aralıklarla kısa bir non-stream completion gönder.

    İlk istek aralık dolmadan gönderilmez. Hata bir sonraki aralığı engellemez;
    bu task yalnızca deney/ölçüm içindir ve sabit token TTL'sini yenileme garantisi
    vermez. Render süreci uyuduğunda task da durur.
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
    logger.info("upstream_keepalive_started", interval_seconds=interval)

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        trust_env=False,
    ) as client:
        while True:
            await asyncio.sleep(interval)
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
                logger.warning(
                    "upstream_keepalive_failed",
                    error_type=type(exc).__name__,
                )
                continue

            if response.status_code == 200:
                logger.info("upstream_keepalive_completed")
            else:
                logger.warning(
                    "upstream_keepalive_failed",
                    status=response.status_code,
                )
