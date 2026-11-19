"""Secret ``UPSTREAM_CURL`` (ve ``UPSTREAM_CURL_2..4``) değerlerini ayarlara uygular.

``UPSTREAM_CURL`` 1. hesabın tam anlık görüntüsüdür: hedef, akış yolu, ``mode``,
tarayıcı başlıkları ve kimlik. ``UPSTREAM_CURL_2`` (``_3``, ``_4``) **aynı
servisin başka bir hesabıdır**: ondan yalnız hesaba özgü alanlar (çerez/token,
user-agent, dil, referer yolu, statik captcha token'ı) okunur ve ilgili hesap
yuvasına yazılır. Ortak alanlar 1. hesaptan gelir.
"""

from __future__ import annotations

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.core.logging import get_logger
from app.upstream.auth import decode_jwt_claims, resolve_bearer_token
from app.upstream.curl_import import build_settings, parse_curl

logger = get_logger(__name__)

# cURL importer'ın ürettiği alanların Settings karşılıkları. Bu liste bilinçli
# olarak allowlist'tir: cURL'den API_KEYS, RECAPTCHA_BROWSER_URL veya başka bir
# yerel çalışma ayarı ezilemez.
_CURL_FIELDS: dict[str, str] = {
    "TARGET_DOMAIN": "target_domain",
    "UPSTREAM_STREAM_PATH": "upstream_stream_path",
    "UPSTREAM_MODE": "upstream_mode",
    "UPSTREAM_RECAPTCHA_FIELD": "upstream_recaptcha_field",
    "UPSTREAM_ACCEPT_LANGUAGE": "upstream_accept_language",
    "UPSTREAM_USER_AGENT": "upstream_user_agent",
    "UPSTREAM_EXTRA_HEADERS": "upstream_extra_headers",
    "UPSTREAM_REFERER_PATH": "upstream_referer_path",
    "UPSTREAM_COOKIE": "upstream_cookie",
    "UPSTREAM_AUTH_FROM_COOKIE": "upstream_auth_from_cookie",
    "UPSTREAM_ACCESS_TOKEN": "upstream_access_token",
    "UPSTREAM_AUTH_SCHEME": "upstream_auth_scheme",
    "RECAPTCHA_PROVIDER": "recaptcha_provider",
    "RECAPTCHA_STATIC_TOKEN": "recaptcha_static_token",
}


#: İkinci+ hesabın cURL'ünden yuvaya yazılan alanlar: cURL anahtarı → yuva alanı
#: öneki (`upstream_cookie` → `upstream_cookie_2`). `Account.from_env` ile aynı küme.
_SLOT_FIELDS: dict[str, str] = {
    "TARGET_DOMAIN": "target_domain",
    "UPSTREAM_COOKIE": "upstream_cookie",
    "UPSTREAM_ACCESS_TOKEN": "upstream_access_token",
    "UPSTREAM_AUTH_FROM_COOKIE": "upstream_auth_from_cookie",
    "UPSTREAM_AUTH_SCHEME": "upstream_auth_scheme",
    "UPSTREAM_USER_AGENT": "upstream_user_agent",
    "UPSTREAM_ACCEPT_LANGUAGE": "upstream_accept_language",
    "UPSTREAM_REFERER_PATH": "upstream_referer_path",
    "RECAPTCHA_STATIC_TOKEN": "recaptcha_static_token",
}

#: Hesaplar arasında aynı olması beklenen (1. hesaptan okunan) alanlar.
_SHARED_SHAPE: dict[str, str] = {
    "UPSTREAM_STREAM_PATH": "upstream_stream_path",
    "UPSTREAM_MODE": "upstream_mode",
    "UPSTREAM_RECAPTCHA_FIELD": "upstream_recaptcha_field",
}


def curl_env_name(slot: int) -> str:
    """Yuvanın ortam değişkeni adı (hata metinleri ve loglar için)."""
    return "UPSTREAM_CURL" if slot <= 1 else f"UPSTREAM_CURL_{slot}"


