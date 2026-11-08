"""Tarayıcı taklidi HTTP başlıklarının üretimi.

cURL analizindeki başlıklar birebir korunur; Chrome'un gönderdiği
`sec-*` başlıkları da eklenerek istek gerçekçi hale getirilir.
"""

from __future__ import annotations

import re
import secrets

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.auth import (
    is_token_expired,
    parse_cookie_header,
    resolve_bearer_token,
    token_seconds_remaining,
)

logger = get_logger(__name__)

#: Token bulunamadigi uyarisi her istekte degil, sureç basina bir kez verilir.
_WARNED_NO_TOKEN = False

#: İstemci sürümünü/deployment'ını taşıyan başlıklar (Vercel skew protection).
#: Değer `UPSTREAM_CURL`'den donmuş gelir; güncel değer kanaryadan okunur
#: (`app/upstream/deployment.py`).
DEPLOYMENT_HEADERS: tuple[str, ...] = ("x-deployment-id", "x-arena-web-deployment")

#: Stream cURL'ünden korunan bu başlıklar HTML ısıtma/anon-key keşif isteğine
#: aynen taşınmamalıdır. Örneğin akışın `sec-fetch-mode: cors` değeri sayfa
#: navigasyonunda `navigate` olarak kalmalıdır. Client Hint başlıkları ise iki
#: istekte de aynı tarayıcıyı temsil ettiği için korunur.
_STREAM_ONLY_EXTRA_HEADERS = frozenset(
    {
        "accept",
        "cache-control",
        "pragma",
        "priority",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
    }
)

_CHROME_VERSION_RE = re.compile(r"Chrome/(\d+)")
_EDGE_VERSION_RE = re.compile(r"(?:Edg|Edge|EdgA|EdgiOS)/(\d+)")


def chrome_major_version(user_agent: str) -> str:
    match = _CHROME_VERSION_RE.search(user_agent)
    return match.group(1) if match else "152"


def platform_from_ua(user_agent: str) -> str:
    ua = user_agent.lower()
    if "windows" in ua:
        return '"Windows"'
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"'
    if "android" in ua:
        return '"Android"'
    if "iphone" in ua or "ipad" in ua or "ios" in ua:
        return '"iOS"'
    if "linux" in ua:
        return '"Linux"'
    return '"Unknown"'


def is_mobile_ua(user_agent: str) -> bool:
    """UA mobil istemciye ait mi? Client Hint '?0'/'?1' doğru üretilmeli."""
    ua = user_agent.lower()
    # Android telefon, iPhone, Lumia, Opera Mini, Mobil Safari vb.
    return any(m in ua for m in ("android", "iphone", "ipad", "ipod",
                                 "mobile", "lumia", "opera mini",
                                 "windows phone"))


#: Client Hint başlıkları; değerleri User-Agent ile **aynı tarayıcıya** ait
#: olmalıdır. `UPSTREAM_EXTRA_HEADERS` tarayıcıdan birebir kopyalandığı için
#: normalde örtüşür. Çoklu hesapta (`UPSTREAM_USER_AGENT_2`) veya kısmen
#: yenilenmiş bir `.env`'de ise yuva-1'in donmuş ipuçları başka bir UA ile
#: gönderilir; UA ile çelişen ipucu gerçek bir isteğin asla üretemeyeceği bir
#: parmak izi çelişkisi yaratır.
_CLIENT_HINT_HEADERS: tuple[str, ...] = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")

_CHROME_BRAND_MAJOR_RE = re.compile(r'"[^"]*";\s*v="?(\d+)')

#: Aynı çelişki uyarısı logda tekrarlanmasın (değer başına tek satır).
_warned_hint_conflicts: set[str] = set()


