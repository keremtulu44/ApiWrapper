"""Telegram ↔ OpenAI-compatible wrapper köprüsü (webhook gerektirmez).

Çalıştırma:
    TELEGRAM_BOT_TOKEN=... WRAPPER_BASE_URL=https://... \
    WRAPPER_API_KEY=... TELEGRAM_ALLOWED_CHAT_IDS=123456 \
    python -m app.telegram.bot

Bot yalnızca metin mesajını tek turluk bir chat/completions isteğine çevirir;
konuşma geçmişi veya veritabanı tutmaz. Token ve wrapper cevabının ham gövdesi
loglanmaz. İlk kurulumda allowlist boş bırakılıp /start ile chat ID öğrenilebilir;
bu modda wrapper'a hiçbir istek gönderilmez.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.diagnostics import format_failure
from app.core.logging import configure_logging, get_logger
from app.services.keepalive import describe_probe, last_probe

logger = get_logger(__name__)

_HELP = (
    "ApiWrapper test botu aktif.\n"
    "Bir metin gönderin; wrapper /v1/chat/completions üzerinden yanıt üretir.\n"
    "Komutlar: /ping (wrapper + son upstream kontrolü), /tani (kimlik teşhisi), "
    "/yenile (sunucu "
    "tarayıcısıyla oturumu tazele), /cookie <çerez> ya da /curl <cURL>, /help.\n"
    "/cookie! <çerez> = kontrolü atlayıp zorla uygula.\n"
    "Her mesaj bağımsızdır; konuşma geçmişi tutulmaz."
)


def parse_allowed_chat_ids(raw: str) -> frozenset[int]:
    """Virgülle ayrılmış Telegram chat ID'lerini doğrular."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            ids.add(int(value))
        except ValueError as exc:
            raise ValueError(
                "TELEGRAM_ALLOWED_CHAT_IDS yalnızca virgülle ayrılmış sayılar içermeli."
            ) from exc
    return frozenset(ids)


