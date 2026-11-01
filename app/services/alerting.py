"""Arıza bildirimleri: Telegram'a **anlamlı** ve **spam olmayan** mesaj.

Saatlik probe (veya keepalive) bir isteği uçtan uca dener; bu modül sonucu
yorumlayıp yalnızca gerçekten haber değeri olan anlarda Telegram'a yazar:

* **Yeni arıza** (ilk kez görülen hata kodu) → hemen bildirilir.
* **Aynı arıza devam ediyor** → en fazla `TELEGRAM_ALERT_REPEAT_SECONDS` (vars. 6 saat)
  aralıkla tek bir hatırlatma; yoksa saatlik mesajlar spam'e dönüşür.
* **Arıza kodu değişti** (örn. kota → captcha) → haberdar edilir.
* **Düzeldi** → tek bir "toparlandı" mesajı ve ne kadar sürdüğü bilgisi.

Metinler `app/core/diagnostics.py` üzerinden üretilir: aynı kod her yerde aynı
cümleyle anlatılır, hiçbir gizli değer (cookie/token) mesaja girmez.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.core.config import Settings
from app.core.diagnostics import explain_failure
from app.core.logging import get_logger

logger = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"

#: Telegram mesajı için "insan" okunur zaman.
def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} sn"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} dk"
    hours, minutes = divmod(minutes, 60)
    if hours < 36:
        return f"{hours} sa {minutes} dk" if minutes else f"{hours} sa"
    days, hours = divmod(hours, 24)
    return f"{days} gün {hours} sa" if hours else f"{days} gün"


@dataclass(slots=True)
class ProbeResult:
    """Uçtan uca probe'un (veya keepalive isteğinin) özeti."""

    ok: bool
    status_code: int | None = None
    #: Servisin döndürdüğü OpenAI hata kodu (örn. ``upstream_refresh_required``).
    code: str = ""
    duration_ms: float = 0.0
    checked_at: str = field(default_factory=_stamp)

    @property
    def key(self) -> str:
        """Bildirim durumu için kararlı anahtar."""
        if self.ok:
            return "ok"
        if self.code:
            return self.code
        return f"http_{self.status_code}" if self.status_code else "unknown"


@dataclass(slots=True)
class _AlertState:
    """Bildirim seyreltme durumu."""

    active_key: str | None = None
    active_since: float = 0.0
    last_sent_at: float = 0.0
    failures: int = 0
    total_alerts: int = 0


class AlertPolicy:
    """Hangi durumda mesaj üretileceğine karar verir (yan etkisiz)."""

    def __init__(self, repeat_seconds: float = 21600.0) -> None:
        self.repeat_seconds = max(0.0, float(repeat_seconds))
        self.state = _AlertState()

    def evaluate(
        self, result: ProbeResult, now: float | None = None
    ) -> str | None:
        """Mesaj metni döndürür; sessiz kalınacaksa ``None``."""
        current = time.monotonic() if now is None else now
        state = self.state

        if result.ok:
            if state.active_key is None:
                return None
            previous = state.active_key
            elapsed = current - state.active_since
            state.active_key = None
            state.failures = 0
            state.active_since = 0.0
            hint = explain_failure(result.status_code, previous)
            return (
                "✅ ApiWrapper — upstream yeniden çalışıyor.\n"
                f"Önceki arıza: {previous} ({hint.title})\n"
                f"Süre: {_human_duration(elapsed)} · {result.checked_at} · "
                f"HTTP {result.status_code if result.status_code is not None else '-'}"
            )

        key = result.key
        hint = explain_failure(result.status_code, result.code)

        if state.active_key != key:
            # Yeni arıza ya da arıza türü değişti: her zaman bildir.
            if state.active_key is None:
                state.active_since = current
                state.failures = 0
            state.active_key = key
            state.failures += 1
            state.last_sent_at = current
            state.total_alerts += 1
            severity = "🔴" if hint.severity == "critical" else "⚠️"
            return (
                f"{severity} ApiWrapper — upstream isteği başarısız.\n"
                f"Ne oldu: {hint.title} [{key}]\n"
                f"Yapılacak: {hint.action}\n"
                f"HTTP {result.status_code if result.status_code is not None else '-'} · "
                f"{result.checked_at} · {_human_duration(result.duration_ms / 1000)}"
            )

        # Aynı arıza sürüyor: yalnızca hatırlatma aralığı dolduysa yaz.
        state.failures += 1
        if self.repeat_seconds <= 0 or current - state.last_sent_at >= self.repeat_seconds:
            state.last_sent_at = current
            state.total_alerts += 1
            return (
                "⏳ ApiWrapper — arıza sürüyor.\n"
                f"Ne oldu: {hint.title} [{key}]\n"
                f"Yapılacak: {hint.action}\n"
                f"Üst üste {state.failures}. başarısız deneme · {result.checked_at} · "
                f"başlangıçtan {_human_duration(current - state.active_since)} "
                "önce başladı"
            )
        return None


