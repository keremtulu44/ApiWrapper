"""`/v1/chat/completions` ve `/v1/completions` uç noktaları."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_completion_service
from app.api.disconnect import cancel_on_disconnect
from app.api.streaming import GuardedStreamingResponse
from app.core.errors import InvalidRequestError
from app.core.logging import get_logger
from app.core.security import api_key_dependency
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.services.completion_service import Admission, CompletionService
from app.services.lanes import ACCOUNT_HEADER
from app.services.request_queue import SOURCE_HEADER, max_queue_wait, source_label
from app.utils.sse import SSE_HEADERS

logger = get_logger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(api_key_dependency)])


def _pinned_slot(request: Request) -> int | None:
    """`x-apiwrapper-account: N` başlığı isteği N. hesaba sabitler (yoksa ``None``).

    Keepalive her hesabı kendi probe'uyla dener; operatör de tek bir hesabı elle
    sınayabilir. Sabitlenen istek hesabın sağlığına bakılmadan gönderilir ve
    başka hesaba taşınmaz.
    """
    raw = (request.headers.get(ACCOUNT_HEADER) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidRequestError(
            f"'{ACCOUNT_HEADER}' must be an account slot number (1, 2, ...).",
            param=ACCOUNT_HEADER,
        ) from exc


def _report_reroutes(
    request: Request, service: CompletionService, admission: Admission, *, answered: bool
) -> None:
    """İstek başka hesaba taşındıysa arızalanan hesabı operatöre bildirir.

    İstemci (ör. Telegram sohbeti) normal bir cevap görür; bildirim olmazsa bir
    hesabın devreden çıktığı o hesabın bir sonraki probe'una dek fark edilmezdi.
    """
    if not admission.reroutes:
        return
    alerter = getattr(request.app.state, "alerter", None)
    tasks = getattr(request.app.state, "tasks", None)
    if alerter is None or tasks is None:
        return
    request_id = getattr(request.state, "request_id", None) or "-"
    for report in service.reroute_reports(
        admission, request_id=request_id, answered=answered, source="chat"
    ):
        tasks.spawn(alerter.notify(report), name="chat-alert")


def _admission(request: Request) -> Admission:
    """İsteğin sıra etiketi: servisin kendi probe'u/botu ya da dış API istemcisi."""
    label = source_label(request.headers.get(SOURCE_HEADER))
    return Admission(
        label=label, max_wait=max_queue_wait(label), account_slot=_pinned_slot(request)
    )


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

        # Korumalı yanıt: akış yarıda biterse üretici kapatılır, sıra bırakılır.
        return GuardedStreamingResponse(
            service.stream_completion(
                body,
                is_disconnected=is_disconnected,
                client_identity=client_identity,
                admission=_admission(request),
            ),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    # İstekler sıradan geçer: cevabı beklemekten vazgeçmiş bir istemcinin isteği
    # sırada kalmasın. Upstream'e gönderilmiş istek ise yarıda kesilmez; canlı
    # sürümdeki gibi sonuna kadar sürer (bkz. `app/api/disconnect.py`).
    admission = _admission(request)
    answered = False
    try:
        completion = await cancel_on_disconnect(
            request,
            service.create_completion(
                body, client_identity=client_identity, admission=admission
            ),
            committed=lambda: admission.sent,
        )
        answered = True
        return completion
    finally:
        _report_reroutes(request, service, admission, answered=answered)


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

        # Korumalı yanıt: akış yarıda biterse üretici kapatılır, sıra bırakılır.
        return GuardedStreamingResponse(
            service.stream_completion(
                chat_body, is_disconnected=is_disconnected, admission=_admission(request)
            ),
            media_type="text/event-stream",
            headers={k: v for k, v in SSE_HEADERS.items() if k != "Content-Type"},
        )

    admission = _admission(request)
    completion = await cancel_on_disconnect(
        request,
        service.create_completion(chat_body, admission=admission),
        committed=lambda: admission.sent,
    )
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
