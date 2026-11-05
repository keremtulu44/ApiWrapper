"""API anahtarı doğrulama — sabit zamanlı karşılaştırma."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionError_
from app.core.logging import get_logger, mask_value

logger = get_logger(__name__)

ANONYMOUS = "anonymous"


def client_fingerprint(key: str | None) -> str:
    """Anahtardan log'a yazılabilir, çakışmasız bir istemci kimliği üretir.

    `mask_value` yalnızca ilk 3 + son 2 karakteri tuttuğu için farklı anahtarlar
    aynı maskeye düşebilir; oturum anahtarı olarak kullanılamaz. Bunun yerine
    SHA-256 özeti alınır — geri döndürülemez ama aynı anahtar için kararlıdır.
    """
    if not key or key == ANONYMOUS:
        return ANONYMOUS
    return "cli_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def extract_api_key(request: Request) -> str | None:
    """Authorization: Bearer <key> veya x-api-key başlığından anahtarı çıkarır."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        if not value and auth.strip():
            # "Authorization: sk-..." biçimi (bazı istemciler)
            return auth.strip()
    header_key = request.headers.get("x-api-key")
    if header_key and header_key.strip():
        return header_key.strip()
    return None


def verify_api_key(request: Request, settings: Settings | None = None) -> str:
    """Anahtarı doğrular ve maskelenmiş kimliğini döndürür.

    Doğrulama kapalıysa `ANONYMOUS` döner.
    """
    if settings is None:
        settings = getattr(request.app.state, "settings", None) or get_settings()
    if settings.auth_disabled:
        return ANONYMOUS

    key = extract_api_key(request)
    if not key:
        raise AuthenticationError(
            "Missing API key. Provide it via 'Authorization: Bearer <key>' header.",
            code="missing_api_key",
        )

    for allowed in settings.api_keys:
        if hmac.compare_digest(key, allowed):
            return mask_value(key)

    raise AuthenticationError("Incorrect API key provided.")


async def api_key_dependency(request: Request) -> str:
    """FastAPI bağımlılığı; doğrulanan anahtar kimliğini ve parmak izini state'e yazar."""
    identity = verify_api_key(request)
    request.state.api_identity = identity
    request.state.client_fingerprint = client_fingerprint(extract_api_key(request))
    return identity


def require_admin(request: Request) -> None:
    """Oturum kimliğini değiştirebilen `/v1/admin/*` uçları için ek yetki kapısı.

    `ADMIN_API_KEYS` boşsa hiçbir şey değişmez (geriye dönük uyumluluk: bugüne
    kadar tüm `API_KEYS` admin sayılıyordu). Doluysa yalnız listelenen anahtarlar
    geçer; normal bir istemci anahtarı sızdığında saldırgan upstream oturumunu
    değiştiremez, Render env'ini ezemez veya tüm sohbetleri temizleyemez.
    """
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:  # pragma: no cover - uygulama her zaman settings koyar
        return
    allowed = [key.strip() for key in settings.admin_api_keys if key.strip()]
    if not allowed:
        return
    key = extract_api_key(request) or ""
    if settings.auth_disabled:
        # Kimlik doğrulama kapalıyken ek kapı eklemek kurulumu kilitleyebilir;
        # istek geçer (auth_disabled zaten "güvenli yerel ağ" varsayımıdır).
        logger.warning("admin_gate_bypassed_auth_disabled", path=request.url.path)
        return
    if any(hmac.compare_digest(key, candidate) for candidate in allowed):
        return
    raise PermissionError_(
        "This endpoint requires an admin API key (ADMIN_API_KEYS).",
        code="admin_key_required",
    )
