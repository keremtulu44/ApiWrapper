"""Kendi kendine yürüyen proje otopilotu: Durumsuz `max` ↔ Sandbox'lu Agent Mode.

Bu paket mevcut canlı `max` gönderme hattına (`app/upstream/*`) hiç dokunmadan,
onun kanıtlanmış yapıtaşlarından (bellek içi cookie jar, `Set-Cookie` korunumu,
tarayıcı başlıkları, sürüm damgası kanaryası, UUIDv7) türetilmiş izole bir
Agent Mode istemcisi ve çok turlu orkestratör sağlar.
"""

from __future__ import annotations

__all__ = [
    "AgentChatState",
    "AgentCurlConfig",
    "AgentMessage",
    "AgentSessionClient",
    "AutopilotConfig",
    "AutopilotOrchestrator",
    "AutopilotRunState",
    "ToolExecutionEvidence",
    "parse_agent_curl",
]


def __getattr__(name: str) -> object:
    if name in {"AgentCurlConfig", "parse_agent_curl"}:
        from app.autopilot import curl_parse

        return getattr(curl_parse, name)
    if name in {"AgentChatState", "AgentMessage", "ToolExecutionEvidence"}:
        from app.autopilot import events_parser

        return getattr(events_parser, name)
    if name == "AgentSessionClient":
        from app.autopilot.agent_client import AgentSessionClient

        return AgentSessionClient
    if name in {"AutopilotConfig", "AutopilotOrchestrator", "AutopilotRunState"}:
        from app.autopilot import orchestrator

        return getattr(orchestrator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
