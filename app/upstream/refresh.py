"""Erişim token'ını **refresh token** ile kendiliğinden yeniler.

Neden gerekli: upstream oturumu 1 saatlik (`expires_in: 3600`) bir erişim
token'ı taşıyor. Süre dolunca her istek 401 döner; çerez elle taşınmadıkça
servis ölü kalır. Oturum çerezinde `refresh_token` bulunduğu için token
upstream'in kendi token uç noktasından yenilenebilir.

Akış (Supabase/GoTrue biçimi):

```
POST {base}/auth/v1/token?grant_type=refresh_token
{"refresh_token": "..."}          (+ varsa `apikey` başlığı)
→ {"access_token": "eyJ...", "refresh_token": "yeni...", "expires_in": 3600}
```

Tasarım kuralları:

* Hiçbir token/log/yanıt gövdesi yazılmaz; yalnız durum ve süre döner.
* Yenileme başarısız olursa **mevcut token korunur** (çalışanı bozmadan dene).
* `refresh_token` her yenilemede döner (rotation); yeni değer hem bellekteki
  ayarlara hem de oturum çerezine yazılır.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import (
    decode_jwt_claims,
    extract_access_token,
    extract_refresh_token,
    parse_cookie_header,
    token_seconds_remaining,
)

logger = get_logger(__name__)

#: Yenileme isteğinin varsayılan zaman aşımı (sn).
_REFRESH_TIMEOUT = 20.0

#: `ad.0`, `ad.1` biçimindeki parçalı çerez son eki.
_CHUNK_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\.(?P<index>\d+)$")

#: Oturum çerezi gibi görünen adlarda aranan izler.
_SESSION_HINTS = ("auth", "session", "token", "sb-")


@dataclass(slots=True)
class RefreshOutcome:
    """Bir yenileme denemesinin sonucu (gizli değer içermez)."""

    ok: bool
    #: Türkçe, kullanıcıya gösterilebilir açıklama.
    detail: str = ""
    status_code: int | None = None
    #: Yeni erişim token'ı (yalnız bellekte kalır, hiçbir yere yazılmaz).
    access_token: str = ""
    refresh_token: str = ""
    expires_in: int | None = None
    #: Token uç noktası (alan adı loglanmasın diye yalnız yol).
    endpoint_path: str = ""
    notes: list[str] = field(default_factory=list)


def _strip_base64_prefix(value: str) -> str:
    for prefix in ("base64-", "base64_", "b64-"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _decode_session_json(value: str) -> dict[str, Any] | None:
    """`base64-…` oturum değerini JSON sözlüğüne çevirir (başarısızsa None)."""
    body = _strip_base64_prefix(value.strip())
    body = body.replace("-", "+").replace("_", "/")
    body += "=" * (-len(body) % 4)
    try:
        decoded = base64.b64decode(body, validate=False).decode("utf-8", "ignore")
        parsed = json.loads(decoded)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _encode_session_json(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return "base64-" + base64.b64encode(raw).decode()


def chunk_families(cookies: dict[str, str]) -> dict[str, list[int]]:
    """`ad.0`, `ad.1` … parçalarını aile → indeks listesi olarak döndürür."""
    families: dict[str, list[int]] = {}
    for name in cookies:
        match = _CHUNK_SUFFIX_RE.match(name)
        if match:
            families.setdefault(match.group("base"), []).append(int(match.group("index")))
    return {base: sorted(indices) for base, indices in families.items()}


def _session_entries(cookies: dict[str, str]) -> list[tuple[str, dict[str, Any]]]:
    """Oturum JSON'u çözülebilen (aile adı, JSON) çiftlerini döndürür."""
    entries: list[tuple[str, dict[str, Any]]] = []
    for base, indices in chunk_families(cookies).items():
        merged = "".join(cookies.get(f"{base}.{index}", "") for index in indices)
        payload = _decode_session_json(merged)
        if payload and ("access_token" in payload or "refresh_token" in payload):
            entries.append((base, payload))
    for name, value in cookies.items():
        if _CHUNK_SUFFIX_RE.match(name):
            continue
        payload = _decode_session_json(value)
        if payload and ("access_token" in payload or "refresh_token" in payload):
            entries.append((name, payload))
    return entries