class TelegramSettings(BaseSettings):
    """Bot ayarları; bütün secret'lar yalnızca ortamdan okunur."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = ""
    wrapper_base_url: str = "http://127.0.0.1:8000"
    wrapper_api_key: str = ""
    telegram_allowed_chat_ids: str = ""
    #: Arıza bildirimlerinin gideceği sohbet. Boşsa ilk allowlist kaydı kullanılır.
    telegram_alert_chat_id: str = ""
    telegram_model: str = "max"
    telegram_poll_timeout: int = Field(default=30, ge=1, le=50)
    telegram_request_timeout: float = Field(default=180.0, gt=0)
    telegram_max_reply_chars: int = Field(default=12000, ge=1000, le=50000)

    @field_validator("wrapper_base_url")
    @classmethod
    def normalize_wrapper_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("WRAPPER_BASE_URL http:// veya https:// ile başlamalı.")
        return value

    @field_validator("telegram_allowed_chat_ids")
    @classmethod
    def validate_chat_ids(cls, value: str) -> str:
        parse_allowed_chat_ids(value)
        return value

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return parse_allowed_chat_ids(self.telegram_allowed_chat_ids)

    @property
    def alert_chat_id(self) -> int | None:
        """Arıza bildirimlerinin hedefi; yoksa ``None`` (bildirim kapalı).

        Ayrı bir değişken zorunlu değildir: botu zaten kullandığınız sohbet
        (allowlist'in ilk kaydı) varsayılan hedeftir. Böylece ek ayar
        yapılmadan da anlamlı uyarı alınır.
        """
        raw = self.telegram_alert_chat_id.replace(";", ",").split(",")[0].strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                logger.warning("telegram_alert_chat_id_invalid")
                return None
        allowed = sorted(self.allowed_chat_ids)
        return allowed[0] if allowed else None

    def missing_secrets(self) -> list[str]:
        missing = []
        if not self.telegram_bot_token.strip():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.wrapper_api_key.strip():
            missing.append("WRAPPER_API_KEY")
        return missing


def split_telegram_text(text: str, limit: int = 3500, max_chars: int = 12000) -> list[str]:
    """Telegram'ın 4096 karakter sınırının altında, gerektiğinde kısaltılmış parçalar."""
    text = text.strip() or "(wrapper boş yanıt döndürdü)"
    if len(text) > max_chars:
        text = text[: max_chars - 24].rstrip() + "\n\n… yanıt kısaltıldı."
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks or ["(wrapper boş yanıt döndürdü)"]


def wrapper_error_code(response: httpx.Response) -> str:
    """Yalnız yapısal OpenAI hata kodunu al; ham cevabı kullanıcıya taşıma."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    code = error.get("code") or error.get("type")
    return str(code)[:100] if code else ""


class TelegramBot:
    """Küçük, sıralı long-polling istemcisi; aynı anda tek wrapper isteği yeterli."""

    def __init__(
        self,
        settings: TelegramSettings,
        telegram: httpx.AsyncClient,
        wrapper: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.telegram = telegram
        self.wrapper = wrapper
        #: Sohbet başına birleştirilen çerez parçaları (uzun JSON 4 KB'a sığmaz).
        self._cookie_buffers: dict[int, list[str]] = {}

    @staticmethod
    def _cookie_complete(text: str) -> bool:
        """Toplanan metin tamamlandı mı?

        JSON dışa aktarımı geçerli JSON olduğunda tamamdır; `ad=değer` başlık
        satırı tek mesajda biter (parçalı gönderimde `/cookie bitir` kullanılır).
        """
        stripped = text.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return False
            return isinstance(parsed, list) and bool(parsed)
        return "=" in stripped

    @property
    def telegram_api_url(self) -> str:
        # Token Telegram Bot API'nin zorunlu path parçasıdır; hiçbir log/event'e
        # bu URL veya token yazılmaz.
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token.strip()}"

    async def send_text(self, chat_id: int, text: str) -> None:
        for chunk in split_telegram_text(
            text, max_chars=self.settings.telegram_max_reply_chars
        ):
            response = await self.telegram.post(
                f"{self.telegram_api_url}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            if response.status_code >= 400:
                logger.warning(
                    "telegram_send_failed",
                    status_code=response.status_code,
                )

    async def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(text, str):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        text = text.strip()
        if not text:
            return

        allowed = self.settings.allowed_chat_ids
        if not allowed:
            # İlk açılışta kim olduğunu öğrenmek güvenli; wrapper kotası kapalı.
            await self.send_text(
                chat_id,
                "Kurulum gerekli: bu sohbetin chat ID'si "
                f"{chat_id}. Bunu TELEGRAM_ALLOWED_CHAT_IDS değişkenine ekleyip "
                "botu yeniden başlatın. Allowlist boşken wrapper'a istek gönderilmez.",
            )
            return
        if chat_id not in allowed:
            logger.warning("telegram_message_ignored", reason="chat_not_allowed")
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await self.send_text(chat_id, _HELP)
            return
        if command == "/ping":
            await self.ping(chat_id)
            return
        if command in {"/tani", "/tanı", "/durum", "/status", "/diagnose"}:
            await self.diagnose(chat_id)
            return
        if command in {"/curl", "/setcurl"}:
            await self.set_upstream_curl(chat_id, text)
            return
        if command in {"/yenile", "/refresh", "/capture"}:
            await self.capture_upstream_session(chat_id)
            return
        if command in {"/cookie", "/cerez"}:
            await self.send_cookie(chat_id, text)
            return
        if command in {"/cookie!", "/cerez!", "/cookie-force", "/cerezforce"}:
            await self.send_cookie(chat_id, text, force=True)
            return
        if text.startswith("/"):
            await self.send_text(chat_id, "Bilinmeyen komut. /help yazın.")
            return
        await self.ask_wrapper(chat_id, text)

    async def ping(self, chat_id: int) -> None:
        """`/ping`: wrapper süreci + son upstream kontrolünü birlikte raporlar.

        `/healthz` upstream'e dokunmaz; süreç ayakta olsa bile oturum ölmüş
        olabilir. Bu yüzden son keepalive sonucu da yazılır: telefondan tek
        bakışta "gerçekten çalışıyor mu" sorusu cevaplanır.
        """
        lines: list[str] = []
        try:
            response = await self.wrapper.get("/healthz")
        except httpx.HTTPError as exc:
            logger.warning("telegram_wrapper_health_failed", error_type=type(exc).__name__)
            lines.append("Wrapper health kontrolüne ulaşılamadı.")
        else:
            lines.append(
                f"Wrapper /healthz: HTTP {response.status_code}"
                + (" (hazır değil)" if response.status_code != 200 else " (çalışıyor)")
            )
        # Süreç ayakta olsa da upstream oturumu ölmüş olabilir.
        lines.append(describe_probe(last_probe()))
        await self.send_text(chat_id, "\n".join(lines))

    async def diagnose(self, chat_id: int) -> None:
        """`/tani`: telefonda tek mesajda "neyim eksik, ne yapayım".

        `/auth` ucunun gizli değer içermeyen çıktısını okunur bir özete çevirir;
        Network/DevTools açmadan oturumun neden çalışmadığını söyler.
        """
        try:
            response = await self.wrapper.get("/v1/admin/auth")
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            await self.send_text(chat_id, "Wrapper'a ulaşılamadı; teşhis yapılamadı.")
            return
        if response.status_code != 200 or not isinstance(payload, dict):
            await self.send_text(
                chat_id,
                f"Teşhis alınamadı (HTTP {response.status_code}). API anahtarını "
                "kontrol edin.",
            )
            return

        lines = ["🔎 Teşhis"]
        names = [str(item) for item in (payload.get("cookie_names_present") or [])]
        shown = ", ".join(names[:8]) + (f" (+{len(names) - 8})" if len(names) > 8 else "")
        lines.append(
            f"Çerez: {payload.get('cookie_count', 0)} ad"
            + (f" → {shown}" if shown else " → **hiç yok**")
        )
        candidates_raw = payload.get("session_cookie_candidates")
        candidates: list[Any] = candidates_raw if isinstance(candidates_raw, list) else []
        session_names = [
            str(item.get("cookie"))
            for item in candidates
            if isinstance(item, dict) and item.get("cookie")
        ]
        lines.append(
            "Oturum adayı çerez: "
            + (", ".join(session_names[:5]) if session_names else "yok")
        )
        token_raw = payload.get("token")
        token: dict[str, Any] = token_raw if isinstance(token_raw, dict) else {}
        cookie_token_raw = payload.get("cookie_token")
        cookie_token: dict[str, Any] = (
            cookie_token_raw if isinstance(cookie_token_raw, dict) else {}
        )
        # Çerezdeki token ile **kullanılan** token ayrı gösterilir: "token yok"
        # cümlesi, çerezde süresi dolmuş token varken yanıltıcı oluyordu.
        if cookie_token.get("found"):
            lines.append("Çerezde token: " + TelegramBot._describe_token(cookie_token))
        else:
            lines.append("Çerezde token: **yok** (değer JWT/JSON olarak okunamadı)")
        if token.get("found"):
            lines.append("Kullanılan token: " + TelegramBot._describe_token(token))
        else:
            lines.append("Kullanılan token: **yok**")
        read_flag = payload.get("extract_from_cookie_enabled")
        lines.append("Çerezden token okuma: " + ("açık" if read_flag else "**kapalı**"))
        refresh = payload.get("refresh_token")
        if isinstance(refresh, dict):
            if refresh.get("found"):
                lines.append(f"Refresh token: var ({refresh.get('length', 0)} karakter)")
            else:
                lines.append("Refresh token: **yok**")
        lines.append(
            "Authorization başlığı: "
            + (
                "gidecek"
                if payload.get("authorization_header_will_be_sent")
                else "**gitmeyecek**"
            )
        )
        probe = describe_probe(last_probe())
        lines.append(f"Son upstream: {probe}")
        lines.append("→ " + self._diagnose_advice(payload, probe))
        await self.send_text(chat_id, "\n".join(lines))

    @staticmethod
    def _describe_token(token: dict[str, Any]) -> str:
        """Token durumunu tek parça metne çevirir (değer asla yazılmaz)."""
        if token.get("expired"):
            return "**süresi dolmuş**" + (" (JWT)" if token.get("is_jwt") else "")
        remaining = token.get("expires_in_seconds")
        if isinstance(remaining, int):
            return f"var (≈{remaining // 60} dk kaldı)"
        return "var (süre bilgisi yok)"

    @staticmethod
    def _diagnose_advice(payload: dict[str, Any], probe: str) -> str:
        """Teşhis çıktısından tek bir sonraki adım seçer."""
        token_raw = payload.get("token")
        token: dict[str, Any] = token_raw if isinstance(token_raw, dict) else {}
        cookie_token_raw = payload.get("cookie_token")
        cookie_token: dict[str, Any] = (
            cookie_token_raw if isinstance(cookie_token_raw, dict) else {}
        )
        refresh_raw = payload.get("refresh_token")
        refresh: dict[str, Any] = refresh_raw if isinstance(refresh_raw, dict) else {}
        count = int(payload.get("cookie_count") or 0)
        candidates = payload.get("session_cookie_candidates") or []
        if cookie_token.get("expired") and refresh.get("found"):
            return (
                "Erişim token'ı süresi dolmuş ama **refresh token duruyor**: sunucu "
                "oturumu kendisi yenileyebilir. Şimdilik tarayıcıda sayfayı açıp "
                "taze çerezi /cookie ile gönderin."
            )
        if cookie_token.get("expired"):
            return (
                "Çerezdeki token süresi dolmuş, refresh token yok: tarayıcıda "
                "yeniden giriş → /cookie <çerez>. /yenile bunu çözmez."
            )
        if not count:
            return (
                "Elde çerez yok. Giriş yapmış tarayıcıda Cookie-Editor → Export → "
                "Header String → /cookie <çerez>."
            )
        if not token.get("found"):
            if candidates:
                return (
                    "Çerez var ama oturum token'ı ayıklanamadı: /yenile deneyin; "
                    "olmazsa tam çerez setini /cookie ile gönderin."
                )
            return (
                "Çerezlerde oturum izi yok (muhtemelen eksik set). Giriş yapmış "
                "tarayıcıdan TÜM çerezleri /cookie ile gönderin."
            )
        if token.get("expired"):
            return (
                "Token süresi dolmuş: tarayıcıda yeniden giriş → /cookie <çerez>. "
                "/yenile ölü oturumu diriltmez."
            )
        if "401" in probe:
            return (
                "Kimlik var ama upstream 401 veriyor: damga/referer eski olabilir → "
                "/yenile; sürerse taze çerez."
            )
        if "409" in probe:
            return "Kimlik sağlam; 409 damga eskimesi → /yenile."
        return "Kimlik görünüşte sağlam. Test için bir mesaj gönderin."

    async def send_cookie(self, chat_id: int, text: str, *, force: bool = False) -> None:
        """Yalnızca çerez satırıyla oturumu tazeler (mobilde en kolay yol).

        `force=False` iken çerez önce **uygulanmadan** incelenir: oturum çerezi
        içermeyen bir set (ör. yalnız `__cf_bm`) çalışan oturumun üzerine
        yazmasın. Zorlamak için `/cookie! <çerez>`.

        **Parçalı yapıştırma:** Cookie-Editor JSON'u 4-10 KB olabiliyor, Telegram
        tek mesajda ~4 KB kabul eder. Bu yüzden JSON parçaları sırayla
        gönderilebilir; birleşim tamamlanınca otomatik uygulanır (`/cookie bitir`
        ile elle de bitirilebilir, `/cookie iptal` temizler).
        """
        parts = text.split(maxsplit=1)
        body = parts[1].strip() if len(parts) > 1 else ""
        buffer = self._cookie_buffers.setdefault(chat_id, [])
        lowered = body.lower()
        if not body:
            buffer.clear()
            await self.send_text(
                chat_id,
                "Kullanım: /cookie ad=değer; ad2=değer2\n"
                "Cookie-Editor → Export → Header String ya da JSON yapıştırın; "
                "cURL gerekmez.\n"
                "Uzun JSON'u parça parça gönderebilirsiniz: her parçayı /cookie "
                "ile başlatın, bitince **/cookie bitir**. Vazgeçmek için "
                "/cookie iptal.",
            )
            return
        if lowered in {"bitir", "bitti", "tamam"}:
            if not buffer:
                await self.send_text(
                    chat_id,
                    "Bekleyen parça yok. Çerezi /cookie ile gönderin "
                    "(uzun JSON ise parçalara bölerek).",
                )
                return
            body = "".join(buffer)
            buffer.clear()
        elif lowered in {"iptal", "vazgec", "vazgeç"}:
            buffer.clear()
            await self.send_text(chat_id, "Bekleyen çerez parçaları temizlendi.")
            return
        else:
            buffer.append(body)
            joined = "".join(buffer)
            if not self._cookie_complete(joined):
                await self.send_text(
                    chat_id,
                    f"Parça alındı ({len(buffer)}). Devam edin; bitince "
                    "/cookie bitir.",
                )
                return
            body = joined
            buffer.clear()
        inspection: dict[str, Any] | None = None
        if not force:
            inspection = await self._cookie_precheck(chat_id, body)
            if inspection is None:
                return

        try:
            response = await self.wrapper.post(
                "/v1/admin/upstream/cookie", json={"cookie": body}
            )
        except httpx.HTTPError as exc:
            logger.warning("telegram_cookie_upload_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper'a ulaşılamadı; çerez uygulanmadı.")
            return

        payload: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:  # pragma: no cover
            payload = {}
        if response.status_code != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            detail = (
                str(error.get("message") or "")
                if isinstance(error, dict)
                else f"HTTP {response.status_code}"
            )
            await self.send_text(chat_id, "Çerez uygulanamadı: " + detail)
            return
        fields = payload.get("fields") if isinstance(payload, dict) else None
        applied = [str(item) for item in fields] if isinstance(fields, list) else []
        lines = [f"✓ Çerez uygulandı ({len(applied)} alan)."]
        if "upstream_access_token" in applied:
            lines.append("Bearer token da güncellendi.")
        info = inspection if isinstance(inspection, dict) else {}
        if info:
            if info.get("access_token_found"):
                remaining = info.get("access_token_expires_in_seconds")
                if isinstance(remaining, int) and remaining > 0:
                    lines.append(f"Çerezdeki token: ≈{remaining // 60} dk geçerli")
                elif info.get("access_token_expired"):
                    lines.append("⚠ Çerezdeki token **süresi dolmuş**")
                else:
                    lines.append("Çerezdeki token: var")
            else:
                lines.append("⚠ Çerezde okunabilir access_token yok")
            if info.get("refresh_token_found"):
                lines.append("Refresh token: var (otomatik yenileme mümkün)")
            families = info.get("chunked_families")
            if isinstance(families, dict) and families:
                shown = ", ".join(f"{k} ({v} parça)" for k, v in families.items())
                lines.append(f"Parçalı çerez: {shown}")
            problems = info.get("problems")
            if isinstance(problems, list):
                for item in problems[:3]:
                    lines.append(f"⚠ {item}")
        lines.append("Şimdi bir mesaj atarak test edin.")
        await self.send_text(chat_id, "\n".join(lines))

    async def _cookie_precheck(self, chat_id: int, body: str) -> dict[str, Any] | None:
        """Çerezi uygulamadan incele; uygunsuzsa uyarıp `None` döndürür.

        Dönen değer inceleme sonucudur (uygulanabilir). Ağ hatasında engellemeyiz:
        karar veremiyorsak kullanıcıyı kilitlemek yanlış olur.
        """
        try:
            response = await self.wrapper.post(
                "/v1/admin/upstream/cookie?dry_run=true", json={"cookie": body}
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}

        if response.status_code != 200 or not isinstance(payload, dict):
            return {}

        names = payload.get("cookie_names") or []
        shown = ", ".join(str(name) for name in names[:8]) or "(yok)"
        if payload.get("safe_to_apply"):
            if payload.get("access_token_found") and payload.get("access_token_expired"):
                await self.send_text(
                    chat_id,
                    "⚠ Çerezdeki token'ın süresi dolmuş görünüyor; yine de "
                    "uygulanıyor. 401 alırsan tarayıcıda yeniden giriş yapıp "
                    "taze çerez gönder.",
                )
            return payload

        await self.send_text(
            chat_id,
            "⛔ Bu çerez uygulanmadı: içinde oturum taşıyan çerez yok "
            f"(gelenler: {shown}).\n"
            "Bu hâliyle uygulanırsa **çalışan oturumun üzerine yazılabilir**.\n"
            "Giriş yapmış tarayıcıdan TÜM çerezleri aktarın (auth/session/token "
            "adlı çerez mutlaka gelmeli).\n"
            "Yine de zorlamak isterseniz: /cookie! <çerez>",
        )
        return None

    async def capture_upstream_session(self, chat_id: int) -> None:
        """Sunucudaki Chromium ile taze oturum yakalar (telefonda DevTools gerekmez)."""
        await self.send_text(chat_id, "Oturum yakalanıyor, bu 10-40 sn sürebilir…")
        try:
            response = await self.wrapper.post("/v1/admin/upstream/capture", json={})
        except httpx.HTTPError as exc:
            logger.warning("telegram_capture_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper'a ulaşılamadı; yakalama yapılmadı.")
            return

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code != 200:
            error = payload.get("error") if isinstance(payload, dict) else None
            detail = (
                str(error.get("message") or "")
                if isinstance(error, dict)
                else f"HTTP {response.status_code}"
            )
            await self.send_text(chat_id, "Yakalama başarısız: " + detail)
            return

        fields = payload.get("fields") or []
        lines = [
            ("✓ Oturum yakalandı." if payload.get("logged_in") else "⚠ Oturum yakalanamadı."),
            f"Cookie: {'var' if payload.get('cookie_found') else 'yok'} · "
            f"Sürüm damgası: {'var' if payload.get('deployment_found') else 'yok'} · "
            f"cx: {'var' if payload.get('cx_found') else 'yok'}",
        ]
        injected = payload.get("injected_cookies")
        if isinstance(injected, int):
            # "Çerez yüklenmedi" ile "çerez yüklendi ama oturum değil" farkı,
            # telefondan bakınca görünmeli.
            lines.append(
                f"Tarayıcıya yüklenen çerez: {injected} "
                + ("(eldeki oturum çerezi bağlandı)" if injected else "(elde çerez yok!)")
            )
        names = payload.get("cookie_names") or []
        if names and not payload.get("logged_in"):
            # Hangi çerezlerin geldiğini göster: "oturum çerezi yok" demek,
            # eksik çerez setini fark etmenin en hızlı yolu.
            shown = ", ".join(str(name) for name in names[:8])
            more = f" (+{len(names) - 8})" if len(names) > 8 else ""
            lines.append(f"Gelen çerezler: {shown}{more}")
            if not payload.get("session_cookie_found"):
                lines.append(
                    "Oturum çerezi (auth/session/token adlı) gelmedi — giriş "
                    "yapmış tarayıcıdan TÜM çerezleri gönderin: /cookie <çerez>"
                )
        if fields:
            lines.append("Güncellenen alanlar: " + ", ".join(fields))
        lines.append(str(payload.get("next") or "Bir mesaj atarak test edin."))
        await self.send_text(chat_id, "\n".join(lines))

    async def set_upstream_curl(self, chat_id: int, text: str) -> None:
        """Taze cURL'ü çalışan servise uygular — Render'da yeniden deploy gerekmez.

        Oturum düştüğünde (401 "User not found") veya sürüm damgası eskidiğinde
        tek gereken şey yeni bir cURL'dir. Kullanım: `/curl <cURL metni>`.
        Ham cURL hiçbir log/yanıt içine yazılmaz.
        """
        parts = text.split(maxsplit=1)
        body = parts[1].strip() if len(parts) > 1 else ""
        if not body:
            await self.send_text(
                chat_id,
                "Kullanım: /curl <cURL>\n"
                "Tarayıcıda Network → create-evaluation isteği → Copy as cURL, "
                "ardından komutun yanına yapıştırın.\n"
                "İstek listede görünmüyorsa: sayfayı yenileyip **ilk** mesajı "
                "gönderin (sonraki mesajlar açık akıştan gider, yeni istek "
                "oluşmaz) ya da Cookie-Editor → Export → Header String → "
                "/cookie <çerez>.",
            )
            return
        if "curl" not in body.lower():
            await self.send_text(
                chat_id,
                "Gönderilen metin bir cURL gibi görünmüyor. "
                "Network sekmesinden create-evaluation isteğini "
                "'Copy as cURL' ile kopyalayın.",
            )
            return
        if len(body) > 30000:
            await self.send_text(
                chat_id,
                f"cURL çok uzun ({len(body)} karakter). Gereksiz başlıkları "
                "(-H) çıkarıp tekrar deneyin.",
            )
            return

        try:
            response = await self.wrapper.post(
                "/v1/admin/upstream/curl", json={"curl": body}
            )
        except httpx.HTTPError as exc:
            logger.warning("telegram_curl_upload_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper'a ulaşılamadı; cURL uygulanmadı.")
            return

        if response.status_code != 200:
            detail = ""
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            await self.send_text(
                chat_id,
                "cURL uygulanamadı: "
                + (detail or f"HTTP {response.status_code}")
                + "\nÖrnek geçerli biçim: /curl curl 'https://...' -H 'cookie: ...'",
            )
            return

        payload = {}
        try:
            payload = response.json()
        except ValueError:  # pragma: no cover - sözleşme her zaman JSON
            payload = {}
        fields = payload.get("fields") or []
        accounts = payload.get("accounts_updated") or {}
        lines = [
            f"✓ cURL uygulandı: {len(fields)} alan güncellendi.",
            "Hesap: " + (", ".join(sorted(accounts)) if accounts else "değişmedi"),
        ]
        if payload.get("upstream_model"):
            lines.append(f"Upstream model: {payload['upstream_model']}")
        lines.append(
            "Değişiklik bellekte; kalıcı olması için Render'daki UPSTREAM_CURL'ü de "
            "güncelleyin."
        )
        warnings = payload.get("warnings") or []
        if isinstance(warnings, list):
            for item in warnings[:3]:
                lines.append(f"⚠ {item}")
        lines.append("Şimdi bir mesaj atarak test edin.")
        await self.send_text(chat_id, "\n".join(lines))

    async def ask_wrapper(self, chat_id: int, prompt: str) -> None:
        payload = {
            "model": self.settings.telegram_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        try:
            response = await self.wrapper.post("/v1/chat/completions", json=payload)
        except httpx.TimeoutException:
            await self.send_text(
                chat_id, "Wrapper yanıtı zaman aşımına uğradı; sonra tekrar deneyin."
            )
            return
        except httpx.HTTPError as exc:
            logger.warning("telegram_wrapper_request_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper'a ulaşılamadı; bağlantıyı kontrol edin.")
            return

        if response.status_code != 200:
            # Kullanıcıya "upstream_failure" gibi anlamsız bir kod yerine
            # ne olduğunu ve ne yapılacağını tek bakışta söyle.
            code = wrapper_error_code(response)
            await self.send_text(
                chat_id,
                format_failure(response.status_code, code or None),
            )
            return

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            await self.send_text(chat_id, "Wrapper 200 döndü ama OpenAI yanıtı okunamadı.")
            return
        if not isinstance(content, str):
            await self.send_text(chat_id, "Wrapper metin yerine beklenmeyen içerik döndürdü.")
            return
        await self.send_text(chat_id, content)

    async def poll_once(self, offset: int | None) -> int | None:
        payload: dict[str, Any] = {
            "timeout": self.settings.telegram_poll_timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = await self.telegram.post(
            f"{self.telegram_api_url}/getUpdates", json=payload
        )
        if response.status_code >= 400:
            logger.warning("telegram_poll_rejected", status_code=response.status_code)
            raise RuntimeError("Telegram getUpdates rejected")
        data = response.json()
        if not isinstance(data, dict) or not data.get("ok"):
            logger.warning("telegram_poll_api_error")
            raise RuntimeError("Telegram getUpdates API error")
        updates = data.get("result") or []
        if not isinstance(updates, list):
            return offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            message = update.get("message")
            if isinstance(message, dict):
                try:
                    await self.handle_message(message)
                except Exception as exc:  # bir mesaj polling döngüsünü öldürmesin
                    logger.warning("telegram_message_failed", error_type=type(exc).__name__)
        return offset

    async def run(self) -> None:
        offset: int | None = None
        logger.info(
            "telegram_bot_started",
            model=self.settings.telegram_model,
            allowlist_configured=bool(self.settings.allowed_chat_ids),
        )
        while True:
            try:
                offset = await self.poll_once(offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # httpx hata metni token içeren Telegram URL'sini taşıyabilir;
                # yalnız sınıf adını logla.
                logger.warning("telegram_poll_failed", error_type=type(exc).__name__)
                await asyncio.sleep(5)


async def run_from_settings(settings: TelegramSettings) -> None:
    timeout = httpx.Timeout(settings.telegram_request_timeout, connect=10.0)
    async with (
        httpx.AsyncClient(
            base_url=settings.wrapper_base_url,
            headers={"Authorization": f"Bearer {settings.wrapper_api_key.strip()}"},
            timeout=timeout,
        ) as wrapper,
        httpx.AsyncClient(timeout=timeout) as telegram,
    ):
        await TelegramBot(settings, telegram, wrapper).run()


def main() -> None:
    configure_logging("INFO", True)
    try:
        settings = TelegramSettings()
    except ValueError:
        raise SystemExit("Telegram ayarları geçersiz; env değerlerini kontrol edin.") from None
    missing = settings.missing_secrets()
    if missing:
        raise SystemExit("Eksik Telegram env: " + ", ".join(missing))
    try:
        asyncio.run(run_from_settings(settings))
    except KeyboardInterrupt:
        logger.info("telegram_bot_stopped")


if __name__ == "__main__":
    main()
