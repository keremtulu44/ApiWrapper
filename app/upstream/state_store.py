"""Süreç dışı oturum/token kalıcılığı (restart sonrası kendi kendini toparlama).

**Neden var?**

Upstream oturumu ~1 saatlik `access_token` ve her yenilemede dönen (rotation)
bir `refresh_token` taşıyor. `app/upstream/refresh.py` her saat yeni token'ları
bellekteki `Settings` nesnesine yazar; ancak süreç yeniden başlarsa (bilgisayar
kapatılıp açıldığında, container restart ettiğinde veya yeniden deploy'da)
uygulama `.env` ya da `UPSTREAM_CURL` içindeki **ilk (artık geçersizleşmiş)**
`refresh_token` ile uyanır ve 401'e düşer.

Bu modül dönen kimlik ve türev değerleri süreç dışında saklar:

1. **Atomik yerel durum dosyası (`UPSTREAM_STATE_FILE`):**
   Her başarılı token yenilemesinde, `/cookie`, `/curl` ve `/yenile` sonrasında
   güncel çerez/token/damga durumu diske (`0600` izinle) atomik olarak yazılır.
2. **Akıllı başlangıç uzlaştırması (`restore_runtime_state`):**
   Uygulama açılırken başlangıçtaki `.env`/`UPSTREAM_CURL` tohumu (`seed_hash`)
   ile diskteki durumu karşılaştırır:
   * Başlangıç tohumu değişmemişse (`saved.seed_hash == current_seed_hash`),
     diskteki durum o tohumun evrilmiş (refresh edilmiş) hâlidir → **geri yüklenir**.
   * Kullanıcı `.env`/`UPSTREAM_CURL` içine **yeni** bir çerez/cURL koyduysa
     (`saved.seed_hash != current_seed_hash`), hangisinin erişim token'ı daha
     taze ise o tercih edilir; yeni tohum taze ise durum dosyası yeni tohumla
     güncellenir.
3. **Render Env Senkronizasyonu (`RENDER_API_KEY` + `RENDER_SERVICE_ID`):**
   Free Render gibi kalıcı diski olmayan ortamlarda asıl kalıcılık katmanıdır.
   Yapılandırıldığında dönen `UPSTREAM_COOKIE` ve `UPSTREAM_REFRESH_TOKEN`
   değerlerini Render REST API üzerinden sessizce günceller (deploy tetiklemeden).
   Böylece sonraki container açılışında taze refresh token doğrudan ortamdan gelir.
   `RENDER_SERVICE_ID`, Render tarafından otomatik sağlanır.

Güvenlik kuralları:
* Hiçbir gizli değer (cookie, access_token, refresh_token) loglanmaz.
* Dosya yazımı geçici dosya + `os.replace` ile atomiktir; yarım kalan yazım
  dosyayı bozmaz.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.services import session_timeline
from app.upstream.auth import (
    extract_access_token,
    extract_refresh_token,
    is_token_expired,
    token_expiry,
)

logger = get_logger(__name__)

#: Durum dosyası şema sürümü.
_STATE_VERSION = 1

#: Kalıcı olarak saklanan `Settings` alanları.
PERSISTED_FIELDS: tuple[str, ...] = (
    "upstream_cookie",
    "upstream_access_token",
    "upstream_refresh_token",
    "upstream_refresh_api_key",
    "upstream_extra_headers",
    "upstream_referer_path",
    "upstream_stream_path",
    "upstream_captured_model_id",
    "upstream_auth_scheme",
    "upstream_user_agent",
    "upstream_accept_language",
    "upstream_mode",
    "upstream_recaptcha_field",
    "recaptcha_provider",
    "recaptcha_static_token",
    # Public değer (secret değil): yanlış site key ile üretilen token hedefte
    # `recaptcha_rejected` olur, bu yüzden cURL'den öğrenilen değer korunur.
    "recaptcha_site_key",
    "upstream_cx",
    "upstream_cx_send",
    # Proje etiketleri (secret değil): `cli_<parmakizi>:<etiket>,…`. Üretilen
    # anahtarların hangi projeye ait olduğu restart'ta kaybolmasın.
    "api_key_labels",
)

# JSON/local state içinde bool türü korunarak saklanan alanlar.
PERSISTED_BOOL_FIELDS: tuple[str, ...] = ("upstream_auth_from_cookie", "upstream_cx_send")

#: Liste alanları virgülle birleştirilmiş metin olarak saklanır. `POST
#: /v1/admin/keys` ile üretilen anahtarlar böylece restart/deploy sonrası da
#: geçerli kalır (Free Render'da kalıcı disk yok).
PERSISTED_LIST_FIELDS: tuple[str, ...] = ("api_keys",)

#: Süreç başında hesaplanan tohum özeti (`id(settings)` -> sha256 hex).
_SEED_HASHES: dict[int, str] = {}

# Render env yazımları tek bir kalıcı gerçeklik kaynağına giden sıralı işlemlerdir.
# Paralel sync'lerde eski görevin yavaş PUT isteği yeni oturumdan sonra bitebilir ve
# restart'ta eski refresh token'ı diriltebilir. Kilit yalnız Render API yazımlarını
# seriler; canlı sohbet isteklerini veya local state yazımını beklemez.
_RENDER_SYNC_LOCK = asyncio.Lock()

#: Render'ın **kalıcı** olarak reddettiği işlemler (`op`, `env_key`). Aynı değeri
#: tekrar göndermek sonucu değiştirmediği için süreç başına bir kez loglanır;
#: böylece retry döngüsü log'u boğmaz (ölçüm: boş değere PUT → 15/30/60 sn'de bir
#: 400, sonsuza dek). Yeni bir süreç (restart/deploy) listeyi sıfırlar.
_PERMANENT_SYNC_REJECTIONS: set[tuple[str, str]] = set()


def compute_seed_hash(settings: Settings) -> str:
    """Başlangıçtaki `.env` / `UPSTREAM_CURL` kimliğinin kısa SHA-256 parmak izi.

    Gizli değer içermez; yalnızca başlangıç yapılandırmasının iki açılış
    arasında kullanıcı tarafından değiştirilip değiştirilmediğini anlamaya yarar.
    """
    raw = "|".join(
        [
            (settings.target_domain or "").strip().lower(),
            (settings.upstream_cookie or "").strip(),
            (settings.upstream_access_token or "").strip(),
            (settings.upstream_refresh_token or "").strip(),
            (settings.upstream_stream_path or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def remember_seed(settings: Settings) -> str:
    """`Settings` örneğinin ilk (tohum) parmak izini kaydeder."""
    seed = compute_seed_hash(settings)
    _SEED_HASHES[id(settings)] = seed
    return seed


#: Süreç açılışında env'den okunan `API_KEYS` listesi. Durum dosyasındaki kayıtla
#: karşılaştırılıp "operatör env'e yeni bir anahtar ekledi mi" sorusunu cevaplar
#: (bkz. `_merge_api_keys`). Tohum gibi gizli değer tutmaz, yalnız karşılaştırır.
_ENV_API_KEYS: dict[int, list[str]] = {}


def remember_api_key_seed(settings: Settings) -> list[str]:
    """Açılıştaki anahtar listesini kaydeder; **ilk** çağrı kazanır."""
    return _ENV_API_KEYS.setdefault(id(settings), list(settings.api_keys or []))


def env_api_keys(settings: Settings) -> list[str]:
    """Açılışta env'de olan anahtarlar (kayıt yoksa anlık liste)."""
    return _ENV_API_KEYS.get(id(settings)) or list(settings.api_keys or [])


