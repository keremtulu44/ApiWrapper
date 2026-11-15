"""Upstream (hedef Web LLM) istek gövdesi ve akış olayı şemaları."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Hedefin şemasının beklediği `mode` değeri (tarayıcı cURL'ünden). `UPSTREAM_MODE`
#: boş kaldığında gövdeye bu değer yazılır; alan hiçbir zaman düşürülmez.
DEFAULT_UPSTREAM_MODE = "direct-battle"


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
    #:
    #: Alan **hiçbir zaman wire'dan düşmez** (2026-11 canlı 400 arızası): Render
    #: panosunda boş env değeri saklanamadığı için `UPSTREAM_MODE` yer tutucuya
    #: dönüşüyor, `Settings` onu boşa çeviriyor ve adapter `None` geçiyordu;
    #: `to_wire(exclude_none=True)` de alanı gövdeden tamamen siliyordu. Tarayıcı
    #: cURL'ünün gövdesinde bu alan her zaman var; eksikliği hedefte 400 (şema
    #: doğrulaması) üretir. Boş/`None` değer bu varsayılana düşer.
    mode: str = DEFAULT_UPSTREAM_MODE
    #: Hedef istemci bağlamı. Tarayıcı cURL'ünde gövdede gelir; boşsa alan
    #: hiç gönderilmez (uydurulmuş bir değer göndermek isteği bozar).
    cx: str | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _default_mode(cls, v: object) -> object:
        """Boş/eksik `mode` değerini hedefin beklediği varsayılana çevirir."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return DEFAULT_UPSTREAM_MODE
        return v.strip() if isinstance(v, str) else v

    def to_wire(self, recaptcha_field: str = "recaptchaV3Token") -> dict[str, Any]:
        """Upstream'in beklediği alan adlarıyla (camelCase) sözlük döndürür.

        `recaptcha_field` hedefe göre değişir (`recaptchaV3Token` /
        `recaptchaV2Token`); varsayılan alan adı buna göre yeniden adlandırılır.
        """
        data = self.model_dump(by_alias=True, exclude_none=True)
        # Boş `cx` alanı gönderilmez: tarayıcı isteğinde değer her zaman doluydu.
        if not str(data.get("cx") or "").strip():
            data.pop("cx", None)
        token = str(data.pop("recaptchaV3Token", "") or "").strip()
        field = (recaptcha_field or "recaptchaV3Token").strip()
        # Boş token'ı alan olarak göndermek `""` değerini iletir; hedef bunu
        # "geçersiz token" sayıp `recaptcha_rejected` döndürebiliyor (noop
        # sağlayıcısında tam olarak bu oluyordu). Alan hiç gönderilmez.
        if token:
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