def client_hint_conflicts(
    user_agent: str, overrides: dict[str, str]
) -> dict[str, str]:
    """`overrides` içinde bu UA ile **çelişen** client hint'lerini döndürür.

    Yalnızca Chromium ailesi için değerlendirilir: `sec-ch-*` başlıklarını
    Firefox/Safari hiç göndermez, dolayısıyla orada "bilinmeyen UA" yüzünden
    doğru bir ipucunu atmak yanlış olurdu. Dönen sözlük `{başlık: UA'ya göre
    beklenen değer}` biçimindedir.
    """
    if "chrome/" not in user_agent.lower():
        return {}
    conflicts: dict[str, str] = {}
    for name, value in overrides.items():
        lowered = name.lower()
        if lowered not in _CLIENT_HINT_HEADERS:
            continue
        current = value.strip()
        if not current:
            continue
        if lowered == "sec-ch-ua-mobile":
            expected = "?1" if is_mobile_ua(user_agent) else "?0"
            if current != expected:
                conflicts[lowered] = expected
        elif lowered == "sec-ch-ua-platform":
            expected = platform_from_ua(user_agent)
            # UA'dan platform çıkarılamıyorsa çelişki kanıtlanmış sayılmaz.
            if expected != '"Unknown"' and current != expected:
                conflicts[lowered] = expected
        elif lowered == "sec-ch-ua":
            expected_major = chrome_major_version(user_agent)
            majors = set(_CHROME_BRAND_MAJOR_RE.findall(current))
            # Marka sürümleri UA'daki Chrome ana sürümünü hiç içermiyorsa bu
            # ipucu başka bir tarayıcıdan kopyalanmıştır.
            if majors and expected_major and expected_major not in majors:
                conflicts[lowered] = sec_ch_ua(user_agent)
    return conflicts


def _drop_conflicting_hints(headers: dict[str, str], settings: Settings) -> None:
    """UA ile çelişen donmuş client hint'lerini başlık kümesinden çıkarır.

    Çıkarılan değerlerin yerini `build_*_headers`'ın UA'dan hesapladığı
    tutarlı değerler alır; yani istek "eksik" değil, **kendisiyle barışık**
    olur. Ham değer loglanmaz, yalnızca başlık adı ve UA'dan beklenen değer
    paylaşılır (ikisi de gizli bilgi değildir).
    """
    if not settings.upstream_client_hint_autofix:
        return
    conflicts = client_hint_conflicts(settings.upstream_user_agent, headers)
    if not conflicts:
        return
    for name, expected in conflicts.items():
        for key in [k for k, v in headers.items() if k.lower() == name]:
            del headers[key]
        headers[name] = expected
    fingerprint = f"{settings.target_domain}:{','.join(sorted(conflicts))}"
    if fingerprint not in _warned_hint_conflicts:
        _warned_hint_conflicts.add(fingerprint)
        if len(_warned_hint_conflicts) > 32:  # pragma: no cover - savunmacı
            _warned_hint_conflicts.clear()
        logger.warning(
            "upstream_client_hint_mismatch",
            hints=sorted(conflicts),
            user_agent=settings.upstream_user_agent[:80],
            hint=(
                "UPSTREAM_EXTRA_HEADERS içindeki client hint değerleri gönderilen "
                "User-Agent ile çelişiyor; UA'dan hesaplanan değerler kullanıldı. "
                "Başlıklar aynı tarayıcının taze cURL'ünden kopyalanmalı "
                "(çok hesaplı kurulumda UPSTREAM_USER_AGENT_2 gibi alanlar için de)."
            ),
        )


def sec_ch_ua(user_agent: str) -> str:
    version = chrome_major_version(user_agent)
    edge = _EDGE_VERSION_RE.search(user_agent)
    if edge:
        # Edge UA'si yalnızca Google Chrome markasıyla taklit edilemez; gerçek
        # tarayıcıda Chromium, Microsoft Edge ve Not A(Brand sırası gönderilir.
        return (
            f'"Chromium";v="{version}", "Microsoft Edge";v="{edge.group(1)}", '
            '"Not A(Brand";v="99"'
        )
    # Sıra Chrome'un gönderdiğiyle birebir aynı olmalı (parmak izi).
    return (
        f'"Chromium";v="{version}", "Not?A_Brand";v="24", '
        f'"Google Chrome";v="{version}"'
    )


