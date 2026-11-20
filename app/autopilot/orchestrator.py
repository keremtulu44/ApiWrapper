"""Kendi kendine yürüyen proje orkestratörü (`AutopilotOrchestrator`).

Akış:
1. Başlangıçta **iki Google hesabının ayrı olduğunu** doğrular (Agent oturumu ile
   `max` oturumu aynı JWT `sub` kimliğini taşıyorsa çalışmayı reddeder).
2. Agent Mode sohbetine bağlanır (`GET /api/chat/{id}?recoverRun=false`), yarım
   kalan bir ajan çalışması varsa bitmesini bekler.
3. Durumsuz `max` moduna proje fikrini vererek 1. tur başlangıç talimatını üretir,
   Agent'a gönderir (`POST /api/chat/{id}/input`) ve akışı (`GET /events`) izler.
4. Her tur sonunda Agent'ın hem metnini hem de **gerçek sandbox araç/komut kanıtını**
   çıkarır; durumsuz `max`'a güncellenmiş hafıza defteriyle birlikte sunar.
5. `max` `<DURUM>TAMAMLANDI</DURUM>` verene, `TAKILDI` diyene veya `max_turns`
   tavanına ulaşana dek doğal rastgele aralıklarla (`5–10 sn`) projeyi yürütür.
6. Her adımı gizli değer içermeyen bir JSON durum dosyasına (`checkpoint`) yazar;
   böylece süreç izlenebilir ve kesilirse kaldığı hafızadan devam ettirilebilir.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.autopilot.agent_client import AgentSessionClient, DeltaCallback
from app.autopilot.events_parser import AgentMessage, format_sandbox_evidence
from app.autopilot.max_bridge import MaxBridge, MaxBridgeError
from app.autopilot.prompts import (
    AGENT_OPERATING_GUARDRAILS,
    MAX_SYSTEM_PROMPT,
    MaxDecision,
    TurnDigest,
    build_early_exit_fallback_instruction,
    build_kickoff_user_prompt,
    build_turn_user_prompt,
    compute_min_turns_before_completion,
    parse_max_decision,
    similarity_ratio,
)
from app.core.logging import get_logger
from app.services.alerting import TelegramAlerter
from app.utils.ids import random_suffix

logger = get_logger(__name__)


class SameAccountError(ValueError):
    """Agent cURL'ü ile `max` cURL'ü aynı Google hesabına ait olduğunda fırlatılır."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_one_line(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


@dataclass(slots=True)
class AutopilotConfig:
    """Tek bir otopilot koşusunun çalışma parametreleri."""

    project_idea: str
    max_turns: int = 8
    min_turns_before_complete: int | None = None
    gap_min_seconds: float = 5.0
    gap_max_seconds: float = 10.0
    state_file: Path | None = None
    run_id: str = field(default_factory=lambda: f"auto_{random_suffix(12)}")
    append_guardrails_on_first_turn: bool = True

    @property
    def effective_min_turns(self) -> int:
        raw = (
            self.min_turns_before_complete
            if self.min_turns_before_complete is not None
            else compute_min_turns_before_completion(self.max_turns)
        )
        return max(1, min(self.max_turns, raw))

    def validate(self) -> None:
        if not self.project_idea or not self.project_idea.strip():
            raise ValueError("Proje fikri (project_idea) boş olamaz.")
        if self.max_turns < 1 or self.max_turns > 30:
            raise ValueError("max_turns 1 ile 30 arasında olmalıdır.")
        if self.min_turns_before_complete is not None and (
            self.min_turns_before_complete < 1
            or self.min_turns_before_complete > self.max_turns
        ):
            raise ValueError("min_turns_before_complete 1 ile max_turns arasında olmalıdır.")
        if self.gap_min_seconds < 0 or self.gap_max_seconds < self.gap_min_seconds:
            raise ValueError("gap_min_seconds / gap_max_seconds aralığı geçersiz.")


