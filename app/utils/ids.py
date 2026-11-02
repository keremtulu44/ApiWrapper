"""Kimlik üreteçleri (chat/mesaj/completion id)."""

from __future__ import annotations

import secrets
import string
import time
import uuid

_ALPHABET = string.ascii_letters + string.digits


def new_uuid4() -> str:
    return str(uuid.uuid4())


def new_uuid7() -> str:
    """Zaman sıralı UUIDv7 üretir (Arena upstream kimlik biçimi)."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))


def new_chat_id() -> str:
    """Upstream sohbet kimliği (UUIDv7)."""
    return new_uuid7()


def new_message_id() -> str:
    """Upstream mesaj kimliği (UUIDv7)."""
    return new_uuid7()


def random_suffix(length: int = 24) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_completion_id() -> str:
    return f"chatcmpl-{random_suffix(24)}"


def new_request_id() -> str:
    return f"req_{random_suffix(20)}"


def now_ts() -> int:
    return int(time.time())
