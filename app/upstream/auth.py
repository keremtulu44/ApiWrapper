"""Oturum çerezinden `access_token` ayıklama ve `Authorization` başlığı üretimi.

Hedef servis yalnızca `Cookie` göndermeyi yeterli bulmayıp `Authorization: Bearer <token>`
başlığını da zorunlu kıldığı için, token'ın çerez içinden güvenilir biçimde çıkarılması
gerekir. Çerezler pratikte tek bir biçimde gelmez; bu modül aşağıdaki varyantların
tamamını ele alır:

1. Doğrudan token:      ``access_token=eyJhbGciOi...``
2. URL-encoded değer:   ``access_token=eyJ...%3D%3D`` veya ``%7B%22access_token%22...%7D``
3. JSON nesnesi:        ``sb-auth-token={"access_token":"eyJ...","refresh_token":"..."}``
4. JSON dizisi:         ``sb-auth-token=["eyJ...","refresh-token",null,null]``
5. base64 JSON:         ``sb-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOi...``
6. Parçalı çerezler:    ``sb-auth-token.0=base64-eyJ...``, ``sb-auth-token.1=...``
   (4096 baytlık çerez sınırı nedeniyle bölünür; sırayla birleştirilir)

Ayıklama başarısız olursa `None` döner ve çağıran taraf yalnızca `Cookie` ile devam eder.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from typing import Any
from urllib.parse import unquote, unquote_plus

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Çerez adı içinde aranacak varsayılan ipuçları (küçük harfe indirgenmiş, kısmi eşleşme).
DEFAULT_COOKIE_HINTS: tuple[str, ...] = (
    # Genel "auth" eşleşmesi: sağlayıcı/proje öneki ne olursa olsun
    # (ör. "<proje>-auth-prod-v1.0") oturum çerezini yakalar. Yanlış pozitif
    # riski NEVER_TOKEN_COOKIES filtresi ve değer doğrulamasıyla sınırlanır.
    "auth",
    "arena-auth",
    "arena_auth",
    "access_token",
    "access-token",
    "accesstoken",
    "auth-token",
    "auth_token",
    "authtoken",
    "session-token",
    "session_token",
    "__session",
    "id_token",
    "jwt",
    "token",
)

#: JSON gövdelerinde token'ı taşıyan anahtar adları (öncelik sırasıyla).
REFRESH_TOKEN_JSON_KEYS: tuple[str, ...] = (
    "refresh_token",
    "refreshToken",
    "refresh-token",
)

TOKEN_JSON_KEYS: tuple[str, ...] = (
    "access_token",
    "accessToken",
    "access-token",
    "idToken",
    "id_token",
    "token",
    "jwt",
    "sessionToken",
    "session_token",
    "authToken",
    "value",
)

#: Kırpılmış JSON gövdesinde access_token alanını yakalar.
_TRUNCATED_TOKEN_RE = re.compile(
    r'"(?:access_token|accessToken)"\s*:\s*"(?P<token>[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)"'
)

#: JWT biçimi: üç base64url parçası, noktayla ayrılmış.
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*$")

#: Parçalı çerez son eki: ``ad.0``, ``ad.1`` ...
_CHUNK_SUFFIX_RE = re.compile(r"^(?P<base>.+)\.(?P<index>\d+)$")

#: Ham (JWT olmayan) bir değerin token sayılabileceği azami uzunluk.
_MAX_RAW_TOKEN_LENGTH = 1024

#: Base64 alfabesi (dolgu ve URL-safe karakterler dâhil).
_B64_ALPHABET_RE = re.compile(r"[A-Za-z0-9+/=_-]+")

#: Token olarak kabul edilebilecek asgari uzunluk (rastgele kısa değerleri elemek için).
_MIN_TOKEN_LENGTH = 16

#: Özyinelemeli JSON taramasında izin verilen azami derinlik.
_MAX_SCAN_DEPTH = 6

#: Token olmadığı kesin olan çerezler (Cloudflare/analitik). Sezgisel aramada atlanır.
NEVER_TOKEN_COOKIES = frozenset(
    {
        "cf_clearance",
        "__cf_bm",
        "cf_chl_rc_m",
        "_cfuvid",
        "_ga",
        "_gid",
        "_gat",
        "_fbp",
        "amplitude_id",
        "intercom-session",
        "csrftoken",
        "xsrf-token",
        "_dd_s",
        "_visit_id",
        "sidebar_state",
        "user_country_code",
    }
)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Ham `Cookie` başlığını ad→değer sözlüğüne çevirir.

    Ayrıştırma bağışlayıcıdır: değer içinde ``=`` bulunabilir, boşluklar kırpılır,
    adsız parçalar yok sayılır. Aynı ad birden çok kez geçerse ilki korunur
    (tarayıcı davranışıyla uyumlu olarak en özgül çerez genelde başta gelir).
    """
    cookies: dict[str, str] = {}
    if not raw:
        return cookies
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name and name not in cookies:
            cookies[name] = value
    return cookies


