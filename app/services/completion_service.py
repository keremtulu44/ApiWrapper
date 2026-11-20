"""Sohbet tamamlama orkestrasyonu: stream ve non-stream akışlar."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.adapters.openai_to_upstream import build_upstream_request, validate_request
from app.adapters.prompt_builder import build_prompt
from app.adapters.upstream_to_openai import (
    build_usage,
    make_completion,
    make_content_chunk,
    make_finish_chunk,
    make_role_chunk,
    make_usage_chunk,
)
from app.core.config import Settings
from app.core.diagnostics import explain_failure
from app.core.errors import (
    APIWrapperError,
    InvalidRequestError,
    QueueFullError,
    QueueTimeoutError,
    RecaptchaError,
    UpstreamError,
    UpstreamQuotaError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.schemas.upstream import EventType, StreamEvent
from app.services.account import Account, AccountPool
from app.services.alerting import ProbeResult
from app.services.lanes import ACCOUNT_HEADER, Lane, LaneSet
from app.services.model_registry import ModelRegistry
from app.services.recaptcha.base import RecaptchaProvider
from app.services.request_queue import (
    QueueAbandoned,
    QueueFull,
    QueueTimeout,
    RequestQueue,
    Ticket,
)
from app.services.session_manager import SessionManager
from app.services.upstream_health import UpstreamHealth
from app.upstream.client import UpstreamClient, error_excerpt
from app.upstream.exceptions import (
    CircuitOpen,
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamProtocolError,
    UpstreamQuotaExceeded,
    UpstreamRefreshRequired,
    UpstreamTimeout,
)
from app.upstream.quota import QuotaTextScanner, find_quota_marker, quota_error_message
from app.upstream.stream_parser import parse_stream
from app.utils.ids import new_completion_id, now_ts
from app.utils.sse import sse_done, sse_event
from app.utils.text import SurrogateJoiner, repair_surrogates
from app.utils.tokens import count_tokens, tiktoken_available

logger = get_logger(__name__)

T = TypeVar("T")

#: Upstream hakkında bir şey söylemeyen (wrapper'ın kendi) hata kodları.
_LOCAL_ERROR_CODES = frozenset({"queue_full", "queue_timeout", "client_closed_request"})


@dataclass(frozen=True, slots=True)
class Reroute:
    """İşi yarıda kalan isteğin bir hesaptan öbürüne taşınması (bildirim için)."""

    #: İsteği cevaplayamayan hesap.
    scope: str
    #: O hesaptaki arızanın kodu ve wrapper'ın iç durum kodu.
    code: str
    status_code: int
    #: Upstream'in o hesaba verdiği cevabın özeti (varsa; gizli değer taşımaz).
    upstream_line: str | None
    #: İsteğin taşındığı hesap.
    moved_to: str


@dataclass(slots=True)
class Admission:
    """Bir isteğin sıraya giriş koşulları ve (çıkışta) ölçülen süreleri."""

    #: Loglarda ve yönetim ucunda görünen kaynak ("api", "ask", "keepalive", "telegram").
    label: str = "api"
    #: Sırada en fazla bekleme (saniye). ``None``/0 = sınırsız.
    max_wait: float | None = None
    #: Sıra geldikten **sonraki** işlem süresi sınırı (saniye). ``None``/0 = yalnız
    #: upstream toplam sınırı (`TOTAL_REQUEST_TIMEOUT`) geçerli.
    deadline: float | None = None
    #: True (`/v1/ask`): arızası bilinen hesaba gidilmez. Hiçbir hesap kabul
    #: etmiyorsa `LaneUnavailable`, beklerken hesap arızalanır ve gidecek başka
    #: hesap kalmazsa `QueueAbandoned` fırlar; çağıran beklemeden "problem" der.
    respect_hold: bool = False
    #: İsteği bu hesap yuvasına sabitler (probe, elle sınama); ``None`` = bölüşüm.
    account_slot: int | None = None
    #: Servis doldurur: sırada (aralık dahil) beklenen toplam süre.
    waited: float = 0.0
    #: Servis doldurur: işlemin başladığı an (`time.monotonic()`); 0 = hiç başlamadı.
    started_at: float = 0.0
    #: Servis doldurur: istek upstream'e gönderildi mi (en az bir kez)? Akışsız
    #: sohbet ucu buna bakar: gönderilmiş istek, istemcisi kopsa da yarıda
    #: kesilmez (canlı sürümdeki davranış; bkz. `app/api/disconnect.py`).
    sent: bool = False
    #: Servis doldurur: işlemde geçen süre.
    duration: float = 0.0
    #: Servis doldurur: isteği işleyen hesabın adı (tek hesapta boş) ve yuvası.
    scope: str = ""
    slot: int | None = None
    #: Servis doldurur: istek bir hesaptan öbürüne taşındıysa her taşıma.
    reroutes: list[Reroute] = field(default_factory=list)


@dataclass(slots=True)
class Turn:
    """Bir isteğin o an tuttuğu hat ve sıra izni (taşınırsa ikisi de değişir)."""

    lane: Lane
    admission: Admission
    ticket: Ticket | None = None
    #: Bu isteğin daha önce denediği hesap yuvaları (aynı hesaba geri dönülmez).
    tried: set[int] = field(default_factory=set)

    @property
    def pinned(self) -> bool:
        return self.admission.account_slot is not None


def _retry_after_seconds(error: APIWrapperError) -> float | None:
    """Hatanın `Retry-After` başlığını saniyeye çevirir (yoksa ``None``)."""
    raw = (error.headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _retrieve_exception(task: asyncio.Future[Any]) -> None:
    """İptal edilip beklenmeyen görevin istisnasını "alındı" say (log gürültüsü olmasın)."""
    if not task.cancelled():
        task.exception()


#: İstemciye içerik taşıyan olay türleri. Akış yolunda istemciye giden tek şey
#: `EventType.TEXT` delta'sıdır (`make_content_chunk`); `REASONING`, `DATA`,
#: `TOOL_*`, `MESSAGE_ANNOTATION` ve `FINISH` olayları parse edilir ama tek
#: bayt bile iletilmez. Bunları "gönderildi" saymak, düşünme (thinking)
#: modellerinde devri haksız yere kapatırdı: bu modeller metinden önce düşünme
#: delta'ları gönderir, dolayısıyla istemciye hiçbir şey ulaşmamışken
#: `emitted > 0` olur ve hesap devri yapılamazdı.
_CLIENT_VISIBLE_EVENTS = frozenset({EventType.TEXT})


def _dump(chunk) -> dict:
    """Chunk'ı OpenAI'ye yakın biçimde serialize eder (boş alanlar atlanır).

    `delta.content` bir istisnadır: içerik parçası her zaman görünmelidir.
    """
    data = chunk.model_dump(exclude_none=True)
    for choice in data.get("choices", []):
        delta = choice.get("delta")
        if isinstance(delta, dict) and "content" not in delta and choice.get(
            "finish_reason"
        ) is None:
            delta["content"] = ""
    return data


class StopSequenceTracker:
    """Yerel stop-sequence kesme mantığı (upstream desteklemediği için)."""

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.max_len = max((len(s) for s in self.stops), default=0)
        self._pending = ""
        self.triggered = False

    def process(self, chunk: str) -> str:
        """Yayınlanabilir metni döndürür; stop bulunursa `triggered` işaretlenir."""
        if not self.stops or self.triggered:
            return "" if self.triggered else chunk

        buffer = self._pending + chunk
        earliest = -1
        for stop in self.stops:
            idx = buffer.find(stop)
            if idx != -1 and (earliest == -1 or idx < earliest):
                earliest = idx
        if earliest != -1:
            self.triggered = True
            self._pending = ""
            return buffer[:earliest]

        # Kısmi eşleşme olabilecek kuyruğu beklet.
        hold = max(0, self.max_len - 1)
        if hold and len(buffer) > hold:
            emit, self._pending = buffer[:-hold], buffer[-hold:]
            return emit
        if hold:
            self._pending = buffer
            return ""
        self._pending = ""
        return buffer

    def flush(self) -> str:
        if self.triggered:
            return ""
        pending, self._pending = self._pending, ""
        return pending


class CompletionService:
    """OpenAI isteğini upstream'e taşıyan ana servis."""

    def __init__(
        self,
        settings: Settings,
        upstream: UpstreamClient,
        registry: ModelRegistry,
        sessions: SessionManager,
        recaptcha: RecaptchaProvider,
        accounts: AccountPool | None = None,
        queue: RequestQueue | None = None,
        health: UpstreamHealth | None = None,
        lanes: LaneSet | None = None,
    ) -> None:
        self.settings = settings
        self.upstream = upstream
        self.registry = registry
        self.sessions = sessions
        self.recaptcha = recaptcha
        self.accounts = accounts if accounts is not None else AccountPool(settings)
        #: Hesap başına bir hat: sıra + sağlık kaydı (bkz. `app/services/lanes.py`).
        #: Tek hesapta tek hat vardır; bütün tamamlamalar o sıradan geçer.
        if lanes is None:
            lanes = LaneSet.from_settings(settings, self.accounts)
            if queue is not None:
                lanes.primary.queue = queue
            if health is not None:
                lanes.primary.health = health
        self.lanes = lanes

    @property
    def queue(self) -> RequestQueue:
        """İlk hattın sırası (tek hesapta: bütün isteklerin geçtiği tek sıra)."""
        return self.lanes.primary.queue

    @property
    def health(self) -> UpstreamHealth:
        """İlk hattın sağlık kaydı (tek hesapta: upstream'in bilinen durumu)."""
        return self.lanes.primary.health

    def lane_of(self, admission: Admission) -> Lane:
        """İsteği (en son) işleyen hat; hiç hatta girmediyse ilk hat."""
        if admission.slot is not None:
            lane = self.lanes.get(admission.slot)
            if lane is not None:
                return lane
        return self.lanes.primary

    # ------------------------------------------------------------ helpers
    @property
    def system_fingerprint(self) -> str:
        suffix = "tiktoken" if tiktoken_available() else "estimate"
        return f"fp_{self.settings.app_version.replace('.', '')}_{suffix}"

    @staticmethod
    def _translate(exc: Exception) -> APIWrapperError:
        """Upstream istisnalarını API hatalarına çevirir."""
        if isinstance(exc, APIWrapperError):
            return exc
        if isinstance(exc, CircuitOpen):
            return UpstreamUnavailableError(
                "Upstream service is temporarily unavailable (circuit breaker open). "
                "Please retry shortly."
            )
        if isinstance(exc, UpstreamQuotaExceeded):
            headers: dict[str, str] = {}
            if exc.retry_after is not None:
                headers["retry-after"] = str(max(1, int(exc.retry_after + 0.999)))
            return UpstreamQuotaError(exc.message, headers=headers)
        if isinstance(exc, UpstreamRefreshRequired):
            # İstemci anlık görüntüsü eskimiş: erişilebilirlik arızası değil,
            # yeniden ele alınması gereken bir yapılandırma durumu.
            return APIWrapperError(
                "The upstream service rejected the captured client snapshot as "
                "outdated (HTTP 409 refresh_required). Re-copy the browser request "
                "as cURL, update UPSTREAM_CURL and redeploy. Cookies, quota and "
                "the API key are not the cause.",
                status_code=503,
                err_type="upstream_error",
                code="upstream_refresh_required",
                headers={"retry-after": "300"},
            )
        if isinstance(exc, UpstreamAuthRejected):
            return APIWrapperError(
                "The upstream service rejected the session credentials (401). "
                "Refresh UPSTREAM_COOKIE (and/or UPSTREAM_ACCESS_TOKEN) in the "
                "environment; the extracted access_token is missing or expired.",
                status_code=502,
                err_type="upstream_error",
                code="upstream_unauthorized",
            )
        if isinstance(exc, RecaptchaRejected):
            # Eski metin "token'ı yenile ya da browser/external sağlayıcıya geç"
            # diyordu; ikisi de bu hedefte faydasız (docs/LESSONS §15.4).
            return RecaptchaError(
                "The upstream service rejected the request at its reCAPTCHA check. "
                "This is usually temporary on the upstream side: retry later without "
                "changing anything.",
                code="recaptcha_rejected",
            )
        if isinstance(exc, UpstreamTimeout):
            return UpstreamTimeoutError(str(exc))
        if isinstance(exc, UpstreamHTTPError):
            status = exc.status_code or 502
            if status == 429:
                return APIWrapperError(
                    "Upstream rate limit reached. Please retry later.",
                    status_code=429,
                    err_type="rate_limit_error",
                    code="upstream_rate_limited",
                )
            if status == 404:
                # Sağlayıcının URL'i istemciye yansıtılmaz; operatör için
                # ayrıntı logda (`upstream_http_error`) zaten var.
                return APIWrapperError(
                    "Upstream returned HTTP 404: UPSTREAM_STREAM_PATH does not match "
                    "the live endpoint. Re-import the path from a fresh browser cURL "
                    "(scripts/curl_to_env.py).",
                    status_code=502,
                    err_type="upstream_error",
                    code="upstream_not_found",
                )
            if 300 <= status < 400:
                # Yalnızca yol gösterilir: sağlayıcı host'u istemciye sızmaz.
                location = str(getattr(exc, "location", "") or "")
                path = location.split("://", 1)[-1]
                if "://" in location:
                    path = "/" + path.split("/", 1)[-1] if "/" in path else "/"
                suffix = f" Target path: {path}." if path else ""
                return APIWrapperError(
                    "Upstream redirected the request instead of answering it."
                    + suffix
                    + " Update UPSTREAM_STREAM_PATH to the redirected path "
                    "(scripts/curl_to_env.py).",
                    status_code=502,
                    err_type="upstream_error",
                    code="upstream_redirect",
                )
            return UpstreamError(
                f"Upstream request failed with HTTP {status}."
            )
        if isinstance(exc, (UpstreamNetworkError, UpstreamProtocolError)):
            return UpstreamError(str(exc))
        return UpstreamError(f"Unexpected upstream failure: {exc}")

    # ------------------------------------------------------ sıra ve sonuç
    def _sticky_key(
        self, request: ChatCompletionRequest, client_identity: str | None
    ) -> str | None:
        """Sohbet yeniden kullanılıyorsa istekleri aynı hesapta tutan anahtar."""
        if not self.lanes.multi:
            return None
        return self.sessions.base_key(request.conversation_id, client_identity)

    async def _admit(self, admission: Admission, sticky_key: str | None = None) -> Turn:
        """İsteği bir hesabın hattına atar; sırası (ve istekler arası bekleme) gelince döner.

        Sıra hataları API hatasına çevrilir. Hiçbir hesap isteği kabul etmiyorsa
        (`respect_hold`) `LaneUnavailable` çağırana aynen ulaşır. İstek beklerken
        hattı arızalanırsa çalışan başka bir hatta geçer; yoksa `QueueAbandoned`.
        """
        while True:
            try:
                lane = self.lanes.choose(
                    admission,
                    respect_hold=admission.respect_hold,
                    pinned_slot=admission.account_slot,
                    sticky_key=sticky_key,
                )
            except LookupError as exc:
                raise InvalidRequestError(
                    f"No upstream account in slot {admission.account_slot}.",
                    param=ACCOUNT_HEADER,
                ) from exc
            turn = Turn(lane=lane, admission=admission)
            try:
                await self._enter(turn)
            except QueueAbandoned:
                lane.health.release_trial(admission)
                if not turn.pinned and self.lanes.failover and self.lanes.others_usable(lane):
                    # Beklediği hesap arızalandı: iş çalışan hesaba verilir.
                    logger.info(
                        "request_reassigned",
                        label=admission.label,
                        from_account=lane.label or None,
                        waited_seconds=round(admission.waited, 2),
                    )
                    continue
                raise
            except BaseException:
                lane.health.release_trial(admission)
                raise
            return turn

    async def _enter(self, turn: Turn) -> None:
        """`turn.lane` sırasına girer; izin gelince `turn.ticket` dolar."""
        admission, lane = turn.admission, turn.lane
        max_wait = admission.max_wait or None
        if max_wait is not None:
            # Süre sınırı isteğe aittir: başka hatta beklenen süre düşülür.
            max_wait = max(0.001, max_wait - admission.waited)
        should_leave = None
        if admission.respect_hold and not turn.pinned:
            # Sırada beklerken hesabın arızası öğrenilirse (öndeki istek başarısız
            # oldu) bekleyenler tek tek denenmesin: hepsi hattan ayrılsın.
            def should_leave(health: UpstreamHealth = lane.health) -> bool:
                return health.blocked() is not None

        try:
            ticket = await lane.queue.acquire(
                admission.label, max_wait=max_wait, should_leave=should_leave
            )
        except QueueFull as exc:
            raise QueueFullError(
                f"The request queue is full ({exc.waiting} requests waiting). "
                "Retry shortly.",
                headers={"retry-after": "30"},
            ) from exc
        except QueueTimeout as exc:
            admission.waited += exc.waited
            raise QueueTimeoutError(
                f"The request waited {int(admission.waited)}s in the queue "
                "without being served."
            ) from exc
        except QueueAbandoned as exc:
            admission.waited += exc.waited
            raise
        turn.ticket = ticket
        admission.waited += ticket.waited
        if admission.started_at <= 0:
            admission.started_at = time.monotonic()
        admission.scope = self.lanes.scope(lane)
        admission.slot = lane.slot

    def _release(self, turn: Turn | None) -> None:
        """Hattın sırasını (ve varsa deneme hakkını) bırakır; her çıkış yolunda çağrılır."""
        if turn is None:
            return
        turn.lane.queue.release(turn.ticket)
        turn.ticket = None
        # Deneme isteği sonuç bildirmeden bittiyse (iptal) hakkı sıradakine bırak.
        turn.lane.health.release_trial(turn.admission)

    async def _reroute(self, turn: Turn, exc: Exception, reason: str) -> bool:
        """Hesaba özgü ret alan isteği çalışan başka bir hesaba taşır.

        Yalnız istemciye henüz içerik gönderilmediyse çağrılır. Taşınan istek yeni
        hesabın sırasına girer (o hesap da aynı anda tek istek işler); bu bekleyiş
        isteğin işlem süresi sınırından düşer. Sabitlenmiş istek (probe) taşınmaz:
        o, kendi hesabını sınamak içindir. True dönerse `turn` yeni hattadır.
        """
        if turn.pinned:
            return False
        old = turn.lane
        turn.tried.add(old.slot)
        lane = self.lanes.alternative(turn.tried)
        if lane is None:
            return False
        # Arıza, yeni hattın sırası beklenmeden kayda geçer ki bu hesap yeni iş almasın.
        api_error = self._translate(exc)
        self._record_outcome(old, api_error)
        admission = turn.admission
        status = getattr(exc, "status_code", None)
        body = str(getattr(exc, "body", "") or "")
        upstream_line = None
        if status is not None:
            excerpt = error_excerpt(body) if body else ""
            upstream_line = f"Upstream cevabı: HTTP {status}" + (
                f" · {excerpt}" if excerpt else ""
            )
        # Taşıma, yeni sıra beklenmeden not edilir: istek orada süresini doldursa
        # bile operatör bu hesabın neden devreden çıktığını öğrenir.
        move = Reroute(
            scope=old.label,
            code=api_error.code or f"http_{api_error.status_code}",
            status_code=api_error.status_code,
            upstream_line=upstream_line,
            moved_to=lane.label,
        )
        admission.reroutes.append(move)
        # Eski hattın sırası bırakılır (upstream'e gidildi: aralık uygulanır),
        # yeni hattın sırası beklenir.
        old.queue.release(turn.ticket)
        turn.ticket = None
        turn.lane = lane
        try:
            await self._enter(turn)
        except QueueAbandoned:
            # Beklerken yeni hesap da arızalandı: istek ilk hatasıyla biter.
            turn.lane = old
            admission.reroutes.remove(move)
            return False
        logger.warning(
            "account_switched",
            from_account=old.label or None,
            to_account=lane.label or None,
            switch=len(admission.reroutes),
            reason=reason,
        )
        # Etiketler eski seriyle aynı: kota kilidi `report_quota` içinde "quota"
        # olarak sayılır, kota yüzünden yapılan geçiş "switch", kimlik reddi "auth".
        metrics.inc(
            "apiwrapper_account_switches_total",
            labels={"reason": "switch" if reason == "quota" else reason},
        )
        return True

    def reroute_reports(
        self, admission: Admission, *, request_id: str, answered: bool, source: str = "ask"
    ) -> list[ProbeResult]:
        """Başka hesaba taşınan isteğin geride bıraktığı hesap arızalarının bildirimleri.

        Karşı taraf bu arızayı görmez (istek öbür hesaptan cevaplanır); operatör
        ise bir hesabın devreden çıktığını bilmelidir. Her taşıma için, arızalanan
        hesabın kaydına işlenecek bir sonuç döner.
        """
        lanes = self.lanes
        reports: list[ProbeResult] = []
        unharmed = (
            'karşı sisteme "problem" dönmedi' if source == "ask" else "istemciye hata dönmedi"
        )
        for move in admission.reroutes:
            outcome = (
                f"İstek {request_id} {move.moved_to} hesabından cevaplandı; {unharmed}."
                if answered
                else f"İstek {request_id} {move.moved_to} hesabına taşındı ama orada da "
                "cevaplanamadı."
            )
            details = [line for line in (move.upstream_line, outcome) if line]
            failed = next((lane for lane in lanes.lanes if lane.label == move.scope), None)
            if failed is not None:
                critical = (
                    explain_failure(move.status_code, move.code).severity == "critical"
                )
                outlook = lanes.outlook(failed)
                if outlook:
                    details.append(outlook)
                details.extend(lanes.incident_notes(failed, critical=critical))
            reports.append(
                ProbeResult(
                    ok=False,
                    status_code=move.status_code,
                    code=move.code,
                    source=source,
                    details=tuple(details),
                    scope=move.scope,
                    rerouted=True,
                )
            )
        return reports

    @staticmethod
    async def _within_deadline(work: Coroutine[Any, Any, T], deadline: float | None) -> T:
        """İşi ``deadline`` saniyeyle sınırlar; dolunca iptal edip 504 fırlatır.

        İş ayrı bir görevde koşar: upstream istemcisinin kendi toplam süre sınırı
        (`_total_deadline`) "geçerli görevi" iptal ederek çalışır; iki sınırın
        aynı görevi iptal etmesi Python sürümüne göre farklı davranır.
        """
        if deadline is None or deadline <= 0:
            return await work
        task = asyncio.ensure_future(work)
        task.add_done_callback(_retrieve_exception)
        try:
            done, _ = await asyncio.wait({task}, timeout=deadline)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            # Upstream bağlantısı kapanana dek bekle: sıra, iş gerçekten bitmeden
            # bir sonraki isteğe verilmesin.
            await asyncio.gather(task, return_exceptions=True)
            raise UpstreamTimeoutError(
                f"The request was not answered within {deadline:g}s (request deadline)."
            )
        return task.result()

    def _record_outcome(self, lane: Lane, error: Exception | None) -> None:
        """Tamamlamanın sonucunu işi yapan hesabın sağlık kaydına bildirir.

        İsteğe özgü hatalar (geçersiz gövde, bilinmeyen model) ve wrapper'ın kendi
        yük sınırları upstream hakkında bir şey söylemez; kayda geçmez.
        """
        health = lane.health
        if error is None:
            health.record_success()
            return
        if not isinstance(error, APIWrapperError):
            return
        if error.err_type == "invalid_request_error" or error.code in _LOCAL_ERROR_CODES:
            return
        health.record_failure(
            error.code or f"http_{error.status_code}", _retry_after_seconds(error)
        )

    def _new_quota_scanner(self) -> QuotaTextScanner:
        """Düz metin kota taraması için yeni bir tarayıcı üretir."""
        return QuotaTextScanner(
            self.settings.upstream_limit_markers,
            window=self.settings.quota_text_scan_chars,
        )

    def _raise_for_quota(self, marker: str | None, detail: str) -> None:
        """Kota işareti bulunduysa uygun istisnayı fırlatır."""
        if not marker:
            return
        metrics.inc(
            "apiwrapper_upstream_quota_errors_total", labels={"source": "stream"}
        )
        logger.warning(
            "upstream_quota_exceeded",
            marker=marker,
            hint=(
                "Upstream limited this account mid-stream; retrying would only "
                "extend the lockout."
            ),
        )
        raise UpstreamQuotaExceeded(
            quota_error_message(marker, "stream", detail), marker=marker
        )

    async def _prepare(
        self,
        request: ChatCompletionRequest,
        client_identity: str | None = None,
        account: Account | None = None,
    ):
        """Model çözümü, oturum, captcha token'ı ve gövde inşası."""
        entry = self.registry.resolve(request.model)
        # Sohbet hesaba bağlıdır: hesap değişince upstream tarafında başka bir
        # sohbete geçilir (başka hesabın sohbetine yazmak olmaz).
        account_label = account.label if account else None
        warmup = (
            (lambda chat_id: self.upstream.warmup(chat_id, account=account))
            if account is not None
            else None
        )
        session = await self.sessions.acquire(
            request.conversation_id, client_identity, account_label, warmup=warmup
        )
        # Yalnız static kipte hesabın kendi token'ı kullanılır; token tarayıcı
        # oturumuna bağlı olduğu için hesaplar arası paylaşılmamalıdır. Dinamik
        # sağlayıcılar ve noop, hesapta kalmış eski statik değerlerle ezilemez.
        if (
            self.settings.recaptcha_provider == "static"
            and account is not None
            and account.has_own_captcha_token
        ):
            token = account.recaptcha_token
        else:
            token = await self.recaptcha.get_token()
        built = build_upstream_request(
            request,
            settings=self.settings,
            upstream_model_id=entry.upstream_id,
            chat_id=session.chat_id,
            recaptcha_token=token,
        )
        return entry, built

    def _refuse_if_cooling(self, account: Account | None) -> None:
        """Hesap kota dinlenmesindeyse isteği upstream'e göndermeden reddeder.

        Kilitli hesapla zorlamak kilidi uzatır. Bölüşüm dinlenen hesabı zaten
        atlar; buraya yalnız gidecek başka hesap kalmadığında (ya da istek bu
        hesaba sabitlendiğinde) gelinir.
        """
        if account is None:
            return
        wait = self.accounts.cooldown_remaining(account.slot)
        if wait <= 0:
            return
        raise UpstreamQuotaExceeded(
            "All upstream accounts are temporarily locked by the provider. "
            f"Earliest retry in {int(wait)}s.",
            retry_after=wait,
        )

    async def _events(
        self,
        request: ChatCompletionRequest,
        client_identity: str | None = None,
        turn: Turn | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Upstream olay akışı.

        İstek, `turn` ile gelen hattın hesabıyla gönderilir. İki otomatik kurtarma
        yolu vardır: captcha reddinde (dinamik sağlayıcıda) token bir kez yenilenir;
        hesaba özgü rette (kota, kimlik) istek çalışan başka bir hesaba taşınır
        (`_reroute`). İkisi de **yalnızca istemciye henüz tek bir olay
        gönderilmediyse** yapılır; aksi halde iki farklı yanıtın parçaları
        birbirine karışırdı.

        Kota tespiti de burada yapılır (HTTP hata gövdesi + hata olayı + düz
        metin delta'sı) ki `report_quota` her yolda çağrılsın.
        """
        # Geçersiz istekleri sohbet açma ve CAPTCHA üretiminden önce reddet. Aksi
        # halde örneğin boş `messages`, hesap dinlenmedeyken doğru 400 yerine
        # yanıltıcı bir 429 döndürür ve gereksiz iş yapar.
        validate_request(request, self.settings)
        max_switches = max(0, self.settings.account_max_switches)
        switches = 0
        captcha_retried = False
        emitted = 0

        while True:
            account = turn.lane.account if turn is not None else None
            self._refuse_if_cooling(account)
            # Kota taraması burada yapılır — çağıran tarafta değil. Aksi halde
            # istisna `except UpstreamQuotaExceeded` bloğunun *dışında* fırlar
            # ve hesap devri/cooldown/AIMD hiç devreye girmez (HTTP hata
            # gövdesi yolunda client istisnayı içeride fırlattığı için o yol
            # çalışıyordu, metin mesajı yolu çalışmıyordu).
            #
            # Tarayıcı **deneme başına** yenilenir: tampon denemeler arasında
            # korunursa birinci hesabın kota metni ikinci hesabın normal
            # cevabıyla birleşir ve sağlam hesap da kilitlenir.
            scanner = self._new_quota_scanner()
            # Upstream isteği kabul ettiğinde pencereye BİR mesaj işlenir.
            # `emitted` ile kontrol edilmez: `emitted` yalnızca istemciye içerik
            # taşıyan TEXT olaylarında artar, `f:` (START) olayında artmaz —
            # dolayısıyla `emitted == 0` hem `f:` hem ilk TEXT için doğru kalır
            # ve mesaj iki kez sayılırdı (kota penceresi yarı yarıya kısalırdı).
            message_recorded = False
            entry, built = await self._prepare(request, client_identity, account)
            request_settings = (
                account.effective_settings(self.settings) if account else self.settings
            )
            account_label = account.label if account else None
            logger.info(
                "upstream_request",
                model=entry.id,
                upstream_model=entry.upstream_id,
                chat_id=built.chat_id,
                account=account_label,
                prompt_chars=len(built.prompt),
                url=request_settings.stream_url(built.chat_id),
            )
            if turn is not None:
                # Upstream'e gerçekten gidiliyor: bu andan sonra akışsız istek,
                # istemcisi kopsa da sonuna kadar sürer (`Admission.sent`).
                turn.admission.sent = True
            if turn is not None and turn.ticket is not None:
                # Bu istekten sonra sıradaki, istekler arası bekleme kadar
                # bekletilecek.
                turn.ticket.touched = True
            try:
                async with self.upstream.stream_completion(
                    built.chat_id,
                    built.payload.to_wire(self.settings.upstream_recaptcha_field),
                    account=account,
                ) as chunks:
                    idle = self.settings.stream_idle_timeout
                    iterator = parse_stream(chunks).__aiter__()
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                iterator.__anext__(), timeout=idle
                            )
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            raise UpstreamTimeout(
                                f"No data received from upstream for {idle}s."
                            ) from exc
                        if not message_recorded and account is not None:
                            # İlk olay geldi: upstream isteği kabul etti.
                            self.accounts.record_message(account.slot)
                            message_recorded = True
                        # Kota tespitini yield'dan önce yap: içerik istemciye
                        # ulaştıktan sonra devir yapılamaz (iki yanıt karışır).
                        if event.type is EventType.ERROR and event.text:
                            marker = find_quota_marker(
                                event.text, self.settings.upstream_limit_markers
                            )
                            self._raise_for_quota(marker, event.text)
                        elif event.type is EventType.TEXT and event.text and scanner.active:
                            self._raise_for_quota(scanner.feed(event.text), event.text)
                        if event.type in _CLIENT_VISIBLE_EVENTS and event.text:
                            emitted += 1
                        yield event
                return
            except UpstreamQuotaExceeded as exc:
                if account is not None:
                    self.accounts.report_quota(account.slot)
                movable = emitted == 0 and switches < max_switches
                if turn is None or not movable or not await self._reroute(turn, exc, "quota"):
                    raise
                switches += 1
                continue
            except UpstreamAuthRejected as exc:
                # Oturum kimliği reddedildi (401). Kota yolundan farkı: hesap
                # dinlenmeye alınmaz; arızası sağlık kaydına işlenir ve çalışan
                # başka hesap varsa istek oradan tamamlanır.
                movable = emitted == 0 and switches < max_switches
                if turn is None or not movable or not await self._reroute(turn, exc, "auth"):
                    raise
                switches += 1
                continue
            except RecaptchaRejected:
                self.recaptcha.invalidate()
                can_refresh = self.settings.recaptcha_provider in {"browser", "external"}
                if not can_refresh or captcha_retried or emitted > 0:
                    raise
                captcha_retried = True
                logger.warning("recaptcha_rejected_retrying")
                continue

    # --------------------------------------------------------- non-stream
    async def create_completion(
        self,
        request: ChatCompletionRequest,
        client_identity: str | None = None,
        admission: Admission | None = None,
    ) -> ChatCompletionResponse:
        """Tam yanıtı biriktirip tek seferde döndürür.

        İstek önce bir hesabın sırasına girer; sırası gelince (ve bir önceki istekle araya
        rastgele bekleme konduktan sonra) işlenir. Sonuç sağlık kaydına bildirilir.
        """
        admission = admission if admission is not None else Admission()
        # Geçersiz istek sıraya hiç girmesin: ne sırayı ne aralığı harcasın.
        self.registry.resolve(request.model)
        validate_request(request, self.settings)
        turn = await self._admit(admission, self._sticky_key(request, client_identity))
        try:
            response = await self._within_deadline(
                self._create_completion(request, client_identity, turn),
                admission.deadline,
            )
        except Exception as exc:
            if turn.ticket is None and isinstance(exc, UpstreamTimeoutError):
                # Süre, isteğin taşındığı hesabın sırası beklenirken doldu. Bu bir
                # upstream arızası değil yoğunluktur: o hesap isteği hiç görmedi.
                raise QueueTimeoutError(
                    "The request deadline expired while it was waiting for another "
                    "account's queue."
                ) from exc
            self._record_outcome(turn.lane, exc)
            raise
        finally:
            admission.duration = max(0.0, time.monotonic() - admission.started_at)
            self._release(turn)
        self._record_outcome(turn.lane, None)
        return response

    async def _create_completion(
        self,
        request: ChatCompletionRequest,
        client_identity: str | None = None,
        turn: Turn | None = None,
    ) -> ChatCompletionResponse:
        """Sırası gelmiş isteği upstream'e taşır ve yanıtı biriktirir."""
        started = time.monotonic()
        completion_id = new_completion_id()
        entry = self.registry.resolve(request.model)
        stopper = StopSequenceTracker(request.stop_sequences())

        parts: list[str] = []
        finish_reason = "stop"
        reported_usage: dict[str, int] = {}
        first_token_at: float | None = None
        scanner = self._new_quota_scanner()

        metrics.inc("apiwrapper_requests_total", labels={"endpoint": "completions",
                                                          "stream": "false"})
        try:
            async for event in self._events(
                request, client_identity=client_identity, turn=turn
            ):
                if event.type is EventType.ERROR:
                    # Kota işaretleri `_events` içinde yakalanıp devir denenir;
                    # buraya ulaşan hata olayı gerçek bir upstream hatasıdır.
                    raise UpstreamError(f"Upstream error: {event.text}")
                if event.type in (EventType.TEXT,) and event.text:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                        metrics.observe(
                            "apiwrapper_time_to_first_token_seconds", first_token_at - started
                        )
                    emitted = stopper.process(event.text)
                    if emitted:
                        parts.append(emitted)
                        scanner.disable()
                    if stopper.triggered:
                        finish_reason = "stop"
                        break
                elif event.type in (EventType.FINISH, EventType.STEP_FINISH):
                    if event.finish_reason:
                        finish_reason = event.finish_reason
                    if event.usage:
                        reported_usage.update(event.usage)
        except Exception as exc:
            raise self._translate(exc) from exc

        tail = stopper.flush()
        if tail:
            parts.append(tail)
        # İki parçaya bölünmüş emoji (tek başına vekil) UTF-8'e çevrilemez ve
        # yanıtı HTTP 500'e düşürürdü; yarımlar burada birleştirilir.
        content = repair_surrogates("".join(parts))

        prompt_tokens = count_tokens(self._prompt_preview(request))
        completion_tokens = count_tokens(content)
        usage = build_usage(prompt_tokens, completion_tokens, reported_usage or None)

        metrics.observe(
            "apiwrapper_request_duration_seconds",
            time.monotonic() - started,
            {"endpoint": "completions"},
        )
        metrics.inc("apiwrapper_tokens_total", usage.total_tokens, {"model": entry.id})

        response = make_completion(
            completion_id=completion_id,
            model=entry.id,
            content=content,
            usage=usage,
            finish_reason=finish_reason,
            created=now_ts(),
            system_fingerprint=self.system_fingerprint,
        )
        logger.info(
            "completion_completed",
            completion_id=completion_id,
            model=entry.id,
            stream=False,
            finish_reason=finish_reason,
            content_chars=len(content),
            usage_total_tokens=usage.total_tokens,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return response

    # ------------------------------------------------------------- stream
    async def stream_completion(
        self,
        request: ChatCompletionRequest,
        is_disconnected=None,
        client_identity: str | None = None,
        admission: Admission | None = None,
    ) -> AsyncIterator[str]:
        """SSE gövdesini parça parça üretir. `[DONE]` her koşulda gönderilir.

        Akış da hesabın sırasına girer: rol parçası hemen gönderilir, upstream isteği
        sırası gelince başlar ve sıra akış bitene dek tutulur.
        """
        admission = admission if admission is not None else Admission()
        turn: Turn | None = None
        events: AsyncIterator[StreamEvent] | None = None
        started = time.monotonic()
        completion_id = new_completion_id()
        entry = self.registry.resolve(request.model)
        created = now_ts()
        stopper = StopSequenceTracker(request.stop_sequences())
        # İki parçaya bölünmüş emoji yarımlarını birleştirir (bkz. `app/utils/text.py`).
        joiner = SurrogateJoiner()

        finish_reason = "stop"
        reported_usage: dict[str, int] = {}
        completion_text: list[str] = []
        first_token_at: float | None = None
        usage_total_tokens: int | None = None
        scanner = self._new_quota_scanner()
        stream_failed = False

        metrics.inc("apiwrapper_requests_total", labels={"endpoint": "completions",
                                                          "stream": "true"})
        metrics.inc("apiwrapper_active_streams", 1.0)

        try:
            yield sse_event(_dump(make_role_chunk(completion_id, entry.id, created)))

            try:
                # Geçersiz istek sırayı beklemeden reddedilsin.
                validate_request(request, self.settings)
                turn = await self._admit(admission, self._sticky_key(request, client_identity))
                events = self._events(request, client_identity=client_identity, turn=turn)
                async for event in events:
                    if is_disconnected is not None and await is_disconnected():
                        logger.info(
                            "client_disconnected",
                            completion_id=completion_id,
                            model=entry.id,
                            content_chars=sum(len(part) for part in completion_text),
                            duration_ms=round((time.monotonic() - started) * 1000, 2),
                        )
                        return

                    if event.type is EventType.ERROR:
                        # Kota işaretleri `_events` içinde yakalanıp devir
                        # denenir; buraya ulaşan hata gerçek bir upstream
                        # hatasıdır.
                        raise UpstreamError(f"Upstream error: {event.text}")

                    if event.type is EventType.TEXT and event.text:
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                            metrics.observe(
                                "apiwrapper_time_to_first_token_seconds",
                                first_token_at - started,
                            )
                        emitted = joiner.feed(stopper.process(event.text))
                        if emitted:
                            completion_text.append(emitted)
                            scanner.disable()
                            yield sse_event(
                                _dump(
                                    make_content_chunk(
                                        completion_id, entry.id, created, emitted
                                    )
                                )
                            )
                        if stopper.triggered:
                            finish_reason = "stop"
                            break
                    elif event.type in (EventType.FINISH, EventType.STEP_FINISH):
                        if event.finish_reason:
                            finish_reason = event.finish_reason
                        if event.usage:
                            reported_usage.update(event.usage)

                tail = joiner.feed(stopper.flush()) + joiner.flush()
                if tail:
                    completion_text.append(tail)
                    yield sse_event(
                        _dump(make_content_chunk(completion_id, entry.id, created, tail))
                    )

            except Exception as exc:
                api_error = self._translate(exc)
                if turn is not None:
                    self._record_outcome(turn.lane, api_error)
                logger.warning(
                    "stream_failed",
                    completion_id=completion_id,
                    model=entry.id,
                    error=api_error.message,
                    error_code=api_error.code,
                    error_type=api_error.err_type,
                    status=api_error.status_code,
                    content_chars=sum(len(part) for part in completion_text),
                    content_chunks=len(completion_text),
                    first_token_ms=(
                        round((first_token_at - started) * 1000, 2)
                        if first_token_at is not None
                        else None
                    ),
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                yield sse_event(api_error.to_payload())
                stream_failed = True

            if not stream_failed:
                if turn is not None:
                    self._record_outcome(turn.lane, None)
                yield sse_event(
                    _dump(
                        make_finish_chunk(completion_id, entry.id, created, finish_reason)
                    )
                )

            if not stream_failed and request.wants_usage():
                content = "".join(completion_text)
                usage = build_usage(
                    count_tokens(self._prompt_preview(request)),
                    count_tokens(content),
                    reported_usage or None,
                )
                metrics.inc("apiwrapper_tokens_total", usage.total_tokens, {"model": entry.id})
                usage_total_tokens = usage.total_tokens
                yield sse_event(
                    _dump(make_usage_chunk(completion_id, entry.id, created, usage))
                )

            if not stream_failed:
                logger.info(
                    "stream_completed",
                    completion_id=completion_id,
                    model=entry.id,
                    stream=True,
                    finish_reason=finish_reason,
                    content_chars=sum(len(part) for part in completion_text),
                    content_chunks=len(completion_text),
                    first_token_ms=(
                        round((first_token_at - started) * 1000, 2)
                        if first_token_at is not None
                        else None
                    ),
                    usage_total_tokens=usage_total_tokens,
                    usage_included=request.wants_usage(),
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )

        except (asyncio.CancelledError, GeneratorExit):
            # İstemci koptu ya da sunucu iptal etti. `finally` içinde yield
            # etmek jeneratörü bozar: kapanış sırasında Python
            # "async generator ignored GeneratorExit" fırlatır ve bağlantı
            # askıda kalır. Bu yüzden burada yalnızca yukarı fırlatıyoruz;
            # `[DONE]` iptal edilmiş bir bağlantıya zaten yazılamaz.
            raise
        finally:
            try:
                # Yarıda bırakılan upstream akışını (bağlantı, eşzamanlılık izni,
                # süre sayacı) çöp toplayıcıya bırakmadan kapat. Akış zaten
                # bittiyse bu çağrı hiçbir şey yapmaz.
                if events is not None:
                    with contextlib.suppress(Exception):
                        await events.aclose()  # type: ignore[attr-defined]
            finally:
                # Sıra her çıkış yolunda bırakılır (bitti, hata, istemci koptu, iptal).
                self._release(turn)
                metrics.inc("apiwrapper_active_streams", -1.0)
                metrics.observe(
                    "apiwrapper_request_duration_seconds",
                    time.monotonic() - started,
                    {"endpoint": "completions"},
                )

        # `[DONE]` başarı ve hata yollarında, `finally` dışında gönderilir;
        # istemciler bunun varlığına güvenir.
        yield sse_done()

    # ------------------------------------------------------------ private
    def _prompt_preview(self, request: ChatCompletionRequest) -> str:
        """Token sayımı için **gerçek** prompt metnini üretir.

        Ham `messages` metnini toplamak prompt token'ını sistematik olarak düşük
        gösteriyordu: rol etiketleri, `System:` bloğu, araç tanımları, ipuçları
        ve `Assistant:` işareti sayılmıyordu. Burada aynı `build_prompt` çağrısı
        kullanılır; saf string işlemi olduğu için maliyeti yoktur.
        """
        try:
            return build_prompt(
                request.messages,
                tools=request.tools,
                include_hints=self.settings.unsupported_params == "hint",
                temperature=request.temperature,
                top_p=request.top_p,
                max_tokens=request.max_tokens or request.max_completion_tokens,
                response_format=request.response_format,
            )
        except Exception:  # pragma: no cover - sayım asla isteği bozmamalı
            return "\n".join(m.text_content() for m in request.messages)
