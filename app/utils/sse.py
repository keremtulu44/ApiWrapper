"""Server-Sent Events yardımcıları."""

from __future__ import annotations

import json
from typing import Any

DONE_SENTINEL = "[DONE]"

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}


def sse_event(data: Any, event: str | None = None) -> str:
    """Tek bir SSE kaydı üretir."""
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    body = "".join(f"data: {line}\n" for line in payload.split("\n"))
    return f"{prefix}{body}\n"


def sse_done() -> str:
    return f"data: {DONE_SENTINEL}\n\n"


def sse_comment(text: str) -> str:
    """Bağlantıyı canlı tutmak için yorum satırı (heartbeat)."""
    return f": {text}\n\n"
