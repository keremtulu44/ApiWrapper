"""Tarayıcı cURL'ünü hem CLI importer hem de çalışan uygulama için ayrıştırır.

Bu modül özellikle ``scripts`` klasörüne bağımlı değildir; Docker/Render imajında
uygulama, secret ``UPSTREAM_CURL`` değerini aynı kurallarla işleyebilir. Ham cURL,
cookie ve token değerleri bu modülde loglanmaz veya diske yazılmaz.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.upstream.auth import extract_access_token, looks_like_jwt
from app.upstream.header_store import format_extra_headers


def normalize_curl(raw: str) -> str:
    """Windows (`^`) ve bash (`\\`) satır devamı kaçışlarını temizler."""
    text = raw.strip()
    # Windows cmd: ^" -> "  ve satır sonundaki ^
    text = re.sub(r"\^\s*\n", " ", text)
    text = text.replace('^"', '"').replace("^%", "%").replace("^^", "^")
    # bash: satır sonu \
    text = re.sub(r"\\\s*\n", " ", text)
    # PowerShell: satır sonu `
    text = re.sub(r"`\s*\n", " ", text)
    return " ".join(text.split())


def _split_top_level(text: str) -> list[tuple[str, str]]:
    """`-H "..."`, `-b "..."`, `--data-raw "..."` çiftlerini sırayla çıkarır.

    `shlex` bitişik tırnaklı parçaları birleştirip JSON gövdesini bozduğu için
    tırnak eşleştirmesi elle yapılır.
    """
    pairs: list[tuple[str, str]] = []
    pattern = re.compile(
        r"(--data-raw|--data-binary|--data|--header|--url|--cookie|-H|-d|-b)\s+"
    )
    pos = 0
    while True:
        match = pattern.search(text, pos)
        if not match:
            break
        flag = match.group(1)
        start = match.end()
        if start < len(text) and text[start] in "\"'":
            quote = text[start]
            end = start + 1
            buf: list[str] = []
            while end < len(text):
                if text[end] == "\\" and end + 1 < len(text):
                    buf.append(text[end + 1])
                    end += 2
                    continue
                if text[end] == quote:
                    # Bitişik aynı tırnak: JSON içi tırnak olabilir, ileriye bak.
                    nxt = end + 1
                    while nxt < len(text) and text[nxt] == " ":
                        nxt += 1
                    if nxt >= len(text) or text[nxt] in "-\n" or text[nxt:nxt + 2] == "--":
                        break
                    buf.append(text[end])
                    end += 1
                    continue
                buf.append(text[end])
                end += 1
            value = "".join(buf)
            pos = end + 1
        else:
            end = text.find(" ", start)
            end = len(text) if end == -1 else end
            value = text[start:end]
            pos = end
        pairs.append((flag, value))
    return pairs


def parse_curl(raw: str) -> dict[str, Any]:
    """cURL komutundan url, başlıklar, çerezler ve gövdeyi çıkarır."""
    text = normalize_curl(raw)

    url = ""
    headers: dict[str, str] = {}
    cookie = ""
    body = ""

    for flag, value in _split_top_level(text):
        if flag in ("-H", "--header"):
            name, _, header_value = value.partition(":")
            headers[name.strip().lower()] = header_value.strip()
        elif flag in ("-b", "--cookie"):
            cookie = value
        elif flag in ("--data-raw", "--data", "-d", "--data-binary"):
            body = value
        elif flag == "--url":
            url = value

    if not url:
        found = re.search(r"https?://[^\s\"']+", text)
        if found:
            url = found.group(0)
    if cookie:
        headers["cookie"] = cookie
    return {"url": url, "headers": headers, "body": body}


def load_body(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}



#: Gövdede captcha token'ını taşıyabilecek alan adları.
CAPTCHA_FIELDS: tuple[str, ...] = (
    "recaptchaV2Token",
    "recaptchaV3Token",
    "recaptchaToken",
    "captchaToken",
    "turnstileToken",
)

#: Tarayıcı parmak izi için korunan, dinamik olmayan ek başlıklar.
#: Authorization/cookie gibi kimlikler ve her istekte değişen traceparent buraya
#: alınmaz. Rastgele bütün x-* başlıklarını almak gizli anahtar da sızdırabilirdi.
#: Client Hint ve Fetch Metadata başlıkları ise uydurulmamalıdır: özellikle mobil
#: cURL'deki `sec-ch-ua-mobile: ?1` değerini masaüstü `?0` ile değiştirmek isteğin
#: gerçek tarayıcı parmak izini bozar.
PASSTHROUGH_HEADERS: tuple[str, ...] = (
    "accept",
    "cache-control",
    "dnt",
    "pragma",
    "priority",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "x-arena-web-deployment",
    "x-deployment-id",
)


#: Oturum çerezi olma ihtimali olan adlar (uyarı üretmek için).
_SESSION_HINT = "auth"

#: reCAPTCHA site key biçimi (Google "6L..." public anahtarı).
_RECAPTCHA_SITE_KEY_RE = re.compile(r"\b6L[A-Za-z0-9_-]{30,}\b")

#: Telemetri/analitik uç noktaları: yanlış isteğin cURL'ü kopyalanmış demektir.
TELEMETRY_HOSTS: tuple[str, ...] = (
    "datadoghq",
    "sentry.io",
    "google-analytics",
    "googletagmanager",
    "posthog",
    "segment.io",
    "amplitude",
    "mixpanel",
    "doubleclick",
    "facebook",
    "clarity.ms",
    "bugsnag",
    "newrelic",
    "intercom",
    "hotjar",
)

#: Aynı domain veya proxy üzerinden gönderilen kanıtlanmış telemetri yolları.
#: Host kontrolü tek başına yeterli değil: PostHog capture, uygulamanın kendi
#: domainindeki `/rpc/i/v0/e/` altından da gönderilebiliyor.
TELEMETRY_PATH_PREFIXES: tuple[str, ...] = (
    "/rpc/i/v0/e",
    "/api/v2/rum",
    "/g/collect",
)

#: Varsayılan akış yolu (app/core/config.py ile aynı).
DEFAULT_STREAM_PATH = "/nextjs-api/stream/post-to-evaluation/{chat_id}"

#: URL yolunda sohbet kimliği olarak geçebilecek UUID biçimi.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: Hedef akış ucunu tanıyan izler. Eski `post-to-evaluation/{chat_id}` ve yeni
#: `create-evaluation` (kimlik gövdede) birlikte kabul edilir.
_STREAM_PATH_HINTS: tuple[str, ...] = (
    "post-to-evaluation",
    "create-evaluation",
)



def extract_captcha(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Gövdeden captcha alan adını ve değerini bulur."""
    for field in CAPTCHA_FIELDS:
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            return field, value.strip()
    # Bilinmeyen bir ad kullanılmışsa sezgisel olarak ara.
    for key, value in body.items():
        if "captcha" in key.lower() and isinstance(value, str) and value.strip():
            return key, value.strip()
    return None, None


