"""Arka planda çalışan otopilot koşularının bellek içi yöneticisi.

Yalnızca `/v1/autopilot/*` uçları kullanıldığında devreye girer; uygulama
açılışında hiçbir ağ isteği veya arka plan görevi başlatmaz. Ana `max` istek
hattından tamamen yalıtılmıştır.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Literal

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
    split_notification_chunks,
)
from app.core.logging import get_logger
from app.services.alerting import TelegramAlerter
from app.services.completion_service import CompletionService
from app.upstream.client import error_excerpt

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
        self._morning_tasks: dict[str, asyncio.Task[None]] = {}

    def active_count(self) -> int:
        return sum(1 for item in self._runs.values() if not item.task.done())

    def active_agent_identities(self) -> dict[str, str]:
        """Aktif/gece sırasındaki koşuların Google `sub` -> `run_id` eşlemesini döndürür."""
        active: dict[str, str] = {}
        for rid, item in self._runs.items():
            if item.task.done():
                continue
            st = item.orchestrator.state.status
            if st not in {"starting", "waiting_window", "running"}:
                continue
            sub = item.orchestrator.agent.config.account_identity()
            if sub:
                active[sub] = rid
        return active

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
                    "has_handoff_report": bool(st.handoff_report.strip()),
                    "morning_report_pending": rid in self._morning_tasks,
                    "current_phase": st.current_phase,
                    "last_note": item.last_note,
                    "stop_reason": st.stop_reason,
                    "error_detail": (
                        error_excerpt(st.error_detail, limit=200)
                        if st.error_detail
                        else ""
                    ),
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
        gap_min_seconds: float = 15.0,
        gap_max_seconds: float = 30.0,
        agent_turn_timeout: float | None = None,
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

        def _schedule_morning(rid: str, report_text: str, delay_seconds: float) -> None:
            if alerter is None or not alerter.enabled:
                return
            old_task = self._morning_tasks.pop(rid, None)
            if old_task is not None and not old_task.done():
                old_task.cancel()

            async def _deliver() -> None:
                try:
                    await asyncio.sleep(delay_seconds)
                    for chunk in split_notification_chunks(report_text):
                        await alerter.send(chunk, label="autopilot_morning")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "autopilot_morning_report_send_failed",
                        run_id=rid,
                        error_type=type(exc).__name__,
                    )
                finally:
                    self._morning_tasks.pop(rid, None)

            self._morning_tasks[rid] = asyncio.create_task(
                _deliver(), name=f"autopilot-morning-{rid}"
            )

        orch = AutopilotOrchestrator(
            config=cfg,
            agent=agent,
            max_bridge=bridge,
            alerter=alerter,
            on_progress=_on_progress,
            execution_semaphore=self._night_semaphore,
            morning_dispatcher=_schedule_morning,
        )
        # Hesap ayrılığı kontrolünü senkron olarak hemen yap ki 400 dönebilsin.
        orch._verify_separate_accounts()

        async def _runner() -> AutopilotRunState:
            try:
                return await orch.run()
            except asyncio.CancelledError:
                logger.info(
                    "autopilot_background_run_cancelled",
                    run_id=orch.state.run_id,
                )
                return orch.state
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

    def resume_run(
        self,
        run_id: str,
        *,
        agent_curl: str,
        respect_night_window: bool | None = None,
        max_turns: int | None = None,
        agent_turn_timeout: float | None = None,
        in_process_service: CompletionService | None = None,
        alerter: TelegramAlerter | None = None,
    ) -> AutopilotRunState | None:
        """Yarıda kalmış (`failed` / `stopped` / `max_turns_reached`) bir koşuyu
        taze Agent cURL'üyle hafıza ve tur geçmişini koruyarak kaldığı yerden sürdürür.
        """
        existing = self._runs.get(run_id)
        if existing is None:
            return None
        if not existing.task.done():
            raise ValueError(
                f"'{run_id}' zaten çalışıyor veya gece sırasında bekliyor."
            )
        prev_orch = existing.orchestrator
        prev_state = prev_orch.state
        if prev_state.status == "completed":
            raise ValueError(f"'{run_id}' zaten tamamlanmış durumda.")
        if self.active_count() >= self.max_concurrent_runs:
            raise ValueError(
                f"Aynı anda en fazla {self.max_concurrent_runs} otopilot koşusu "
                "kuyrukta/aktif olabilir."
            )

        agent = AgentSessionClient.from_curl(
            agent_curl,
            turn_timeout=agent_turn_timeout,
        )
        if prev_state.chat_id and agent.config.chat_id != prev_state.chat_id:
            raise ValueError(
                f"Gönderilen cURL farklı bir Agent sohbetine ({agent.config.chat_id}) ait; "
                f"'{run_id}' koşusunu sürdürmek için {prev_state.chat_id} sohbetinden "
                "alınmış taze cURL kullanın."
            )

        if in_process_service is not None:
            bridge: MaxBridge = InProcessMaxBridge(in_process_service)
        else:
            bridge = prev_orch.max_bridge

        prev_cfg = prev_orch.config
        eff_max_turns = max_turns if max_turns is not None else prev_cfg.max_turns
        eff_night = (
            respect_night_window
            if respect_night_window is not None
            else prev_cfg.respect_night_window
        )
        cfg = AutopilotConfig(
            project_idea=prev_state.project_idea,
            max_turns=eff_max_turns,
            auto_turns=prev_cfg.auto_turns,
            min_turns_before_complete=prev_cfg.min_turns_before_complete,
            respect_night_window=eff_night,
            night_start_hour=prev_cfg.night_start_hour,
            night_end_hour=prev_cfg.night_end_hour,
            gap_min_seconds=prev_cfg.gap_min_seconds,
            gap_max_seconds=prev_cfg.gap_max_seconds,
            run_id=prev_state.run_id,
        )

        prev_state.status = "starting"
        prev_state.stop_reason = ""
        prev_state.error_detail = ""
        eff_alerter = alerter if alerter is not None else prev_orch.alerter

        holder: dict[str, ManagedRun] = {}

        def _on_progress(_state: AutopilotRunState, note: str) -> None:
            managed_item = holder.get("item")
            if managed_item is not None:
                managed_item.last_note = note

        def _schedule_morning(rid: str, report_text: str, delay_seconds: float) -> None:
            if eff_alerter is None or not eff_alerter.enabled:
                return
            old_task = self._morning_tasks.pop(rid, None)
            if old_task is not None and not old_task.done():
                old_task.cancel()

            async def _deliver() -> None:
                try:
                    await asyncio.sleep(delay_seconds)
                    for chunk in split_notification_chunks(report_text):
                        await eff_alerter.send(chunk, label="autopilot_morning")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "autopilot_morning_report_send_failed",
                        run_id=rid,
                        error_type=type(exc).__name__,
                    )
                finally:
                    self._morning_tasks.pop(rid, None)

            self._morning_tasks[rid] = asyncio.create_task(
                _deliver(), name=f"autopilot-morning-{rid}"
            )

        orch = AutopilotOrchestrator(
            config=cfg,
            agent=agent,
            max_bridge=bridge,
            alerter=eff_alerter,
            on_progress=_on_progress,
            resume_state=prev_state,
            execution_semaphore=self._night_semaphore,
            morning_dispatcher=_schedule_morning,
        )
        orch._verify_separate_accounts()

        async def _runner() -> AutopilotRunState:
            try:
                return await orch.run()
            except asyncio.CancelledError:
                logger.info("autopilot_background_run_cancelled", run_id=orch.state.run_id)
                return orch.state
            except Exception as exc:
                logger.warning(
                    "autopilot_background_run_stopped_with_error",
                    run_id=orch.state.run_id,
                    error_type=type(exc).__name__,
                )
                return orch.state

        task = asyncio.create_task(_runner(), name=f"autopilot-{orch.state.run_id}")
        managed = ManagedRun(
            orchestrator=orch,
            task=task,
            last_note="Kaldığı yerden yeniden başlatıldı",
        )
        holder["item"] = managed
        self._runs[prev_state.run_id] = managed
        return orch.state

    def stop_run(
        self,
        run_id: str,
        *,
        force: bool = False,
    ) -> AutopilotRunState | None:
        item = self._runs.get(run_id)
        if item is None:
            return None
        mtask = self._morning_tasks.pop(run_id, None)
        if mtask is not None and not mtask.done():
            mtask.cancel()
        item.orchestrator.request_stop()
        if force and not item.task.done():
            st = item.orchestrator.state
            if st.status not in {"completed", "max_turns_reached", "failed"}:
                st.status = "stopped"
                st.stop_reason = "Kullanıcı tarafından anında durduruldu."
            item.last_note = "Anında durduruldu ve bağlantılar kapatılıyor."
            item.task.cancel()
        else:
            item.last_note = "Durdurma isteği alındı; mevcut adım bitince duracak."
        return item.orchestrator.state

    async def stop_all_runs(self, *, force: bool = True) -> dict[str, Any]:
        """Aktif veya gece sırasındaki tüm otopilot koşularını durdurur."""
        to_await: list[asyncio.Task[AutopilotRunState]] = []
        stopped_ids: list[str] = []
        for rid, item in list(self._runs.items()):
            if item.task.done():
                continue
            stopped_ids.append(rid)
            self.stop_run(rid, force=force)
            if force:
                to_await.append(item.task)
        for task in to_await:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return {
            "stopped_count": len(stopped_ids),
            "stopped_run_ids": stopped_ids,
            "active_count": self.active_count(),
        }

    async def clear_runs(
        self,
        *,
        include_active: bool = True,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Otopilot işlemlerini güvenli biçimde durdurup bellek listesinden temizler.

        1. ana Max hesabına (`slot_manager` / `upstream_curl`) kesinlikle dokunmaz;
        yalnızca Otopilot Agent koşularını, bekleyen sabah raporu zamanlayıcılarını
        ve geçici Agent çerez önbelleğini temizler.
        """
        target_rid = (run_id or "").strip()
        tasks_to_await: list[asyncio.Task[Any]] = []
        stopped_active = 0
        cancelled_morning = 0
        removed_ids: list[str] = []

        if target_rid:
            item = self._runs.get(target_rid)
            if item is None:
                return {
                    "found": False,
                    "stopped_active": 0,
                    "cancelled_morning_reports": 0,
                    "removed_runs": 0,
                    "removed_run_ids": [],
                    "remaining_runs": len(self._runs),
                }
            if not item.task.done():
                if not include_active:
                    raise ValueError(
                        f"'{target_rid}' şu anda aktif; silmek için önce durdurun veya "
                        "include_active=true kullanın."
                    )
                stopped_active += 1
                self.stop_run(target_rid, force=True)
                tasks_to_await.append(item.task)
            mtask = self._morning_tasks.pop(target_rid, None)
            if mtask is not None and not mtask.done():
                cancelled_morning += 1
                mtask.cancel()
                tasks_to_await.append(mtask)
            for task in tasks_to_await:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._runs.pop(target_rid, None)
            self._order = [r for r in self._order if r != target_rid]
            removed_ids.append(target_rid)
            return {
                "found": True,
                "stopped_active": stopped_active,
                "cancelled_morning_reports": cancelled_morning,
                "removed_runs": 1,
                "removed_run_ids": removed_ids,
                "remaining_runs": len(self._runs),
            }

        for rid, item in list(self._runs.items()):
            is_live = not item.task.done()
            if is_live and not include_active:
                continue
            if is_live:
                stopped_active += 1
                self.stop_run(rid, force=True)
                tasks_to_await.append(item.task)
            mtask = self._morning_tasks.pop(rid, None)
            if mtask is not None and not mtask.done():
                cancelled_morning += 1
                mtask.cancel()
                tasks_to_await.append(mtask)
            removed_ids.append(rid)

        if include_active:
            for rid, mtask in list(self._morning_tasks.items()):
                self._morning_tasks.pop(rid, None)
                if not mtask.done():
                    cancelled_morning += 1
                    mtask.cancel()
                    tasks_to_await.append(mtask)

        for task in tasks_to_await:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

        removed_set = set(removed_ids)
        for rid in removed_ids:
            self._runs.pop(rid, None)
        self._order = [r for r in self._order if r not in removed_set]

        logger.info(
            "autopilot_runs_cleared",
            stopped_active=stopped_active,
            cancelled_morning=cancelled_morning,
            removed_runs=len(removed_ids),
            remaining_runs=len(self._runs),
        )
        return {
            "found": True,
            "stopped_active": stopped_active,
            "cancelled_morning_reports": cancelled_morning,
            "removed_runs": len(removed_ids),
            "removed_run_ids": removed_ids,
            "remaining_runs": len(self._runs),
        }

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
        morning = list(self._morning_tasks.values())
        for item in active:
            item.orchestrator.request_stop()
            item.task.cancel()
        for mtask in morning:
            mtask.cancel()
        for item in active:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await item.task
        for mtask in morning:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await mtask
