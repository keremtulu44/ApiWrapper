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
  ayarlara, hem oturum çerezine hem de süreç dışı durum deposuna yazılır.
* Upstream `apikey` isterse ve ayarlarda yoksa, sitenin HTML/JS'inden public
  anon anahtar otomatik keşfedilip istek tekrarlanır.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.core.logging import get_logger
from app.upstream.auth import (
    DEFAULT_COOKIE_HINTS,
    NEVER_TOKEN_COOKIES,
    decode_jwt_claims,
    extract_access_token,
    extract_refresh_token,
    looks_like_jwt,
    parse_cookie_header,
    replace_cookie_values,
    token_seconds_remaining,
)
from app.upstream.headers import build_page_headers
from app.upstream.state_store import save_runtime_state, sync_render_env

logger = get_logger(__name__)

#: Yenileme isteğinin varsayılan zaman aşımı (sn).
_REFRESH_TIMEOUT = 20.0

#: `ad.0`, `ad.1` biçimindeki parçalı çerez son eki.
_CHUNK_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\.(?P<index>\d+)$")

#: Oturum çerezi gibi görünen adlarda aranan izler.
_SESSION_HINTS = ("auth", "session", "token", "sb-")

#: Sayfa/JS içinde geçen JWT adayları (`role: "anon"` kontrolü için).
_JWT_SCAN_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"
)

#: Sayfa/JS içinde açıkça adlandırılmış Supabase anon/publishable anahtarları.
_EXPLICIT_ANON_KEY_RE = re.compile(
    r"(?:NEXT_PUBLIC_SUPABASE_ANON_KEY|SUPABASE_ANON_KEY|supabaseAnonKey|anonKey)"
    r"""\s*[:=]\s*["'](?P<key>[A-Za-z0-9._-]{20,})["']"""
)
_PUBLISHABLE_KEY_RE = re.compile(r"\bsb_publishable_[A-Za-z0-9_-]{16,}\b")

#: HTML içindeki Next.js chunk script yolları.
_NEXT_SCRIPT_RE = re.compile(r"""src=["'](?P<path>/_next/static/[^"']+\.js)["']""")

#: Tek bir çerez parçasının güvenli azami uzunluğu (tarayıcı 4096 bayt sınırı).
_MAX_COOKIE_CHUNK_LEN = 3900


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


def _slot_suffix(slot: int) -> str:
    return "" if slot <= 1 else f"_{slot}"


def _slot_cookie(settings: Settings, slot: int = 1) -> str:
    attr = "upstream_cookie" if slot <= 1 else f"upstream_cookie_{slot}"
    return str(getattr(settings, attr, "") or "")


def _slot_access_token(settings: Settings, slot: int = 1) -> str:
    attr = "upstream_access_token" if slot <= 1 else f"upstream_access_token_{slot}"
    return str(getattr(settings, attr, "") or "").strip()


def _slot_cookie_names(settings: Settings, slot: int = 1) -> list[str]:
    attr = (
        "upstream_token_cookie_names"
        if slot <= 1
        else f"upstream_token_cookie_names_{slot}"
    )
    return list(getattr(settings, attr, None) or [])


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
    return "base64-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


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


def extract_anon_key_from_text(text: str) -> str | None:
    """HTML veya JS metninden Supabase public `anon` anahtarını ayıklar.

    Öncelik sırası:
    1. JWT biçiminde olup payload'ında `"role": "anon"` taşıyan token (sıfır yanlış pozitif).
    2. `NEXT_PUBLIC_SUPABASE_ANON_KEY` / `supabaseAnonKey` / `anonKey` ataması.
    3. `sb_publishable_...` biçimindeki yeni nesil Supabase publishable anahtarı.
    """
    if not text:
        return None
    sample = text[:500_000]
    for match in _JWT_SCAN_RE.finditer(sample):
        candidate = match.group(0)
        claims = decode_jwt_claims(candidate)
        role = str(claims.get("role") or "").strip().lower()
        if role == "anon":
            return candidate

    explicit = _EXPLICIT_ANON_KEY_RE.search(sample)
    if explicit:
        key = explicit.group("key").strip()
        if key:
            return key

    publishable = _PUBLISHABLE_KEY_RE.search(sample)
    if publishable:
        return publishable.group(0).strip()

    return None