def _parse_secret(
    raw: str, settings: Settings, slot: int
) -> tuple[dict[str, str], list[str], str | None]:
    """Ham cURL'ü ayrıştırır ve kullanılabilir olduğunu doğrular.

    Hata metinleri hangi değişkenin bozuk olduğunu söyler; gizli değer taşımaz.
    """
    name = curl_env_name(slot)
    parsed = parse_curl(raw)
    # browser/external/noop seçimi Render'da ayrı bir ayardır. cURL'deki kısa
    # ömürlü token bu seçimi static'e geri döndürmemeli.
    env, warnings, model_id = build_settings(
        parsed,
        existing={"RECAPTCHA_PROVIDER": settings.recaptcha_provider},
    )

    critical = [item for item in warnings if item.startswith("KRİTİK")]
    if critical:
        raise ValueError(
            f"{name} bir telemetri/analitik isteği görünüyor. "
            "Network'te create-evaluation veya post-to-evaluation isteğini kopyalayın."
        )
    if not env.get("TARGET_DOMAIN") or not env.get("UPSTREAM_STREAM_PATH"):
        raise ValueError(
            f"{name} içinden hedef URL ve akış yolu çıkarılamadı; "
            "tam create-evaluation cURL'ünü yapıştırın."
        )
    if not env.get("UPSTREAM_COOKIE") and not env.get("UPSTREAM_ACCESS_TOKEN"):
        raise ValueError(
            f"{name} içinde cookie veya authorization başlığı yok; "
            "siteye giriş yapmışken tamamlanmış sohbet isteğini kopyalayın."
        )
    return env, warnings, model_id.strip() if isinstance(model_id, str) else None


def _identity(cookie: str, access_token: str) -> str:
    """Oturumun hangi hesaba ait olduğunu gösteren kararlı değer (yoksa boş).

    JWT'nin `sub` alanı hesabın kimliğidir ve token yenilense de değişmez. JWT
    çözülemiyorsa kimlik bilinmez sayılır (boş): karşılaştırma yapılmaz.
    """
    token = resolve_bearer_token(explicit_token=access_token, cookie_header=cookie)
    if not token:
        return ""
    claims = decode_jwt_claims(token)
    subject = claims.get("sub") if isinstance(claims, dict) else None
    return str(subject).strip() if subject else ""


def _reject_duplicate_accounts(settings: Settings, from_curl: set[int]) -> None:
    """cURL ile gelen bir yuva, başka bir yuvayla aynı hesabı taşıyorsa reddet.

    Aynı hesabı iki yuvaya koymak bölüşümü anlamsız kılar: tek hesap iki hattan
    aynı anda kullanılır (hesap başına "tek tek ve aralıklı" kuralı bozulur) ve
    aynı oturumun iki kopyası birbirinin yenilenen çerezini geçersiz kılabilir.
    """
    seen: dict[str, int] = {}
    for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
        suffix = "" if slot == 1 else f"_{slot}"
        cookie = str(getattr(settings, f"upstream_cookie{suffix}", "") or "").strip()
        token = str(getattr(settings, f"upstream_access_token{suffix}", "") or "").strip()
        if not cookie and not token:
            continue
        keys = [key for key in (_identity(cookie, token), cookie, token) if key]
        for key in keys:
            other = seen.get(key)
            if other is not None and other != slot and (slot in from_curl or other in from_curl):
                raise ValueError(
                    f"{curl_env_name(slot)} ile {curl_env_name(other)} aynı hesaba ait "
                    "(aynı oturum kimliği). İkinci hesap için siteye BAŞKA bir hesapla "
                    "giriş yapıp cURL'ü o oturumdan alın."
                )
        for key in keys:
            seen.setdefault(key, slot)