@dataclass(slots=True)
class AutopilotTurnRecord:
    """Tamamlanmış tek bir turun gizli değer içermeyen kaydı."""

    turn: int
    phase: str
    max_status: str
    max_evaluation: str
    sent_to_agent: str
    agent_message_id: str
    agent_reply_text: str
    sandbox_evidence: str
    tool_count: int
    bash_count: int
    error_tool_count: int
    duration_seconds: float
    completed_at: str


@dataclass(slots=True)
class AutopilotRunState:
    """Otopilot koşusunun anlık ve kalıcı durumu (secret içermez)."""

    run_id: str
    chat_id: str
    project_idea: str
    target_domain: str = ""
    # starting | running | completed | stopped | failed | max_turns_reached
    status: str = "starting"
    current_turn: int = 0
    max_turns: int = 8
    current_phase: str = "1_VIZYON_VE_CEKIRDEK"
    memory_summary: str = ""
    started_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)
    stop_reason: str = ""
    error_detail: str = ""
    turns: list[AutopilotTurnRecord] = field(default_factory=list)

    @property
    def agent_url(self) -> str:
        if self.target_domain and self.chat_id:
            return f"https://{self.target_domain}/agent/{self.chat_id}"
        return ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["agent_url"] = self.agent_url
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutopilotRunState:
        raw_turns = data.get("turns") or []
        turns = [
            AutopilotTurnRecord(**item)
            for item in raw_turns
            if isinstance(item, dict)
        ]
        return cls(
            run_id=str(data.get("run_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            project_idea=str(data.get("project_idea") or ""),
            target_domain=str(data.get("target_domain") or ""),
            status=str(data.get("status") or "starting"),
            current_turn=int(data.get("current_turn") or 0),
            max_turns=int(data.get("max_turns") or 8),
            current_phase=str(data.get("current_phase") or "1_VIZYON_VE_CEKIRDEK"),
            memory_summary=str(data.get("memory_summary") or ""),
            started_at=str(data.get("started_at") or _utc_iso()),
            updated_at=str(data.get("updated_at") or _utc_iso()),
            stop_reason=str(data.get("stop_reason") or ""),
            error_detail=str(data.get("error_detail") or ""),
            turns=turns,
        )


ProgressCallback = Callable[[AutopilotRunState, str], None]


class AutopilotOrchestrator:
    """Durumsuz `max` ile Sandbox'lu `AgentSessionClient` arasındaki proje döngüsü."""

    def __init__(
        self,
        *,
        config: AutopilotConfig,
        agent: AgentSessionClient,
        max_bridge: MaxBridge,
        alerter: TelegramAlerter | None = None,
        on_progress: ProgressCallback | None = None,
        on_agent_delta: DeltaCallback | None = None,
        resume_state: AutopilotRunState | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.agent = agent
        self.max_bridge = max_bridge
        self.alerter = alerter
        self.on_progress = on_progress
        self.on_agent_delta = on_agent_delta
        self._stop_requested = False

        if resume_state is not None:
            self.state = resume_state
            self.state.max_turns = config.max_turns
            if not self.state.target_domain:
                self.state.target_domain = agent.config.target_domain
        else:
            self.state = AutopilotRunState(
                run_id=config.run_id,
                chat_id=agent.config.chat_id,
                project_idea=config.project_idea.strip(),
                target_domain=agent.config.target_domain,
                max_turns=config.max_turns,
            )

    def request_stop(self) -> None:
        """Döngüyü mevcut adım tamamlandığında güvenli biçimde durdurur."""
        self._stop_requested = True

    def _verify_separate_accounts(self) -> None:
        agent_sub = self.agent.account_identity()
        max_subs = self.max_bridge.account_identities()
        if agent_sub and max_subs and agent_sub in max_subs:
            raise SameAccountError(
                "Agent cURL'ü ile Max cURL'ü AYNI Google hesabına (aynı oturum kimliğine) "
                "ait! Aynı hesabı iki modda birden kullanmak oturumları düşürür. "
                "Lütfen Agent Mode ve Max için iki ayrı Google hesabı kullanın."
            )

    def _persist_state(self) -> None:
        self.state.updated_at = _utc_iso()
        if self.config.state_file is None:
            return
        target = self.config.state_file
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)

    def _emit_progress(self, note: str) -> None:
        self._persist_state()
        if self.on_progress is not None:
            self.on_progress(self.state, note)

    async def _notify_telegram(self, text: str) -> None:
        if self.alerter is not None and self.alerter.enabled:
            await self.alerter.send(text, label="autopilot")

    async def _sleep_gap(self) -> None:
        if self._stop_requested:
            return
        hi = self.config.gap_max_seconds
        lo = self.config.gap_min_seconds
        if hi <= 0:
            return
        delay = lo if hi <= lo else random.uniform(lo, hi)
        if delay > 0:
            await asyncio.sleep(delay)

    def _build_digests(self) -> list[TurnDigest]:
        out: list[TurnDigest] = []
        for rec in self.state.turns:
            tools_brief = (
                f"{rec.tool_count} araç ({rec.bash_count} bash, {rec.error_tool_count} hata)"
                if rec.tool_count
                else "0 araç (yalnız metin)"
            )
            out.append(
                TurnDigest(
                    turn=rec.turn,
                    phase=rec.phase,
                    max_summary=_summarize_one_line(rec.sent_to_agent, 120),
                    agent_tools_summary=tools_brief,
                )
            )
        return out

    def _project_header_lines(self) -> list[str]:
        lines = [
            f"Run: {self.state.run_id}",
            f"Proje: {_summarize_one_line(self.state.project_idea, 100)}",
        ]
        if self.state.agent_url:
            lines.append(f"Agent: {self.state.agent_url}")
        else:
            lines.append(f"Agent Sohbet: {self.state.chat_id}")
        return lines

    @staticmethod
    def _is_non_retryable_max_error(exc: Exception) -> bool:
        lowered = str(exc).lower()
        return any(
            marker in lowered
            for marker in (
                "401",
                "403",
                "409",
                "429",
                "auth",
                "quota",
                "limit",
                "recaptcha",
                "captcha",
                "refresh_required",
            )
        )

    async def _complete_max_safe(self, *, system_prompt: str, user_prompt: str) -> str:
        """Geçici ağ/akış kesintilerinde `max` çağrısını en fazla 1 kez tekrar dener."""
        try:
            return await self.max_bridge.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except MaxBridgeError as exc:
            if self._stop_requested or self._is_non_retryable_max_error(exc):
                raise
            logger.warning("autopilot_max_transient_retry", run_id=self.state.run_id)
            await asyncio.sleep(5.0)
            return await self.max_bridge.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )

    async def _ask_max_for_kickoff(self) -> MaxDecision:
        user_prompt = build_kickoff_user_prompt(
            project_idea=self.state.project_idea,
            max_turns=self.config.max_turns,
            min_turns=self.config.effective_min_turns,
        )
        raw = await self._complete_max_safe(
            system_prompt=MAX_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        decision = parse_max_decision(raw, fallback_memory=self.state.memory_summary)
        if decision.status == "TAMAMLANDI" or not decision.message_to_agent.strip():
            # Başlangıçta henüz hiçbir tur çalışmadan TAMAMLANDI dönülemez
            decision = MaxDecision(
                status="DEVAM",
                phase="1_VIZYON_VE_CEKIRDEK",
                evaluation=decision.evaluation or "Başlangıç vizyon ve çekirdek adımı.",
                updated_memory=decision.updated_memory or self.state.memory_summary,
                message_to_agent=(
                    decision.message_to_agent.strip()
                    or (
                        f"Bu projeyi ('{self.state.project_idea}') sıradan bir şablon "
                        "olmaktan çıkarıp özgün ve derinlikli bir ürün olarak kuralım. "
                        "Önce çalışma dizinini incele, ardından gerçek veri katmanını, "
                        "karakterli arayüz iskeletini, gerçekçi Türkçe başlangıç "
                        "verilerini ve çalışan çekirdek akışı kurup doğrula."
                    )
                ),
                raw_response=decision.raw_response,
            )
        return decision

    async def _ask_max_for_next_step(
        self,
        *,
        completed_turns: int,
        last_sent: str,
        agent_reply: AgentMessage,
        sandbox_evidence: str,
        anti_loop_warning: str = "",
        early_exit_rejection_note: str = "",
    ) -> MaxDecision:
        has_blocker = any(t.is_interactive_blocker for t in agent_reply.tools) or (
            "ETKİLEŞİM ENGELİ" in sandbox_evidence
        )
        next_turn_label = min(completed_turns + 1, self.config.max_turns)
        user_prompt = build_turn_user_prompt(
            project_idea=self.state.project_idea,
            turn=next_turn_label,
            max_turns=self.config.max_turns,
            current_phase=self.state.current_phase,
            memory_summary=self.state.memory_summary,
            turn_digests=self._build_digests(),
            last_sent_to_agent=last_sent,
            agent_reply_text=agent_reply.text,
            sandbox_evidence=sandbox_evidence,
            anti_loop_warning=anti_loop_warning,
            completed_turns=completed_turns,
            min_turns=self.config.effective_min_turns,
            has_interactive_blocker=has_blocker,
            early_exit_rejection_note=early_exit_rejection_note,
        )
        raw = await self._complete_max_safe(
            system_prompt=MAX_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        return parse_max_decision(raw, fallback_memory=self.state.memory_summary)

    async def _guard_premature_completion(
        self,
        decision: MaxDecision,
        *,
        completed_turns: int,
        last_sent: str,
        agent_reply: AgentMessage,
        sandbox_evidence: str,
        anti_loop_warning: str = "",
    ) -> MaxDecision:
        """Olgunluk kapısından önce gelen erken `TAMAMLANDI` kararlarını durdurur."""
        min_turns = self.config.effective_min_turns
        if (
            decision.status != "TAMAMLANDI"
            or completed_turns >= min_turns
            or completed_turns >= self.config.max_turns
        ):
            return decision

        logger.info(
            "autopilot_premature_completion_intercepted",
            run_id=self.state.run_id,
            completed_turns=completed_turns,
            min_turns=min_turns,
        )
        if decision.updated_memory.strip():
            self.state.memory_summary = decision.updated_memory.strip()

        self._emit_progress(
            f"Tur {completed_turns}: Erken bitirme engellendi "
            f"({completed_turns}/{min_turns} olgunluk kapısı); derinleştirme adımı alınıyor..."
        )
        rejection_note = (
            f"Az önce {completed_turns}. tur sonunda erken <DURUM>TAMAMLANDI</DURUM> verdin, "
            f"ancak bu koşuda asgari ürün olgunluk kapısı {min_turns} turdur! "
            "İlk çalışır iskelette projeyi bitirip kaçmak kesinlikle yasaktır. "
            "Mevcut uygulamayı incele ve tekrara düşmeden ürünü sıradanlıktan çıkaracak "
            "YARATICI İMZA ÖZELLİK (2_YARATICI_DERINLIK), UX/uç durum/etkileşim cilası "
            "(3_DENEYIM_VE_CILA) veya gerçek uçtan uca test + 0.0.0.0 canlı sunucu "
            "doğrulaması (4_DOGRULAMA_VE_SUNUCU) adımlarından sıradakini "
            "<DURUM>DEVAM</DURUM> ve dolu bir <AJANA_MESAJ> ile üret."
        )
        retried = await self._ask_max_for_next_step(
            completed_turns=completed_turns,
            last_sent=last_sent,
            agent_reply=agent_reply,
            sandbox_evidence=sandbox_evidence,
            anti_loop_warning=anti_loop_warning,
            early_exit_rejection_note=rejection_note,
        )
        if retried.status == "DEVAM" and retried.message_to_agent.strip():
            return retried

        # Çok nadir durumda ikinci denemede bile erken bitirmeye diretirse güvenli derinleştirme:
        return MaxDecision(
            status="DEVAM",
            phase="2_YARATICI_DERINLIK",
            evaluation=(
                retried.evaluation
                or f"Olgunluk kapısı ({completed_turns}/{min_turns}) nedeniyle derinleştirme turu."
            ),
            updated_memory=retried.updated_memory or self.state.memory_summary,
            message_to_agent=build_early_exit_fallback_instruction(
                project_idea=self.state.project_idea,
                completed_turns=completed_turns,
                max_turns=self.config.max_turns,
            ),
            raw_response=retried.raw_response,
        )

    async def _finish_stopped(self, reason: str) -> AutopilotRunState:
        self.state.status = "stopped"
        self.state.stop_reason = reason
        self._emit_progress(reason)
        await self._notify_telegram(
            "\n".join(
                [
                    "🛑 ApiWrapper Autopilot durduruldu",
                    *self._project_header_lines(),
                    f"Tamamlanan Tur: {len(self.state.turns)}/{self.config.max_turns}",
                ]
            )
        )
        return self.state

    async def run(self) -> AutopilotRunState:
        """Otopilot döngüsünü baştan (veya `resume_state`'ten) sonuna dek yürütür."""
        self._verify_separate_accounts()
        await self.agent.startup()
        await self.max_bridge.startup()

        try:
            self.state.status = "running"
            self._emit_progress("Agent oturumuna bağlanılıyor...")
            await self.agent.connect(wait_if_busy=True)
            self._emit_progress(f"Agent sohbetine bağlandı (chat_id={self.state.chat_id}).")

            await self._notify_telegram(
                "\n".join(
                    [
                        "🚀 ApiWrapper Autopilot başladı",
                        *self._project_header_lines(),
                        f"Azami Tur: {self.config.max_turns}",
                    ]
                )
            )

            # Devam (resume) modunda önceki turun son ajan cevabından karar üreterek başla;
            # sıfırdan başlıyorsak 1. tur kickoff talimatını üret.
            next_decision: MaxDecision | None = None
            start_turn = len(self.state.turns) + 1

            if start_turn > self.config.max_turns:
                self.state.status = "max_turns_reached"
                self.state.stop_reason = "Mevcut tur sayısı azami sınıra zaten ulaşmış."
                self._emit_progress(self.state.stop_reason)
                return self.state

            if start_turn == 1:
                self._emit_progress("Tur 1: Max başlangıç mimarisini ve ilk talimatı hazırlıyor...")
                next_decision = await self._ask_max_for_kickoff()
            else:
                prev = self.state.turns[-1]
                synthetic_msg = AgentMessage(
                    id=prev.agent_message_id,
                    role="assistant",
                    text=prev.agent_reply_text,
                )
                done_so_far = len(self.state.turns)
                self._emit_progress(f"Tur {start_turn}: Kaldığı yerden Max kararı alınıyor...")
                next_decision = await self._ask_max_for_next_step(
                    completed_turns=done_so_far,
                    last_sent=prev.sent_to_agent,
                    agent_reply=synthetic_msg,
                    sandbox_evidence=prev.sandbox_evidence,
                )
                next_decision = await self._guard_premature_completion(
                    next_decision,
                    completed_turns=done_so_far,
                    last_sent=prev.sent_to_agent,
                    agent_reply=synthetic_msg,
                    sandbox_evidence=prev.sandbox_evidence,
                )

            for turn in range(start_turn, self.config.max_turns + 1):
                if self._stop_requested:
                    return await self._finish_stopped("Kullanıcı tarafından durduruldu.")

                assert next_decision is not None
                self.state.current_turn = turn
                self.state.current_phase = next_decision.phase
                if next_decision.updated_memory.strip():
                    self.state.memory_summary = next_decision.updated_memory.strip()

                if next_decision.status == "TAMAMLANDI":
                    self.state.status = "completed"
                    self.state.stop_reason = (
                        next_decision.evaluation or "Max projeyi kabul etti ve tamamladı."
                    )
                    self._emit_progress(
                        f"✅ Proje tamamlandı (Tur {turn - 1} sonunda kabul edildi)."
                    )
                    await self._notify_telegram(
                        "\n".join(
                            [
                                "✅ ApiWrapper Autopilot tamamlandı",
                                *self._project_header_lines(),
                                f"Toplam Tur: {len(self.state.turns)}",
                                f"Özet: {_summarize_one_line(self.state.stop_reason, 240)}",
                            ]
                        )
                    )
                    return self.state

                if next_decision.status == "TAKILDI":
                    self.state.status = "failed"
                    self.state.stop_reason = (
                        next_decision.evaluation
                        or "Max ilerleme sağlanamadığını bildirdi (TAKILDI)."
                    )
                    self._emit_progress(f"⚠️ Otopilot durdu: {self.state.stop_reason}")
                    await self._notify_telegram(
                        "\n".join(
                            [
                                "⚠️ ApiWrapper Autopilot takıldı",
                                *self._project_header_lines(),
                                f"Tur: {turn}",
                                f"Neden: {_summarize_one_line(self.state.stop_reason, 240)}",
                            ]
                        )
                    )
                    return self.state

                # Ajana gidecek mesajı hazırla (1. turda çalışma kurallarını ekle)
                outgoing = next_decision.message_to_agent.strip()
                if turn == 1 and self.config.append_guardrails_on_first_turn:
                    outgoing = outgoing + AGENT_OPERATING_GUARDRAILS

                await self._sleep_gap()
                if self._stop_requested:
                    return await self._finish_stopped("Kullanıcı tarafından durduruldu.")

                t0 = time.monotonic()
                self._emit_progress(
                    f"Tur {turn}/{self.config.max_turns} [{self.state.current_phase}]: "
                    "Talimat Agent'a gönderiliyor ve sandbox çalışması bekleniyor..."
                )
                agent_reply = await self.agent.send_and_wait(
                    outgoing,
                    on_delta=self.on_agent_delta,
                )
                elapsed = round(time.monotonic() - t0, 2)
                evidence = format_sandbox_evidence(agent_reply)

                bash_count = sum(1 for t in agent_reply.tools if t.tool_name == "bash")
                err_count = sum(1 for t in agent_reply.tools if t.is_error)

                record = AutopilotTurnRecord(
                    turn=turn,
                    phase=self.state.current_phase,
                    max_status=next_decision.status,
                    max_evaluation=next_decision.evaluation,
                    sent_to_agent=next_decision.message_to_agent.strip(),
                    agent_message_id=agent_reply.id,
                    agent_reply_text=agent_reply.text,
                    sandbox_evidence=evidence,
                    tool_count=len(agent_reply.tools),
                    bash_count=bash_count,
                    error_tool_count=err_count,
                    duration_seconds=elapsed,
                    completed_at=_utc_iso(),
                )
                self.state.turns.append(record)
                self._emit_progress(
                    f"Tur {turn} Agent yanıtı alındı ({elapsed:.1f} sn, "
                    f"{len(agent_reply.tools)} araç çağrısı)."
                )

                if self._stop_requested:
                    return await self._finish_stopped(
                        "Kullanıcı tarafından tur sonunda durduruldu."
                    )

                # Son tura ulaştıysak son kez Max'tan kabul/değerlendirme kararı alalım:
                await self._sleep_gap()
                anti_loop_note = ""
                if len(self.state.turns) >= 2:
                    prev_msg = self.state.turns[-2].sent_to_agent
                    curr_msg = self.state.turns[-1].sent_to_agent
                    if similarity_ratio(prev_msg, curr_msg) >= 0.80:
                        anti_loop_note = (
                            "Önceki iki turda ajana neredeyse aynı talimatı verdin! "
                            "Aynı cümleyi tekrarlama: ya somut farklı bir teknik yön ver "
                            "ya da iş tamamlandıysa <DURUM>TAMAMLANDI</DURUM> ile bitir."
                        )

                done_so_far = len(self.state.turns)
                self._emit_progress(
                    f"Tur {turn} değerlendirmesi: Max sandbox kanıtlarını inceliyor..."
                )
                next_decision = await self._ask_max_for_next_step(
                    completed_turns=done_so_far,
                    last_sent=record.sent_to_agent,
                    agent_reply=agent_reply,
                    sandbox_evidence=evidence,
                    anti_loop_warning=anti_loop_note,
                )
                next_decision = await self._guard_premature_completion(
                    next_decision,
                    completed_turns=done_so_far,
                    last_sent=record.sent_to_agent,
                    agent_reply=agent_reply,
                    sandbox_evidence=evidence,
                    anti_loop_warning=anti_loop_note,
                )
                if next_decision.updated_memory.strip():
                    self.state.memory_summary = next_decision.updated_memory.strip()
                self.state.current_phase = next_decision.phase

                if next_decision.status == "TAMAMLANDI":
                    self.state.status = "completed"
                    self.state.stop_reason = (
                        next_decision.evaluation or "Max projeyi kabul etti ve tamamladı."
                    )
                    self._emit_progress(f"✅ Proje Tur {turn} sonunda tamamlandı ve kabul edildi!")
                    await self._notify_telegram(
                        "\n".join(
                            [
                                "✅ ApiWrapper Autopilot tamamlandı",
                                *self._project_header_lines(),
                                f"Toplam Tur: {turn}",
                                f"Özet: {_summarize_one_line(self.state.stop_reason, 240)}",
                            ]
                        )
                    )
                    return self.state

                if next_decision.status == "TAKILDI":
                    self.state.status = "failed"
                    self.state.stop_reason = (
                        next_decision.evaluation or "Max ilerleme sağlanamadığını bildirdi."
                    )
                    self._emit_progress(f"⚠️ Otopilot durdu: {self.state.stop_reason}")
                    await self._notify_telegram(
                        "\n".join(
                            [
                                "⚠️ ApiWrapper Autopilot takıldı",
                                *self._project_header_lines(),
                                f"Tur: {turn}",
                                f"Neden: {_summarize_one_line(self.state.stop_reason, 240)}",
                            ]
                        )
                    )
                    return self.state

            # Döngü max_turns sınırına ulaştı
            self.state.status = "max_turns_reached"
            self.state.stop_reason = (
                next_decision.evaluation
                if next_decision and next_decision.evaluation
                else f"Azami tur sınırına ({self.config.max_turns}) ulaşıldı."
            )
            self._emit_progress(f"⏹️ Azami tur sınırına ({self.config.max_turns}) ulaşıldı.")
            await self._notify_telegram(
                "\n".join(
                    [
                        f"⏹️ ApiWrapper Autopilot tur sınırına ulaştı ({self.config.max_turns} tur)",
                        *self._project_header_lines(),
                        f"Son değerlendirme: {_summarize_one_line(self.state.stop_reason, 240)}",
                    ]
                )
            )
            return self.state

        except Exception as exc:
            self.state.status = "failed"
            self.state.error_detail = f"{type(exc).__name__}: {exc}"
            self._persist_state()
            logger.error(
                "autopilot_run_failed",
                run_id=self.state.run_id,
                error_type=type(exc).__name__,
            )
            await self._notify_telegram(
                "\n".join(
                    [
                        "🔴 ApiWrapper Autopilot hata ile durdu",
                        *self._project_header_lines(),
                        f"Tur: {self.state.current_turn}",
                        f"Hata türü: {type(exc).__name__}",
                    ]
                )
            )
            raise
        finally:
            await self.max_bridge.shutdown()
            await self.agent.shutdown()