def current_seed(settings: Settings) -> str:
    """Kaydedilmiş başlangıç tohumunu (yoksa anlık hesaplananı) döndürür."""
    return _SEED_HASHES.get(id(settings)) or compute_seed_hash(settings)


def _effective_token(cookie: str, access_token: str, cookie_names: list[str]) -> str | None:
    return (access_token or "").strip() or extract_access_token(
        cookie or "", cookie_names=cookie_names
    )


def _state_path(settings: Settings) -> Path | None:
    raw = (getattr(settings, "upstream_state_file", "") or "").strip()
    if not raw:
        return None
    return Path(raw)


def save_runtime_state(settings: Settings, *, source: str = "runtime") -> bool:
    """Güncel kimlik/türev alanlarını durum dosyasına atomik olarak yazar.

    `upstream_state_file` boşsa hiçbir şey yapmaz ve `False` döner.
    """
    path = _state_path(settings)
    if path is None:
        return False

    domain = (settings.target_domain or "").strip().lower()
    if not domain or domain in {"localhost", "127.0.0.1"}:
        return False

    payload: dict[str, Any] = {
        "version": _STATE_VERSION,
        "snapshot_version": 1,
        "target_domain": domain,
        "seed_hash": current_seed(settings),
        "updated_at": time.time(),
        "source": source,
    }
    for field in PERSISTED_FIELDS:
        payload[field] = getattr(settings, field, "") or ""
    for field in PERSISTED_BOOL_FIELDS:
        payload[field] = bool(getattr(settings, field, False))
    for field in PERSISTED_LIST_FIELDS:
        payload[field] = ",".join(getattr(settings, field, None) or [])
    # Açılış env'i ayrıca kaydedilir: restart'ta "operatör env'e yeni anahtar
    # ekledi mi" ile "çalışma sırasında üretilen anahtar" ayırt edilebilsin.
    payload["api_keys_env"] = ",".join(env_api_keys(settings))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp_path.write_text(encoded, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning(
            "upstream_state_save_failed",
            error_type=type(exc).__name__,
            source=source,
        )
        return False

    logger.info(
        "upstream_state_saved",
        source=source,
        has_cookie=bool(payload["upstream_cookie"]),
        has_refresh_token=bool(
            payload["upstream_refresh_token"]
            or extract_refresh_token(payload["upstream_cookie"])
        ),
    )
    # Ölçüm: kimlik yenileme sınırları olay defterine işlenir. "Ölüm taze
    # cURL'den 3 dakika sonra mı oldu, yoksa 6 saat sonra mı?" sorusu ancak
    # bu kayıtla cevaplanır; log satırları restart'ta kaybolur, defter kalır.
    saved_token = str(payload.get("upstream_access_token") or "")
    if not saved_token:
        saved_token = extract_access_token(str(payload.get("upstream_cookie") or "")) or ""
    session_timeline.note_state_saved(source=source, token=saved_token)
    return True


def load_runtime_state(settings: Settings) -> dict[str, Any] | None:
    """Durum dosyasını okur; yoksa ya da bozuksa `None` döner."""
    path = _state_path(settings)
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("upstream_state_load_failed", error_type=type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _should_restore(settings: Settings, saved: dict[str, Any]) -> tuple[bool, str]:
    """Diskteki durumun başlangıç ayarlarının üzerine yazılıp yazılmayacağını seçer."""
    domain = (settings.target_domain or "").strip().lower()
    saved_domain = str(saved.get("target_domain") or "").strip().lower()
    if not domain or saved_domain != domain:
        return False, "domain_mismatch"

    saved_cookie = str(saved.get("upstream_cookie") or "").strip()
    saved_access = str(saved.get("upstream_access_token") or "").strip()
    saved_refresh = str(saved.get("upstream_refresh_token") or "").strip()
    if not saved_cookie and not saved_access and not saved_refresh:
        return False, "empty_saved_state"

    seed_now = current_seed(settings)
    saved_seed = str(saved.get("seed_hash") or "").strip()

    # 1. Durum: `.env` / `UPSTREAM_CURL` hiç değişmemiş (aynı tohum).
    # Diskteki kayıt bu tohumun çalışma sırasında yenilenmiş/dönmüş hâlidir.
    if saved_seed and saved_seed == seed_now:
        return True, "same_seed_evolved"

    # 2. Durum: Başlangıç ayarları değişmiş (kullanıcı yeni bir cURL/çerez vermiş
    # olabilir ya da proses ilk kez farklı env ile açıldı).
    # Token son kullanma zamanlarını karşılaştır: yeni verilen env token'ı geçerli
    # ve disktekinden daha yeni (veya eşit) ise kullanıcının yeni girdisi kazanır.
    env_token = _effective_token(
        settings.upstream_cookie,
        settings.upstream_access_token,
        settings.upstream_token_cookie_names,
    )
    saved_token = _effective_token(
        saved_cookie,
        saved_access,
        settings.upstream_token_cookie_names,
    )
    env_exp = token_expiry(env_token) if env_token else None
    saved_exp = token_expiry(saved_token) if saved_token else None

    if saved_exp is not None and env_exp is not None:
        if saved_exp > env_exp:
            return True, "saved_token_newer"
        return False, "env_token_newer_or_equal"

    if (
        saved_token
        and not is_token_expired(saved_token)
        and (not env_token or is_token_expired(env_token))
    ):
        return True, "saved_token_valid_env_expired"

    return False, "env_preferred"


def _merge_api_keys(
    env_keys: list[str], saved_keys: list[str], saved: dict[str, Any]
) -> list[str]:
    """Durum dosyasındaki anahtar listesiyle açılış env'ini uzlaştırır.

    İki ayrı hata modu var ve ikisi de pahalı:
    * Üretilmiş anahtarı düşürmek → tüketici 401 alır (yeni cURL deploy'unda
      olurdu, çünkü kimlik kararı "env daha taze" deyip disk kaydını atlar).
    * İptal edilmiş anahtarı diriltmek → güvenlik açığı.

    Kural: **kayıt kazanır** (iptal niyeti korunur), ama env'de olup kaydın
    açılış env'inde *olmayan* anahtarlar operatörün yeni girdisidir ve sona
    eklenir. Kayıt eski şemadaysa (`api_keys_env` yok) ayırt edilemediği için
    olduğu gibi uygulanır.
    """
    baseline = [item.strip() for item in str(saved.get("api_keys_env") or "").split(",")]
    baseline = [item for item in baseline if item]
    if not baseline:
        return saved_keys
    added_by_operator = [
        key for key in env_keys if key not in baseline and key not in saved_keys
    ]
    if added_by_operator:
        logger.info(
            "runtime_state_api_keys_env_added", added_count=len(added_by_operator)
        )
    return [*saved_keys, *added_by_operator]


def _restore_non_identity_fields(settings: Settings, saved: dict[str, Any]) -> list[str]:
    """Kimlik **olmayan** alanları (anahtar listesi + proje etiketleri) uygular.

    Çerez/token kararından (`_should_restore`) bağımsızdır: `API_KEYS` upstream
    kimliği değildir, dolayısıyla "hangisi daha taze" sorusunun token son
    kullanma zamanıyla cevabı yok. Bu alanlar yalnız domain eşleştiğinde ve
    kayıt boş değilse uygulanır.
    """
    domain = (settings.target_domain or "").strip().lower()
    saved_domain = str(saved.get("target_domain") or "").strip().lower()
    if not domain or saved_domain != domain:
        return []

    changed: list[str] = []
    for field in PERSISTED_LIST_FIELDS:
        if field not in saved:
            continue
        val = saved.get(field)
        if not isinstance(val, str):
            continue
        parsed = [item.strip() for item in val.split(",") if item.strip()]
        if not parsed and field == "api_keys":
            # Boş anahtar listesi **uygulanmaz**: eksik/bozuk bir kayıt wrapper'ı
            # herkese kapatırdı ve geri almak için env'e elle müdahale gerekirdi.
            # "Anahtarları boşalt" diye bir niyet yok; iptal tek tek
            # `DELETE /v1/admin/keys/{key_id}` ile yapılır ve son anahtarın
            # iptali zaten reddediliyor.
            logger.warning("runtime_state_empty_api_keys_ignored")
            continue
        current = list(getattr(settings, field, None) or [])
        if field == "api_keys":
            parsed = _merge_api_keys(current, parsed, saved)
        if current != parsed:
            setattr(settings, field, parsed)
            changed.append(field)

    if "api_key_labels" in saved:
        label_val = saved.get("api_key_labels")
        if isinstance(label_val, str) and settings.api_key_labels != label_val:
            settings.api_key_labels = label_val
            changed.append("api_key_labels")
    return changed


def restore_runtime_state(settings: Settings) -> list[str]:
    """Başlangıçta durum dosyasındaki daha taze kimlik/türevleri ayarlara uygular.

    Dönüş: güncellenen alan adları listesi.
    """
    remember_seed(settings)
    remember_api_key_seed(settings)
    saved = load_runtime_state(settings)
    if saved is None:
        return []

    # Üretilmiş proje anahtarları kimlik kararına bağlı değil: yeni bir cURL
    # deploy edildi diye tüketicinin anahtarı düşmemeli.
    changed: list[str] = _restore_non_identity_fields(settings, saved)

    should_apply, reason = _should_restore(settings, saved)
    if not should_apply:
        logger.info(
            "upstream_state_skipped", reason=reason, restored_fields=changed
        )
        # Kullanıcı `.env`'e daha yeni bir cURL/çerez koyduysa durum dosyasını
        # bu yeni tohumla hizala ki sonraki restart'ta eski kayıt hortlamasın.
        if reason in {"env_token_newer_or_equal", "env_preferred"} and (
            settings.upstream_cookie or settings.upstream_access_token
        ):
            save_runtime_state(settings, source="seed_sync")
        return changed

    if saved.get("snapshot_version") == 1 and settings.upstream_snapshot_version != 1:
        settings.upstream_snapshot_version = 1
        changed.append("upstream_snapshot_version")
    for field in PERSISTED_FIELDS:
        # Kayıt şemasında bulunan boş string de bilinçli bir durumdur: yeni
        # cURL eski refresh token'ı temizlemiş olabilir. Boşları atlamak,
        # aynı tohumla restart'ta eski secret'ı yeniden diriltir. Şemada hiç
        # bulunmayan alanlar ise geriye uyumluluk için korunur.
        if field not in saved:
            continue
        val = saved.get(field)
        if not isinstance(val, str):
            continue
        if getattr(settings, field, "") != val:
            setattr(settings, field, val)
            changed.append(field)
    for field in PERSISTED_BOOL_FIELDS:
        val = saved.get(field)
        if isinstance(val, bool) and getattr(settings, field, False) != val:
            setattr(settings, field, val)
            changed.append(field)

    if changed:
        logger.info(
            "upstream_state_restored",
            reason=reason,
            fields=changed,
        )
    return changed


async def sync_render_env(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Güncel kimlik snapshot'ını Render'a sırayla ve atomik sırayla yazar.

    Render API her env anahtarını ayrı PUT ile günceller. Kilidin içinde snapshot
    almak, bekleyen eski bir görevin yeni kimliği tekrar eski değerlerle ezmesini
    önler; son görev daima o anda `Settings` içindeki en güncel değerleri okur.
    """
    if not (
        (getattr(settings, "render_api_key", "") or "").strip()
        and (getattr(settings, "render_service_id", "") or "").strip()
    ):
        return False
    async with _RENDER_SYNC_LOCK:
        return await _sync_render_env_locked(settings, client)


async def _sync_render_env_locked(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Yapılandırılmışsa dönen çerez/refresh token'ı Render servis env'ine yazar.

    Render'da container diski yeniden başlatmada sıfırlanabilir. Eğer
    `RENDER_API_KEY` ve `RENDER_SERVICE_ID` verilmişse, dönen `UPSTREAM_COOKIE`,
    `UPSTREAM_REFRESH_TOKEN` ve `UPSTREAM_ACCESS_TOKEN` değerleri Render REST API
    üzerinden tekil env değişkenleri olarak güncellenir (deploy tetiklenmez).
    """
    api_key = (getattr(settings, "render_api_key", "") or "").strip()
    service_id = (getattr(settings, "render_service_id", "") or "").strip()
    if not api_key or not service_id:
        return False

    updates: dict[str, str] = {}
    if settings.upstream_cookie:
        # Cookie bir oturum anlık görüntüsüyse token alanları da onunla birlikte
        # yazılır — **boş değerler dahil**. Aksi halde yeni cURL refresh token
        # taşımazken Render'daki eski değer durur ve restart'ta iki farklı
        # oturumun kimlikleri birleşir. Cookie'siz, yalnız token kullanan
        # kurulumların davranışı aşağıdaki bağımsız alanlarla korunur.
        updates["UPSTREAM_COOKIE"] = settings.upstream_cookie
        updates["UPSTREAM_REFRESH_TOKEN"] = settings.upstream_refresh_token or ""
        updates["UPSTREAM_ACCESS_TOKEN"] = settings.upstream_access_token or ""
    else:
        if settings.upstream_refresh_token:
            updates["UPSTREAM_REFRESH_TOKEN"] = settings.upstream_refresh_token
        if settings.upstream_access_token:
            updates["UPSTREAM_ACCESS_TOKEN"] = settings.upstream_access_token

    # Cookie ve tarayıcı parmak izi aynı cURL snapshot'ının parçalarıdır. Yalnız
    # cookie'yi saklayıp eski UPSTREAM_CURL'deki UA/Authorization/mode/header
    # değerleriyle birleştirmek restart'ta yarı eski bir istemci üretir. Boş
    # değerler de yazılır: yeni cURL'de kaldırılan eski header/token dirilmez.
    snapshot_fields = {
        "UPSTREAM_EXTRA_HEADERS": settings.upstream_extra_headers or "",
        "UPSTREAM_AUTH_FROM_COOKIE": (
            "true" if settings.upstream_auth_from_cookie else "false"
        ),
        "UPSTREAM_AUTH_SCHEME": settings.upstream_auth_scheme or "",
        "UPSTREAM_USER_AGENT": settings.upstream_user_agent or "",
        "UPSTREAM_ACCEPT_LANGUAGE": settings.upstream_accept_language or "",
        "UPSTREAM_MODE": settings.upstream_mode or "",
        "UPSTREAM_RECAPTCHA_FIELD": settings.upstream_recaptcha_field or "",
        "UPSTREAM_REFERER_PATH": settings.upstream_referer_path or "",
        "UPSTREAM_STREAM_PATH": settings.upstream_stream_path or "",
        "UPSTREAM_CAPTURED_MODEL_ID": settings.upstream_captured_model_id or "",
        "UPSTREAM_CX": settings.upstream_cx or "",
        "UPSTREAM_CX_SEND": "true" if settings.upstream_cx_send else "false",
        "UPSTREAM_SNAPSHOT_VERSION": "1",
        "RECAPTCHA_PROVIDER": settings.recaptcha_provider or "",
        "RECAPTCHA_STATIC_TOKEN": settings.recaptcha_static_token or "",
        "RECAPTCHA_SITE_KEY": settings.recaptcha_site_key or "",
    }
    updates.update(snapshot_fields)
    # Üretilen proje anahtarları ve etiketleri de kalıcı olsun: Free Render'da
    # disk yok, restart'ta bu değerler env'den okunuyor. `API_KEYS` boş
    # yazılmaz — boş liste yukarıda DELETE'e dönüşürdü ve son anahtarın iptali
    # zaten `ProjectRegistry.revoke` tarafından reddediliyor.
    if settings.api_keys:
        updates["API_KEYS"] = ",".join(settings.api_keys)
    updates["API_KEY_LABELS"] = settings.api_key_labels or ""
    if settings.upstream_refresh_api_key:
        updates["UPSTREAM_REFRESH_API_KEY"] = settings.upstream_refresh_api_key
    if not updates:
        return False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    # Boş değerler snapshot'a **bilerek** girer: yeni cURL'de olmayan eski
    # header/token restart'ta dirilmesin. Ama Render boş değeri PUT ile kabul
    # etmiyor (ölçüm: 400 → 15/30/60 sn'lik sonsuz tekrar deneme). İstenen son
    # durum "bu anahtar yok" olduğu için doğru işlem DELETE.
    writes = {key: value for key, value in updates.items() if value.strip()}
    clears = sorted(key for key, value in updates.items() if not value.strip())
    ok = True
    try:
        for key, value in writes.items():
            if not await _put_env_var(http, headers, service_id, key, value):
                ok = False
        for key in clears:
            if not await _delete_env_var(http, headers, service_id, key):
                ok = False
    except httpx.HTTPError as exc:
        ok = False
        logger.warning("render_env_sync_error", error_type=type(exc).__name__)
    finally:
        if owns_client:
            await http.aclose()

    if ok:
        logger.info("render_env_synced", keys=sorted(writes), cleared=clears)
    return ok


def _env_var_url(service_id: str, key: str) -> str:
    return f"https://api.render.com/v1/services/{service_id}/env-vars/{key}"


def _is_transient_status(status: int) -> bool:
    """Tekrar denemeye değer mi: 429 ve 5xx evet, diğer 4xx hayır.

    `app/upstream/client.py` upstream için aynı kuralı uyguluyor; iki ayrı yerde
    iki ayrı tanım olmasın diye buradaki de aynı biçimde yazıldı.
    """
    return status == 429 or status >= 500


def _note_permanent_rejection(op: str, key: str, status_code: int) -> None:
    """Kalıcı reddi (4xx) süreç başına **bir kez** loglar.

    4xx'te sorun ağda değil değerde/yetkidedir: tekrar denemek aynı yanıtı
    üretir. Ölçümde bu, her 15/30/60 sn'de bir `render_env_sync_failed` +
    `render_env_sync_retry_scheduled` çifti demekti ve gerçek arızalar
    gürültünün altında kayboluyordu.
    """
    marker = (op, key)
    if marker in _PERMANENT_SYNC_REJECTIONS:
        return
    _PERMANENT_SYNC_REJECTIONS.add(marker)
    logger.warning(
        "render_env_sync_rejected", op=op, env_key=key, status_code=status_code
    )


async def _put_env_var(
    http: httpx.AsyncClient,
    headers: dict[str, str],
    service_id: str,
    key: str,
    value: str,
) -> bool:
    """Tek anahtarı yazar.

    Dönüş değeri "tekrar denemeye değer mi" sorusudur: `False` yalnız geçici
    hatada (5xx) döner; kalıcı red loglanır ama retry döngüsünü beslemez.
    """
    response = await http.put(
        _env_var_url(service_id, key), headers=headers, json={"value": value}
    )
    status = response.status_code
    if status < 400:
        return True
    if not _is_transient_status(status):
        _note_permanent_rejection("put", key, status)
        return True
    logger.warning("render_env_sync_failed", op="put", env_key=key, status_code=status)
    return False


async def _delete_env_var(
    http: httpx.AsyncClient, headers: dict[str, str], service_id: str, key: str
) -> bool:
    """Boş değeri "anahtarı sil" olarak uygular.

    404 başarısızlık değildir: anahtar zaten yok, yani istenen son durum sağlanmış.
    """
    response = await http.delete(_env_var_url(service_id, key), headers=headers)
    status = response.status_code
    if status < 400 or status == 404:
        return True
    if not _is_transient_status(status):
        _note_permanent_rejection("delete", key, status)
        return True
    logger.warning(
        "render_env_sync_failed", op="delete", env_key=key, status_code=status
    )
    return False
