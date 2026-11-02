"""OpenAI uyumlu hata hiyerarşisi ve FastAPI exception handler'ları."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)


def error_payload(
    message: str,
    err_type: str,
    *,
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    """OpenAI'nin `{"error": {...}}` zarfını üretir."""
    return {
        "error": {
            "message": message,
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


class APIWrapperError(Exception):
    """Tüm uygulama hatalarının tabanı."""

    status_code: int = 500
    err_type: str = "internal_error"
    code: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        err_type: str | None = None,
        param: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if err_type is not None:
            self.err_type = err_type
        if code is not None:
            self.code = code
        self.param = param
        self.headers = headers or {}

    def to_payload(self) -> dict[str, Any]:
        return error_payload(self.message, self.err_type, param=self.param, code=self.code)


class AuthenticationError(APIWrapperError):
    status_code = 401
    err_type = "invalid_request_error"
    code = "invalid_api_key"


class PermissionError_(APIWrapperError):
    status_code = 403
    err_type = "invalid_request_error"
    code = "permission_denied"


class InvalidRequestError(APIWrapperError):
    status_code = 400
    err_type = "invalid_request_error"
    code = "invalid_request"


class ModelNotFoundError(APIWrapperError):
    status_code = 404
    err_type = "invalid_request_error"
    code = "model_not_found"


class PayloadTooLargeError(APIWrapperError):
    status_code = 413
    err_type = "invalid_request_error"
    code = "payload_too_large"


class RateLimitError(APIWrapperError):
    status_code = 429
    err_type = "rate_limit_error"
    code = "rate_limit_exceeded"


class UpstreamError(APIWrapperError):
    status_code = 502
    err_type = "upstream_error"
    code = "upstream_failure"


class UpstreamTimeoutError(UpstreamError):
    status_code = 504
    code = "upstream_timeout"


class UpstreamUnavailableError(UpstreamError):
    status_code = 503
    code = "upstream_unavailable"


class UpstreamQuotaError(UpstreamError):
    """Upstream hesabı geçici olarak kısıtladı (kota penceresi dolu).

    İstemciler `Retry-After` başlığına bakarak ne kadar bekleyeceğini görebilir;
    bu yüzden hata 429 olarak yüzeye çıkar.
    """

    status_code = 429
    err_type = "rate_limit_error"
    code = "upstream_quota_reached"


class RecaptchaError(APIWrapperError):
    status_code = 503
    err_type = "upstream_error"
    code = "recaptcha_unavailable"


def register_exception_handlers(app: FastAPI) -> None:
    """FastAPI uygulamasına tüm hata yakalayıcıları bağlar."""

    @app.exception_handler(APIWrapperError)
    async def _app_error(_request: Request, exc: APIWrapperError) -> JSONResponse:
        logger.warning(
            "request_failed",
            error=exc.message,
            error_type=exc.err_type,
            status_code=exc.status_code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query")]
        param = ".".join(loc) if loc else None
        message = first.get("msg", "Invalid request body.")
        return JSONResponse(
            status_code=422,
            content=error_payload(
                message, "invalid_request_error", param=param, code="invalid_request"
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        err_type = "invalid_request_error" if exc.status_code < 500 else "internal_error"
        message = str(exc.detail)
        code = None
        if exc.status_code == 404:
            path = request.url.path
            message = (
                f"Unknown endpoint {request.method} {path}. "
                "OpenAI-compatible routes: POST /v1/chat/completions, GET /v1/models "
                "(the /v1 prefix is optional)."
            )
            code = "not_found"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(message, err_type, code=code),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=str(exc))
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "An internal server error occurred.", "internal_error", code="internal_error"
            ),
        )
