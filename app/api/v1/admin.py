"""Yönetim uç noktaları: oturum/captcha/devre kesici kontrolü."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import (
    get_recaptcha,
    get_sessions,
    get_settings_dep,
    get_upstream,
)
from app.core.config import Settings
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
from app.upstream.curl_runtime import apply_curl_to_settings
from app.upstream.headers import build_authorization
from app.upstream.session_capture import (
    apply_capture_to_settings,
    apply_cookie_to_settings,
    capture_with_provider,
    inspect_cookie,
)

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


class UpstreamCookiePayload(BaseModel):
    """Ham `Cookie` başlığı (mobilde cURL'den çok daha kolay elde edilir)."""

    cookie: str = Field(min_length=10, max_length=30000)


@router.post("/upstream/cookie", summary="Swap upstream cookies without a cURL")
async def update_upstream_cookie(
    payload: UpstreamCookiePayload,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    upstream: UpstreamClient = Depends(get_upstream),
    accounts: AccountPool = Depends(get_accounts),
    sessions: SessionManager = Depends(get_sessions),
    recaptcha: RecaptchaProvider = Depends(get_recaptcha),
) -> dict[str, Any]:
    """Yalnızca çerez setini uygular — telefonda cURL üretmek zor olduğunda.

    Çerez başlığı tek satırdır ve tarayıcı eklentisi/dışa aktarma aracıyla
    kopyalanabilir; eklentinin JSON dışa aktarımı da kabul edilir. Ham değer
    yanıta ve loglara yazılmaz.

    `?dry_run=true` ile **uygulamadan** inceleme yapılır: hangi çerez adları
    geldi, oturum çerezi var mı, token süresi dolmuş mu. Telefonda yanlış seti
    uygulayıp çalışan oturumu ezmeyi önler.
    """
    from app.core.logging import get_logger

    logger = get_logger(__name__)

    if request.query_params.get("dry_run", "").lower() in {"1", "true", "yes"}:
        inspection = inspect_cookie(payload.cookie)
        return {
            "status": "inspected",
            "dry_run": True,
            "applied": False,
            **inspection,
            "next": (
                "Çerez uygulanabilir: /upstream/cookie (dry_run olmadan) ile gönderin."
                if inspection["safe_to_apply"]
                else "Bu sette oturum çerezi yok; giriş yapmış tarayıcıdan TÜM "
                "çerezleri aktarın. Bu hâliyle uygulanırsa çalışan oturum ezilebilir."
            ),
        }

    try:
        changed = apply_cookie_to_settings(settings, payload.cookie)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="cookie") from exc

    account_changes = accounts.reload_credentials(settings)
    dropped_clients = await upstream.reset_account_clients()
    upstream.breaker.reset()
    await sessions.clear()
    recaptcha.invalidate()
    from app.upstream.state_store import sync_render_env

    await sync_render_env(settings)
    logger.info(
        "upstream_cookie_applied",
        fields=changed,
        accounts=sorted(account_changes),
        dropped_clients=dropped_clients,
    )
    return {
        "status": "applied",
        "fields": changed,
        "accounts_updated": account_changes,
        "clients_reset": dropped_clients,
        "next": "Bir mesaj atarak test edin.",
    }


class UpstreamCurlPayload(BaseModel):
    """Taze cURL metni (ham değer hiçbir yere loglanmaz/yanıtta dönmez)."""

    curl: str = Field(min_length=20, max_length=30000)
    #: Yol sohbet akış ucu gibi görünmüyorsa varsayılan olarak canlı akış yolu
    #: korunur; yolu da değiştirmek için bu bayrağı gönderin.
    allow_path_change: bool = False


@router.post("/upstream/refresh", summary="Refresh the upstream access token")
async def refresh_upstream_credentials(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    upstream: UpstreamClient = Depends(get_upstream),
    accounts: AccountPool = Depends(get_accounts),
    sessions: SessionManager = Depends(get_sessions),
) -> dict[str, Any]:
    """`refresh_token` ile erişim token'ını yeniler (deploy gerekmez).

    Erişim token'ı ~1 saatte öldüğü için servis sessizce 401'e düşüyordu. Bu uç
    nokta aynı işi elle tetikler; arka plan döngüsü de aynı fonksiyonu kullanır.
    Token değerleri yanıta **yazılmaz**.
    """
    from app.services.token_refresh import record_refresh, refresh_state
    from app.upstream.refresh import refresh_upstream_token

    outcome = await refresh_upstream_token(settings)
    record_refresh(ok=outcome.ok, detail=outcome.detail)
    state = refresh_state()
    if not outcome.ok:
        return {
            "status": "failed",
            "detail": outcome.detail,
            "endpoint_path": outcome.endpoint_path,
            "refresh_state": state,
        }
    account_changes = accounts.reload_credentials(settings)
    dropped_clients = await upstream.reset_account_clients()
    upstream.breaker.reset()
    await sessions.clear()
    request.app.state.recaptcha.invalidate()
    return {
        "status": "refreshed",
        "expires_in": outcome.expires_in,
        "endpoint_path": outcome.endpoint_path,
        "accounts_updated": account_changes,
        "clients_reset": dropped_clients,
        "notes": outcome.notes,
        "note": "Yeni token bellekte ve aktif istemcilerde güncellendi.",
        "refresh_state": state,
    }


