"""Opsiyonel, servis içi sentetik completion ve arıza probe'u.

İki işi var:

1. **Canlılık denemesi:** belirli aralıkla kısa bir non-stream completion atar.
   Upstream oturumunu yenileme garantisi vermez (sabit token TTL'si bizim
   elimizde değil), ama uçtan uca yolu sıcak tutar.
2. **Teşhis / bildirim:** dönen cevabı sınıflandırır (sürüm eski, kota, captcha,
   kimlik, ağ...) ve `TelegramAlerter` üzerinden **spam yapmadan** haber verir.

İlk probe `UPSTREAM_KEEPALIVE_INITIAL_DELAY_SECONDS` (vars. 60 sn) sonra çalışır:
deploy sonrası durumu bir saat bekleyip öğrenmemek için.

Probe da diğer istekler gibi hesabının sırasından geçer (`RequestQueue`). Şu
kurallar bunu güvenli kılar:

* **Boştayken at:** son aralıkta gerçek bir istek başarıyla cevaplandıysa oturum
  zaten tazelenmiştir; probe o istekten bir aralık sonraya ertelenir
  (`UPSTREAM_KEEPALIVE_SKIP_WHEN_ACTIVE`). Böylece probe kotayı boşuna harcamaz
  ve "son başarılı istekten bu yana en çok bir aralık" kuralı korunur.
* **Hesap başına:** birden çok hesap varken (`UPSTREAM_CURL_2`) her hesabın
  kendi probe döngüsü vardır. Probe, hesabına sabitlenir (`x-apiwrapper-account`)
  ve "boşta" olup olmadığına o hesabın **kendi** son başarılı isteğine bakarak
  karar verir. Arıza verip devreden çıkan hesap, kendi probe'u başarılı olunca
  yeniden iş almaya başlar.
* **Sıra doluyken bekle:** bir istek işlenirken probe sıraya girip kendi
  180 sn'lik süresini doldurmasın diye sıra boşalana dek ertelenir. Yine de
  araya istek girerse probe sırada en çok 60 sn bekler
  (`KEEPALIVE_MAX_QUEUE_WAIT_SECONDS`); sıra yüzünden cevapsız kalan ya da
  reddedilen probe (`queue_timeout`, `queue_full`) arıza sayılmaz — upstream'e
  hiç gidilmemiştir, bildirim de bekletme de üretilmez — ve tam aralık
  beklenmeden, yarım dakika sonra yeniden denenir.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.alerting import ProbeResult, TelegramAlerter
from app.services.lanes import ACCOUNT_HEADER
from app.services.request_queue import SOURCE_HEADER, RequestQueue
from app.services.upstream_health import UpstreamHealth

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
#: Sıra doluyken probe'un yeniden bakma aralığı ve en uzun bekleyişi.
_BUSY_POLL_SECONDS = 5.0
_BUSY_MAX_WAIT_SECONDS = 600.0
#: Wrapper'ın kendi sırasından dönen ret kodları: upstream hakkında bir şey söylemez.
_QUEUE_CODES = frozenset({"queue_full", "queue_timeout"})
#: Sıra yüzünden atlanan probe bu kadar sonra yeniden denenir (tam aralık beklenmez;
#: yoksa son başarılı istekten bu yana iki aralığa yakın sessizlik oluşabilirdi).
_RETRY_AFTER_SKIP_SECONDS = 30.0


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


def transport_failure_code(exc: BaseException) -> str:
    """Loopback isteği cevapsız kaldığında arıza kodunu seçer.

    Zaman aşımı "wrapper ayakta ama istek bitmedi" demektir (upstream cevap
    vermiyor ya da tekrar denemelerde bekliyor) → `upstream_timeout`. Yalnız
    bağlantının hiç kurulamaması `wrapper_unreachable`'dır. İkisini tek etikette
    toplamak 18 Kasım'da yanlış teşhise yol açtı (docs/LESSONS §16).
    """
    if isinstance(exc, httpx.TimeoutException) and not isinstance(exc, httpx.ConnectTimeout):
        return "upstream_timeout"
    return "wrapper_unreachable"


async def wait_until_due(
    delay: float,
    *,
    interval: float,
    jitter: float,
    health: UpstreamHealth | None = None,
    queue: RequestQueue | None = None,
    skip_when_active: bool = True,
) -> None:
    """Probe zamanını bekler; gerçek trafik probe'u gereksiz kılıyorsa erteler."""
    mark = time.monotonic()
    await asyncio.sleep(delay)
    busy_deadline = time.monotonic() + _BUSY_MAX_WAIT_SECONDS
    while True:
        if queue is not None and queue.busy and time.monotonic() < busy_deadline:
            # Bir istek işleniyor ya da bekliyor: probe sıraya girip kendi süresini
            # doldurmasın. O isteğin sonucu oturumun durumunu zaten gösterecek.
            await asyncio.sleep(_BUSY_POLL_SECONDS)
            continue
        if health is None or not skip_when_active:
            return
        last = health.last_success_at
        if last is None or last <= mark:
            return
        # Bu bekleyiş sırasında gerçek bir istek başarıyla cevaplandı: oturum
        # taze. Probe'u o istekten bir aralık sonraya al.
        mark = last
        remaining = last + jittered_delay(interval, jitter) - time.monotonic()
        if remaining <= 0:
            return
        logger.info(
            "upstream_keepalive_deferred",
            reason="recent_traffic",
            seconds=round(remaining, 1),
        )
        await asyncio.sleep(remaining)


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