def merge_chunked_cookies(cookies: dict[str, str]) -> dict[str, str]:
    """`ad.0`, `ad.1` biçimindeki parçalı çerezleri sayısal sıraya göre birleştirir.

    Orijinal parçalar da korunur; yalnızca birleştirilmiş `ad` girdisi eklenir
    (zaten mevcutsa üzerine yazılmaz).
    """
    chunks: dict[str, dict[int, str]] = {}
    for name, value in cookies.items():
        match = _CHUNK_SUFFIX_RE.match(name)
        if match:
            base = match.group("base")
            chunks.setdefault(base, {})[int(match.group("index"))] = value

    merged = dict(cookies)
    for base, parts in chunks.items():
        if base in merged:
            continue
        merged[base] = "".join(parts[idx] for idx in sorted(parts))
    return merged


def looks_like_jwt(value: str) -> bool:
    """Değerin JWT biçiminde olup olmadığını söyler."""
    return bool(value) and bool(_JWT_RE.match(value))


def _b64url_decode(segment: str) -> bytes:
    """Dolgusuz base64url dizesini çözer."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """JWT payload'ını **imza doğrulamadan** çözer.

    Yalnızca gözlemlenebilirlik (son kullanma uyarısı) amaçlıdır; güvenlik kararı
    için kullanılmaz — token'ı doğrulayan taraf upstream'dir.
    """
    if not looks_like_jwt(token):
        return {}
    try:
        payload = _b64url_decode(token.split(".")[1])
        claims = json.loads(payload)
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expiry(token: str) -> int | None:
    """JWT `exp` alanını (unix saniye) döndürür; yoksa `None`."""
    exp = decode_jwt_claims(token).get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return int(exp)
    return None


def token_seconds_remaining(token: str, now: float | None = None) -> float | None:
    """Token'ın dolmasına kalan saniye; `exp` yoksa `None`. Negatif olabilir."""
    exp = token_expiry(token)
    if exp is None:
        return None
    return exp - (time.time() if now is None else now)


def is_token_expired(token: str, leeway: float = 0.0, now: float | None = None) -> bool:
    """Token süresi dolmuş mu? `exp` taşımayan token'lar 'dolmamış' sayılır."""
    remaining = token_seconds_remaining(token, now=now)
    return remaining is not None and remaining <= leeway


