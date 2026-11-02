"""Asenkron token-bucket rate limiter (tek süreç, in-memory)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated_at: float = field(default_factory=time.monotonic)


class TokenBucketLimiter:
    """Anahtar başına token-bucket.

    `rate_per_minute` dakikada yenilenen token sayısı, `burst` kova kapasitesidir.
    """

    def __init__(self, rate_per_minute: int, burst: int, max_keys: int = 10_000) -> None:
        self.rate_per_second = max(rate_per_minute, 1) / 60.0
        self.capacity = float(max(burst, 1))
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Token tüketmeyi dener.

        Returns:
            (izin_verildi, retry_after_saniye)
        """
        now = time.monotonic()
        async with self._lock:
            if len(self._buckets) > self.max_keys:
                self._evict(now)

            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated_at=now)
                self._buckets[key] = bucket

            elapsed = max(now - bucket.updated_at, 0.0)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate_per_second)
            bucket.updated_at = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0

            deficit = cost - bucket.tokens
            retry_after = deficit / self.rate_per_second if self.rate_per_second else 60.0
            return False, round(retry_after, 3)

    def _evict(self, now: float) -> None:
        """Kovası dolmuş (uzun süredir kullanılmayan) girdileri temizler."""
        stale_after = self.capacity / self.rate_per_second if self.rate_per_second else 300.0
        stale = [k for k, b in self._buckets.items() if now - b.updated_at > stale_after]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) > self.max_keys:
            # Hâlâ büyükse en eskiden başlayarak kırp.
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1].updated_at)
            for key, _ in ordered[: len(self._buckets) - self.max_keys]:
                self._buckets.pop(key, None)

    def reset(self) -> None:
        self._buckets.clear()
