"""`POST /v1/ask` — otomatik sistemler için "bekle / bitti / problem" ucu.

`/v1/chat/completions` bir insanın ya da OpenAI SDK'sının kullanımına göredir:
cevap gelene dek sessizdir ve arızayı ayrıntılı bir hata koduyla bildirir. Bir
alarm sistemi için ikisi de yanlıştır: sessizlik "çalışıyor mu, asıldı mı"
sorusunu cevapsız bırakır, ayrıntılı hata kodu ise karşı tarafa yorumlayamayacağı
bir yük bindirir. Bu uç sözleşmeyi üç duruma indirir:

``{"status":"wait"}``
    İstek alındı, cevap henüz hazır değil. İlki kabulden
    `ASK_FIRST_WAIT_SECONDS` (30 sn) sonra, sonrakiler
    `ASK_WAIT_INTERVAL_SECONDS` (15 sn) arayla gönderilir.
``{"status":"done","text":"..."}``
    Cevabın **tamamı**, tek parça. Son satırdır.
``{"status":"problem"}``
    Cevap alınamadı; karşı sistem yedeğine geçmelidir. Son satırdır. **Neden
    söylenmez** — ayrıntı Telegram'dan operatöre gider.

Satırlar `text/event-stream` biçiminde (`data: {...}`) aynı bağlantı üzerinden
akar; her satır yalnız ASCII karakter içerir (bkz. `ask_line`). Sözleşmenin
dördüncü kuralı istemcidedir: 45 sn boyunca hiçbir şey gelmezse ya da bağlantı
son satır olmadan kapanırsa bu da "problem" sayılır (wrapper çökse bile yedeğe
geçilir).

Süreler:

* İstek sıraya girer (`RequestQueue`); sıra beklemesi süre sınırından düşmez.
* Sırası gelince **istek başına** `ASK_REQUEST_TIMEOUT_SECONDS` (180 sn) işlem
  süresi vardır; dolarsa "problem".
* Sırada en çok `ASK_MAX_QUEUE_WAIT_SECONDS` (600 sn) beklenir; dolarsa "problem".
* Arıza zaten biliniyorsa (`UpstreamHealth`) istek upstream'e hiç gitmeden,
  beklemeden "problem" alır.
* Birden çok hesap varsa (`UPSTREAM_CURL_2`) istekler hesaplar arasında bölünür
  ve her hesap kendi sırasını işler (`app/services/lanes.py`). Bir hesap arıza
  verirse iş çalışan hesaba gider; "problem" yalnız cevap verebilecek hesap
  kalmadığında söylenir. Sözleşme değişmez: karşı sistem hesapları görmez.

Çağıranın kendi hatası (yanlış anahtar, boş `prompt`, bilinmeyen model) akış
başlamadan düz HTTP 4xx olarak döner; istemci için bu da "problem"dir. Arıza
sayılmaz ama operatöre seyreltilerek bildirilir (`report_rejected`): düzeltilmeyen
bir kurulum hatası her soruyu sessizce yedeğe gönderirdi.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.adapters.openai_to_upstream import validate_request
from app.api.deps import get_completion_service
from app.api.disconnect import wait_for_disconnect
from app.api.streaming import GuardedStreamingResponse
from app.core.config import Settings
from app.core.diagnostics import explain_failure
from app.core.errors import APIWrapperError, InvalidRequestError
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.core.security import client_fingerprint, extract_api_key, verify_api_key
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.services.alerting import (
    ProbeResult,
    TelegramAlerter,
    format_event,
    format_rejected,
    human_duration,
)
from app.services.completion_service import Admission, CompletionService
from app.services.lanes import Lane, LaneUnavailable
from app.services.request_queue import QueueAbandoned
from app.services.upstream_health import Hold
from app.upstream.client import error_excerpt
from app.utils.ids import new_request_id
from app.utils.sse import SSE_HEADERS, sse_comment
from app.utils.tasks import TaskRegistry

logger = get_logger(__name__)


def ask_line(payload: dict[str, Any]) -> str:
    """Sözleşmenin tek bir satırı: ``data: {...}`` + boş satır, yalnız ASCII.

    ASCII dışı her karakter JSON kaçışıyla (``\\uXXXX``) yazılır; istemcinin JSON
    çözücüsü metni birebir geri verir. İki nedeni var:

    * Cevapta U+2028 / U+2029 / U+0085 geçerse `str.splitlines()` ile satır okuyan
      istemciler (httpx `iter_lines`, requests `decode_unicode=True`) ``data:``
      satırını ortasından böler ve JSON çözülemez. Ölçüldü; ASCII çıktıda bu
      karakterler yoktur, satır hiçbir okuyucuda bölünmez.
    * Karakter kodlaması yanlış tahmin edilse bile (ör. `text/*` için Latin-1
      varsayan istemciler) içerik bozulmaz.
    """
    return "data: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n\n"

def report_rejected(request: Request, exc: APIWrapperError) -> None:
    """Çağıranın kendi hatasıyla reddedilen isteği operatöre bildirir (seyreltilmiş).

    Arıza kaydı açmaz, bekletme başlatmaz: upstream'de sorun yoktur. Aynı tür
    saatte en çok bir kez yazılır; aradakiler bir sonraki mesajda sayılır.
    """
    code = exc.code or f"http_{exc.status_code}"
    request_id = getattr(request.state, "request_id", None) or "-"
    logger.warning("ask_rejected", ask_id=request_id, code=code, status=exc.status_code)
    metrics.inc("apiwrapper_ask_total", labels={"status": "rejected", "code": code})
    alerter: TelegramAlerter | None = getattr(request.app.state, "alerter", None)
    tasks: TaskRegistry | None = getattr(request.app.state, "tasks", None)
    if alerter is None or tasks is None:
        return
    text = format_rejected(
        code, exc.status_code, error_excerpt(exc.message), (f"İstek {request_id}",)
    )
    tasks.spawn(alerter.notify_event(f"ask_rejected:{code}", text), name="ask-alert")


async def ask_api_key(request: Request) -> str:
    """API anahtarını doğrular; yanlış/eksik anahtarı operatöre de bildirir.

    `API_KEYS`'e ek olarak yalnız bu uçta geçerli `ASK_API_KEYS` kabul edilir:
    karşı sisteme verilen anahtar sohbet ve yönetim uçlarını çağıramaz.
    """
    settings: Settings = request.app.state.settings
    try:
        identity = verify_api_key(request, settings, extra_keys=settings.ask_api_keys)
    except APIWrapperError as exc:
        report_rejected(request, exc)
        raise
    request.state.api_identity = identity
    request.state.client_fingerprint = client_fingerprint(extract_api_key(request))
    return identity


router = APIRouter(tags=["ask"], dependencies=[Depends(ask_api_key)])

#: Arıza kaydı açmayan, wrapper'ın kendi yük sınırından doğan hata kodları.
_OVERLOAD_CODES = frozenset({"queue_full", "queue_timeout"})


class AskRequest(BaseModel):
    """`POST /v1/ask` gövdesi: ya `prompt` (+ isteğe bağlı `system`) ya `messages`."""

    model_config = ConfigDict(extra="ignore")

    #: Sorulacak metin.
    prompt: str | None = None
    #: İsteğe bağlı sistem talimatı (yalnız `prompt` ile birlikte).
    system: str | None = None
    #: İsteğe bağlı model adı; verilmezse `ASK_DEFAULT_MODEL`, o da boşsa varsayılan.
    model: str | None = None
    #: `prompt` yerine OpenAI biçiminde mesaj listesi.
    messages: list[ChatMessage] | None = None


@dataclass(slots=True)
class AskOutcome:
    """İşin sonucu. Yalnız `status` ve (bitince) `text` istemciye gider."""

    status: str  # "done" | "problem"
    text: str = ""
    model: str = ""
    #: Problemin nedeni — yalnız log ve Telegram içindir, istemciye gönderilmez.
    code: str = ""


def build_chat_request(body: AskRequest, settings: Settings) -> ChatCompletionRequest:
    """`/v1/ask` gövdesini iç tamamlama isteğine çevirir (akışsız, tek parça cevap)."""
    prompt = (body.prompt or "").strip()
    if body.messages and prompt:
        raise InvalidRequestError(
            "Provide either 'prompt' or 'messages', not both.", param="prompt"
        )
    if body.messages:
        messages = list(body.messages)
    elif prompt:
        messages = [ChatMessage(role="user", content=prompt)]
    else:
        raise InvalidRequestError("'prompt' must be a non-empty string.", param="prompt")
    system = (body.system or "").strip()
    if system:
        messages.insert(0, ChatMessage(role="system", content=system))
    model = (body.model or settings.ask_default_model or "").strip()
    return ChatCompletionRequest(model=model, messages=messages, stream=False)


class AskRunner:
    """Tek bir `/v1/ask` isteğinin yaşam döngüsü: sıra, işlem, işaretler, bildirim."""

    def __init__(
        self,
        *,
        request: Request,
        request_id: str,
        chat_request: ChatCompletionRequest,
        client_identity: str | None,
        service: CompletionService,
        settings: Settings,
        alerter: TelegramAlerter | None,
        tasks: TaskRegistry,
    ) -> None:
        self._request = request
        self.request_id = request_id
        self._chat_request = chat_request
        self._client_identity = client_identity
        self._service = service
        self._settings = settings
        self._alerter = alerter
        self._tasks = tasks
        self._accepted = time.monotonic()
        # Sıfır/negatif ayar sonsuz döngü üretmesin diye alt sınır.
        self._first_wait = max(0.01, float(settings.ask_first_wait_seconds))
        self._interval = max(0.01, float(settings.ask_wait_interval_seconds))

    # ------------------------------------------------------------ akış
    async def events(self) -> AsyncIterator[str]:
        """SSE gövdesi: (varsa) `wait` satırları, ardından tek bir son satır."""
        # Açılış yorumu aradaki vekil sunucuların tamponunu boşaltır. Sözleşmenin
        # parçası değildir: istemci `data:` ile başlamayan satırları yok sayar.
        yield sse_comment("ok")

        work = self._tasks.spawn(self._work(), name=f"ask:{self.request_id}")
        watch = asyncio.ensure_future(wait_for_disconnect(self._request))
        next_wait = self._first_wait
        outcome: AskOutcome | None = None
        try:
            while True:
                if work.done():
                    outcome = work.result()
                    break
                if watch.done():
                    break  # istemci beklemekten vazgeçti
                remaining = self._accepted + next_wait - time.monotonic()
                if remaining <= 0:
                    yield ask_line({"status": "wait", "elapsed": self._elapsed(0)})
                    next_wait += self._interval
                    continue
                await asyncio.wait(
                    {work, watch}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
        finally:
            # Buraya iptalle de gelinir (istemci koptu). İptal edilmiş bir kapsamda
            # `await` güvenilir değildir; yalnız eşzamanlı işler yapılır. İş kendi
            # görevinde koşar ve sırayı/denemeyi kendi `finally`'sinde bırakır.
            watch.cancel()
            if not work.done():
                work.cancel()
                logger.info(
                    "ask_abandoned",
                    ask_id=self.request_id,
                    elapsed_ms=round((time.monotonic() - self._accepted) * 1000, 2),
                )
                metrics.inc("apiwrapper_ask_total", labels={"status": "abandoned"})

        if outcome is None:
            return
        if outcome.status == "done":
            yield ask_line(
                {
                    "status": "done",
                    "text": outcome.text,
                    "model": outcome.model,
                    "elapsed": self._elapsed(1),
                    "id": self.request_id,
                }
            )
        else:
            yield ask_line({"status": "problem", "id": self.request_id})

    def _elapsed(self, digits: int) -> float | int:
        value = time.monotonic() - self._accepted
        return round(value, digits) if digits else round(value)

    # --------------------------------------------------------------- iş
    async def _work(self) -> AskOutcome:
        """İsteği işler. İptal dışında istisna fırlatmaz: her sonuç bir `AskOutcome`."""
        settings = self._settings
        service = self._service
        admission = Admission(
            label="ask",
            max_wait=settings.ask_max_queue_wait_seconds,
            deadline=settings.ask_request_timeout_seconds,
            # Arızası bilinen hesaba gidilmez; sırada beklerken hesabın arızası
            # öğrenilirse (öndeki istek başarısız oldu) bekleyenler tek tek
            # denenmez. Cevap verebilecek hesap kalmadıysa hemen "problem".
            respect_hold=True,
        )
        try:
            completion = await service.create_completion(
                self._chat_request,
                client_identity=self._client_identity,
                admission=admission,
            )
        except LaneUnavailable as exc:
            # Arıza biliniyor: upstream'e gitmeden, beklemeden cevapla.
            waited = admission.waited > 0
            return self._held(
                exc.hold,
                reason="drained" if waited else "held",
                lane=exc.lane,
                admission=admission if waited else None,
            )
        except QueueAbandoned:
            lane = service.lane_of(admission)
            current = lane.health.blocked()
            if current is None:  # pragma: no cover - koşulu sağlayan kayıt az önce kalktı
                current = Hold("upstream_failure", 0.0, 0.0)
            return self._held(current, reason="drained", lane=lane, admission=admission)
        except APIWrapperError as exc:
            self._report_reroutes(admission, answered=False)
            return self._failed(exc, admission)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ask_internal_error", ask_id=self.request_id, error=str(exc))
            return self._local_problem("internal_error", 500, admission, reason="internal")

        choice = completion.choices[0] if completion.choices else None
        text = (choice.message.content or "") if choice is not None else ""
        if not text.strip():
            self._report_reroutes(admission, answered=False)
            service.lane_of(admission).health.record_failure("empty_response")
            return self._incident(
                "empty_response", 200, admission, reason="empty", upstream_line=None
            )

        logger.info(
            "ask_done",
            ask_id=self.request_id,
            model=completion.model,
            account=admission.scope or None,
            content_chars=len(text),
            waited_ms=round(admission.waited * 1000, 2),
            duration_ms=round(admission.duration * 1000, 2),
        )
        metrics.inc("apiwrapper_ask_total", labels={"status": "done"})
        self._report_reroutes(admission, answered=True)
        self._notify_recovery(admission.scope)
        return AskOutcome("done", text=text, model=completion.model)

    # ---------------------------------------------------------- sonuçlar
    def _failed(self, exc: APIWrapperError, admission: Admission) -> AskOutcome:
        code = exc.code or f"http_{exc.status_code}"
        if exc.err_type == "invalid_request_error" or code in _OVERLOAD_CODES:
            # İsteğe özgü ya da wrapper'ın kendi yük sınırı: upstream arızası değil.
            reason = "overload" if code in _OVERLOAD_CODES else "rejected"
            return self._local_problem(code, exc.status_code, admission, reason=reason)
        return self._incident(
            code,
            exc.status_code,
            admission,
            reason="failed",
            upstream_line=self._upstream_line(exc, admission),
        )

    def _upstream_line(self, exc: APIWrapperError, admission: Admission) -> str | None:
        """Upstream'in bu istek sırasında verdiği son cevabın özeti (varsa)."""
        cause = exc.__cause__
        status = getattr(cause, "status_code", None)
        body = getattr(cause, "body", "")
        if status is not None:
            excerpt = error_excerpt(str(body)) if body else ""
            return f"Upstream cevabı: HTTP {status}" + (f" · {excerpt}" if excerpt else "")
        if admission.started_at > 0:
            note = self._service.upstream.error_since(admission.started_at, admission.slot)
            if note is not None:
                return f"Upstream'den görülen son hata: {note.describe()}"
        return None

    def _request_line(self, admission: Admission | None) -> str:
        if admission is None or admission.started_at <= 0:
            waited = admission.waited if admission is not None else 0.0
            return f"İstek {self.request_id} · sırada {human_duration(waited)}"
        return (
            f"İstek {self.request_id} · sırada {human_duration(admission.waited)} · "
            f"işlem {human_duration(admission.duration)}"
        )

    def _hold_line(self, lane: Lane) -> str | None:
        """Operatöre "bundan sonra ne olacak" sorusunun cevabı."""
        lanes = self._service.lanes
        if lanes.multi:
            return lanes.outlook(lane)
        hold = lane.health.blocked()
        if hold is None:
            return None
        return (
            f"Bundan sonra: dış isteklere {human_duration(math.ceil(hold.remaining))} boyunca "
            'upstream\'e gidilmeden "problem" denir; sonra tek istek denenir.'
        )

    def _lane_lines(self, lane: Lane, status_code: int | None, code: str) -> tuple[str, ...]:
        """Çok hesaplı kurulumda mesaja eklenen satırlar (hesap durumu, deploy notu)."""
        critical = explain_failure(status_code, code).severity == "critical"
        return self._service.lanes.incident_notes(lane, critical=critical)

    def _incident(
        self,
        code: str,
        status_code: int | None,
        admission: Admission,
        *,
        reason: str,
        upstream_line: str | None,
    ) -> AskOutcome:
        """Upstream kaynaklı arıza: arıza kaydına işlenir (ilkinde mesaj, sonra sayım)."""
        lane = self._service.lane_of(admission)
        details = [
            line
            for line in (
                upstream_line,
                self._request_line(admission),
                self._hold_line(lane),
                *self._lane_lines(lane, status_code, code),
            )
            if line
        ]
        self._log_problem(code, status_code, reason, admission, upstream_line)
        self._notify(
            ProbeResult(
                ok=False,
                status_code=status_code,
                code=code,
                duration_ms=admission.duration * 1000,
                source="ask",
                details=tuple(details),
                scope=admission.scope,
            )
        )
        return AskOutcome("problem", code=code)

    def _report_reroutes(self, admission: Admission, *, answered: bool) -> None:
        """Başka hesaba taşınan isteğin geride bıraktığı hesap arızalarını bildirir.

        Karşı sistem bu arızayı görmez (istek öbür hesaptan cevaplanır); operatör
        ise bir hesabın devreden çıktığını bilmelidir.
        """
        reports = self._service.reroute_reports(
            admission, request_id=self.request_id, answered=answered
        )
        for move, report in zip(admission.reroutes, reports, strict=True):
            logger.warning(
                "ask_rerouted",
                ask_id=self.request_id,
                from_account=move.scope,
                to_account=move.moved_to,
                code=move.code,
                answered=answered,
            )
            metrics.inc("apiwrapper_ask_total", labels={"status": "rerouted", "code": move.code})
            self._notify(report)

    def _held(
        self,
        hold: Hold,
        *,
        reason: str,
        lane: Lane | None = None,
        admission: Admission | None = None,
    ) -> AskOutcome:
        """Arıza zaten biliniyordu: istek upstream'e gönderilmedi."""
        service = self._service
        lane = lane if lane is not None else service.lanes.primary
        if hold.trial_in_flight:
            when = "bir deneme isteği şu an işleniyor"
        else:
            when = f"sıradaki deneme {human_duration(math.ceil(hold.remaining))} sonra"
        details = [
            "Bu istek upstream'e gönderilmedi: arıza "
            f"{human_duration(hold.failing_for)} önce öğrenildi, {when}.",
        ]
        # Arızayı başka bir istek (Telegram mesajı, probe) ortaya çıkardıysa bu
        # mesaj olayın ilk bildirimi olabilir: upstream'in o andaki cevabını ekle.
        note = service.upstream.error_since(
            time.monotonic() - hold.failing_for - 1.0, lane.slot
        )
        if note is not None:
            details.append(f"Upstream'den görülen son hata: {note.describe()}")
        details.append(self._request_line(admission))
        details.extend(self._lane_lines(lane, None, hold.code))
        self._log_problem(hold.code, None, reason, admission, None)
        self._notify(
            ProbeResult(
                ok=False,
                status_code=None,
                code=hold.code,
                source="ask",
                details=tuple(details),
                scope=service.lanes.scope(lane),
            )
        )
        return AskOutcome("problem", code=hold.code)

    def _local_problem(
        self, code: str, status_code: int | None, admission: Admission, *, reason: str
    ) -> AskOutcome:
        """Upstream arızası olmayan sorun: arıza kaydı açılmaz, seyreltilmiş bildirim."""
        self._log_problem(code, status_code, reason, admission, None)
        if self._alerter is not None:
            text = format_event(code, status_code, (self._request_line(admission),))
            self._tasks.spawn(self._alerter.notify_event(code, text), name="ask-alert")
        return AskOutcome("problem", code=code)

    # --------------------------------------------------------- bildirim
    def _notify(self, result: ProbeResult) -> None:
        """Telegram bildirimini arka planda gönderir; cevabı geciktirmez."""
        if self._alerter is None:
            return
        self._tasks.spawn(self._alerter.notify(result), name="ask-alert")

    def _notify_recovery(self, scope: str = "") -> None:
        alerter = self._alerter
        if alerter is None or alerter.policy_for(scope).state.active_key is None:
            return  # açık arıza yok: ağ çağrısı da görev de gerekmez
        self._notify(ProbeResult(ok=True, status_code=200, source="ask", scope=scope))

    def _log_problem(
        self,
        code: str,
        status_code: int | None,
        reason: str,
        admission: Admission | None,
        upstream_line: str | None,
    ) -> None:
        fields: dict[str, Any] = {
            "ask_id": self.request_id,
            "code": code,
            "status": status_code,
            "reason": reason,
            "elapsed_ms": round((time.monotonic() - self._accepted) * 1000, 2),
        }
        if admission is not None:
            fields["waited_ms"] = round(admission.waited * 1000, 2)
            fields["duration_ms"] = round(admission.duration * 1000, 2)
        if upstream_line:
            fields["upstream"] = upstream_line
        logger.warning("ask_problem", **fields)
        metrics.inc("apiwrapper_ask_total", labels={"status": "problem", "code": code})


