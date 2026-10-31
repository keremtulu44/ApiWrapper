"""Vercel AI SDK "data stream" protokolü çözücüsü.

Protokol: her satır `<kod>:<json>` biçimindedir. Örnekler::

    f:{"messageId":"msg-123"}
    0:"Merhaba"
    0:" dünya"
    9:{"toolCallId":"c1","toolName":"search","args":{}}
    a:{"toolCallId":"c1","result":{}}
    e:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    d:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    3:"upstream error text"

Ayrıştırıcı ayrıca SSE (`data: {...}`) ve düz metin yayınlarını da tolere eder,
böylece upstream protokol değiştirse bile metin akışı korunur.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger
from app.schemas.upstream import EventType, StreamEvent

logger = get_logger(__name__)

#: Kod → olay türü eşlemesi.
CODE_MAP: dict[str, EventType] = {
    "0": EventType.TEXT,
    "a0": EventType.TEXT,  # yeni AI SDK metin delta
    "f": EventType.START,
    "2": EventType.DATA,
    "8": EventType.MESSAGE_ANNOTATION,
    "9": EventType.TOOL_CALL,
    "a": EventType.TOOL_RESULT,
    "b": EventType.TOOL_CALL,
    "c": EventType.TOOL_CALL,
    "g": EventType.REASONING,
    "ag": EventType.REASONING,  # yeni AI SDK düşünme/akıl yürütme
    "i": EventType.REASONING,
    "j": EventType.REASONING,
    "3": EventType.ERROR,
    "e": EventType.STEP_FINISH,
    "d": EventType.FINISH,
    "ad": EventType.FINISH,  # yeni AI SDK metadata/bitiş
}

_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "content-filter": "content_filter",
    "content_filter": "content_filter",
    "tool-calls": "tool_calls",
    "tool_calls": "tool_calls",
    "error": "stop",
    "other": "stop",
    "unknown": "stop",
}


def normalize_finish_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _FINISH_REASON_MAP.get(value.strip().lower(), "stop")


def normalize_usage(raw: Any) -> dict[str, Any]:
    """Upstream usage sözlüğünü OpenAI alan adlarına çevirir."""
    if not isinstance(raw, dict):
        return {}
    prompt = raw.get("promptTokens", raw.get("prompt_tokens", raw.get("inputTokens")))
    completion = raw.get(
        "completionTokens", raw.get("completion_tokens", raw.get("outputTokens"))
    )
    total = raw.get("totalTokens", raw.get("total_tokens"))
    out: dict[str, Any] = {}
    if isinstance(prompt, (int, float)):
        out["prompt_tokens"] = int(prompt)
    if isinstance(completion, (int, float)):
        out["completion_tokens"] = int(completion)
    if isinstance(total, (int, float)):
        out["total_tokens"] = int(total)
    elif "prompt_tokens" in out or "completion_tokens" in out:
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out


def _extract_text(payload: Any) -> str:
    """Farklı biçimlerdeki metin taşıyıcılarından düz metni çıkarır."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "textDelta", "delta", "content", "value"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                # `delta: {"text": "..."}` gibi iç içe taşıyıcılar.
                nested = _extract_text(value)
                if nested:
                    return nested
        return ""
    if isinstance(payload, list):
        return "".join(_extract_text(item) for item in payload)
    return ""


