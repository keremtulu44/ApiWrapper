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
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.autopilot.curl_parse import (
    detect_pasted_curl_kind,
    looks_like_agent_curl_start,
    reassemble_agent_curl_chunks,
    reassemble_upstream_curl_chunks,
)
from app.core.diagnostics import format_failure
from app.core.logging import configure_logging, get_logger
from app.services.request_queue import SOURCE_HEADER

logger = get_logger(__name__)

_HELP = (
    "ApiWrapper botu aktif.\n"
    "Normal metin gönderin; /v1/chat/completions üzerinden tek turluk yanıt üretilir.\n\n"
    "🔑 Canlı Hesap & cURL Yönetimi (Deploy Gerekmez):\n"
    "• /upstream_curl — 1. ana Max hesabının cURL'ünü canlı güncelle (+ /bitir)\n"
    "• /upstream_curl2 — Gündüz 2. Max hesabını canlı ekle/güncelle (+ /bitir)\n"
    "• /upstream_curl2 kapat — 2. Max hesabının bağlantısını ve keepalive'ını kökten kes\n"
    "• /hesaplar — Açık Max yuvalarını, rollerini ve bağlantı durumunu görüntüle\n"
    "• /rotasyon [a b] — İki Max yuvasının yerini değiştir (3 hesap dönüşümü)\n\n"
    "🤖 Otopilot (Model A — Agent Mode Proje):\n"
    "• /agent_curl (veya /curl) — Agent Mode '/input' cURL'ünü yapıştır (+ /bitir)\n"
    "• /agent_test — Bellekteki Agent cURL oturumunun canlı sağlığını tekrar test et\n"
    "• /bitir — Yapıştırılan (Telegram'ın böldüğü) tüm cURL parçalarını birleştir ve doğrula\n"
    "• /tur oto | /tur <1-25> — Otomatik tur (Max projeye göre seçer) veya sabit tavan\n"
    "• /gece — Yalnız 22:00–07:00 (TR) arasında çalıştır (artık isteğe bağlı)\n"
    "• /simdi [run_id] — 22:00'yi beklemeden hemen çalıştır / sıradaki projeyi uyandır\n"
    "• /devam [run_id] [simdi] — Taze cURL sonrası yarıda kalan projeyi kaldığı yerden sürdür\n"
    "• /projeler (veya /durum) — Çalışan, sırada bekleyen ve biten projeleri listele\n"
    "• /durum <run_id> — Seçilen projenin tur/hafıza detayını ve Agent linkini gör\n"
    "• /rapor [run_id] — Akşam planını ve Agent sabah teslim özetini gör\n"
    "• /durdur [run_id | hepsi] — Çalışan/bekleyen projeyi durdur (`hepsi` anında keser)\n"
    "• /temizle [hepsi | bitenler | run_id] — Otopilot işlemlerini durdurup bellekten sil\n"
    "• /iptal — Açık cURL/fikir girişini temizle\n\n"
    "Diğer komutlar: /ping (wrapper health), /help."
)

