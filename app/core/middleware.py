"""Özel ASGI middleware'leri: request-id, erişim logu, gövde limiti, rate limit."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import Settings
from app.core.errors import error_payload
from app.core.logging import get_logger, request_id_ctx
from app.core.metrics import metrics
from app.core.rate_limit import TokenBucketLimiter
from app.core.security import ANONYMOUS, extract_api_key
from app.utils.ids import new_request_id

logger = get_logger(__name__)

Handler = Callable[[Request], Awaitable[Response]]


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Gelen `X-Request-ID` başlığını yayar veya yenisini üretir."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        incoming = request.headers.get("x-request-id", "").strip()
        request_id = incoming[:128] if incoming else new_request_id()
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["x-request-id"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Yapılandırılmış erişim logu ve gecikme metrikleri."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        started = time.monotonic()
        path = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            duration = time.monotonic() - started
            logger.exception(
                "request_error", method=request.method, path=path, duration_ms=duration * 1000
            )
            metrics.inc("apiwrapper_requests_total", labels={"path": path, "status": "500"})
            raise

        duration = time.monotonic() - started
        metrics.inc(
            "apiwrapper_requests_total",
            labels={"path": path, "status": str(response.status_code)},
        )
        metrics.observe("apiwrapper_request_duration_seconds", duration, {"path": path})
        if path not in ("/health", "/healthz", "/readyz", "/metrics"):
            logger.info(
                "request",
                method=request.method,
                path=path,
                status=response.status_code,
                duration_ms=round(duration * 1000, 2),
                client=request.client.host if request.client else None,
            )
        response.headers["x-response-time-ms"] = f"{duration * 1000:.2f}"
        return response


def _declared_content_length(scope: Any) -> int | None:
    """`Content-Length` başlığını (varsa) tamsayıya çevirir."""
    for name, value in scope.get("headers") or []:
        if name == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def _reject_too_large(
    scope: Any, receive: Any, send: Any, limit: int, declared: int | None = None
) -> None:
    detail = f" ({declared} bytes declared)" if declared else ""
    response = JSONResponse(
        status_code=413,
        content=error_payload(
            f"Request body too large{detail}; limit is {limit} bytes.",
            "invalid_request_error",
            code="payload_too_large",
        ),
    )
    await response(scope, receive, send)


class BodyLimitMiddleware:
    """Gövde boyutu sınırı — başlığa ek olarak **akan baytı** da sayar.

    Önceki sürüm yalnızca `Content-Length` başlığına bakıyordu. `Transfer-Encoding:
    chunked` ile gönderilen bir istek sınırı tamamen atlıyor, 4 MB'lık JSON
    doğrudan Pydantic'e giriyordu (küçük bir instance'ta OOM riski).

    Çözüm saf ASGI'de: gövde `max_bytes` sınırına kadar okunur ve uygulamaya
    **tekrar oynatılır**. Sınır aşılırsa uygulama hiç çağrılmadan `413` döner.
    (`receive` içinden istisna fırlatmak işe yaramıyordu: FastAPI gövde ayrıştırma
    hatalarını yutup 400'e çeviriyor.)
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self.max_bytes
        declared = _declared_content_length(scope)
        if declared is not None and declared > limit:
            await _reject_too_large(scope, receive, send, limit, declared)
            return

        buffered: list[Any] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") != "http.request":
                # İstemci koptu: olduğu gibi uygulamaya ilet.
                buffered.append(message)
                break
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                await _reject_too_large(scope, receive, send, limit, total)
                return
            buffered.append(message)
            more_body = bool(message.get("more_body", False))

        pending = list(buffered)

        async def replay() -> Any:
            if pending:
                return pending.pop(0)
            return await receive()

        await self.app(scope, replay, send)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """API anahtarı (yoksa IP) başına token-bucket sınırlama."""

    EXEMPT_PATHS = frozenset(
        {
            "/health",
            "/healthz",
            "/readyz",
            "/metrics",
            "/",
            "/docs",
            "/openapi.json",
            "/redoc",
        }
    )

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.limiter = TokenBucketLimiter(
            rate_per_minute=settings.rate_limit_rpm, burst=settings.rate_limit_burst
        )

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        if not self.settings.rate_limit_enabled or request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        key = extract_api_key(request)
        if not key:
            key = f"ip:{request.client.host}" if request.client else f"ip:{ANONYMOUS}"

        allowed, retry_after = await self.limiter.acquire(key)
        if not allowed:
            logger.warning("rate_limited", path=request.url.path, retry_after=retry_after)
            return JSONResponse(
                status_code=429,
                content=error_payload(
                    "Rate limit exceeded. Please slow down and retry.",
                    "rate_limit_error",
                    code="rate_limit_exceeded",
                ),
                headers={"retry-after": str(max(1, int(retry_after + 0.999)))},
            )
        return await call_next(request)
