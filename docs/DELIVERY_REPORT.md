# ApiWrapper — Proje Teslimat Özeti

**Durum:** AŞAMA 2 tamamlandı · **Branch:** `arena/01a06835-apiwrapper` (push edildi)
**Kalite kapıları:** 68/68 test geçiyor · `ruff` temiz · `app/` içinde 0 TODO/placeholder
**Boyut:** `app/` 3.965 satır · `tests/` 823 satır · 63 dosya / 17 klasör

Bu rapordaki tüm kod blokları depodaki dosyaların **birebir mevcut içeriğidir**.

---

## 1. Proje Klasör Ağacı

```text
ApiWrapper/
├── app/
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── openai_to_upstream.py      # OpenAI isteği -> UpstreamPayload (+ doğrulama)
│   │   ├── prompt_builder.py          # messages[] -> tek prompt (rol/araç/multimodal)
│   │   └── upstream_to_openai.py      # chunk/completion/usage nesne üreticileri
│   ├── api/
│   │   ├── v1/
│   │   │   ├── __init__.py
│   │   │   ├── admin.py               # sessions/clear, recaptcha/invalidate, breaker/reset, config
│   │   │   ├── chat.py                # /v1/chat/completions + /v1/completions
│   │   │   ├── health.py              # /health, /healthz, /metrics, /
│   │   │   └── models.py              # /v1/models, /v1/models/{id}
│   │   ├── __init__.py
│   │   └── deps.py                    # app.state -> servis çözümü
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                  # pydantic-settings (NoDecode'lu CSV listeler)
│   │   ├── errors.py                  # OpenAI hata zarfı + exception handler'lar
│   │   ├── logging.py                 # structlog JSON + gizli veri maskeleme
│   │   ├── metrics.py                 # bağımlılıksız Prometheus registry
│   │   ├── middleware.py              # request-id, access log, body limit, rate limit
│   │   ├── rate_limit.py              # asyncio token-bucket
│   │   └── security.py                # sabit zamanlı API key doğrulama
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── openai.py                  # Request/Chunk/Completion/Usage/ModelCard
│   │   └── upstream.py                # UpstreamPayload, StreamEvent, EventType
│   ├── services/
│   │   ├── recaptcha/
│   │   │   ├── __init__.py            # sağlayıcı fabrikası
│   │   │   ├── base.py                # arayüz + TTL/tek-uçuş sarmalayıcı
│   │   │   ├── browser.py             # Playwright grecaptcha.execute()
│   │   │   ├── external.py            # harici çözücü HTTP API
│   │   │   └── static.py              # static (varsayılan) + noop
│   │   ├── __init__.py
│   │   ├── completion_service.py      # orkestrasyon + StopSequenceTracker + SSE üretimi
│   │   ├── model_registry.py          # models.yaml, alias çözümü
│   │   └── session_manager.py         # chat_id üretimi, LRU+TTL oturum cache'i
│   ├── upstream/
│   │   ├── __init__.py
│   │   ├── breaker.py                 # devre kesici (CLOSED/OPEN/HALF_OPEN)
│   │   ├── client.py                  # httpx havuzu, retry+jitter, streaming
│   │   ├── exceptions.py              # upstream istisna hiyerarşisi
│   │   ├── headers.py                 # tarayıcı taklidi başlıklar (sec-ch-ua ...)
│   │   └── stream_parser.py           # Vercel AI SDK data-stream çözücü
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── backoff.py                 # full jitter + Retry-After
│   │   ├── cache.py                   # LRUTTLCache + SingleFlightValue
│   │   ├── ids.py                     # uuid4 chat/message id, chatcmpl-
│   │   ├── sse.py                     # SSE kayıt biçimlendirme + [DONE]
│   │   └── tokens.py                  # tiktoken + sezgisel fallback
│   ├── __init__.py
│   └── main.py                        # app factory, lifespan, middleware montajı
├── ci/
│   └── github-actions-ci.yml          # (workflows izni yok -> ci/ altında)
├── config/
│   └── models.yaml                    # model adı <-> modelAId eşlemesi
├── docs/
│   └── ARCHITECTURE.md                # AŞAMA 1 mimari planı
├── scripts/
│   └── mock_upstream.py               # sahte upstream (AI SDK data-stream yayınlar)
├── tests/
│   ├── data/models.yaml
│   ├── __init__.py
│   ├── conftest.py                    # fixture'lar + ai_stream() üreteci
│   ├── test_adapters.py               # 13 test
│   ├── test_api.py                    # 24 test (respx ile uçtan uca)
│   ├── test_core.py                   # 19 test
│   └── test_stream_parser.py          # 12 test
├── .env.example
├── .gitignore
├── Dockerfile
├── Makefile
├── README.md
├── docker-compose.yml
└── pyproject.toml

17 klasör, 63 dosya
```

---

## 2. Kritik Dosya Kodları


### 2.1 `app/upstream/stream_parser.py` — Vercel AI SDK data-stream çözücü

