"""Sağlık kontrolü ve metrik uç noktaları (kimlik doğrulama gerektirmez)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.deps import get_registry, get_settings_dep
from app.core.config import Settings
from app.core.errors import ModelNotFoundError
from app.core.metrics import metrics
from app.services.account import AccountPool
from app.services.model_registry import ModelRegistry
from app.upstream.breaker import BreakerState

router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


@router.get("/health", summary="Detailed runtime health")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    registry: ModelRegistry = Depends(get_registry),
) -> dict[str, Any]:
    upstream = request.app.state.upstream
    breaker_state = BreakerState(upstream.breaker.state).name
    sessions = await request.app.state.sessions.size()
    return {
        "status": "ok" if breaker_state != "OPEN" else "degraded",
        "version": settings.app_version,
        "uptime_seconds": round(time.time() - _STARTED_AT, 2),
        "upstream": {
            "base_url": settings.base_url,
            "circuit_breaker": breaker_state,
            "http2": settings.upstream_http2,
        },
        "models": len(registry),
        "sessions_cached": sessions,
        "recaptcha_provider": settings.recaptcha_provider,
    }


def _deployment_issues(
    settings: Settings, registry: ModelRegistry, accounts: AccountPool
) -> list[str]:
    """Canlı upstream'e çıkmadan kontrol edilebilen dağıtım eksiklerini döndür.

    Burada cookie/token geçerliliği veya upstream erişimi denenmez. Aksi halde
    geçici bir upstream arızası Render healthcheck'i üzerinden restart döngüsü
    oluşturabilir. Dönen kodlar gizli değer içermez ve istemciye gösterilebilir.
    """
    issues: list[str] = []
    target = settings.target_domain.strip().lower()
    hostname = target.split(":", 1)[0]
    if hostname in {"", "localhost", "127.0.0.1", "::1", "[::1]"} or target in {
        "example.com",
        "example-llm.com",
    }:
        issues.append("target_domain_not_configured")

    if settings.auth_disabled:
        issues.append("authentication_disabled")
    keys = [key.strip() for key in settings.api_keys if key.strip()]
    if not keys:
        issues.append("api_keys_missing")
    elif any(key == "sk-local-dev-key" for key in keys):
        issues.append("default_development_api_key")

    try:
        default_model = registry.resolve(None)
    except ModelNotFoundError:
        issues.append("default_model_missing")
    else:
        upstream_id = default_model.upstream_id.strip()
        if not upstream_id or upstream_id == "default-model" or upstream_id.startswith(
            "REPLACE_WITH_"
        ):
            issues.append("default_upstream_model_placeholder")

    provider = settings.recaptcha_provider
    if provider == "browser" and not settings.recaptcha_site_key.strip():
        issues.append("recaptcha_site_key_missing")
    elif provider == "external" and not settings.recaptcha_external_url.strip():
        issues.append("recaptcha_external_url_missing")
    # Static kipte hesapların tamamı kendi token'ını taşıyorsa global değer gerekmez.
    elif provider == "static" and not settings.recaptcha_static_token.strip() and (
        accounts.size == 0
        or any(not account.has_own_captcha_token for account in accounts.accounts)
    ):
        issues.append("recaptcha_static_token_missing")
    return issues


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness: yalnızca HTTP prosesi cevap verebiliyor mu?"""
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    registry: ModelRegistry = Depends(get_registry),
) -> JSONResponse:
    """Readiness: statik dağıtım ayarları tamam mı; canlı upstream denenmez."""
    issues = _deployment_issues(settings, registry, request.app.state.accounts)
    if issues:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "issues": issues},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})


@router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
async def prometheus_metrics(
    settings: Settings = Depends(get_settings_dep),
) -> PlainTextResponse:
    if not settings.metrics_enabled:
        return PlainTextResponse("# metrics disabled\n", status_code=404)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/", include_in_schema=False)
async def index(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/models",
            "/health",
            "/readyz",
            "/metrics",
        ],
    }
