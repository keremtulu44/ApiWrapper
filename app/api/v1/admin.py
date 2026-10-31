"""Yönetim uç noktaları: oturum/captcha/devre kesici kontrolü."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_recaptcha, get_sessions, get_upstream
from app.core.errors import InvalidRequestError
from app.core.logging import mask_value
from app.core.metrics import metrics
from app.core.security import api_key_dependency
from app.services.account import AccountPool
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.auth import (
    DEFAULT_COOKIE_HINTS,
    NEVER_TOKEN_COOKIES,
    extract_access_token,
    extract_refresh_token,
    looks_like_jwt,
    parse_cookie_header,
    token_seconds_remaining,
)
from app.upstream.breaker import BreakerState
from app.upstream.client import UpstreamClient
from app.upstream.headers import build_authorization

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(api_key_dependency)])


def get_accounts(request: Request) -> AccountPool:
    return request.app.state.accounts


@router.get("/accounts", summary="Upstream account pool status")
async def account_status(
    request: Request, accounts: AccountPool = Depends(get_accounts)
) -> dict[str, Any]:
    """Havuzdaki hesapların kota/dinlenme durumunu **gizli değer ifşa etmeden** gösterir.

    Upstream'in gerçek kota sayısını bilmediğimiz için buradaki `learned_limit`
    kilitlenmelerden öğrenilen tavandır; `messages_in_window` ise bizim saydığımız
    kayan pencere değeridir.
    """
    snapshot = accounts.snapshot()
    return {
        "configured_accounts": len([a for a in snapshot if a["configured"]]),
        "quota_window_seconds": request.app.state.settings.account_quota_window_seconds,
        "cooldown_seconds": request.app.state.settings.account_cooldown_seconds,
        "initial_budget": request.app.state.settings.account_msg_budget,
        "max_switches_per_request": request.app.state.settings.account_max_switches,
        "switches_total": metrics.counter_total("apiwrapper_account_switches_total"),
        "accounts": snapshot,
    }


@router.post("/accounts/{slot}/reset", summary="Clear an account's cooldown")
async def reset_account_cooldown(
    slot: int, accounts: AccountPool = Depends(get_accounts)
) -> dict[str, Any]:
    """Bir hesabın dinlenme süresini sıfırlar (örn. kilit erken açıldıysa)."""
    account = accounts.get(slot)
    if account is None:
        raise InvalidRequestError(
            f"No account in slot {slot}.", param="slot"
        )
    accounts.clear_cooldown(slot)
    return {"slot": slot, "name": account.label, "cooldown_remaining_seconds": 0.0}


@router.post("/sessions/clear", summary="Clear cached upstream sessions")
async def clear_sessions(sessions: SessionManager = Depends(get_sessions)) -> dict[str, Any]:
    before = await sessions.size()
    await sessions.clear()
    return {"cleared": before}


@router.post("/recaptcha/invalidate", summary="Force a new reCAPTCHA token")
async def invalidate_recaptcha(
    recaptcha: RecaptchaProvider = Depends(get_recaptcha),
) -> dict[str, Any]:
    recaptcha.invalidate()
    return {"status": "invalidated", "provider": recaptcha.name}


@router.post("/breaker/reset", summary="Reset the upstream circuit breaker")
async def reset_breaker(upstream: UpstreamClient = Depends(get_upstream)) -> dict[str, Any]:
    upstream.breaker.reset()
    return {"status": "closed"}


@router.get("/config", summary="Effective non-sensitive configuration")
async def effective_config(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    upstream: UpstreamClient = request.app.state.upstream
    return {
        "base_url": settings.base_url,
        "stream_path": settings.upstream_stream_path,
        "http2": settings.upstream_http2,
        "proxy_configured": bool(settings.upstream_proxy),
        "cookie_configured": bool(settings.upstream_cookie),
        "recaptcha_provider": settings.recaptcha_provider,
        "unsupported_params_policy": settings.unsupported_params,
        "stateless_sessions": settings.session_stateless,
        "quota": {
            "markers": list(settings.upstream_limit_markers),
            "text_scan_chars": settings.quota_text_scan_chars,
            "detected_total": metrics.counter_total(
                "apiwrapper_upstream_quota_errors_total"
            ),
        },
        "session": {
            "reuse": settings.session_reuse,
            "stateless": settings.session_stateless,
            "rotate_after_messages": settings.session_rotate_after_messages,
            "rotate_after_seconds": settings.session_rotate_after_seconds,
            "ttl_seconds": settings.session_ttl,
            "cached": await request.app.state.sessions.size(),
            "rotations_total": metrics.counter_total("apiwrapper_sessions_rotated_total"),
        },
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "rpm": settings.rate_limit_rpm,
            "burst": settings.rate_limit_burst,
        },
        "retry": {
            "max_attempts": settings.retry_max_attempts,
            "base_delay": settings.retry_base_delay,
            "max_delay": settings.retry_max_delay,
        },
        "circuit_breaker": {
            "enabled": settings.breaker_enabled,
            "state": BreakerState(upstream.breaker.state).name,
            "failure_threshold": settings.breaker_failure_threshold,
            "reset_timeout": settings.breaker_reset_timeout,
        },
        "limits": {
            "max_body_bytes": settings.max_body_bytes,
            "max_messages": settings.max_messages,
            "max_prompt_chars": settings.max_prompt_chars,
            "max_concurrent_upstream": settings.max_concurrent_upstream,
        },
    }


@router.get("/auth", summary="Diagnose upstream credential extraction")
async def auth_diagnostics(
    request: Request,
    slot: int | None = None,
    accounts: AccountPool = Depends(get_accounts),
) -> dict[str, Any]:
    """Seçilen hesabın token ayıklamasını **gizli değerleri açmadan** gösterir.

    401 sorunlarını teşhis etmek için: token bulundu mu, hangi çerezler mevcut,
    JWT mi, ne zaman doluyor? İkinci+ hesap için ``?slot=2`` kullanılabilir.
    """
    settings = request.app.state.settings
    account = accounts.get(slot) if slot is not None else None
    if slot is not None and account is None:
        raise InvalidRequestError(f"No account in slot {slot}.", param="slot")
    if account is not None:
        settings = account.effective_settings(settings)
    cookies = parse_cookie_header(settings.upstream_cookie)
    token = extract_access_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    ) if settings.upstream_auth_from_cookie else None
    effective = settings.upstream_access_token.strip() or token
    refresh_token = extract_refresh_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    )
    header_value = build_authorization(settings)

    remaining = token_seconds_remaining(effective) if effective else None
    suffix = f"_{slot}" if slot is not None and slot > 1 else ""
    return {
        "slot": slot,
        "account": account.label if account is not None else None,
        "authorization_header_will_be_sent": bool(header_value),
        "auth_scheme": settings.upstream_auth_scheme,
        "source": (
            f"UPSTREAM_ACCESS_TOKEN{suffix}"
            if settings.upstream_access_token.strip()
            else ("cookie" if token else None)
        ),
        "extract_from_cookie_enabled": settings.upstream_auth_from_cookie,
        "refresh_token": {
            "found": bool(refresh_token),
            "masked": mask_value(refresh_token) if refresh_token else None,
            "length": len(refresh_token) if refresh_token else 0,
        },
        "configured_cookie_names": settings.upstream_token_cookie_names,
        "cookie_names_present": sorted(cookies),
        "cookie_count": len(cookies),
        "session_cookie_candidates": _session_cookie_candidates(cookies),
        "token": {
            "found": bool(effective),
            "masked": mask_value(effective) if effective else None,
            "length": len(effective) if effective else 0,
            "is_jwt": looks_like_jwt(effective) if effective else False,
            "expires_in_seconds": int(remaining) if remaining is not None else None,
            "expired": (remaining is not None and remaining <= 0),
        },
    }


def _session_cookie_candidates(cookies: dict[str, str]) -> list[dict[str, object]]:
    """Oturum çerezi olabilecek adayları, neden elendikleriyle birlikte listeler.

    Token bulunamadığında hangi çerezin incelendiğini ve hangi adımda elendiğini
    göstererek ``UPSTREAM_TOKEN_COOKIE_NAMES`` ayarını kolaylaştırır.
    """
    candidates: list[dict[str, object]] = []
    for name, value in sorted(cookies.items()):
        lowered = name.lower()
        if lowered in NEVER_TOKEN_COOKIES:
            continue
        matched_hint = next((h for h in DEFAULT_COOKIE_HINTS if h in lowered), None)
        decoded = extract_access_token(f"{name}={value}")
        if matched_hint is None and decoded is None:
            continue
        candidates.append(
            {
                "cookie": name,
                "matched_hint": matched_hint,
                "value_length": len(value),
                "token_extracted": decoded is not None,
                "reason": (
                    "token extracted"
                    if decoded
                    else "name matched but value did not decode to a JWT/JSON token"
                ),
            }
        )
    return candidates
