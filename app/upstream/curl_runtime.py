"""Secret ``UPSTREAM_CURL`` değerini uygulama ayarlarına güvenle uygular."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.core.logging import get_logger
from app.upstream.curl_import import build_settings, looks_like_stream_path, parse_curl

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


def _runtime_fields() -> tuple[str, ...]:
    """Canlı `Settings` nesnesine yazılabilecek alan adları (allowlist)."""
    fields: list[str] = list(_CURL_FIELDS.values())
    for slot in range(2, MAX_ACCOUNT_SLOTS + 1):
        fields.extend(
            [
                f"upstream_cookie_{slot}",
                f"upstream_access_token_{slot}",
                f"target_domain_{slot}",
                f"recaptcha_static_token_{slot}",
                f"upstream_account_{slot}_name",
            ]
        )
    return tuple(dict.fromkeys(fields))


def _curl_path(raw: str) -> str:
    """cURL'ün URL yolunu döndürür; ayrıştırılamazsa boş metin."""
    try:
        parsed = parse_curl(raw)
    except ValueError:
        return ""
    url = parsed.get("url") or ""
    return urlsplit(url).path if url else ""


def apply_curl_to_settings(
    settings: Settings, raw: str, *, allow_path_change: bool = False
) -> dict[str, object]:
    """Yeni bir cURL'ü **çalışan** ayarlara uygular (yeniden deploy gerekmez).

    Oturum düştüğünde tek yapılacak iş taze bir cURL'dir; her seferinde Render'da
    secret güncelleyip redeploy etmek yerine bu fonksiyon alanları yerinde
    değiştirir. Ham cURL hiçbir yere yazılmaz/loglanmaz; yalnızca hangi alanların
    değiştiği döner.

    ``allow_path_change=False`` (varsayılan) iken, cURL'ün yolu sohbet akış
    ucuna benzemiyorsa (telefonda yanlış istek — ör. belge isteği ``/`` —
    kopyalanmışsa) **canlı akış yolu korunur**: çerez/token uygulanır, servis
    yolu bozulmaz. Bilinçli değişiklik için ``allow_path_change=True``.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Boş cURL gönderildi.")
    candidate = settings.model_copy(update={"upstream_curl": text})
    updated, model_id = apply_upstream_curl(candidate)

    warnings: list[str] = []
    incoming_path = _curl_path(text)
    if (
        not allow_path_change
        and incoming_path
        and not looks_like_stream_path(incoming_path)
        and settings.upstream_stream_path
        and updated.upstream_stream_path != settings.upstream_stream_path
    ):
        warnings.append(
            "Bu cURL sohbet akış isteği gibi görünmüyor (yol: "
            f"{incoming_path}). Canlı akış yolu korundu "
            f"({settings.upstream_stream_path}); yalnızca çerez/token uygulandı."
        )
        updated = updated.model_copy(
            update={"upstream_stream_path": settings.upstream_stream_path}
        )

    changed: list[str] = []
    for field in _runtime_fields():
        new_value = getattr(updated, field)
        if getattr(settings, field, None) != new_value:
            setattr(settings, field, new_value)
            changed.append(field)
    # Ham cURL bellekte tutulmaz: yalnızca çıkarılan alanlar yaşar.
    settings.upstream_curl = ""
    logger.info("upstream_curl_hot_swapped", fields=changed, warnings=len(warnings))
    return {"fields": changed, "model_id": model_id, "warnings": warnings}


def apply_upstream_curl(settings: Settings) -> tuple[Settings, str | None]:
    """Ham cURL varsa onu mevcut upstream alanlarının üzerine uygular.

    Dönüşteki ikinci değer cURL gövdesindeki ``modelAId``'dir; uygulama yaşam
    döngüsü bunu varsayılan modelin upstream kimliği olarak kullanır. cURL yoksa
    nesne olduğu gibi döner. Secret değerler hata metinlerine ve loglara eklenmez.
    """
    raw = settings.upstream_curl.strip()
    if not raw:
        return settings, None

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
            "UPSTREAM_CURL bir telemetri/analitik isteği görünüyor. "
            "Network'te create-evaluation veya post-to-evaluation isteğini kopyalayın."
        )
    if not env.get("TARGET_DOMAIN") or not env.get("UPSTREAM_STREAM_PATH"):
        raise ValueError(
            "UPSTREAM_CURL içinden hedef URL ve akış yolu çıkarılamadı; "
            "tam create-evaluation cURL'ünü yapıştırın."
        )
    if not env.get("UPSTREAM_COOKIE") and not env.get("UPSTREAM_ACCESS_TOKEN"):
        raise ValueError(
            "UPSTREAM_CURL içinde cookie veya authorization başlığı yok; "
            "siteye giriş yapmışken tamamlanmış sohbet isteğini kopyalayın."
        )

    updates = {
        field: env[key]
        for key, field in _CURL_FIELDS.items()
        if key in env
    }
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
    # Bu secret tek-hesap Max kurulumunun tam anlık görüntüsüdür. Render'da
    # unutulmuş eski yuvalar yeni cURL hesabıyla karışıp kota/sohbet davranışını
    # değiştirmesin; ikinci+ hesap kimliklerini etkisizleştir.
    for slot in range(2, MAX_ACCOUNT_SLOTS + 1):
        for prefix in (
            "upstream_cookie",
            "upstream_access_token",
            "target_domain",
            "recaptcha_static_token",
        ):
            updates[f"{prefix}_{slot}"] = ""
        updates[f"upstream_account_{slot}_name"] = ""
    # model_copy(update=...) doğrulama yapmaz; "false" metnini bool alana
    # string olarak bırakıp truthy davranmasını önlemek için Settings'i yeniden
    # doğruluyoruz. Mevcut tüm yerel ayarlar korunur, yalnız allowlist ezilir.
    merged = settings.model_dump()
    merged.update(updates)
    updated = Settings(**merged)

    for item in warnings:
        logger.warning("upstream_curl_warning", detail=item)
    logger.info(
        "upstream_curl_loaded",
        fields=sorted(updates),
        model_id_detected=bool(model_id),
    )
    return updated, model_id.strip() if isinstance(model_id, str) else None
