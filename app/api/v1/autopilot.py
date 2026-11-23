"""İzole `/v1/autopilot` HTTP uçları (yalnız `API_KEYS` ile erişilebilir).

Mevcut `/v1/chat/completions`, `/v1/ask` ve `/v1/admin/*` akışlarına hiçbir
etkisi yoktur; yalnızca çağrıldığında `AutopilotManager` üzerinden Agent ↔ Max
proje döngüsünü başlatır ve durumunu raporlar. Ham cURL, çerez veya token
değerleri ne yanıtlara ne de loglara yazılır.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.autopilot.agent_client import AgentSessionClient
from app.autopilot.curl_parse import (
    extract_max_slot_identities,
    inspect_agent_cookie_health,
    parse_agent_curl,
)
from app.autopilot.manager import AutopilotManager
from app.autopilot.orchestrator import (
    TURN_GAP_MAX_SECONDS,
    TURN_GAP_MIN_SECONDS,
    SameAccountError,
)
from app.autopilot.upstream_slots import UpstreamSlotRuntimeManager
from app.core.config import MAX_ACCOUNT_SLOTS
from app.core.errors import InvalidRequestError, ModelNotFoundError
from app.core.security import api_key_dependency
from app.upstream.client import error_excerpt
from app.upstream.exceptions import (
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamQuotaExceeded,
    UpstreamRefreshRequired,
    UpstreamTimeout,
)

router = APIRouter(tags=["autopilot"], dependencies=[Depends(api_key_dependency)])

_SYNTHETIC_TEST_TLDS: tuple[str, ...] = (
    ".example",
    ".example.com",
    ".example.org",
    ".example.net",
    ".test",
    ".invalid",
    ".localhost",
)


def _is_synthetic_test_domain(host: str) -> bool:
    lowered = (host or "").strip().lower().split(":", 1)[0]
    if lowered in {"localhost", "127.0.0.1", "::1", "target.example"}:
        return True
    return lowered.endswith(_SYNTHETIC_TEST_TLDS)


def _get_manager(request: Request) -> AutopilotManager:
    manager: AutopilotManager | None = getattr(request.app.state, "autopilot_manager", None)
    if manager is None:
        # Kalıcılık klasörü `Settings`'ten gelir (`AUTOPILOT_STATE_DIR`); boşsa
        # koşular yalnız bellekte tutulur (eski davranış korunur).
        settings = getattr(request.app.state, "settings", None)
        state_dir = getattr(settings, "autopilot_state_dir", "") or None
        manager = AutopilotManager(state_dir=state_dir)
        request.app.state.autopilot_manager = manager
    return manager


def _get_slot_manager(request: Request) -> UpstreamSlotRuntimeManager:
    slot_mgr: UpstreamSlotRuntimeManager | None = getattr(
        request.app.state, "slot_manager", None
    )
    if slot_mgr is None:
        raise InvalidRequestError("Upstream slot yöneticisi hazır değil.")
    return slot_mgr


class InspectAgentCurlRequest(BaseModel):
    agent_curl: str = Field(..., min_length=10)
    chat_id: str | None = None
    verify_live: bool = False


class UpdateUpstreamSlotRequest(BaseModel):
    upstream_curl: str = Field(..., min_length=20)


class ClearAutopilotRunsRequest(BaseModel):
    include_active: bool = True
    run_id: str | None = None


class StopAllAutopilotRunsRequest(BaseModel):
    force: bool = True


class ExtendAutopilotRequest(BaseModel):
    """Bitmemiş bir koşuya eklenecek tur sayısı (koşu kapanmadan sürer)."""

    extra_turns: int = Field(default=4, ge=1, le=24)


class ResumeAutopilotRequest(BaseModel):
    agent_curl: str = Field(..., min_length=20)
    respect_night_window: bool | None = None
    max_turns: int | None = Field(default=None, ge=1, le=25)
    agent_turn_timeout: float | None = Field(default=None, ge=0.0)


class StartAutopilotRequest(BaseModel):
    project_idea: str = Field(..., min_length=5, max_length=20000)
    agent_curl: str = Field(..., min_length=20)
    chat_id: str | None = None
    max_turns: int = Field(default=8, ge=1, le=25)
    auto_turns: bool = False
    #: Gece penceresi artık **opt-in**: varsayılan "hemen ilerle". 22:00-07:00
    #: arasında beklemek sürekliliğin en büyük düşmanıydı (gündüz başlatılan iş
    #: akşama kadar hiç ilerlemiyordu).
    respect_night_window: bool = False
    night_start_hour: int = Field(default=22, ge=0, le=23)
    night_end_hour: int = Field(default=7, ge=0, le=23)
    #: Tur aralığı artık küçük: nazik aralık tele giden yolda (REQUEST_GAP_*)
    #: uygulanıyor, ikisi birlikte tur başına ~45 sn saf bekleme üretiyordu.
    gap_min_seconds: float = Field(default=TURN_GAP_MIN_SECONDS, ge=0.0, le=120.0)
    gap_max_seconds: float = Field(default=TURN_GAP_MAX_SECONDS, ge=0.0, le=180.0)
    agent_turn_timeout: float | None = Field(default=None, ge=0.0)
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
    """Agent Mode cURL'ünü ve istenirse canlı oturum sağlığını doğrular (secret sızdırmaz)."""
    try:
        cfg = parse_agent_curl(body.agent_curl, chat_id_override=body.chat_id)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="agent_curl") from exc

    agent_sub = cfg.account_identity()
    cookie_health = inspect_agent_cookie_health(cfg.cookie, cfg.access_token)
    settings = getattr(request.app.state, "settings", None)
    slot_subs = (
        extract_max_slot_identities(settings) if settings is not None else {}
    )
    conflict_slots = (
        sorted(slot for slot, sub in slot_subs.items() if sub == agent_sub)
        if agent_sub
        else []
    )
    same_conflict = bool(conflict_slots)

    result: dict[str, Any] = {
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
        "conflict_slots": conflict_slots,
        "cookie_diagnostics": cookie_health,
        "live_checked": False,
        "live_ok": None,
        "warnings": cfg.warnings,
    }

    if (
        body.verify_live
        and not same_conflict
        and not _is_synthetic_test_domain(cfg.target_domain)
    ):
        try:
            async with AgentSessionClient(
                cfg,
                connect_timeout=10.0,
                read_timeout=15.0,
            ) as probe_client:
                before_names = {
                    (c.domain, c.name): c.value for c in probe_client.client.cookies.jar
                }
                state = await probe_client.fetch_state(recover_run=False)
                after_names = {
                    (c.domain, c.name): c.value for c in probe_client.client.cookies.jar
                }
                rotated = bool(after_names and after_names != before_names)
                result.update(
                    {
                        "live_checked": True,
                        "live_ok": True,
                        "live_message_count": len(state.messages),
                        "live_has_pending_turn": state.has_pending_turn,
                        "cookie_rotated_on_probe": rotated,
                    }
                )
        except (
            UpstreamAuthRejected,
            RecaptchaRejected,
            UpstreamQuotaExceeded,
            UpstreamRefreshRequired,
            UpstreamHTTPError,
        ) as exc:
            err_name = (
                "UpstreamAuthRejected"
                if exc.status_code == 401 and type(exc) is UpstreamHTTPError
                else type(exc).__name__
            )
            result.update(
                {
                    "live_checked": True,
                    "live_ok": False,
                    "live_status_code": exc.status_code,
                    "live_error_type": err_name,
                    "live_error_detail": error_excerpt(str(exc), limit=220),
                }
            )
        except (UpstreamTimeout, UpstreamNetworkError) as exc:
            result.update(
                {
                    "live_checked": True,
                    "live_ok": None,
                    "live_error_type": type(exc).__name__,
                    "live_error_detail": error_excerpt(str(exc), limit=180),
                }
            )

    return result


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
            auto_turns=body.auto_turns,
            respect_night_window=body.respect_night_window,
            night_start_hour=body.night_start_hour,
            night_end_hour=body.night_end_hour,
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
        "auto_turns": state.auto_turns,
        "respect_night_window": state.respect_night_window,
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