```python
"""Vercel AI SDK "data stream" protokolü çözücüsü.

Protokol: her satır `<kod>:<json>` biçimindedir. Örnekler::

    f:{"messageId":"msg-123"}
    0:"Merhaba"
    0:" dünya"
    9:{"toolCallId":"c1","toolName":"search","args":{}}
    a:{"toolCallId":"c1","result":{}}
    e:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    d:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    3:"upstream error text"

Ayrıştırıcı ayrıca SSE (`data: {...}`) ve düz metin yayınlarını da tolere eder,
böylece upstream protokol değiştirse bile metin akışı korunur.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger
from app.schemas.upstream import EventType, StreamEvent

logger = get_logger(__name__)

#: Kod → olay türü eşlemesi.
CODE_MAP: dict[str, EventType] = {
    "0": EventType.TEXT,
    "f": EventType.START,
    "2": EventType.DATA,
    "8": EventType.MESSAGE_ANNOTATION,
    "9": EventType.TOOL_CALL,
    "a": EventType.TOOL_RESULT,
    "b": EventType.TOOL_CALL,
    "c": EventType.TOOL_CALL,
    "g": EventType.REASONING,
    "i": EventType.REASONING,
    "j": EventType.REASONING,
    "3": EventType.ERROR,
    "e": EventType.STEP_FINISH,
    "d": EventType.FINISH,
}

_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "content-filter": "content_filter",
    "content_filter": "content_filter",
    "tool-calls": "tool_calls",
    "tool_calls": "tool_calls",
    "error": "stop",
    "other": "stop",
    "unknown": "stop",
}


def normalize_finish_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _FINISH_REASON_MAP.get(value.strip().lower(), "stop")


def normalize_usage(raw: Any) -> dict[str, Any]:
    """Upstream usage sözlüğünü OpenAI alan adlarına çevirir."""
    if not isinstance(raw, dict):
        return {}
    prompt = raw.get("promptTokens", raw.get("prompt_tokens", raw.get("inputTokens")))
    completion = raw.get(
        "completionTokens", raw.get("completion_tokens", raw.get("outputTokens"))
    )
    total = raw.get("totalTokens", raw.get("total_tokens"))
    out: dict[str, Any] = {}
    if isinstance(prompt, (int, float)):
        out["prompt_tokens"] = int(prompt)
    if isinstance(completion, (int, float)):
        out["completion_tokens"] = int(completion)
    if isinstance(total, (int, float)):
        out["total_tokens"] = int(total)
    elif "prompt_tokens" in out or "completion_tokens" in out:
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out


def _extract_text(payload: Any) -> str:
    """Farklı biçimlerdeki metin taşıyıcılarından düz metni çıkarır."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "textDelta", "delta", "content", "value"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return ""
    if isinstance(payload, list):
        return "".join(_extract_text(item) for item in payload)
    return ""


def parse_line(line: str) -> StreamEvent | None:
    """Tek bir protokol satırını `StreamEvent`'e çevirir.

    Tanınmayan/boş satırlar için `None` döner.
    """
    line = line.strip()
    if not line:
        return None

    # SSE biçimi toleransı
    if line.startswith("data:"):
        line = line[5:].strip()
        if not line or line == "[DONE]":
            return StreamEvent(type=EventType.FINISH, raw_code="sse", finish_reason="stop")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return StreamEvent(type=EventType.TEXT, text=line, raw_code="sse")
        return _from_sse_payload(payload)

    if line.startswith((":", "event:", "id:", "retry:")):
        return None

    code, sep, rest = line.partition(":")
    if not sep or len(code) > 2:
        # Protokol dışı düz metin: içerik olarak kabul et.
        return StreamEvent(type=EventType.TEXT, text=line, raw_code="raw")

    code = code.strip()
    rest = rest.strip()
    try:
        payload: Any = json.loads(rest) if rest else None
    except json.JSONDecodeError:
        payload = rest

    etype = CODE_MAP.get(code, EventType.UNKNOWN)

    if etype is EventType.TEXT or etype is EventType.REASONING:
        return StreamEvent(type=etype, text=_extract_text(payload), raw_code=code, data=payload)

    if etype is EventType.ERROR:
        message = _extract_text(payload) or (
            json.dumps(payload, ensure_ascii=False) if payload is not None else "upstream error"
        )
        return StreamEvent(type=EventType.ERROR, text=message, raw_code=code, data=payload)

    if etype in (EventType.FINISH, EventType.STEP_FINISH):
        reason = None
        usage: dict[str, Any] = {}
        if isinstance(payload, dict):
            reason = normalize_finish_reason(payload.get("finishReason"))
            usage = normalize_usage(payload.get("usage"))
        return StreamEvent(
            type=etype,
            raw_code=code,
            data=payload,
            finish_reason=reason,
            usage=usage,
        )

    return StreamEvent(type=etype, raw_code=code, data=payload)


def _from_sse_payload(payload: Any) -> StreamEvent:
    """OpenAI benzeri SSE gövdesinden olay üretir (tolerans katmanı)."""
    if isinstance(payload, dict):
        if "error" in payload:
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            return StreamEvent(type=EventType.ERROR, text=str(message), raw_code="sse")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or choice.get("message") or {}
            text = delta.get("content") if isinstance(delta, dict) else ""
            reason = normalize_finish_reason(choice.get("finish_reason"))
            if reason:
                return StreamEvent(
                    type=EventType.FINISH,
                    text=text or "",
                    raw_code="sse",
                    finish_reason=reason,
                    usage=normalize_usage(payload.get("usage")),
                )
            return StreamEvent(type=EventType.TEXT, text=text or "", raw_code="sse")
    return StreamEvent(type=EventType.TEXT, text=_extract_text(payload), raw_code="sse")


class StreamDecoder:
    """Bayt parçalarını satırlara böler; çok baytlı UTF-8 sınırlarını korur."""

    def __init__(self) -> None:
        self._decoder = __import__("codecs").getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Yeni baytları işler ve tamamlanmış satırları döndürür."""
        if chunk:
            self._buffer += self._decoder.decode(chunk)
        return self._drain_lines()

    def _drain_lines(self) -> list[str]:
        lines: list[str] = []
        while True:
            idx = self._buffer.find("\n")
            if idx == -1:
                break
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            if line.endswith("\r"):
                line = line[:-1]
            lines.append(line)
        return lines

    def flush(self) -> list[str]:
        """Akış bitince tamponda kalanları döndürür."""
        self._buffer += self._decoder.decode(b"", final=True)
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


async def parse_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[StreamEvent]:
    """Bayt akışını `StreamEvent` akışına dönüştürür."""
    decoder = StreamDecoder()
    async for chunk in chunks:
        for line in decoder.feed(chunk):
            event = parse_line(line)
            if event is not None:
                yield event
    for line in decoder.flush():
        event = parse_line(line)
        if event is not None:
            yield event
```


