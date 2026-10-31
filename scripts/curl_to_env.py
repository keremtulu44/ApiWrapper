#!/usr/bin/env python3
"""Tarayıcıdan kopyalanan cURL komutunu `.env` ve `models.yaml` ayarlarına çevirir.

Kullanım:
    python scripts/curl_to_env.py curl.txt
    python scripts/curl_to_env.py curl.txt --write
    type curl.txt | python scripts/curl_to_env.py -

Elle kopyala-yapıştır sırasında en sık yapılan hatalar (çerezin ortadan kesilmesi,
değerin alt satıra taşması, tırnakların yarım kalması) bu araçla tamamen ortadan
kalkar: cURL ne veriyorsa birebir o yazılır.

Çıkarılanlar:
  * ``TARGET_DOMAIN``        — URL'nin alan adı (şemasız)
  * ``UPSTREAM_STREAM_PATH`` — gerçek akış yolu (``{chat_id}`` varsa parametreleştirilir)
  * ``UPSTREAM_MODE``        — gövdedeki ``mode`` (örn. ``direct``)
  * ``UPSTREAM_COOKIE``      — ``cookie`` başlığının tamamı
  * ``UPSTREAM_ACCEPT_LANGUAGE``
  * ``UPSTREAM_USER_AGENT``
  * ``UPSTREAM_EXTRA_HEADERS`` — dnt/deployment başlıkları
  * ``UPSTREAM_RECAPTCHA_FIELD`` — gövdedeki captcha alanının gerçek adı
  * ``RECAPTCHA_STATIC_TOKEN``   — gövdedeki captcha değeri
  * ``models.yaml`` için ``upstream_id`` (gövdedeki ``modelAId``)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import MAX_ACCOUNT_SLOTS  # noqa: E402
from scripts.compare_curl import load_body, parse_curl  # noqa: E402

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
PASSTHROUGH_HEADERS: tuple[str, ...] = (
    "dnt",
    "x-arena-web-deployment",
    "x-deployment-id",
)

#: `.env` içine yazılacak anahtarların sırası.
ENV_ORDER: tuple[str, ...] = (
    "TARGET_DOMAIN",
    "UPSTREAM_STREAM_PATH",
    "UPSTREAM_MODE",
    "UPSTREAM_RECAPTCHA_FIELD",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_EXTRA_HEADERS",
    "UPSTREAM_REFERER_PATH",
    "UPSTREAM_COOKIE",
    "UPSTREAM_AUTH_FROM_COOKIE",
    "UPSTREAM_ACCESS_TOKEN",
    "UPSTREAM_AUTH_SCHEME",
    "RECAPTCHA_PROVIDER",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Hesap başına tutulan anahtarlar. 1. hesap soneksiz yazılır (mevcut tek hesaplı
#: kurulum aynen çalışmaya devam eder); 2. hesap `_2`, 3. hesap `_3` soneksi alır.
#: Böylece cURL'ler sırayla atıldığında her biri kendi boşluğuna yazılır ve
#: hiçbir hesap diğerini ezmez.
ACCOUNT_ENV_KEYS: tuple[str, ...] = (
    "TARGET_DOMAIN",
    "UPSTREAM_REFERER_PATH",
    "UPSTREAM_COOKIE",
    "UPSTREAM_AUTH_FROM_COOKIE",
    "UPSTREAM_ACCESS_TOKEN",
    "UPSTREAM_AUTH_SCHEME",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Hesap yuvasının adını taşıyan anahtar (`UPSTREAM_ACCOUNT_NAME`, `..._2_NAME`, …).
ACCOUNT_NAME_PREFIX = "UPSTREAM_ACCOUNT"

#: `--reset-accounts` ile temizlenecek anahtarlar.
ACCOUNT_SCAN_KEYS: tuple[str, ...] = ACCOUNT_ENV_KEYS + ("UPSTREAM_RECAPTCHA_FIELD",)

#: `merge_env` yeni anahtarları bu yorum satırının altına ekler.
ENV_MARKER_COMMENT = "# scripts/curl_to_env.py tarafından eklendi"

#: Uygulamanın gerçekten okuyabildiği yuva sayısıyla aynı sınır.
MAX_SCAN_SLOTS = MAX_ACCOUNT_SLOTS

#: Oturum çerezi olma ihtimali olan adlar (uyarı üretmek için).
_SESSION_HINT = "auth"

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


def _mask(value: str, keep: int = 6) -> str:
    """Uzun gizli değerleri günlüğe basmadan önce kısaltır."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]} ({len(value)} karakter)"


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