@router.post("/autopilot/runs/stop-all")
async def stop_all_autopilot_runs(
    request: Request,
    body: StopAllAutopilotRunsRequest | None = None,
) -> dict[str, Any]:
    """Aktif veya gece sırasındaki tüm otopilot koşularını topluca durdurur."""
    manager = _get_manager(request)
    force = True if body is None else body.force
    return await manager.stop_all_runs(force=force)


@router.post("/autopilot/runs/clear")
async def clear_autopilot_runs(
    request: Request,
    body: ClearAutopilotRunsRequest | None = None,
) -> dict[str, Any]:
    """Çalışan/bekleyen/bitmiş otopilot işlemlerini güvenli biçimde durdurup temizler."""
    manager = _get_manager(request)
    include_active = True if body is None else body.include_active
    run_id = None if body is None else body.run_id
    try:
        return await manager.clear_runs(include_active=include_active, run_id=run_id)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="run_id") from exc


@router.get("/autopilot/runs/{run_id}")
async def get_autopilot_run(run_id: str, request: Request) -> dict[str, Any]:
    """Tek bir otopilot koşusunun hafıza defterini ve tüm tur kayıtlarını döndürür."""
    manager = _get_manager(request)
    managed = manager.get(run_id)
    if managed is not None:
        data = managed.orchestrator.state.to_dict()
        data["last_note"] = managed.last_note
        data["restored_from_disk"] = False
        return data
    archived = manager.get_state(run_id)
    if archived is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")
    data = archived.to_dict()
    data["last_note"] = "Diskten geri yüklendi (süreç yeniden başladı); /devam ile sürdürün."
    data["restored_from_disk"] = True
    return data