@router.post(
    "/ask",
    response_model=None,
    summary="Ask and wait: 'wait' / 'done' / 'problem' status lines (for automated callers)",
)
async def ask(
    body: AskRequest,
    request: Request,
    service: CompletionService = Depends(get_completion_service),
) -> StreamingResponse:
    """Soruyu sıraya alır ve durumu satır satır bildirir (bkz. modül açıklaması)."""
    settings: Settings = request.app.state.settings
    # Çağıranın hatası akış başlamadan, düz HTTP hatası olarak döner. Upstream
    # arızası değildir (bekletme başlatmaz) ama operatöre bildirilir: karşı sistem
    # bunu da "problem" sayar ve düzeltilmezse her soru sessizce yedeğe gider.
    try:
        chat_request = build_chat_request(body, settings)
        service.registry.resolve(chat_request.model)
        validate_request(chat_request, settings)
    except APIWrapperError as exc:
        report_rejected(request, exc)
        raise

    runner = AskRunner(
        request=request,
        request_id=getattr(request.state, "request_id", None) or new_request_id(),
        chat_request=chat_request,
        # Her soru bağımsızdır: sohbet yeniden kullanımı (SESSION_REUSE /
        # SESSION_STATELESS=false) açık olsa bile kimlik verilmez, böylece her
        # istek yeni bir upstream sohbeti alır. Aksi halde alarm soruları hem
        # birbirinin hem de aynı anahtarı kullanan Telegram sohbetinin
        # bağlamına karışırdı (ölçüldü).
        client_identity=None,
        service=service,
        settings=settings,
        alerter=getattr(request.app.state, "alerter", None),
        tasks=request.app.state.tasks,
    )
    # Korumalı yanıt: bağlantı yarıda biterse üretici kapatılır, iş iptal edilir.
    return GuardedStreamingResponse(
        runner.events(),
        media_type="text/event-stream",
        headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
    )
