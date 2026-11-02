"""OpenAI `messages[]` dizisini upstream'in beklediği tek prompt'a düzleştirir."""

from __future__ import annotations

import json
from typing import Any

from app.schemas.openai import ChatMessage

ROLE_LABELS = {
    "system": "System",
    "developer": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "function": "Tool",
}

#: Yalnızca tek bir kullanıcı mesajı varsa prompt'u sarmalamadan göndeririz.
_ASSISTANT_CUE = "Assistant:"


def _format_tool_calls(message: ChatMessage) -> str:
    if not message.tool_calls:
        return ""
    calls = [
        {
            "id": call.id,
            "name": call.function.name,
            "arguments": call.function.arguments,
        }
        for call in message.tool_calls
    ]
    return "[tool_calls] " + json.dumps(calls, ensure_ascii=False)


def _render_message(message: ChatMessage) -> str:
    label = ROLE_LABELS.get(message.role, message.role.capitalize())
    if message.name:
        label = f"{label} ({message.name})"
    body = message.text_content().strip()
    tool_calls = _format_tool_calls(message)
    if tool_calls:
        body = f"{body}\n{tool_calls}".strip()
    if message.role in ("tool", "function") and message.tool_call_id:
        body = f"(tool_call_id={message.tool_call_id})\n{body}".strip()
    return f"{label}: {body}" if body else f"{label}:"


def _render_tools(tools: list[dict[str, Any]] | None) -> str:
    """Araç tanımlarını sistem bloğu olarak prompt'a ekler."""
    if not tools:
        return ""
    described: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        fn: dict[str, Any] = function if isinstance(function, dict) else tool
        described.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            }
        )
    if not described:
        return ""
    return (
        "You may use the following tools. To call one, reply with a JSON object "
        'of the form {"tool_call": {"name": "...", "arguments": {...}}}.\n'
        + json.dumps(described, ensure_ascii=False)
    )


def _render_param_hints(
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
    response_format: dict[str, Any] | None,
) -> str:
    hints: list[str] = []
    if temperature is not None:
        hints.append(f"sampling temperature ≈ {temperature}")
    if top_p is not None:
        hints.append(f"nucleus sampling top_p ≈ {top_p}")
    if max_tokens is not None:
        hints.append(f"keep the answer within roughly {max_tokens} tokens")
    if response_format:
        rtype = response_format.get("type")
        if rtype == "json_object":
            hints.append("respond with a single valid JSON object and nothing else")
        elif rtype == "json_schema":
            schema = response_format.get("json_schema", {})
            hints.append(
                "respond with JSON validating against this schema: "
                + json.dumps(schema, ensure_ascii=False)
            )
    if not hints:
        return ""
    return "Generation preferences: " + "; ".join(hints) + "."


def build_prompt(
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None = None,
    include_hints: bool = False,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    response_format: dict[str, Any] | None = None,
) -> str:
    """Mesaj geçmişini tek bir prompt metnine dönüştürür.

    Tek bir kullanıcı mesajı ve sistem/araç bağlamı yoksa mesaj olduğu gibi
    gönderilir; aksi halde rol etiketli transkript üretilir.
    """
    system_blocks: list[str] = []
    if include_hints:
        hint = _render_param_hints(temperature, top_p, max_tokens, response_format)
        if hint:
            system_blocks.append(hint)
    tool_block = _render_tools(tools)
    if tool_block:
        system_blocks.append(tool_block)

    conversational = [m for m in messages if m.role not in ("system", "developer")]
    for message in messages:
        if message.role in ("system", "developer"):
            text = message.text_content().strip()
            if text:
                system_blocks.insert(0, text)

    # Basit durum: tek kullanıcı mesajı, ek bağlam yok.
    if not system_blocks and len(conversational) == 1 and conversational[0].role == "user":
        return conversational[0].text_content().strip()

    parts: list[str] = []
    if system_blocks:
        parts.append("System:\n" + "\n\n".join(system_blocks))
    for message in conversational:
        parts.append(_render_message(message))

    if conversational and conversational[-1].role != "assistant":
        parts.append(_ASSISTANT_CUE)

    return "\n\n".join(part for part in parts if part).strip()


def collect_attachments(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Son kullanıcı mesajındaki ekleri toplar."""
    for message in reversed(messages):
        if message.role == "user":
            return message.attachments()
    return []