def _candidate_strings(value: str) -> list[str]:
    """Bir çerez değerinin makul çözüm varyantlarını üretir (özgünler korunur)."""
    seen: list[str] = []

    def push(item: str) -> None:
        item = item.strip().strip('"')
        if item and item not in seen:
            seen.append(item)

    push(value)
    # URL-encoding bir veya iki kez uygulanmış olabilir.
    current = value
    for _ in range(2):
        decoded = unquote_plus(current)
        if decoded == current:
            break
        push(decoded)
        current = decoded
    plain = unquote(value)
    push(plain)

    # Supabase ve benzerleri gövdeyi "base64-" ön ekiyle taşır; bazı sürümler
    # ön ek olmadan doğrudan base64 gönderir. Her iki durumu da çözmeyi dene.
    for candidate in list(seen):
        body = candidate
        for prefix in ("base64-", "base64_", "b64-"):
            if candidate.startswith(prefix):
                body = candidate[len(prefix) :]
                break
        decoded_text = _try_b64_decode(body)
        if decoded_text is not None:
            push(decoded_text)
        # Çözülen metin geçerli JSON olmayabilir (çerez kırpılmışsa gövde yarıda
        # kesilir), bu yüzden kurtarma her hâlükârda denenir.
        salvaged = _salvage_truncated_b64(body)
        if salvaged is not None:
            push(salvaged)
    return seen


def _try_b64_decode(value: str) -> str | None:
    """Base64 (standart veya URL-safe) çözmeyi dener; JSON/JWT çıkarsa döndürür.

    Ön eksiz çerezlerde yanlış pozitifi önlemek için sonuç yalnızca anlamlı
    görünüyorsa (JSON gövdesi ya da JWT) kabul edilir.
    """
    candidate = value.strip()
    if len(candidate) < 16:
        return None
    try:
        padded = candidate + "=" * (-len(candidate) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=False)
        text = raw.decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    stripped = text.strip()
    if stripped.startswith(("{", "[")) or _JWT_RE.match(stripped):
        return stripped
    return None


def _salvage_truncated_b64(value: str) -> str | None:
    """Kırpılmış base64 gövdesinden token'ı kurtarmayı dener.

    Tarayıcılar 4096 baytı aşan oturum çerezlerini ``ad.0``/``ad.1`` parçalarına
    böler. Kullanıcı yalnızca ilk parçayı kopyaladığında değer geçerli base64
    değildir ve tamamı çözülemez. Base64 4 karakterlik bloklar hâlinde
    çözülebildiği için baştaki en uzun tam bloğu açar ve JSON tamamlanmamış olsa
    bile ``access_token`` alanını düz metin olarak arar.
    """
    candidate = value.strip()
    if len(candidate) < 64:
        return None
    usable = candidate[: len(candidate) - (len(candidate) % 4)]
    while usable:
        try:
            text = base64.b64decode(usable, altchars=b"-_", validate=False).decode(
                "utf-8", errors="ignore"
            )
        except (ValueError, binascii.Error):
            return None
        if text.lstrip().startswith("{"):
            match = _TRUNCATED_TOKEN_RE.search(text)
            if match:
                token = match.group("token")
                return token if looks_like_jwt(token) else None
            return None
        # Baş kısım JSON değilse hizalama kaymış olabilir; bir blok geri çekil.
        usable = usable[:-4]
    return None


def _scan_json(node: Any, depth: int = 0) -> str | None:
    """JSON ağacında token taşıyan alanı arar (önce bilinen anahtarlar)."""
    if depth > _MAX_SCAN_DEPTH:
        return None

    if isinstance(node, dict):
        for key in TOKEN_JSON_KEYS:
            value = node.get(key)
            if isinstance(value, str) and _acceptable(value):
                return value
        # İç içe yaygın kapsayıcılar.
        for key in ("currentSession", "session", "data", "user", "tokens", "auth"):
            nested = node.get(key)
            if nested is not None:
                found = _scan_json(nested, depth + 1)
                if found:
                    return found
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _scan_json(value, depth + 1)
                if found:
                    return found
        return None

    if isinstance(node, list):
        # Supabase dizi biçimi: [access_token, refresh_token, ...]
        for item in node:
            if isinstance(item, str) and looks_like_jwt(item):
                return item
        for item in node:
            if isinstance(item, (dict, list)):
                found = _scan_json(item, depth + 1)
                if found:
                    return found
    return None