@router.post("/autopilot/runs/{run_id}/stop")
async def stop_autopilot_run(
    run_id: str,
    request: Request,
    force: bool = Query(default=False),
) -> dict[str, Any]:
    """Çalışan bir otopilot koşusuna güvenli veya `force=true` ile anında durdurma gönderir."""
    manager = _get_manager(request)
    state = manager.stop_run(run_id, force=force)
    if state is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")
    return {
        "run_id": state.run_id,
        "status": state.status,
        "stop_requested": True,
        "forced": force,
    }


@router.post("/autopilot/runs/{run_id}/extend")
async def extend_autopilot_run(
    run_id: str,
    request: Request,
    body: ExtendAutopilotRequest | None = None,
) -> dict[str, Any]:
    """Tur tavanını uzatır; `max_turns_reached` artık koşunun ölümü değil.

    Aynı Agent sohbeti (aynı dosyalar, aynı sunucular) korunur; yalnız izin
    verilen tur sayısı artar. Varsayılan uzatma 4 turdur.
    """
    manager = _get_manager(request)
    extra = 4 if body is None else body.extra_turns
    try:
        state = manager.extend_run_turns(run_id, extra)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="run_id") from exc
    if state is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")
    return {
        "run_id": state.run_id,
        "status": state.status,
        "max_turns": state.max_turns,
        "turn_extensions": state.turn_extensions,
    }


@router.post("/autopilot/runs/{run_id}/start-now")
async def start_autopilot_run_now(run_id: str, request: Request) -> dict[str, Any]:
    """Gece penceresini bekleyen bir koşuyu 22:00'yi beklemeden derhal başlatır."""
    manager = _get_manager(request)
    state = manager.start_now(run_id)
    if state is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")
    return {
        "run_id": state.run_id,
        "status": state.status,
        "bypassed_night_window": True,
    }


@router.post("/autopilot/runs/{run_id}/resume")
async def resume_autopilot_run(
    run_id: str,
    body: ResumeAutopilotRequest,
    request: Request,
) -> dict[str, Any]:
    """Yarıda kalmış bir otopilot koşusunu taze Agent cURL'üyle kaldığı yerden sürdürür."""
    manager = _get_manager(request)
    completion_service = getattr(request.app.state, "completion_service", None)
    alerter = getattr(request.app.state, "alerter", None)
    try:
        state = manager.resume_run(
            run_id,
            agent_curl=body.agent_curl,
            respect_night_window=body.respect_night_window,
            max_turns=body.max_turns,
            agent_turn_timeout=body.agent_turn_timeout,
            in_process_service=completion_service,
            alerter=alerter,
        )
    except SameAccountError as exc:
        raise InvalidRequestError(str(exc), param="agent_curl") from exc
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="agent_curl") from exc

    if state is None:
        raise ModelNotFoundError(f"Autopilot run '{run_id}' bulunamadı.", param="run_id")

    return {
        "resumed": True,
        "run_id": state.run_id,
        "chat_id": state.chat_id,
        "agent_url": state.agent_url,
        "status": state.status,
        "current_turn": state.current_turn,
        "turn_count": len(state.turns),
        "max_turns": state.max_turns,
        "respect_night_window": state.respect_night_window,
    }


@router.get("/autopilot/upstream-slots")
async def get_upstream_slots_status(request: Request) -> dict[str, Any]:
    """Canlı `max` hesap yuvalarının (Yuva 1 & Yuva 2) secret-free durumunu döndürür."""
    slot_mgr = _get_slot_manager(request)
    autopilot_mgr: AutopilotManager | None = getattr(
        request.app.state, "autopilot_manager", None
    )
    snap = slot_mgr.status_snapshot()
    snap["active_autopilot_runs"] = (
        autopilot_mgr.active_count() if autopilot_mgr is not None else 0
    )
    return snap


