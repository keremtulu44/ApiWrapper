"""Upstream HTTP istemcisi: havuzlu httpx.AsyncClient, retry, devre kesici, streaming."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.services.account import Account
from app.upstream.auth import parse_cookie_header
from app.upstream.breaker import CircuitBreaker
from app.upstream.deployment import DeploymentIdResolver
from app.upstream.exceptions import (
    CircuitOpen,
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamQuotaExceeded,
    UpstreamRefreshRequired,
    UpstreamTimeout,
)
from app.upstream.headers import (
    DEPLOYMENT_HEADERS,
    build_page_headers,
    build_stream_headers,
)
from app.upstream.quota import find_quota_marker, normalize_markers, quota_error_message
from app.utils.backoff import full_jitter_delay, parse_retry_after

logger = get_logger(__name__)

#: Free Render env yazımı başarısız olduğunda artan aralıkla yeniden deneme.
#: İlk denemeler hızlıdır (geçici Render 5xx/ağ hatası), üst sınır log ve API
#: yükünü sınırlar.
_RENDER_SYNC_RETRY_INITIAL_SECONDS = 15.0
_RENDER_SYNC_RETRY_MAX_SECONDS = 300.0

#: Yeniden denenebilir HTTP durum kodları.
#: Not: 429 burada kalır çünkü upstream geçici bir yavaşlatma için de 429 dönebilir;
#: ancak gövdede kota işareti varsa istek retry edilmez (aşağıda `_is_quota_body`).
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: Kimlik/captcha reddi olabilecek durum kodları.
CAPTCHA_STATUS = frozenset({401, 403})

#: Bir 403/401 gövdesinin reCAPTCHA reddine ait olduğunu gösteren **özgü**
#: ifadeler. Genel "forbidden"/"bot"/"verification" kelimeleri bilinçli olarak
#: dışarıda: onlar herhangi bir yetki hatasında geçebiliyor.
CAPTCHA_MARKERS: tuple[str, ...] = (
    "recaptcha",
    "g-recaptcha-response",
    "captcha verification failed",
    "captcha token",
    "captcha_rejected",
    "invalid captcha",
    "captcha required",
)
#: Gövdede captcha izi yokken oturum sorununa işaret eden kodlar.
AUTH_STATUS = frozenset({401})
#: İstemci sürümünün eskidiğini bildiren durum kodları (deployment/forced update).
REFRESH_STATUS = frozenset({409, 426})
#: Gövdede "istemciyi yenile" anlamına gelen işaretler. Genel sözler ("update",
#: "deployment") bilinçli olarak dışarıda: model çıktısında da geçebilirler.
REFRESH_MARKERS = (
    "refresh_required",
    "forced_update",
    "force_update",
    "client_outdated",
    "outdated_client",
    "deployment_disabled",
    "stale_client",
)
#: Gövdede oturum kimliği / istemci parmak izi alanı olarak geçen kısa anahtar.
#: Canlı cURL'lerde upstream'in gönderdiği stabil bir değer; wrapper şimdilik
#: göndermiyor ve istekler çalışıyor. Bir 4xx gövdesinde geçmeye başlarsa bu
#: yeni bir zorunlu alan sınıfı demektir.
CX_FLAG_PATTERN = re.compile(r'"cx"|\bcx\b|_cx_', re.IGNORECASE)

#: Gövdede oturum/token sorununa işaret eden anahtar kelimeler.
AUTH_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "invalid token",
    "invalid_token",
    "expired",
    "jwt",
    "access token",
    "access_token",
    "not signed in",
    "login",
    "session",
    # Canlı gözlem: oturum düştüğünde upstream 401 + {"message":"User not found"}
    # döndürüyor. Bu metin eski listede yoktu, bu yüzden hata "upstream_failure"
    # gibi jenerik görünüyordu.
    "user not found",
    "user_not_found",
    "no such user",
    "user does not exist",
    "unknown user",
    "account not found",
    "invalid user",
)


def _is_availability_failure(error: Exception | None) -> bool:
    """Hata gerçekten "upstream erişilemez" sınıfında mı?

    Devre kesici yalnızca erişilebilirliği izler. Kota (429), kimlik (401/403),
    captcha reddi ve istemci sürümü (409) bizim tarafımızdaki ya da hesap
    bazındaki sorunlardır; bunları saymak sağlıklı hesapları da bloklar.
    """
    if isinstance(error, (UpstreamTimeout, UpstreamNetworkError)):
        return True
    if isinstance(error, UpstreamHTTPError):
        status = error.status_code or 0
        return status == 429 or status >= 500
    return False


class UpstreamClient:
    """Hedef servisle tüm HTTP iletişimini yöneten tekil istemci."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.breaker = CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            reset_timeout=settings.breaker_reset_timeout,
            enabled=settings.breaker_enabled,
        )
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_upstream))
        self._client: httpx.AsyncClient | None = None
        # Cookie jar ve bağlantı havuzu hesaplar arasında paylaşılmaz. Ortak jar,
        # bir hesabın Set-Cookie cevabını başka hesabın isteğine sızdırıyordu.
        self._account_clients: dict[int, httpx.AsyncClient] = {}
        # Kimlik tazelemede eski client'ı hemen kapatmak, üzerindeki canlı SSE
        # akışını kesebilir. Kullanım sayacı, eski client'ı yeni isteklere
        # kapatırken mevcut isteklerin güvenle bitmesini sağlar.
        self._client_use_counts: dict[int, int] = {}
        self._retiring_clients: set[httpx.AsyncClient] = set()
        # Set-Cookie ile dönen kimlikleri Free Render env'ine yazan tekil işçi.
        # İlk token gecikmesini uzatmamak için cevap akışını bekletmez; ardışık
        # değişiklikleri birleştirir ve eski/yeni yazımların yarışmasını önler.
        self._render_sync_pending = False
        self._render_sync_task: asyncio.Task[None] | None = None
        # Sürüm damgası (x-deployment-id / x-arena-web-deployment) kanaryası.
        self.deployment = DeploymentIdResolver(settings)

    # ----------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        if self._client is not None:
            return
        self._client = self._build_client(self.settings)
        logger.info("upstream_client_started", base_url=self.settings.base_url)
        self._log_referer_hint()

    def _build_client(self, settings: Settings) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=settings.connect_timeout,
            read=settings.read_timeout,
            write=settings.write_timeout,
            pool=settings.pool_timeout,
        )
        limits = httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=settings.max_keepalive_connections,
            keepalive_expiry=30.0,
        )

        def create(http2: bool) -> httpx.AsyncClient:
            return httpx.AsyncClient(
                http2=http2,
                timeout=timeout,
                limits=limits,
                # Stream POST'unda yönlendirme takip edilmez (send() override).
                # 301/302 POST→GET'e dönüşüp gövdeyi düşürür ve sahte 404 üretir.
                follow_redirects=True,
                verify=settings.upstream_verify_tls,
                trust_env=False,
                proxy=settings.upstream_proxy,
            )

        try:
            client = create(settings.upstream_http2)
        except ImportError:  # h2 kurulu değilse HTTP/1.1'e düş
            logger.warning("http2_unavailable_fallback_http11")
            client = create(False)

        # Başlangıç cookie'si hesabın kendi jar'ına yazılır. İsteklere ayrıca ham
        # Cookie başlığı eklemek httpx jar birleştirmesini bastırır ve warm-up /
        # refresh sonrası gelen Set-Cookie değerlerinin kullanılmasını engeller.
        # Çerez alanı yalnızca ana makine adıyla yazılmalı: httpx alanı porta dahil
        # edilmiş hâliyle eşleştirmez, bu yüzden `TARGET_DOMAIN=host:8080` gibi bir
        # değerde çerez sessizce hiç gönderilmezdi.
        cookie_host = settings.target_domain.split(":", 1)[0]
        for name, value in parse_cookie_header(settings.upstream_cookie).items():
            client.cookies.set(name, value, domain=cookie_host, path="/")
        return client

    async def shutdown(self) -> None:
        sync_task = self._render_sync_task
        self._render_sync_task = None
        self._render_sync_pending = False
        if sync_task is not None and not sync_task.done():
            sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await sync_task
        clients = list(self._account_clients.values())
        clients.extend(self._retiring_clients)
        if self._client is not None:
            clients.append(self._client)
        self._account_clients.clear()
        self._retiring_clients.clear()
        self._client_use_counts.clear()
        self._client = None
        # Aynı client hem aktif hem emekli listede bulunmasın diye kimlikle
        # tekilleştir; kapanış sırasında bütün bağlantılar serbest bırakılır.
        unique_clients = {id(client): client for client in clients}
        for client in unique_clients.values():
            await client.aclose()
        if unique_clients:
            logger.info("upstream_client_closed", count=len(unique_clients))

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("UpstreamClient is not started. Call startup() first.")
        return self._client

    def _client_for(self, account: Account | None) -> httpx.AsyncClient:
        """Hesaba özel client/cookie jar döndürür; ilk çağrıda tembel kurulur."""
        if account is None:
            return self.client
        # startup() kontrolü hem global client'ın hazır olmasını sağlar hem de
        # kapanmış client'a yeni hesap client'ı eklenmesini engeller.
        if self._client is None:
            raise RuntimeError("UpstreamClient is not started. Call startup() first.")
        client = self._account_clients.get(account.slot)
        if client is None:
            client = self._build_client(account.effective_settings(self.settings))
            self._account_clients[account.slot] = client
            logger.debug("upstream_account_client_started", account=account.label)
        return client

    def _retain_client(self, client: httpx.AsyncClient) -> None:
        """Client üzerinde bir işlem başladığını işaretle (aynı event-loop)."""
        key = id(client)
        self._client_use_counts[key] = self._client_use_counts.get(key, 0) + 1

    async def _release_client(self, client: httpx.AsyncClient) -> None:
        """İşlemi bitir; emekli client'ın son kullanıcısıysa bağlantıyı kapat."""
        key = id(client)
        remaining = max(0, self._client_use_counts.get(key, 1) - 1)
        if remaining:
            self._client_use_counts[key] = remaining
            return
        self._client_use_counts.pop(key, None)
        if client not in self._retiring_clients:
            return
        self._retiring_clients.discard(client)
        try:
            await client.aclose()
        except Exception as exc:  # pragma: no cover - kapanış isteği bozmamalı
            logger.debug("upstream_retired_client_close_failed", error_type=type(exc).__name__)

    async def _retire_client(self, client: httpx.AsyncClient) -> None:
        """Client'ı yeni isteklerden ayır; aktif işlem varsa kapanışı ertele."""
        if self._client_use_counts.get(id(client), 0) > 0:
            self._retiring_clients.add(client)
            return
        await client.aclose()

    async def reset_account_clients(self) -> int:
        """Hesap client'larını yeniler; devam eden akışları kapatmadan.

        Yeni kimlik bilgileri uygulandığında eski jar'daki çerezlerin yeni
        isteklere karışmaması için hesap eşlemesi hemen sıfırlanır. Üzerinde
        istek bulunan eski client ise yalnızca son işi bitince kapanır.
        """
        clients = list(self._account_clients.values())
        self._account_clients.clear()
        for client in clients:
            await self._retire_client(client)
        if self._client is not None:
            self._client.cookies.clear()
            cookie_host = self.settings.target_domain.split(":", 1)[0]
            for name, value in parse_cookie_header(self.settings.upstream_cookie).items():
                self._client.cookies.set(name, value, domain=cookie_host, path="/")
        self.deployment.reset()
        return len(clients)

    # ------------------------------------------------------------- helpers
    def _log_referer_hint(self) -> None:
        """Sohbet sayfası yolunun gerçek isteklerle uyumlu olduğunu görünür kıl.

        Canlı cURL'lerde `referer` her zaman `<domain>/text/direct` biçiminde
        geliyor; wrapper'ın eski varsayılanı `/c/{chat_id}` idi. `UPSTREAM_CURL`
        varsa yol oradan okunur, bu yüzden uyarı yalnızca cURL yokken üretilir.
        """
        path = (self.settings.upstream_referer_path or "").strip()
        if path and path != "/c/{chat_id}":
            logger.info("upstream_referer_path", referer_path=path)
            return
        if self.settings.upstream_curl.strip():
            # cURL gövdesinden gelen yol referer_path'e yazılmadıysa ufak not.
            logger.info(
                "upstream_referer_path_from_curl",
                referer_path=path or None,
            )
            return
        logger.warning(
            "upstream_referer_path_default",
            referer_path=path or None,
            hint=(
                "UPSTREAM_REFERER_PATH ayarlanmamış: varsayılan /c/{chat_id} "
                "kullanılıyor. Canlı isteklerde referer genellikle /text/direct "
                "olduğu için UPSTREAM_REFERER_PATH=/text/direct önerilir."
            ),
        )

    @staticmethod
    def _strip_deployment_headers(headers: dict[str, str]) -> dict[str, str] | None:
        """Deployment başlıklarını çıkarır; çıkarılacak başlık yoksa None."""
        stripped = {
            name: value
            for name, value in headers.items()
            if name.lower() not in DEPLOYMENT_HEADERS
        }
        return stripped if len(stripped) != len(headers) else None

    @staticmethod
    def _cx_flag(status: int, body: str) -> bool:
        """4xx gövdesi upstream'in `cx` alanına atıfta bulunuyor mu?"""
        if not (400 <= status < 500) or not body:
            return False
        return bool(CX_FLAG_PATTERN.search(body))

    @staticmethod
    def _raw_cookie_header_required(settings: Settings) -> bool:
        """Standart dışı/opak eski COOKIE değerlerini geriye uyumlu taşı.

        `ad=değer` çerezleri jar'a yüklenir ve dinamik Set-Cookie ile birleşir.
        Eşittir işareti olmayan ham değerler jar'da temsil edilemez; bunlar eski
        davranıştaki gibi doğrudan Cookie başlığı olarak gönderilir.
        """
        return bool(settings.upstream_cookie.strip()) and not parse_cookie_header(
            settings.upstream_cookie
        )

    def _schedule_render_env_sync(self) -> None:
        """Değişen ana hesap kimliğini Free Render'a tekil işçiyle kaydet."""
        if not (
            self.settings.render_api_key.strip()
            and self.settings.render_service_id.strip()
        ):
            return
        self._render_sync_pending = True
        if self._render_sync_task is not None and not self._render_sync_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - yalnız savunmacı/test çağrısı
            return
        self._render_sync_task = loop.create_task(
            self._run_render_env_sync(), name="upstream-render-credential-sync"
        )

    async def _run_render_env_sync(self) -> None:
        """Ardışık değişiklikleri sırayla ve en güncel ayarlarla senkronize et.

        Başarısız yazım **tekrar denenir**: Free Render'da disk kalıcı değildir,
        bu yüzden dönen oturumun env'e yazılamaması restart'ta oturumun
        kaybolması demektir. Bir sonraki token rotasyonuna (saatler sonra)
        bırakmak yerine artan aralıkla yeniden denenir; yeni bir Set-Cookie
        bekleyen bayrağı zaten tazeler ve tekrar deneme en güncel ayarları okur.
        """
        from app.upstream.state_store import sync_render_env

        delay = _RENDER_SYNC_RETRY_INITIAL_SECONDS
        while self._render_sync_pending:
            self._render_sync_pending = False
            ok = False
            try:
                ok = await sync_render_env(self.settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - savunmacı
                logger.warning(
                    "render_env_sync_worker_failed", error_type=type(exc).__name__
                )
            if ok:
                delay = _RENDER_SYNC_RETRY_INITIAL_SECONDS
                continue
            self._render_sync_pending = True
            logger.warning(
                "render_env_sync_retry_scheduled", delay_seconds=round(delay, 1)
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RENDER_SYNC_RETRY_MAX_SECONDS)

    def _ingest_set_cookie(
        self,
        response: httpx.Response,
        client: httpx.AsyncClient,
        account: Account | None,
    ) -> None:
        """Sunucudan gelen `Set-Cookie` başlıklarını ayarlara ve durum deposuna yansıtır."""
        if not response.headers.get("set-cookie"):
            return
        slot = account.slot if account is not None else 1
        cookie_attr = "upstream_cookie" if slot <= 1 else f"upstream_cookie_{slot}"
        old_cookie = (
            account.cookie
            if account is not None
            else str(getattr(self.settings, cookie_attr, "") or "")
        ).strip()
        if not old_cookie:
            return
        jar_cookies = {c.name: c.value for c in client.cookies.jar if c.value}
        if not jar_cookies:
            return
        merged = parse_cookie_header(old_cookie)
        merged.update(jar_cookies)
        new_cookie = "; ".join(f"{k}={v}" for k, v in merged.items())
        if new_cookie == old_cookie:
            return

        if account is not None:
            account.cookie = new_cookie
        setattr(self.settings, cookie_attr, new_cookie)

        from app.upstream.auth import extract_access_token, extract_refresh_token
        from app.upstream.state_store import save_runtime_state

        cookie_names = (
            account.token_cookie_names
            if account is not None
            else self.settings.upstream_token_cookie_names
        )
        old_access = extract_access_token(old_cookie, cookie_names=cookie_names)
        new_access = extract_access_token(new_cookie, cookie_names=cookie_names)
        if new_access and new_access != old_access:
            if (
                account is not None
                and account.access_token
                and account.access_token == old_access
            ):
                account.access_token = new_access
            access_attr = (
                "upstream_access_token"
                if slot <= 1
                else f"upstream_access_token_{slot}"
            )
            current_access = str(getattr(self.settings, access_attr, "") or "")
            if current_access and current_access == old_access:
                setattr(self.settings, access_attr, new_access)

        if slot <= 1:
            new_refresh = extract_refresh_token(new_cookie, cookie_names=cookie_names)
            if new_refresh and new_refresh != self.settings.upstream_refresh_token:
                self.settings.upstream_refresh_token = new_refresh
            save_runtime_state(self.settings, source="upstream_set_cookie")
            self._schedule_render_env_sync()

    async def _retry_with_fresh_stamp(
        self,
        *,
        client: httpx.AsyncClient,
        request_settings: Settings,
        sent_value: str | None,
        status: int,
        marker: str,
    ) -> bool:
        """409 sonrası sürüm damgasını zorla tazeler.

        Yalnızca gerçekten **farklı** bir damga bulursa True döner; aynı değerle
        tekrar denemek boşuna istek olurdu. Kanarya kapalıysa ya da damga
        `UPSTREAM_CURL`'de hiç yoksa False döner ve çağıran mevcut net hatayı
        fırlatır (davranış eskisiyle aynı).
        """
        if not request_settings.deployment_id_auto_retry:
            logger.info("deployment_stamp_retry_disabled", status=status)
            return False
        if not self.deployment.active:
            logger.error(
                "deployment_stamp_unmanaged",
                status=status,
                marker=marker,
                hint=(
                    "409 alındı ama takip edilen bir deployment başlığı yok. "
                    "UPSTREAM_CURL'ü tazelemek gerekiyor."
                ),
            )
            return False

        reading = await self.deployment.refresh(
            client, request_settings.base_url, force=True
        )
        if reading.value and reading.value != sent_value:
            logger.warning(
                "deployment_stamp_refreshed",
                status=status,
                marker=marker,
                previous=sent_value,
                current=reading.value,
                source=reading.source,
                hint="İstek taze sürüm damgasıyla bir kez tekrar ediliyor.",
            )
            return True

        logger.error(
            "deployment_stamp_unchanged",
            status=status,
            marker=marker,
            deployment_id=reading.value,
            source=reading.source,
            hint=(
                "Upstream 409 verdi ama okunan sürüm damgası değişmedi. Oturum "
                "anlık görüntüsü (cookie/token) tazelenmeli."
            ),
        )
        return False

    async def _refresh_session_cookie(
        self, chat_id: str, account: Account | None = None
    ) -> None:
        """409'dan sonra oturum çerezini tazelemek için tek bir sayfa GET'i.

        Bu istek başarısız çağrıyı kurtarmaz; yalnızca cookie jar'ı güncelleyip
        sonraki isteğin şansını artırır. Hata yutulur (kritik değil).
        """
        try:
            await self.warmup(chat_id, account)
        except Exception:  # pragma: no cover - warmup zaten hata yutmuyor
            logger.debug("upstream_session_refresh_failed")

    async def warmup(self, chat_id: str, account: Account | None = None) -> None:
        """Sohbet sayfasını seçilen hesabın kimliği ve cookie jar'ı ile ısıtır."""
        request_settings = (
            account.effective_settings(self.settings) if account else self.settings
        )
        url = request_settings.referer_url(chat_id)
        client = self._client_for(account)
        self._retain_client(client)
        try:
            response = await client.get(
                url,
                headers=build_page_headers(
                    request_settings,
                    include_cookie=self._raw_cookie_header_required(request_settings),
                ),
                timeout=httpx.Timeout(request_settings.connect_timeout + 10.0),
            )
            self._ingest_set_cookie(response, client, account)
            # Aynı yanıtı damga için de değerlendir (ek istek yok).
            self.deployment.ingest(response, request_settings.base_url)
            logger.debug(
                "upstream_warmup",
                status=response.status_code,
                chat_id=chat_id,
                account=account.label if account else None,
            )
        except Exception as exc:  # pragma: no cover - ısıtma kritik değil
            logger.debug("upstream_warmup_failed", error=str(exc))
        finally:
            await self._release_client(client)

    #: Cloudflare'in bot/oturum duvarı imzaları. Bunlar reCAPTCHA reddi
    #: **değildir**: `cf_clearance`/`__cf_bm` eskimişse hedef HTML hata sayfası
    #: döner. Bu ayrım yapılmadığında operatör yanlış yöne (CAPTCHA token'ı)
    #: bakıyordu.
    _CLOUDFLARE_MARKERS: tuple[str, ...] = (
        "just a moment",
        "cf-browser-verification",
        "cf_chl_",
        "cloudflare",
        "attention required",
        "enable javascript and cookies to continue",
    )

    @classmethod
    def _is_cloudflare_challenge(cls, status: int, body: str) -> bool:
        """403/503 gövdesi Cloudflare bot/oturum duvarı mı?"""
        if status not in {403, 503} or not body:
            return False
        lowered = body.lower()
        return any(marker in lowered for marker in cls._CLOUDFLARE_MARKERS)

    @staticmethod
    def _is_captcha_body(status: int, body: str) -> bool:
        """403/401 yanıtı **gerçekten** reCAPTCHA reddi mi?

        Yalnızca "forbidden" veya "bot" kelimesini aramak yanlış teşhis
        üretiyordu: hedefin herhangi bir 403 gövdesi bu kelimeleri içerebilir
        ve o zaman operatör CAPTCHA token'ına bakmaya yönlendirilir. Bu yüzden
        reCAPTCHA'ya özgü ifadeler aranır.
        """
        if status not in CAPTCHA_STATUS or not body:
            return False
        # Cloudflare sayfası "forbidden"/"bot" kelimelerini içerir; onu
        # reCAPTCHA reddiyle karıştırmamak için önce onu ayır.
        if UpstreamClient._is_cloudflare_challenge(status, body):
            return False
        lowered = body.lower()
        return any(marker in lowered for marker in CAPTCHA_MARKERS)

    def _is_quota_body(self, status: int, body: str) -> str | None:
        """Gövde upstream'in hesap kısıtlama mesajını taşıyorsa işareti döndürür.

        Hedef servis kotayı özel bir durum koduyla değil, metinle bildiriyor
        (örn. "upstream limit reached"). Bu yüzden kod değil gövde esas alınır ve
        her 4xx/5xx gövdesine bakılır.
        """
        if status < 400 or not body:
            return None
        markers = normalize_markers(self.settings.upstream_limit_markers)
        return find_quota_marker(body, markers)

    @staticmethod
    def _refresh_marker(status: int, body: str) -> str | None:
        """Upstream istemciyi eski bulduysa eşleşen işareti döndürür.

        Bu sınıf hata, "upstream bozuk" demek değildir: sunucu yeni bir sürüm
        yayınlamış, gönderdiğimiz `x-...-deployment`/`x-deployment-id` başlığı
        ya da oturum anlık görüntüsü eskimiş demektir. Yeniden denemek faydasız,
        devre kesiciyi açmak ise zararlıdır (sağlıklı istekleri de engeller).
        """
        lowered = body.lower()
        for marker in REFRESH_MARKERS:
            if marker in lowered:
                return marker
        if status in REFRESH_STATUS and not lowered.strip():
            return f"http_{status}"
        return None

    @staticmethod
    def _is_user_missing(status: int, body: str) -> bool:
        """Upstream "bu kullanıcı yok" diyor mu? (401/403, oturum ölümü)

        Gövde boşsa da kimlik reddi sayılır: 401/403 + boş gövde bizim
        tarafımızdaki oturum sorunudur.
        """
        if status not in CAPTCHA_STATUS:
            return False
        lowered = (body or "").lower()
        if not lowered.strip():
            return True
        return any(
            marker in lowered
            for marker in (
                "user not found",
                "user_not_found",
                "no such user",
                "user does not exist",
                "unknown user",
                "account not found",
                "invalid user",
            )
        )

    @staticmethod
    def _is_auth_body(status: int, body: str) -> bool:
        """401 yanıtının oturum/`access_token` reddi olup olmadığını ayırt eder.

        Gövde boş bir 401 de kimlik sorunu sayılır: hedef servis `Authorization`
        başlığını zorunlu kıldığı için en olası neden token'ın eksik/süresi dolmuş
        olmasıdır.
        """
        if status not in AUTH_STATUS:
            return False
        lowered = body.lower()
        if not lowered.strip():
            return True
        return any(marker in lowered for marker in AUTH_MARKERS)

    # ------------------------------------------------------------- request
    @asynccontextmanager
    async def _total_deadline(self, settings: Settings, url: str) -> AsyncIterator[None]:
        """Retry/backoff dahil upstream çağrısına tek bir toplam süre sınırı koy."""
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - async bağlamda her zaman bir task vardır
            yield
            return
        expired = False

        def expire() -> None:
            nonlocal expired
            expired = True
            task.cancel()

        # asyncio.timeout yalnız Python 3.11+; proje 3.10'u da destekler.
        handle = asyncio.get_running_loop().call_later(
            max(0.001, settings.total_request_timeout), expire
        )
        try:
            yield
        except asyncio.CancelledError as exc:
            if not expired:
                raise
            raise UpstreamTimeout(
                f"Upstream exceeded total request timeout ({settings.total_request_timeout}s).",
                url=url,
            ) from exc
        finally:
            handle.cancel()

    @asynccontextmanager
    async def stream_completion(
        self, chat_id: str, payload: dict[str, object], account: Account | None = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """İsteği client yaşam süresi boyunca koru; reset akışı kesmesin."""
        client = self._client_for(account)
        self._retain_client(client)
        try:
            async with self._stream_completion_with_client(
                chat_id, payload, account=account, client=client
            ) as chunks:
                yield chunks
        finally:
            await self._release_client(client)

    @asynccontextmanager
    async def _stream_completion_with_client(
        self,
        chat_id: str,
        payload: dict[str, object],
        *,
        account: Account | None,
        client: httpx.AsyncClient,
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Upstream'e istek atıp bayt akışı döndürür (retry + breaker dahil).

        `--data-raw` ile birebir aynı gövde `text/plain` olarak gönderilir.
        `account` verilirse o hesabın kimlik bilgileri ve başlıkları kullanılır.
        """
        if not await self.breaker.allows():
            metrics.inc("apiwrapper_upstream_errors_total", labels={"reason": "circuit_open"})
            raise CircuitOpen(
                "Upstream circuit breaker is open.",
                status_code=503,
            )

        request_settings = (
            account.effective_settings(self.settings) if account else self.settings
        )
        url = request_settings.stream_url(chat_id)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        # Sürüm damgasını tazele (önbellek sıcaksa ağa çıkmaz). Okunamazsa
        # `UPSTREAM_CURL`'deki donmuş değer kullanılır: kanarya isteği bozmaz.
        reading = await self.deployment.refresh(client, request_settings.base_url)
        deployment_id = reading.value
        headers = build_stream_headers(
            request_settings,
            chat_id,
            include_cookie=self._raw_cookie_header_required(request_settings),
            deployment_id=deployment_id,
        )

        last_error: Exception | None = None
        attempts = max(1, self.settings.retry_max_attempts)
        attempt = 0
        # 409 `refresh_required` için tek seferlik tazeleme hakları.
        stamp_retry_used = False
        stamp_strip_used = False
        # 409 sonrası damga çıkarılınca oturum da reddedildiyse ısıtma faydasız.
        session_dead = False

        async with self._total_deadline(request_settings, url), self._semaphore:
            while attempt < attempts:
                metrics.inc("apiwrapper_upstream_requests_total")
                request = client.build_request(
                    "POST", url, headers=headers, content=body
                )
                response: httpx.Response | None = None
                try:
                    # POST gövdesini 301/302 ile GET'e çevirmemek için yönlendirme kapalı.
                    response = await client.send(
                        request, stream=True, follow_redirects=False
                    )
                except httpx.TimeoutException as exc:
                    last_error = UpstreamTimeout(f"Upstream timed out: {exc}", url=url)
                except httpx.HTTPError as exc:
                    last_error = UpstreamNetworkError(
                        f"Upstream network error: {exc}", url=url
                    )
                else:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location", "")
                        error_body = await self._read_error_body(response)
                        await response.aclose()
                        metrics.inc(
                            "apiwrapper_upstream_errors_total",
                            labels={"status": str(status)},
                        )
                        logger.warning(
                            "upstream_redirect",
                            status=status,
                            url=url,
                            location=location or None,
                        )
                        redirect_error = UpstreamHTTPError(
                            f"Upstream redirected HTTP {status} from {url} "
                            f"to {location or '(no Location header)'}.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )
                        # Hedef yol istemciye yalnızca path olarak gösterilir;
                        # hata sınıfı bunu taşımıyorsa öznitelikle ekliyoruz.
                        redirect_error.location = location
                        raise redirect_error

                    if status < 400:
                        await self.breaker.record_success()
                        if response.headers.get("set-cookie"):
                            self._ingest_set_cookie(response, client, account)
                            logger.info(
                                "upstream_set_cookie",
                                status=status,
                                count=len(response.headers.get_list("set-cookie")),
                                hint=(
                                    "Upstream oturum çerezini tazeledi; yeni değer "
                                    "belleğe ve durum deposuna kaydedildi."
                                ),
                            )
                        try:
                            yield response.aiter_bytes()
                        finally:
                            await response.aclose()
                        return

                    error_body = await self._read_error_body(response)
                    await response.aclose()
                    metrics.inc(
                        "apiwrapper_upstream_errors_total", labels={"status": str(status)}
                    )
                    logger.warning(
                        "upstream_http_error",
                        status=status,
                        url=url,
                        body=error_body[:300],
                    )
                    if stamp_strip_used and self._is_user_missing(status, error_body):
                        # Kritik ayrım: 409 "refresh_required" damga sorunu gibi
                        # görünür ama damga çıkarılınca oturum da reddediliyorsa
                        # asıl arıza oturumun ölmüş olmasıdır. Yeni cURL şart.
                        session_dead = True
                        metrics.inc("apiwrapper_upstream_session_dead_total")
                        logger.error(
                            "upstream_session_dead",
                            status=status,
                            account=account.label if account else None,
                            body=error_body[:200],
                            hint=(
                                "Damga tazelenemedi ve damga çıkarılınca upstream "
                                "oturumu da reddetti (401/403 'user not found'). "
                                "Sorun sürüm damgası değil, oturumun kendisi: "
                                "tarayıcıdan taze cURL alıp UPSTREAM_CURL'ü "
                                "güncelleyin ve servisi yeniden deploy edin."
                            ),
                        )

                    if self._cx_flag(status, error_body):
                        metrics.inc(
                            "apiwrapper_upstream_cx_flags_total",
                            labels={"status": str(status)},
                        )
                        logger.warning(
                            "upstream_cx_flag",
                            status=status,
                            hint=(
                                "Upstream gövdesi `cx` alanına atıfta bulunuyor. Bu değer "
                                "upstream tarafında oturum bazlı; canlı cURL'den "
                                "UPSTREAM_EXTRA_HEADERS/cx olarak taşınması gerekebilir."
                            ),
                        )

                    quota_marker = self._is_quota_body(status, error_body)
                    if quota_marker is not None:
                        retry_after = parse_retry_after(
                            response.headers.get("retry-after")
                        )
                        # Kota bir erişilebilirlik arızası değildir: devre kesiciyi
                        # açmak sağlıklı hesapları da bloke ederdi.
                        metrics.inc(
                            "apiwrapper_upstream_quota_errors_total",
                            labels={"source": "http_body", "status": str(status)},
                        )
                        logger.warning(
                            "upstream_quota_exceeded",
                            status=status,
                            url=url,
                            marker=quota_marker,
                            retry_after=retry_after,
                            hint=(
                                "Upstream limited this account; retrying would only "
                                "extend the lockout. Wait for the window to expire "
                                "(or switch account once a credential pool exists)."
                            ),
                        )
                        raise UpstreamQuotaExceeded(
                            quota_error_message(quota_marker, "HTTP response body",
                                                error_body),
                            status_code=status,
                            body=error_body,
                            url=url,
                            retry_after=retry_after,
                            marker=quota_marker,
                        )

                    refresh_marker = self._refresh_marker(status, error_body)
                    if refresh_marker is not None and not stamp_retry_used:
                        stamp_retry_used = True
                        if await self._retry_with_fresh_stamp(
                            client=client,
                            request_settings=request_settings,
                            sent_value=deployment_id,
                            status=status,
                            marker=refresh_marker,
                        ):
                            refreshed = self.deployment.cached(request_settings.base_url)
                            headers = build_stream_headers(
                                request_settings,
                                chat_id,
                                include_cookie=self._raw_cookie_header_required(
                                    request_settings
                                ),
                                deployment_id=refreshed,
                            )
                            deployment_id = refreshed
                            # Damga retry'ı normal deneme bütçesinden yemez.
                            continue

                    if (
                        refresh_marker is not None
                        and not stamp_strip_used
                        and request_settings.deployment_stamp_strip_on_conflict
                    ):
                        # İkinci kademe: yeni damga bulunamadıysa eskisini hiç
                        # gönderme. Skew koruması devre dışı kalan istek genelde
                        # kabul edilir; bu kademe hiçbir değer bilmeyi gerektirmez.
                        stripped = self._strip_deployment_headers(headers)
                        if stripped is not None:
                            stamp_strip_used = True
                            headers = stripped
                            deployment_id = None
                            metrics.inc("apiwrapper_deployment_stamp_strip_total")
                            logger.warning(
                                "deployment_stamp_stripped",
                                status=status,
                                marker=refresh_marker,
                                hint=(
                                    "Sürüm damgası tazelenemedi; istek deployment "
                                    "başlıkları gönderilmeden bir kez daha denenecek."
                                ),
                            )
                            continue

                    if refresh_marker is not None:
                        # Retry YOK, devre kesiciye kayıt YOK: sorun bizim
                        # sakladığımız istemci anlık görüntüsünde.
                        metrics.inc(
                            "apiwrapper_upstream_errors_total",
                            labels={"status": str(status), "reason": "refresh_required"},
                        )
                        logger.error(
                            "upstream_refresh_required",
                            status=status,
                            url=url,
                            marker=refresh_marker,
                            body=error_body[:200],
                            hint=(
                                (
                                    "Sürüm damgası otomatik tazelendi ama upstream yine 409 "
                                    "verdi. Oturum anlık görüntüsü eskimiş olabilir: "
                                    "tarayıcıdan tamamlanmış isteği yeni cURL olarak alıp "
                                    "UPSTREAM_CURL'ü güncelleyin (scripts/curl_to_env.py)."
                                )
                                if stamp_retry_used
                                else (
                                    "Upstream istemci sürümünü eski buldu. Kanarya pasif ya "
                                    "da damga takip edilmiyor: tamamlanmış isteği cURL "
                                    "olarak alıp UPSTREAM_CURL'ü güncelleyin "
                                    "(scripts/curl_to_env.py)."
                                )
                            ),
                        )
                        # Son kademe: damga değişmediyse oturum çerezini tazelemeyi
                        # dene (tek GET). Bu isteği kurtarmaz ama bir sonraki
                        # isteğin taze cookie ile gitmesini sağlar.
                        if not session_dead:
                            await self._refresh_session_cookie(chat_id, account)
                        raise UpstreamRefreshRequired(
                            "Upstream rejected the captured client snapshot as outdated.",
                            status_code=status,
                            body=error_body,
                            url=url,
                            marker=refresh_marker,
                        )

                    if (
                        self._is_auth_body(status, error_body) or session_dead
                    ) and not self._is_captcha_body(
                        status, error_body
                    ):
                        raise UpstreamAuthRejected(
                            (
                                "Upstream rejected the session (401/403 after a stale "
                                "deployment 409); the captured cookie/token is no "
                                "longer valid."
                            )
                            if session_dead
                            else "Upstream rejected the session credentials.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    if self._is_cloudflare_challenge(status, error_body):
                        # `cf_clearance`/`__cf_bm` eskimiş demektir; CAPTCHA
                        # token'ı suçlu değil.
                        raise UpstreamAuthRejected(
                            "Upstream returned a Cloudflare bot/clearance challenge; "
                            "the captured Cloudflare cookies (cf_clearance, __cf_bm) "
                            "have expired. Capture a fresh browser cURL.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    if self._is_captcha_body(status, error_body):
                        # Operatör tam olarak hangi gövdenin geldiğini
                        # görsün: "captcha_rejected" diye bildiğimiz şey
                        # bazen cx alanı, deployment skew veya path
                        # kaynaklı olabiliyor; gövdenin ilk 400 karakteri
                        # bunu ayırt etmeye yeter.
                        logger.warning(
                            "upstream_recaptcha_rejected_body",
                            status=status,
                            # URL'yi operatöre gösterirken host'u maskeleme
                            # zaten log sistemi yapıyor; burada yol kısmı
                            # yeterli (full path gizli bilgi taşımaz).
                            path=str(url).split("://", 1)[-1].split("/", 1)[-1][:200]
                            if "://" in str(url)
                            else str(url)[:200],
                            body_preview=(error_body or "")[:400],
                        )
                        raise RecaptchaRejected(
                            "Upstream rejected the reCAPTCHA token.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    last_error = UpstreamHTTPError(
                        f"Upstream returned HTTP {status} for {url}.",
                        status_code=status,
                        body=error_body,
                        url=url,
                    )
                    if status not in RETRYABLE_STATUS:
                        raise last_error

                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    attempt += 1
                    if attempt < attempts:
                        await asyncio.sleep(
                            retry_after
                            if retry_after is not None
                            else full_jitter_delay(
                                attempt,
                                self.settings.retry_base_delay,
                                self.settings.retry_max_delay,
                            )
                        )
                        continue

                # Ağ/timeout hatası yolu
                attempt += 1
                if attempt < attempts:
                    logger.warning(
                        "upstream_retry",
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        error=str(last_error),
                    )
                    await asyncio.sleep(
                        full_jitter_delay(
                            attempt,
                            self.settings.retry_base_delay,
                            self.settings.retry_max_delay,
                        )
                    )

            if _is_availability_failure(last_error):
                await self.breaker.record_failure()
            raise last_error or UpstreamNetworkError("Upstream request failed.")

    @staticmethod
    async def _read_error_body(response: httpx.Response, limit: int = 4096) -> str:
        """Hata gövdesini sınırlı biçimde okur."""
        try:
            collected = bytearray()
            async for chunk in response.aiter_bytes():
                collected.extend(chunk)
                if len(collected) >= limit:
                    break
            return bytes(collected[:limit]).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return ""
