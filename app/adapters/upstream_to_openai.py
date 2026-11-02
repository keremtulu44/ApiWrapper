"""Upstream akış olaylarını OpenAI yanıt nesnelerine dönüştürür."""

from __future__ import annotations

from app.schemas.openai import (
    ChatCompletionChunk,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    DeltaMessage,
    ResponseMessage,
    Usage,
)
from app.utils.ids import now_ts


def make_role_chunk(completion_id: str, model: str, created: int) -> ChatCompletionChunk:
    """İlk chunk: yalnızca rol bilgisi taşır."""
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(role="assistant", content=""))],
    )


def make_content_chunk(
    completion_id: str, model: str, created: int, content: str
) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=content))],
    )


def make_finish_chunk(
    completion_id: str,
    model: str,
    created: int,
    finish_reason: str = "stop",
) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(), finish_reason=finish_reason)],
    )


def make_usage_chunk(
    completion_id: str, model: str, created: int, usage: Usage
) -> ChatCompletionChunk:
    """`stream_options.include_usage` için son chunk (choices boş)."""
    return ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[],
        usage=usage,
    )


def make_completion(
    completion_id: str,
    model: str,
    content: str,
    usage: Usage,
    finish_reason: str = "stop",
    created: int | None = None,
    system_fingerprint: str | None = None,
) -> ChatCompletionResponse:
    """Non-stream tam yanıt nesnesi."""
    return ChatCompletionResponse(
        id=completion_id,
        created=created if created is not None else now_ts(),
        model=model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(role="assistant", content=content),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
        system_fingerprint=system_fingerprint,
    )


def build_usage(
    prompt_tokens: int, completion_tokens: int, reported: dict[str, int] | None = None
) -> Usage:
    """Upstream raporladıysa onu, aksi halde yerel tahmini kullanır."""
    if reported:
        prompt_tokens = int(reported.get("prompt_tokens", prompt_tokens))
        completion_tokens = int(reported.get("completion_tokens", completion_tokens))
        total = int(reported.get("total_tokens", prompt_tokens + completion_tokens))
    else:
        total = prompt_tokens + completion_tokens
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
    )
