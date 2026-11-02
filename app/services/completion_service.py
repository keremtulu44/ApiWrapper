"""Sohbet tamamlama orkestrasyonu: stream ve non-stream akışlar."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

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
from app.core.errors import (
    APIWrapperError,
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
from app.services.model_registry import ModelRegistry
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.client import UpstreamClient
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
from app.utils.tokens import count_tokens, tiktoken_available

logger = get_logger(__name__)

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
    ) -> None:
        self.settings = settings
        self.upstream = upstream
        self.registry = registry
        self.sessions = sessions
        self.recaptcha = recaptcha
        self.accounts = accounts if accounts is not None else AccountPool(settings)

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
            return RecaptchaError(
                "The upstream service rejected the reCAPTCHA token. "
                "Refresh RECAPTCHA_STATIC_TOKEN or use the 'browser'/'external' provider.",
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

    def _pick_account(self, exclude_slot: int | None = None) -> Account | None:
        """Kullanılabilir bir hesap seçer; havuz boşsa `None` (eski davranış)."""
        account = self.accounts.pick(exclude_slot=exclude_slot)
        if account is None and self.accounts.size:
            # Tüm hesaplar dinlenmede: ne zaman açılacağını söyleyerek reddet.
            wait = self.accounts.next_cooldown_end()
            raise UpstreamQuotaExceeded(
                "All upstream accounts are temporarily locked by the provider. "
                f"Earliest retry in {int(wait)}s.",
                retry_after=wait or None,
            )
        return account

    async def _events(
        self,
        request: ChatCompletionRequest,
        client_identity: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Upstream olay akışı.

        Üç otomatik kurtarma yolu vardır: captcha reddinde token bir kez
        yenilenir, hesap kısıtlamasında başka hesaba geçilir. Devir ve captcha
        yenileme **yalnızca istemciye henüz tek bir olay gönderilmediyse**
        yapılır; aksi halde iki farklı yanıtın parçaları birbirine karışırdı.

        Kota tespiti de burada yapılır (HTTP hata gövdesi + hata olayı + düz
        metin delta'sı) ki `report_quota` her yolda çağrılsın.
        """
        # Geçersiz istekleri hesap seçimi, sohbet açma ve CAPTCHA üretiminden
        # önce reddet. Aksi halde örneğin boş `messages`, hesaplar cooldown'dayken
        # doğru 400 yerine yanıltıcı bir 429 döndürür ve gereksiz iş yapar.
        validate_request(request, self.settings)
        max_switches = max(0, self.settings.account_max_switches)
        switches = 0
        captcha_retried = False
        emitted = 0
        account = self._pick_account()

        while True:
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
                        # Python 3.10'da asyncio.TimeoutError, yerleşik TimeoutError
                        # ile aynı sınıf değildir; ikisini de yakala.
                        except (TimeoutError, asyncio.TimeoutError) as exc:
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
            except UpstreamQuotaExceeded:
                if account is not None:
                    self.accounts.report_quota(account.slot)
                retryable = emitted == 0 and switches < max_switches
                replacement = self._pick_account() if retryable else None
                if replacement is None or (
                    account is not None and replacement.slot == account.slot
                ):
                    raise
                switches += 1
                logger.warning(
                    "account_switched",
                    from_account=account_label,
                    to_account=replacement.label,
                    switch=switches,
                )
                metrics.inc(
                    "apiwrapper_account_switches_total", labels={"reason": "switch"}
                )
                account = replacement
                continue
            except UpstreamAuthRejected:
                # Oturum kimliği reddedildi (401). Kota yolundan farkı: hesabı
                # cezalandırmıyoruz (token global olarak düşmüş olabilir), ama
                # başka hesap varsa onu denemek toplam kesintiyi önler.
                retryable = emitted == 0 and switches < max_switches and account is not None
                # Burada bilinçli olarak `self.accounts.pick` kullanılıyor, `_pick_account`
                # değil: `_pick_account` havuz boşken UpstreamQuotaExceeded fırlatır ve bu,
                # tam da bu blokta orijinal 401'i ezip istemciye "kota doldu" (429) olarak
                # görünmesine yol açıyordu. Oturum ölüyse cevap "unauthorized" olmalı.
                replacement = (
                    self.accounts.pick(exclude_slot=account.slot)
                    if retryable and account is not None
                    else None
                )
                if replacement is None:
                    raise
                switches += 1
                metrics.inc(
                    "apiwrapper_account_switches_total", labels={"reason": "auth"}
                )
                logger.warning(
                    "account_switched",
                    from_account=account_label,
                    to_account=replacement.label,
                    switch=switches,
                    reason="auth_rejected",
                )
                account = replacement
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
        self, request: ChatCompletionRequest, client_identity: str | None = None
    ) -> ChatCompletionResponse:
        """Tam yanıtı biriktirip tek seferde döndürür."""
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
            async for event in self._events(request, client_identity=client_identity):
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
        content = "".join(parts)

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
    def stream_completion(
        self,
        request: ChatCompletionRequest,
        is_disconnected=None,
        client_identity: str | None = None,
    ) -> AsyncIterator[str]:
        """SSE gövdesi. Model çözümü burada, yanıt başlamadan yapılır.

        `_stream_completion` bir async generator olduğu için içindeki `yield`lara kadar
        hiçbir satırı çalışmaz. Model çözümü generator'ın içinde kalsaydı, HTTP 200 ve
        başlıklar gönderildikten sonra `ModelNotFoundError` fırlar; istemci boş/kesik bir
        akış görür ve sunucu "response already started" hatası loglardı. Bu yüzden model
        burada, döndürmeden önce çözülür ve hata normal 404 yolundan çıkar.
        """
        self.registry.resolve(request.model)
        return self._stream_completion(
            request, is_disconnected=is_disconnected, client_identity=client_identity
        )

    async def _stream_completion(
        self,
        request: ChatCompletionRequest,
        is_disconnected=None,
        client_identity: str | None = None,
    ) -> AsyncIterator[str]:
        """SSE gövdesini parça parça üretir. `[DONE]` her koşulda gönderilir."""
        started = time.monotonic()
        completion_id = new_completion_id()
        entry = self.registry.resolve(request.model)
        created = now_ts()
        stopper = StopSequenceTracker(request.stop_sequences())

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
                async for event in self._events(request, client_identity=client_identity):
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
                        emitted = stopper.process(event.text)
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

                tail = stopper.flush()
                if tail:
                    completion_text.append(tail)
                    yield sse_event(
                        _dump(make_content_chunk(completion_id, entry.id, created, tail))
                    )

            except Exception as exc:
                api_error = self._translate(exc)
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