def build_authorization(settings: Settings) -> str | None:
    """`Authorization` başlık değerini üretir.

    Configured modunda açık token veya etkinse çerez tokenı kullanılır.
    Cookie-only modunda ve token yoksa `None` döner; destek upstream'e bağlıdır.
    """
    if settings.upstream_auth_header_mode == "cookie_only":
        return None
    if not settings.upstream_auth_from_cookie and not settings.upstream_access_token:
        return None

    token = resolve_bearer_token(
        explicit_token=settings.upstream_access_token,
        cookie_header=settings.upstream_cookie if settings.upstream_auth_from_cookie else "",
        cookie_names=settings.upstream_token_cookie_names,
    )
    if not token:
        global _WARNED_NO_TOKEN
        if (settings.upstream_cookie or settings.upstream_access_token) and not _WARNED_NO_TOKEN:
            _WARNED_NO_TOKEN = True
            names = sorted(parse_cookie_header(settings.upstream_cookie))
            logger.warning(
                "access_token_not_found",
                cookies_seen=names,
                hint=(
                    "No access token could be extracted from UPSTREAM_COOKIE. The request "
                    "is still sent with the Cookie header only. If upstream returns 401, "
                    "set UPSTREAM_ACCESS_TOKEN directly, or point at the right cookie with "
                    "UPSTREAM_TOKEN_COOKIE_NAMES. If upstream works fine, set "
                    "UPSTREAM_AUTH_FROM_COOKIE=false to silence this."
                ),
            )
        return None

    if settings.upstream_warn_on_expired_token and is_token_expired(token):
        remaining = token_seconds_remaining(token)
        logger.warning(
            "access_token_expired",
            expired_seconds_ago=abs(int(remaining)) if remaining is not None else None,
            hint="Refresh UPSTREAM_COOKIE / UPSTREAM_ACCESS_TOKEN; upstream will return 401.",
        )

    scheme = settings.upstream_auth_scheme.strip()
    return f"{scheme} {token}" if scheme else token


def _apply_auth(headers: dict[str, str], settings: Settings) -> None:
    """Authorization başlığını yerinde ekler (varsa)."""
    authorization = build_authorization(settings)
    if authorization:
        headers["authorization"] = authorization


def build_stream_headers(
    settings: Settings,
    chat_id: str,
    *,
    include_cookie: bool = True,
    deployment_id: str | None = None,
) -> dict[str, str]:
    """Stream uç noktası için tam başlık kümesi.

    `deployment_id` verilirse, kullanıcının **zaten gönderdiği** deployment
    başlıkları (varsa) bu taze değerle değiştirilir. Böylece tanımadığımız
    upstream'lere yeni başlık uydurmayız; yalnızca eskimesi bilinen damgayı
    tazeleriz.
    """
    ua = settings.upstream_user_agent
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    headers: dict[str, str] = {
        "accept": "*/*",
        "accept-language": settings.upstream_accept_language,
        "content-type": "text/plain;charset=UTF-8",
        "origin": settings.origin,
        "referer": settings.referer_url(chat_id),
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?1" if is_mobile_ua(ua) else "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "tracestate": "dd=s:1;o:rum",
    }
    if include_cookie and settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    _apply_auth(headers, settings)
    # Kullanıcı tanımlı başlıklar en son uygulanır; bilinçli override'a izin verilir.
    headers.update(settings.parsed_extra_headers())
    # Donmuş parmak izi, gönderilen UA ile çelişiyorsa UA kazanır.
    _drop_conflicting_hints(headers, settings)
    if settings.upstream_auth_header_mode == "cookie_only":
        # Ek başlıklardaki farklı harfli Authorization da bu açık modda gitmez.
        headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
    if deployment_id:
        # Adlar küçük harfe indirgenmiş olsa da eşleşme yine de kapsayıcı
        # yapılsın: başka bir yerden gelen başlık demeti burada birikebilir.
        for key in [k for k in headers if k.lower() in DEPLOYMENT_HEADERS]:
            headers[key] = deployment_id
    return headers


def build_page_headers(settings: Settings, *, include_cookie: bool = True) -> dict[str, str]:
    """Oturum ısıtma (HTML sayfa) istekleri için başlıklar."""
    ua = settings.upstream_user_agent
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": settings.upstream_accept_language,
        "user-agent": ua,
        "sec-ch-ua": sec_ch_ua(ua),
        "sec-ch-ua-mobile": "?1" if is_mobile_ua(ua) else "?0",
        "sec-ch-ua-platform": platform_from_ua(ua),
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
    }
    if include_cookie and settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    page_extras = {
        name: value
        for name, value in settings.parsed_extra_headers().items()
        if name.lower() not in _STREAM_ONLY_EXTRA_HEADERS
        and not (settings.upstream_auth_header_mode == "cookie_only"
                 and name.lower() == "authorization")
    }
    headers.update(page_extras)
    # Isınma/kanarya GET'i aynı oturumu temsil eder: UA ile çelişen ipuçları
    # burada da düşürülür, aksi halde POST ile GET farklı tarayıcı gibi görünür.
    _drop_conflicting_hints(headers, settings)
    return headers
