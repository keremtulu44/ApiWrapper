"""`max`'ın `UpstreamClient`'ından türetilmiş izole Agent Mode oturum istemcisi.

Ana `max` koduna (`app/upstream/client.py`) dokunmadan onun kanıtlanmış tüm
oturum mekanizmalarını miras alır:

* Hesaba özel `httpx.AsyncClient` ve bellek içi `Cookies` kavanozu (`Set-Cookie`
  ile dönen ~20 dk'lık `access_token` çerezlerini bellekte yaşatır),
* `app.upstream.headers` üzerinden tarayıcı parmak izi başlıkları (`sec-ch-ua`,
  `traceparent`, `tracestate`, `sec-fetch-*`),
* `DeploymentIdResolver` sürüm damgası kanaryası (`dpl_...` eskimesinde 409
  otomatik kurtarması),
* `app.upstream.client` hata sınıflandırması (401 kimlik, 403 captcha, 429 kota,
  409 sürüm damgası) ve gizli değer maskelemesi (`error_excerpt`).

Üzerine Agent Mode'un 3 adımlı protokolünü ekler:
1. `GET /api/chat/{chat_id}?recoverRun=false` (durum, `lastUserMessageId`, kanıt),
2. `POST /api/chat/{chat_id}/input` (`{"ok": true, "seq": N}`),
3. `GET /api/chat/{chat_id}/events` (`last-event-id` ile kesintisiz SSE/batch akışı).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx

from app.autopilot.curl_parse import AgentCurlConfig, parse_agent_curl
from app.autopilot.events_parser import (
    AgentChatState,
    AgentEventStreamDecoder,
    AgentMessage,
    AgentStreamDelta,
    parse_chat_state,
)
from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import parse_cookie_header, resolve_bearer_token
from app.upstream.client import (
    AUTH_MARKERS,
    AUTH_STATUS,
    CAPTCHA_STATUS,
    REFRESH_MARKERS,
    REFRESH_STATUS,
    error_excerpt,
)
from app.upstream.deployment import DeploymentIdResolver
from app.upstream.exceptions import (
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamQuotaExceeded,
    UpstreamRefreshRequired,
    UpstreamTimeout,
)
from app.upstream.headers import DEPLOYMENT_HEADERS, platform_from_ua, sec_ch_ua
from app.upstream.quota import find_quota_marker, normalize_markers, quota_error_message
from app.utils.ids import new_uuid4, new_uuid7

logger = get_logger(__name__)

DeltaCallback = Callable[[AgentStreamDelta], None]


class AgentSessionClient:
    """Tek bir açık Agent Mode sohbetine (sandbox oturumuna) bağlı HTTP istemcisi."""

    def __init__(
        self,
        config: AgentCurlConfig,
        *,
        connect_timeout: float = 15.0,
        read_timeout: float = 150.0,
        turn_timeout: float | None = None,
        events_window_seconds: int = 120,
        http2: bool = True,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.turn_timeout = turn_timeout
        self.events_window_seconds = max(30, min(300, int(events_window_seconds)))
        self.http2 = http2
        self.verify_tls = verify_tls
        self._transport = transport

        self._client: httpx.AsyncClient | None = None
        self._last_user_message_id: str = config.last_user_message_id
        self._last_event_id: str = config.last_event_id
        self._trigger_address: str = config.effective_trigger_address

        # Sürüm damgası kanaryası için izole Settings gölgesi (`max` ile aynı mantık)
        extra_str = ";".join(f"{k}={v}" for k, v in config.extra_headers.items())
        scheme: Literal["https", "http"] = "http" if config.scheme == "http" else "https"
        self._canary_settings = Settings(  # type: ignore[call-arg]
            _env_file=None,
            target_domain=config.target_domain,
            upstream_scheme=scheme,
            upstream_user_agent=config.user_agent,
            upstream_accept_language=config.accept_language,
            upstream_extra_headers=extra_str,
        )
        self.deployment = DeploymentIdResolver(self._canary_settings)
        self._quota_markers = normalize_markers(self._canary_settings.upstream_limit_markers)

    @classmethod
    def from_curl(
        cls,
        raw_curl: str,
        *,
        chat_id_override: str | None = None,
        turn_timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> AgentSessionClient:
        config = parse_agent_curl(raw_curl, chat_id_override=chat_id_override)
        return cls(config, turn_timeout=turn_timeout, transport=transport)

    # --------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        if self._client is not None:
            return
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=30.0,
            pool=15.0,
        )
        limits = httpx.Limits(
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30.0,
        )

        def _create(use_h2: bool) -> httpx.AsyncClient:
            kwargs: dict[str, Any] = {
                "http2": use_h2,
                "timeout": timeout,
                "limits": limits,
                "follow_redirects": False,
                "verify": self.verify_tls,
                "trust_env": False,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            return httpx.AsyncClient(**kwargs)

        try:
            client = _create(self.http2 and self._transport is None)
        except ImportError:
            client = _create(False)

        # Çerezleri cURL'deki sırayla bellek içi jar'a yükle (Python 3.11+ sıra korur).
        for name, value in parse_cookie_header(self.config.cookie).items():
            client.cookies.set(name, value, domain=self.config.target_domain, path="/")

        self._client = client
        logger.info(
            "agent_session_client_started",
            chat_id=self.config.chat_id,
            source_kind=self.config.source_endpoint_kind,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> AgentSessionClient:
        await self.startup()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.shutdown()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("AgentSessionClient başlatılmadı; önce startup() çağırın.")
        return self._client

    @property
    def last_user_message_id(self) -> str:
        return self._last_user_message_id

    @property
    def last_event_id(self) -> str:
        return self._last_event_id

    def account_identity(self) -> str:
        return self.config.account_identity()

    # ----------------------------------------------------------------- headers
    def _common_headers(
        self,
        *,
        accept: str = "*/*",
        deployment_id: str | None = None,
        include_origin: bool = False,
    ) -> dict[str, str]:
        ua = self.config.user_agent
        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        headers: dict[str, str] = {
            "accept": accept,
            "accept-language": self.config.accept_language,
            "cache-control": "no-cache",
        }
        if "dnt" in self.config.extra_headers:
            headers["dnt"] = self.config.extra_headers["dnt"]
        if include_origin:
            headers["origin"] = self.config.base_url
        headers.update(
            {
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": self.config.referer_url,
                "sec-ch-ua": self.config.sec_ch_ua_override or sec_ch_ua(ua),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": (
                    self.config.sec_ch_ua_platform_override or platform_from_ua(ua)
                ),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "traceparent": f"00-{trace_id}-{span_id}-01",
                "tracestate": "dd=s:1;o:rum",
                "user-agent": ua,
            }
        )
        # Yalnızca cURL'de açık authorization başlığı geldiyse ekle; cookie-only
        # Agent oturumlarında fazladan başlık uydurma (`max` ile aynı parmak izi kuralı).
        if self.config.access_token:
            token = resolve_bearer_token(explicit_token=self.config.access_token)
            if token:
                scheme = self.config.auth_scheme.strip()
                headers["authorization"] = f"{scheme} {token}" if scheme else token

        for key, value in self.config.extra_headers.items():
            headers[key] = value
        if deployment_id:
            for name in DEPLOYMENT_HEADERS:
                if name in headers:
                    headers[name] = deployment_id
        return headers

    def _note_set_cookie(self, response: httpx.Response, step: str) -> None:
        if response.headers.get("set-cookie"):
            logger.info(
                "agent_session_set_cookie",
                step=step,
                count=len(response.headers.get_list("set-cookie")),
            )

    def _classify_and_raise(self, status: int, body: str, url: str) -> None:
        excerpt = error_excerpt(body)
        lowered = (body or "").lower()

        quota_marker = find_quota_marker(body, self._quota_markers) if body else None
        if quota_marker is not None:
            raise UpstreamQuotaExceeded(
                quota_error_message(quota_marker, "Agent response body", excerpt),
                status_code=status,
                body=excerpt,
                url=url,
                marker=quota_marker,
            )

        for marker in REFRESH_MARKERS:
            if marker in lowered:
                raise UpstreamRefreshRequired(
                    "Agent upstream istemci sürümünü eski buldu (409 refresh_required).",
                    status_code=status,
                    body=excerpt,
                    url=url,
                    marker=marker,
                )
        if status in REFRESH_STATUS and not lowered.strip():
            raise UpstreamRefreshRequired(
                f"Agent upstream HTTP {status} döndürdü.",
                status_code=status,
                body=excerpt,
                url=url,
                marker=f"http_{status}",
            )

        is_captcha = status in CAPTCHA_STATUS and any(
            m in lowered for m in ("recaptcha", "captcha", "verification", "bot", "forbidden")
        )
        auth_body_match = (
            not lowered.strip()
            or any(m in lowered for m in AUTH_MARKERS)
            or "user not found" in lowered
        )
        if status in AUTH_STATUS and not is_captcha and auth_body_match:
            raise UpstreamAuthRejected(
                f"Agent oturumu reddedildi (HTTP {status}): {excerpt}",
                status_code=status,
                body=excerpt,
                url=url,
            )
        if is_captcha:
            raise RecaptchaRejected(
                f"Agent reCAPTCHA/bot doğrulaması reddedildi (HTTP {status}): {excerpt}",
                status_code=status,
                body=excerpt,
                url=url,
            )

        raise UpstreamHTTPError(
            f"Agent upstream HTTP {status} döndürdü: {excerpt}",
            status_code=status,
            body=excerpt,
            url=url,
        )

    # ----------------------------------------------------------- 1. state read
    async def fetch_state(self, *, recover_run: bool = False) -> AgentChatState:
        """`GET /api/chat/{chat_id}?recoverRun=false` ile sohbet durumunu okur."""
        url = self.config.state_url(recover_run=recover_run)
        reading = await self.deployment.refresh(self.client, self.config.base_url)
        headers = self._common_headers(accept="*/*", deployment_id=reading.value)

        try:
            response = await self.client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(
                f"Agent durum okuması zaman aşımına uğradı: {exc}", url=url
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamNetworkError(f"Agent durum ağ hatası: {exc}", url=url) from exc

        if response.status_code >= 400:
            body_text = response.text[:4096]
            # 409 sürüm damgası için tek seferlik tazeleme
            if response.status_code in REFRESH_STATUS or any(
                m in body_text.lower() for m in REFRESH_MARKERS
            ):
                fresh = await self.deployment.refresh(
                    self.client, self.config.base_url, force=True
                )
                if fresh.value and fresh.value != reading.value:
                    headers = self._common_headers(accept="*/*", deployment_id=fresh.value)
                    response = await self.client.get(url, headers=headers)
            if response.status_code >= 400:
                self._classify_and_raise(response.status_code, response.text[:4096], url)

        self._note_set_cookie(response, "fetch_state")
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamHTTPError(
                "Agent durum uç noktası geçerli JSON döndürmedi.",
                status_code=response.status_code,
                body=error_excerpt(response.text),
                url=url,
            ) from exc

        if not isinstance(payload, dict):
            raise UpstreamHTTPError(
                "Agent durum yanıtı beklenen nesne biçiminde değil.",
                status_code=response.status_code,
                url=url,
            )

        state = parse_chat_state(payload)
        if state.trigger_address:
            self._trigger_address = state.trigger_address
        if state.last_user_message_id:
            self._last_user_message_id = state.last_user_message_id
        if state.last_event_id:
            self._last_event_id = state.last_event_id
        return state

    async def connect(self, *, wait_if_busy: bool = True) -> AgentChatState:
        """Sohbete bağlanır, zincir kimliklerini senkronize eder ve hazırsa döner."""
        await self.startup()
        state = await self.fetch_state(recover_run=False)
        if wait_if_busy and state.has_pending_turn:
            logger.info(
                "agent_session_waiting_pending_turn",
                chat_id=self.config.chat_id,
                last_event_id=self._last_event_id,
            )
            await self.wait_for_turn_completion(
                baseline_assistant_id=None,
                require_new_assistant=False,
            )
            state = await self.fetch_state(recover_run=False)
        logger.info(
            "agent_session_connected",
            chat_id=state.chat_id or self.config.chat_id,
            message_count=len(state.messages),
            last_event_id=self._last_event_id,
            has_last_user_id=bool(self._last_user_message_id),
        )
        return state

    # ----------------------------------------------------------- 2. send input
    def _build_input_payload(self, text: str, message_id: str) -> dict[str, Any]:
        attempt_id = new_uuid7()
        # Şablondaki tüm metadata anahtarlarını ve sırasını koru; dinamik kimlikleri yenile.
        meta: dict[str, Any] = {}
        if self.config.metadata_template:
            meta.update(self.config.metadata_template)
        meta.setdefault("timezone", self.config.timezone)
        if self.config.arena_visit_id:
            meta.setdefault("arenaVisitId", self.config.arena_visit_id)
        meta.setdefault("clientPlatform", self.config.client_platform)
        meta.setdefault("submissionSource", self.config.submission_source)
        if self.config.cx:
            meta.setdefault("cx", self.config.cx)
        meta["timingBrowserAttemptId"] = attempt_id
        if self.config.recaptcha_token:
            meta["recaptchaV3Token"] = self.config.recaptcha_token
        meta["timingInputId"] = message_id

        msg_metadata: dict[str, Any] = {}
        if self._last_user_message_id:
            msg_metadata["lastUserMessageId"] = self._last_user_message_id

        return {
            "kind": "message",
            "payload": {
                "message": {
                    "parts": [{"type": "text", "text": text}],
                    "id": message_id,
                    "role": "user",
                    "metadata": msg_metadata,
                },
                "chatId": self._trigger_address,
                "trigger": "submit-message",
                "messageId": message_id,
                "metadata": meta,
            },
        }

    async def send_input(self, text: str) -> tuple[str, int | None]:
        """`POST /api/chat/{chat_id}/input` ile kullanıcı mesajını gönderir.

        Dönüş: `(gönderilen_message_id, seq)`.
        """
        prompt = text.strip()
        if not prompt:
            raise ValueError("Agent'a boş mesaj gönderilemez.")

        if not self._last_user_message_id:
            # Henüz connect() çağrılmadıysa son kullanıcı mesaj kimliğini öğren.
            await self.fetch_state(recover_run=False)

        url = self.config.input_url
        message_id = new_uuid7()
        payload = self._build_input_payload(prompt, message_id)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        reading = await self.deployment.refresh(self.client, self.config.base_url)
        stamp_retried = False

        while True:
            headers = self._common_headers(
                accept="*/*",
                deployment_id=reading.value if reading else None,
                include_origin=True,
            )
            headers["content-type"] = "application/json"
            headers["x-part-id"] = new_uuid4()

            try:
                response = await self.client.post(url, headers=headers, content=body)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(f"Agent /input zaman aşımına uğradı: {exc}", url=url) from exc
            except httpx.HTTPError as exc:
                raise UpstreamNetworkError(f"Agent /input ağ hatası: {exc}", url=url) from exc

            if response.status_code >= 400:
                err_body = response.text[:4096]
                if not stamp_retried and (
                    response.status_code in REFRESH_STATUS
                    or any(m in err_body.lower() for m in REFRESH_MARKERS)
                ):
                    stamp_retried = True
                    fresh = await self.deployment.refresh(
                        self.client, self.config.base_url, force=True
                    )
                    if fresh.value and (reading is None or fresh.value != reading.value):
                        reading = fresh
                        continue
                self._classify_and_raise(response.status_code, err_body, url)

            self._note_set_cookie(response, "send_input")
            seq: int | None = None
            try:
                ack = response.json()
                if isinstance(ack, dict):
                    if ack.get("ok") is False:
                        raise UpstreamHTTPError(
                            f"Agent /input isteği kabul etmedi: {error_excerpt(response.text)}",
                            status_code=response.status_code,
                            body=error_excerpt(response.text),
                            url=url,
                        )
                    if isinstance(ack.get("seq"), int):
                        seq = ack["seq"]
            except ValueError:
                pass

            self._last_user_message_id = message_id
            logger.info("agent_input_accepted", chat_id=self.config.chat_id, seq=seq)
            return message_id, seq

    # --------------------------------------------------------- 3. watch events
    async def _consume_events_once(
        self,
        *,
        remaining_seconds: float,
        on_delta: DeltaCallback | None = None,
    ) -> bool:
        """Tek bir `/events` penceresini dinler; `turn_complete` / `finish` geldiyse True."""
        url = self.config.events_url
        reading = await self.deployment.refresh(self.client, self.config.base_url)
        headers = self._common_headers(
            accept="text/event-stream",
            deployment_id=reading.value,
            include_origin=False,
        )
        window = min(self.events_window_seconds, max(15, int(remaining_seconds)))
        headers["timeout-seconds"] = str(window)
        if self._last_event_id:
            headers["last-event-id"] = str(self._last_event_id)

        decoder = AgentEventStreamDecoder()
        saw_finish = False
        finish_at = 0.0

        req_timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=min(float(window) + 25.0, max(20.0, remaining_seconds + 5.0)),
            write=15.0,
            pool=15.0,
        )
        request = self.client.build_request("GET", url, headers=headers, timeout=req_timeout)
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code >= 400:
                err_body = (await response.aread())[:4096].decode("utf-8", errors="replace")
                self._classify_and_raise(response.status_code, err_body, url)

            self._note_set_cookie(response, "events")
            async for chunk in response.aiter_bytes():
                for delta in decoder.feed(chunk):
                    if delta.seq_num is not None:
                        self._last_event_id = str(delta.seq_num)
                    if on_delta is not None:
                        on_delta(delta)
                    if delta.kind == "turn_complete":
                        return True
                    if delta.kind == "finish":
                        saw_finish = True
                        finish_at = time.monotonic()
                    if delta.kind == "error":
                        logger.warning("agent_stream_error_event", detail=error_excerpt(delta.text))
                if saw_finish and (time.monotonic() - finish_at) >= 2.5:
                    return True

            for delta in decoder.flush():
                if delta.seq_num is not None:
                    self._last_event_id = str(delta.seq_num)
                if on_delta is not None:
                    on_delta(delta)
                if delta.kind == "turn_complete":
                    return True
                if delta.kind == "finish":
                    saw_finish = True
        finally:
            await response.aclose()

        return saw_finish

    @staticmethod
    def _find_completed_assistant_reply(
        state: AgentChatState,
        *,
        after_user_id: str | None,
        baseline_assistant_id: str | None,
        require_new_assistant: bool,
    ) -> AgentMessage | None:
        if after_user_id:
            seen_user = False
            candidates: list[AgentMessage] = []
            for msg in state.messages:
                if msg.id == after_user_id:
                    seen_user = True
                    continue
                if seen_user and msg.role == "assistant":
                    candidates.append(msg)
            if seen_user:
                if candidates and not candidates[-1].pending:
                    return candidates[-1]
                return None

        last_asst = state.last_assistant_message
        if last_asst is None or last_asst.pending:
            return None
        if (
            require_new_assistant
            and baseline_assistant_id
            and last_asst.id == baseline_assistant_id
        ):
            return None
        return last_asst

    async def wait_for_turn_completion(
        self,
        *,
        after_user_id: str | None = None,
        baseline_assistant_id: str | None = None,
        require_new_assistant: bool = True,
        on_delta: DeltaCallback | None = None,
    ) -> AgentMessage:
        """Ajanın turunu bitirmesini bekler ve kesinleşmiş `AgentMessage`'ı döndürür.

        `self.turn_timeout` `None` veya `<= 0` olduğunda yapay süre sınırı uygulanmaz;
        karşı taraftaki Agent turunu bitirene kadar (veya kullanıcı `/durdur` diyene kadar)
        2 dakikalık `/events` pencereleriyle izlemeye devam eder.
        """
        deadline = (
            time.monotonic() + max(10.0, float(self.turn_timeout))
            if self.turn_timeout is not None and self.turn_timeout > 0
            else None
        )
        stream_failures = 0

        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise UpstreamTimeout(
                        f"Agent yanıtı {int(self.turn_timeout or 0)} sn içinde tamamlanmadı.",
                        url=self.config.events_url,
                    )
            else:
                remaining = float(self.events_window_seconds) + 30.0

            try:
                completed_signal = await self._consume_events_once(
                    remaining_seconds=remaining,
                    on_delta=on_delta,
                )
                stream_failures = 0
            except (UpstreamAuthRejected, RecaptchaRejected, UpstreamQuotaExceeded):
                raise
            except (
                httpx.HTTPError,
                UpstreamHTTPError,
                UpstreamNetworkError,
                UpstreamTimeout,
            ) as exc:
                stream_failures += 1
                logger.warning(
                    "agent_events_stream_interrupted",
                    attempt=stream_failures,
                    error_type=type(exc).__name__,
                )
                completed_signal = False
                await asyncio.sleep(min(5.0, 1.5 * stream_failures))

            # Her pencere sonunda (veya turn-complete geldiğinde) kayıt defterinden doğrula.
            state = await self.fetch_state(recover_run=False)
            reply = self._find_completed_assistant_reply(
                state,
                after_user_id=after_user_id,
                baseline_assistant_id=baseline_assistant_id,
                require_new_assistant=require_new_assistant,
            )
            if reply is not None:
                return reply

            if completed_signal:
                # `turn-complete` geldi ama state kaydı birkaç yüz ms gecikmiş olabilir.
                await asyncio.sleep(1.2)
                state = await self.fetch_state(recover_run=False)
                reply = self._find_completed_assistant_reply(
                    state,
                    after_user_id=after_user_id,
                    baseline_assistant_id=baseline_assistant_id,
                    require_new_assistant=require_new_assistant,
                )
                if reply is not None:
                    return reply

            await asyncio.sleep(1.0)

    async def send_and_wait(
        self,
        text: str,
        *,
        on_delta: DeltaCallback | None = None,
    ) -> AgentMessage:
        """Mesajı gönderir, akışı sonuna kadar izler ve tam asistan yanıtını döndürür."""
        pre_state = await self.fetch_state(recover_run=False)
        if pre_state.has_pending_turn:
            await self.wait_for_turn_completion(
                baseline_assistant_id=None,
                require_new_assistant=False,
                on_delta=on_delta,
            )
            pre_state = await self.fetch_state(recover_run=False)

        baseline_asst = pre_state.last_assistant_message
        baseline_id = baseline_asst.id if baseline_asst else None
        sent_user_id, _ = await self.send_input(text)

        return await self.wait_for_turn_completion(
            after_user_id=sent_user_id,
            baseline_assistant_id=baseline_id,
            require_new_assistant=True,
            on_delta=on_delta,
        )
