"""Tek sıra + istekler arası rastgele bekleme.

Upstream'e giden her tamamlama bu kapıdan geçer:

* Aynı anda **bir** istek işlenir; diğerleri geliş sırasıyla (FIFO) bekler.
* Bir istek bitince sıradaki, ``min_gap``–``max_gap`` aralığından rastgele
  seçilen süre kadar bekletilir. Aralık yalnız upstream'e gerçekten gidilmiş
  isteklerden sonra uygulanır (``Ticket.touched``); yerelde reddedilen istek
  kimseyi bekletmez.

Sıra bütün trafiğin önündeki tek kilittir, yani tek hata noktasıdır. Bu yüzden
üç emniyet vardır:

* **İptal güvenliği:** bekleyen görev iptal edilirse sıradan çıkar; sıra tam o
  anda kendisine verilmişse bir sonrakine devredilir.
* **Bekleme sınırları:** ``max_waiting`` (derinlik), ``max_wait`` (süre) ve
  çağıranın ``should_leave`` koşulu.
* **Sızıntı supabı:** bir istek sırayı ``max_hold`` saniyeden uzun tutarsa
  (serbest bırakma hiç çalışmadıysa) sıra onu yok sayıp ilerler.

Saat, uyku ve rastgelelik dışarıdan verilebilir; testler gerçek zaman beklemez.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics

logger = get_logger(__name__)

#: Bekleyen bir isteğin koşullarını (süre sınırı, ayrılma, sızıntı) yoklama aralığı.
_POLL_SECONDS = 1.0

#: Servisin kendi loopback istemcileri (probe, Telegram botu) isteklerini bu
#: başlıkla etiketler. Yalnız loglarda ve sıra görünümünde kaynağı ayırt etmeye
#: yarar; yetki ya da öncelik vermez.
SOURCE_HEADER = "x-apiwrapper-source"
_KNOWN_SOURCES = frozenset({"keepalive", "telegram"})


#: Probe'un sırada bekleyebileceği en uzun süre (saniye). Probe'un kendi istek
#: süresi 180 sn'dir: sırada uzun bekleyip geç başlarsa süresi **işlenirken**
#: dolar ve sağlıklı bir upstream "zaman aşımı" diye raporlanır. Bu sınır dolunca
#: probe `queue_timeout` alır; keepalive bunu arıza saymaz ve kısa süre sonra
#: yeniden dener.
KEEPALIVE_MAX_QUEUE_WAIT_SECONDS = 60.0


def source_label(value: str | None, default: str = "api") -> str:
    """Başlık değerini sıra etiketine çevirir; tanınmayan değerler `default` olur."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in _KNOWN_SOURCES else default


def max_queue_wait(label: str) -> float | None:
    """Etikete göre sırada en uzun bekleme (``None`` = sınırsız)."""
    return KEEPALIVE_MAX_QUEUE_WAIT_SECONDS if label == "keepalive" else None


class QueueFull(Exception):
    """Sıra dolu; istek beklemeye alınmadı."""

    def __init__(self, waiting: int) -> None:
        super().__init__(f"request queue is full ({waiting} waiting)")
        self.waiting = waiting


class QueueTimeout(Exception):
    """İstek, sırası gelmeden ``max_wait`` süresini doldurdu."""

    def __init__(self, waited: float) -> None:
        super().__init__(f"request waited {waited:.1f}s in the queue")
        self.waited = waited


class QueueAbandoned(Exception):
    """Çağıranın ``should_leave`` koşulu doğru oldu; istek sıradan çıktı."""

    def __init__(self, waited: float) -> None:
        super().__init__(f"request left the queue after {waited:.1f}s")
        self.waited = waited


@dataclass(slots=True)
class Ticket:
    """Bir isteğin sıradaki kaydı."""

    label: str
    enqueued_at: float
    #: Sıranın bu isteğe verildiği an (aralık beklemesinden önce).
    turn_at: float = 0.0
    #: İşlemin başladığı an (aralık beklemesinden sonra).
    started_at: float = 0.0
    #: Sıra + aralık için beklenen toplam süre (saniye).
    waited: float = 0.0
    #: Upstream'e gerçekten gidildi mi? Yalnız o zaman aralık uygulanır.
    touched: bool = False
    #: False ise sıra kapalıdır; kayıt yalnız zaman tutar.
    queued: bool = True
    released: bool = False
    #: Sızıntı supabı bu kaydı yok saydı.
    stale: bool = False


@dataclass(slots=True)
class _Waiter:
    ticket: Ticket
    future: asyncio.Future[None]
    should_leave: Callable[[], bool] | None = None


