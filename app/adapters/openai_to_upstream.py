"""OpenAI isteğini upstream gövdesine dönüştürür."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.prompt_builder import build_prompt, collect_attachments
from app.core.config import Settings
from app.core.errors import InvalidRequestError, PayloadTooLargeError
from app.schemas.openai import ChatCompletionRequest
from app.schemas.upstream import UpstreamPayload, UserMessage
from app.utils.ids import new_message_id

#: Upstream'in desteklemediği ve politikaya tabi parametreler.
UNSUPPORTED_FIELDS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "max_tokens",
    "max_completion_tokens",
    "response_format",
    "tools",
    "tool_choice",
)


@dataclass(slots=True)
class BuiltRequest:
    """Dönüştürülmüş istek ve yan bilgiler."""

    payload: UpstreamPayload
    prompt: str
    chat_id: str
    user_message_id: str
    model_message_id: str
    upstream_model_id: str
    #: Model kaydından gelen mod (gövdeye yazıldı; teşhis/log için de taşınır).
    modality: str = "chat"
    #: Model kaydına ait referer yolu; boşsa ayarlardaki değer kullanılır.
    referer_path: str = ""


def _requested_unsupported(request: ChatCompletionRequest) -> list[str]:
    present: list[str] = []
    for field in UNSUPPORTED_FIELDS:
        value = getattr(request, field, None)
        if value is not None:
            present.append(field)
    return present


def validate_request(request: ChatCompletionRequest, settings: Settings) -> None:
    """Girdi sertleştirme ve desteklenmeyen parametre politikası."""
    if not request.messages:
        raise InvalidRequestError(
            "'messages' must contain at least one message.", param="messages"
        )
    if len(request.messages) > settings.max_messages:
        raise PayloadTooLargeError(
            f"Too many messages: {len(request.messages)} > {settings.max_messages}.",
            param="messages",
        )
    if request.n is not None and request.n > 1:
        raise InvalidRequestError(
            "Only n=1 is supported by this upstream provider.", param="n"
        )
    # Yalnız system mesajı / tamamen boş içerik: upstream'e boş prompt gitmesin.
    has_turn = any(m.role in ("user", "tool", "function") for m in request.messages)
    has_text = any(m.text_content().strip() for m in request.messages)
    if not has_turn and not has_text:
        raise InvalidRequestError(
            "At least one message must contain non-empty content.", param="messages"
        )

    if settings.unsupported_params == "error":
        offenders = _requested_unsupported(request)
        if offenders:
            raise InvalidRequestError(
                "Unsupported parameters for this upstream provider: "
                + ", ".join(sorted(offenders)),
                param=sorted(offenders)[0],
            )


def build_upstream_request(
    request: ChatCompletionRequest,
    *,
    settings: Settings,
    upstream_model_id: str,
    chat_id: str,
    recaptcha_token: str,
    modality: str = "chat",
    referer_path: str = "",
) -> BuiltRequest:
    """OpenAI isteğinden `UpstreamPayload` üretir.

    `modality` model kaydından gelir (`chat` · `search` · `image`). Gövdede
    **birebir** bu adla gönderilir: hedef modu bu alandan okur, referer yolundan
    değil. İkisi birlikte tutarlı olmalıdır (örn. `search` + `/search/direct`).
    """
    validate_request(request, settings)

    include_hints = settings.unsupported_params == "hint"
    prompt = build_prompt(
        request.messages,
        tools=request.tools,
        include_hints=include_hints,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens or request.max_completion_tokens,
        response_format=request.response_format,
    )

    if not prompt.strip():
        raise InvalidRequestError(
            "Resulting prompt is empty after normalization.", param="messages"
        )
    if len(prompt) > settings.max_prompt_chars:
        raise PayloadTooLargeError(
            f"Prompt too long: {len(prompt)} characters "
            f"(limit {settings.max_prompt_chars}).",
            param="messages",
        )

    metadata: dict[str, Any] = {}
    if request.user:
        metadata["user"] = request.user

    user_message_id = new_message_id()
    model_message_id = new_message_id()
    # Normalizasyon tek yerde: hem gövdeye yazılan değer hem teşhis/log için
    # döndürülen değer aynı olsun (boş/ büyük harfli yazım upstream'e sızmasın).
    resolved_modality = (modality or "").strip().lower() or "chat"

    payload = UpstreamPayload(
        id=chat_id,
        modelAId=upstream_model_id,
        userMessageId=user_message_id,
        modelAMessageId=model_message_id,
        userMessage=UserMessage(
            content=prompt,
            experimental_attachments=collect_attachments(request.messages),
            metadata=metadata,
        ),
        modality=resolved_modality,
        recaptchaV3Token=recaptcha_token,
        mode=settings.upstream_mode.strip() or None,
        # `cx` yalnızca açıkça istendiğinde gönderilir; varsayılan davranış
        # (alan yok) canlıda çalıştığı gözlendi, gereksiz risk alınmaz.
        cx=(settings.upstream_cx.strip() or None) if settings.upstream_cx_send else None,
    )

    return BuiltRequest(
        payload=payload,
        prompt=prompt,
        chat_id=chat_id,
        user_message_id=user_message_id,
        model_message_id=model_message_id,
        upstream_model_id=upstream_model_id,
        modality=resolved_modality,
        referer_path=(referer_path or "").strip(),
    )
