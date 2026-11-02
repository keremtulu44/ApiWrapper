"""Upstream (hedef Web LLM) istek gövdesi ve akış olayı şemaları."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserMessage(BaseModel):
    """`userMessage` alanı."""

    model_config = ConfigDict(extra="allow")

    content: str
    experimental_attachments: list[dict[str, Any]] = Field(
        default_factory=list, alias="experimental_attachments"
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class UpstreamPayload(BaseModel):
    """cURL analizindeki `--data-raw` gövdesinin birebir karşılığı."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    model_a_id: str = Field(alias="modelAId")
    user_message_id: str = Field(alias="userMessageId")
    model_a_message_id: str = Field(alias="modelAMessageId")
    user_message: UserMessage = Field(alias="userMessage")
    modality: str = "chat"
    recaptcha_v3_token: str = Field(default="", alias="recaptchaV3Token")
    #: Hedef servisin beklediği `mode`. Tarayıcı cURL'ündeki gerçek değer
    #: `UPSTREAM_MODE` ile ezilir; boş bırakılırsa bu varsayılan gönderilir.
    mode: str | None = "direct-battle"

    def to_wire(self, recaptcha_field: str = "recaptchaV3Token") -> dict[str, Any]:
        """Upstream'in beklediği alan adlarıyla (camelCase) sözlük döndürür.

        `recaptcha_field` hedefe göre değişir (`recaptchaV3Token` /
        `recaptchaV2Token`); varsayılan alan adı buna göre yeniden adlandırılır.
        """
        data = self.model_dump(by_alias=True, exclude_none=True)
        token = data.pop("recaptchaV3Token", "")
        field = (recaptcha_field or "recaptchaV3Token").strip()
        data[field] = token
        return data


class EventType(str, Enum):
    """Vercel AI SDK data-stream olay türleri."""

    TEXT = "text"
    START = "start"
    DATA = "data"
    MESSAGE_ANNOTATION = "message_annotation"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    ERROR = "error"
    STEP_FINISH = "step_finish"
    FINISH = "finish"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class StreamEvent:
    """Ayrıştırılmış tek bir upstream akış olayı."""

    type: EventType
    text: str = ""
    raw_code: str = ""
    data: Any = None
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.type in (EventType.FINISH, EventType.ERROR)
