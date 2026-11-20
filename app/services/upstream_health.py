"""Upstream'in **bilinen** durumu: son başarı anı ve "arıza biliniyor" bekletmesi.

Bütün tamamlamalar (dış sistem, Telegram mesajları, probe) sonucunu buraya
bildirir. İki tüketicisi vardır:

* **`/v1/ask`** — arıza biliniyorken gelen isteği upstream'e hiç göndermeden
  "problem" diye cevaplar; karşı sistem beklemeden yedeğine geçer. Bekletme
  süresi dolunca **tek** istek deneme olarak geçirilir (yarı-açık); deneme
  sürerken gelenler yine hemen "problem" alır.
* **Keepalive** — son başarılı isteğin üzerinden bir aralık geçmediyse probe
  atmaz (gerçek trafik oturumu zaten tazelemiştir).

Bekletme yalnız `/v1/ask` isteklerini etkiler. `/v1/chat/completions` (Telegram
mesajları, probe) her zaman denenir; onların başarısı bekletmeyi anında kaldırır.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Deneme isteği sonucunu hiç bildirmezse (iptal, çökme) hakkın geri alınacağı süre.
_TRIAL_TTL_SECONDS = 900.0


@dataclass(frozen=True, slots=True)
class Hold:
    """Bir isteğin neden upstream'e gönderilmediği."""

    #: Bekletmeyi başlatan arıza kodu (örn. ``upstream_quota_reached``).
    code: str
    #: Bir sonraki denemeye kalan süre (saniye); deneme sürüyorsa 0.
    remaining: float
    #: Arızanın ilk görüldüğü andan bu yana geçen süre (saniye).
    failing_for: float
    #: True ise bekletme dolmuş ama başka bir istek şu an deneme olarak işleniyor.
    trial_in_flight: bool = False


class UpstreamHealth:
    """Son başarı/arıza kaydı ve yarı-açık bekletme."""

    def __init__(
        self,
        hold_seconds: float = 300.0,
        *,
        max_hold_seconds: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hold_seconds = max(0.0, float(hold_seconds))
        self.max_hold_seconds = max(self.hold_seconds, float(max_hold_seconds))
        self._clock = clock
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._failing_since: float | None = None
        self._failures = 0
        self._code = ""
        self._hold_until = 0.0
        self._trial_owner: object | None = None
        self._trial_started_at = 0.0

    # ---------------------------------------------------------------- kayıt
    def record_success(self) -> None:
        """Bir tamamlama başarıyla bitti: arıza kaydı ve bekletme kalkar."""
        now = self._clock()
        if self._failing_since is not None:
            logger.info(
                "upstream_hold_cleared",
                code=self._code or None,
                failures=self._failures,
                failing_seconds=round(now - self._failing_since, 1),
            )
        self._last_success_at = now
        self._failing_since = None
        self._failures = 0
        self._code = ""
        self._hold_until = 0.0
        self._trial_owner = None

    def record_failure(self, code: str, retry_after: float | None = None) -> float:
        """Upstream kaynaklı bir arıza görüldü; uygulanan bekletmeyi döndürür.

        ``retry_after`` (örn. hesabın kalan dinlenme süresi) taban süreden uzunsa
        o kullanılır: sonucu şimdiden belli bir denemeyi tekrarlamanın anlamı yok.
        """
        now = self._clock()
        hold = self.hold_seconds
        if hold > 0 and retry_after is not None and retry_after > hold:
            hold = min(float(retry_after), self.max_hold_seconds)
        first = self._failing_since is None
        changed = code != self._code
        if first:
            self._failing_since = now
        self._failures += 1
        self._code = code
        self._last_failure_at = now
        self._hold_until = now + hold
        self._trial_owner = None
        if hold > 0 and (first or changed):
            logger.warning(
                "upstream_hold_started",
                code=code,
                hold_seconds=round(hold, 1),
                hint=(
                    "Arıza biliniyor: /v1/ask istekleri bu süre boyunca upstream'e "
                    "gönderilmeden 'problem' diye cevaplanır; sonra tek istek denenir."
                ),
            )
        return hold

    # --------------------------------------------------------------- sorgu
    @property
    def last_success_at(self) -> float | None:
        """Son başarılı tamamlamanın anı (``clock`` ölçeğinde); hiç yoksa ``None``."""
        return self._last_success_at

    @property
    def failing(self) -> bool:
        return self._failing_since is not None

    @property
    def code(self) -> str:
        """Süren arızanın kodu (arıza yoksa boş)."""
        return self._code

    def since_last_failure(self) -> float | None:
        """Son arızanın üzerinden geçen süre (saniye); arıza yoksa ``None``."""
        if self._failing_since is None or self._last_failure_at is None:
            return None
        return max(0.0, self._clock() - self._last_failure_at)

    def reset(self) -> None:
        """Arıza kaydını elle kaldırır (operatör kararı); son başarı anına dokunmaz."""
        if self._failing_since is not None:
            logger.info("upstream_hold_reset", code=self._code or None, failures=self._failures)
        self._failing_since = None
        self._failures = 0
        self._code = ""
        self._hold_until = 0.0
        self._trial_owner = None

    def blocked(self) -> Hold | None:
        """Bekletme **şu an** sürüyor mu? (Durum değiştirmez.)"""
        if self._failing_since is None or self.hold_seconds <= 0:
            return None
        now = self._clock()
        remaining = self._hold_until - now
        if remaining <= 0:
            return None
        return Hold(self._code, remaining, now - self._failing_since)

    def peek(self, owner: object) -> Hold | None:
        """`admit` ile aynı kararı verir ama durum değiştirmez (deneme hakkı almaz)."""
        if self._failing_since is None or self.hold_seconds <= 0:
            return None
        now = self._clock()
        remaining = self._hold_until - now
        if remaining > 0:
            return Hold(self._code, remaining, now - self._failing_since)
        if (
            self._trial_owner is not None
            and self._trial_owner is not owner
            and now - self._trial_started_at < _TRIAL_TTL_SECONDS
        ):
            return Hold(self._code, 0.0, now - self._failing_since, trial_in_flight=True)
        return None

    def admit(self, owner: object) -> Hold | None:
        """Bir `/v1/ask` isteği upstream'e gidebilir mi?

        ``None`` → gidebilir. Arıza biliniyorsa ve bekletme dolduysa çağıran
        **deneme** isteği olur; sonucu bildirene (ya da `release_trial` çağırana)
        dek diğer istekler `Hold` alır. Bekletme kapalıysa (``hold_seconds`` 0)
        her istek denenir.
        """
        hold = self.peek(owner)
        if hold is not None or self._failing_since is None or self.hold_seconds <= 0:
            return hold
        if self._trial_owner is not owner:
            self._trial_owner = owner
            self._trial_started_at = self._clock()
            logger.info("upstream_trial_started", code=self._code or None)
        return None

    def release_trial(self, owner: object) -> None:
        """Deneme sonuç bildirmeden bitti (iptal vb.): hakkı sıradaki isteğe bırak."""
        if self._trial_owner is owner:
            self._trial_owner = None

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        hold = self.blocked()
        return {
            "failing": self.failing,
            "code": self._code or None,
            "failures": self._failures,
            "failing_seconds": (
                round(now - self._failing_since, 1) if self._failing_since is not None else None
            ),
            "hold_remaining_seconds": round(hold.remaining, 1) if hold is not None else 0.0,
            "trial_in_flight": self._trial_owner is not None,
            "hold_seconds": self.hold_seconds,
            "seconds_since_success": (
                round(now - self._last_success_at, 1)
                if self._last_success_at is not None
                else None
            ),
        }