### 2.2 `app/upstream/client.py` — httpx istek motoru

```python
"""Upstream HTTP istemcisi: havuzlu httpx.AsyncClient, retry, devre kesici, streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.upstream.breaker import CircuitBreaker
from app.upstream.exceptions import (
    CircuitOpen,
    RecaptchaRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamTimeout,
)
from app.upstream.headers import build_page_headers, build_stream_headers
from app.utils.backoff import full_jitter_delay, parse_retry_after

logger = get_logger(__name__)

#: Yeniden denenebilir HTTP durum kodları.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: reCAPTCHA yenilemesi gerektiren durum kodları.
CAPTCHA_STATUS = frozenset({401, 403})


class UpstreamClient:
    """Hedef servisle tüm HTTP iletişimini yöneten tekil istemci."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.breaker = CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            reset_timeout=settings.breaker_reset_timeout,
            enabled=settings.breaker_enabled,
        )
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_upstream))
        self._client: httpx.AsyncClient | None = None

    # ----------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        if self._client is not None:
            return
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout,
            read=self.settings.read_timeout,
            write=self.settings.write_timeout,
            pool=self.settings.pool_timeout,
        )
        limits = httpx.Limits(
            max_connections=self.settings.max_connections,
            max_keepalive_connections=self.settings.max_keepalive_connections,
            keepalive_expiry=30.0,
        )
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "limits": limits,
            "follow_redirects": True,
            "verify": self.settings.upstream_verify_tls,
            "trust_env": False,
        }
        if self.settings.upstream_proxy:
            kwargs["proxy"] = self.settings.upstream_proxy
        try:
            self._client = httpx.AsyncClient(http2=self.settings.upstream_http2, **kwargs)
        except ImportError:  # h2 kurulu değilse HTTP/1.1'e düş
            logger.warning("http2_unavailable_fallback_http11")
            self._client = httpx.AsyncClient(http2=False, **kwargs)
        logger.info("upstream_client_started", base_url=self.settings.base_url)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("upstream_client_closed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("UpstreamClient is not started. Call startup() first.")
        return self._client

    # ------------------------------------------------------------- helpers
    async def warmup(self, chat_id: str) -> None:
        """Sohbet sayfasını GET ederek çerezleri toplar. Hatalar yutulur."""
        url = self.settings.referer_url(chat_id)
        try:
            response = await self.client.get(
                url,
                headers=build_page_headers(self.settings),
                timeout=httpx.Timeout(self.settings.connect_timeout + 10.0),
            )
            logger.debug("upstream_warmup", status=response.status_code, chat_id=chat_id)
        except Exception as exc:  # pragma: no cover - ısıtma kritik değil
            logger.debug("upstream_warmup_failed", error=str(exc))

    @staticmethod
    def _is_captcha_body(status: int, body: str) -> bool:
        if status not in CAPTCHA_STATUS:
            return False
        lowered = body.lower()
        return any(
            marker in lowered
            for marker in ("recaptcha", "captcha", "verification", "bot", "forbidden")
        )

    # ------------------------------------------------------------- request
    @asynccontextmanager
    async def stream_completion(
        self, chat_id: str, payload: dict[str, object]
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Upstream'e istek atıp bayt akışı döndürür (retry + breaker dahil).

        `--data-raw` ile birebir aynı gövde `text/plain` olarak gönderilir.
        """
        if not await self.breaker.allows():
            metrics.inc("apiwrapper_upstream_errors_total", labels={"reason": "circuit_open"})
            raise CircuitOpen(
                "Upstream circuit breaker is open.",
                status_code=503,
            )

        url = self.settings.stream_url(chat_id)
        headers = build_stream_headers(self.settings, chat_id)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        last_error: Exception | None = None
        attempts = max(1, self.settings.retry_max_attempts)

        async with self._semaphore:
            for attempt in range(attempts):
                metrics.inc("apiwrapper_upstream_requests_total")
                request = self.client.build_request(
                    "POST", url, headers=headers, content=body
                )
                response: httpx.Response | None = None
                try:
                    response = await self.client.send(request, stream=True)
                except httpx.TimeoutException as exc:
                    last_error = UpstreamTimeout(f"Upstream timed out: {exc}")
                except httpx.HTTPError as exc:
                    last_error = UpstreamNetworkError(f"Upstream network error: {exc}")
                else:
                    status = response.status_code
                    if status < 400:
                        await self.breaker.record_success()
                        try:
                            yield response.aiter_bytes()
                        finally:
                            await response.aclose()
                        return

                    error_body = await self._read_error_body(response)
                    await response.aclose()
                    metrics.inc(
                        "apiwrapper_upstream_errors_total", labels={"status": str(status)}
                    )

                    if self._is_captcha_body(status, error_body):
                        await self.breaker.record_failure()
                        raise RecaptchaRejected(
                            "Upstream rejected the reCAPTCHA token.",
                            status_code=status,
                            body=error_body,
                        )

                    last_error = UpstreamHTTPError(
                        f"Upstream returned HTTP {status}.",
                        status_code=status,
                        body=error_body,
                    )
                    if status not in RETRYABLE_STATUS:
                        await self.breaker.record_failure()
                        raise last_error

                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if attempt < attempts - 1:
                        await asyncio.sleep(
                            retry_after
                            if retry_after is not None
                            else full_jitter_delay(
                                attempt,
                                self.settings.retry_base_delay,
                                self.settings.retry_max_delay,
                            )
                        )
                        continue

                # Ağ/timeout hatası yolu
                if attempt < attempts - 1:
                    logger.warning(
                        "upstream_retry",
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        error=str(last_error),
                    )
                    await asyncio.sleep(
                        full_jitter_delay(
                            attempt,
                            self.settings.retry_base_delay,
                            self.settings.retry_max_delay,
                        )
                    )

            await self.breaker.record_failure()
            raise last_error or UpstreamNetworkError("Upstream request failed.")

    @staticmethod
    async def _read_error_body(response: httpx.Response, limit: int = 4096) -> str:
        """Hata gövdesini sınırlı biçimde okur."""
        try:
            collected = bytearray()
            async for chunk in response.aiter_bytes():
                collected.extend(chunk)
                if len(collected) >= limit:
                    break
            return bytes(collected[:limit]).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return ""
```


