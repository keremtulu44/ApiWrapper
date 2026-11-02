"""Çekirdek bileşen testleri: cache, rate limit, breaker, registry, loglama."""

from __future__ import annotations

import asyncio

import pytest

from app.core.logging import mask_value
from app.core.metrics import MetricsRegistry
from app.core.rate_limit import TokenBucketLimiter
from app.services.completion_service import StopSequenceTracker
from app.services.model_registry import ModelRegistry
from app.services.session_manager import SessionManager
from app.upstream.breaker import BreakerState, CircuitBreaker
from app.utils.backoff import full_jitter_delay, parse_retry_after
from app.utils.cache import LRUTTLCache, SingleFlightValue
from app.utils.tokens import count_tokens
from tests.conftest import make_settings


# ------------------------------------------------------------------ cache
@pytest.mark.asyncio
async def test_cache_set_get_and_expiry():
    cache: LRUTTLCache[str, int] = LRUTTLCache(maxsize=8, ttl=0.05)
    await cache.set("a", 1)
    assert await cache.get("a") == 1
    await asyncio.sleep(0.07)
    assert await cache.get("a") is None


@pytest.mark.asyncio
async def test_cache_evicts_lru():
    cache: LRUTTLCache[str, int] = LRUTTLCache(maxsize=2, ttl=60)
    await cache.set("a", 1)
    await cache.set("b", 2)
    await cache.get("a")
    await cache.set("c", 3)
    assert await cache.get("b") is None
    assert await cache.get("a") == 1


@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_calls():
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "token"

    value: SingleFlightValue[str] = SingleFlightValue(ttl=10)
    results = await asyncio.gather(*(value.get(factory) for _ in range(25)))
    assert results == ["token"] * 25
    assert calls == 1

    value.invalidate()
    await value.get(factory)
    assert calls == 2


# ------------------------------------------------------------- rate limit
@pytest.mark.asyncio
async def test_token_bucket_allows_burst_then_blocks():
    limiter = TokenBucketLimiter(rate_per_minute=60, burst=3)
    results = [await limiter.acquire("k") for _ in range(5)]
    assert [r[0] for r in results] == [True, True, True, False, False]
    assert results[-1][1] > 0


@pytest.mark.asyncio
async def test_token_bucket_is_per_key():
    limiter = TokenBucketLimiter(rate_per_minute=60, burst=1)
    assert (await limiter.acquire("a"))[0] is True
    assert (await limiter.acquire("b"))[0] is True
    assert (await limiter.acquire("a"))[0] is False


# ---------------------------------------------------------------- breaker
@pytest.mark.asyncio
async def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=0.05)
    assert await breaker.allows()
    await breaker.record_failure()
    await breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not await breaker.allows()
    await asyncio.sleep(0.06)
    assert await breaker.allows()
    assert breaker.state is BreakerState.HALF_OPEN
    await breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_disabled_breaker_always_allows():
    breaker = CircuitBreaker(failure_threshold=1, enabled=False)
    await breaker.record_failure()
    assert await breaker.allows()


# --------------------------------------------------------------- registry
def test_registry_resolves_aliases_and_default():
    registry = ModelRegistry.from_dict(
        {
            "default": "b",
            "models": [
                {"id": "a", "upstream_id": "ua", "aliases": ["A-ALIAS"]},
                {"id": "b", "upstream_id": "ub"},
            ],
        }
    )
    assert registry.resolve("a-alias").upstream_id == "ua"
    assert registry.resolve(None).id == "b"
    assert "A" in registry
    assert len(registry) == 2


def test_registry_rejects_empty_file():
    with pytest.raises(ValueError):
        ModelRegistry.from_dict({"models": []})


# --------------------------------------------------------------- sessions
@pytest.mark.asyncio
async def test_stateless_sessions_are_unique():
    manager = SessionManager(make_settings(session_stateless=True))
    first = await manager.acquire("conv")
    second = await manager.acquire("conv")
    assert first.chat_id != second.chat_id


@pytest.mark.asyncio
async def test_stateful_sessions_are_reused():
    manager = SessionManager(make_settings(session_stateless=False))
    first = await manager.acquire("conv")
    second = await manager.acquire("conv")
    assert first.chat_id == second.chat_id
    assert second.message_count == 2
    await manager.invalidate("conv")
    assert (await manager.acquire("conv")).chat_id != first.chat_id


# ------------------------------------------------------------ stop tracker
def test_stop_tracker_truncates_across_chunk_boundary():
    tracker = StopSequenceTracker(["END"])
    out = tracker.process("hello E") + tracker.process("ND more")
    assert out == "hello "
    assert tracker.triggered
    assert tracker.flush() == ""


def test_stop_tracker_without_stops_passes_through():
    tracker = StopSequenceTracker([])
    assert tracker.process("abc") == "abc"
    assert tracker.flush() == ""


def test_stop_tracker_holds_partial_tail():
    tracker = StopSequenceTracker(["FIN"])
    assert tracker.process("data FI") == "data "
    assert tracker.flush() == "FI"


# ---------------------------------------------------------------- backoff
def test_full_jitter_within_bounds():
    for attempt in range(5):
        delay = full_jitter_delay(attempt, 0.5, 8.0)
        assert 0.0 <= delay <= 8.0


def test_parse_retry_after():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("bogus") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("100000") == 300.0


# ----------------------------------------------------------- stream url
def test_stream_url_substitutes_chat_id():
    settings = make_settings()
    assert settings.stream_url("abc").endswith("/nextjs-api/stream/post-to-evaluation/abc")


def test_stream_url_without_placeholder_keeps_path():
    settings = make_settings(upstream_stream_path="/nextjs-api/stream/create-evaluation")
    url = settings.stream_url("abc-should-not-appear")
    assert url.endswith("/nextjs-api/stream/create-evaluation")
    assert "abc-should-not-appear" not in url


def test_stream_url_adds_leading_slash_and_ignores_extra_braces():
    settings = make_settings(upstream_stream_path="api/stream/{chat_id}?x={keep}")
    url = settings.stream_url("zz")
    assert url.endswith("/api/stream/zz?x={keep}")


# ----------------------------------------------------------------- tokens
def test_token_counting_is_positive_and_monotonic():
    assert count_tokens("") == 0
    short = count_tokens("hello")
    long = count_tokens("hello " * 200)
    assert 0 < short < long


# ---------------------------------------------------------------- logging
def test_mask_value_hides_secrets():
    assert mask_value("short") == "***"
    masked = mask_value("sk-super-secret-value")
    assert "super" not in masked
    assert masked.startswith("sk-")


# ---------------------------------------------------------------- metrics
def test_metrics_render_includes_all_types():
    registry = MetricsRegistry()
    registry.inc("c_total", 2, {"a": "1"})
    registry.set_gauge("g_value", 5)
    registry.observe("h_seconds", 0.3)
    output = registry.render()
    assert 'c_total{a="1"} 2' in output
    assert "g_value 5" in output
    assert "h_seconds_bucket" in output
    assert "h_seconds_count 1" in output