class UpstreamApiKeyPayload(BaseModel):
    """Supabase/GoTrue yenileme `apikey` (anon anahtar) gövdesi. Boşsa otomatik keşfedilir."""

    apikey: str = Field(default="", max_length=4096)


@router.post("/upstream/apikey", summary="Set or auto-discover upstream refresh apikey")
async def update_upstream_apikey(
    payload: UpstreamApiKeyPayload,
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """Token yenileme için gereken `apikey` (Supabase anon anahtarı) değerini ayarlar
    veya boş gönderilirse sitenin HTML/JS'inden otomatik keşfeder.
    """
    from app.upstream.refresh import discover_refresh_api_key
    from app.upstream.state_store import save_runtime_state, sync_render_env

    provided = (payload.apikey or "").strip()
    if provided:
        settings.upstream_refresh_api_key = provided
        save_runtime_state(settings, source="apikey_manual")
        await sync_render_env(settings)
        return {
            "status": "applied",
            "source": "manual",
            "has_api_key": True,
            "length": len(provided),
        }

    discovered = await discover_refresh_api_key(settings)
    if discovered:
        save_runtime_state(settings, source="apikey_discovered")
        await sync_render_env(settings)
        return {
            "status": "discovered",
            "source": "auto",
            "has_api_key": True,
            "length": len(discovered),
        }
    return {
        "status": "not_found",
        "source": None,
        "has_api_key": bool((settings.upstream_refresh_api_key or "").strip()),
        "detail": (
            "Sitenin ana sayfasından anon anahtar otomatik keşfedilemedi. "
            "Anahtarı biliyorsanız /apikey <anahtar> ile doğrudan verebilirsiniz."
        ),
    }


@router.post("/upstream/curl", summary="Swap upstream credentials from a fresh cURL")
async def update_upstream_curl(
    payload: UpstreamCurlPayload,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    upstream: UpstreamClient = Depends(get_upstream),
    accounts: AccountPool = Depends(get_accounts),
    sessions: SessionManager = Depends(get_sessions),
) -> dict[str, Any]:
    """Çalışan servise yeni cURL uygular — **yeniden deploy gerekmez**.

    Oturum düştüğünde (401 "User not found") ya da sürüm damgası eskidiğinde tek
    gereken şey taze bir cURL'dir. Render'da secret güncelleyip 2-3 dakika deploy
    beklemek yerine bu uç nokta alanları yerinde değiştirir, hesap cookie
    jar'larını kapatır ve oturum önbelleğini temizler.

    Not: bu değişiklik **bellekte**dir; süreç yeniden başlarsa Render'daki
    `UPSTREAM_CURL` değerine döner. Kalıcı olması için secret'ı da güncelleyin.
    """
    from app.core.logging import get_logger

    logger = get_logger(__name__)
    try:
        result = apply_curl_to_settings(
            settings, payload.curl, allow_path_change=payload.allow_path_change
        )
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="curl") from exc

    account_changes = accounts.reload_credentials(settings)
    dropped_clients = await upstream.reset_account_clients()
    upstream.breaker.reset()
    await sessions.clear()
    request.app.state.recaptcha.invalidate()
    from app.upstream.state_store import sync_render_env

    await sync_render_env(settings)

    logger.info(
        "upstream_curl_applied",
        fields=result["fields"],
        accounts=sorted(account_changes),
        dropped_clients=dropped_clients,
    )
    warnings = result.get("warnings") or []
    return {
        "status": "applied",
        "fields": result["fields"],
        "accounts_updated": account_changes,
        "clients_reset": dropped_clients,
        "upstream_model": result["model_id"],
        "warnings": warnings,
        "note": (
            "Değişiklik bellekte uygulandı; kalıcı olması için Render'daki "
            "UPSTREAM_CURL secret'ını da aynı cURL ile güncelleyin."
        ),
    }