### 2.3 `app/upstream/headers.py` — tarayıcı taklidi header yapısı

```python
"""Tarayıcı taklidi HTTP başlıklarının üretimi.

cURL analizindeki başlıklar birebir korunur; Chrome'un gönderdiği
`sec-*` başlıkları da eklenerek istek gerçekçi hale getirilir.
"""

from __future__ import annotations

import re

from app.core.config import Settings

_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")


def chrome_major_version(user_agent: str) -> str:
    match = _CHROME_VERSION_RE.search(user_agent)
    return match.group(1) if match else "152"


def platform_from_ua(user_agent: str) -> str:
    ua = user_agent.lower()
    if "windows" in ua:
        return '"Windows"'
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"'
    if "android" in ua:
        return '"Android"'
    if "linux" in ua:
        return '"Linux"'
    return '"Unknown"'


def sec_ch_ua(user_agent: str) -> str:
    version = chrome_major_version(user_agent)
    return (
        f'"Chromium";v="{version}", "Google Chrome";v="{version}", '
        '"Not?A_Brand";v="24"'
    )


def build_stream_headers(settings: Settings, chat_id: str) -> dict[str, str]:
    """Stream uç noktası için tam başlık kümesi."""
    ua = settings.upstream_user_agent
    headers: dict[str, str] = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "origin": settings.origin,
        "referer": settings.referer_url(chat_id),
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
    }
    if settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    headers.update(settings.parsed_extra_headers())
    return headers


def build_page_headers(settings: Settings) -> dict[str, str]:
    """Oturum ısıtma (HTML sayfa) istekleri için başlıklar."""
    ua = settings.upstream_user_agent
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
    }
    if settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    headers.update(settings.parsed_extra_headers())
    return headers
```


