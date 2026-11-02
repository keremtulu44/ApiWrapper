"""Basit asenkron devre kesici (circuit breaker)."""

from __future__ import annotations

import asyncio
import time
from enum import IntEnum

from app.core.metrics import metrics


class BreakerState(IntEnum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


class CircuitBreaker:
    """Ardışık hatalarda devreyi açar, `reset_timeout` sonrası yarı-açık dener."""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        enabled: bool = True,
        name: str = "upstream",
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.reset_timeout = reset_timeout
        self.enabled = enabled
        self.name = name
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    async def allows(self) -> bool:
        """İstek gönderilebilir mi?"""
        if not self.enabled:
            return True
        async with self._lock:
            if self._state is BreakerState.OPEN:
                if time.monotonic() - self._opened_at >= self.reset_timeout:
                    self._set_state(BreakerState.HALF_OPEN)
                    return True
                return False
            return True

    async def record_success(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            self._failures = 0
            if self._state is not BreakerState.CLOSED:
                self._set_state(BreakerState.CLOSED)

    async def record_failure(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                self._set_state(BreakerState.OPEN)

    def _set_state(self, state: BreakerState) -> None:
        self._state = state
        if state is BreakerState.CLOSED:
            self._failures = 0
        metrics.set_gauge(
            "apiwrapper_circuit_breaker_state", float(int(state)), {"name": self.name}
        )

    def retry_after(self) -> float:
        if self._state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self.reset_timeout - (time.monotonic() - self._opened_at))

    def reset(self) -> None:
        self._failures = 0
        self._set_state(BreakerState.CLOSED)
