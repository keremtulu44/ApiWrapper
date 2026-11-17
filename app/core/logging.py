"""structlog tabanlı yapılandırılmış loglama + hassas veri maskeleme."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "api_key",
        "apikey",
        "x-api-key",
        "recaptchav3token",
        "recaptcha_token",
        "recaptcha_static_token",
        "recaptcha_external_api_key",
        "token",
        "password",
        "secret",
    }
)

_MASK = "***"


def mask_value(value: Any) -> str:
    """Gizli değerin yalnızca uzunluk ipucunu bırakır."""
    if value is None:
        return _MASK
    text = str(value)
    if len(text) <= 8:
        return _MASK
    return f"{text[:3]}{_MASK}{text[-2:]}"


def _mask_mapping(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            out[key] = mask_value(value)
        elif isinstance(value, dict):
            out[key] = _mask_mapping(value)
        elif isinstance(value, list):
            out[key] = [_mask_mapping(i) if isinstance(i, dict) else i for i in value]
        else:
            out[key] = value
    return out


def _mask_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _mask_mapping(event_dict)


def _request_id_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.setdefault("request_id", request_id_ctx.get())
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    """Uygulama genelinde loglamayı yapılandırır (idempotent)."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _request_id_processor,
        _mask_processor,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "apiwrapper") -> Any:
    return structlog.get_logger(name)