class TelegramAlerter:
    """AlertPolicy + Telegram Bot API gönderimi."""

    def __init__(
        self,
        bot_token: str,
        chat_id: int,
        *,
        repeat_seconds: float = 21600.0,
        timeout: float = 10.0,
    ) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id
        self.timeout = timeout
        self.policy = AlertPolicy(repeat_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token) and self.chat_id is not None

    def evaluate(self, result: ProbeResult, now: float | None = None) -> str | None:
        return self.policy.evaluate(result, now)

    async def notify(self, result: ProbeResult) -> str | None:
        """Gerekliyse mesajı gönderir; gönderilen metni döndürür."""
        text = self.evaluate(result)
        if text is None:
            logger.debug(
                "telegram_alert_suppressed",
                code=result.key,
                failures=self.policy.state.failures,
            )
            return None
        await self.send(text)
        return text

    async def send(self, text: str) -> bool:
        url = f"{_TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
        except Exception as exc:
            # URL token içerir: hata metni yerine yalnızca tür loglanır.
            logger.warning("telegram_alert_failed", error_type=type(exc).__name__)
            return False
        if response.status_code >= 400:
            logger.warning("telegram_alert_failed", status_code=response.status_code)
            return False
        logger.info(
            "telegram_alert_sent",
            code=self.policy.state.active_key or "recovered",
            alerts=self.policy.state.total_alerts,
        )
        return True


def build_alerter(settings: Settings) -> TelegramAlerter | None:
    """Ayarlardan Telegram bildirimcisi kurar; eksikse ``None`` döndürür.

    Bildirimci, gömülü Telegram botundan bağımsızdır: bot kapalıyken de (kota
    tasarrufu için) arıza bildirimi alınabilir.
    """
    if not settings.telegram_alerts_enabled:
        logger.info("telegram_alerts_disabled")
        return None

    # Döngüsel import yok: TelegramSettings yapılandırmayı ayrı okur.
    from app.telegram.bot import TelegramSettings

    try:
        telegram = TelegramSettings()
    except ValueError:
        logger.warning("telegram_alerts_configuration_invalid")
        return None

    token = telegram.telegram_bot_token.strip()
    chat_id = telegram.alert_chat_id
    if not token or chat_id is None:
        logger.info(
            "telegram_alerts_not_configured",
            missing=[
                name
                for name, ok in (
                    ("TELEGRAM_BOT_TOKEN", bool(token)),
                    ("TELEGRAM_ALERT_CHAT_ID/TELEGRAM_ALLOWED_CHAT_IDS", chat_id is not None),
                )
                if not ok
            ],
        )
        return None

    logger.info("telegram_alerts_enabled", repeat_seconds=settings.telegram_alert_repeat_seconds)
    return TelegramAlerter(
        token,
        chat_id,
        repeat_seconds=settings.telegram_alert_repeat_seconds,
    )
