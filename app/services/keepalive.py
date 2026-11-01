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
import random
import time
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.alerting import ProbeResult, TelegramAlerter

logger = get_logger(__name__)

#: Sabit tek cümle ("Reply only with OK.") hem tekrarlayan hem robotik bir desen
#: bırakıyordu. Kısa, doğal, birbirinden farklı istekler sırayla kullanılır.
_KEEPALIVE_PROMPTS: tuple[str, ...] = (
    "Merhaba, tek cümleyle kendini tanıt.",
    "2 + 2 kaç eder? Sadece sonucu yaz.",
    "Bugün günlerden ne? Kısaca cevapla.",
    "Bana bir kelime söyle.",
    "Türkiye'nin başkenti neresi?",
    "Kısaca bir motivasyon cümlesi yaz.",
)
_REQUEST_TIMEOUT = 180.0


def pick_prompt(index: int) -> str:
    """Sırayla farklı bir kısa istem döndürür (sabit desen bırakmamak için)."""
    return _KEEPALIVE_PROMPTS[index % len(_KEEPALIVE_PROMPTS)]


def jittered_delay(base: float, jitter: float) -> float:
    """Aralığa ±sapma ekler; çok küçük aralıklarda (testler) orantılı küçültür."""
    base = max(0.0, base)
    if jitter <= 0 or base <= 0:
        return base
    spread = min(jitter, base * 0.15)
    return max(0.001, base + random.uniform(-spread, spread))


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


async def stream_smoke(
    client: Any, headers: dict[str, str], settings: Settings
) -> ProbeResult:
    """`stream: true` ile akış yolunu bir kez dener.

    Non-stream probe streaming ucundaki bir kırılmayı (SSE üretilmemesi, gövdenin
    boş dönmesi) göremez. Bu kontrol günde bir kez çalışır; cevabın **içeriği**
    değil, akan veri olup olmadığı ölçülür.
    """
    payload = {
        "model": settings.upstream_keepalive_model,
        "messages": [{"role": "user", "content": "Tek kelimeyle cevap ver: merhaba"}],
        "stream": True,
    }
    started = time.monotonic()
    try:
        response = await client.post("/v1/chat/completions", headers=headers, json=payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        duration_ms = round((time.monotonic() - started) * 1000, 2)
        logger.warning(
            "upstream_keepalive_stream_smoke_failed",
            error_type=type(exc).__name__,
            duration_ms=duration_ms,
        )
        return ProbeResult(
            ok=False,
            status_code=None,
            code="wrapper_unreachable",
            duration_ms=duration_ms,
        )

    duration_ms = round((time.monotonic() - started) * 1000, 2)
    classified = classify_response(response, duration_ms)
    if not classified.ok:
        logger.warning(
            "upstream_keepalive_stream_smoke_failed",
            status=classified.status_code,
            code=classified.code or None,
            duration_ms=duration_ms,
        )
        return classified

    text = getattr(response, "text", "") or ""
    events = [line for line in text.splitlines() if line.startswith("data:")]
    finished = "[DONE]" in text or "finish_reason" in text
    if not events or not finished:
        logger.warning(
            "upstream_keepalive_stream_smoke_empty",
            events=len(events),
            finished=finished,
            duration_ms=duration_ms,
        )
        return ProbeResult(
            ok=False,
            status_code=classified.status_code,
            code="stream_empty",
            duration_ms=duration_ms,
        )

    logger.info(
        "upstream_keepalive_stream_smoke_completed",
        events=len(events),
        duration_ms=duration_ms,
    )
    return ProbeResult(ok=True, status_code=classified.status_code, duration_ms=duration_ms)


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
    base_url = f"http://127.0.0.1:{settings.port}"
    initial_delay = max(0.0, settings.upstream_keepalive_initial_delay_seconds)
    jitter = max(0.0, settings.upstream_keepalive_jitter_seconds)
    smoke_interval = max(0.0, settings.upstream_keepalive_stream_smoke_interval_seconds)
    logger.info(
        "upstream_keepalive_started",
        interval_seconds=interval,
        initial_delay_seconds=initial_delay,
        jitter_seconds=jitter,
        stream_smoke_interval_seconds=smoke_interval,
        alerts=alerter is not None,
    )

    first = True
    prompt_index = 0
    # İlk stream kontrolü bir sonraki güne planlanır: deploy anında upstream'i
    # gereksiz yere dürtmeyelim.
    next_smoke_at = time.monotonic() + smoke_interval if smoke_interval > 0 else None
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
            await asyncio.sleep(jittered_delay(delay, jitter))

            started = time.monotonic()
            use_stream = next_smoke_at is not None and time.monotonic() >= next_smoke_at
            payload = {
                "model": settings.upstream_keepalive_model,
                "messages": [
                    {"role": "user", "content": pick_prompt(prompt_index)}
                ],
                "stream": False,
            }
            prompt_index += 1
            try:
                if use_stream:
                    # Akış yolu kontrolü kendi hata/probe dönüşünü üretir.
                    result = await stream_smoke(client, headers, settings)
                    next_smoke_at = time.monotonic() + smoke_interval
                    if alerter is not None:
                        await alerter.notify(result)
                    continue
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
