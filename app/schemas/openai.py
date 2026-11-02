"""OpenAI Chat Completions API şemaları (Pydantic v2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["system", "developer", "user", "assistant", "tool", "function"]


class ContentPartText(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"] = "text"
    text: str = ""


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="allow")
    url: str = ""
    detail: str | None = None


class ContentPartImage(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["image_url"] = "image_url"
    image_url: ImageURL = Field(default_factory=ImageURL)


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    arguments: str = ""


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    type: str = "function"
    function: FunctionCall = Field(default_factory=FunctionCall)


class ChatMessage(BaseModel):
    """Gelen sohbet mesajı; içerik string veya parça listesi olabilir."""

    model_config = ConfigDict(extra="allow")

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None

    def text_content(self) -> str:
        """İçeriği düz metne indirger (görseller yer tutucu ile temsil edilir)."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        chunks: list[str] = []
        for part in self.content:
            if not isinstance(part, dict):
                chunks.append(str(part))
                continue
            ptype = part.get("type")
            if ptype == "text":
                chunks.append(str(part.get("text", "")))
            elif ptype == "image_url":
                url = part.get("image_url") or {}
                href = str(url.get("url", "")) if isinstance(url, dict) else str(url)
                label = "inline image" if href.startswith("data:") else href
                chunks.append(f"[image: {label}]")
            elif ptype == "input_audio":
                chunks.append("[audio attachment]")
            elif "text" in part:
                chunks.append(str(part["text"]))
        return "\n".join(c for c in chunks if c)

    def attachments(self) -> list[dict[str, Any]]:
        """OpenAI içerik parçalarından upstream ek listesi üretir."""
        out: list[dict[str, Any]] = []
        if not isinstance(self.content, list):
            return out
        for part in self.content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            url_field = part.get("image_url") or {}
            href = url_field.get("url", "") if isinstance(url_field, dict) else str(url_field)
            if not href:
                continue
            content_type = "image/png"
            if href.startswith("data:"):
                content_type = href[5:].split(";", 1)[0] or content_type
            out.append(
                {
                    "name": f"attachment-{len(out) + 1}",
                    "contentType": content_type,
                    "url": href,
                }
            )
        return out


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """`POST /v1/chat/completions` gövdesi."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    n: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    #: Uzantı: aynı upstream sohbetini yeniden kullanmak için.
    conversation_id: str | None = None

    @field_validator("stop")
    @classmethod
    def _validate_stop(cls, v: str | list[str] | None) -> str | list[str] | None:
        if isinstance(v, list) and len(v) > 8:
            raise ValueError("At most 8 stop sequences are supported.")
        return v

    def stop_sequences(self) -> list[str]:
        if self.stop is None:
            return []
        if isinstance(self.stop, str):
            return [self.stop] if self.stop else []
        return [s for s in self.stop if s]

    def wants_usage(self) -> bool:
        return bool(self.stream_options and self.stream_options.include_usage)


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    refusal: str | None = None


class Choice(BaseModel):
    model_config = ConfigDict(extra="allow")
    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = "stop"
    logprobs: None = None


class ChatCompletionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: str | None = None


class DeltaMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["assistant"] | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChunkChoice(BaseModel):
    model_config = ConfigDict(extra="allow")
    index: int = 0
    delta: DeltaMessage = Field(default_factory=DeltaMessage)
    finish_reason: str | None = None
    logprobs: None = None


class ChatCompletionChunk(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None
    system_fingerprint: str | None = None


class ModelCard(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "apiwrapper"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard] = Field(default_factory=list)