def parse_line(line: str) -> StreamEvent | None:
    """Tek bir protokol satırını `StreamEvent`'e çevirir.

    Tanınmayan/boş satırlar için `None` döner.
    """
    line = line.strip()
    if not line:
        return None

    # SSE biçimi toleransı
    if line.startswith("data:"):
        line = line[5:].strip()
        if not line or line == "[DONE]":
            return StreamEvent(type=EventType.FINISH, raw_code="sse", finish_reason="stop")
        try:
            sse_payload = json.loads(line)
        except json.JSONDecodeError:
            # Bazı proxy/SSE katmanları Vercel data-stream satırını tekrar
            # `data:` içine sarar: `data: 0:"metin"`.
            if ":" in line and len(line.split(":", 1)[0].strip()) <= 2:
                nested = parse_line(line)
                if nested is not None:
                    return nested
            return StreamEvent(type=EventType.TEXT, text=line, raw_code="sse")
        return _from_sse_payload(sse_payload)

    if line.startswith((":", "event:", "id:", "retry:")):
        return None

    code, sep, rest = line.partition(":")
    if not sep or len(code) > 2:
        # Protokol dışı düz metin: içerik olarak kabul et.
        return StreamEvent(type=EventType.TEXT, text=line, raw_code="raw")

    code = code.strip()
    rest = rest.strip()
    try:
        payload: Any = json.loads(rest) if rest else None
    except json.JSONDecodeError:
        payload = rest

    etype = CODE_MAP.get(code, EventType.UNKNOWN)

    if etype is EventType.TEXT or etype is EventType.REASONING:
        return StreamEvent(type=etype, text=_extract_text(payload), raw_code=code, data=payload)

    if etype is EventType.ERROR:
        message = _extract_text(payload) or (
            json.dumps(payload, ensure_ascii=False) if payload is not None else "upstream error"
        )
        return StreamEvent(type=EventType.ERROR, text=message, raw_code=code, data=payload)

    if etype in (EventType.FINISH, EventType.STEP_FINISH):
        reason = None
        usage: dict[str, Any] = {}
        if isinstance(payload, dict):
            reason = normalize_finish_reason(payload.get("finishReason"))
            usage = normalize_usage(payload.get("usage"))
        return StreamEvent(
            type=etype,
            raw_code=code,
            data=payload,
            finish_reason=reason,
            usage=usage,
        )

    return StreamEvent(type=etype, raw_code=code, data=payload)


def _from_sse_payload(payload: Any) -> StreamEvent:
    """OpenAI benzeri SSE gövdesinden olay üretir (tolerans katmanı)."""
    if isinstance(payload, dict):
        if "error" in payload:
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            return StreamEvent(type=EventType.ERROR, text=str(message), raw_code="sse")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or choice.get("message") or {}
            text = delta.get("content") if isinstance(delta, dict) else ""
            reason = normalize_finish_reason(choice.get("finish_reason"))
            if reason:
                return StreamEvent(
                    type=EventType.FINISH,
                    text=text or "",
                    raw_code="sse",
                    finish_reason=reason,
                    usage=normalize_usage(payload.get("usage")),
                )
            return StreamEvent(type=EventType.TEXT, text=text or "", raw_code="sse")
    return StreamEvent(type=EventType.TEXT, text=_extract_text(payload), raw_code="sse")


class StreamDecoder:
    """Bayt parçalarını satırlara böler; çok baytlı UTF-8 sınırlarını korur."""

    def __init__(self) -> None:
        self._decoder = __import__("codecs").getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Yeni baytları işler ve tamamlanmış satırları döndürür."""
        if chunk:
            self._buffer += self._decoder.decode(chunk)
        return self._drain_lines()

    def _drain_lines(self) -> list[str]:
        lines: list[str] = []
        while True:
            idx = self._buffer.find("\n")
            if idx == -1:
                break
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            if line.endswith("\r"):
                line = line[:-1]
            lines.append(line)
        return lines

    def flush(self) -> list[str]:
        """Akış bitince tamponda kalanları döndürür."""
        self._buffer += self._decoder.decode(b"", final=True)
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


async def parse_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[StreamEvent]:
    """Bayt akışını `StreamEvent` akışına dönüştürür."""
    decoder = StreamDecoder()
    async for chunk in chunks:
        for line in decoder.feed(chunk):
            event = parse_line(line)
            if event is not None:
                yield event
    for line in decoder.flush():
        event = parse_line(line)
        if event is not None:
            yield event
