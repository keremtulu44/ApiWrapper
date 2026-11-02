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
    "Komutlar: /ping (wrapper + son upstream kontrolü), /yenile (sunucu "
    "tarayıcısıyla oturumu tazele), /curl <cURL> (elle cURL), /help.\n"
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
        if command in {"/curl", "/setcurl"}:
            await self.set_upstream_curl(chat_id, text)
            return
        if command in {"/yenile", "/refresh", "/capture"}:
            await self.capture_upstream_session(chat_id)
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
                "ardından komutun yanına yapıştırın.",
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
