"""LRU + TTL bellek içi cache ve tek-uçuş (single-flight) token cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LRUTTLCache(Generic[K, V]):
    """Sabit kapasiteli, süre aşımlı LRU cache (asyncio-güvenli)."""

    def __init__(self, maxsize: int = 1024, ttl: float = 300.0) -> None:
        self.maxsize = max(1, maxsize)
        self.ttl = ttl
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: K) -> V | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= time.monotonic():
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    async def set(self, key: K, value: V, ttl: float | None = None) -> None:
        async with self._lock:
            expires_at = time.monotonic() + (self.ttl if ttl is None else ttl)
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            self._purge_locked()

    async def delete(self, key: K) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._data.clear()

    async def size(self) -> int:
        async with self._lock:
            self._purge_locked()
            return len(self._data)

    def _purge_locked(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if exp <= now]
        for key in expired:
            self._data.pop(key, None)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


class SingleFlightValue(Generic[V]):
    """TTL'li tek değer cache'i; eşzamanlı yenileme isteklerini tek çağrıda birleştirir."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._value: V | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, factory: Callable[[], Awaitable[V]]) -> V:
        now = time.monotonic()
        if self._value is not None and now < self._expires_at:
            return self._value
        async with self._lock:
            now = time.monotonic()
            if self._value is not None and now < self._expires_at:
                return self._value
            value = await factory()
            self._value = value
            self._expires_at = time.monotonic() + self.ttl
            return value

    def invalidate(self) -> None:
        self._value = None
        self._expires_at = 0.0