def _acceptable(value: str) -> bool:
    """Değerin token olarak makul olup olmadığını değerlendirir."""
    value = value.strip()
    if len(value) < _MIN_TOKEN_LENGTH:
        return False
    return looks_like_jwt(value) or not any(ch.isspace() for ch in value)


def _valid_refresh_token(value: str) -> bool:
    candidate = value.strip()
    return bool(candidate) and not any(ch.isspace() for ch in candidate)


def _scan_refresh_json(node: Any, depth: int = 0) -> str | None:
    """Oturum JSON'unda yalnızca refresh token alanlarını arar."""
    if depth > _MAX_SCAN_DEPTH:
        return None
    if isinstance(node, dict):
        for key in REFRESH_TOKEN_JSON_KEYS:
            value = node.get(key)
            if isinstance(value, str) and _valid_refresh_token(value):
                return value.strip()
        for key in ("currentSession", "session", "data", "user", "tokens", "auth"):
            nested = node.get(key)
            if nested is not None:
                found = _scan_refresh_json(nested, depth + 1)
                if found:
                    return found
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _scan_refresh_json(value, depth + 1)
                if found:
                    return found
        return None
    if isinstance(node, list):
        # Supabase dizi biçimi: [access_token, refresh_token, ...]. Erişim
        # token'ı JWT değilse rastgele bir dizinin ikinci elemanını alma.
        if (
            len(node) > 1
            and isinstance(node[0], str)
            and looks_like_jwt(node[0])
            and isinstance(node[1], str)
            and _valid_refresh_token(node[1])
        ):
            return node[1].strip()
        for item in node:
            if isinstance(item, (dict, list)):
                found = _scan_refresh_json(item, depth + 1)
                if found:
                    return found
    return None


def _refresh_from_value(value: str, *, allow_raw: bool) -> str | None:
    candidates = _candidate_strings(value)
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped.startswith(("{", "[")):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        found = _scan_refresh_json(parsed)
        if found:
            return found
    if allow_raw:
        # URL-encoded ham refresh token'da çözülmüş varyantı tercih et.
        for candidate in reversed(candidates):
            stripped = candidate.strip()
            if not stripped.startswith(("{", "[")) and _valid_refresh_token(stripped):
                return stripped
    return None


