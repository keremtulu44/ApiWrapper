"""Arka planda çalışan otopilot koşularının bellek içi yöneticisi.

Yalnızca `/v1/autopilot/*` uçları kullanıldığında devreye girer; uygulama
açılışında hiçbir ağ isteği veya arka plan görevi başlatmaz. Ana `max` istek
hattından tamamen yalıtılmıştır.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Literal

from app.autopilot.agent_client import AgentSessionClient
from app.autopilot.max_bridge import (
    DirectCurlMaxBridge,
    ExternalHttpMaxBridge,
    InProcessMaxBridge,
    MaxBridge,
)
from app.autopilot.orchestrator import (
    AutopilotConfig,
    AutopilotOrchestrator,
    AutopilotRunState,
)
from app.core.logging import get_logger
from app.services.alerting import TelegramAlerter
from app.services.completion_service import CompletionService

logger = get_logger(__name__)


@dataclass(slots=True)
class ManagedRun:
    orchestrator: AutopilotOrchestrator
    task: asyncio.Task[AutopilotRunState]
    last_note: str = ""


class AutopilotManager:
    """API üzerinden başlatılan otopilot koşularını izler ve yönetir."""

    def __init__(self, *, max_concurrent_runs: int = 10) -> None:
        self.max_concurrent_runs = max(1, max_concurrent_runs)
        self._runs: dict[str, ManagedRun] = {}
        self._order: list[str] = []
        # Gece sırasına bırakılan projelerin 22:00'de aynı saniyede Max'a yüklenmemesi
        # için sıralı yürütme kilidi (biri bitince sıradaki başlar).
        self._night_semaphore = asyncio.Semaphore(1)

    def active_count(self) -> int:
        return sum(1 for item in self._runs.values() if not item.task.done())

    def get(self, run_id: str) -> ManagedRun | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for rid in reversed(self._order[-50:]):
            item = self._runs.get(rid)
            if item is None:
                continue
            st = item.orchestrator.state
            idea_flat = " ".join(st.project_idea.split())
            if len(idea_flat) > 120:
                idea_flat = idea_flat[:119].rstrip() + "…"
            out.append(
                {
                    "run_id": st.run_id,
                    "chat_id": st.chat_id,
                    "target_domain": st.target_domain,
                    "agent_url": st.agent_url,
                    "project_idea": idea_flat,
                    "status": st.status,
                    "current_turn": st.current_turn,
                    "max_turns": st.max_turns,
                    "planned_turns": st.planned_turns,
                    "auto_turns": st.auto_turns,
                    "respect_night_window": st.respect_night_window,
                    "current_phase": st.current_phase,
                    "last_note": item.last_note,
                    "stop_reason": st.stop_reason,
                    "started_at": st.started_at,
                    "updated_at": st.updated_at,
                    "turn_count": len(st.turns),
                }
            )
        return out

    def start_run(
        self,
        *,
        project_idea: str,
        agent_curl: str,
        chat_id_override: str | None = None,
        max_turns: int = 8,
        auto_turns: bool = False,
        respect_night_window: bool = False,
        night_start_hour: int = 22,
        night_end_hour: int = 7,
        gap_min_seconds: float = 5.0,
        gap_max_seconds: float = 10.0,
        agent_turn_timeout: float = 900.0,
        max_mode: Literal["in_process", "external_http", "direct_curl"] = "in_process",
        in_process_service: CompletionService | None = None,
        external_max_url: str = "",
        external_max_api_key: str = "",
        external_endpoint_mode: Literal["chat", "ask"] = "chat",
        direct_max_curl: str = "",
        alerter: TelegramAlerter | None = None,
    ) -> AutopilotRunState:
        if self.active_count() >= self.max_concurrent_runs:
            raise ValueError(
                f"Aynı anda en fazla {self.max_concurrent_runs} otopilot koşusu "
                "kuyrukta/aktif olabilir."
            )

        agent = AgentSessionClient.from_curl(
            agent_curl,
            chat_id_override=chat_id_override,
            turn_timeout=agent_turn_timeout,
        )

        bridge: MaxBridge
        if max_mode == "external_http":
            bridge = ExternalHttpMaxBridge(
                external_max_url,
                external_max_api_key,
                endpoint_mode=external_endpoint_mode,
            )
        elif max_mode == "direct_curl":
            bridge = DirectCurlMaxBridge(direct_max_curl)
        else:
            if in_process_service is None:
                raise ValueError("Süreç içi CompletionService hazır değil.")
            bridge = InProcessMaxBridge(in_process_service)

        cfg = AutopilotConfig(
            project_idea=project_idea,
            max_turns=max_turns,
            auto_turns=auto_turns,
            respect_night_window=respect_night_window,
            night_start_hour=night_start_hour,
            night_end_hour=night_end_hour,
            gap_min_seconds=gap_min_seconds,
            gap_max_seconds=gap_max_seconds,
        )

        holder: dict[str, ManagedRun] = {}

        def _on_progress(_state: AutopilotRunState, note: str) -> None:
            managed = holder.get("item")
            if managed is not None:
                managed.last_note = note

        orch = AutopilotOrchestrator(
            config=cfg,
            agent=agent,
            max_bridge=bridge,
            alerter=alerter,
            on_progress=_on_progress,
            execution_semaphore=self._night_semaphore,
        )
        # Hesap ayrılığı kontrolünü senkron olarak hemen yap ki 400 dönebilsin.
        orch._verify_separate_accounts()

        async def _runner() -> AutopilotRunState:
            try:
                return await orch.run()
            except Exception as exc:
                logger.warning(
                    "autopilot_background_run_stopped_with_error",
                    run_id=orch.state.run_id,
                    error_type=type(exc).__name__,
                )
                return orch.state

        task = asyncio.create_task(_runner(), name=f"autopilot-{orch.state.run_id}")
        managed = ManagedRun(orchestrator=orch, task=task, last_note="Başlatıldı")
        holder["item"] = managed
        self._runs[orch.state.run_id] = managed
        self._order.append(orch.state.run_id)
        return orch.state

    def stop_run(self, run_id: str) -> AutopilotRunState | None:
        item = self._runs.get(run_id)
        if item is None:
            return None
        item.orchestrator.request_stop()
        item.last_note = "Durdurma isteği alındı; mevcut adım bitince duracak."
        return item.orchestrator.state

    def start_now(self, run_id: str) -> AutopilotRunState | None:
        """Gece penceresini bekleyen bir koşuyu derhal uyandırır."""
        item = self._runs.get(run_id)
        if item is None:
            return None
        item.orchestrator.bypass_night_window()
        item.last_note = "Gece penceresi beklemesi atlandı; hemen başlatılıyor."
        return item.orchestrator.state

    async def shutdown(self) -> None:
        active = [item for item in self._runs.values() if not item.task.done()]
        for item in active:
            item.orchestrator.request_stop()
            item.task.cancel()
        for item in active:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await item.task
