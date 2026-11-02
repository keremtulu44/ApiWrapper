"""İnsan diline çevrilmiş arıza açıklamaları.

API cevapları OpenAI uyumluluğu için İngilizce kalır; **operatöre** dönen yüzeyler
(Telegram botu, saatlik probe bildirimleri, log `hint` alanları) ise "ne oldu" ve
"ne yapmalıyım" sorusunu tek satırda yanıtlamalıdır. Bu modül tek doğruluk
kaynağıdır: aynı kod her yerde aynı cümleyle anlatılır.

Kurallar:
* Metinler kısa ve eyleme dönük; spam'e dönüşmemesi için bildirim katmanı
  tekrar sıklığını ayrıca sınırlar.
* Hiçbir metin gizli değer (cookie/token/URL kimliği) içermez.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureHint:
    """Bir arıza kodunun operatör diliyle karşılığı."""

    title: str
    action: str
    #: "critical" → elle müdahale gerekir, "warning" → bekle/kendiliğinden geçer.
    severity: str = "warning"


#: Kod → açıklama. Anahtarlar `app/core/errors.py` ve `_translate` çıktılarıyla
#: birebir aynıdır; eşleşmeyen kodlar için `explain_failure` durum koduna düşer.
_HINTS: dict[str, FailureHint] = {
    "upstream_refresh_required": FailureHint(
        title="Upstream, istemci sürümünü eskimiş buldu (HTTP 409 refresh_required).",
        action=(
            "Tarayıcıda sayfayı sert yenileyip (Ctrl+Shift+R) tamamlanmış isteği "
            "yeniden cURL olarak kopyalayın, UPSTREAM_CURL değerini güncelleyip "
            "yeniden deploy edin. Cookie/token suçlu değildir: sorun "
            "x-...-deployment / x-deployment-id sürüm damgasında."
        ),
        severity="critical",
    ),
    "upstream_quota_reached": FailureHint(
        title="Upstream hesabı geçici olarak kilitledi (kota penceresi doldu).",
        action=(
            "Retry-After / pencere süresi dolana kadar bekleyin; çok hesaplıysanız "
            "/v1/admin/accounts ile hangi hesabın dinlendiğini görün. Israrla "
            "denemek kilidi uzatır."
        ),
    ),
    "recaptcha_unavailable": FailureHint(
        title="reCAPTCHA token'ı üretilemedi veya yenilenemedi.",
        action=(
            "RECAPTCHA_PROVIDER=browser ise Chromium/Google erişimini, "
            "RECAPTCHA_SITE_KEY/ACTION/VERSION değerlerini kontrol edin. "
            "static ise token ~2 dakikada ölür: yenileyin."
        ),
        severity="critical",
    ),
    "recaptcha_rejected": FailureHint(
        title="Upstream reCAPTCHA token'ını reddetti.",
        action=(
            "Token üretimini yenileyin (/v1/admin/recaptcha/invalidate); "
            "browser/external sağlayıcıya geçmek kalıcı çözümdür."
        ),
        severity="critical",
    ),
    "upstream_unauthorized": FailureHint(
        title="Upstream oturum kimliğini reddetti (HTTP 401).",
        action=(
            "Cookie/token süresi dolmuş. Tarayıcıdan taze cURL alıp UPSTREAM_CURL'ü "
            "güncelleyin; /v1/admin/auth?slot=N ile hangi hesabın düştüğünü görün."
        ),
        severity="critical",
    ),
    "upstream_not_found": FailureHint(
        title="Upstream akış yolu bulunamadı (HTTP 404).",
        action=(
            "UPSTREAM_STREAM_PATH güncel değil. Tarayıcıdaki isteği cURL olarak "
            "kopyalayıp scripts/curl_to_env.py ile yolu yeniden içe aktarın."
        ),
        severity="critical",
    ),
    "upstream_redirect": FailureHint(
        title="Upstream isteği başka bir adrese yönlendirdi.",
        action=(
            "POST'un GET'e dönüşmemesi için yönlendirme takip edilmiyor. "
            "UPSTREAM_STREAM_PATH'i yönlendirilen yola güncelleyin."
        ),
        severity="critical",
    ),
    "upstream_unavailable": FailureHint(
        title="Upstream geçici olarak erişilemez (devre kesici açık).",
        action=(
            "Kısa süre bekleyin; kalıcıysa /v1/admin/breaker/reset ve upstream "
            "durumunu kontrol edin."
        ),
    ),
    "upstream_timeout": FailureHint(
        title="Upstream zaman aşımına uğradı.",
        action=(
            "Akış kurulduktan sonra sessizlik sürdüyse STREAM_IDLE_TIMEOUT, "
            "toplam süre aşıldıysa TOTAL_REQUEST_TIMEOUT değerini gözden geçirin."
        ),
    ),
    "wrapper_unreachable": FailureHint(
        title="Servis kendi API'sine ulaşamadı (loopback bağlantısı kurulamadı).",
        action=(
            "Süreç ayakta mı ve PORT doğru mu? Probe 127.0.0.1:{PORT} adresine "
            "gidiyor; uvicorn farklı portu dinliyorsa bu hata görülür."
        ),
        severity="critical",
    ),
    "upstream_failure": FailureHint(
        title="Upstream isteği başarısız oldu.",
        action="Live tail'de aynı request_id ile upstream_http_error satırına bakın.",
    ),
    "invalid_api_key": FailureHint(
        title="API anahtarı reddedildi (401).",
        action=(
            "Telegram botundaki WRAPPER_API_KEY ile servisteki API_KEYS aynı mı? "
            "Anahtar değiştiyse botu yeniden başlatın."
        ),
        severity="critical",
    ),
    "missing_api_key": FailureHint(
        title="İstekte API anahtarı yok (401).",
        action="Authorization: Bearer <API_KEYS> başlığını ekleyin.",
        severity="critical",
    ),
    "rate_limit_exceeded": FailureHint(
        title="Yerel hız sınırı aşıldı (429).",
        action=(
            "RATE_LIMIT_RPM / RATE_LIMIT_BURST değerlerini artırın ya da istek "
            "sıklığını düşürün."
        ),
    ),
    "model_not_found": FailureHint(
        title="İstenen model tanımlı değil (404).",
        action="config/models.yaml içindeki id/alias değerlerini kontrol edin.",
        severity="critical",
    ),
    "payload_too_large": FailureHint(
        title="İstek gövdesi/prompt çok büyük (413).",
        action="MAX_BODY_BYTES, MAX_MESSAGES ve MAX_PROMPT_CHARS sınırlarını gözden geçirin.",
    ),
    "internal_error": FailureHint(
        title="Wrapper içinde beklenmeyen hata (500).",
        action="Live tail'de aynı request_id ile unhandled_exception satırına bakın.",
        severity="critical",
    ),
}

#: Yalnızca durum kodundan çıkarılabilen yedekler.
_STATUS_HINTS: dict[int, FailureHint] = {
    401: FailureHint(
        title="Kimlik doğrulanamadı (HTTP 401).",
        action="API anahtarını ve upstream cookie/token'ını kontrol edin.",
        severity="critical",
    ),
    403: FailureHint(
        title="Erişim reddedildi (HTTP 403).",
        action="Hesabın bu modele/uç noktaya yetkisi var mı kontrol edin.",
        severity="critical",
    ),
    404: FailureHint(
        title="Uç nokta bulunamadı (HTTP 404).",
        action="Yol ve model tanımlarını kontrol edin.",
        severity="critical",
    ),
    429: FailureHint(
        title="Hız/kota sınırı (HTTP 429).",
        action="Retry-After süresi kadar bekleyin.",
    ),
    502: FailureHint(
        title="Upstream isteği başarısız (HTTP 502).",
        action="Upstream cevabını live tail'den kontrol edin.",
    ),
    503: FailureHint(
        title="Servis geçici olarak hizmet veremiyor (HTTP 503).",
        action="Kısa süre sonra tekrar deneyin.",
    ),
    504: FailureHint(
        title="Upstream zaman aşımı (HTTP 504).",
        action="Timeout ayarlarını ve upstream gecikmesini kontrol edin.",
    ),
}

_FALLBACK = FailureHint(
    title="Beklenmeyen bir arıza oluştu.",
    action="Live tail'de aynı request_id ile ilgili log satırlarına bakın.",
)


def explain_failure(status_code: int | None, code: str | None = None) -> FailureHint:
    """Arıza kodunu (yoksa HTTP durumunu) operatör diline çevirir."""
    if code:
        hint = _HINTS.get(code.strip())
        if hint is not None:
            return hint
    if status_code is not None:
        hint = _STATUS_HINTS.get(int(status_code))
        if hint is not None:
            return hint
    return _FALLBACK


def format_failure(status_code: int | None, code: str | None, *, action: bool = True) -> str:
    """Telegram'da tek bakışta okunacak iki satırlık özet üretir."""
    hint = explain_failure(status_code, code)
    head = f"{hint.title}"
    if code:
        head += f" [{code}]"
    if not action:
        return head
    return f"{head}\n→ {hint.action}"