def extract_refresh_token(
    cookie_header: str,
    *,
    cookie_names: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Cookie içindeki refresh token'ı ayıklar; hiçbir ağ isteği yapmaz.

    Erişim token'ıyla aynı URL-encoded/JSON/base64/parçalı çerez biçimleri
    desteklenir. `cookie_names` öncelikli oturum çerezlerini belirtir. Ham bir
    değer yalnızca adında `refresh` ve `token` geçen çerezden kabul edilir;
    böylece access token yanlışlıkla refresh token olarak döndürülmez.
    """
    if not cookie_header:
        return None
    cookies = merge_chunked_cookies(parse_cookie_header(cookie_header))
    # Birleştirilmiş taban çerez varken .0/.1 parçalarını ayrı token adayları
    # olarak tarama; kısa ham refresh token'da ilk parça yanlışlıkla dönebilir.
    entries = [
        (name, value) for name, value in cookies.items() if not _CHUNK_SUFFIX_RE.match(name)
    ]
    prioritized: list[tuple[str, str]] = []
    for wanted in cookie_names or ():
        needle = wanted.strip().lower()
        if not needle:
            continue
        prioritized.extend(
            (name, value)
            for name, value in entries
            if needle == name.lower() or needle in name.lower()
        )
    prioritized.extend(entries)
    seen: set[tuple[str, str]] = set()
    for name, value in prioritized:
        if (name, value) in seen or name.lower() in NEVER_TOKEN_COOKIES:
            continue
        seen.add((name, value))
        lowered = name.lower()
        token = _refresh_from_value(
            value,
            allow_raw="refresh" in lowered and "token" in lowered,
        )
        if token:
            return token
    return None


def _token_from_value(value: str) -> str | None:
    """Tek bir çerez değerinden token çıkarmayı dener."""
    for candidate in _candidate_strings(value):
        stripped = candidate.strip()
        if looks_like_jwt(stripped):
            return stripped
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            found = _scan_json(parsed)
            if found:
                return found
    # JSON/JWT değilse, ham değer yeterince uzun ve boşluksuzsa kabul et.
    # Ancak çözülemeyen base64 oturum gövdesini token diye göndermek, upstream'e
    # kilometrelerce anlamsız bir Authorization başlığı yollamak demektir.
    for candidate in _candidate_strings(value):
        if _looks_like_session_blob(candidate):
            continue
        if _acceptable(candidate):
            return candidate.strip()
    return None


def _looks_like_session_blob(value: str) -> bool:
    """Değerin, token değil kodlanmış oturum gövdesi olduğunu tahmin eder."""
    candidate = value.strip().strip('"')
    if candidate.startswith(("base64-", "base64_", "b64-")):
        return True
    # JWT olmayan, çok uzun ve base64 alfabesinden oluşan değerler oturum
    # gövdesidir; gerçek erişim token'ları bu boyuta ulaşmaz.
    return (
        len(candidate) > _MAX_RAW_TOKEN_LENGTH
        and not looks_like_jwt(candidate)
        and _B64_ALPHABET_RE.fullmatch(candidate) is not None
    )


def extract_access_token(
    cookie_header: str,
    *,
    cookie_names: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Ham `Cookie` başlığından `access_token` değerini ayıklar.

    Args:
        cookie_header: Tarayıcıdan alınan ham `Cookie` başlığı.
        cookie_names: Öncelikli aranacak çerez adları. Verilirse tam ad eşleşmesi
            (küçük/büyük harf duyarsız) önce denenir; ardından varsayılan ipuçlarıyla
            kısmi eşleşmeye geçilir.

    Returns:
        Bulunan token veya `None`.
    """
    if not cookie_header:
        return None

    cookies = merge_chunked_cookies(parse_cookie_header(cookie_header))
    if not cookies:
        return None

    lowered = {name.lower(): (name, value) for name, value in cookies.items()}

    # 1) Kullanıcının açıkça belirttiği adlar (tam eşleşme).
    for wanted in cookie_names or ():
        entry = lowered.get(wanted.strip().lower())
        if entry:
            token = _token_from_value(entry[1])
            if token:
                return token

    # 2) Kullanıcının belirttiği adlar (kısmi eşleşme).
    for wanted in cookie_names or ():
        needle = wanted.strip().lower()
        if not needle:
            continue
        for name_lower, (_, value) in lowered.items():
            if needle in name_lower:
                token = _token_from_value(value)
                if token:
                    return token

    # 3) Varsayılan ipuçları — önce JWT görünümlü sonuçları tercih et.
    fallback: str | None = None
    for hint in DEFAULT_COOKIE_HINTS:
        for name_lower, (_, value) in lowered.items():
            if hint not in name_lower or name_lower in NEVER_TOKEN_COOKIES:
                continue
            token = _token_from_value(value)
            if not token:
                continue
            if looks_like_jwt(token):
                return token
            fallback = fallback or token
    if fallback:
        return fallback

    # 4) Son çare: herhangi bir çerezin içinde JWT taşıyan değer var mı?
    for name, value in cookies.items():
        if name.lower() in NEVER_TOKEN_COOKIES:
            continue
        for candidate in _candidate_strings(value):
            if looks_like_jwt(candidate.strip()):
                return candidate.strip()
    return None


def resolve_bearer_token(
    *,
    explicit_token: str = "",
    cookie_header: str = "",
    cookie_names: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    """Kullanılacak bearer token'ı belirler.

    Öncelik: açıkça verilen `UPSTREAM_ACCESS_TOKEN` → çerezden ayıklanan token.
    `Bearer ` ön eki verilmişse temizlenir.
    """
    if explicit_token and explicit_token.strip():
        token = explicit_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None
    return extract_access_token(cookie_header, cookie_names=cookie_names)
