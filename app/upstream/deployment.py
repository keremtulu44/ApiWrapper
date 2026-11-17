"""Güncel deployment kimliği kanaryası (Vercel `dpl_...`).

**Neden var?**

Upstream, isteğin hangi sürümden geldiğini `x-deployment-id` /
`x-arena-web-deployment` başlıklarından okuyor ve eski bir kimlikle gelen istekleri
``409 {"error":"refresh_required","reason":"forced_update"}`` ile reddediyor.
Wrapper bu değeri `UPSTREAM_CURL`'den **dondurulmuş** olarak taşıdığı için her
üretim deploy'unda istekler kırılıyor ve elle cURL tazelemek gerekiyordu.

**Ne yapıyor?**

Değeri tahmin etmez; upstream'in **kendi sayfasından** okur:

1. yanıt başlıkları (`x-deployment-id`, `x-arena-web-deployment`),
2. olmazsa HTML/gövde içindeki `dpl_...` deseni.

Okunan değer TTL boyunca önbelleklenir ve isteğe yazılır. 409 alınırsa önbellek
atlanıp **zorla** yenilenir (bir kez). Okuma başarısız olursa (Cloudflare
challenge, ağ hatası, sayfa değişikliği) davranış eskisi gibi kalır: yapılandırılmış
başlık değeri kullanılır — yani kanarya hiçbir zaman isteği bozmaz.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.upstream.headers import DEPLOYMENT_HEADERS, build_page_headers

logger = get_logger(__name__)

#: Sayfa gövdesinde geçen Vercel deployment kimliği (örn. `dpl_DJU19p...`).
_DPL_PATTERN = re.compile(r"dpl_[A-Za-z0-9]{6,}")
#: Gövdede aranacak azami karakter: koskoca HTML'i taramaya gerek yok.
_BODY_SCAN_LIMIT = 400_000


@dataclass(slots=True)
class DeploymentReading:
    """Bir kanarya okumasının sonucu."""

    #: Kullanılacak kimlik (okunamadıysa yapılandırılmış değer, o da yoksa None).
    value: str | None
    #: "header" | "body" | "cache" | "configured" | "none"
    source: str
    #: Değer bu okumada değişti mi?
    changed: bool = False


@dataclass(slots=True)
class _Entry:
    value: str | None = None
    checked_at: float = 0.0
    source: str = "none"


def _from_headers(headers: httpx.Headers) -> str | None:
    """Yanıt başlıklarından deployment kimliğini çıkarır."""
    for name in DEPLOYMENT_HEADERS:
        value = (headers.get(name) or "").strip()
        if value:
            return value
    return None


def _from_body(text: str) -> str | None:
    """HTML/gövde içindeki `dpl_...` desenini bulur."""
    match = _DPL_PATTERN.search(text[:_BODY_SCAN_LIMIT])
    return match.group(0) if match else None


class DeploymentIdResolver:
    """Upstream'in güncel deployment kimliğini okur ve önbellekler.

    Önbellek `base_url` başına tutulur: çok hesaplı kurulumda hesaplar farklı
    domain'lere gidebilir ve deployment kimlikleri farklıdır.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._warned: set[str] = set()

    # ------------------------------------------------------------- helpers
    @property
    def configured(self) -> str | None:
        """`UPSTREAM_EXTRA_HEADERS` içinde gelen (donmuş) deployment kimliği."""
        for key, value in self.settings.parsed_extra_headers().items():
            if key.lower() in DEPLOYMENT_HEADERS and value.strip():
                return value.strip()
        return None

    @property
    def active(self) -> bool:
        """Kanarya yalnızca bu başlığı zaten gönderiyorsak devreye girer."""
        return bool(self.settings.deployment_id_refresh_enabled) and (
            self.configured is not None
        )

    def cached(self, base_url: str) -> str | None:
        entry = self._entries.get(base_url)
        return entry.value if entry else None

    def _lock(self, base_url: str) -> asyncio.Lock:
        lock = self._locks.get(base_url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[base_url] = lock
        return lock

    def _probe_url(self, base_url: str) -> str:
        path = (self.settings.deployment_probe_path or "/").strip() or "/"
        if not path.startswith("/"):
            path = "/" + path
        return f"{base_url.rstrip('/')}{path}"

    def _fallback(self, base_url: str, *, changed: bool = False) -> DeploymentReading:
        cached = self.cached(base_url)
        if cached:
            return DeploymentReading(cached, "cache", changed)
        configured = self.configured
        return DeploymentReading(configured, "configured" if configured else "none", changed)

    # ---------------------------------------------------------------- read
    async def refresh(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        *,
        force: bool = False,
    ) -> DeploymentReading:
        """Güncel kimliği döndürür; gerekirse sayfayı bir kez okur.

        `force=True` TTL'i atlar (409 sonrası kullanılır). Hata durumunda
        istisna fırlatmaz: önbelleğe/yapılandırılmış değere düşer.
        """
        if not self.active:
            configured = self.configured
            return DeploymentReading(configured, "configured" if configured else "none")

        ttl = max(0.0, self.settings.deployment_id_ttl_seconds)
        entry = self._entries.setdefault(base_url, _Entry())
        now = time.monotonic()
        if not force and entry.value and now - entry.checked_at < ttl:
            return DeploymentReading(entry.value, "cache")

        async with self._lock(base_url):
            now = time.monotonic()
            if not force and entry.value and now - entry.checked_at < ttl:
                return DeploymentReading(entry.value, "cache")

            probe_url = self._probe_url(base_url)
            try:
                response = await client.get(
                    probe_url,
                    headers=build_page_headers(self.settings, include_cookie=False),
                    timeout=httpx.Timeout(self.settings.deployment_probe_timeout),
                    follow_redirects=True,
                )
            except Exception as exc:
                self._warn_once(base_url, "probe_failed", error_type=type(exc).__name__)
                return self._fallback(base_url)

            value: str | None = None
            source = "none"
            if 200 <= response.status_code < 400:
                value = _from_headers(response.headers)
                if value:
                    source = "header"
                else:
                    value = _from_body(response.text or "")
                    if value:
                        source = "body"
            else:
                self._warn_once(
                    base_url,
                    "probe_rejected",
                    status=response.status_code,
                    hint=(
                        "Kanarya sayfası okunamadı (bot koruması olabilir). "
                        "Yapılandırılmış deployment kimliği kullanılmaya devam eder."
                    ),
                )

            if not value:
                # Okunamadı: önbelleği koru, yapılandırılmış değere düş.
                entry.checked_at = now
                return self._fallback(base_url)

            previous = entry.value
            changed = previous is not None and previous != value
            entry.value = value
            entry.source = source
            entry.checked_at = now
            self._warned.discard(base_url)
            if previous is None:
                logger.info(
                    "deployment_id_detected",
                    source=source,
                    deployment_id=value,
                    configured=bool(self.configured),
                )
            elif changed:
                logger.warning(
                    "deployment_id_changed",
                    previous=previous,
                    current=value,
                    source=source,
                    hint=(
                        "Upstream yeni bir sürüm yayınladı; istekler yeni kimlikle "
                        "gönderilecek. Elle cURL tazelemek gerekmez."
                    ),
                )
            return DeploymentReading(value, source, changed)

    def _warn_once(self, base_url: str, event: str, **fields: object) -> None:
        """Aynı hedef için aynı uyarıyı oturum başına bir kez üretir."""
        marker = f"{base_url}:{event}"
        if marker in self._warned:
            return
        self._warned.add(marker)
        # Domain loglanmaz: yalnızca kanaryanın hangi aşamada takıldığı görünür.
        logger.warning(event, **fields)

    def snapshot(self, base_url: str) -> dict[str, object]:
        """Sağlık uç noktası için özet: **değer** değil, durum bilgisi.

        Ham deployment kimliği kimlik doğrulamasız bir yanıtta gösterilmez;
        yalnızca kanaryanın takip edip etmediği ve okumanın yaşı paylaşılır.
        """
        entry = self._entries.get(base_url)
        age: float | None = None
        if entry is not None and entry.checked_at:
            age = round(time.monotonic() - entry.checked_at, 1)
        return {
            "managed": self.active,
            "source": entry.source if entry else ("configured" if self.configured else "none"),
            "age_seconds": age,
        }

    def reset(self) -> None:
        """Önbelleği temizler (test/admin kullanımı)."""
        self._entries.clear()
        self._warned.clear()