def _stream_error_code(events: list[str]) -> str | None:
    """Akış gövdesindeki (`data: {"error": ...}`) hata kodunu döndürür; yoksa ``None``."""
    for line in events:
        raw = line[5:].strip()
        if not raw.startswith("{") or '"error"' not in raw:
            continue
        code = _error_code(raw)
        if code:
            return code
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("error"):
            return "upstream_failure"
    return None


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
            code=transport_failure_code(exc),
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
    # Akışlı uçta hata HTTP 200 içinde, gövdede bildirilir. Onu "veri aktı" diye
    # başarı saymak arıza sürerken "yeniden çalışıyor" mesajı üretirdi.
    failure = _stream_error_code(events)
    if failure is not None:
        logger.warning(
            "upstream_keepalive_stream_smoke_failed",
            status=classified.status_code,
            code=failure,
            duration_ms=duration_ms,
        )
        return ProbeResult(
            ok=False,
            status_code=classified.status_code,
            code=failure,
            duration_ms=duration_ms,
        )
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
    settings: Settings,
    alerter: TelegramAlerter | None = None,
    *,
    health: UpstreamHealth | None = None,
    queue: RequestQueue | None = None,
    account_slot: int | None = None,
    scope: str = "",
    start_delay: float = 0.0,
    notes: Callable[[ProbeResult], tuple[str, ...]] | None = None,
) -> None:
    """Belirli aralıklarla kısa bir non-stream completion gönder.

    Hata bir sonraki aralığı engellemez; bu task yalnızca deney/ölçüm içindir ve
    sabit token TTL'sini yenileme garantisi vermez. Render süreci uyuduğunda
    task da durur.

    ``health`` ve ``queue`` verilirse probe yalnız upstream boştayken atılır
    (bkz. modül açıklaması) ve cevapsız kalan probe sağlık kaydına işlenir.

    Çok hesaplı kurulumda her hesap için ayrı bir döngü çalışır: ``account_slot``
    probe'u o hesaba sabitler, ``health``/``queue`` o hesabın kaydı ve sırasıdır,
    ``scope`` bildirimlerde hesabın adıdır. ``start_delay`` ilk probe'ları
    hesaplar arasında kaydırır (iki hesap aynı saniyede yoklanmasın). ``notes``
    başarısız probe'un bildirimine eklenecek satırları üretir (öbür hesapların
    durumu, deploy notu).
    """
    interval = settings.upstream_keepalive_interval_seconds
    if interval <= 0:
        return

    api_key = next((key.strip() for key in settings.api_keys if key.strip()), "")
    if not api_key and not settings.auth_disabled:
        logger.error("upstream_keepalive_configuration_error", missing=["API_KEYS"])
        return

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers[SOURCE_HEADER] = "keepalive"
    if account_slot is not None:
        headers[ACCOUNT_HEADER] = str(account_slot)
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
        skip_when_active=settings.upstream_keepalive_skip_when_active and health is not None,
        account=scope or None,
    )

    first = True
    retry_soon = False
    # Hesaplar aynı istem sırasını aynı adımdan izlemesin (iki hesapta aynı
    # cümlelerin aynı sırayla görünmesi ortak bir iz olurdu).
    prompt_index = account_slot or 0
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
            if first:
                delay = min(initial_delay, interval) + max(0.0, start_delay)
            elif retry_soon:
                # Önceki probe sıra yüzünden atlandı: tam aralığı bekleme.
                delay = min(_RETRY_AFTER_SKIP_SECONDS, interval)
            else:
                delay = interval
            first = False
            retry_soon = False
            await wait_until_due(
                jittered_delay(delay, jitter),
                interval=interval,
                jitter=jitter,
                health=health,
                queue=queue,
                skip_when_active=settings.upstream_keepalive_skip_when_active,
            )

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
                    result.scope = scope
                    next_smoke_at = time.monotonic() + smoke_interval
                    if _never_served(queue, result):
                        retry_soon = True
                        continue
                    _report_health(health, result)
                    if alerter is not None:
                        await alerter.notify(_with_notes(result, notes))
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
                    code=transport_failure_code(exc),
                    duration_ms=duration_ms,
                    account=scope or None,
                )
                result = ProbeResult(
                    ok=False,
                    status_code=None,
                    code=transport_failure_code(exc),
                    duration_ms=duration_ms,
                )
            else:
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                result = classify_response(response, duration_ms)
                if result.ok:
                    logger.info(
                        "upstream_keepalive_completed",
                        duration_ms=duration_ms,
                        account=scope or None,
                    )
                else:
                    # Kod, logdan tek bakışta teşhis edilebilsin: "status 409"
                    # yerine "code=upstream_refresh_required".
                    logger.warning(
                        "upstream_keepalive_failed",
                        status=result.status_code,
                        code=result.code or None,
                        duration_ms=duration_ms,
                        account=scope or None,
                    )

            result.scope = scope
            if _never_served(queue, result):
                retry_soon = True
                continue
            _report_health(health, result)
            if alerter is not None:
                await alerter.notify(_with_notes(result, notes))