### 2.4 `app/services/session_manager.py` — UUID/oturum mantığı

```python
"""Sohbet oturumu yönetimi: chat_id üretimi, yeniden kullanım ve ısıtma."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.utils.cache import LRUTTLCache
from app.utils.ids import new_chat_id

logger = get_logger(__name__)


@dataclass(slots=True)
class Session:
    """Tek bir upstream sohbet oturumu."""

    chat_id: str
    created_at: float = field(default_factory=time.time)
    warmed: bool = False
    message_count: int = 0

    def touch(self) -> None:
        self.message_count += 1


class SessionManager:
    """`conversation_id` → upstream `chat_id` eşlemesini yönetir.

    `session_stateless=True` iken her istek için yeni bir chat_id üretilir
    (upstream tarafında durum tutulmaz, prompt tam geçmişi içerir).
    """

    def __init__(self, settings: Settings, warmup=None) -> None:
        self.settings = settings
        self._warmup = warmup
        self._cache: LRUTTLCache[str, Session] = LRUTTLCache(
            maxsize=settings.session_cache_size, ttl=settings.session_ttl
        )

    async def acquire(self, conversation_id: str | None = None) -> Session:
        """İstek için bir oturum döndürür (gerekirse oluşturur)."""
        if self.settings.session_stateless or not conversation_id:
            session = Session(chat_id=new_chat_id())
            await self._maybe_warm(session)
            session.touch()
            return session

        cached = await self._cache.get(conversation_id)
        if cached is not None:
            cached.touch()
            logger.debug(
                "session_reused", conversation_id=conversation_id, chat_id=cached.chat_id
            )
            return cached

        session = Session(chat_id=new_chat_id())
        await self._maybe_warm(session)
        session.touch()
        await self._cache.set(conversation_id, session)
        logger.debug("session_created", conversation_id=conversation_id, chat_id=session.chat_id)
        return session

    async def _maybe_warm(self, session: Session) -> None:
        if session.warmed or self._warmup is None:
            return
        await self._warmup(session.chat_id)
        session.warmed = True

    async def invalidate(self, conversation_id: str) -> None:
        await self._cache.delete(conversation_id)

    async def size(self) -> int:
        return await self._cache.size()

    async def clear(self) -> None:
        await self._cache.clear()
```


### 2.5 `app/api/v1/chat.py` — OpenAI uyumlu endpoint

```python
"""`/v1/chat/completions` ve `/v1/completions` uç noktaları."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_completion_service
from app.core.errors import InvalidRequestError
from app.core.logging import get_logger
from app.core.security import api_key_dependency
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.services.completion_service import CompletionService
from app.utils.sse import SSE_HEADERS

logger = get_logger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(api_key_dependency)])


@router.post(
    "/chat/completions",
    response_model=None,
    summary="Create a chat completion (OpenAI-compatible)",
)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    service: CompletionService = Depends(get_completion_service),
) -> ChatCompletionResponse | StreamingResponse:
    """OpenAI Chat Completions ile uyumlu tamamlama üretir."""
    if body.stream:
        async def is_disconnected() -> bool:
            try:
                return await request.is_disconnected()
            except Exception:  # pragma: no cover - transport'a bağlı
                return False

        return StreamingResponse(
            service.stream_completion(body, is_disconnected=is_disconnected),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    return await service.create_completion(body)


@router.post(
    "/completions",
    response_model=None,
    summary="Legacy text completion (mapped onto chat)",
)
async def create_text_completion(
    payload: dict[str, Any],
    request: Request,
    service: CompletionService = Depends(get_completion_service),
) -> Any:
    """Eski `/v1/completions` biçimini sohbet formatına eşler."""
    prompt = payload.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidRequestError("'prompt' must be a non-empty string.", param="prompt")

    chat_body = ChatCompletionRequest(
        model=str(payload.get("model", "")),
        messages=[ChatMessage(role="user", content=prompt)],
        stream=bool(payload.get("stream", False)),
        max_tokens=payload.get("max_tokens"),
        temperature=payload.get("temperature"),
        top_p=payload.get("top_p"),
        stop=payload.get("stop"),
        user=payload.get("user"),
    )

    if chat_body.stream:
        async def is_disconnected() -> bool:
            try:
                return await request.is_disconnected()
            except Exception:  # pragma: no cover
                return False

        return StreamingResponse(
            service.stream_completion(chat_body, is_disconnected=is_disconnected),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    completion = await service.create_completion(chat_body)
    return {
        "id": completion.id.replace("chatcmpl-", "cmpl-"),
        "object": "text_completion",
        "created": completion.created,
        "model": completion.model,
        "choices": [
            {
                "index": 0,
                "text": completion.choices[0].message.content or "",
                "finish_reason": completion.choices[0].finish_reason,
                "logprobs": None,
            }
        ],
        "usage": completion.usage.model_dump(),
    }
```


