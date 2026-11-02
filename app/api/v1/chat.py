"""`/v1/chat/completions` ve `/v1/completions` uç noktaları."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_completion_service
from app.core.errors import InvalidRequestError
from app.core.logging import get_logger
from app.core.security import api_key_dependency
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.services.completion_service import CompletionService
from app.utils.sse import SSE_HEADERS

logger = get_logger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(api_key_dependency)])


@router.post(
    "/chat/completions",
    response_model=None,
    summary="Create a chat completion (OpenAI-compatible)",
)
async def create_chat_completion(
    body: ChatCompletionRequest,
    request: Request,
    service: CompletionService = Depends(get_completion_service),
) -> ChatCompletionResponse | StreamingResponse:
    """OpenAI Chat Completions ile uyumlu tamamlama üretir."""
    # İstemcinin `conversation_id` gönderemediği durumlarda (Continue, OpenAI SDK)
    # sohbet yeniden kullanımı API anahtarı parmak izine bağlanır.
    client_identity: str | None = getattr(request.state, "client_fingerprint", None)

    if body.stream:
        async def is_disconnected() -> bool:
            try:
                return await request.is_disconnected()
            except Exception:  # pragma: no cover - transport'a bağlı
                return False

        return StreamingResponse(
            service.stream_completion(
                body, is_disconnected=is_disconnected, client_identity=client_identity
            ),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    return await service.create_completion(body, client_identity=client_identity)


@router.post(
    "/completions",
    response_model=None,
    summary="Legacy text completion (mapped onto chat)",
)
async def create_text_completion(
    payload: dict[str, Any],
    request: Request,
    service: CompletionService = Depends(get_completion_service),
) -> Any:
    """Eski `/v1/completions` biçimini sohbet formatına eşler."""
    prompt = payload.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt)
    if not isinstance(prompt, str) or not prompt.strip():
        raise InvalidRequestError("'prompt' must be a non-empty string.", param="prompt")

    chat_body = ChatCompletionRequest(
        model=str(payload.get("model", "")),
        messages=[ChatMessage(role="user", content=prompt)],
        stream=bool(payload.get("stream", False)),
        max_tokens=payload.get("max_tokens"),
        temperature=payload.get("temperature"),
        top_p=payload.get("top_p"),
        stop=payload.get("stop"),
        user=payload.get("user"),
    )

    if chat_body.stream:
        async def is_disconnected() -> bool:
            try:
                return await request.is_disconnected()
            except Exception:  # pragma: no cover
                return False

        return StreamingResponse(
            service.stream_completion(chat_body, is_disconnected=is_disconnected),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    completion = await service.create_completion(chat_body)
    return {
        "id": completion.id.replace("chatcmpl-", "cmpl-"),
        "object": "text_completion",
        "created": completion.created,
        "model": completion.model,
        "choices": [
            {
                "index": 0,
                "text": completion.choices[0].message.content or "",
                "finish_reason": completion.choices[0].finish_reason,
                "logprobs": None,
            }
        ],
        "usage": completion.usage.model_dump(),
    }