@dataclass(slots=True)
class ApiKeyDiscovery:
    """`apikey` keşif denemesinin sonucu ve **başarısızlık nedeni**.

    Eski sürüm yalnız boş metin döndürüyordu; telefonundan `/apikey` deneyen
    operatör "bulunamadı" dışında hiçbir ipucu alamıyordu (ana sayfa 403 mü,
    anahtar hiç gömülmemiş mi, yoksa yalnız ilk 4 JS parçasına mı bakıldı?).
    """

    key: str = ""
    #: Kullanıcıya gösterilebilir, gizli değer içermeyen açıklama.
    detail: str = ""
    home_status: int | None = None
    scripts_seen: int = 0
    scripts_fetched: int = 0
    found_in: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.key)


#: Keşifte taranacak azami Next.js parça sayısı (ilk denemede 4 ile sınırlıydı;
#: anahtar genelde geç yüklenen chunk'larda duruyor).
_MAX_DISCOVERY_SCRIPTS = 12


async def discover_refresh_api_key_report(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> ApiKeyDiscovery:
    """`apikey`'i keşfeder ve başarısızlığın **nedenini** de raporlar."""
    existing = (settings.upstream_refresh_api_key or "").strip()
    if existing:
        return ApiKeyDiscovery(
            key=existing, detail="Anahtar zaten yapılandırılmış.", found_in="settings"
        )

    domain = (settings.target_domain or "").strip().lower()
    if not domain or domain in {"localhost", "127.0.0.1"}:
        return ApiKeyDiscovery(
            detail=(
                "TARGET_DOMAIN gerçek hedefi göstermiyor; keşif atlandı. "
                "Anahtarı /apikey <anahtar> ile elle verin."
            )
        )

    report = ApiKeyDiscovery()
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    headers = build_page_headers(settings, include_cookie=False)
    found = ""
    try:
        try:
            response = await http.get(
                f"{settings.base_url}/",
                headers=headers,
                timeout=10.0,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            report.detail = (
                f"Hedefin ana sayfasına ulaşılamadı ({type(exc).__name__}). "
                "Anahtarı /apikey <anahtar> ile elle verin."
            )
            return report

        report.home_status = response.status_code
        if not (200 <= response.status_code < 400):
            report.detail = (
                f"Hedefin ana sayfası HTTP {response.status_code} döndü; "
                "anahtar okunamadı (bot koruması/oturum duvarı olabilir). "
                "Anahtarı /apikey <anahtar> ile elle verin."
            )
            return report

        html = response.text or ""
        found = extract_anon_key_from_text(html) or ""
        if found:
            report.found_in = "home_page"

        if not found:
            # Next.js env değişkenleri `_next/static/chunks/*.js` içine gömülür.
            # Anahtar ilk 4 parçada olmayabilir; daha geniş tarama yapılır.
            script_paths: list[str] = []
            for match in _NEXT_SCRIPT_RE.finditer(html[:400_000]):
                script_path = match.group("path")
                if script_path not in script_paths:
                    script_paths.append(script_path)
                if len(script_paths) >= _MAX_DISCOVERY_SCRIPTS:
                    break
            report.scripts_seen = len(script_paths)
            for script_path in script_paths:
                try:
                    js_resp = await http.get(
                        f"{settings.base_url}{script_path}",
                        headers=headers,
                        timeout=8.0,
                    )
                except httpx.HTTPError:
                    continue
                report.scripts_fetched += 1
                if 200 <= js_resp.status_code < 400:
                    found = extract_anon_key_from_text(js_resp.text or "") or ""
                    if found:
                        report.found_in = "script"
                        break
    finally:
        if owns_client:
            await http.aclose()

    if found:
        settings.upstream_refresh_api_key = found
        report.key = found
        report.detail = "Anahtar otomatik keşfedildi."
        logger.info("upstream_refresh_api_key_discovered", found_in=report.found_in)
        return report

    report.detail = (
        "Ana sayfa ve "
        f"{report.scripts_fetched}/{report.scripts_seen} JS parçası tarandı; "
        "anon anahtar bulunamadı. Site anahtarı istemciye hiç göndermiyorsa "
        "otomatik keşif çalışmaz: /apikey <anahtar> ile elle verin veya "
        "UPSTREAM_REFRESH_API_KEY ayarlayın."
    )
    return report


async def discover_refresh_api_key(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Hedef sayfanın HTML/JS'inden Supabase `apikey` değerini otomatik keşfeder.

    Bulunursa `settings.upstream_refresh_api_key` içine de yazılır; böylece
    sonraki yenilemelerde tekrar sayfa okunmaz. Ayrıntılı neden için
    `discover_refresh_api_key_report` kullanın.
    """
    report = await discover_refresh_api_key_report(settings, client)
    return report.key


def derive_refresh_url(settings: Settings, *, slot: int = 1) -> str:
    """Yenileme uç noktasını belirler.

    Sıra: açık ayar → çerezdeki JWT'nin `iss` alanı → `base_url` + `/auth/v1`.
    Alan adı loglanmaz; çağıran taraf yalnız yolu gösterir.
    """
    configured = (settings.upstream_refresh_url or "").strip()
    if configured:
        return configured
    cookie = _slot_cookie(settings, slot)
    cookie_names = _slot_cookie_names(settings, slot)
    token = extract_access_token(cookie, cookie_names=cookie_names)
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


def cookie_refresh_token(settings: Settings, *, slot: int = 1) -> str:
    """Çerezden (yoksa ayarlardan) refresh token'ı döndürür."""
    from_cookie = extract_refresh_token(
        _slot_cookie(settings, slot),
        cookie_names=_slot_cookie_names(settings, slot),
    )
    if slot <= 1:
        return from_cookie or (settings.upstream_refresh_token or "").strip()
    return from_cookie or ""


def cookie_access_token(settings: Settings, *, slot: int = 1) -> str:
    """Çerezden (yoksa ayarlardan) access token'ı döndürür."""
    from_cookie = extract_access_token(
        _slot_cookie(settings, slot),
        cookie_names=_slot_cookie_names(settings, slot),
    )
    return (from_cookie or _slot_access_token(settings, slot)).strip()


def needs_refresh(
    settings: Settings,
    *,
    leeway_seconds: int | None = None,
    slot: int = 1,
) -> bool:
    """Token (varsa) yakında dolacak mı? Refresh token yoksa her zaman False."""
    if not cookie_refresh_token(settings, slot=slot):
        return False
    leeway = (
        settings.upstream_refresh_leeway_seconds
        if leeway_seconds is None
        else leeway_seconds
    )
    token = extract_access_token(
        _slot_cookie(settings, slot),
        cookie_names=_slot_cookie_names(settings, slot),
    ) or _slot_access_token(settings, slot)
    if not token:
        # Token hiç okunamıyorsa yine de denemeye değer: oturum bozuk olabilir.
        return True
    remaining = token_seconds_remaining(token)
    if remaining is None:
        return True
    return remaining <= leeway


def _is_missing_apikey(status: int, payload: dict[str, Any]) -> bool:
    if status not in (400, 401):
        return False
    message = str(
        payload.get("error_description")
        or payload.get("msg")
        or payload.get("message")
        or payload.get("error")
        or ""
    ).lower()
    return "api key" in message or "apikey" in message


def _parse_json_dict(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


#: Yuva başına eşzamanlı token yenileme kilidi: aynı anda birden fazla 401
#: kurtarması veya arka plan döngüsü + `/token` çakıştığında aynı tek-kullanımlık
#: `refresh_token` ile paralel istek atılıp oturumun iptal edilmesini önler.
_REFRESH_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _get_refresh_lock(slot: int) -> asyncio.Lock:
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    key = (loop_id, slot)
    lock = _REFRESH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _REFRESH_LOCKS[key] = lock
    return lock


async def refresh_upstream_token(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    slot: int = 1,
) -> RefreshOutcome:
    """Refresh token ile yeni erişim token'ı alır ve **ayarlara uygular**.

    Başarısızlıkta mevcut token'a dokunulmaz; hata Türkçe ve gizli değer
    içermeyen bir cümleyle döner. Eşzamanlı çağrılar `asyncio.Lock` ile sıraya
    alınır; kilit beklenirken başka bir görev token'ı zaten yenilediyse ikinci
    kez `/auth/v1/token` çağrılmaz.
    """
    token_before = cookie_access_token(settings, slot=slot)
    refresh_before = cookie_refresh_token(settings, slot=slot)

    async with _get_refresh_lock(slot):
        token_inside = cookie_access_token(settings, slot=slot)
        refresh_inside = cookie_refresh_token(settings, slot=slot)
        if (
            (token_inside and token_inside != token_before)
            or (refresh_inside and refresh_inside != refresh_before)
        ):
            # Başlangıçta access token hiç yoksa da başka görevin ürettiği yeni
            # değeri yakala. Eski koşul ``token_before`` zorunlu tuttuğu için
            # non-rotating refresh token kullanan bu sınır durumunda ikinci
            # istek yeniden ağa çıkıyordu.
            rem = token_seconds_remaining(token_inside) if token_inside else None
            return RefreshOutcome(
                ok=True,
                detail="Token eşzamanlı görev tarafından yenilendi (çerez güncellendi).",
                status_code=200,
                access_token=token_inside or "",
                refresh_token=refresh_inside,
                expires_in=int(rem) if rem is not None and rem > 0 else 3600,
                notes=["deduplicated_concurrent_refresh"],
            )
        return await _refresh_upstream_token_locked(settings, client, slot=slot)


async def _refresh_upstream_token_locked(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    slot: int = 1,
) -> RefreshOutcome:
    endpoint = derive_refresh_url(settings, slot=slot)
    path = urlsplit(endpoint).path if endpoint else ""
    refresh_token = cookie_refresh_token(settings, slot=slot)
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

    notes: list[str] = []
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=_REFRESH_TIMEOUT)
    try:
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

        payload = _parse_json_dict(response)

        # Upstream `apikey` istediyse ve henüz yapılandırılmamışsa sayfadan
        # otomatik keşfetmeyi dene ve isteği bir kez tekrar et.
        if (
            response.status_code != 200
            and not api_key
            and _is_missing_apikey(response.status_code, payload)
        ):
            discovered = await discover_refresh_api_key(settings, http)
            if discovered:
                notes.append("apikey otomatik keşfedildi")
                headers["apikey"] = discovered
                headers["Authorization"] = f"Bearer {discovered}"
                try:
                    response = await http.post(
                        endpoint,
                        json={"refresh_token": refresh_token},
                        headers=headers,
                        timeout=_REFRESH_TIMEOUT,
                    )
                    payload = _parse_json_dict(response)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "upstream_token_refresh_failed",
                        error_type=type(exc).__name__,
                    )
                    return RefreshOutcome(
                        ok=False,
                        detail=f"Yenileme isteği başarısız ({type(exc).__name__}).",
                        endpoint_path=path,
                        notes=notes,
                    )

        if response.status_code != 200 or not payload.get("access_token"):
            detail = _describe_failure(response.status_code, payload, endpoint)
            logger.warning(
                "upstream_token_refresh_rejected",
                status_code=response.status_code,
                endpoint_path=path,
                slot=slot,
            )
            return RefreshOutcome(
                ok=False,
                detail=detail,
                status_code=response.status_code,
                endpoint_path=path,
                notes=notes,
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
            notes=notes,
        )
        apply_refresh(settings, outcome, slot=slot)
        if slot <= 1:
            await sync_render_env(settings, http)
        logger.info(
            "upstream_token_refreshed",
            endpoint_path=path,
            expires_in=outcome.expires_in,
            rotated=bool(payload.get("refresh_token")),
            slot=slot,
        )
        return outcome
    finally:
        if owns_client:
            await http.aclose()


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
            "Upstream `apikey` istiyor: önce /apikey komutunu parametresiz "
            "göndererek otomatik keşfi deneyin; anahtarı biliyorsanız "
            "/apikey <anahtar> veya UPSTREAM_REFRESH_API_KEY kullanın."
        )
    if status in (400, 401) and ("refresh" in lowered or "token" in lowered):
        return (
            "Refresh token geçersiz/süresi dolmuş görünüyor; tarayıcıdan taze "
            "çerez gerekir (/cookie)."
        )
    if not urlsplit(endpoint).path:
        return "Yenileme adresi geçersiz; UPSTREAM_REFRESH_URL ayarlayın."
    return f"Yenileme reddedildi (HTTP {status}); mevcut token korunuyor."


def apply_refresh(
    settings: Settings, outcome: RefreshOutcome, *, slot: int = 1
) -> list[str]:
    """Yenileme sonucunu ayarlara uygular; değişen alan adlarını döndürür."""
    if not outcome.ok or not outcome.access_token:
        return []
    suffix = _slot_suffix(slot)
    access_attr = f"upstream_access_token{suffix}"
    cookie_attr = f"upstream_cookie{suffix}"

    changed: list[str] = []
    if getattr(settings, access_attr, "") != outcome.access_token:
        setattr(settings, access_attr, outcome.access_token)
        changed.append(access_attr)
    if (
        slot <= 1
        and outcome.refresh_token
        and settings.upstream_refresh_token != outcome.refresh_token
    ):
        settings.upstream_refresh_token = outcome.refresh_token
        changed.append("upstream_refresh_token")

    current_cookie = str(getattr(settings, cookie_attr, "") or "")
    rewritten, cookie_changed = rewrite_session_cookie(
        current_cookie,
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token or None,
        expires_in=outcome.expires_in,
    )
    if cookie_changed and rewritten != current_cookie:
        setattr(settings, cookie_attr, rewritten)
        changed.append(cookie_attr)
    elif current_cookie.strip() and access_attr in changed:
        # Kimliğin iki yarısı farklı kuşaktan kaldı: `Authorization` taze,
        # `Cookie` eski. Bu, tek başına bir ret sebebi olduğunu kanıtlamaz ama
        # sessizce yarı-taze kimlik göndermek yerine görünür bir iz bırakır.
        logger.warning(
            "upstream_refresh_cookie_not_rewritten",
            slot=slot,
            hint=(
                "Yeni erişim token'ı ayarlara yazıldı fakat oturum çerezi "
                "güncellenemedi (çerez biçimi tanınmadı). İstek taze Authorization "
                "+ bayat Cookie ile gidebilir; tarayıcıdan taze cURL/çerez "
                "uygulayın (/upstream/cookie)."
            ),
        )

    if slot <= 1 and changed:
        save_runtime_state(settings, source="token_refresh")
    return changed


def configured_refresh_slots(settings: Settings) -> list[int]:
    """Refresh token taşıyan hesap yuvalarını (1..MAX_ACCOUNT_SLOTS) döndürür."""
    slots: list[int] = []
    for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
        if cookie_refresh_token(settings, slot=slot):
            slots.append(slot)
    return slots


def rewrite_session_cookie(
    cookie_header: str,
    *,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
) -> tuple[str, bool]:
    """Oturum çerezindeki JSON'u (veya düz JWT çerezini) yeni token'larla günceller.

    Parçalı çerezler (`ad.0`, `ad.1`) orijinal sınır uzunlukları korunarak
    yeniden bölünür; ancak yeni gövde parça sınırlarını taşıracak kadar büyüdüyse
    veya küçüldüyse 4 KB sınırını aşmadan dengeli bölünür. Çözülemeyen bir set
    gelirse çerez **olduğu gibi** döner (`changed=False`) — bozuk bir çerez
    yazmaktansa hiç dokunmamak doğrudur.
    """
    if not cookie_header:
        return cookie_header, False
    cookies = parse_cookie_header(cookie_header)
    if not cookies:
        return cookie_header, False
    entries = _session_entries(cookies)
    if not entries:
        # Düz JWT taşıyan oturum çerezi varsa (ör. `sb-access-token=eyJ...`) onu güncelle.
        for name, value in cookies.items():
            lowered = name.lower()
            if lowered in NEVER_TOKEN_COOKIES:
                continue
            if any(hint in lowered for hint in DEFAULT_COOKIE_HINTS) and looks_like_jwt(
                value.strip()
            ):
                if cookies[name] == access_token:
                    return cookie_header, False
                return replace_cookie_values(cookie_header, {name: access_token}), True
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
    replacements: dict[str, str] = {}
    if not indices:
        replacements[base] = encoded
    else:
        lengths = [len(cookies.get(f"{base}.{index}", "")) for index in indices]
        # Orijinal parça boyları yeni gövdeye uyuyorsa birebir koru; aksi halde
        # (boş parça oluşacaksa veya son parça 4 KB sınırını aşacaksa) dengeli böl.
        prefix_sum = sum(lengths[:-1])
        last_len = len(encoded) - prefix_sum
        if prefix_sum >= len(encoded) or last_len > _MAX_COOKIE_CHUNK_LEN:
            even_size = max(1, (len(encoded) + len(indices) - 1) // len(indices))
            lengths = [even_size] * len(indices)

        cursor = 0
        for position, index in enumerate(indices):
            is_last = position == len(indices) - 1
            size = len(encoded) - cursor if is_last else lengths[position]
            replacements[f"{base}.{index}"] = encoded[cursor : cursor + size]
            cursor += size
    return replace_cookie_values(cookie_header, replacements), True


def _now() -> float:
    import time

    return time.time()