---

## 3. Konfigürasyon Örnekleri

### 3.1 `.env.example`

```bash
# ----------------------------------------------------------------- server
HOST=0.0.0.0
PORT=8000
DEBUG=false
LOG_LEVEL=INFO
LOG_JSON=true

# ------------------------------------------------------------------- auth
# Virgülle ayrılmış yerel API anahtarları (istemciler bunu Bearer olarak yollar).
API_KEYS=sk-local-dev-key
AUTH_DISABLED=false

# ------------------------------------------------------------------- CORS
CORS_ORIGINS=*
CORS_ALLOW_CREDENTIALS=false

# --------------------------------------------------------------- upstream
# ŞEMASIZ domain yazın (https:// otomatik eklenir).
TARGET_DOMAIN=example-llm.com
UPSTREAM_SCHEME=https
UPSTREAM_STREAM_PATH=/nextjs-api/stream/post-to-evaluation/{chat_id}
UPSTREAM_REFERER_PATH=/c/{chat_id}
UPSTREAM_USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36
# Oturum gerekiyorsa tarayıcıdan alınan ham Cookie başlığı:
UPSTREAM_COOKIE=
# Ek başlıklar "K1=V1;K2=V2" biçiminde:
UPSTREAM_EXTRA_HEADERS=
# UPSTREAM_PROXY=http://user:pass@127.0.0.1:8080
UPSTREAM_VERIFY_TLS=true
UPSTREAM_HTTP2=true

# --------------------------------------------------------------- timeouts
CONNECT_TIMEOUT=10
READ_TIMEOUT=300
WRITE_TIMEOUT=30
POOL_TIMEOUT=10
TOTAL_REQUEST_TIMEOUT=600
STREAM_IDLE_TIMEOUT=120

# ------------------------------------------------------------------ pools
MAX_CONNECTIONS=200
MAX_KEEPALIVE_CONNECTIONS=50
MAX_CONCURRENT_UPSTREAM=32

# ------------------------------------------------------ retry & resilience
RETRY_MAX_ATTEMPTS=3
RETRY_BASE_DELAY=0.5
RETRY_MAX_DELAY=8
BREAKER_ENABLED=true
BREAKER_FAILURE_THRESHOLD=5
BREAKER_RESET_TIMEOUT=30

# ------------------------------------------------------------- rate limit
RATE_LIMIT_ENABLED=true
RATE_LIMIT_RPM=120
RATE_LIMIT_BURST=30

# ------------------------------------------------------------------ input
MAX_BODY_BYTES=4194304
MAX_MESSAGES=400
MAX_PROMPT_CHARS=500000
# ignore | hint | error
UNSUPPORTED_PARAMS=ignore

# -------------------------------------------------------------- recaptcha
# static | noop | browser | external
RECAPTCHA_PROVIDER=static
RECAPTCHA_STATIC_TOKEN=
RECAPTCHA_TOKEN_TTL=100
# browser sağlayıcısı için:
RECAPTCHA_SITE_KEY=
RECAPTCHA_ACTION=chat
RECAPTCHA_BROWSER_TIMEOUT=45
# external sağlayıcısı için:
RECAPTCHA_EXTERNAL_URL=
RECAPTCHA_EXTERNAL_API_KEY=
RECAPTCHA_EXTERNAL_TIMEOUT=60

# ---------------------------------------------------------------- session
SESSION_STATELESS=true
SESSION_TTL=1800
SESSION_CACHE_SIZE=1024

# ----------------------------------------------------------------- models
MODELS_FILE=config/models.yaml
METRICS_ENABLED=true
```

### 3.2 `config/models.yaml`

```yaml
# Model kayıt defteri.
#
# id           → istemcinin "model" alanında göndereceği ad (OpenAI tarafı)
# upstream_id  → upstream gövdesindeki "modelAId" değeri
# aliases      → aynı modele işaret eden alternatif adlar (opsiyonel)
#
# Gerçek upstream model kimliklerini hedef servisin ağ trafiğinden alıp buraya yazın.
# Kodda hiçbir model kimliği gömülü DEĞİLDİR; tek kaynak bu dosyadır.

default: gpt-4o-mini

models:
  - id: gpt-4o-mini
    upstream_id: REPLACE_WITH_UPSTREAM_MODEL_ID_1
    owned_by: upstream
    description: Hızlı ve ucuz genel amaçlı model.
    aliases:
      - gpt-4o-mini-2024-07-18
      - fast

  - id: gpt-4o
    upstream_id: REPLACE_WITH_UPSTREAM_MODEL_ID_2
    owned_by: upstream
    description: Yüksek kaliteli genel amaçlı model.
    aliases:
      - gpt-4
      - default

  - id: reasoning-pro
    upstream_id: REPLACE_WITH_UPSTREAM_MODEL_ID_3
    owned_by: upstream
    description: Uzun düşünme zincirli akıl yürütme modeli.
    aliases:
      - o1
      - reasoner
```

