"""İzole `/v1/autopilot` HTTP uçları (yalnız `API_KEYS` ile erişilebilir).

Mevcut `/v1/chat/completions`, `/v1/ask` ve `/v1/admin/*` akışlarına hiçbir
etkisi yoktur; yalnızca çağrıldığında `AutopilotManager` üzerinden Agent ↔ Max
proje döngüsünü başlatır ve durumunu raporlar. Ham cURL, çerez veya token
değerleri ne yanıtlara ne de loglara yazılır.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.autopilot.curl_parse import extract_all_max_identities, parse_agent_curl
from app.autopilot.manager import AutopilotManager
from app.autopilot.orchestrator import SameAccountError
from app.core.errors import InvalidRequestError, ModelNotFoundError
from app.core.security import api_key_dependency

router = APIRouter(tags=["autopilot"], dependencies=[Depends(api_key_dependency)])


def _get_manager(request: Request) -> AutopilotManager:
    manager: AutopilotManager | None = getattr(request.app.state, "autopilot_manager", None)
    if manager is None:
        manager = AutopilotManager()
        request.app.state.autopilot_manager = manager
    return manager


class InspectAgentCurlRequest(BaseModel):
    agent_curl: str = Field(..., min_length=10)
    chat_id: str | None = None


class StartAutopilotRequest(BaseModel):
    project_idea: str = Field(..., min_length=5, max_length=20000)
    agent_curl: str = Field(..., min_length=20)
    chat_id: str | None = None
    max_turns: int = Field(default=8, ge=1, le=25)
    gap_min_seconds: float = Field(default=5.0, ge=0.0, le=120.0)
    gap_max_seconds: float = Field(default=10.0, ge=0.0, le=180.0)
    agent_turn_timeout: float = Field(default=900.0, ge=60.0, le=3600.0)
    max_mode: Literal["in_process", "external_http", "direct_curl"] = "in_process"
    max_base_url: str = ""
    max_api_key: str = ""
    max_endpoint_mode: Literal["chat", "ask"] = "chat"
    max_curl: str = ""


@router.post("/autopilot/inspect-curl")
async def inspect_agent_curl_endpoint(
    body: InspectAgentCurlRequest,
    request: Request,
) -> dict[str, Any]:
    """Agent Mode cURL'ünü ağa çıkmadan doğrular (gizli değer döndürmez)."""
    try:
        cfg = parse_agent_curl(body.agent_curl, chat_id_override=body.chat_id)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="agent_curl") from exc

    agent_sub = cfg.account_identity()
    settings = getattr(request.app.state, "settings", None)
    server_max_subs = (
        extract_all_max_identities(settings) if settings is not None else frozenset()
    )
    same_conflict = bool(agent_sub and agent_sub in server_max_subs)

    return {
        "valid": True,
        "target_domain": cfg.target_domain,
        "chat_id": cfg.chat_id,
        "trigger_address": cfg.effective_trigger_address,
        "source_endpoint_kind": cfg.source_endpoint_kind,
        "has_cookie": bool(cfg.cookie),
        "has_recaptcha_token": bool(cfg.recaptcha_token),
        "has_cx": bool(cfg.cx),
        "has_last_user_message_id": bool(cfg.last_user_message_id),
        "has_account_identity": bool(agent_sub),
        "same_account_conflict": same_conflict,
        "warnings": cfg.warnings,
    }


@router.post("/autopilot/runs")
async def start_autopilot_run(
    body: StartAutopilotRequest,
    request: Request,
) -> dict[str, Any]:
    """Yeni bir kendi kendine yürüyen `max` ↔ Agent Mode proje koşusu başlatır."""
    manager = _get_manager(request)
    completion_service = getattr(request.app.state, "completion_service", None)
    alerter = getattr(request.app.state, "alerter", None)

    try:
        state = manager.start_run(
            project_idea=body.project_idea,
            agent_curl=body.agent_curl,
            chat_id_override=body.chat_id,
            max_turns=body.max_turns,
            gap_min_seconds=body.gap_min_seconds,
            gap_max_seconds=body.gap_max_seconds,
            agent_turn_timeout=body.agent_turn_timeout,
            max_mode=body.max_mode,
            in_process_service=completion_service,
            external_max_url=body.max_base_url,
            external_max_api_key=body.max_api_key,
            external_endpoint_mode=body.max_endpoint_mode,
            direct_max_curl=body.max_curl,
            alerter=alerter,
        )
    except SameAccountError as exc:
        raise InvalidRequestError(str(exc), param="agent_curl") from exc
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc

    return {
        "run_id": state.run_id,
        "chat_id": state.chat_id,
        "target_domain": state.target_domain,
        "agent_url": state.agent_url,
        "status": state.status,
        "max_turns": state.max_turns,
        "started_at": state.started_at,
    }


@router.get("/autopilot/runs")
async def list_autopilot_runs(request: Request) -> dict[str, Any]:
    """Başlatılmış otopilot koşularının özet listesini döndürür."""
    manager = _get_manager(request)
    return {
        "active_count": manager.active_count(),
        "runs": manager.list_runs(),
    }


@router.get("/autopilot/runs/{run_id}")
async def get_autopilot_run(run_id: str, request: Request) -> dict[str, Any]:
    """Tek bir otopilot koşusunun hafıza defterini ve tüm tur kayıtlarını döndürür."""
    manager = _get_manager(request)
    managed = manager.get(run_id)
    if managed is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")

    data = managed.orchestrator.state.to_dict()
    data["last_note"] = managed.last_note
    return data


@router.post("/autopilot/runs/{run_id}/stop")
async def stop_autopilot_run(run_id: str, request: Request) -> dict[str, Any]:
    """Çalışan bir otopilot koşusuna güvenli durdurma işareti gönderir."""
    manager = _get_manager(request)
    state = manager.stop_run(run_id)
    if state is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")
    return {
        "run_id": state.run_id,
        "status": state.status,
        "stop_requested": True,
    }