def apply_upstream_curl(settings: Settings) -> tuple[Settings, str | None]:
    """Ham cURL'ler varsa onları mevcut upstream alanlarının üzerine uygular.

    Dönüşteki ikinci değer 1. cURL'ün gövdesindeki ``modelAId``'dir; uygulama
    yaşam döngüsü bunu varsayılan modelin upstream kimliği olarak kullanır.
    Hiç cURL yoksa nesne olduğu gibi döner. Secret değerler hata metinlerine ve
    loglara eklenmez.
    """
    raw = settings.upstream_curl.strip()
    extra = {
        slot: value
        for slot in range(2, MAX_ACCOUNT_SLOTS + 1)
        if (value := str(getattr(settings, f"upstream_curl_{slot}", "") or "").strip())
    }
    if not raw and not extra:
        return settings, None

    updates: dict[str, object] = {}
    warnings: list[tuple[int, str]] = []
    model_id: str | None = None

    if raw:
        env, found, model_id = _parse_secret(raw, settings, 1)
        warnings.extend((1, item) for item in found)
        updates.update(
            {field: env[key] for key, field in _CURL_FIELDS.items() if key in env}
        )
        # İsteğe bağlı alanlar cURL'de yoksa önceki secret/env'den özel değer
        # taşıma; sınıfın güvenli varsayılanına dön. Özellikle eski statik CAPTCHA
        # token'ının yeni oturumda kullanılmasını önler.
        for key in (
            "UPSTREAM_MODE",
            "UPSTREAM_RECAPTCHA_FIELD",
            "UPSTREAM_ACCEPT_LANGUAGE",
            "UPSTREAM_USER_AGENT",
            "UPSTREAM_REFERER_PATH",
            "RECAPTCHA_STATIC_TOKEN",
        ):
            if key not in env:
                field = _CURL_FIELDS[key]
                default = Settings.model_fields[field].default
                updates[field] = default if isinstance(default, str) else ""
        # Bu secret 1. hesabın tam anlık görüntüsüdür. Render'da unutulmuş eski
        # yuvalar yeni cURL hesabıyla karışıp kota/sohbet davranışını
        # değiştirmesin: kendi cURL'ü (`UPSTREAM_CURL_N`) verilmemiş ikinci+
        # hesap kimliklerini etkisizleştir.
        for slot in range(2, MAX_ACCOUNT_SLOTS + 1):
            if slot in extra:
                continue
            for prefix in (
                "upstream_cookie",
                "upstream_access_token",
                "target_domain",
                "recaptcha_static_token",
            ):
                updates[f"{prefix}_{slot}"] = ""
            updates[f"upstream_account_{slot}_name"] = ""

    #: Ortak alanların (akış yolu, mode, captcha alanı) geçerli değerleri.
    shared = {
        key: str(updates.get(field, getattr(settings, field)) or "")
        for key, field in _SHARED_SHAPE.items()
    }
    primary_domain = str(updates.get("target_domain", settings.target_domain) or "")
    model_differs: list[int] = []

    for slot, value in extra.items():
        env, found, slot_model_id = _parse_secret(value, settings, slot)
        warnings.extend((slot, item) for item in found)
        for key, prefix in _SLOT_FIELDS.items():
            field = f"{prefix}_{slot}"
            default = Settings.model_fields[field].default
            # cURL'de olmayan alan önceki env değerini taşımaz: yuva, bu cURL'ün
            # tam anlık görüntüsüdür.
            updates[field] = env.get(key, default if isinstance(default, (str, bool)) else "")
        mismatched = [
            field
            for key, field in _SHARED_SHAPE.items()
            if (env.get(key) or "") != shared[key]
        ]
        if env.get("TARGET_DOMAIN") != primary_domain:
            mismatched.insert(0, "target_domain")
        if mismatched:
            logger.warning(
                "upstream_curl_slot_mismatch",
                slot=slot,
                source=curl_env_name(slot),
                fields=mismatched,
                hint=(
                    "Bu cURL 1. hesabınkinden farklı bir hedef/akış biçimi taşıyor. "
                    "Ortak alanlar 1. hesaptan okunur; iki cURL'ü de aynı sayfadan, "
                    "aynı türde bir sohbet isteğinden alın."
                ),
            )
        if slot_model_id and model_id and slot_model_id != model_id:
            model_differs.append(slot)

    # model_copy(update=...) doğrulama yapmaz; "false" metnini bool alana
    # string olarak bırakıp truthy davranmasını önlemek için Settings'i yeniden
    # doğruluyoruz. Mevcut tüm yerel ayarlar korunur, yalnız allowlist ezilir.
    merged = settings.model_dump()
    merged.update(updates)
    updated = Settings(**merged)
    _reject_duplicate_accounts(updated, from_curl=set(extra) | ({1} if raw else set()))

    for slot, item in warnings:
        logger.warning("upstream_curl_warning", source=curl_env_name(slot), detail=item)
    if model_differs:
        logger.info(
            "upstream_curl_model_differs",
            slots=model_differs,
            hint="Varsayılan modelin kimliği 1. cURL'den alınır; diğerleri yok sayıldı.",
        )
    logger.info(
        "upstream_curl_loaded",
        fields=sorted(updates),
        accounts=sorted({1} | set(extra)) if raw else sorted(extra),
        model_id_detected=bool(model_id),
    )
    return updated, model_id