@router.post("/upstream/capture", summary="Capture a fresh session with the server browser")
async def capture_upstream_session(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    upstream: UpstreamClient = Depends(get_upstream),
    accounts: AccountPool = Depends(get_accounts),
    sessions: SessionManager = Depends(get_sessions),
    recaptcha: RecaptchaProvider = Depends(get_recaptcha),
) -> dict[str, Any]:
    """Telefonda DevTools olmadan taze oturum yakalar.

    Servisteki Chromium (reCAPTCHA sağlayıcısı) hedef sayfayı açar, gerçek bir
    `create-evaluation` isteğini yakalar ve cookie/sürüm damgası/`cx`/referer
    yolunu çalışan ayarlara uygular. Hiçbir gizli değer yanıta konmaz.
    """
    from app.core.logging import get_logger

    logger = get_logger(__name__)
    try:
        result = await capture_with_provider(recaptcha, settings)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc
    except Exception as exc:  # pragma: no cover - tarayıcı hatası
        logger.warning("upstream_capture_failed", error_type=type(exc).__name__)
        raise InvalidRequestError(
            "Oturum yakalanamadı (tarayıcı hatası). Birkaç saniye sonra tekrar deneyin."
        ) from exc

    changed = apply_capture_to_settings(settings, result)
    if changed:
        accounts.reload_credentials(settings)
        await upstream.reset_account_clients()
        upstream.breaker.reset()
        if "upstream_cookie" in changed:
            await sessions.clear()
            recaptcha.invalidate()
        from app.upstream.state_store import sync_render_env

        await sync_render_env(settings)

    logger.info(
        "upstream_capture",
        logged_in=result.logged_in,
        cookie_found=result.has_cookie,
        deployment_found=bool(result.deployment_id),
        cx_found=bool(result.cx),
        triggered=result.triggered_request,
        changed=changed,
    )
    applied_cookie = "upstream_cookie" in changed
    if applied_cookie:
        next_step = "Bir mesaj atarak test edin."
    elif result.reason:
        # Net olalım: sunucu tarayıcısı **giriş yapamaz** (şifre/2FA yok).
        # Yapabildiği tek şey, elindeki oturum çerezini kullanmak.
        next_step = (
            f"Oturum yakalanamadı: {result.reason} "
            "Sunucu sizin yerinize giriş yapamaz; tarayıcıda giriş yapmışken "
            "TÜM çerezleri /cookie ile gönderin (cURL de olur: /curl)."
        )
    else:
        next_step = "Değişiklik yok; bir mesaj atarak test edin."
    return {
        "status": "captured" if applied_cookie else "not_captured",
        "logged_in": result.logged_in,
        "cookie_found": result.has_cookie,
        "cookie_applied": applied_cookie,
        "deployment_found": bool(result.deployment_id),
        "cx_found": bool(result.cx),
        "triggered_request": result.triggered_request,
        "injected_cookies": result.injected_cookies,
        "cookie_names": result.cookie_names,
        "session_cookie_found": result.session_cookie_found,
        "login_redirect": result.login_redirect,
        "access_token_found": result.access_token_found,
        "access_token_expired": result.access_token_expired,
        "final_path": result.final_path,
        "reason": result.reason,
        "fields": changed,
        "notes": result.notes,
        "next": next_step,
    }


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
    ) or (
        settings.upstream_refresh_token.strip()
        if slot is None or slot <= 1
        else ""
    )
    header_value = build_authorization(settings)
    from app.services.token_refresh import refresh_state

    # Kullanılan token `UPSTREAM_AUTH_FROM_COOKIE` kapalıyken boş görünebilir;
    # teşhis için çerezdeki token **her durumda** ayrıca okunur. Aksi hâlde
    # "token yok" yanıtı, çerezde süresi dolmuş bir token varken yanıltıcı olur.
    cookie_token = extract_access_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    )
    cookie_remaining = token_seconds_remaining(cookie_token) if cookie_token else None
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
        "auto_refresh": {
            "enabled": settings.upstream_refresh_enabled,
            "leeway_seconds": settings.upstream_refresh_leeway_seconds,
            "has_api_key": bool((settings.upstream_refresh_api_key or "").strip()),
            "configured_url": bool((settings.upstream_refresh_url or "").strip()),
            "state_file_configured": bool((settings.upstream_state_file or "").strip()),
            "render_sync_configured": bool(
                (settings.render_api_key or "").strip()
                and (settings.render_service_id or "").strip()
            ),
            "state": refresh_state(),
        },
        # Kullanılan token'dan bağımsız: çerezdeki oturum token'ı ne durumda?
        "cookie_token": {
            "found": bool(cookie_token),
            "is_jwt": looks_like_jwt(cookie_token) if cookie_token else False,
            "expires_in_seconds": (
                int(cookie_remaining) if cookie_remaining is not None else None
            ),
            "expired": (cookie_remaining is not None and cookie_remaining <= 0),
            "used_for_authorization": effective == cookie_token and bool(cookie_token),
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