def derive_refresh_url(settings: Settings) -> str:
    """Yenileme uç noktasını belirler.

    Sıra: açık ayar → çerezdeki JWT'nin `iss` alanı → `base_url` + `/auth/v1`.
    Alan adı loglanmaz; çağıran taraf yalnız yolu gösterir.
    """
    configured = (settings.upstream_refresh_url or "").strip()
    if configured:
        return configured
    cookie = settings.upstream_cookie or ""
    token = extract_access_token(
        cookie, cookie_names=settings.upstream_token_cookie_names
    )
    issuer = str(decode_jwt_claims(token).get("iss") or "") if token else ""
    if issuer:
        base = issuer if issuer.rstrip("/").endswith("/auth/v1") else issuer.rstrip("/")
        return f"{base.rstrip('/')}/token?grant_type=refresh_token"
    cookies = parse_cookie_header(cookie)
    for _, payload in _session_entries(cookies):
        candidate = extract_access_token(
            json.dumps({k: v for k, v in payload.items() if isinstance(v, str)})
        )
        issuer = str(decode_jwt_claims(candidate).get("iss") or "") if candidate else ""
        if issuer:
            base = issuer if issuer.rstrip("/").endswith("/auth/v1") else issuer.rstrip("/")
            return f"{base.rstrip('/')}/token?grant_type=refresh_token"
    site = urlsplit(settings.base_url).netloc
    if site:
        scheme = urlsplit(settings.base_url).scheme or "https"
        return f"{scheme}://{site}/auth/v1/token?grant_type=refresh_token"
    return ""


def cookie_refresh_token(settings: Settings) -> str:
    """Çerezden (yoksa ayarlardan) refresh token'ı döndürür."""
    from_cookie = extract_refresh_token(
        settings.upstream_cookie or "",
        cookie_names=settings.upstream_token_cookie_names,
    )
    return from_cookie or (settings.upstream_refresh_token or "").strip()


def needs_refresh(settings: Settings, *, leeway_seconds: int | None = None) -> bool:
    """Token (varsa) yakında dolacak mı? Refresh token yoksa her zaman False."""
    if not cookie_refresh_token(settings):
        return False
    leeway = (
        settings.upstream_refresh_leeway_seconds
        if leeway_seconds is None
        else leeway_seconds
    )
    token = extract_access_token(
        settings.upstream_cookie or "",
        cookie_names=settings.upstream_token_cookie_names,
    ) or (settings.upstream_access_token or "").strip()
    if not token:
        # Token hiç okunamıyorsa yine de denemeye değer: oturum bozuk olabilir.
        return True
    remaining = token_seconds_remaining(token)
    if remaining is None:
        return True
    return remaining <= leeway