---

## 4. Çalıştırma ve Test Talimatı

### 4.1 Kurulum

```bash
git clone <repo> && cd ApiWrapper
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,tokens]"

cp .env.example .env          # TARGET_DOMAIN, API_KEYS, RECAPTCHA_STATIC_TOKEN
$EDITOR config/models.yaml    # REPLACE_WITH_UPSTREAM_MODEL_ID_* -> gerçek modelAId
```

### 4.2 Uvicorn ile başlatma

```bash
make run
# eşdeğeri:
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log

# geliştirme (hot reload):
make dev
```

Açılış logu (structlog JSON):

```json
{"base_url": "https://<TARGET_DOMAIN>", "event": "upstream_client_started", "level": "info"}
{"count": 3, "event": "models_loaded", "level": "info"}
{"version": "1.0.0", "models": 3, "recaptcha_provider": "static", "event": "application_started"}
```

### 4.3 Docker ile başlatma

```bash
make docker                 # docker compose up --build
# veya:
docker build -t apiwrapper:1.0.0 .
docker run --rm -p 8000:8000 --env-file .env \
  -v "$PWD/config:/app/config:ro" apiwrapper:1.0.0
```

Konteyner non-root (uid 10001) çalışır, `no-new-privileges` etkindir ve
`/healthz` üzerinden HEALTHCHECK tanımlıdır.

### 4.4 Upstream olmadan deneme (sahte upstream)

```bash
python scripts/mock_upstream.py &        # :9100, AI SDK data-stream yayınlar
# .env: TARGET_DOMAIN=127.0.0.1:9100  UPSTREAM_SCHEME=http  RECAPTCHA_PROVIDER=noop
make run &
python scripts/test_client.py
```

### 4.5 `scripts/test_client.py` — gerçek çıktı

```text
=== ApiWrapper doğrulama · http://127.0.0.1:8000/v1 ===

[1] GET /models
  PASS model listesi dolu ['gpt-4o-mini']

[2] POST /chat/completions (stream=false)
  PASS object == chat.completion
  PASS içerik boş değil 155 karakter, 0.35s
  PASS usage.total_tokens > 0 {"prompt_tokens": 7, "completion_tokens": 38, "total_tokens": 45}
  PASS finish_reason == stop

[3] POST /chat/completions (stream=true, include_usage)
  PASS content-type text/event-stream
  PASS ilk chunk role=assistant
  PASS metin biriktirildi 155 karakter
  PASS finish_reason chunk'ı var
  PASS usage chunk'ı var
  PASS [DONE] alındı
    TTFT: 28 ms · 20 chunk

[4] Hata yolları
  PASS bilinmeyen model 404 model_not_found
  PASS boş messages 400
  PASS anahtarsız istek 401

=== 14 geçti, 0 başarısız ===
```

### 4.6 cURL — streaming istek

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "stream": true,
    "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "Merhaba"}]
  }'
```

**Beklenen SSE yanıt formatı** (aşağısı canlı alınmış gerçek çıktıdır):

```text
data: {"id":"chatcmpl-Mh9JxlmOUUIphuK3IvB6evJl","object":"chat.completion.chunk","created":1788456249,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}

data: {"id":"chatcmpl-Mh9JxlmOUUIphuK3IvB6evJl","object":"chat.completion.chunk","created":1788456249,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"[live-model-alpha] "}}]}

data: {"id":"chatcmpl-Mh9JxlmOUUIphuK3IvB6evJl","object":"chat.completion.chunk","created":1788456249,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"Merhaba! "}}]}

... (her token için bir chunk) ...

data: {"id":"chatcmpl-Mh9JxlmOUUIphuK3IvB6evJl","object":"chat.completion.chunk","created":1788456249,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: {"id":"chatcmpl-Mh9JxlmOUUIphuK3IvB6evJl","object":"chat.completion.chunk","created":1788456249,"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":38,"total_tokens":39}}

