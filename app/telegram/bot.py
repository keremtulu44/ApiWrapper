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
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.autopilot.curl_parse import (
    looks_like_agent_curl_start,
    reassemble_agent_curl_chunks,
)
from app.core.diagnostics import format_failure
from app.core.logging import configure_logging, get_logger
from app.services.request_queue import SOURCE_HEADER

logger = get_logger(__name__)

_HELP = (
    "ApiWrapper botu aktif.\n"
    "Normal metin gönderin; /v1/chat/completions üzerinden tek turluk yanıt üretilir.\n\n"
    "🤖 Otopilot (Model A — Agent Mode Proje):\n"
    "• /curl — Agent Mode '/input' cURL parçalarını toplamaya başla\n"
    "• /curl bitir (veya /bitir) — Yapıştırılan tüm cURL parçalarını birleştir ve doğrula\n"
    "• /tur oto | /tur <1-25> — Otomatik tur (Max projeye göre seçer) veya sabit tavan\n"
    "• /gece — Yalnız 22:00–07:00 (TR) arasında çalıştır (varsayılan)\n"
    "• /simdi [run_id] — 22:00'yi beklemeden hemen çalıştır / sıradaki projeyi uyandır\n"
    "• /projeler (veya /durum) — Çalışan, sırada bekleyen ve biten projeleri listele\n"
    "• /durum <run_id> — Seçilen projenin tur/hafıza detayını ve Agent linkini gör\n"
    "• /rapor [run_id] — Akşam planını ve Agent sabah teslim özetini gör\n"
    "• /durdur [run_id] — Çalışan veya gece sırasındaki projeyi güvenli biçimde durdur\n"
    "• /iptal — Açık cURL/fikir girişini temizle\n\n"
    "Diğer komutlar: /ping (wrapper health), /help."
)

_CURL_SESSION_TTL_SECONDS = 1800.0
_MAX_CURL_CHUNKS = 25
_MAX_CURL_BUFFER_CHARS = 250_000

_STATUS_LABELS: dict[str, str] = {
    "starting": "⏳ Başlatılıyor",
    "waiting_window": "🌙 Gece Sırasında (22:00–07:00)",
    "running": "🔄 Çalışıyor",
    "completed": "✅ Tamamlandı",
    "max_turns_reached": "⏹️ Tur Sınırına Ulaştı",
    "stopped": "🛑 Durduruldu",
    "failed": "🔴 Hata / Takıldı",
}


@dataclass(slots=True)
class _ChatAutopilotSession:
    """Telegram sohbeti başına geçici cURL birleştirme ve proje başlatma durumu."""

    mode: Literal["idle", "collecting_curl", "awaiting_idea"] = "idle"
    curl_chunks: list[str] = field(default_factory=list)
    validated_curl: str = ""
    validated_chat_id: str = ""
    validated_domain: str = ""
    max_turns: int = 12
    auto_turns: bool = True
    respect_night_window: bool = True
    updated_at: float = field(default_factory=time.monotonic)

    def clear_pending(self) -> None:
        self.mode = "idle"
        self.curl_chunks.clear()
        self.validated_curl = ""
        self.validated_chat_id = ""
        self.validated_domain = ""
        # Bir sonraki proje için güvenli varsayılanlara (otomatik tur + gece penceresi) dön:
        self.max_turns = 12
        self.auto_turns = True
        self.respect_night_window = True
        self.updated_at = time.monotonic()


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