@router.post("/autopilot/upstream-slots/{slot}/close")
async def close_upstream_slot(slot: int, request: Request) -> dict[str, Any]:
    """Bir `max` yuvasını (2, 3, 4) bağlantısı ve keepalive'ıyla kökten bırakır.

    Gece Agent Mode'a hazırlığın genel hâli: hangi hesap serbest bırakılacaksa
    yuvası buradan kapatılır. 1. yuva bırakılamaz (ana hesap).
    """
    slot_mgr = _get_slot_manager(request)
    try:
        return await slot_mgr.release_slot(slot)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="slot") from exc


@router.post("/autopilot/upstream-slots/2/close")
async def close_upstream_slot_2(request: Request) -> dict[str, Any]:
    """2. `max` hesabının (`upstream_curl2`) bağlantısını ve kaydını kökten kapatır."""
    slot_mgr = _get_slot_manager(request)
    return await slot_mgr.close_slot_2()


class SetSlotRoleRequest(BaseModel):
    """Yuvanın rolü. Hesap **kapatılmaz**; yalnız görevi değişir."""

    role: Literal["max", "spare", "agent", "yedek", "ajan"] = "max"


@router.post("/autopilot/upstream-slots/{slot}/role")
async def set_upstream_slot_role(
    slot: int, body: SetSlotRoleRequest, request: Request
) -> dict[str, Any]:
    """Bir yuvaya rol verir: `max` (gündüz trafiği), `spare` (sıcak yedek),
    `agent` (Agent projesi hesabı).

    Hesap açık kalır: cURL/çerez/token silinmez, yuva yerinde durur. `agent`
    rolündeki yuva gündüz `max` isteği almaz ve keepalive yapılmaz; `spare`
    trafik almaz ama açık ve sıcak bekler.
    """
    slot_mgr = _get_slot_manager(request)
    try:
        return await slot_mgr.set_role(slot, body.role)
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="role") from exc


class SwapUpstreamSlotsRequest(BaseModel):
    """İki Max yuvasının rollerini takas eder (3 hesaplı rotasyon)."""

    slot_a: int = Field(..., ge=1, le=MAX_ACCOUNT_SLOTS)
    slot_b: int = Field(..., ge=1, le=MAX_ACCOUNT_SLOTS)


@router.post("/autopilot/upstream-slots/swap")
async def swap_upstream_slots(
    body: SwapUpstreamSlotsRequest,
    request: Request,
) -> dict[str, Any]:
    """İki yuvanın **yerini** değiştirir; hesaplar yer değiştirir, trafik şekli değişmez.

    Üç hesapta rotasyonun çekirdeği: hiçbir hesap sürekli aynı rolde kalmayıp
    Max-1 / Max-2 / yedek / Agent rolleri dönüşümlü olur. Takas atomiktir; aynı
    Google hesabı iki yuvada görünmez.
    """
    slot_mgr = _get_slot_manager(request)
    autopilot_mgr: AutopilotManager | None = getattr(
        request.app.state, "autopilot_manager", None
    )
    try:
        return await slot_mgr.swap_roles(
            body.slot_a, body.slot_b, autopilot_manager=autopilot_mgr
        )
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="slot") from exc


@router.post("/autopilot/upstream-slots/{slot}")
async def update_upstream_slot(
    slot: int,
    body: UpdateUpstreamSlotRequest,
    request: Request,
) -> dict[str, Any]:
    """Bir `max` yuvasının (`upstream_curl` / `upstream_curl_N`) cURL'ünü canlı günceller.

    3 ve 4 numaralı yuvalar desteklidir; üç hesap rotasyonunda yedek/Agent
    hesabını Max hattına almak için kullanılır.
    """
    if not 1 <= slot <= MAX_ACCOUNT_SLOTS:
        raise InvalidRequestError(
            f"Yuva 1..{MAX_ACCOUNT_SLOTS} arasında olmalıdır.", param="slot"
        )
    slot_mgr = _get_slot_manager(request)
    autopilot_mgr: AutopilotManager | None = getattr(
        request.app.state, "autopilot_manager", None
    )
    try:
        return await slot_mgr.update_slot(
            slot,
            body.upstream_curl,
            autopilot_manager=autopilot_mgr,
        )
    except ValueError as exc:
        raise InvalidRequestError(str(exc), param="upstream_curl") from exc

