"""Hangi katmanın bozuk olduğunu tek bakışta söyleyen karar katmanı.

Belirti hep aynı görünür ("istek çalışmıyor"), ama dört ayrı katman aynı sonucu
üretir: yapılandırma, kimlik, sürüm damgası ve anti-bot (Cloudflare/CAPTCHA).
Operatörün yanlış katmana bakması bu depoda tekrar tekrar yaşandı; bu modül
kontrolleri **tek yerde** toplar ve en olası katmanı tek cümleyle söyler.

Kurallar:

* Gizli değer döndürmez; yalnız "var / yok / süresi doldu" gibi durumlar.
* Her başarısız kontrolün yanında **tek cümlelik düzeltme** vardır.
* Katman sırası önemlidir: yapılandırma hatası varken kimlik tartışılmaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from app.core.config import Settings
from app.upstream.auth import (
    decode_jwt_claims,
    extract_access_token,
    extract_refresh_token,
    token_seconds_remaining,
)
from app.upstream.headers import DEPLOYMENT_HEADERS, build_authorization

LayerName = Literal["config", "identity", "deployment", "captcha", "ok"]
CheckStatus = Literal["ok", "warn", "fail", "unknown"]

#: Katman önceliği: karar verirken önce yapılandırma, sonra kimlik, damga, anti-bot.
_LAYER_ORDER: tuple[str, ...] = ("config", "identity", "deployment", "captcha")

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0"}


@dataclass(slots=True)
class Check:
    """Tek bir teşhis kontrolü."""

    id: str
    layer: str
    status: CheckStatus
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "layer": self.layer,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix or None,
        }


@dataclass(slots=True)
class LayerVerdict:
    """Kontrollerin özeti: en olası katman + tek cümle + ayrıntı listesi."""

    layer: LayerName
    summary: str
    checks: list[Check]

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "summary": self.summary,
            "checks": [check.as_dict() for check in self.checks],
        }


def _first(checks: list[Check], status: str) -> Check | None:
    """Katman önceliğine göre ilk eşleşen kontrolü döndürür."""
    for layer in _LAYER_ORDER:
        for check in checks:
            if check.status == status and check.layer == layer:
                return check
    return None


def _token_ttl(token: str) -> float | None:
    """JWT'nin `exp - iat` değeri (saniye); yoksa `None`."""
    claims = decode_jwt_claims(token)
    exp, iat = claims.get("exp"), claims.get("iat")
    if isinstance(exp, (int, float)) and isinstance(iat, (int, float)) and exp > iat:
        return float(exp) - float(iat)
    return None


