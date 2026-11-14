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

#: Client Hint (`sec-ch-ua*`) gönderen motor aileleri. Yalnız Chromium gönderir.
_HINT_ENGINE_RE = re.compile(r"(?:Chrome|Chromium|SamsungBrowser|HeadlessChrome)/")
#: iOS'ta Chrome/Edge/Firefox da WebKit'tir ve Client Hint **göndermez**.
_WEBKIT_IOS_RE = re.compile(r"(?:CriOS|FxiOS|EdgiOS|OPiOS)/")


def chrome_major_version(user_agent: str) -> str:
    match = _CHROME_VERSION_RE.search(user_agent)
    return match.group(1) if match else "152"


def sends_client_hints(user_agent: str) -> bool:
    """UA Client Hint gönderen bir tarayıcıya mı ait?

    Safari/Firefox hiç göndermez; iOS'ta Chrome ve Edge de WebKit olduğu için
    göndermez. Göndermeyen bir UA'ya `sec-ch-ua` uydurmak, yakalanan cihazın
    parmak izini **bozar**: iPhone Safari'ye `"Google Chrome";v="152"` yazmak
    gerçek trafikte asla görülmeyecek bir kombinasyondur.
    """
    if _WEBKIT_IOS_RE.search(user_agent):
        return False
    return bool(_HINT_ENGINE_RE.search(user_agent))


def platform_from_ua(user_agent: str) -> str:
    ua = user_agent.lower()
    if "windows" in ua:
        return '"Windows"'
    # iPhone/iPad UA'sı "like Mac OS X" içerir; macOS kontrolü önce gelirse
    # mobil cihaz masaüstü sayılır (`sec-ch-ua-platform: "macOS"` + `?1`).
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return '"iOS"'
    if "macintosh" in ua or "mac os x" in ua:
        return '"macOS"'
    if "android" in ua:
        return '"Android"'
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


_KNOWN_PLATFORMS = frozenset({"Windows", "macOS", "Linux", "Android", "iOS", "Chrome OS"})


def fingerprint_conflicts(
    user_agent: str, extra_headers: dict[str, str]
) -> list[str]:
    """Yakalanan cihaz izi UA ile çelişiyor mu?

    cURL'ün `sec-ch-ua*` başlıkları `UPSTREAM_EXTRA_HEADERS` içinde aynen
    saklanır. Bu başlıklar ile `user-agent` farklı cihazlardan geldiyse (iki
    ayrı yakalamanın karışması, yanlış cURL, elle düzenleme) istek kendi
    kendisiyle çelişir ve anti-bot katmanı bunu görür. Sessiz kalmak yerine
    çelişki adıyla söylenir.
    """
    lower = {name.lower(): value.strip() for name, value in extra_headers.items()}
    problems: list[str] = []

    flag = lower.get("sec-ch-ua-mobile", "")
    if flag in {"?0", "?1"}:
        ua_is_mobile = is_mobile_ua(user_agent)
        if (flag == "?1") != ua_is_mobile:
            problems.append(
                f"sec-ch-ua-mobile={flag} ama user-agent "
                f"{'mobil' if ua_is_mobile else 'masaüstü'} diyor"
            )

    platform = lower.get("sec-ch-ua-platform", "").strip('"')
    if platform in _KNOWN_PLATFORMS and user_agent.strip():
        expected = platform_from_ua(user_agent).strip('"')
        if expected != "Unknown" and platform != expected:
            problems.append(
                f"sec-ch-ua-platform=\"{platform}\" ama user-agent \"{expected}\" diyor"
            )
    return problems


def client_hint_headers(user_agent: str) -> dict[str, str]:
    """UA'nın gerçekten gönderdiği Client Hint başlıkları (yoksa boş sözlük)."""
    if not sends_client_hints(user_agent):
        return {}
    return {
        "sec-ch-ua": sec_ch_ua(user_agent),
        "sec-ch-ua-mobile": "?1" if is_mobile_ua(user_agent) else "?0",
        "sec-ch-ua-platform": platform_from_ua(user_agent),
    }


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
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "priority": "u=1, i",
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "tracestate": "dd=s:1;o:rum",
    }
    # Client Hint başlıkları yalnız UA gerçekten gönderiyorsa eklenir;
    # yakalanan cihaz Safari/Firefox ise bu üç başlık **hiç** gitmemelidir.
    headers.update(client_hint_headers(ua))
    if include_cookie and settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    _apply_auth(headers, settings)
    # Kullanıcı tanımlı başlıklar en son uygulanır; bilinçli override'a izin verilir.
    headers.update(settings.parsed_extra_headers())
    if settings.upstream_auth_header_mode == "cookie_only":
        # Ek başlıklardaki farklı harfli Authorization da bu açık modda gitmez.
        headers = {key: value for key, value in headers.items() if key.lower() != "authorization"}
    if deployment_id:
        # Değiştirme **ada duyarsız** olmalı: başlık adları cURL'den küçük harfle
        # geliyor (`curl_import` satır 93) ama `UPSTREAM_EXTRA_HEADERS` elle
        # yazıldığında `X-Arena-Web-Deployment` gibi karışık yazılabiliyor. Duyarlı
        # karşılaştırmada kanarya taze değeri okuyor ama yazamıyordu: istek donmuş
        # damgayla gidip 409 `refresh_required` alıyor ve operatör yanlış yöne
        # (CAPTCHA token'ı) bakıyordu. Anahtarın kendi yazımı korunur.
        wanted = set(DEPLOYMENT_HEADERS)
        for name in list(headers):
            if name.lower() in wanted:
                headers[name] = deployment_id
    return headers


def build_page_headers(settings: Settings, *, include_cookie: bool = True) -> dict[str, str]:
    """Oturum ısıtma (HTML sayfa) istekleri için başlıklar."""
    ua = settings.upstream_user_agent
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": settings.upstream_accept_language,
        "user-agent": ua,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "upgrade-insecure-requests": "1",
    }
    headers.update(client_hint_headers(ua))
    if include_cookie and settings.upstream_cookie:
        headers["cookie"] = settings.upstream_cookie
    headers.update(
        {
            name: value
            for name, value in settings.parsed_extra_headers().items()
            if name.lower() not in _STREAM_ONLY_EXTRA_HEADERS
            and not (settings.upstream_auth_header_mode == "cookie_only"
                     and name.lower() == "authorization")
        }
    )
    return headers