def _with_notes(
    result: ProbeResult, notes: Callable[[ProbeResult], tuple[str, ...]] | None
) -> ProbeResult:
    """Başarısız probe sonucuna çağıranın ek satırlarını ekler (varsa)."""
    if notes is not None and not result.ok:
        result.details = (*result.details, *notes(result))
    return result


def _never_served(queue: RequestQueue | None, result: ProbeResult) -> bool:
    """Probe cevapsız kaldı ama upstream'e hiç gitmedi mi (sırada bekliyordu)?

    Öndeki gerçek istek uzun sürdüyse probe kendi süresini sırada doldurabilir.
    Bu upstream hakkında hiçbir şey söylemez; arıza diye bildirmek hem yanlış
    alarm üretir hem `/v1/ask` isteklerini boş yere "problem"e düşürürdü.
    """
    if result.ok:
        return False
    if result.code in _QUEUE_CODES:
        # Wrapper'ın kendi sırası reddetti (dolu ya da bekleme süresi doldu).
        reason = "queue_rejected"
    elif queue is not None and result.status_code is None and queue.untouched("keepalive"):
        reason = "queued_behind_traffic"
    else:
        return False
    logger.info(
        "upstream_keepalive_skipped",
        reason=reason,
        code=result.code or None,
        duration_ms=result.duration_ms,
        hint="Probe sırada kaldı; upstream'e gidilmedi, arıza sayılmadı. Kısa süre sonra "
        "yeniden denenecek.",
    )
    return True


def _report_health(health: UpstreamHealth | None, result: ProbeResult) -> None:
    """Cevapsız kalan probe'u sağlık kaydına işler.

    HTTP cevabı dönen probe'un sonucunu tamamlama servisi zaten bildirmiştir.
    Zaman aşımı/bağlantı hatasında ise servis tarafındaki istek iptal edilir ve
    kayıt düşülmez; `/v1/ask` isteklerinin de aynı duvara tek tek çarpmaması
    için arıza buradan bildirilir.
    """
    if health is not None and not result.ok and result.status_code is None:
        health.record_failure(result.key)