class RequestQueue:
    """FIFO tek sıra ve istekler arası rastgele bekleme."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        min_gap: float = 5.0,
        max_gap: float = 10.0,
        max_waiting: int = 20,
        max_hold: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[float, float], float] = random.uniform,
        poll_seconds: float = _POLL_SECONDS,
        name: str = "",
    ) -> None:
        low, high = max(0.0, float(min_gap)), max(0.0, float(max_gap))
        if low > high:
            low, high = high, low
        #: Sıranın ait olduğu hesap (çok hesaplı kurulumda her hesabın kendi
        #: sırası vardır); tek sırada boştur. Yalnız log ve metrik etiketidir.
        self.name = name
        self.enabled = bool(enabled)
        self.min_gap = low
        self.max_gap = high
        self.max_waiting = max(0, int(max_waiting))
        self.max_hold = max(0.0, float(max_hold))
        self.poll_seconds = max(0.001, float(poll_seconds))
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._waiters: deque[_Waiter] = deque()
        self._active: Ticket | None = None
        #: Bir sonraki isteğin başlayabileceği en erken an (aralık).
        self._next_start_at = 0.0
        self._served = 0
        self._last_gap = 0.0

    @classmethod
    def from_settings(cls, settings: Settings, name: str = "") -> RequestQueue:
        return cls(
            enabled=settings.request_queue_enabled,
            min_gap=settings.request_gap_min_seconds,
            max_gap=settings.request_gap_max_seconds,
            max_waiting=settings.request_queue_max_waiting,
            max_hold=settings.request_queue_max_hold_seconds,
            name=name,
        )

    # ------------------------------------------------------------- durum
    @property
    def waiting(self) -> int:
        return len(self._waiters)

    @property
    def busy(self) -> bool:
        """Şu an işlenen ya da bekleyen bir istek var mı?"""
        if not self.enabled:
            return False
        self._evict_stale(self._clock())
        return self._active is not None or bool(self._waiters)

    @property
    def load(self) -> int:
        """İşlenen + bekleyen istek sayısı (hesaplar arası bölüşümün ölçütü)."""
        if not self.enabled:
            return 0
        self._evict_stale(self._clock())
        return (1 if self._active is not None else 0) + len(self._waiters)

    def untouched(self, label: str) -> bool:
        """Bu etiketli bir istek sırada mı, ya da sırası geldi ama upstream'e gitmedi mi?

        Cevapsız kalan bir isteğin (örn. kendi süresini dolduran probe) upstream
        yüzünden mi yoksa yalnız sıra beklediği için mi geciktiğini ayırt eder.
        """
        active = self._active
        if active is not None and active.label == label and not active.touched:
            return True
        return any(waiter.ticket.label == label for waiter in self._waiters)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        active = self._active
        return {
            "enabled": self.enabled,
            "active": active.label if active is not None else None,
            "active_seconds": (
                round(now - (active.started_at or active.turn_at), 1)
                if active is not None
                else None
            ),
            "waiting": len(self._waiters),
            "waiting_labels": [waiter.ticket.label for waiter in self._waiters],
            "gap_seconds": [self.min_gap, self.max_gap],
            "gap_remaining_seconds": round(max(0.0, self._next_start_at - now), 1),
            "max_waiting": self.max_waiting,
            "served_total": self._served,
        }

    # ------------------------------------------------------------- giriş
    async def acquire(
        self,
        label: str = "api",
        *,
        max_wait: float | None = None,
        should_leave: Callable[[], bool] | None = None,
    ) -> Ticket:
        """Sırayı bekler ve işlem iznini döndürür.

        ``max_wait`` yalnız **sıra** beklemesini sınırlar; sıra geldikten sonraki
        aralık beklemesi (en çok ``max_gap``) bu sınıra dahil değildir.
        """
        now = self._clock()
        ticket = Ticket(label=label, enqueued_at=now)
        if not self.enabled:
            ticket.queued = False
            ticket.turn_at = ticket.started_at = now
            return ticket

        self._evict_stale(now)
        if self._active is None and not self._waiters:
            self._active = ticket
            ticket.turn_at = now
        else:
            if self.max_waiting and len(self._waiters) >= self.max_waiting:
                metrics.inc("apiwrapper_request_queue_rejected_total", labels={"reason": "full"})
                raise QueueFull(len(self._waiters))
            waiter = _Waiter(
                ticket, asyncio.get_running_loop().create_future(), should_leave
            )
            self._waiters.append(waiter)
            self._publish()
            logger.info(
                "request_queued",
                label=label,
                position=len(self._waiters),
                active=self._active.label if self._active is not None else None,
                account=self.name or None,
            )
            try:
                await self._wait_turn(waiter, max_wait)
            except BaseException:
                self._drop(waiter)
                raise

        # Sıra bizde: bir önceki istekle araya rastgele bekleme koy.
        try:
            await self._wait_gap(ticket, should_leave)
        except BaseException:
            self._finish(ticket, paced=False)
            raise

        ticket.started_at = self._clock()
        ticket.waited = max(0.0, ticket.started_at - ticket.enqueued_at)
        metrics.observe(
            "apiwrapper_request_queue_wait_seconds", ticket.waited, {"label": label}
        )
        if ticket.waited >= 0.5:
            logger.info(
                "request_dequeued", label=label, waited_seconds=round(ticket.waited, 2)
            )
        return ticket

    def release(self, ticket: Ticket | None, *, paced: bool | None = None) -> None:
        """İşlem iznini bırakır; sıradakine yol verir.

        ``paced`` verilmezse aralık, upstream'e gerçekten gidildiyse uygulanır.
        Aynı kayıt için ikinci çağrı etkisizdir.
        """
        if ticket is None or not ticket.queued or ticket.released:
            return
        self._finish(ticket, paced=ticket.touched if paced is None else paced)

    # ----------------------------------------------------------- iç işler
    async def _wait_turn(self, waiter: _Waiter, max_wait: float | None) -> None:
        ticket = waiter.ticket
        while True:
            timeout = self.poll_seconds
            if max_wait is not None and max_wait > 0:
                # Süre sınırı yoklama aralığına yuvarlanmasın: tam dolduğu anda uyan.
                remaining = ticket.enqueued_at + max_wait - self._clock()
                timeout = max(0.0, min(timeout, remaining))
            done, _ = await asyncio.wait({waiter.future}, timeout=timeout)
            if done:
                return
            now = self._clock()
            # Sırayı tutan istek sızmışsa burada fark edilir ve sıra ilerler.
            self._evict_stale(now)
            if waiter.future.done():
                return
            waited = now - ticket.enqueued_at
            if max_wait is not None and max_wait > 0 and waited >= max_wait:
                metrics.inc(
                    "apiwrapper_request_queue_rejected_total", labels={"reason": "timeout"}
                )
                raise QueueTimeout(waited)
            if waiter.should_leave is not None and waiter.should_leave():
                raise QueueAbandoned(waited)

    async def _wait_gap(
        self, ticket: Ticket, should_leave: Callable[[], bool] | None
    ) -> None:
        while True:
            # Koşul uykudan ÖNCE yoklanır: öndeki istek başarısız olup sırayı
            # devrettiğinde bekleyenler tek tek birer saniye oyalanmadan ayrılır.
            if should_leave is not None and should_leave():
                raise QueueAbandoned(self._clock() - ticket.enqueued_at)
            remaining = self._next_start_at - self._clock()
            if remaining <= 0:
                return
            await self._sleep(min(remaining, self.poll_seconds))

    def _drop(self, waiter: _Waiter) -> None:
        """Beklemekten vazgeçen kaydı çıkarır; sıra verildiyse devreder."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            # Kuyrukta değil: sıra tam vazgeçtiği anda kendisine verilmiş.
            if self._active is waiter.ticket:
                self._finish(waiter.ticket, paced=False)
            return
        if not waiter.future.done():
            waiter.future.cancel()
        self._publish()

    def _finish(self, ticket: Ticket, *, paced: bool) -> None:
        if ticket.released:
            return
        ticket.released = True
        if self._active is not ticket:
            # Sızıntı supabı bu kaydı çoktan geçmiş; sıra başkasında.
            return
        now = self._clock()
        if paced:
            self._last_gap = self._rng(self.min_gap, self.max_gap)
            self._next_start_at = now + self._last_gap
            self._served += 1
        self._grant_next(now)

    def _grant_next(self, now: float) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.done():
                continue
            self._active = waiter.ticket
            waiter.ticket.turn_at = now
            waiter.future.set_result(None)
            self._publish()
            return
        self._active = None
        self._publish()

    def _evict_stale(self, now: float) -> None:
        """Sırayı ``max_hold`` saniyeden uzun tutan kaydı yok say."""
        active = self._active
        if active is None or self.max_hold <= 0:
            return
        held = now - (active.started_at or active.turn_at)
        if held <= self.max_hold:
            return
        active.stale = True
        logger.error(
            "request_queue_stale_slot",
            label=active.label,
            account=self.name or None,
            held_seconds=round(held, 1),
            hint=(
                "Bir istek sırayı serbest bırakmadı; sıra kendini kurtardı. "
                "Aynı dakikadaki hata loglarına bakın."
            ),
        )
        metrics.inc("apiwrapper_request_queue_rejected_total", labels={"reason": "stale"})
        self._grant_next(now)

    def _publish(self) -> None:
        # Tek sırada etiket yok (eski seri adı korunur); hesap başına sırada her
        # hesap kendi serisini yazar, aksi halde sıralar birbirinin değerini ezerdi.
        metrics.set_gauge(
            "apiwrapper_request_queue_waiting",
            float(len(self._waiters)),
            {"account": self.name} if self.name else None,
        )