def _autopilot_error_message(response: httpx.Response) -> str:
    """Yalnız `/v1/autopilot/*` uçlarının yapısal, secret içermeyen hata mesajını okur."""
    if response.status_code in {400, 404, 422}:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            msg = str(payload["error"].get("message") or "").strip()
            if msg:
                return msg[:320]
    code = wrapper_error_code(response)
    return format_failure(response.status_code, code or None)


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
        self._sessions: dict[int, _ChatAutopilotSession] = {}

    def _session(self, chat_id: int) -> _ChatAutopilotSession:
        sess = self._sessions.get(chat_id)
        now = time.monotonic()
        if sess is None:
            sess = _ChatAutopilotSession(updated_at=now)
            self._sessions[chat_id] = sess
            return sess
        if (
            sess.mode != "idle"
            and (now - sess.updated_at) > _CURL_SESSION_TTL_SECONDS
        ):
            sess.clear_pending()
        return sess

    @property
    def telegram_api_url(self) -> str:
        # Token Telegram Bot API'nin zorunlu path parçasıdır; hiçbir log/event'e
        # bu URL veya token yazılmaz.
        return f"https://api.telegram.org/bot{self.settings.telegram_bot_token.strip()}"

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        disable_preview: bool = False,
    ) -> None:
        for chunk in split_telegram_text(
            text, max_chars=self.settings.telegram_max_reply_chars
        ):
            body: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if disable_preview:
                body["disable_web_page_preview"] = True
            response = await self.telegram.post(
                f"{self.telegram_api_url}/sendMessage",
                json=body,
            )
            if response.status_code >= 400:
                logger.warning(
                    "telegram_send_failed",
                    status_code=response.status_code,
                )

    async def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        raw_text = message.get("text")
        if not isinstance(chat, dict) or not isinstance(raw_text, str):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        text = raw_text.strip()
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

        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        sess = self._session(chat_id)

        if command in {"/start", "/help"}:
            await self.send_text(chat_id, _HELP)
            return
        if command == "/ping":
            await self.ping(chat_id)
            return
        if command in {"/iptal", "/cancel"}:
            sess.clear_pending()
            await self.send_text(
                chat_id,
                "🧹 Bekleyen cURL/proje girişi temizlendi. Normal sohbet modundasınız.",
            )
            return
        if command == "/curl":
            await self._handle_curl_command(chat_id, sess, arg)
            return
        if command in {"/bitir", "/curl_bitir", "/curlbitir", "/tamam"}:
            await self._finish_curl_collection(chat_id, sess)
            return
        if command == "/tur":
            await self._handle_turn_limit(chat_id, sess, arg)
            return
        if command == "/gece":
            await self._handle_night_mode(chat_id, sess)
            return
        if command in {"/simdi", "/hemen"}:
            await self._handle_start_now(chat_id, sess, arg)
            return
        if command in {"/proje", "/baslat"}:
            if not arg:
                await self.send_text(
                    chat_id,
                    "Kullanım: /proje <yaptırmak istediğiniz proje fikri>",
                )
                return
            await self._start_autopilot_with_idea(chat_id, sess, arg)
            return
        if command == "/projeler":
            await self._list_projects(chat_id)
            return
        if command in {"/durum", "/detay"}:
            if arg:
                await self._show_project_detail(chat_id, arg.split()[0])
            else:
                await self._list_projects(chat_id)
            return
        if command in {"/rapor", "/ozet"}:
            await self._show_project_report(chat_id, arg.split()[0] if arg else "")
            return
        if command in {"/durdur", "/stop"}:
            await self._stop_project(chat_id, arg.split()[0] if arg else "")
            return
        if text.startswith("/"):
            await self.send_text(chat_id, "Bilinmeyen komut. /help yazın.")
            return

        # Komut olmayan metin akışı:
        if sess.mode == "collecting_curl":
            await self._append_curl_chunk(chat_id, sess, raw_text)
            return
        if looks_like_agent_curl_start(raw_text):
            # Kullanıcı /curl yazmayı unutup doğrudan cURL yapıştırdıysa çerezleri
            # asla max sohbetine gönderme; otomatik toplama moduna al.
            sess.clear_pending()
            sess.mode = "collecting_curl"
            await self._append_curl_chunk(chat_id, sess, raw_text, auto_detected=True)
            return
        if sess.mode == "awaiting_idea":
            await self._start_autopilot_with_idea(chat_id, sess, text)
            return

        await self.ask_wrapper(chat_id, text)

    # ------------------------------------------------- Autopilot (Model A) akışı
    async def _handle_curl_command(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        arg: str,
    ) -> None:
        lowered = arg.lower()
        if lowered in {"bitir", "bitti", "tamam", "son", "done"}:
            await self._finish_curl_collection(chat_id, sess)
            return
        if lowered in {"iptal", "temizle", "sil", "cancel"}:
            sess.clear_pending()
            await self.send_text(chat_id, "🧹 cURL toplama iptal edildi.")
            return

        sess.clear_pending()
        sess.mode = "collecting_curl"
        sess.updated_at = time.monotonic()

        if arg and looks_like_agent_curl_start(arg):
            await self._append_curl_chunk(chat_id, sess, arg)
            return

        await self.send_text(
            chat_id,
            "📥 Agent cURL toplama modu açıldı.\n"
            "Şimdi Agent Mode '/api/chat/<id>/input' cURL'ünü yapıştırın — Telegram "
            "mesajı 3-4 parçaya bölse de hepsini sırayla bekleyip birleştireceğim.\n"
            "Tüm parçalar gidince /curl bitir (veya /bitir) yazın. İptal: /iptal",
        )

    async def _append_curl_chunk(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        raw_chunk: str,
        *,
        auto_detected: bool = False,
    ) -> None:
        total_after = sum(len(c) for c in sess.curl_chunks) + len(raw_chunk)
        if len(sess.curl_chunks) >= _MAX_CURL_CHUNKS or total_after > _MAX_CURL_BUFFER_CHARS:
            sess.clear_pending()
            await self.send_text(
                chat_id,
                "⚠️ cURL tampon sınırı aşıldı ve temizlendi. /curl ile baştan deneyin.",
            )
            return

        sess.curl_chunks.append(raw_chunk)
        sess.updated_at = time.monotonic()
        count = len(sess.curl_chunks)
        prefix = "📥 Agent cURL algılandı — " if auto_detected else "📥 "
        await self.send_text(
            chat_id,
            f"{prefix}Parça {count} alındı (toplam {total_after} krk). "
            "Tüm parçalar gidince /curl bitir yazın.",
        )

    async def _finish_curl_collection(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
    ) -> None:
        if sess.mode == "awaiting_idea" and sess.validated_curl:
            await self.send_text(
                chat_id,
                "✅ Agent cURL zaten doğrulandı. Şimdi yaptırmak istediğiniz "
                "proje fikrini mesaj olarak gönderin (İptal: /iptal).",
            )
            return

        if not sess.curl_chunks:
            await self.send_text(
                chat_id,
                "⚠️ Henüz hiç cURL parçası yapıştırılmadı. Önce /curl yazıp Agent "
                "cURL'ünü yapıştırın, tüm parçalar gidince /curl bitir yazın.",
            )
            return

        chunk_count = len(sess.curl_chunks)
        joined = reassemble_agent_curl_chunks(sess.curl_chunks)
        sess.curl_chunks.clear()

        try:
            response = await self.wrapper.post(
                "/v1/autopilot/inspect-curl",
                json={"agent_curl": joined},
            )
        except httpx.HTTPError as exc:
            sess.clear_pending()
            logger.warning("telegram_autopilot_inspect_failed", error_type=type(exc).__name__)
            await self.send_text(
                chat_id,
                "Wrapper /v1/autopilot/inspect-curl ucuna ulaşılamadı.",
            )
            return

        if response.status_code != 200:
            sess.mode = "collecting_curl"
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                f"❌ Agent cURL doğrulanamadı: {_autopilot_error_message(response)}\n\n"
                "Tekrar cURL yapıştırıp /curl bitir yazabilir veya /iptal diyebilirsiniz.",
            )
            return

        data = response.json()
        if data.get("same_account_conflict"):
            sess.clear_pending()
            await self.send_text(
                chat_id,
                "⛔ Hesap Çakışması: Yapıştırdığınız Agent cURL'ü sunucudaki Max hesabıyla "
                "AYNI Google hesabına ait! İki oturumun birbirini düşürmemesi için Agent "
                "Mode cURL'ünü farklı bir Google hesabından alıp /curl ile tekrar deneyin.",
            )
            return

        sess.mode = "awaiting_idea"
        sess.validated_curl = joined
        sess.validated_chat_id = str(data.get("chat_id") or "")
        sess.validated_domain = str(data.get("target_domain") or "")
        sess.updated_at = time.monotonic()

        warnings = [str(w) for w in (data.get("warnings") or []) if w]
        warn_block = ("\n⚠️ Not: " + " ".join(warnings)) if warnings else ""
        turn_label = (
            f"Otomatik (Max projeye göre belirler, güvenlik tavanı: {sess.max_turns})"
            if sess.auto_turns
            else f"Sabit tavan: {sess.max_turns}"
        )
        schedule_label = (
            "🌙 Gece Penceresi (22:00–07:00 TR · hemen başlatmak için önce /simdi yazın)"
            if sess.respect_night_window
            else "⚡ Hemen Başlat (gece penceresini beklemez)"
        )
        await self.send_text(
            chat_id,
            f"✅ Agent cURL doğrulandı ({chunk_count} parça birleştirildi)!\n"
            f"• Agent Sohbet: {sess.validated_chat_id}\n"
            f"• Kaynak Türü: {data.get('source_endpoint_kind', 'unknown')}\n"
            f"• Tur Planı: {turn_label}\n"
            f"• Çalışma Zamanı: {schedule_label}"
            f"{warn_block}\n\n"
            "✏️ Şimdi yaptırmak istediğiniz **proje fikrini** yazıp gönderin.",
            disable_preview=True,
        )

    async def _handle_turn_limit(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        arg: str,
    ) -> None:
        cleaned = arg.strip().lower()
        if cleaned in {"oto", "otomatik", "auto"}:
            sess.auto_turns = True
            sess.max_turns = 12
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                "⚙️ Tur planlaması OTOMATİK moda alındı (Max projenin kapsamına göre "
                "ideal tur sayısını kendi belirleyecek, üst güvenlik tavanı: 12).",
            )
            return
        if not cleaned:
            mode_txt = (
                f"Otomatik (tavan: {sess.max_turns})"
                if sess.auto_turns
                else f"Sabit ({sess.max_turns} tur)"
            )
            await self.send_text(
                chat_id,
                f"Mevcut tur ayarı: {mode_txt}\n"
                "Değiştirmek için: /tur oto veya /tur <1 ile 25 arası sayı>",
            )
            return
        try:
            turns = int(cleaned)
        except ValueError:
            await self.send_text(
                chat_id,
                "Kullanım: /tur oto veya /tur <1 ile 25 arası sayı> (ör. /tur 10)",
            )
            return
        if turns < 1 or turns > 25:
            await self.send_text(chat_id, "Tur sayısı 1 ile 25 arasında olmalıdır.")
            return
        sess.max_turns = turns
        sess.auto_turns = False
        sess.updated_at = time.monotonic()
        extra = (
            " Şimdi proje fikrini gönderebilirsiniz."
            if sess.mode == "awaiting_idea"
            else ""
        )
        await self.send_text(
            chat_id,
            f"⚙️ Otopilot azami tur sayısı {turns} olarak ayarlandı.{extra}",
        )

    async def _handle_night_mode(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
    ) -> None:
        sess.respect_night_window = True
        sess.updated_at = time.monotonic()
        await self.send_text(
            chat_id,
            "🌙 Gece çalışma penceresi aktif: Başlatacağınız proje yalnızca "
            "22:00–07:00 (TR) saatleri arasında çalışacak; gündüz gönderirseniz "
            "Max'a dokunmadan 22:00'yi bekleyecek.",
        )

    async def _handle_start_now(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        arg: str,
    ) -> None:
        target_id = arg.split()[0].strip() if arg.strip() else ""
        if not target_id and sess.mode == "awaiting_idea":
            sess.respect_night_window = False
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                "⚡ Hemen başlatma modu seçildi: Şimdi yazacağınız proje fikri "
                "22:00 gece penceresini beklemeden anında başlayacak.",
            )
            return

        if not target_id:
            try:
                list_resp = await self.wrapper.get("/v1/autopilot/runs")
            except httpx.HTTPError:
                list_resp = None
            if list_resp is not None and list_resp.status_code == 200:
                runs = list_resp.json().get("runs") or []
                waiting = [
                    str(r.get("run_id"))
                    for r in runs
                    if isinstance(r, dict) and r.get("status") == "waiting_window"
                ]
                if len(waiting) == 1:
                    target_id = waiting[0]
                elif len(waiting) > 1:
                    await self.send_text(
                        chat_id,
                        "Gece sırasında bekleyen birden fazla proje var; hangisini "
                        "hemen başlatacağınızı seçin:\n"
                        + "\n".join(f"• /simdi {rid}" for rid in waiting),
                    )
                    return

        if target_id:
            try:
                resp = await self.wrapper.post(f"/v1/autopilot/runs/{target_id}/start-now")
            except httpx.HTTPError:
                await self.send_text(chat_id, "Wrapper start-now ucuna ulaşılamadı.")
                return
            if resp.status_code != 200:
                await self.send_text(chat_id, _autopilot_error_message(resp))
                return
            await self.send_text(
                chat_id,
                f"⚡ {target_id} için gece penceresi beklemesi kaldırıldı; hemen başlatılıyor!",
            )
            return

        sess.respect_night_window = False
        sess.updated_at = time.monotonic()
        await self.send_text(
            chat_id,
            "⚡ Sıradaki proje için hemen başlatma modu açıldı (22:00 beklenmeyecek). "
            "Tekrar gece moduna dönmek için /gece yazabilirsiniz.",
        )

    async def _start_autopilot_with_idea(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        idea: str,
    ) -> None:
        if not sess.validated_curl:
            await self.send_text(
                chat_id,
                "⚠️ Hazır Agent cURL yok. Önce /curl yazıp Agent cURL'ünü yapıştırın "
                "ve /curl bitir ile doğrulayın.",
            )
            return

        cleaned_idea = idea.strip()
        if len(cleaned_idea) < 5:
            await self.send_text(
                chat_id,
                "Proje fikri çok kısa (en az 5 karakter). Lütfen biraz daha detay yazın.",
            )
            return

        agent_curl = sess.validated_curl
        max_turns = sess.max_turns
        auto_turns = sess.auto_turns
        respect_night = sess.respect_night_window
        try:
            response = await self.wrapper.post(
                "/v1/autopilot/runs",
                json={
                    "project_idea": cleaned_idea,
                    "agent_curl": agent_curl,
                    "max_turns": max_turns,
                    "auto_turns": auto_turns,
                    "respect_night_window": respect_night,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_start_failed", error_type=type(exc).__name__)
            await self.send_text(
                chat_id,
                "Wrapper /v1/autopilot/runs ucuna ulaşılamadı; cURL bellekte korundu, "
                "fikri tekrar gönderebilirsiniz.",
            )
            return

        if response.status_code != 200:
            err_msg = _autopilot_error_message(response)
            await self.send_text(
                chat_id,
                f"❌ Otopilot başlatılamadı: {err_msg}",
            )
            return

        # Başarıyla başladı: ham cURL'ü bot belleğinden derhal temizle.
        sess.clear_pending()
        data = response.json()
        run_id = str(data.get("run_id") or "")
        agent_url = str(data.get("agent_url") or data.get("chat_id") or "")
        run_status = str(data.get("status") or "")
        turn_desc = (
            f"Otomatik (Tavan: {data.get('max_turns', max_turns)})"
            if auto_turns
            else str(data.get("max_turns", max_turns))
        )
        sched_desc = (
            "🌙 Gece Penceresi (22:00–07:00 TR · hemen başlatmak için: /simdi "
            f"{run_id})"
            if respect_night
            else "⚡ Anında Yürütme"
        )
        headline = (
            "🌙 Otopilot projesi gece sırasına alındı!"
            if respect_night and run_status == "waiting_window"
            else "🚀 Otopilot projesi arka planda başlatıldı!"
        )
        await self.send_text(
            chat_id,
            f"{headline}\n"
            f"• Run ID: {run_id}\n"
            f"• Agent: {agent_url}\n"
            f"• Tur Planı: {turn_desc}\n"
            f"• Zamanlama: {sched_desc}\n\n"
            f"Durum takibi: /projeler veya /durum {run_id}\n"
            f"Durdurmak için: /durdur {run_id}\n"
            "Yeni proje eklemek için tekrar /curl yazabilirsiniz.",
            disable_preview=True,
        )

    async def _list_projects(self, chat_id: int) -> None:
        try:
            response = await self.wrapper.get("/v1/autopilot/runs")
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_list_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Proje listesine ulaşılamadı.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        data = response.json()
        runs = data.get("runs") or []
        active_count = data.get("active_count", 0)
        if not runs:
            await self.send_text(
                chat_id,
                "Henüz başlatılmış otopilot projesi yok.\n"
                "Yeni proje başlatmak için /curl yazıp Agent cURL'ünü yapıştırın.",
            )
            return

        lines = [f"📋 Otopilot Projeleri (Aktif: {active_count} · Toplam: {len(runs)})"]
        for item in runs[:12]:
            rid = item.get("run_id", "-")
            st = str(item.get("status") or "")
            badge = _STATUS_LABELS.get(st, st)
            cur = item.get("current_turn", 0)
            mx = item.get("max_turns", 0)
            planned = item.get("planned_turns", 0)
            turn_tag = (
                f"Tur {cur}/~{planned} [tavan {mx}]"
                if planned
                else f"Tur {cur}/{mx}"
            )
            idea = str(item.get("project_idea") or "").strip()
            link = str(item.get("agent_url") or item.get("chat_id") or "")
            note = str(item.get("last_note") or "").strip()
            block = [
                f"\n• {rid} — {badge} ({turn_tag})",
            ]
            if idea:
                block.append(f"  Proje: {idea}")
            if link:
                block.append(f"  Agent: {link}")
            if note and st in {"running", "starting", "waiting_window"}:
                block.append(f"  Son adım: {note}")
            elif item.get("stop_reason") and st != "running":
                reason_short = " ".join(str(item["stop_reason"]).split())[:140]
                block.append(f"  Sonuç: {reason_short}")
            lines.extend(block)

        lines.append("\nDetay için: /durum <run_id> · Yeni proje: /curl")
        await self.send_text(chat_id, "\n".join(lines), disable_preview=True)

    async def _show_project_detail(self, chat_id: int, run_id: str) -> None:
        try:
            response = await self.wrapper.get(f"/v1/autopilot/runs/{run_id}")
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_detail_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Proje detayına ulaşılamadı.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        data = response.json()
        st = str(data.get("status") or "")
        badge = _STATUS_LABELS.get(st, st)
        link = str(data.get("agent_url") or data.get("chat_id") or "")
        idea = " ".join(str(data.get("project_idea") or "").split())[:300]
        lines = [
            f"🔎 Proje Detayı: {data.get('run_id', run_id)}",
            f"• Durum: {badge}",
            f"• Tur: {data.get('current_turn', 0)}/{data.get('max_turns', 0)} "
            f"({data.get('current_phase', '-')})",
            f"• Agent: {link}",
            f"• Fikir: {idea}",
        ]
        if data.get("last_note"):
            lines.append(f"• Son Not: {data['last_note']}")
        if data.get("stop_reason"):
            lines.append(f"• Sonuç/Neden: {str(data['stop_reason'])[:300]}")
        if data.get("error_detail"):
            lines.append(f"• Hata: {str(data['error_detail'])[:220]}")
        if data.get("evening_plan"):
            plan_txt = str(data["evening_plan"]).strip()
            if len(plan_txt) > 500:
                plan_txt = plan_txt[:497].rstrip() + "..."
            lines.append(f"\n🌆 Akşam Başlangıç Planı:\n{plan_txt}")
        if data.get("handoff_report"):
            rep_txt = str(data["handoff_report"]).strip()
            if len(rep_txt) > 1200:
                rep_txt = rep_txt[:1197].rstrip() + "...\n(Tamamı için: /rapor " + str(
                    data.get("run_id", run_id)
                ) + ")"
            lines.append(f"\n🌅 Sabah Teslim Özeti:\n{rep_txt}")
        elif data.get("memory_summary"):
            mem = str(data["memory_summary"]).strip()
            if len(mem) > 600:
                mem = mem[:597].rstrip() + "..."
            lines.append(f"\n🧠 Mimari Hafıza:\n{mem}")

        turns = data.get("turns") or []
        if turns:
            lines.append("\n📜 Son Turlar:")
            for t in turns[-4:]:
                if not isinstance(t, dict):
                    continue
                lines.append(
                    f"  - Tur {t.get('turn')}: [{t.get('phase')}] "
                    f"{t.get('duration_seconds')} sn · {t.get('tool_count')} araç "
                    f"({t.get('bash_count')} bash, {t.get('error_tool_count')} hata)"
                )

        await self.send_text(chat_id, "\n".join(lines), disable_preview=True)

    async def _show_project_report(self, chat_id: int, run_id: str) -> None:
        target_id = run_id.strip()
        if not target_id:
            try:
                list_resp = await self.wrapper.get("/v1/autopilot/runs")
            except httpx.HTTPError:
                await self.send_text(chat_id, "Proje listesine ulaşılamadı.")
                return
            if list_resp.status_code != 200:
                await self.send_text(chat_id, _autopilot_error_message(list_resp))
                return
            runs = list_resp.json().get("runs") or []
            if not runs:
                await self.send_text(chat_id, "Henüz kayıtlı bir otopilot projesi yok.")
                return
            target_id = str(runs[0].get("run_id") or "")

        try:
            response = await self.wrapper.get(f"/v1/autopilot/runs/{target_id}")
        except httpx.HTTPError:
            await self.send_text(chat_id, "Proje raporuna ulaşılamadı.")
            return
        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        data = response.json()
        link = str(data.get("agent_url") or data.get("chat_id") or "")
        raw_st = str(data.get("status") or "")
        parts = [
            f"📑 Proje Raporu: {data.get('run_id', target_id)}",
            f"• Durum: {_STATUS_LABELS.get(raw_st, raw_st)}",
            f"• Agent: {link}",
        ]
        if data.get("evening_plan"):
            parts.append(f"\n🌆 Akşam Başlangıç Planı (Max):\n{str(data['evening_plan']).strip()}")
        if data.get("handoff_report"):
            parts.append(
                f"\n🌅 Sabah Teslim Raporu (Agent):\n{str(data['handoff_report']).strip()}"
            )
        elif not data.get("evening_plan"):
            parts.append("\nHenüz akşam planı veya sabah teslim özeti oluşmadı.")
        else:
            parts.append("\n⏳ Sabah teslim raporu geliştirme turları bittiğinde oluşacak.")

        await self.send_text(chat_id, "\n".join(parts), disable_preview=True)

    async def _stop_project(self, chat_id: int, run_id: str) -> None:
        target_id = run_id.strip()
        if not target_id:
            try:
                list_resp = await self.wrapper.get("/v1/autopilot/runs")
            except httpx.HTTPError:
                await self.send_text(chat_id, "Çalışan projeler sorgulanamadı.")
                return
            if list_resp.status_code != 200:
                await self.send_text(chat_id, _autopilot_error_message(list_resp))
                return
            runs = list_resp.json().get("runs") or []
            active = [
                str(r.get("run_id"))
                for r in runs
                if isinstance(r, dict)
                and r.get("status") in {"starting", "waiting_window", "running"}
            ]
            if not active:
                await self.send_text(chat_id, "Şu anda çalışan bir otopilot projesi yok.")
                return
            if len(active) > 1:
                await self.send_text(
                    chat_id,
                    "Birden fazla aktif proje var; hangisini durduracağınızı belirtin:\n"
                    + "\n".join(f"• /durdur {rid}" for rid in active),
                )
                return
            target_id = active[0]

        try:
            response = await self.wrapper.post(f"/v1/autopilot/runs/{target_id}/stop")
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_stop_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Durdurma isteği gönderilemedi.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        await self.send_text(
            chat_id,
            f"🛑 {target_id} için durdurma işareti gönderildi. Mevcut adım tamamlandığında "
            "güvenli biçimde duracak.",
        )

    async def ping(self, chat_id: int) -> None:
        try:
            response = await self.wrapper.get("/healthz")
        except httpx.HTTPError as exc:
            logger.warning("telegram_wrapper_health_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper health kontrolüne ulaşılamadı.")
            return
        await self.send_text(
            chat_id,
            f"Wrapper /healthz: HTTP {response.status_code}"
            + (" (hazır değil)" if response.status_code != 200 else " (çalışıyor)"),
        )

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
            headers={
                "Authorization": f"Bearer {settings.wrapper_api_key.strip()}",
                # İstek sırasında kaynağın loglarda görünmesi için (yetki vermez).
                SOURCE_HEADER: "telegram",
            },
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
