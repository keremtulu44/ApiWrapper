"""`RequestQueue`: tek sıra, istekler arası bekleme ve emniyetleri."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.config import Settings
from app.services.request_queue import (
    QueueAbandoned,
    QueueFull,
    QueueTimeout,
    RequestQueue,
    source_label,
)
from tests.conftest import make_settings


class FakeTime:
    """Elle ilerletilen saat; `sleep` beklemeden saati ileri alır."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def make_queue(fake: FakeTime | None = None, **kwargs) -> RequestQueue:
    fake = fake or FakeTime()
    kwargs.setdefault("min_gap", 0.0)
    kwargs.setdefault("max_gap", 0.0)
    kwargs.setdefault("poll_seconds", 0.01)
    return RequestQueue(clock=fake.clock, sleep=fake.sleep, **kwargs)


async def settle(rounds: int = 5) -> None:
    """Bekleyen görevlere birkaç döngü çalışma fırsatı verir."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# ------------------------------------------------------------------ sıra
async def test_first_request_passes_without_waiting():
    queue = make_queue()
    ticket = await queue.acquire("ask")
    assert ticket.queued and ticket.waited == 0.0
    assert queue.busy
    assert queue.snapshot()["active"] == "ask"
    queue.release(ticket)
    assert not queue.busy


async def test_only_one_request_runs_at_a_time_in_arrival_order():
    """Kullanıcının tarifi: arka arkaya 4 istek → biri işlenir, diğerleri sırayla bekler."""
    queue = make_queue()
    order: list[str] = []
    running = 0
    peak = 0

    async def job(name: str) -> None:
        nonlocal running, peak
        ticket = await queue.acquire(name)
        running += 1
        peak = max(peak, running)
        order.append(name)
        await asyncio.sleep(0.01)
        running -= 1
        queue.release(ticket)

    tasks = []
    for name in ("bir", "iki", "üç", "dört"):
        tasks.append(asyncio.create_task(job(name)))
        await settle()  # geliş sırası belli olsun
    assert queue.waiting == 3
    assert queue.snapshot()["waiting_labels"] == ["iki", "üç", "dört"]
    await asyncio.gather(*tasks)

    assert order == ["bir", "iki", "üç", "dört"]
    assert peak == 1
    assert not queue.busy


# ---------------------------------------------------------------- aralık
async def test_gap_is_drawn_from_the_configured_range_after_a_real_request():
    fake = FakeTime()
    drawn: list[tuple[float, float]] = []

    def rng(low: float, high: float) -> float:
        drawn.append((low, high))
        return 7.0

    queue = make_queue(fake, min_gap=5.0, max_gap=10.0, rng=rng, poll_seconds=1.0)
    first = await queue.acquire("ask")
    first.touched = True  # upstream'e gerçekten gidildi
    fake.now += 3.0  # istek 3 sn sürdü
    queue.release(first)
    finished_at = fake.now

    second = await queue.acquire("ask")

    assert drawn == [(5.0, 10.0)]
    assert second.started_at - finished_at == pytest.approx(7.0)
    assert second.waited == pytest.approx(7.0)
    assert sum(fake.slept) == pytest.approx(7.0)


async def test_gap_counts_from_the_end_of_the_previous_request_not_from_arrival():
    """Bir sonraki istek aralık dolduktan sonra gelirse hiç bekletilmez."""
    fake = FakeTime()
    queue = make_queue(fake, min_gap=5.0, max_gap=10.0, rng=lambda low, high: 8.0)
    first = await queue.acquire("ask")
    first.touched = True
    queue.release(first)
    fake.now += 60.0  # bir dakika sonra yeni istek

    second = await queue.acquire("ask")

    assert second.waited == 0.0
    assert fake.slept == []


async def test_request_that_never_reached_upstream_does_not_delay_the_next():
    """Yerelde reddedilen (upstream'e gitmeyen) istek kimseyi bekletmez."""
    fake = FakeTime()
    queue = make_queue(fake, min_gap=5.0, max_gap=10.0)
    first = await queue.acquire("ask")
    assert first.touched is False
    queue.release(first)

    second = await queue.acquire("ask")

    assert second.waited == 0.0
    assert queue.snapshot()["served_total"] == 0


async def test_swapped_gap_bounds_are_normalised():
    queue = RequestQueue(min_gap=10.0, max_gap=5.0)
    assert (queue.min_gap, queue.max_gap) == (5.0, 10.0)


def test_production_defaults_match_the_requested_five_to_ten_seconds():
    """Üretim varsayılanları (testlerdeki aralık sıfırlaması olmadan)."""
    production = Settings(_env_file=None, target_domain="example.test")
    queue = RequestQueue.from_settings(production)
    assert queue.enabled is True
    assert (queue.min_gap, queue.max_gap) == (15.0, 30.0)
    assert queue.max_waiting == 20
    assert queue.max_hold == 1800.0


def test_test_settings_keep_the_queue_on_but_drop_the_gap():
    queue = RequestQueue.from_settings(make_settings())
    assert queue.enabled is True
    assert (queue.min_gap, queue.max_gap) == (0.0, 0.0)


# ------------------------------------------------------------ emniyetler
async def test_cancelled_waiter_leaves_the_queue():
    queue = make_queue()
    holder = await queue.acquire("ask")
    waiter = asyncio.create_task(queue.acquire("ask"))
    await settle()
    assert queue.waiting == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert queue.waiting == 0
    queue.release(holder)
    assert not queue.busy


async def test_turn_granted_to_a_cancelled_waiter_is_handed_to_the_next():
    """Sıra, tam iptal edildiği anda bir bekleyene verilmişse kaybolmaz."""
    queue = make_queue()
    holder = await queue.acquire("ask")
    doomed = asyncio.create_task(queue.acquire("doomed"))
    await settle()
    survivor = asyncio.create_task(queue.acquire("survivor"))
    await settle()

    # Aynı döngü adımında: sıra `doomed`'a verilir VE `doomed` iptal edilir.
    queue.release(holder)
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    ticket = await asyncio.wait_for(survivor, timeout=1)
    assert ticket.label == "survivor"
    assert queue.snapshot()["active"] == "survivor"


async def test_cancel_during_gap_wait_releases_the_turn():
    fake = FakeTime()
    blocker = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        await blocker.wait()

    queue = RequestQueue(
        min_gap=5.0, max_gap=5.0, clock=fake.clock, sleep=blocking_sleep, poll_seconds=0.01
    )
    first = await queue.acquire("ask")
    first.touched = True
    queue.release(first)

    waiting = asyncio.create_task(queue.acquire("ask"))
    await settle()
    assert queue.busy  # sıra onda, aralığı bekliyor
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert not queue.busy


async def test_full_queue_rejects_immediately():
    queue = make_queue(max_waiting=1)
    holder = await queue.acquire("ask")
    waiter = asyncio.create_task(queue.acquire("ask"))
    await settle()

    with pytest.raises(QueueFull) as error:
        await queue.acquire("ask")
    assert error.value.waiting == 1

    queue.release(holder)
    queue.release(await waiter)


async def test_waiter_times_out_and_leaves():
    fake = FakeTime()
    queue = make_queue(fake)
    holder = await queue.acquire("ask")
    waiter = asyncio.create_task(queue.acquire("ask", max_wait=600.0))
    await settle()

    fake.now += 601.0
    with pytest.raises(QueueTimeout) as error:
        await asyncio.wait_for(waiter, timeout=1)

    assert error.value.waited >= 600.0
    assert queue.waiting == 0
    queue.release(holder)


async def test_should_leave_drains_waiters_without_serving_them():
    """Arıza öğrenilince bekleyenler tek tek denenmeden sıradan çıkar."""
    queue = make_queue()
    failing = False
    holder = await queue.acquire("ask")
    waiters = [
        asyncio.create_task(queue.acquire("ask", should_leave=lambda: failing))
        for _ in range(3)
    ]
    await settle()
    assert queue.waiting == 3

    failing = True
    queue.release(holder)
    results = await asyncio.wait_for(
        asyncio.gather(*waiters, return_exceptions=True), timeout=1
    )

    assert all(isinstance(result, QueueAbandoned) for result in results)
    assert not queue.busy


async def test_should_leave_is_checked_before_the_gap_sleep():
    """Öndeki istek başarısız olunca sıradaki, aralığı beklemeden ayrılır."""
    fake = FakeTime()
    queue = make_queue(fake, min_gap=5.0, max_gap=5.0)
    first = await queue.acquire("ask")
    first.touched = True
    waiter = asyncio.create_task(queue.acquire("ask", should_leave=lambda: True))
    await settle()
    queue.release(first)

    with pytest.raises(QueueAbandoned):
        await asyncio.wait_for(waiter, timeout=1)
    assert fake.slept == []  # aralık hiç beklenmedi


async def test_stale_slot_is_evicted_so_the_queue_cannot_deadlock():
    """Serbest bırakılmayan (sızmış) istek sırayı sonsuza dek tutamaz."""
    fake = FakeTime()
    queue = make_queue(fake, max_hold=1800.0)
    leaked = await queue.acquire("leaky")
    waiter = asyncio.create_task(queue.acquire("next"))
    await settle()

    fake.now += 1801.0
    ticket = await asyncio.wait_for(waiter, timeout=1)

    assert ticket.label == "next"
    assert leaked.stale is True
    # Geç gelen serbest bırakma, sırayı yeni sahibinden almaz.
    queue.release(leaked)
    assert queue.snapshot()["active"] == "next"


async def test_release_is_idempotent():
    queue = make_queue()
    ticket = await queue.acquire("ask")
    waiter = asyncio.create_task(queue.acquire("ask"))
    await settle()

    queue.release(ticket)
    queue.release(ticket)  # ikinci çağrı sırayı bir kez daha ilerletmemeli
    queue.release(None)

    second = await asyncio.wait_for(waiter, timeout=1)
    assert queue.snapshot()["active"] == "ask"
    queue.release(second)
    assert not queue.busy


async def test_disabled_queue_lets_everything_through():
    queue = make_queue(enabled=False)
    first = await queue.acquire("ask")
    second = await queue.acquire("ask")
    assert first.queued is False and second.queued is False
    assert not queue.busy
    queue.release(first)
    queue.release(second)


# ------------------------------------------------------------- yardımcılar
async def test_untouched_tells_a_queued_request_from_one_that_reached_upstream():
    queue = make_queue()
    holder = await queue.acquire("ask")
    probe = asyncio.create_task(queue.acquire("keepalive"))
    await settle()
    assert queue.untouched("keepalive") is True  # sırada bekliyor

    queue.release(holder)
    ticket = await asyncio.wait_for(probe, timeout=1)
    assert queue.untouched("keepalive") is True  # sırası geldi, upstream'e gitmedi

    ticket.touched = True
    assert queue.untouched("keepalive") is False
    queue.release(ticket)
    assert queue.untouched("keepalive") is False


def test_source_label_accepts_only_known_internal_sources():
    assert source_label("keepalive") == "keepalive"
    assert source_label(" Telegram ") == "telegram"
    assert source_label(None) == "api"
    assert source_label("") == "api"
    # Dışarıdan gelen keyfi bir değer sıra görünümüne/loglara etiket olarak girmez.
    assert source_label("ask") == "api"
    assert source_label("x" * 500) == "api"


async def test_wait_limit_fires_when_it_expires_not_at_the_next_poll():
    """Gerçek saatle: süre sınırı yoklama aralığına (1 sn) yuvarlanmaz."""
    queue = RequestQueue(min_gap=0.0, max_gap=0.0, poll_seconds=1.0)
    holder = await queue.acquire("ask")
    started = time.monotonic()

    with pytest.raises(QueueTimeout) as error:
        await queue.acquire("keepalive", max_wait=0.1)

    elapsed = time.monotonic() - started
    assert 0.09 <= elapsed < 0.6  # 1 sn'lik yoklamayı beklemedi
    assert error.value.waited >= 0.1
    assert queue.snapshot()["waiting"] == 0
    queue.release(holder)