def extract_authorization(headers: dict[str, str]) -> tuple[str, str] | None:
    """`authorization` başlığından (şema, token) çiftini çıkarır.

    Bu başlık cURL'de varsa upstream onu bekliyor demektir; `.env`'e yazılmazsa
    istek eksik parmak iziyle gider ve hedef 401 döner.
    """
    raw = (headers.get("authorization") or "").strip()
    if not raw:
        return None
    scheme, _, token = raw.partition(" ")
    if token.strip():
        return scheme.strip(), token.strip()
    return "", raw  # şemasız token


def extract_referer_path(headers: dict[str, str]) -> str | None:
    """`referer` başlığından sohbet sayfası yolunu `{chat_id}` şablonu olarak alır."""
    raw = (headers.get("referer") or headers.get("origin") or "").strip()
    if not raw:
        return None
    split = urlsplit(raw)
    if not split.path or split.path == "/":
        return None
    match = _UUID_RE.search(split.path)
    if match:
        return split.path[: match.start()] + "{chat_id}" + split.path[match.end() :]
    return split.path



def build_settings(
    parsed: dict[str, Any], existing: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str], str | None]:
    """cURL çıktısından `.env` çiftlerini, uyarıları ve model kimliğini üretir.

    `existing` mevcut `.env` içeriğidir; alan adı, sağlayıcı ve başlık modu
    uyarıları için kullanılır.
    """
    headers: dict[str, str] = parsed["headers"]
    body = load_body(parsed["body"])
    warnings: list[str] = []
    env: dict[str, str] = {}

    # --- alan adı
    url = parsed["url"]
    split = urlsplit(url) if url else None
    host = split.netloc if split else ""
    if host:
        env["TARGET_DOMAIN"] = host
        previous = (existing or {}).get("TARGET_DOMAIN", "")
        if previous and previous != host:
            warnings.append(
                f"UYARI: bu cURL '{host}' alan adına ait ancak .env'de '{previous}' "
                "kayıtlı. Havuz aynı servisin farklı hesaplarını bekler; farklı bir "
                "servisin cURL'ü olabilir."
            )
    else:
        warnings.append("URL bulunamadı; TARGET_DOMAIN elle yazılmalı.")

    # Yanlış isteğin cURL'ü kopyalandığında sessizce telemetri sunucusuna
    # yönlenmemek için erken uyar.
    lowered_host = host.lower()
    lowered_path = split.path.lower() if split else ""
    telemetry_host = any(marker in lowered_host for marker in TELEMETRY_HOSTS)
    telemetry_path = any(
        lowered_path.startswith(prefix) for prefix in TELEMETRY_PATH_PREFIXES
    )
    if telemetry_host or telemetry_path:
        telemetry_target = host if telemetry_host else (split.path if split else "")
        warnings.append(
            f"KRİTİK: '{telemetry_target}' bir telemetri/analitik/olay toplama "
            "isteği; sohbet ucu değil. Network sekmesinde sohbet akış isteğine "
            "('create-evaluation' veya 'post-to-evaluation') sağ tıklayıp cURL'ü "
            "yeniden kopyalayın."
        )
    elif split and not looks_like_stream_path(split.path):
        warnings.append(
            f"URL yolu beklenen sohbet akış ucunu içermiyor: "
            f"{split.path or '/'}. Yanlış isteğin cURL'ü kopyalanmış olabilir "
            "(create-evaluation / post-to-evaluation arayın)."
        )

    # --- akış yolu (sabit varsayım yerine cURL'deki gerçek yol)
    if split and split.path:
        full_path = split.path
        if split.query:
            full_path = f"{full_path}?{split.query}"
        template = stream_path_template(full_path, body)
        env["UPSTREAM_STREAM_PATH"] = template
        if template != DEFAULT_STREAM_PATH:
            warnings.append(
                f"Akış yolu varsayılandan farklı, ayarlara uygulandı: {template}"
            )

    # --- başlıklar
    cookie = headers.get("cookie", "").strip()
    # Yuva tazeleme tam bir kimlik anlık görüntüsüdür: authorization-only yeni
    # cURL'de eski cookie'nin hayatta kalıp başka oturumla karışmasını önle.
    env["UPSTREAM_COOKIE"] = cookie
    if cookie:
        names = [p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p]
        if not names:
            warnings.append(
                "cookie başlığında 'ad=değer' çifti yok. Muhtemelen değerin yalnızca "
                "bir parçası kopyalanmış; cURL'ü yeniden alın."
            )
        elif not any(_SESSION_HINT in n.lower() for n in names):
            warnings.append(
                f"Çerezler arasında oturum çerezi ('*auth*') görünmüyor: {names}. "
                "Siteye giriş yapmış durumdayken cURL almanız gerekir."
            )
    else:
        warnings.append("cookie başlığı yok; giriş yapmış bir istekten cURL alın.")

    if lang := headers.get("accept-language", "").strip():
        env["UPSTREAM_ACCEPT_LANGUAGE"] = lang
    if agent := headers.get("user-agent", "").strip():
        env["UPSTREAM_USER_AGENT"] = agent

    # Yalnız bilinen, dinamik olmayan parmak izi başlıklarını koru. Değerler
    # tek satırlık `K1=V1;K2=V2` biçimine getirilir; eski hedefe ait değerlerin
    # kalmasını önlemek için cURL'de hiçbiri yoksa alan boş yazılır.
    extra_headers: dict[str, str] = {}
    for name in PASSTHROUGH_HEADERS:
        value = headers.get(name, "").strip()
        if value:
            extra_headers[name] = value.replace("\r", " ").replace("\n", " ")
    env["UPSTREAM_EXTRA_HEADERS"] = format_extra_headers(extra_headers)

    # --- referer yolu (sohbet sayfası şablonu; parmak izi için gönderilir)
    if referer_path := extract_referer_path(headers):
        env["UPSTREAM_REFERER_PATH"] = referer_path

    # --- Authorization -----------------------------------------------------
    # cURL'de bu başlık varsa upstream onu bekliyordur. Yazılmazsa istek eksik
    # gider ve hedef 401 döner; eskiden başlık okunuyor ama sessizce atılıyordu.
    auth = extract_authorization(headers)
    if auth is not None:
        scheme, token = auth
        env["UPSTREAM_ACCESS_TOKEN"] = token
        env["UPSTREAM_AUTH_FROM_COOKIE"] = "false"
        env["UPSTREAM_AUTH_SCHEME"] = scheme
        if (existing or {}).get("UPSTREAM_AUTH_HEADER_MODE") == "cookie_only":
            warnings.append(
                "cURL'de authorization başlığı var; cookie_only modu bu başlığı "
                "upstream isteklerinde göndermeyecek."
            )
        if not scheme:
            warnings.append(
                "authorization başlığı şemasız (örn. 'Bearer' yok); token ham "
                "gönderilecek."
            )
    else:
        # Cookie-only cURL ile tazelenen yuvada önceki access token kalırsa
        # açık token her zaman öncelikli olduğu için eski kimlik gönderilmeye devam eder.
        env["UPSTREAM_ACCESS_TOKEN"] = ""
        # Çerezden token çıkarılabiliyorsa bunu **aç**: aksi hâlde çerezde taze
        # bir oturum olmasına rağmen Authorization başlığı hiç üretilmez ve hedef
        # 401 döner. Bu, canlıda "Çerezden token okuma: kapalı" satırıyla
        # görülen sessiz kimlik kaybının kaynağıydı.
        # Token'ın gerçekten **JWT** olması aranır: `session=opaque` gibi opak
        # çerezlerde bayrağı açıp Authorization başlığı uydurmak istek parmak
        # izini bozar (bu yüzden eski sürüm bilinçli olarak kapalı bırakıyordu).
        cookie_token = extract_access_token(cookie) if cookie else None
        if cookie_token and not looks_like_jwt(cookie_token):
            cookie_token = None
        if cookie_token:
            env["UPSTREAM_AUTH_FROM_COOKIE"] = "true"
            if (existing or {}).get("UPSTREAM_AUTH_HEADER_MODE") == "cookie_only":
                warnings.append(
                    "cURL'de 'authorization' başlığı yok; cookie_only modu "
                    "bu başlığı upstream isteklerinde göndermeyecek."
                )
            else:
                warnings.append(
                    "cURL'de 'authorization' başlığı yok ama oturum çerezi token "
                    "taşıyor: UPSTREAM_AUTH_FROM_COOKIE=true yapıldı, token çerezden "
                    "ayıklanıp Authorization başlığı olarak gönderilecek."
                )
        else:
            env["UPSTREAM_AUTH_FROM_COOKIE"] = "false"
            # Cookie-only kimlik doğrulama meşru bir kurulum; oturum çerezi zaten
            # varsa gürültü yapma. İkisi de yoksa istek anonim gider.
            cookie_names = [
                p.split("=", 1)[0].strip().lower() for p in cookie.split(";") if "=" in p
            ]
            if not any(_SESSION_HINT in name for name in cookie_names):
                warnings.append(
                    "Ne 'authorization' başlığı ne de oturum çerezi var; istek anonim "
                    "gider ve hedef büyük olasılıkla 401 döner. Giriş yapmış bir "
                    "istekten cURL alın."
                )
            else:
                if (existing or {}).get("UPSTREAM_AUTH_HEADER_MODE") == "cookie_only":
                    warnings.append(
                        "cURL'de 'authorization' başlığı yok; cookie_only modu "
                        "bu başlığı upstream isteklerinde göndermeyecek."
                    )
                else:
                    warnings.append(
                        "cURL'de 'authorization' başlığı yok ve çerezden erişim token'ı "
                        "okunamadı; istek yalnızca Cookie ile gönderilecek. Hedef 401 "
                        "dönerse taze çerez gönderin (/cookie) veya "
                        "UPSTREAM_ACCESS_TOKEN ayarlayın."
                    )

    # --- captcha
    configured_field = (existing or {}).get("UPSTREAM_RECAPTCHA_FIELD", "").strip()
    field, captcha_token = extract_captcha(body)
    if field and captcha_token:
        env["UPSTREAM_RECAPTCHA_FIELD"] = field
        configured_provider = (existing or {}).get("RECAPTCHA_PROVIDER", "").strip().lower()
        if configured_provider in {"browser", "external"}:
            # Gerçekten dinamik sağlayıcılar (browser/external) seçiliyse
            # kısa ömürlü cURL token'ını yok say; gerçek token her istekte
            # sağlayıcıdan alınır.
            env["RECAPTCHA_PROVIDER"] = configured_provider
            env["RECAPTCHA_STATIC_TOKEN"] = ""
        else:
            # Varsayılan/static/noop: kullanıcı cURL'ü içinde captcha token'ı
            # kopyaladıysa hedef alanı doğruluyor demektir. noop modda bile
            # token'ı statik olarak saklayıp gönder; aksi hâlde boş token ile
            # her istek `recaptcha_rejected` ile düşer.
            env["RECAPTCHA_PROVIDER"] = "static"
            env["RECAPTCHA_STATIC_TOKEN"] = captcha_token
    else:
        # Gövdede captcha alanı yok: bu, operatörün elle girdiği
        # RECAPTCHA_STATIC_TOKEN'ı silmek için gerekçe **değildir**. Eski kod
        # burada alanı boşaltıyordu; statik kipe dönülünce token yok oluyor ve
        # her istek `recaptcha_rejected` ile düşüyordu. Yalnız alan adı
        # biliniyorsa ve hâlâ ayarlıysa koru.
        if configured_field:
            env["UPSTREAM_RECAPTCHA_FIELD"] = configured_field
        warnings.append(
            "Gövdede captcha alanı bulunamadı. Mevcut RECAPTCHA_STATIC_TOKEN / "
            "RECAPTCHA_PROVIDER ayarları korunuyor. Statik token ~2 dk ömürlüdür: "
            "eskimişse hedef `recaptcha_rejected` döner. Kalıcı çözüm "
            "RECAPTCHA_PROVIDER=browser (veya external); hedef captcha "
            "istemiyorsa noop."
        )

    # --- gövde `mode` (yeni uçlar "direct" bekler)
    mode = body.get("mode")
    if isinstance(mode, str) and mode.strip():
        env["UPSTREAM_MODE"] = mode.strip()

    # --- gövde `cx` (istemci bağlamı)
    # cURL gövdesinde `cx` varsa alanı da otomatik olarak aç: kullanıcı
    # tarayıcısından kopyaladığı istekte bu alan bulunduysa hedef onu
    # doğruluyor demektir; sessizce çıkarmak istemci bağlamını eksiltir.
    cx = body.get("cx")
    if isinstance(cx, str) and cx.strip():
        env["UPSTREAM_CX"] = cx.strip()
        env["UPSTREAM_CX_SEND"] = "true"
    elif body:
        # Taze cURL'ün gövdesi ayrıştırılabilir JSON ama `cx` taşımıyorsa hedef
        # bu alanı artık göndermiyor demektir. `cx` oturuma bağlı bir bağlam
        # işareti olduğu için **eski oturumun değeri hayatta kalmamalı**: eski
        # davranışta yeni cURL yalnızca çerez/token tazeliyor, yarım-taze
        # istemci bağlamı gönderilmeye devam ediyordu. (Gövde hiç
        # ayrıştırılamadıysa "yok" demek kanıtsız olur; o durumda alan
        # olduğu gibi bırakılır ve yukarıdaki uyarı yönlendirir.)
        env["UPSTREAM_CX"] = ""
        env["UPSTREAM_CX_SEND"] = "false"
        if (existing or {}).get("UPSTREAM_CX_SEND", "").strip().lower() == "true":
            warnings.append(
                "Bu cURL'ün gövdesinde 'cx' yok; önceki oturumdan kalan "
                "UPSTREAM_CX/UPSTREAM_CX_SEND temizlendi (oturuma bağlı bir alan "
                "olduğu için eski değer yeni kimlikle gönderilmez)."
            )

    # Site key yalnız açıkça etiketlenmiş bir alandan alınabilir. Cookie,
    # response token veya serbest metinde biçim eşleşmesi yeterli değildir.
    site_key_candidates: list[Any] = [
        body.get("siteKey"), body.get("site_key"), body.get("recaptchaSiteKey"),
        headers.get("x-recaptcha-site-key"),
    ]
    if split:
        query = parse_qs(split.query)
        for name in ("sitekey", "site_key", "siteKey"):
            site_key_candidates.extend(query.get(name, []))
        if "recaptcha/" in split.path.lower():
            site_key_candidates.extend(query.get("render", []))
    for candidate in site_key_candidates:
        if isinstance(candidate, str) and _RECAPTCHA_SITE_KEY_RE.fullmatch(candidate.strip()):
            env["RECAPTCHA_SITE_KEY"] = candidate.strip()
            break

    # --- model kimliği
    model_id = body.get("modelAId")
    if not isinstance(model_id, str) or not model_id.strip():
        model_id = None
        warnings.append(
            "Gövdede 'modelAId' yok; config/models.yaml içindeki upstream_id "
            "değerini elle güncellemeniz gerekir."
        )

    if not body and parsed["body"]:
        warnings.append("Gövde JSON olarak ayrıştırılamadı; cURL eksik kopyalanmış olabilir.")

    return env, warnings, model_id