data: [DONE]
```

Sıra garantisi: **rol chunk'ı → N adet içerik chunk'ı → finish_reason chunk'ı →
(istenirse) usage chunk'ı → `[DONE]`**. `[DONE]` bir `finally` bloğundan yayınlanır,
yani hata/iptal durumunda bile mutlaka gönderilir.

**Non-streaming yanıt** (gerçek çıktı):

```json
{
    "id": "chatcmpl-Oio0cwq7gxQhtyVfzM7kfoKJ",
    "object": "chat.completion",
    "created": 1788456250,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "...", "tool_calls": null, "refusal": null},
            "finish_reason": "stop",
            "logprobs": null
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 38, "total_tokens": 39},
    "system_fingerprint": "fp_100_estimate"
}
```

### 4.7 Resmi OpenAI SDK ile

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-local-dev-key")

for chunk in client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "FastAPI'yi üç cümlede anlat."}],
    stream=True,
):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

> Bu senaryo sahte upstream'e karşı **resmi `openai` paketiyle** çalıştırılıp
> doğrulandı: `models.list()`, non-stream, stream + `include_usage`, alias'lı model
> ve `NotFoundError` yolu dahil.

### 4.8 Otomatik test paketi

```bash
make test     # 68 test
make lint     # ruff
```

| Dosya | Test | Kapsam |
|---|---|---|
| `tests/test_stream_parser.py` | 12 | protokol kodları, SSE toleransı, bozuk JSON, UTF-8 sınırı |
| `tests/test_adapters.py` | 13 | prompt düzleştirme, multimodal, araçlar, limitler, wire formatı |
| `tests/test_core.py` | 19 | cache, tek-uçuş, rate limit, breaker, registry, stop tracker |
| `tests/test_api.py` | 24 | uçtan uca API (respx ile upstream mock), auth, hata yolları |

---

## 5. Edge-Case / Risk Analizi

**reCAPTCHA token süresinin dolması.** Statik token ~2 dakikada geçersizleşir; upstream
401/403 döndüğünde `UpstreamClient._is_captcha_body()` gövdedeki `recaptcha|captcha|
verification|bot|forbidden` işaretlerini tanıyıp `RecaptchaRejected` fırlatır, sağlayıcının
TTL cache'i anında invalidate edilir ve istek **tam olarak bir kez** yeni token'la yeniden
denenir (`_events(..., allow_captcha_retry=False)` ile sonsuz döngü imkânsız). İkinci
başarısızlıkta istemciye eyleme dönük `503 recaptcha_rejected` mesajı gider; `browser`
sağlayıcısında ise token otomatik yenilendiği için bu çoğunlukla şeffaf biçimde iyileşir.

**Cloudflare / bot engeli.** İstekler `origin`, `referer`, `sec-ch-ua*`, `sec-fetch-*` ve
`priority` başlıklarıyla Chrome trafiğini taklit eder; `UPSTREAM_COOKIE`, `UPSTREAM_PROXY`
ve oturum ısıtma (`warmup()` ile sohbet sayfasına önden GET) desteklenir. Kalıcı engelde
(403/429) 5 ardışık hatadan sonra devre kesici açılır ve 30 saniye boyunca istekler upstream'e
hiç gitmeden hızlıca `503` alır — bu hem hedefi yormaz hem de IP/oturum itibarının daha fazla
yıpranmasını önler. Not: httpx TLS parmak izini (JA3) taklit etmez; agresif korumalarda
`curl_cffi`'ye geçiş gerekebilir — mimari bunu tek dosyada (`upstream/client.py`) izole eder.

**Stream kesintisi.** Akış ortasında bağlantı koparsa `StreamDecoder` tamponundaki yarım satır
`flush()` ile değerlendirilir, o ana kadar üretilmiş tüm token'lar istemcide kalır ve akış
`finish_reason` + `[DONE]` ile düzgünce kapatılır — istemci asla asılı kalmaz. İki token arası
`STREAM_IDLE_TIMEOUT` (vars. 120 sn) sessizlik `asyncio.wait_for` ile yakalanıp `504`'e çevrilir.
Kritik nokta: yanıt gövdesi **akmaya başladıktan sonra** retry yapılmaz (aksi halde kullanıcı
metni tekrar görürdü); retry yalnızca bağlantı/başlık aşamasındaki 429/5xx ve ağ hatalarında,
üstel geri çekilme + full jitter ve `Retry-After` saygısıyla uygulanır.

**İstemci kopması ve kaynak sızıntısı.** Her chunk'tan önce `request.is_disconnected()`
kontrol edilir; istemci sekmeyi kapatırsa üretici durur, `asynccontextmanager`'ın `finally`
bloğu upstream yanıtını `aclose()` ile kapatır ve semafor slotu serbest bırakılır. Böylece
terk edilmiş istekler upstream bağlantı havuzunu (`MAX_CONCURRENT_UPSTREAM`) tüketemez.

---

## Ek Notlar

1. **CI dosyası:** GitHub App'in `workflows` izni olmadığı için `.github/workflows/ci.yml`
   push edilemedi. Dosya `ci/github-actions-ci.yml` altında hazır; etkinleştirmek için:
   `mkdir -p .github/workflows && git mv ci/github-actions-ci.yml .github/workflows/ci.yml`
2. **Token sayımı:** `tiktoken` kuruluysa `cl100k_base` ile gerçek sayım yapılır; BPE dosyası
   indirilemezse sezgisel tahmine düşer ve bu `system_fingerprint` alanında
   (`fp_100_estimate` / `fp_100_tiktoken`) şeffafça raporlanır.
3. **Yasal:** Hedef servisin kullanım koşullarına ve oran sınırlarına uyum kullanıcının
   sorumluluğundadır.
