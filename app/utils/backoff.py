"""Üstel geri çekilme (full jitter) yardımcıları."""

from __future__ import annotations

import random


def full_jitter_delay(attempt: int, base_delay: float, max_delay: float) -> float:
    """AWS "full jitter" stratejisi: uniform(0, min(max, base * 2^attempt))."""
    attempt = max(0, attempt)
    ceiling = min(max_delay, base_delay * (2**attempt))
    if ceiling <= 0:
        return 0.0
    return random.uniform(0.0, ceiling)


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After başlığını saniyeye çevirir (yalnız saniye biçimi desteklenir)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, 300.0)
