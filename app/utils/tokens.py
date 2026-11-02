"""Token sayımı — tiktoken varsa gerçek, yoksa sezgisel tahmin."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

_ENCODING_NAME = "cl100k_base"


@lru_cache(maxsize=1)
def _encoder() -> Any | None:
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:  # pragma: no cover - tiktoken opsiyonel
        return None


def tiktoken_available() -> bool:
    return _encoder() is not None


def count_tokens(text: str) -> int:
    """Metnin token sayısı. tiktoken yoksa ~4 karakter = 1 token tahmini."""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # pragma: no cover
            pass
    # Sezgisel: kelime ve karakter sayısının harmanı, gerçeğe yakın kalır.
    words = text.split()
    return max(1, int(len(text) / 4.0 + len(words) * 0.25))


def count_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Sohbet mesajlarının yaklaşık toplam token maliyeti (rol ek yüküyle)."""
    total = 0
    for message in messages:
        total += 4  # rol/ayraç ek yükü
        for key, value in message.items():
            if isinstance(value, str):
                total += count_tokens(value)
            elif isinstance(value, list):
                for part in value:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            total += count_tokens(text)
                        else:
                            total += 85  # görsel/ek için sabit tahmin
            if key == "name":
                total += 1
    return total + 3