def looks_like_stream_path(path: str) -> bool:
    """cURL'ün sohbet akış ucundan alınmış olma ihtimalini sezgisel kontrol eder."""
    lowered = (path or "").lower()
    if any(hint in lowered for hint in _STREAM_PATH_HINTS):
        return True
    parts = [part for part in lowered.split("/") if part]
    return "stream" in parts


#: Eski iç ad; dış çağrılar için `looks_like_stream_path` kullanın.
_looks_like_stream_path = looks_like_stream_path


def stream_path_template(path: str, body: dict[str, Any]) -> str:
    """URL yolundaki sohbet kimliğini ``{chat_id}`` yer tutucusuna çevirir.

    Upstream yolu sürümlenebildiği ve kimliği URL'de taşımayabildiği için
    (ör. ``/nextjs-api/stream/create-evaluation``) sabit varsayım HTTP 404
    üretir. Yol her zaman cURL'den alınır; kimlik bir yol segmentiyse
    parametreleştirilir, değilse yol olduğu gibi yazılır.
    """
    raw_path, _, query = (path or "/").partition("?")
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path

    def _with_query(template: str) -> str:
        return f"{template}?{query}" if query else template

    chat_id = body.get("id")
    # Yalnızca tam bir yol segmentiyle eşleşmeli: kısa bir "id" değeri yolun
    # ortasındaki harflere denk gelip yolu bozabilir.
    if isinstance(chat_id, str) and chat_id:
        segments = raw_path.split("/")
        if chat_id in segments:
            templated = "/".join("{chat_id}" if seg == chat_id else seg for seg in segments)
            return _with_query(templated)
    match = _UUID_RE.search(raw_path)
    if match:
        return _with_query(raw_path[: match.start()] + "{chat_id}" + raw_path[match.end() :])
    return _with_query(raw_path)