async def refresh_upstream_token(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> RefreshOutcome:
    """Refresh token ile yeni erişim token'ı alır ve **ayarlara uygular**.

    Başarısızlıkta mevcut token'a dokunulmaz; hata Türkçe ve gizli değer
    içermeyen bir cümleyle döner.
    """
    endpoint = derive_refresh_url(settings)
    path = urlsplit(endpoint).path if endpoint else ""
    refresh_token = cookie_refresh_token(settings)
    if not refresh_token:
        return RefreshOutcome(
            ok=False,
            detail=(
                "Çerezde refresh token yok; otomatik yenileme yapılamaz. "
                "Tarayıcıdan taze çerez (/cookie) gerekir."
            ),
            endpoint_path=path,
        )
    if not endpoint:
        return RefreshOutcome(
            ok=False,
            detail=(
                "Yenileme uç noktası belirlenemedi. UPSTREAM_REFRESH_URL "
                "ayarlayın (ör. https://<proje>.supabase.co/auth/v1/token"
                "?grant_type=refresh_token)."
            ),
            endpoint_path=path,
        )

    headers = {"Content-Type": "application/json"}
    api_key = (settings.upstream_refresh_api_key or "").strip()
    if api_key:
        headers["apikey"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_REFRESH_TIMEOUT)
    try:
        response = await http.post(
            endpoint,
            json={"refresh_token": refresh_token},
            headers=headers,
            timeout=_REFRESH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        logger.warning("upstream_token_refresh_failed", error_type=type(exc).__name__)
        return RefreshOutcome(
            ok=False,
            detail=f"Yenileme isteği başarısız ({type(exc).__name__}).",
            endpoint_path=path,
        )
    finally:
        if owns_client:
            await http.aclose()

    payload: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            payload = parsed
    except ValueError:
        payload = {}

    if response.status_code != 200 or not payload.get("access_token"):
        detail = _describe_failure(response.status_code, payload, endpoint)
        logger.warning(
            "upstream_token_refresh_rejected",
            status_code=response.status_code,
            endpoint_path=path,
        )
        return RefreshOutcome(
            ok=False,
            detail=detail,
            status_code=response.status_code,
            endpoint_path=path,
        )

    access_token = str(payload.get("access_token") or "")
    new_refresh = str(payload.get("refresh_token") or "") or refresh_token
    expires_in = payload.get("expires_in")
    outcome = RefreshOutcome(
        ok=True,
        detail="Erişim token'ı yenilendi.",
        status_code=response.status_code,
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=int(expires_in) if isinstance(expires_in, (int, float)) else None,
        endpoint_path=path,
    )
    apply_refresh(settings, outcome)
    logger.info(
        "upstream_token_refreshed",
        endpoint_path=path,
        expires_in=outcome.expires_in,
        rotated=bool(payload.get("refresh_token")),
    )
    return outcome


def _describe_failure(status: int, payload: dict[str, Any], endpoint: str) -> str:
    """Yenileme hatasını eyleme dönük tek cümleye çevirir."""
    message = str(
        payload.get("error_description")
        or payload.get("msg")
        or payload.get("message")
        or payload.get("error")
        or ""
    )
    lowered = message.lower()
    if status in (400, 401) and ("api key" in lowered or "apikey" in lowered):
        return (
            "Upstream `apikey` istiyor: sitede kullanılan anon anahtarını "
            "UPSTREAM_REFRESH_API_KEY olarak ekleyin (ya da /apikey <anahtar>)."
        )
    if status in (400, 401) and ("refresh" in lowered or "token" in lowered):
        return (
            "Refresh token geçersiz/süresi dolmuş görünüyor; tarayıcıdan taze "
            "çerez gerekir (/cookie)."
        )
    if not urlsplit(endpoint).path:
        return "Yenileme adresi geçersiz; UPSTREAM_REFRESH_URL ayarlayın."
    return f"Yenileme reddedildi (HTTP {status}); mevcut token korunuyor."


def apply_refresh(settings: Settings, outcome: RefreshOutcome) -> list[str]:
    """Yenileme sonucunu ayarlara uygular; değişen alan adlarını döndürür."""
    if not outcome.ok or not outcome.access_token:
        return []
    changed: list[str] = []
    if settings.upstream_access_token != outcome.access_token:
        settings.upstream_access_token = outcome.access_token
        changed.append("upstream_access_token")
    if outcome.refresh_token and settings.upstream_refresh_token != outcome.refresh_token:
        settings.upstream_refresh_token = outcome.refresh_token
        changed.append("upstream_refresh_token")
    rewritten, cookie_changed = rewrite_session_cookie(
        settings.upstream_cookie or "",
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token or None,
        expires_in=outcome.expires_in,
    )
    if cookie_changed and rewritten != settings.upstream_cookie:
        settings.upstream_cookie = rewritten
        changed.append("upstream_cookie")
    return changed


def rewrite_session_cookie(
    cookie_header: str,
    *,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> tuple[str, bool]:
    """Oturum çerezindeki JSON'u yeni token'larla günceller.

    Parçalı çerezler (`ad.0`, `ad.1`) orijinal sınır uzunlukları korunarak
    yeniden bölünür; adlar ve sıra değişmez. Çözülemeyen bir set gelirse
    çerez **olduğu gibi** döner (`changed=False`) — bozuk bir çerez yazmaktansa
    hiç dokunmamak doğrudur.
    """
    if not cookie_header:
        return cookie_header, False
    cookies = parse_cookie_header(cookie_header)
    entries = _session_entries(cookies)
    if not entries:
        return cookie_header, False

    base, payload = entries[0]
    updated = dict(payload)
    updated["access_token"] = access_token
    if refresh_token:
        updated["refresh_token"] = refresh_token
    if expires_in is not None:
        updated["expires_in"] = int(expires_in)
        updated["expires_at"] = int(_now() + int(expires_in))
    elif "expires_at" in updated:
        # Süre bilgisi verilmediyse eski `expires_at` yanlış kalmasın.
        updated.pop("expires_at", None)
    encoded = _encode_session_json(updated)

    indices = chunk_families(cookies).get(base)
    if not indices:
        cookies[base] = encoded
    else:
        lengths = [len(cookies.get(f"{base}.{index}", "")) for index in indices]
        cursor = 0
        for position, index in enumerate(indices):
            is_last = position == len(indices) - 1
            size = len(encoded) - cursor if is_last else lengths[position]
            cookies[f"{base}.{index}"] = encoded[cursor : cursor + size]
            cursor += size
    rebuilt = "; ".join(f"{name}={value}" for name, value in cookies.items())
    return rebuilt, True


def _now() -> float:
    import time

    return time.time()