def account_name_key(slot: int) -> str:
    """Hesap yuvasının ad anahtarı: 1. hesap soneksiz, 2. hesap `_2`."""
    suffix = "" if slot <= 1 else f"_{slot}"
    return f"{ACCOUNT_NAME_PREFIX}{suffix}_NAME"


def account_key(name: str, slot: int) -> str:
    """Hesaba özgü `.env` anahtarı."""
    return name if slot <= 1 else f"{name}_{slot}"


def split_account_env(
    env: dict[str, str], slot: int, label: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Çıkarılan ayarları (paylaşılan, hesaba özgü) olarak ikiye ayırır."""
    shared: dict[str, str] = {}
    account: dict[str, str] = {account_name_key(slot): label}
    for key, value in env.items():
        # CAPTCHA sağlayıcısı globaldir. İkinci/üçüncü hesabı eklemek, önceden
        # seçilmiş browser/external/noop kipini sessizce static'e çevirmemeli.
        if key == "RECAPTCHA_PROVIDER" and slot > 1:
            continue
        if key in ACCOUNT_ENV_KEYS:
            account[account_key(key, slot)] = value
        else:
            shared[key] = value
    return shared, account


def read_env_keys(path: Path) -> dict[str, str]:
    """`.env` içindeki `ANAHTAR=değer` satırlarını okur (yorumlar atlanır)."""
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        found.setdefault(key.strip(), value.strip())
    return found


def next_free_slot(path: Path) -> int:
    """Boş olan ilk hesap yuvasını bulur (1'den başlar).

    Adı yazılmış ama kimliği olmayan yuvalar **boş sayılır**: cookie başlığı
    olmayan bir cURL'den arta kalmışlardır ve yeniden kaydetme o yuvayı
    onarmalı, yeni bir yuva açmamalı.
    """
    existing = read_env_keys(path)
    for slot in range(1, MAX_SCAN_SLOTS + 1):
        if not slot_has_credentials(existing, slot):
            return slot
    return MAX_SCAN_SLOTS + 1


def _is_account_key(key: str) -> bool:
    """Anahtar bir hesap yuvasına mı ait? (`X`, `X_2`, `X_3`, `..._NAME`)"""
    if key in ACCOUNT_SCAN_KEYS:
        return True
    if key.startswith(ACCOUNT_NAME_PREFIX) and key.endswith("_NAME"):
        return True
    base, sep, suffix = key.rpartition("_")
    return bool(sep) and base in ACCOUNT_SCAN_KEYS and suffix.isdigit()


def strip_account_keys(path: Path) -> int:
    """`.env`'den hesap anahtarlarını (tüm yuvalar) söker; kalan satır sayısını döndürür."""
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == ENV_MARKER_COMMENT:
            continue  # tekrar birikmesin
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if _is_account_key(key):
                continue
        kept.append(line)
    # Sonda biriken boş satırları at.
    while kept and not kept[-1].strip():
        kept.pop()
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return len(kept)


def slot_has_credentials(values: dict[str, str], slot: int) -> bool:
    """Yuva, isteği kimlikle gönderebilecek bilgiye sahip mi?

    `app/services/account.py::Account.is_configured` ile aynı kural: cookie
    veya access_token boş değilse dolu. **Yalnızca adı yazılmış yuvalar hesap
    değildir** — cookie başlığı olmayan bir cURL kaydedildiğinde oluşurlar ve
    uygulama onları zaten atlar.
    """
    return bool(
        values.get(account_key("UPSTREAM_COOKIE", slot), "").strip()
        or values.get(account_key("UPSTREAM_ACCESS_TOKEN", slot), "").strip()
    )


def count_accounts(values: dict[str, str]) -> int:
    """`.env` içeriğinde kaç *kullanılabilir* hesap var?

    Tüm yuvalar taranır; aradaki boş yuvalar atlanır. Uygulama
    (`load_accounts`) da böyle saydığı için iki sayı birbirini tutar.
    """
    return sum(
        1
        for slot in range(1, MAX_SCAN_SLOTS + 1)
        if slot_has_credentials(values, slot)
    )


def build_settings(
    parsed: dict[str, Any], existing: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str], str | None]:
    """cURL çıktısından `.env` çiftlerini, uyarıları ve model kimliğini üretir.

    `existing` mevcut `.env` içeriğidir; yalnızca alan adı tutarlılık uyarısı
    için kullanılır.
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
        telemetry_target = host if telemetry_host else split.path
        warnings.append(
            f"KRİTİK: '{telemetry_target}' bir telemetri/analitik/olay toplama "
            "isteği; sohbet ucu değil. Network sekmesinde sohbet akış isteğine "
            "('create-evaluation' veya 'post-to-evaluation') sağ tıklayıp cURL'ü "
            "yeniden kopyalayın."
        )
    elif split and not _looks_like_stream_path(split.path):
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
        template = _stream_path_template(full_path, body)
        env["UPSTREAM_STREAM_PATH"] = template
        if template != DEFAULT_STREAM_PATH:
            warnings.append(
                f"Akış yolu varsayılandan farklı, .env'e yazıldı: {template}"
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
    extra_headers: list[str] = []
    for name in PASSTHROUGH_HEADERS:
        value = headers.get(name, "").strip()
        if value:
            value = value.replace("\r", " ").replace("\n", " ")
            extra_headers.append(f"{name}={value}")
    env["UPSTREAM_EXTRA_HEADERS"] = ";".join(extra_headers)

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
        if not scheme:
            warnings.append(
                "authorization başlığı şemasız (örn. 'Bearer' yok); token ham "
                "gönderilecek."
            )
    else:
        env["UPSTREAM_AUTH_FROM_COOKIE"] = "false"
        # Cookie-only cURL ile tazelenen yuvada önceki access token kalırsa
        # açık token her zaman öncelikli olduğu için eski kimlik gönderilmeye devam eder.
        env["UPSTREAM_ACCESS_TOKEN"] = ""
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
            warnings.append(
                "cURL'de 'authorization' başlığı yok; istek yalnızca Cookie ile "
                "gönderilecek. Hedef 401 dönerse UPSTREAM_AUTH_FROM_COOKIE=true yapın "
                "(token çerezden ayıklanır). Olmayan bir başlık eklemek istek parmak "
                "izini bozacağı için bu otomatik yapılmıyor."
            )

    # --- captcha
    field, token = extract_captcha(body)
    if field and token:
        env["UPSTREAM_RECAPTCHA_FIELD"] = field
        env["RECAPTCHA_PROVIDER"] = "static"
        env["RECAPTCHA_STATIC_TOKEN"] = token
    else:
        warnings.append(
            "Gövdede captcha alanı bulunamadı. Hedef captcha istemiyorsa "
            "RECAPTCHA_PROVIDER=noop kullanın."
        )

    # --- gövde `mode` (yeni uçlar "direct" bekler)
    mode = body.get("mode")
    if isinstance(mode, str) and mode.strip():
        env["UPSTREAM_MODE"] = mode.strip()

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


def _looks_like_stream_path(path: str) -> bool:
    """cURL'ün sohbet akış ucundan alınmış olma ihtimalini sezgisel kontrol eder."""
    lowered = (path or "").lower()
    if any(hint in lowered for hint in _STREAM_PATH_HINTS):
        return True
    parts = [part for part in lowered.split("/") if part]
    return "stream" in parts


def _stream_path_template(path: str, body: dict[str, Any]) -> str:
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


def merge_env(path: Path, updates: dict[str, str]) -> str:
    """Var olan `.env` içeriğini koruyarak anahtarları günceller.

    Aynı anahtardan birden çok satır varsa ilki güncellenir, kalan kopyalar
    silinir; böylece mükerrer tanımların sessizce birbirini ezmesi önlenir.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            result.append(f"{key}={remaining.pop(key)}")
            seen.add(key)
        elif key in seen:
            continue  # mükerrer satırı at
        else:
            result.append(line)

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append(ENV_MARKER_COMMENT)
        for key in ENV_ORDER:
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
        for key, value in remaining.items():
            result.append(f"{key}={value}")

    return "\n".join(result) + "\n"


def update_models_yaml(path: Path, upstream_id: str) -> bool:
    """`models.yaml` içindeki ilk `upstream_id` değerini günceller."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"(^\s*upstream_id:\s*).*$",
        lambda m: f"{m.group(1)}{upstream_id}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count and new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "cURL komutunu .env ayarlarına çevirir. Her çalıştırma bir hesap "
            "yuvasına yazar: 1. cURL soneksiz anahtarlara, 2. cURL '_2' "
            "soneksli anahtarlara. Böylece cURL'ler sırayla atıldığında hiçbiri "
            "diğerini ezmez."
        ),
    )
    parser.add_argument("curl_file", help="cURL komutunu içeren dosya ('-' = stdin)")
    parser.add_argument(
        "--write", action="store_true", help=".env ve models.yaml dosyalarını güncelle"
    )
    parser.add_argument("--env-file", default=".env", help="Hedef .env yolu")
    parser.add_argument(
        "--models-file", default="config/models.yaml", help="Hedef models.yaml yolu"
    )
    parser.add_argument(
        "--show-secrets", action="store_true", help="Değerleri maskelemeden yazdır"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kritik uyarılara rağmen yazmayı sürdür",
    )
    parser.add_argument(
        "--account",
        help=(
            "Hesap yuvası: '1'/'2' gibi bir yuva numarası ya da 'hesap-1' gibi "
            "özel bir ad. Verilmezse ilk boş yuva seçilir."
        ),
    )
    parser.add_argument(
        "--list-accounts", action="store_true", help="Kayıtlı hesapları göster ve çık"
    )
    parser.add_argument(
        "--reset-accounts",
        action="store_true",
        help=".env içindeki tüm hesap yuvalarını temizle",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)

    if args.reset_accounts:
        strip_account_keys(env_path)
        print(f"{env_path} içindeki hesap yuvaları temizlendi.")
        return 0

    if args.list_accounts:
        values = read_env_keys(env_path)
        used = [
            slot
            for slot in range(1, MAX_SCAN_SLOTS + 1)
            if slot_has_credentials(values, slot)
            or values.get(account_name_key(slot), "").strip()
        ]
        if not used:
            print(f"{env_path} içinde kayıtlı hesap yok.")
            return 0
        total = count_accounts(values)
        print(f"{env_path} içinde {total} kullanılabilir hesap var:")
        for slot in used:
            cookie = values.get(account_key("UPSTREAM_COOKIE", slot), "")
            token = values.get(account_key("RECAPTCHA_STATIC_TOKEN", slot), "")
            name = values.get(account_name_key(slot), "?") or "?"
            flag = "" if slot_has_credentials(values, slot) else "   ← KULLANILAMIYOR"
            print(
                f"  {slot}. {name} "
                f"(cookie {len(cookie)} kr, token {len(token)} kr){flag}"
            )
        broken = [s for s in used if not slot_has_credentials(values, s)]
        if broken:
            slots = ", ".join(str(s) for s in broken)
            print(
                f"\n  {slots}. yuva(lar)ın adı var ama çerezi/token'ı yok: cookie\n"
                "  başlığı olmayan bir cURL kaydedilmiş. Uygulama bu yuvaları zaten\n"
                "  atlar, yani hesap sayısını şişirmezler. Onarmak için o hesabın\n"
                "  cURL'ünü siteye giriş yapmışken alıp yeniden kaydedin; aynı yuva\n"
                "  kullanılır, yeni yuva açılmaz."
            )
        return 0

    if args.curl_file == "-":
        raw = sys.stdin.read()
    else:
        curl_path = Path(args.curl_file)
        if not curl_path.is_file():
            print(
                f"HATA: '{args.curl_file}' bulunamadı (aranan dizin: {Path.cwd()}).\n"
                "  İki yol var:\n"
                "  1) cURL'ü panoya kopyalayıp doğrudan aktarın (dosya gerekmez):\n"
                "       Get-Clipboard | python scripts/curl_to_env.py --write -\n"
                "  2) cURL'ü bir dosyaya kaydedin (Notepad → curl1.txt, UTF-8) ve\n"
                "     dosya adını tam yol verin.",
                file=sys.stderr,
            )
            return 2
        # utf-8-sig: Windows PowerShell'in yazdığı BOM'u da tolere eder.
        raw = curl_path.read_text(encoding="utf-8-sig", errors="replace")
    if not raw.strip():
        print("HATA: cURL girdisi boş.", file=sys.stderr)
        return 2

    parsed = parse_curl(raw)
    existing = read_env_keys(env_path)
    env, warnings, model_id = build_settings(parsed, existing)

    if not env:
        print("HATA: cURL'den hiçbir ayar çıkarılamadı.", file=sys.stderr)
        return 2

    if not env.get("UPSTREAM_COOKIE") and not env.get("UPSTREAM_ACCESS_TOKEN"):
        found_headers = ", ".join(sorted(parsed.get("headers", {}))) or "(hiç yok)"
        url = parsed.get("url", "")
        print(
            "HATA: cURL'de ne 'cookie' ne de 'authorization' başlığı bulundu.\n"
            "  Böyle bir cURL kaydedilirse yuva adıyla birlikte yazılır ama kimliği\n"
            "  olmaz; uygulama o yuvayı kullanamaz.\n"
            "\n"
            "  Aracın gördüğü girdi:\n"
            f"    uzunluk     : {len(raw)} karakter\n"
            f"    url         : {url or '(çıkarılamadı)'}\n"
            f"    başlıklar   : {found_headers}\n"
            f"    gövde       : {len(parsed.get('body', ''))} karakter\n"
            f"    ilk 300 kr  : {raw.strip()[:300]!r}\n"
            "\n"
            "  Olası sebepler:\n"
            "   * İstek 'Provisional headers are shown' durumundayken kopyalandı\n"
            "     (yanıt bitmeden kopyalamayın; adblock'u kapatın, Disable cache açın).\n"
            "   * PowerShell panoyu bozdu: 'Get-Clipboard -Raw' deneyin, ya da cURL'ü\n"
            "     Notepad ile bir dosyaya kaydedip dosya adını verin.\n"
            "   * Yanlış istek kopyalandı: akış isteği POST olmalı ve gövdesinde\n"
            "     'modelAId' bulunmalı.",
            file=sys.stderr,
        )
        return 2

    try:
        slot, label = _resolve_slot(args.account, env_path)
    except ValueError as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 2

    print(f"=== Çıkarılan ayarlar ({slot}. hesap yuvası: {label}) ===")
    secret_keys = {
        "UPSTREAM_COOKIE",
        "RECAPTCHA_STATIC_TOKEN",
        "UPSTREAM_ACCESS_TOKEN",
    }
    for key in ENV_ORDER:
        if key not in env:
            continue
        value = env[key]
        target = account_key(key, slot) if key in ACCOUNT_ENV_KEYS else key
        shown = value if args.show_secrets or key not in secret_keys else _mask(value)
        print(f"  {target}={shown}")
    if model_id:
        if slot == 1:
            print(f"  (models.yaml) upstream_id={model_id}")
        else:
            print(
                f"  (models.yaml dokunulmadı; model kimlikleri hesap başına değil. "
                f"Bu cURL'deki modelAId={model_id})"
            )

    if warnings:
        print("\n=== Uyarılar ===")
        for item in warnings:
            print(f"  ! {item}")

    if not args.write:
        print(f"\nYazmak için --write ekleyin (boş olan yuva: {slot}. hesap).")
        return 0

    critical = [w for w in warnings if w.startswith("KRİTİK")]
    if critical and not args.force:
        print(
            "\nYazma iptal edildi: yanlış isteğin cURL'ü kopyalanmış görünüyor.\n"
            "Doğru cURL'ü alıp tekrar deneyin ya da yine de yazmak için --force ekleyin.",
            file=sys.stderr,
        )
        return 3

    shared, account = split_account_env(env, slot, label)
    env_path.write_text(merge_env(env_path, {**shared, **account}), encoding="utf-8")

    total = count_accounts(read_env_keys(env_path))
    print(f"\n{env_path} güncellendi — {label} {slot}. yuvaya yazıldı.")
    print(f"Kayıtlı hesap sayısı: {total}")
    if total > 1:
        print(
            "Hesap havuzu kullanılabilir yuvaların tümünü okur; "
            "değişikliklerin yüklenmesi için çalışan servisi yeniden başlatın."
        )

    if model_id and slot == 1:
        models_path = Path(args.models_file)
        if update_models_yaml(models_path, model_id):
            print(f"{models_path} güncellendi (upstream_id={model_id}).")
        else:
            print(
                f"UYARI: {models_path} güncellenemedi; upstream_id={model_id} "
                "değerini elle yazın."
            )
    return 0