def _first_lines(text: object, count: int, limit: int) -> str:
    """Metnin ilk `count` dolu satırını `limit` karaktere kırparak döndürür."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    picked = [ln.strip() for ln in raw.splitlines() if ln.strip()][:count]
    out = " / ".join(picked)
    return out[:limit] + ("..." if len(out) > limit else "")


_CURL_SESSION_TTL_SECONDS = 1800.0
_MAX_CURL_CHUNKS = 25
_MAX_CURL_BUFFER_CHARS = 250_000

_KNOWN_BOT_COMMANDS: frozenset[str] = frozenset(
    {
        "/start",
        "/help",
        "/ping",
        "/iptal",
        "/cancel",
        "/curl",
        "/agent_curl",
        "/agent_test",
        "/upstream_curl",
        "/upstream_curl2",
        "/hesaplar",
        "/rotasyon",
        "/bitir",
        "/curl_bitir",
        "/curlbitir",
        "/tamam",
        "/tur",
        "/gece",
        "/simdi",
        "/hemen",
        "/devam",
        "/resume",
        "/proje",
        "/baslat",
        "/projeler",
        "/durum",
        "/detay",
        "/rapor",
        "/ozet",
        "/durdur",
        "/stop",
        "/temizle",
        "/sil",
        "/sifirla",
    }
)
_LONG_ARG_COMMANDS: frozenset[str] = frozenset(
    {"/upstream_curl", "/upstream_curl2", "/agent_curl", "/curl", "/proje", "/baslat"}
)
_CMD_PREFIX_RE = re.compile(r"^\s*(/[^\s@]+)(?:@\S+)?(?:[ \t]+|\r?\n|$)")

CurlTarget = Literal["agent", "upstream_1", "upstream_2", "unassigned_max"]

_CURL_TARGET_LABELS: dict[str, str] = {
    "agent": "Agent Mode",
    "upstream_1": "Max 1. Hesap (upstream_curl)",
    "upstream_2": "Max 2. Hesap (upstream_curl2)",
    "unassigned_max": "Max Hesabı (yuva seçilmedi)",
}

_STATUS_LABELS: dict[str, str] = {
    "starting": "⏳ Başlatılıyor",
    "waiting_window": "🌙 Gece Sırasında (22:00–07:00)",
    "running": "🔄 Çalışıyor",
    "completed": "✅ Tamamlandı",
    "max_turns_reached": "⏹️ Tur Sınırına Ulaştı",
    "stopped": "🛑 Durduruldu",
    "failed": "🔴 Hata / Takıldı",
}


def _extract_raw_command_body(raw_text: str) -> str:
    """Komut önekini silerken mesajın sağ ucundaki boşluk/yeni satırı birebir korur.

    Telegram uzun bir `/upstream_curl2 curl ...` mesajını tam 4096. karakterdeki
    bir boşluktan böldüğünde sondaki boşluğun `.strip()` ile yutulmaması gerekir.
    """
    match = _CMD_PREFIX_RE.match(raw_text)
    if not match:
        return ""
    return raw_text[match.end() :]


@dataclass(slots=True)
class _ChatAutopilotSession:
    """Telegram sohbeti başına geçici cURL birleştirme ve proje başlatma durumu."""

    mode: Literal["idle", "collecting_curl", "awaiting_idea"] = "idle"
    curl_target: CurlTarget = "agent"
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
        self.curl_target = "agent"
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

    async def _download_text_document(self, document: dict[str, Any]) -> str | None:
        """Telegram'ın uzun yapıştırmaları `.txt` dosyasına çevirdiği durumları okur."""
        file_id = document.get("file_id")
        file_size = int(document.get("file_size") or 0)
        file_name = str(document.get("file_name") or "").lower()
        mime_type = str(document.get("mime_type") or "").lower()
        if not isinstance(file_id, str) or not file_id.strip():
            return None
        if file_size > _MAX_CURL_BUFFER_CHARS:
            return None
        if (
            file_name
            and not file_name.endswith((".txt", ".curl", ".sh", ".cmd", ".bat"))
            and not mime_type.startswith("text/")
        ):
            return None
        try:
            meta_resp = await self.telegram.get(
                f"{self.telegram_api_url}/getFile",
                params={"file_id": file_id.strip()},
            )
            if meta_resp.status_code != 200:
                return None
            result = meta_resp.json().get("result") or {}
            file_path = str(result.get("file_path") or "").strip()
            if not file_path:
                return None
            token = self.settings.telegram_bot_token.strip()
            file_resp = await self.telegram.get(
                f"https://api.telegram.org/file/bot{token}/{file_path}"
            )
            if file_resp.status_code != 200:
                return None
            return file_resp.content[:_MAX_CURL_BUFFER_CHARS].decode(
                "utf-8-sig", errors="replace"
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "telegram_document_fetch_failed", error_type=type(exc).__name__
            )
            return None

    async def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return

        raw_text = message.get("text")
        document = message.get("document")
        if not isinstance(raw_text, str) and not isinstance(document, dict):
            return

        allowed = self.settings.allowed_chat_ids
        if not allowed:
            # İlk açılışta kim olduğunu öğrenmek güvenli; wrapper kotası kapalı.
            if isinstance(raw_text, str) and raw_text.strip():
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

        if not isinstance(raw_text, str) and isinstance(document, dict):
            doc_text = await self._download_text_document(document)
            if not doc_text or not doc_text.strip():
                return
            caption = str(message.get("caption") or "").strip()
            raw_text = f"{caption}\n{doc_text}" if caption else doc_text

        assert isinstance(raw_text, str)
        text = raw_text.strip()
        if not text:
            return

        parts = text.split(maxsplit=1)
        command = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        raw_arg = _extract_raw_command_body(raw_text)
        sess = self._session(chat_id)

        is_real_command = (command in _KNOWN_BOT_COMMANDS) and (
            command in _LONG_ARG_COMMANDS or len(text) <= 160
        )
        if (
            is_real_command
            and sess.mode == "collecting_curl"
            and command not in {"/upstream_curl", "/upstream_curl2", "/agent_curl", "/curl"}
            and any(ch in text for ch in ("'", '"', "{", "}", ";", "="))
        ):
            # Bölünmüş son cURL parçasının tesadüfen `/...` ile başlaması durumu
            is_real_command = False

        if is_real_command:
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
            if command == "/hesaplar":
                await self._show_accounts_status(chat_id)
                return
            if command == "/upstream_curl":
                await self._handle_upstream_curl_command(
                    chat_id, sess, arg=arg, raw_arg=raw_arg, slot=1
                )
                return
            if command == "/upstream_curl2":
                await self._handle_upstream_curl_command(
                    chat_id, sess, arg=arg, raw_arg=raw_arg, slot=2
                )
                return
            if command in {"/agent_curl", "/curl"}:
                await self._handle_curl_command(chat_id, sess, arg=arg, raw_arg=raw_arg)
                return
            if command == "/agent_test":
                await self._handle_agent_test(chat_id, sess)
                return
            if command in {"/bitir", "/curl_bitir", "/curlbitir", "/tamam"}:
                await self._finish_curl_collection(chat_id, sess)
                return
            if command == "/tur":
                await self._handle_turn_limit(chat_id, sess, arg)
                return
            if command == "/gece":
                await self._handle_night_mode(chat_id, sess, arg)
                return
            if command == "/rotasyon":
                await self._handle_rotation(chat_id, arg)
                return
            if command in {"/simdi", "/hemen"}:
                await self._handle_start_now(chat_id, sess, arg)
                return
            if command in {"/devam", "/resume"}:
                await self._handle_resume_command(chat_id, sess, arg)
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
                await self._stop_project(chat_id, arg)
                return
            if command in {"/temizle", "/sil", "/sifirla"}:
                await self._clear_projects(chat_id, sess, arg)
                return

        # cURL toplama modundayken Telegram'ın 4096. karakterde böldüğü bir parça
        # `/` ile başlasa bile (ör. `/537.36`, `/9kQ+...`, `/*`) komut değil parçadır!
        if sess.mode == "collecting_curl":
            await self._append_curl_chunk(chat_id, sess, raw_text)
            return

        if text.startswith("/"):
            await self.send_text(chat_id, "Bilinmeyen komut. /help yazın.")
            return

        if looks_like_agent_curl_start(raw_text):
            # Kullanıcı komut yazmayı unutup doğrudan cURL yapıştırdıysa çerezleri
            # asla max sohbetine gönderme; otomatik toplama moduna al.
            detected_kind = detect_pasted_curl_kind(raw_text)
            sess.clear_pending()
            sess.mode = "collecting_curl"
            sess.curl_target = (
                "unassigned_max" if detected_kind == "max" else "agent"
            )
            await self._append_curl_chunk(chat_id, sess, raw_text, auto_detected=True)
            return
        if sess.mode == "awaiting_idea":
            await self._start_autopilot_with_idea(chat_id, sess, text)
            return

        await self.ask_wrapper(chat_id, text)

    # ------------------------------------------------- Canlı Max Yuva & Agent cURL akışı
    async def _handle_upstream_curl_command(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        *,
        arg: str,
        raw_arg: str,
        slot: Literal[1, 2],
    ) -> None:
        lowered = arg.lower()
        target: CurlTarget = "upstream_1" if slot == 1 else "upstream_2"
        cmd_name = "/upstream_curl" if slot == 1 else "/upstream_curl2"

        if slot == 2 and lowered in {"kapat", "sil", "durdur", "close", "off"}:
            if sess.mode == "collecting_curl" and sess.curl_target == "upstream_2":
                sess.clear_pending()
            await self._close_upstream_slot_2(chat_id)
            return

        if slot == 1 and lowered in {"kapat", "sil", "close", "off"}:
            await self.send_text(
                chat_id,
                "⚠️ 1. ana Max hesabı (/upstream_curl) kapatılamaz; yalnızca yeni cURL "
                "ile güncellenebilir. 2. hesabı kapatmak için: /upstream_curl2 kapat",
            )
            return

        if lowered in {"durum", "status", "bilgi"}:
            await self._show_accounts_status(chat_id)
            return

        if lowered in {"bitir", "bitti", "tamam", "son", "done"}:
            if sess.curl_chunks and sess.curl_target in {"unassigned_max", target}:
                sess.curl_target = target
            await self._finish_curl_collection(chat_id, sess)
            return

        if lowered in {"iptal", "temizle", "cancel"}:
            sess.clear_pending()
            await self.send_text(chat_id, "🧹 cURL toplama iptal edildi.")
            return

        sess.clear_pending()
        sess.mode = "collecting_curl"
        sess.curl_target = target
        sess.updated_at = time.monotonic()

        if raw_arg.strip():
            await self._append_curl_chunk(chat_id, sess, raw_arg)
            return

        extra_hint = (
            "\n(2. hesabı tamamen kapatmak isterseniz: /upstream_curl2 kapat)"
            if slot == 2
            else ""
        )
        await self.send_text(
            chat_id,
            f"📥 Max {slot}. Hesap ({cmd_name}) cURL toplama modu açıldı.\n"
            "Şimdi tarayıcıdan kopyaladığınız 'create-evaluation' cURL'ünü bölmeden "
            "doğrudan yapıştırın — Telegram mesajı 3-4 parçaya bölse de tamamını "
            "kayıpsız birleştireceğim.\n"
            f"Yapıştırma bitince /bitir yazın. İptal: /iptal{extra_hint}",
        )

    async def _close_upstream_slot_2(self, chat_id: int) -> None:
        try:
            response = await self.wrapper.post("/v1/autopilot/upstream-slots/2/close")
        except httpx.HTTPError as exc:
            logger.warning(
                "telegram_upstream_slot2_close_failed", error_type=type(exc).__name__
            )
            await self.send_text(
                chat_id,
                "Wrapper /v1/autopilot/upstream-slots/2/close ucuna ulaşılamadı.",
            )
            return

        if response.status_code != 200:
            await self.send_text(
                chat_id,
                f"❌ 2. Max hesabı kapatılamadı: {_autopilot_error_message(response)}",
            )
            return

        data = response.json()
        active_slots = data.get("active_slots") or []
        if not data.get("was_active"):
            await self.send_text(
                chat_id,
                "ℹ️ 2. Max hesabı (upstream_curl2) zaten kapalı; açık HTTP havuzu veya "
                f"keepalive görevi bulunmuyor (Aktif Max yuvaları: {active_slots}).",
            )
            return

        reassigned = int(data.get("reassigned_waiters") or 0)
        extra_line = (
            f"\n• Sıradaki {reassigned} istek kesintisiz 1. hesaba aktarıldı"
            if reassigned > 0
            else ""
        )
        await self.send_text(
            chat_id,
            "🔌 2. Max hesabı (upstream_curl2) kökten kapatıldı!\n"
            "• HTTP/2 bağlantı havuzu ve çerez kavanozu: Kapatıldı ve silindi ✅\n"
            "• Keepalive arka plan görevi: Durduruldu ✅\n"
            f"• Aktif kalan Max yuvaları: {active_slots} (yalnız 1. ana hesap)"
            f"{extra_line}\n\n"
            "🌙 Artık bu 2. Google hesabını çakışma olmadan /agent_curl ile gece "
            "Agent Mode projelerinde kullanabilirsiniz.",
        )

    async def _show_accounts_status(self, chat_id: int) -> None:
        try:
            response = await self.wrapper.get("/v1/autopilot/upstream-slots")
        except httpx.HTTPError as exc:
            logger.warning(
                "telegram_upstream_slots_status_failed", error_type=type(exc).__name__
            )
            await self.send_text(chat_id, "Hesap durumu ucuna ulaşılamadı.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        data = response.json()
        slots = data.get("slots") or []
        multi = bool(data.get("multi_lane"))
        active_ap = int(data.get("active_autopilot_runs") or 0)
        mode_str = "Çift Hesap (50/50 Yük Paylaşımı)" if multi else "Tek Hesap"
        lines = [f"🔑 Canlı Max Hesap Yuvaları — {mode_str}"]
        for item in slots:
            if not isinstance(item, dict):
                continue
            s_num = item.get("slot")
            cmd_tag = "/upstream_curl" if s_num == 1 else "/upstream_curl2"
            if not item.get("configured"):
                lines.append(
                    f"\n• Yuva {s_num} ({cmd_tag}): ⚫ Kapalı "
                    "(açık bağlantı ve keepalive yok — Agent için uygun)"
                )
                continue
            usable = bool(item.get("usable"))
            health_badge = "🟢 Sağlıklı" if usable else "🟠 Beklemede/Arızalı"
            fp = item.get("identity_fingerprint") or "kimlik-yok"
            ka_txt = "Açık" if item.get("keepalive_running") else "Kapalı"
            msgs = item.get("messages_in_window", 0)
            budget = item.get("effective_budget", 0)
            q_wait = item.get("queue_waiting", 0)
            lines.append(
                f"\n• Yuva {s_num} ({cmd_tag}): {health_badge} · {fp}\n"
                f"  Pencere: {msgs}/{budget} msj · Sıra bekleyen: {q_wait} · Keepalive: {ka_txt}"
            )
            if item.get("failure_code"):
                lines.append(f"  Son arıza kodu: {item['failure_code']}")
        lines.append(f"\n🤖 Aktif/Gece Sırasındaki Agent Projesi: {active_ap}")
        await self.send_text(chat_id, "\n".join(lines))

    async def _handle_curl_command(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        *,
        arg: str,
        raw_arg: str = "",
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
        sess.curl_target = "agent"
        sess.updated_at = time.monotonic()

        effective_raw = raw_arg if raw_arg.strip() else arg
        if effective_raw.strip():
            await self._append_curl_chunk(chat_id, sess, effective_raw)
            return

        await self.send_text(
            chat_id,
            "📥 Agent cURL toplama modu açıldı.\n"
            "Şimdi Agent Mode '/api/chat/<id>/input' cURL'ünü bölmeden doğrudan "
            "yapıştırın — Telegram mesajı 3-4 parçaya bölse de hepsini sırayla "
            "bekleyip birleştireceğim.\n"
            "Tüm parçalar gidince /bitir yazın. İptal: /iptal",
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
                "⚠️ cURL tampon sınırı aşıldı ve temizlendi. Baştan deneyin.",
            )
            return

        sess.curl_chunks.append(raw_chunk)
        sess.updated_at = time.monotonic()
        count = len(sess.curl_chunks)
        target_label = _CURL_TARGET_LABELS.get(sess.curl_target, "cURL")
        if sess.curl_target == "unassigned_max":
            await self.send_text(
                chat_id,
                f"📥 Max cURL parçası {count} alındı (toplam {total_after} krk).\n"
                "Tüm parçalar gidince hangi yuvaya kaydedileceğini seçerek bitirin:\n"
                "• 1. hesap için: /upstream_curl bitir\n"
                "• 2. hesap için: /upstream_curl2 bitir",
            )
            return

        prefix = (
            f"📥 {target_label} algılandı — "
            if auto_detected
            else f"📥 [{target_label}] "
        )
        await self.send_text(
            chat_id,
            f"{prefix}Parça {count} alındı (toplam {total_after} krk). "
            "Tüm parçalar gidince /bitir yazın.",
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
                "⚠️ Henüz hiç cURL parçası yapıştırılmadı. Önce /upstream_curl, "
                "/upstream_curl2 veya /agent_curl yazıp cURL'ü yapıştırın, ardından "
                "/bitir yazın.",
            )
            return

        if sess.curl_target == "unassigned_max":
            await self.send_text(
                chat_id,
                "⚠️ Yapıştırılan Max cURL'ünün hangi yuvaya yazılacağını seçin:\n"
                "• 1. ana hesap için: /upstream_curl bitir\n"
                "• 2. hesap için: /upstream_curl2 bitir\n"
                "(İptal: /iptal)",
            )
            return

        if sess.curl_target in {"upstream_1", "upstream_2"}:
            slot: Literal[1, 2] = 1 if sess.curl_target == "upstream_1" else 2
            await self._finish_upstream_slot_curl(chat_id, sess, slot=slot)
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
            sess.curl_target = "agent"
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                f"❌ Agent cURL doğrulanamadı: {_autopilot_error_message(response)}\n\n"
                "Tekrar cURL yapıştırıp /bitir yazabilir veya /iptal diyebilirsiniz.",
            )
            return

        data = response.json()
        if data.get("same_account_conflict"):
            conflict_slots = data.get("conflict_slots") or []
            sess.clear_pending()
            if 2 in conflict_slots and 1 not in conflict_slots:
                await self.send_text(
                    chat_id,
                    "⛔ Hesap Çakışması: Yapıştırdığınız Agent cURL'ü şu anda açık olan "
                    "2. Max hesabıyla (upstream_curl2) AYNI Google hesabına ait!\n\n"
                    "Önce 2. Max hesabının bağlantısını ve keepalive görevini kökten "
                    "kapatmak için:\n"
                    "👉 /upstream_curl2 kapat\n"
                    "yazın, ardından /agent_curl ile tekrar yapıştırın.",
                )
            else:
                await self.send_text(
                    chat_id,
                    "⛔ Hesap Çakışması: Yapıştırdığınız Agent cURL'ü sunucudaki 1. ana "
                    "Max hesabıyla (upstream_curl) AYNI Google hesabına ait! İki oturumun "
                    "birbirini düşürmemesi için Agent Mode cURL'ünü diğer Google "
                    "hesabından alıp /agent_curl ile tekrar deneyin.",
                )
            return

        live_checked = bool(data.get("live_checked"))
        live_ok = data.get("live_ok")
        cookie_diag = data.get("cookie_diagnostics") or {}
        if live_checked and live_ok is False:
            sess.mode = "collecting_curl"
            sess.curl_target = "agent"
            sess.updated_at = time.monotonic()
            status_code = data.get("live_status_code") or "?"
            err_type = data.get("live_error_type") or "Hata"
            err_detail = str(data.get("live_error_detail") or "").strip()
            problems = [str(p) for p in (cookie_diag.get("problems") or []) if p]
            diag_lines = [
                f"❌ Canlı Agent Oturumu Reddedildi (HTTP {status_code} · {err_type})!",
                f"• Sohbet ID: {data.get('chat_id', '-')}",
                f"• Çerez Sayısı: {cookie_diag.get('cookie_count', 0)} "
                f"(auth çerezi: {'var' if cookie_diag.get('auth_cookie_found') else 'YOK'}, "
                f"parçalar: {cookie_diag.get('auth_chunk_indices', [])})",
            ]
            if err_detail:
                diag_lines.append(f"• Sunucu Yanıtı: {err_detail}")
            if problems:
                diag_lines.append("• Tespit Edilen Sorun: " + " ".join(problems))
            diag_lines.append(
                "\n💡 Çözüm: Hedef sitede Agent Mode içinde temiz bir sohbet açıp "
                "ilk kısa mesajı gönderin, Network sekmesinden en güncel '/input' "
                "'Copy as cURL (bash)' çıktısını kopyalayıp tekrar yapıştırın ve "
                "/bitir yazın (İptal: /iptal)."
            )
            await self.send_text(chat_id, "\n".join(diag_lines), disable_preview=True)
            return

        sess.mode = "awaiting_idea"
        sess.validated_curl = joined
        sess.validated_chat_id = str(data.get("chat_id") or "")
        sess.validated_domain = str(data.get("target_domain") or "")
        sess.updated_at = time.monotonic()

        warnings = [str(w) for w in (data.get("warnings") or []) if w]
        if live_checked and live_ok is None and data.get("live_error_detail"):
            warnings.append(
                f"Canlı ön kontrol zaman aşımı/ağ uyarısı verdi ({data.get('live_error_type')}); "
                "cURL sözdizimi geçerli olduğu için kaydedildi."
            )
        warn_block = ("\n⚠️ Not: " + " ".join(warnings)) if warnings else ""
        rem_s = cookie_diag.get("token_remaining_seconds")
        ttl_txt = (
            f", JWT ~{max(1, int(rem_s) // 60)} dk"
            if isinstance(rem_s, int) and rem_s > 0
            else ""
        )
        chunk_indices = cookie_diag.get("auth_chunk_indices") or []
        chunks_txt = (
            f"parçalar: {chunk_indices}"
            if chunk_indices
            else ("auth: var" if cookie_diag.get("auth_cookie_found") else "auth: YOK")
        )
        cookie_line = (
            f"\n• Çerez: {cookie_diag.get('cookie_count', 0)} adet "
            f"({chunks_txt}{ttl_txt})"
            if cookie_diag
            else ""
        )
        live_badge = ""
        if live_checked and live_ok is True:
            msg_cnt = data.get("live_message_count", 0)
            rot_txt = (
                " · güncel çerez korundu"
                if data.get("cookie_rotated_on_probe")
                else ""
            )
            live_badge = f"\n• Canlı Oturum: 🟢 200 OK ({msg_cnt} mesaj{rot_txt})"
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
            f"• Kaynak Türü: {data.get('source_endpoint_kind', 'unknown')}"
            f"{cookie_line}"
            f"{live_badge}\n"
            f"• Tur Planı: {turn_label}\n"
            f"• Çalışma Zamanı: {schedule_label}"
            f"{warn_block}\n\n"
            "✏️ Şimdi yaptırmak istediğiniz **proje fikrini** yazıp gönderin\n"
            "🔄 (Yarıda kalan aynı projeyi sürdürmek için fikir yazmadan "
            "`/devam` veya hemen başlatmak için `/devam simdi` yazabilirsiniz).",
            disable_preview=True,
        )

    async def _handle_agent_test(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
    ) -> None:
        if not sess.validated_curl:
            await self.send_text(
                chat_id,
                "ℹ️ Bellekte test edilecek hazır bir Agent cURL yok.\n"
                "Önce /agent_curl yazıp cURL'ü yapıştırın ve /bitir ile canlı doğrulayın.",
            )
            return
        try:
            response = await self.wrapper.post(
                "/v1/autopilot/inspect-curl",
                json={"agent_curl": sess.validated_curl, "verify_live": True},
            )
        except httpx.HTTPError:
            await self.send_text(
                chat_id,
                "Wrapper /v1/autopilot/inspect-curl ucuna ulaşılamadı.",
            )
            return
        if response.status_code != 200:
            await self.send_text(
                chat_id,
                f"❌ Agent cURL testi başarısız: {_autopilot_error_message(response)}",
            )
            return
        data = response.json()
        diag = data.get("cookie_diagnostics") or {}
        if data.get("live_checked") and data.get("live_ok") is False:
            await self.send_text(
                chat_id,
                f"🔴 Canlı Agent oturumu düşmüş (HTTP {data.get('live_status_code', '?')} · "
                f"{data.get('live_error_type', 'Hata')}).\n"
                f"Detay: {data.get('live_error_detail', '-')}\n"
                "Lütfen /agent_curl ile yeni bir cURL yapıştırın.",
                disable_preview=True,
            )
            return
        await self.send_text(
            chat_id,
            f"🟢 Agent oturumu hazır!\n"
            f"• Sohbet: {data.get('chat_id', '-')}\n"
            f"• Canlı Durum: {'200 OK' if data.get('live_ok') else 'Sözdizimi Geçerli'}\n"
            f"• Çerez Sayısı: {diag.get('cookie_count', 0)} "
            f"(auth parçaları: {diag.get('auth_chunk_indices', [])})",
            disable_preview=True,
        )

    async def _finish_upstream_slot_curl(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        *,
        slot: Literal[1, 2],
    ) -> None:
        chunk_count = len(sess.curl_chunks)
        joined = reassemble_upstream_curl_chunks(sess.curl_chunks)
        sess.curl_chunks.clear()
        cmd_name = "/upstream_curl" if slot == 1 else "/upstream_curl2"

        try:
            response = await self.wrapper.post(
                f"/v1/autopilot/upstream-slots/{slot}",
                json={"upstream_curl": joined},
            )
        except httpx.HTTPError as exc:
            sess.clear_pending()
            logger.warning(
                "telegram_upstream_slot_update_failed",
                slot=slot,
                error_type=type(exc).__name__,
            )
            await self.send_text(
                chat_id,
                f"Wrapper /v1/autopilot/upstream-slots/{slot} ucuna ulaşılamadı.",
            )
            return

        if response.status_code != 200:
            sess.mode = "collecting_curl"
            sess.curl_target = "upstream_1" if slot == 1 else "upstream_2"
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                f"❌ Max {slot}. Hesap ({cmd_name}) doğrulanamadı: "
                f"{_autopilot_error_message(response)}\n\n"
                "Tekrar cURL yapıştırıp /bitir yazabilir veya /iptal diyebilirsiniz.",
            )
            return

        sess.clear_pending()
        data = response.json()
        active_slots = data.get("active_slots") or []
        multi_lane = bool(data.get("multi_lane"))
        fp = data.get("identity_fingerprint") or "kimlik-yok"
        label = data.get("label") or f"hesap-{slot}"
        lane_txt = (
            "Çift Hat (50/50 Yük Paylaşımı)"
            if multi_lane
            else "Tek Hat"
        )
        close_note = (
            "\n🌙 Akşam bu hesabı Agent Mode'a devretmeden önce "
            "'/upstream_curl2 kapat' yazmanız yeterli."
            if slot == 2
            else ""
        )
        await self.send_text(
            chat_id,
            f"✅ Max {slot}. Hesap ({cmd_name}) canlı olarak kaydedildi "
            f"({chunk_count} parça birleştirildi)!\n"
            f"• Hesap: {label} ({fp})\n"
            f"• Aktif Max Yuvaları: {active_slots} — {lane_txt}"
            f"{close_note}",
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
        arg: str = "",
    ) -> None:
        if arg.strip().lower() in {"kapat", "off", "hayir", "hayır", "kaldir", "kaldır"}:
            sess.respect_night_window = False
            sess.updated_at = time.monotonic()
            await self.send_text(
                chat_id,
                "☀️ Gece penceresi kapatıldı: projeler gönderildiği an başlar. "
                "Süreklilik için varsayılan davranış budur.",
            )
            return
        sess.respect_night_window = True
        sess.updated_at = time.monotonic()
        await self.send_text(
            chat_id,
            "🌙 Gece çalışma penceresi aktif: Başlatacağınız proje yalnızca "
            "22:00–07:00 (TR) saatleri arasında çalışacak; gündüz gönderirseniz "
            "Max'a dokunmadan 22:00'yi bekleyecek.\n"
            "Geri almak için /gece kapat yazın (varsayılan: bekleme yok).",
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

    async def _handle_rotation(self, chat_id: int, arg: str) -> None:
        """`/rotasyon` — iki Max yuvasının rollerini takas eder.

        Üç hesap dönüşümünde hiçbir hesabın tamamen tükenmemesi için roller
        döndürülür: Max-1 / Max-2 / yedek (3. yuva) / Agent. Argümansız çağrı
        en güvenli takası önerir: 1 ↔ 2 (trafigin çoğu iki Max hesabında kalsın).
        """
        tokens = [t.strip() for t in arg.split() if t.strip()]
        slots: list[int] = []
        for token in tokens[:2]:
            try:
                slots.append(int(token))
            except ValueError:
                await self.send_text(
                    chat_id,
                    "Kullanım: /rotasyon [yuva_a yuva_b] — örn. /rotasyon 1 2",
                )
                return
        if not slots:
            slots = [1, 2]
        if len(slots) == 1:
            slots.append(1 if slots[0] != 1 else 2)

        try:
            resp = await self.wrapper.post(
                "/v1/autopilot/upstream-slots/swap",
                json={"slot_a": slots[0], "slot_b": slots[1]},
            )
        except httpx.HTTPError:
            await self.send_text(
                chat_id, "Wrapper /v1/autopilot/upstream-slots/swap ucuna ulaşılamadı."
            )
            return
        if resp.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(resp))
            return

        data = resp.json()
        roles = data.get("roles") or {}
        primary = roles.get("max_primary") or {}
        secondary = roles.get("max_secondary") or {}
        spare = roles.get("max_spare") or {}
        await self.send_text(
            chat_id,
            f"🔁 Yuvalar takas edildi: {slots[0]} ↔ {slots[1]}\n"
            f"• Max-1 (ana): {primary.get('label') or 'boş'}\n"
            f"• Max-2: {secondary.get('label') or 'boş'}\n"
            f"• Yedek (3. yuva): {spare.get('label') or 'boş'}\n"
            "Hesaplar tükenmesin diye rolleri /rotasyon ile dönüşümlü tutabilirsiniz.",
        )

    async def _handle_resume_command(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        arg: str,
    ) -> None:
        if not sess.validated_curl:
            await self.send_text(
                chat_id,
                "🔄 Yarıda kalan bir projeyi sürdürmek için önce aynı Agent sohbetinden "
                "taze bir `/input` cURL'ünü `.txt` dosyası olarak gönderip `/bitir` yazın;\n"
                "ardından `/devam` (geceyi beklemeden hemen başlatmak için `/devam simdi`) yazın.",
            )
            return

        tokens = [t.strip() for t in arg.split() if t.strip()]
        now_keywords = {"simdi", "şimdi", "hemen", "now"}
        force_now = any(t.lower() in now_keywords for t in tokens)
        explicit_ids = [t for t in tokens if t.lower() not in now_keywords]
        target_id = explicit_ids[0] if explicit_ids else ""

        if not target_id:
            try:
                list_resp = await self.wrapper.get("/v1/autopilot/runs")
            except httpx.HTTPError:
                await self.send_text(chat_id, "Wrapper /v1/autopilot/runs ucuna ulaşılamadı.")
                return
            if list_resp.status_code != 200:
                await self.send_text(chat_id, _autopilot_error_message(list_resp))
                return

            runs = [
                r for r in (list_resp.json().get("runs") or []) if isinstance(r, dict)
            ]
            resumable = [
                r
                for r in runs
                if str(r.get("status") or "") in {"failed", "stopped", "max_turns_reached"}
            ]
            same_chat = [
                r
                for r in resumable
                if sess.validated_chat_id
                and str(r.get("chat_id") or "") == sess.validated_chat_id
            ]
            if same_chat:
                target_id = str(same_chat[0].get("run_id") or "")
            elif len(resumable) == 1:
                target_id = str(resumable[0].get("run_id") or "")
            elif len(resumable) > 1:
                choices = "\n".join(
                    f"• `/devam {r.get('run_id')}` ({r.get('project_idea', '')[:45]})"
                    for r in resumable[:5]
                )
                await self.send_text(
                    chat_id,
                    "Birden fazla durmuş proje var; hangisini sürdüreceğinizi seçin:\n"
                    f"{choices}",
                )
                return
            else:
                if force_now:
                    sess.respect_night_window = False
                await self.send_text(
                    chat_id,
                    "ℹ️ Sunucu belleğinde yarım kalmış eski koşu kaydı bulunmuyor "
                    "(sunucu yeni deploy edilmiş olabilir); ancak taze Agent cURL'ünüz hazır!\n\n"
                    "Şimdi proje fikrini tek mesaj olarak gönderin — sistem Agent sohbetindeki "
                    "mevcut ilerlemeyi otomatik okuyup sıfırdan başlamadan kaldığı yerden "
                    "devam edecektir"
                    + (
                        " (⚡ hemen başlatma aktif)."
                        if not sess.respect_night_window
                        else " (hemen başlatmak için önce `/simdi` yazabilirsiniz)."
                    ),
                )
                return

        payload: dict[str, Any] = {
            "agent_curl": sess.validated_curl,
        }
        if force_now or not sess.respect_night_window:
            payload["respect_night_window"] = False

        try:
            resp = await self.wrapper.post(
                f"/v1/autopilot/runs/{target_id}/resume",
                json=payload,
            )
        except httpx.HTTPError:
            await self.send_text(
                chat_id,
                "Wrapper resume ucuna ulaşılamadı; cURL bellekte korundu, tekrar `/devam` "
                "deneyebilirsiniz.",
            )
            return

        if resp.status_code != 200:
            await self.send_text(
                chat_id,
                f"❌ Devam başlatılamadı: {_autopilot_error_message(resp)}",
            )
            return

        rdata = resp.json()
        sess.clear_pending()
        agent_url = str(rdata.get("agent_url") or "").strip()
        agent_line = f"• Agent: {agent_url}\n" if agent_url else ""
        night_on = bool(rdata.get("respect_night_window"))
        sched_txt = (
            f"🌙 Gece Penceresi (hemen başlatmak için: /simdi {target_id})"
            if night_on
            else "⚡ Hemen Devam Ediyor"
        )
        await self.send_text(
            chat_id,
            f"🔄 Otopilot projesi kaldığı yerden sürdürülüyor!\n"
            f"• Run ID: {target_id}\n"
            f"{agent_line}"
            f"• Kayıtlı Tur: {rdata.get('turn_count', 0)}/{rdata.get('max_turns', '?')} "
            "(Agent tur süre sınırı yok)\n"
            f"• Zamanlama: {sched_txt}\n\n"
            f"Durum takibi: /durum {target_id}",
            disable_preview=True,
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
            elif item.get("morning_report_pending"):
                block.append(f"  Sabah Raporu: 🌅 Hazır (07:00'de iletilecek · /rapor {rid})")
            elif (item.get("stop_reason") or item.get("error_detail")) and st != "running":
                raw_outcome = item.get("stop_reason") or item.get("error_detail") or ""
                reason_short = " ".join(str(raw_outcome).split())[:150]
                block.append(f"  Sonuç: {reason_short}")
            lines.extend(block)

        lines.append("\nDetay: /durum <run_id> · Temizlik: /temizle · Yeni proje: /curl")
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
            lines.append("\n📜 Son Turlar (ne oldu → ne istendi):")
            for t in turns[-4:]:
                if not isinstance(t, dict):
                    continue
                duration = t.get("duration_seconds") or 0
                # Tur süresini dakika olarak da yaz: "1650 sn" kimseye bir şey
                # anlatmıyordu, "27 dk" anlatıyor.
                dur_txt = (
                    f"{duration / 60:.0f} dk"
                    if isinstance(duration, (int, float)) and duration >= 90
                    else f"{duration} sn"
                )
                lines.append(
                    f"  - Tur {t.get('turn')}: [{t.get('phase')}] {dur_txt} · "
                    f"{t.get('tool_count')} araç ({t.get('bash_count')} bash, "
                    f"{t.get('error_tool_count')} hata)"
                )
                done_before = _first_lines(t.get("sent_to_agent"), 1, 120)
                if done_before:
                    lines.append(f"      ↳ istendi: {done_before}")
            # Son turun somut kanıtı: "gerçekten bir şey yapıldı mı" sorusunun
            # cevabı yalnız burada görünüyordu.
            last = turns[-1] if isinstance(turns[-1], dict) else {}
            evidence = _first_lines(last.get("sandbox_evidence"), 2, 160)
            if evidence:
                lines.append(f"\n🔬 Son tur kanıtı:\n{evidence}")

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

    async def _stop_project(self, chat_id: int, raw_arg: str) -> None:
        tokens = [t.strip() for t in raw_arg.split() if t.strip()]
        lowered_tokens = [t.lower() for t in tokens]
        if lowered_tokens and lowered_tokens[0] in {"hepsi", "tümü", "tumu", "all", "*"}:
            try:
                resp = await self.wrapper.post(
                    "/v1/autopilot/runs/stop-all",
                    json={"force": True},
                )
            except httpx.HTTPError as exc:
                logger.warning("telegram_autopilot_stop_all_failed", error_type=type(exc).__name__)
                await self.send_text(chat_id, "Toplu durdurma isteği gönderilemedi.")
                return
            if resp.status_code != 200:
                await self.send_text(chat_id, _autopilot_error_message(resp))
                return
            data = resp.json()
            cnt = data.get("stopped_count", 0)
            ids = data.get("stopped_run_ids") or []
            if not cnt:
                await self.send_text(chat_id, "ℹ️ Şu anda aktif veya bekleyen otopilot projesi yok.")
                return
            await self.send_text(
                chat_id,
                f"🛑 Toplam {cnt} aktif/bekleyen otopilot projesi anında durduruldu: "
                f"{', '.join(str(x) for x in ids)}\n"
                "(Kayıtları da tamamen silmek isterseniz /temizle yazabilirsiniz.)",
            )
            return

        force_stop = any(t in {"zorla", "force", "hemen", "acil"} for t in lowered_tokens)
        id_tokens = [
            orig
            for orig, low in zip(tokens, lowered_tokens, strict=False)
            if low not in {"zorla", "force", "hemen", "acil"}
        ]
        target_id = id_tokens[0] if id_tokens else ""

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
                    "Birden fazla aktif proje var; hangisini durduracağınızı belirtin "
                    "(veya tümünü kesmek için `/durdur hepsi` yazın):\n"
                    + "\n".join(f"• /durdur {rid}" for rid in active),
                )
                return
            target_id = active[0]

        stop_path = (
            f"/v1/autopilot/runs/{target_id}/stop?force=true"
            if force_stop
            else f"/v1/autopilot/runs/{target_id}/stop"
        )
        try:
            response = await self.wrapper.post(stop_path)
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_stop_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Durdurma isteği gönderilemedi.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        if force_stop:
            await self.send_text(
                chat_id,
                f"🛑 {target_id} anında iptal edildi ve bağlantıları kapatıldı.",
            )
        else:
            await self.send_text(
                chat_id,
                f"🛑 {target_id} için durdurma işareti gönderildi. Mevcut adım tamamlandığında "
                "güvenli biçimde duracak (anında kesmek için: `/durdur zorla "
                f"{target_id}`).",
            )

    async def _clear_projects(
        self,
        chat_id: int,
        sess: _ChatAutopilotSession,
        raw_arg: str,
    ) -> None:
        cleaned = raw_arg.strip()
        lowered = cleaned.lower()
        include_active = True
        run_id: str | None = None

        if lowered in {"", "hepsi", "tümü", "tumu", "all", "*"}:
            include_active = True
        elif lowered in {"bitenler", "biten", "gecmis", "geçmiş", "done", "hatalilar"}:
            include_active = False
        else:
            run_id = cleaned.split()[0]

        # Yalnız genel temizlikte bekleyen yarım cURL/fikir oturumunu da sıfırla
        had_pending_session = sess.mode != "idle" or bool(sess.validated_curl)
        if run_id is None and include_active:
            sess.clear_pending()

        payload: dict[str, Any] = {"include_active": include_active}
        if run_id is not None:
            payload["run_id"] = run_id

        try:
            response = await self.wrapper.post("/v1/autopilot/runs/clear", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("telegram_autopilot_clear_failed", error_type=type(exc).__name__)
            await self.send_text(chat_id, "Wrapper /v1/autopilot/runs/clear ucuna ulaşılamadı.")
            return

        if response.status_code != 200:
            await self.send_text(chat_id, _autopilot_error_message(response))
            return

        data = response.json()
        if run_id is not None and not data.get("found", True):
            await self.send_text(
                chat_id,
                f"⚠️ '{run_id}' ID'li bir otopilot kaydı bulunamadı (/projeler ile kontrol edin).",
            )
            return

        stopped_cnt = int(data.get("stopped_active") or 0)
        removed_cnt = int(data.get("removed_runs") or 0)
        remaining_cnt = int(data.get("remaining_runs") or 0)
        morning_cnt = int(data.get("cancelled_morning_reports") or 0)

        extra_bits: list[str] = []
        if stopped_cnt:
            extra_bits.append(f"{stopped_cnt} çalışan/bekleyen görev anında durduruldu")
        if morning_cnt:
            extra_bits.append(f"{morning_cnt} bekleyen sabah raporu iptal edildi")
        if had_pending_session and run_id is None and include_active:
            extra_bits.append("bekleyen cURL/proje girişi sıfırlandı")

        detail_line = (" (" + ", ".join(extra_bits) + ")") if extra_bits else ""
        await self.send_text(
            chat_id,
            f"🧹 Otopilot temizliği tamamlandı: {removed_cnt} proje kaydı silindi"
            f"{detail_line}.\n"
            f"• Kalan otopilot kaydı: {remaining_cnt}\n"
            "• 1. ana Max hesabı (hesap-1) korunmaya devam ediyor.",
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
