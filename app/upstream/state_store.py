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
3. **Opsiyonel Render Env Senkronizasyonu (`RENDER_API_KEY` + `RENDER_SERVICE_ID`):**
   Render gibi geçici diskli (ephemeral) ortamlarda, yapılandırılmışsa dönen
   `UPSTREAM_COOKIE` ve `UPSTREAM_REFRESH_TOKEN` değerlerini Render REST API
   üzerinden sessizce günceller (deploy tetiklemeden). Böylece sonraki container
   açılışında taze refresh token doğrudan ortamdan gelir.

Güvenlik kuralları:
* Hiçbir gizli değer (cookie, access_token, refresh_token) loglanmaz.
* Dosya yazımı geçici dosya + `os.replace` ile atomiktir; yarım kalan yazım
  dosyayı bozmaz.
"""

from __future__ import annotations

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
)

#: Süreç başında hesaplanan tohum özeti (`id(settings)` -> sha256 hex).
_SEED_HASHES: dict[int, str] = {}


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
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def remember_seed(settings: Settings) -> str:
    """`Settings` örneğinin ilk (tohum) parmak izini kaydeder."""
    seed = compute_seed_hash(settings)
    _SEED_HASHES[id(settings)] = seed
    return seed


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
        "target_domain": domain,
        "seed_hash": current_seed(settings),
        "updated_at": time.time(),
        "source": source,
    }
    for field in PERSISTED_FIELDS:
        payload[field] = getattr(settings, field, "") or ""

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


def restore_runtime_state(settings: Settings) -> list[str]:
    """Başlangıçta durum dosyasındaki daha taze kimlik/türevleri ayarlara uygular.

    Dönüş: güncellenen alan adları listesi.
    """
    remember_seed(settings)
    saved = load_runtime_state(settings)
    if saved is None:
        return []

    should_apply, reason = _should_restore(settings, saved)
    if not should_apply:
        logger.info("upstream_state_skipped", reason=reason)
        # Kullanıcı `.env`'e daha yeni bir cURL/çerez koyduysa durum dosyasını
        # bu yeni tohumla hizala ki sonraki restart'ta eski kayıt hortlamasın.
        if reason in {"env_token_newer_or_equal", "env_preferred"} and (
            settings.upstream_cookie or settings.upstream_access_token
        ):
            save_runtime_state(settings, source="seed_sync")
        return []

    changed: list[str] = []
    for field in PERSISTED_FIELDS:
        val = saved.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        if getattr(settings, field, "") != val:
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
        updates["UPSTREAM_COOKIE"] = settings.upstream_cookie
    if settings.upstream_refresh_token:
        updates["UPSTREAM_REFRESH_TOKEN"] = settings.upstream_refresh_token
    if settings.upstream_access_token:
        updates["UPSTREAM_ACCESS_TOKEN"] = settings.upstream_access_token
    if settings.upstream_refresh_api_key:
        updates["UPSTREAM_REFRESH_API_KEY"] = settings.upstream_refresh_api_key
    if settings.upstream_extra_headers:
        updates["UPSTREAM_EXTRA_HEADERS"] = settings.upstream_extra_headers
    if settings.upstream_referer_path:
        updates["UPSTREAM_REFERER_PATH"] = settings.upstream_referer_path
    if not updates:
        return False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    ok = True
    try:
        for key, value in updates.items():
            url = (
                f"https://api.render.com/v1/services/{service_id}/env-vars/{key}"
            )
            response = await http.put(url, headers=headers, json={"value": value})
            if response.status_code >= 400:
                ok = False
                logger.warning(
                    "render_env_sync_failed",
                    env_key=key,
                    status_code=response.status_code,
                )
    except httpx.HTTPError as exc:
        ok = False
        logger.warning("render_env_sync_error", error_type=type(exc).__name__)
    finally:
        if owns_client:
            await http.aclose()

    if ok:
        logger.info("render_env_synced", keys=sorted(updates))
    return ok