def build_layer_verdict(
    settings: Settings,
    *,
    recaptcha_stats: dict[str, Any] | None = None,
    deployment_stamp: str | None = None,
    refresh_last_error: str = "",
) -> LayerVerdict:
    """Ayarlardan ve canlı sayaçlardan katman kararı üretir.

    Args:
        settings: Etkin ayarlar (hesap yuvası verilmişse o hesabın kopyası).
        recaptcha_stats: Sağlayıcının ``stats`` sözlüğü (token'ın kendisi değil).
        deployment_stamp: Kanaryanın okuduğu **güncel** damga (varsa).
        refresh_last_error: Son yenileme denemesinin hata cümlesi (varsa).
    """
    checks: list[Check] = []

    # ---------------------------------------------------------- yapılandırma
    domain = (settings.target_domain or "").strip().lower()
    if domain in _LOCAL_HOSTS:
        checks.append(
            Check(
                id="target_domain",
                layer="config",
                status="fail",
                detail="TARGET_DOMAIN gerçek bir host değil; upstream istekleri atılmaz.",
                fix="TARGET_DOMAIN=<gerçek-host> (şemasız) veya tam UPSTREAM_CURL verin.",
            )
        )
    else:
        checks.append(
            Check("target_domain", "config", "ok", f"Hedef host ayarlı: {domain}")
        )

    authorization = build_authorization(settings)
    if authorization is None:
        status: CheckStatus = "warn" if not settings.upstream_auth_from_cookie else "fail"
        checks.append(
            Check(
                id="authorization_header",
                layer="identity",
                status=status,
                detail=(
                    "İstek `Authorization` başlığı olmadan gidiyor (yalnız Cookie)."
                    if not settings.upstream_auth_from_cookie
                    else "Çerezden bearer token çıkarılamadı; istek kimliksiz gidiyor."
                ),
                fix=(
                    "UPSTREAM_AUTH_FROM_COOKIE=true yapın veya UPSTREAM_ACCESS_TOKEN verin."
                ),
            )
        )

    # ---------------------------------------------------------------- kimlik
    cookie_token = extract_access_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    )
    effective = settings.upstream_access_token.strip() or cookie_token
    remaining = token_seconds_remaining(effective) if effective else None

    if not effective:
        checks.append(
            Check(
                id="access_token",
                layer="identity",
                status="fail",
                detail="Kullanılacak erişim token'ı yok.",
                fix="Taze çerez (/cookie) veya UPSTREAM_CURL gönderin.",
            )
        )
    elif remaining is not None and remaining <= 0:
        checks.append(
            Check(
                id="access_token",
                layer="identity",
                status="fail",
                detail=(
                    "Erişim token'ının süresi dolmuş "
                    f"({abs(int(remaining))} saniye önce)."
                ),
                fix=(
                    "Yenileme çalışmıyorsa taze çerez gerekir; "
                    "refresh_token varsa /token ile elle deneyin."
                ),
            )
        )
    else:
        detail = "Erişim token'ı geçerli"
        if remaining is not None:
            detail += f" (≈{int(remaining)} sn kaldı)"
        checks.append(Check("access_token", "identity", "ok", detail))

    refresh_token = extract_refresh_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    ) or (settings.upstream_refresh_token or "").strip()
    if not refresh_token:
        checks.append(
            Check(
                id="refresh_token",
                layer="identity",
                status="warn",
                detail=(
                    "Çerezde refresh token yok: token bitince otomatik yenileme "
                    "yapılamaz, elle çerez gerekir."
                ),
                fix="Giriş yapmış tarayıcıdan TÜM çerezleri alın (.0/.1 dahil).",
            )
        )
    else:
        checks.append(
            Check("refresh_token", "identity", "ok", "Refresh token çerezde bulundu.")
        )
        if not (settings.upstream_refresh_api_key or "").strip():
            checks.append(
                Check(
                    id="refresh_api_key",
                    layer="identity",
                    status="warn",
                    detail=(
                        "UPSTREAM_REFRESH_API_KEY boş. Supabase tarzı uç `apikey` "
                        "ister; otomatik keşif anti-bot duvarına takılabilir."
                    ),
                    fix=(
                        "Public anon anahtarı bir kez elle yazın "
                        "(UPSTREAM_REFRESH_API_KEY). Sır değildir."
                    ),
                )
            )
        else:
            checks.append(
                Check("refresh_api_key", "identity", "ok", "Yenileme `apikey` ayarlı.")
            )

    ttl = _token_ttl(effective) if effective else None
    leeway = float(settings.upstream_refresh_leeway_seconds)
    if ttl and leeway * 2 >= ttl:
        checks.append(
            Check(
                id="refresh_leeway",
                layer="identity",
                status="warn",
                detail=(
                    f"Token ömrü {int(ttl)} sn, yenileme eşiği {int(leeway)} sn: "
                    "token ömrünün yarısında yenilenir, yani sık rotation."
                ),
                fix=(
                    "UPSTREAM_REFRESH_LEEWAY_SECONDS değerini ömrün ~%15'i yapın "
                    "(ör. 1200 sn ömür için 180)."
                ),
            )
        )

    if refresh_last_error:
        checks.append(
            Check(
                id="refresh_last_attempt",
                layer="identity",
                status="warn",
                detail=f"Son yenileme denemesi başarısız: {refresh_last_error}",
                fix="Hata metnine göre `apikey` veya refresh token sorununu giderin.",
            )
        )

    # --------------------------------------------------------- sürüm damgası
    configured_stamp = None
    for name, value in settings.parsed_extra_headers().items():
        if name.lower() in DEPLOYMENT_HEADERS and value.strip():
            configured_stamp = value.strip()
            break
    if configured_stamp is None:
        checks.append(
            Check(
                "deployment_stamp",
                "deployment",
                "ok",
                "Sürüm damgası gönderilmiyor (hedef istemiyorsa doğru).",
            )
        )
    elif not settings.deployment_id_refresh_enabled:
        checks.append(
            Check(
                id="deployment_stamp",
                layer="deployment",
                status="warn",
                detail="Sürüm damgası gönderiliyor ama kanarya kapalı: damga donar.",
                fix="UPSTREAM_DEPLOYMENT_ID_REFRESH_ENABLED=true yapın.",
            )
        )
    elif deployment_stamp and deployment_stamp != configured_stamp:
        checks.append(
            Check(
                id="deployment_stamp",
                layer="deployment",
                status="warn",
                detail=(
                    "Yapılandırılmış damga eskimiş; kanarya güncel değeri okudu ve "
                    "isteklerde o kullanılacak."
                ),
                fix="Kalıcı çözüm için yeni cURL ile damgayı tazeleyin (/curl).",
            )
        )
    else:
        checks.append(
            Check("deployment_stamp", "deployment", "ok", "Sürüm damgası güncel.")
        )

    if settings.upstream_cx_send and (settings.upstream_cx or "").strip():
        checks.append(
            Check(
                id="cx_send",
                layer="config",
                status="warn",
                detail=(
                    "`cx` gövdeye sabit değerle gönderiliyor; ölçümde `cx` her "
                    "istekte değişiyor."
                ),
                fix="Gerekmiyorsa UPSTREAM_CX_SEND=false yapın.",
            )
        )

    # ------------------------------------------------------------- anti-bot
    stats = recaptcha_stats or {}
    generated = int(stats.get("generated") or 0)
    rejected = int(stats.get("rejected") or 0)
    last_error = str(stats.get("last_error") or "")
    if settings.recaptcha_provider == "noop":
        checks.append(
            Check(
                "recaptcha",
                "captcha",
                "ok",
                "CAPTCHA alanı gönderilmiyor (noop).",
            )
        )
    elif generated == 0 and last_error:
        checks.append(
            Check(
                id="recaptcha",
                layer="captcha",
                status="fail",
                detail=f"CAPTCHA token'ı üretilemiyor: {last_error[:200]}",
                fix="Tarayıcı sağlayıcısını/site key'i doğrulayın (docs/TROUBLESHOOTING.md §4).",
            )
        )
    elif rejected > 0:
        checks.append(
            Check(
                id="recaptcha",
                layer="captcha",
                status="warn",
                detail=(
                    f"{generated} token üretildi, {rejected} kez reddedildi. "
                    "Üretim çalışıyor; reddin kaynağı hedefin kendi değerlendirmesi."
                ),
                fix=(
                    "İsteği insan tarayıcıda yürütmek en sürdürülebilir yol; "
                    "sunucu tarafı üretim tek kullanımlık token'ı tekrar kullanır."
                ),
            )
        )
    else:
        checks.append(
            Check(
                "recaptcha",
                "captcha",
                "ok",
                f"CAPTCHA sağlayıcısı hazır ({settings.recaptcha_provider}).",
            )
        )

    # --------------------------------------------------------------- karar
    failing = _first(checks, "fail")
    warning = _first(checks, "warn")
    decisive = failing or warning
    if decisive is None:
        return LayerVerdict(
            layer="ok",
            summary="Bilinen katmanlarda engel yok: istek kimlik + damga + anti-bot ile çıkabilir.",
            checks=checks,
        )

    prefix = "Engel" if failing is not None else "Uyarı"
    return LayerVerdict(
        layer=cast(LayerName, decisive.layer),
        summary=f"{prefix} · {decisive.layer} katmanı: {decisive.detail}",
        checks=checks,
    )