def _resolve_slot(requested: str | None, env_path: Path) -> tuple[int, str]:
    """`--account` değerini (yuva numarası, hesap adı) çiftine çevirir."""
    existing = read_env_keys(env_path)

    def label_for(slot: int) -> str:
        # Numarayla tazeleme veya yarım yuvayı onarma özel adı silmemeli.
        return existing.get(account_name_key(slot), "").strip() or f"hesap-{slot}"

    if not requested:
        slot = next_free_slot(env_path)
        if slot > MAX_ACCOUNT_SLOTS:
            raise ValueError(
                f"Hesap yuvaları dolu; en fazla {MAX_ACCOUNT_SLOTS} hesap destekleniyor. "
                "Tazelemek için --account 1 (veya 2/3/4) kullanın."
            )
        return slot, label_for(slot)

    text = requested.strip()
    if text.isdigit():
        slot = int(text)
        if not 1 <= slot <= MAX_ACCOUNT_SLOTS:
            raise ValueError(
                f"Geçersiz hesap yuvası {slot}; 1-{MAX_ACCOUNT_SLOTS} arasında olmalı."
            )
        return slot, label_for(slot)

    free: int | None = None
    for slot in range(1, MAX_SCAN_SLOTS + 1):
        if existing.get(account_name_key(slot), "") == text:
            return slot, text  # aynı ad -> aynı yuvayı tazele
        if free is None and not slot_has_credentials(existing, slot):
            free = slot
    if free is None:
        raise ValueError(
            f"Hesap yuvaları dolu; '{text}' için en fazla {MAX_ACCOUNT_SLOTS} hesap "
            "destekleniyor. Var olan hesabı tazelemek için --account NUMARA kullanın."
        )
    return free, text


if __name__ == "__main__":
    raise SystemExit(main())
