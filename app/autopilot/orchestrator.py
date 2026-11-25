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
import contextlib
import json
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.autopilot.agent_client import AgentSessionClient, DeltaCallback
from app.autopilot.events_parser import AgentMessage, format_sandbox_evidence
from app.autopilot.max_bridge import MaxBridge, MaxBridgeError
from app.autopilot.prompts import (
    AGENT_FOLLOWUP_NO_ASK_GUARDRAIL,
    AGENT_MORNING_HANDOFF_PROMPT,
    AGENT_OPERATING_GUARDRAILS,
    MAX_SYSTEM_PROMPT,
    TURN_BUDGET_MINUTES,
    TURN_BUDGET_SOFT_MINUTES,
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
from app.upstream.client import error_excerpt
from app.upstream.exceptions import (
    UpstreamException,
    UpstreamNetworkError,
    UpstreamProtocolError,
    UpstreamTimeout,
)
from app.utils.ids import random_suffix

logger = get_logger(__name__)

#: Türkiye kalıcı saat dilimi (UTC+3; slim konteynerlerde tzdata paketi gerektirmez).
TR_TZ = timezone(timedelta(hours=3), name="TRT")


def is_in_night_window(
    now: datetime,
    *,
    start_hour: int = 22,
    end_hour: int = 7,
) -> bool:
    """Verilen zamanın Türkiye saatine göre çalışma penceresinde olup olmadığını döner."""
    local = now.astimezone(TR_TZ)
    if start_hour == end_hour:
        return True
    if start_hour > end_hour:
        # Gece yarısını aşan pencere: ör. 22:00 -> 07:00
        return local.hour >= start_hour or local.hour < end_hour
    return start_hour <= local.hour < end_hour


def seconds_until_window_opens(
    now: datetime,
    *,
    start_hour: int = 22,
    end_hour: int = 7,
) -> float:
    """Pencere kapalıysa bir sonraki açılışa (`start_hour:00:00` TR) kalan saniye."""
    if is_in_night_window(now, start_hour=start_hour, end_hour=end_hour):
        return 0.0
    local = now.astimezone(TR_TZ)
    target = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return max(0.0, (target - local).total_seconds())


def seconds_until_morning_hour(
    now: datetime,
    *,
    night_start_hour: int = 22,
    morning_hour: int = 7,
) -> float:
    """Gece penceresi içindeysek sabah `morning_hour:00` TR'ye kalan saniye; gündüzse 0."""
    if not is_in_night_window(now, start_hour=night_start_hour, end_hour=morning_hour):
        return 0.0
    local = now.astimezone(TR_TZ)
    target = local.replace(hour=morning_hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return max(0.0, (target - local).total_seconds())


def split_notification_chunks(
    text: str,
    *,
    limit: int = 3500,
    max_chars: int = 10500,
) -> list[str]:
    """Telegram'ın 4096 karakter sınırına takılmaması için uzun raporları böler."""
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 24].rstrip() + "\n\n… (rapor kısaltıldı)"
    chunks: list[str] = []
    rem = cleaned
    while len(rem) > limit:
        cut = rem.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        chunks.append(rem[:cut].rstrip())
        rem = rem[cut:].lstrip()
    if rem:
        chunks.append(rem)
    return chunks


class SameAccountError(ValueError):
    """Agent cURL'ü ile `max` cURL'ü aynı Google hesabına ait olduğunda fırlatılır."""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summarize_one_line(text: str, limit: int = 160) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


#: Tür sürekliliği ayarlarının **tek kaynağı**. Aynı sayılar daha önce
#: orkestratörde, HTTP isteğinde, CLI'da ve Telegram'da ayrı ayrı duruyordu;
#: biri değişince diğerleri sessizce eski değerde kalıyordu.
#:
#: Zamanlama felsefesi: `max` çağrıları arasındaki **nazik aralık tele giden
#: yolda** (`RequestQueue`, `REQUEST_GAP_*`) uygulanır. Orkestratörün kendi tur
#: aralığı bu yüzden küçüktür; ikisi birlikte eskiden tur başına ~45 sn saf
#: bekleme üretiyordu (ölçüm: docs/DENGE_VE_3_HESAP_ANALIZ.md §2.2).
TURN_GAP_MIN_SECONDS = 0.0
TURN_GAP_MAX_SECONDS = 3.0
#: `max` 429 / geçici yoğunluk verdiğinde kademeli dinlenme (sn). İlk basamaklar
#: kısa tutulur: 5 dakika beklemek yerine 1 dakikada yoklamak daha çok tur kazandırır;
#: son basamak yine sabırlıdır.
MAX_COOLDOWN_SCHEDULE: tuple[float, ...] = (60.0, 180.0, 420.0, 900.0)
#: `TAKILDI` kararı koşuyu hemen öldürmez: en fazla bu kadar "kurtarma turu"
#: verilir, sonra koşu `stopped` durumuna alınır (yeniden başlatılabilir kalır).
MAX_STUCK_RECOVERIES = 1
#: Bir turda oluşan geçici hatada (ağ/olay akışı) koşuyu öldürmeden önce
#: yapılacak yeniden deneme sayısı.
MAX_TRANSIENT_TURN_RETRIES = 2
#: `max_turns` tavanına ulaşılınca koşu kapanmadan verilebilecek ek tur sayısı.
MAX_TURN_EXTENSIONS = 48
#: Tur tavanı dolunca otomatik modda eklenecek tur sayısı.
TURN_LIMIT_AUTO_EXTEND = 3
#: Tur sürerken "ne olduğu / ne kadar kaldı" bilgisini Telegram'a kaç saniyede
#: bir yazacağız. 0 = kapalı. Kullanıcı geri bildirimi: uzun turda hiçbir şey
#: görünmediği için süreç anlaşılmıyordu.
TURN_HEARTBEAT_SECONDS = 600.0

#: Agent'tan bu kadar süre **tek olay** gelmezse "hâlâ bekliyorum" notu düşer.
#: Bu bir süre **sınırı değildir**: tur kesilmez, yalnız sessizlik bildirilir
#: (kullanıcı sözleşmesi: "tur süreyle bitmez, ama sessiz de kalmaz").
SILENT_TURN_NOTICE_SECONDS = 1200.0

#: Kalp atışı aynı ilerlemeyi tekrarlamaz; ama canlılık kanıtı tamamen de
#: susmaz: değişiklik olmasa bile en geç bu sürede bir satır yayınlanır.
TURN_HEARTBEAT_MAX_GAP_SECONDS = 3600.0
#: Tur başına en çok sessizlik notu (20/40/60. dk). Son notta "bundan sonra
#: sessizce bekliyorum" denir; tur yine de kesilmez, `/durum` her an bakılabilir.
SILENT_NOTICE_MAX_PER_TURN = 3


@dataclass(slots=True)
class AutopilotConfig:
    """Tek bir otopilot koşusunun çalışma parametreleri."""

    project_idea: str
    max_turns: int = 8
    auto_turns: bool = False
    min_turns_before_complete: int | None = None
    respect_night_window: bool = False
    night_start_hour: int = 22
    night_end_hour: int = 7
    gap_min_seconds: float = TURN_GAP_MIN_SECONDS
    gap_max_seconds: float = TURN_GAP_MAX_SECONDS
    max_cooldown_schedule: tuple[float, ...] = MAX_COOLDOWN_SCHEDULE
    state_file: Path | None = None
    run_id: str = field(default_factory=lambda: f"auto_{random_suffix(12)}")
    append_guardrails_on_first_turn: bool = True
    enable_handoff_report: bool = True
    max_stuck_recoveries: int = MAX_STUCK_RECOVERIES
    max_transient_turn_retries: int = MAX_TRANSIENT_TURN_RETRIES
    #: Kaç kez tur uzatıldığı (kalıcı durumda saklanır; yeniden başlatmada korunur).
    turn_extensions: int = 0
    #: True ise aynı-hesap koruması engellemez, yalnız uyarır
    #: (`Settings.allow_same_account_agent_max` deney anahtarından gelir).
    allow_same_account_agent_max: bool = False

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
        if not (0 <= self.night_start_hour <= 23 and 0 <= self.night_end_hour <= 23):
            raise ValueError("night_start_hour / night_end_hour 0..23 aralığında olmalıdır.")
        if self.gap_min_seconds < 0 or self.gap_max_seconds < self.gap_min_seconds:
            raise ValueError("gap_min_seconds / gap_max_seconds aralığı geçersiz.")


    # ---------------------------------------------------------------- tur uzatma
    def can_extend_turns(self) -> bool:
        """Koşu, tur tavanında kapanmadan uzatılabilir mi?"""
        return self.turn_extensions < MAX_TURN_EXTENSIONS

    def extend_turns(self, extra_turns: int) -> int:
        """Tavanı uzatır ve uzatma hakkını düşer; yeni tavanı döndürür.

        `max_turns_reached` artık **koşunun ölümü değildir**: istenirse aynı
        Agent sohbetinde (aynı dosyalar, aynı sunucular) yeni turlar eklenebilir.
        """
        allowed = max(0, min(int(extra_turns), MAX_TURN_EXTENSIONS - self.turn_extensions))
        if allowed <= 0:
            # Tavan doldu: çağıran taraf "hayır"ı bilmeli ama koşu **çökmemeli**
            # (süreklilik ilkesi). Durum değişmez, mevcut tavan döner.
            logger.info(
                "autopilot_turn_extend_exhausted",
                extensions=self.turn_extensions,
                limit=MAX_TURN_EXTENSIONS,
            )
            return self.max_turns
        self.max_turns += allowed
        self.turn_extensions += allowed
        return self.max_turns


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
    # starting | waiting_window | running | completed | stopped | failed | max_turns_reached
    status: str = "starting"
    current_turn: int = 0
    max_turns: int = 8
    planned_turns: int = 0
    auto_turns: bool = False
    respect_night_window: bool = False
    current_phase: str = "1_VIZYON_VE_CEKIRDEK"
    memory_summary: str = ""
    evening_plan: str = ""
    handoff_report: str = ""
    started_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)
    stop_reason: str = ""
    error_detail: str = ""
    pending_turn: int = 0
    pending_instruction: str = ""
    pending_evaluation: str = ""
    #: Kaç kez tur uzatıldığı ve kaç kez `TAKILDI` kurtarması kullanıldığı
    #: (yeniden başlatmada da korunur; sonsuz döngüyü engeller).
    turn_extensions: int = 0
    stuck_recoveries: int = 0
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
            planned_turns=int(data.get("planned_turns") or 0),
            auto_turns=bool(data.get("auto_turns", False)),
            respect_night_window=bool(data.get("respect_night_window", False)),
            current_phase=str(data.get("current_phase") or "1_VIZYON_VE_CEKIRDEK"),
            memory_summary=str(data.get("memory_summary") or ""),
            evening_plan=str(data.get("evening_plan") or ""),
            handoff_report=str(data.get("handoff_report") or ""),
            started_at=str(data.get("started_at") or _utc_iso()),
            updated_at=str(data.get("updated_at") or _utc_iso()),
            stop_reason=str(data.get("stop_reason") or ""),
            error_detail=str(data.get("error_detail") or ""),
            pending_turn=int(data.get("pending_turn") or 0),
            pending_instruction=str(data.get("pending_instruction") or ""),
            pending_evaluation=str(data.get("pending_evaluation") or ""),
            turn_extensions=int(data.get("turn_extensions") or 0),
            stuck_recoveries=int(data.get("stuck_recoveries") or 0),
            turns=turns,
        )


#: Akşam planı mesajında gösterilecek en fazla satır/karakter. Kullanıcı geri
#: bildirimi: plan uzun çıkıyordu ve ne okunacağı anlaşılmıyordu.
PLAN_MAX_LINES = 6
PLAN_MAX_CHARS = 620


def _compact_plan(text: str) -> str:
    """Plan metnini okunur bir blok hâline getirir (en fazla 6 satır)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    out: list[str] = []
    used = 0
    for line in lines:
        if len(out) >= PLAN_MAX_LINES:
            out.append("…")
            break
        if used + len(line) > PLAN_MAX_CHARS:
            out.append(line[: max(20, PLAN_MAX_CHARS - used)] + "…")
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)


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
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        execution_semaphore: asyncio.Semaphore | None = None,
        morning_dispatcher: Callable[[str, str, float], None] | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.agent = agent
        self.max_bridge = max_bridge
        self.alerter = alerter
        self.on_progress = on_progress
        self.on_agent_delta = on_agent_delta
        #: Tur içi canlı ilerleme (heartbeat) zamanlayıcısı.
        self._last_heartbeat = 0.0
        self._turn_started_at = 0.0
        self._turn_activity = ""
        #: Agent'tan en son **herhangi bir olay** (metin/araç) geldiği an.
        self._last_agent_signal = 0.0
        self._last_silent_notice = 0.0
        #: Tur başına yayınlanan sessizlik notu sayısı (tavan: `SILENT_NOTICE_MAX_PER_TURN`).
        self._silence_notices_this_turn = 0
        #: Son kalp atışındaki ilerleme imzası (tur, biten sayısı, etkinlik).
        #: Aynı imza tekrar yayınlanmaz ("gereksiz mesaj" şikâyetinin çözümü).
        self._last_heartbeat_signature: tuple[int, int, str] | None = None
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn
        self._execution_semaphore = execution_semaphore
        self._morning_dispatcher = morning_dispatcher
        self._stop_requested = False
        self._wake_event = asyncio.Event()

        if resume_state is not None:
            self.state = resume_state
            self.state.max_turns = config.max_turns
            self.state.auto_turns = config.auto_turns
            self.state.respect_night_window = config.respect_night_window
            if not self.state.target_domain:
                self.state.target_domain = agent.config.target_domain
        else:
            self.state = AutopilotRunState(
                run_id=config.run_id,
                chat_id=agent.config.chat_id,
                project_idea=config.project_idea.strip(),
                target_domain=agent.config.target_domain,
                max_turns=config.max_turns,
                auto_turns=config.auto_turns,
                respect_night_window=config.respect_night_window,
            )

    def request_stop(self) -> None:
        """Döngüyü mevcut adım tamamlandığında güvenli biçimde durdurur."""
        self._stop_requested = True
        self._wake_event.set()

    def bypass_night_window(self) -> None:
        """Gece penceresini bekleyen koşuyu derhal uyandırıp gündüz de çalıştırır."""
        self.config.respect_night_window = False
        self.state.respect_night_window = False
        self._wake_event.set()

    def _current_min_turns(self) -> int:
        if self.config.min_turns_before_complete is not None:
            return self.config.effective_min_turns
        if self.config.auto_turns and self.state.planned_turns > 0:
            base = compute_min_turns_before_completion(self.state.planned_turns)
            # Otomatik modda da çok erken (1.-2. turda) kaçmayı önlemek için en az 3-4 tur tabanı:
            floor = min(self.config.max_turns, max(min(4, self.config.max_turns), base))
            return max(1, floor)
        return self.config.effective_min_turns

    async def _wait_for_night_window_if_needed(self, *, stage_note: str) -> bool:
        """Gece penceresi (`22:00–07:00` TR) kapalıysa açılana dek `max`'a dokunmadan bekler.

        Dönüş: Devam edilebiliyorsa ``True``, beklerken durdurma istendiyse ``False``.
        """
        if not self.config.respect_night_window:
            return not self._stop_requested

        start_h = self.config.night_start_hour
        end_h = self.config.night_end_hour
        now = self._now_fn()
        if is_in_night_window(now, start_hour=start_h, end_hour=end_h):
            return not self._stop_requested

        win_label = f"{start_h:02d}:00–{end_h:02d}:00 TR"
        local_str = now.astimezone(TR_TZ).strftime("%H:%M")
        self.state.status = "waiting_window"
        self._emit_progress(
            f"🌙 Gece penceresi ({win_label}) bekleniyor (şu an {local_str} TR · {stage_note}) "
            "— Max'a istek gönderilmiyor."
        )
        await self._notify_telegram(
            "\n".join(
                [
                    f"🌙 ApiWrapper Autopilot gece sırasına alındı ({win_label})",
                    *self._project_header_lines(),
                    f"Durum: Şu an saat {local_str} TR; Max aktif kullanıldığı için "
                    f"saat {start_h:02d}:00'de otomatik başlayacak/devam edecek.",
                    f"Hemen başlatmak isterseniz: /simdi {self.state.run_id}",
                ]
            )
        )

        while (
            not self._stop_requested
            and self.config.respect_night_window
            and not is_in_night_window(
                self._now_fn(), start_hour=start_h, end_hour=end_h
            )
        ):
            rem = seconds_until_window_opens(
                self._now_fn(), start_hour=start_h, end_hour=end_h
            )
            step = min(30.0, max(1.0, rem))
            if self._sleep_fn is not None:
                await self._sleep_fn(step)
            else:
                self._wake_event.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=step)

        if self._stop_requested:
            return False

        self.state.status = "running"
        self._emit_progress("⚡ Çalışma penceresi aktif; otopilot yürütülüyor...")
        return True

    def _verify_separate_accounts(self) -> None:
        agent_sub = self.agent.account_identity()
        max_subs = self.max_bridge.account_identities()
        if agent_sub and max_subs and agent_sub in max_subs:
            if self.config.allow_same_account_agent_max:
                logger.warning(
                    "autopilot_same_account_guard_disabled",
                    run_id=self.state.run_id,
                    detail=(
                        "Agent ve Max aynı Google hesabını kullanıyor; "
                        "koruma deney anahtarıyla (ALLOW_SAME_ACCOUNT_AGENT_MAX) kapalı."
                    ),
                )
                return
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
            for chunk in split_notification_chunks(text):
                await self.alerter.send(chunk, label="autopilot")

    async def _collect_and_dispatch_morning_report(self, *, headline: str) -> None:
        """Geliştirme bitince Agent'tan teslim raporunu alır ve (geceyse 07:00'de) iletir."""
        should_ask_agent = (
            self.config.enable_handoff_report
            and len(self.state.turns) >= 1
            and not self._stop_requested
            and (self.state.status == "completed" or self.config.respect_night_window)
        )
        if should_ask_agent and not self.state.handoff_report.strip():
            try:
                await self._sleep_gap()
                self._emit_progress("Sabah teslim özeti Agent'tan alınıyor...")
                summary_reply = await self.agent.send_and_wait(
                    AGENT_MORNING_HANDOFF_PROMPT,
                    on_delta=self.on_agent_delta,
                )
                if summary_reply.text.strip():
                    self.state.handoff_report = summary_reply.text.strip()
                    self._persist_state()
            except Exception as exc:
                logger.warning(
                    "autopilot_handoff_report_failed",
                    run_id=self.state.run_id,
                    error_type=type(exc).__name__,
                )

        body_section = (
            self.state.handoff_report.strip()
            or self.state.memory_summary.strip()
            or _summarize_one_line(self.state.stop_reason, 400)
        )
        report_text = "\n".join(
            [
                headline,
                *self._project_header_lines(),
                f"Tamamlanan Tur: {len(self.state.turns)}",
                "",
                "📋 Agent Teslim Özeti (Neler Yapıldı & Sonraki Adımlar):",
                body_section,
            ]
        )

        delay = (
            seconds_until_morning_hour(
                self._now_fn(),
                night_start_hour=self.config.night_start_hour,
                morning_hour=self.config.night_end_hour,
            )
            if self.config.respect_night_window
            else 0.0
        )
        if (
            delay > 0
            and self._morning_dispatcher is not None
            and self.alerter is not None
            and self.alerter.enabled
        ):
            self._emit_progress(
                f"✅ Proje tamamlandı; sabah teslim raporu hazırlandı "
                f"(saat {self.config.night_end_hour:02d}:00 TR'de Telegram'a iletilecek)."
            )
            self._morning_dispatcher(self.state.run_id, report_text, delay)
        else:
            await self._notify_telegram(report_text)

    async def _sleep_interruptible(self, total_seconds: float) -> bool:
        """`/durdur` geldiğinde kesilebilen bekleme (`True`=bitti, `False`=durduruldu)."""
        remaining = max(0.0, float(total_seconds))
        while remaining > 0:
            if self._stop_requested:
                return False
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step
        return not self._stop_requested

    @staticmethod
    def _is_transient_turn_error(exc: BaseException) -> bool:
        """Bu hata "tekrar denemeye değer" mi?

        Yalnız ağ/zaman aşımı/protokol hataları geçicidir. Kota (`429`/kilit),
        kimlik reddi ve `recaptcha` **geçici değildir**: yeniden denemek kilidi
        uzatır, o yüzden tur yeniden denenmez; koşu duraklatılıp `/devam`a
        bırakılır.
        """
        return isinstance(
            exc, (UpstreamTimeout, UpstreamNetworkError, UpstreamProtocolError)
        )

    def _turn_start_line(self, turn: int, outgoing: str) -> str:
        """Tur başlarken gönderilen **tek bakışta anlaşılır** satır bloğu."""
        done = len(self.state.turns)
        planned = self.state.planned_turns or self.config.max_turns
        next_step = self._next_step_line() or _summarize_one_line(outgoing, 160)
        return "\n".join(
            [
                f"▶️ Tur {turn} başladı ({done}/{planned} bitti · tavan {self.config.max_turns})",
                f"🎯 Bu turun işi: {_summarize_one_line(next_step, 180)}",
                f"⏱️ Tipik ölçek: {TURN_BUDGET_SOFT_MINUTES}-{TURN_BUDGET_MINUTES} dk "
                "(tur süreyle kesilmez) · ilerleme oldukça durum güncellemesi "
                "(aynısı tekrarlanmaz)",
            ]
        )

    def _note_agent_activity(self, delta: Any) -> None:
        """Akıştan gelen olayla 'şu an ne yapılıyor' satırını tazeler."""
        self._last_agent_signal = time.monotonic()
        kind = getattr(delta, "kind", "")
        if kind == "tool_input" or kind == "tool_output":
            tool = getattr(delta, "tool_name", "") or "araç"
            self._turn_activity = f"{tool} çalıştırıyor"
        elif kind == "text":
            self._turn_activity = "açıklama yazıyor"

    def _turn_status_line(self) -> str:
        """Tur sürerken: geçen süre + ne yapıldığı.

        Kullanıcı itirazı: "65 dk kaldı" bir **sınır** gibi okunuyordu. Değil:
        `AgentSessionClient.turn_timeout` varsayılanı `None`, yani tur süreyle
        kesilmez. Bu yüzden burada "kaldı" yerine **görece ölçek** yazıyoruz ve
        süre aşımı bir uyarı değil, yalnız bilgi oluyor.
        """
        if not self._turn_started_at:
            return ""
        elapsed_min = (time.monotonic() - self._turn_started_at) / 60.0
        over = (
            f" · ölçeğin ({TURN_BUDGET_MINUTES} dk) üzerinde, tur uzuyor — "
            "sistem bekler, kesmez"
            if elapsed_min > TURN_BUDGET_MINUTES
            else ""
        )
        done = len(self.state.turns)
        planned = self.state.planned_turns or self.config.max_turns
        next_step = self._next_step_line()
        tail = f" · Sıradaki: {_summarize_one_line(next_step, 90)}" if next_step else ""
        return (
            f"⏳ Tur {self.state.current_turn}: {elapsed_min:.0f} dk geçti"
            f" ({done}/{planned} bitti · tavan {self.config.max_turns}) · "
            f"{self._turn_activity or 'çalışıyor'}{tail}{over}"
        )

    async def _heartbeat_tick(self) -> bool:
        """Bir kalp atışı vakti geldiyse satırı yayınlar; yayınladıysa ``True``.

        Neden ayrı: tur bekleyişi tek bir `await` içinde olduğu için "60 dakikadır
        ses yok" durumu fark edilmiyordu. Hem hatırlatmayı hem **sessizlik
        nöbetçisini** bu tek noktadan yayınlıyoruz.

        Gereksiz tekrar yok: kalp atışı, ilerleme imzası (tur, biten sayısı,
        etkinlik) değişmedikçe atlanır; sessizlik notu tur başına en çok
        `SILENT_NOTICE_MAX_PER_TURN` kez çıkar. Tur hiçbir koşulda kesilmez.
        """
        if not self._turn_started_at:
            return False
        now = time.monotonic()
        silent = now - self._last_agent_signal
        line = self._turn_status_line()
        if not line:
            return False
        # Sıra önemli: kalp atışı (10 dk) sessizlikten (20 dk) **önce** tetiklenir;
        # önce kalp atışına bakılsaydı sessizlik notu hiçbir zaman yayınlanmazdı.
        if (
            SILENT_TURN_NOTICE_SECONDS > 0
            and silent >= SILENT_TURN_NOTICE_SECONDS
            and now - self._last_silent_notice >= SILENT_TURN_NOTICE_SECONDS
            and self._silence_notices_this_turn < SILENT_NOTICE_MAX_PER_TURN
        ):
            self._last_heartbeat = now
            self._last_silent_notice = now
            self._silence_notices_this_turn += 1
            quiet_after = (
                self._silence_notices_this_turn >= SILENT_NOTICE_MAX_PER_TURN
            )
            text = (
                f"😴 Tur {self.state.current_turn}: {silent / 60:.0f} dk'dır Agent'tan "
                "yeni olay gelmedi (hâlâ çalışıyor olabilir).\n"
                "Sistem bekliyor, turu **kesmiyor**. İstersen `/durdur` ile "
                "durdurup `/devam` ile kaldığı yerden sürdürebilirsin."
            )
            if quiet_after:
                text += (
                    "\nBu turda başka sessizlik notu yazmayacağım; "
                    "`/durum` ile her an bakabilirsin."
                )
            self._emit_progress(line)
            await self._notify_telegram(text)
            return True
        if (
            TURN_HEARTBEAT_SECONDS > 0
            and now - self._last_heartbeat >= TURN_HEARTBEAT_SECONDS
        ):
            signature = (
                self.state.current_turn,
                len(self.state.turns),
                self._turn_activity,
            )
            forced = now - self._last_heartbeat >= TURN_HEARTBEAT_MAX_GAP_SECONDS
            if signature != self._last_heartbeat_signature or forced:
                self._last_heartbeat = now
                self._last_heartbeat_signature = signature
                self._emit_progress(line)
                await self._notify_telegram(line)
                return True
            # Aynı ilerleme: atla. `_last_heartbeat` korunur; böylece zorunlu
            # aralık (`TURN_HEARTBEAT_MAX_GAP_SECONDS`) dolunca yine yayınlanır.
            return False
        return False

    async def _maybe_heartbeat(self) -> None:
        """Ana döngüden çağrılan kalp atışı (bekleyiş yoksa da çalışır)."""
        await self._heartbeat_tick()

    async def _send_turn_with_retries(
        self, outgoing: str, *, turn: int | None = None
    ) -> AgentMessage | None:
        """Ajan turunu geçici hatada yeniden dener; olmazsa koşuyu duraklatır.

        Dönen `None`, koşunun `stopped` durumuna alındığını bildirir (yeniden
        başlatılabilir; tur geçmişi ve hafıza korunur). Kota/kimlik hatalarında
        hiç denenmez, doğrudan duraklatılır.
        """
        attempts = max(0, self.config.max_transient_turn_retries)
        last_exc: BaseException | None = None
        for attempt in range(attempts + 1):
            def _delta(delta: Any) -> None:
                self._note_agent_activity(delta)
                if self.on_agent_delta is not None:
                    self.on_agent_delta(delta)
                # Heartbeat uyandırıcıyı ana döngüde bırakmak yerine buradan
                # tetiklemek yeterli: tur bekleyişi tek bir await içinde.
                now = time.monotonic()
                due = TURN_HEARTBEAT_SECONDS > 0 and (
                    now - self._last_heartbeat >= TURN_HEARTBEAT_SECONDS
                )
                silent_due = (
                    SILENT_TURN_NOTICE_SECONDS > 0
                    and now - self._last_agent_signal >= SILENT_TURN_NOTICE_SECONDS
                    and now - self._last_silent_notice >= SILENT_TURN_NOTICE_SECONDS
                    and self._silence_notices_this_turn < SILENT_NOTICE_MAX_PER_TURN
                )
                if due or silent_due:
                    with contextlib.suppress(RuntimeError):
                        asyncio.get_running_loop().create_task(self._heartbeat_tick())

            try:
                return await self.agent.send_and_wait(
                    outgoing,
                    on_delta=_delta,
                )
            except UpstreamException as exc:
                last_exc = exc
                if not self._is_transient_turn_error(exc) or attempt >= attempts:
                    break
                backoff = 30.0 * (2**attempt)
                self._emit_progress(
                    f"⏳ Geçici Agent hatası ({type(exc).__name__}); "
                    f"{backoff:.0f} sn sonra yeniden denenecek "
                    f"({attempt + 1}/{attempts})."
                )
                if not await self._sleep_interruptible(backoff):
                    return None

        if last_exc is None:
            return None
        self.state.status = "stopped"
        self.state.stop_reason = (
            "Agent turu tamamlanamadı (ağ/akış hatası); taze cURL ile /devam "
            "yazınca kaldığı yerden sürer: "
            f"{_summarize_one_line(str(last_exc), 200)}"
        )
        self.state.error_detail = str(last_exc)[:1000]
        self._persist_state()
        self._emit_progress(
            "⏸️ Otopilot duraklatıldı; taze Agent cURL ile /devam yazınca "
            "kaldığı yerden sürer."
        )
        await self._notify_telegram(
            "\n".join(
                [
                    "⏸️ Otopilot duraklatıldı (geçici olmayan Agent hatası)",
                    *self._project_header_lines(),
                    f"Tur: {self.state.current_turn}",
                    f"Neden: {self.state.stop_reason}",
                    f"Kaldığı yerden sürdürmek için: /devam {self.state.run_id}",
                ]
            )
        )
        return None

    async def _sleep_gap(self) -> None:
        if self._stop_requested:
            return
        hi = self.config.gap_max_seconds
        lo = self.config.gap_min_seconds
        if hi <= 0:
            return
        delay = lo if hi <= lo else random.uniform(lo, hi)
        if delay > 0:
            await self._sleep_interruptible(delay)

    def _next_step_line(self) -> str:
        """Hafızadaki yol haritasından **sıradaki tek** maddeyi seçer.

        Amaç: `max` her turda yeni sohbet açtığı için "ne yapılacaktı?" sorusunun
        cevabını kendisi uydurup yapılmış işi tekrar istemesi. Burada tek satır
        verilir; birden fazla madde verilirse kapsam yeniden genişliyor.
        """
        for raw_line in (self.state.memory_summary or "").splitlines():
            line = raw_line.strip()
            if not line.startswith(("-", "*", "•")):
                continue
            head = line[:6].lower()
            if "[x]" in head or "✅" in head:
                continue
            if "[ ]" in head:
                body = line.lstrip("-*• ").strip()
                body = re.sub(r"^(?:\[[ xX]\]|✅)\s*", "", body).strip()
                if body:
                    return f"- {body}"
        return ""

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
    def _is_fatal_max_error(exc: Exception) -> bool:
        """Beklemekle düzelmeyecek kalıcı kimlik/sürüm/yapılandırma hatalarını ayırır.

        Not: `429` (`prompt failed` / hız sınırı / kota), `/v1/ask 'problem'` ve
        `recaptcha_rejected` üretim ölçümlerinde bekleyince kendiliğinden geçtiği
        için kalıcı ölüm sayılmaz; kademeli soğuma ile beklenir.
        """
        lowered = str(exc).lower()
        if any(
            marker in lowered
            for marker in (
                "401",
                "409",
                "400",
                "404",
                "unauthorized",
                "invalid_api_key",
                "authentication",
                "refresh_required",
                "model_not_found",
                "invalid_request",
            )
        ):
            return True
        return "403" in lowered and "recaptcha" not in lowered and "captcha" not in lowered

    async def _complete_max_safe(self, *, system_prompt: str, user_prompt: str) -> str:
        """`max` hız sınırı (`429` / `/v1/ask problem` / geçici yoğunluk) verdiğinde
        güm güm istek atıp hesabı zorlamak yerine 5+ dk kademeli dinlenip devam eder.
        """
        schedule = tuple(self.config.max_cooldown_schedule)
        attempt = 0
        while True:
            try:
                return await self.max_bridge.complete(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
            except MaxBridgeError as exc:
                if (
                    self._stop_requested
                    or self._is_fatal_max_error(exc)
                    or attempt >= len(schedule)
                ):
                    raise
                wait_sec = max(0.0, float(schedule[attempt]))
                attempt += 1
                mins, secs = divmod(int(round(wait_sec)), 60)
                human_wait = (
                    f"{mins} dk {secs} sn"
                    if mins and secs
                    else (f"{mins} dk" if mins else f"{wait_sec:.0f} sn")
                )
                logger.warning(
                    "autopilot_max_cooldown_wait",
                    run_id=self.state.run_id,
                    attempt=attempt,
                    max_retries=len(schedule),
                    wait_seconds=wait_sec,
                    reason=str(exc),
                )
                self._emit_progress(
                    f"⏳ Max geçici hız sınırı / yoğunluk bildirdi; hesabı zorlamamak için "
                    f"{human_wait} dinlenip kaldığı yerden devam edilecek "
                    f"(deneme {attempt}/{len(schedule)})..."
                )
                completed_sleep = await self._sleep_interruptible(wait_sec)
                if not completed_sleep:
                    raise MaxBridgeError("Dinlenme sırasında durdurma istendi.") from exc

    async def _ask_max_for_kickoff(
        self,
        *,
        existing_reply: AgentMessage | None = None,
    ) -> MaxDecision:
        existing_ev = (
            format_sandbox_evidence(existing_reply)
            if existing_reply is not None and existing_reply.tools
            else ""
        )
        existing_txt = (
            existing_reply.text
            if existing_reply is not None and existing_ev
            else ""
        )
        user_prompt = build_kickoff_user_prompt(
            project_idea=self.state.project_idea,
            max_turns=self.config.max_turns,
            min_turns=self._current_min_turns(),
            auto_turns=self.config.auto_turns,
            existing_agent_reply=existing_txt,
            existing_sandbox_evidence=existing_ev,
        )
        raw = await self._complete_max_safe(
            system_prompt=MAX_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        decision = parse_max_decision(raw, fallback_memory=self.state.memory_summary)
        if self.config.auto_turns and decision.planned_turns is not None:
            low_floor = min(4, self.config.max_turns)
            self.state.planned_turns = max(
                low_floor, min(self.config.max_turns, decision.planned_turns)
            )
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
                planned_turns=decision.planned_turns,
            )
        return decision

    @staticmethod
    def _is_waf_html_block(exc: Exception) -> bool:
        """Render/Cloudflare güvenlik duvarının HTML `403 <title>Blocked</title>` yanıtı mı?"""
        lowered = str(exc).lower()
        return "403" in lowered and (
            "<title>blocked</title>" in lowered or "<!doctype html" in lowered
        )

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
            chained_next_line=self._next_step_line(),
            last_sent_to_agent=last_sent,
            agent_reply_text=agent_reply.text,
            sandbox_evidence=sandbox_evidence,
            anti_loop_warning=anti_loop_warning,
            completed_turns=completed_turns,
            min_turns=self._current_min_turns(),
            planned_turns=self.state.planned_turns or None,
            has_interactive_blocker=has_blocker,
            early_exit_rejection_note=early_exit_rejection_note,
        )
        try:
            raw = await self._complete_max_safe(
                system_prompt=MAX_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        except MaxBridgeError as exc:
            if not self._is_waf_html_block(exc) or self._stop_requested:
                raise
            logger.warning("autopilot_waf_block_compact_retry", run_id=self.state.run_id)
            fallback_prompt = build_turn_user_prompt(
                project_idea=self.state.project_idea,
                turn=next_turn_label,
                max_turns=self.config.max_turns,
                current_phase=self.state.current_phase,
                memory_summary=self.state.memory_summary,
                turn_digests=self._build_digests(),
                last_sent_to_agent="",
                agent_reply_text="(Ajan turu tamamladı; yalnız sayısal sandbox karnesi iletildi.)",
                sandbox_evidence=sandbox_evidence,
                anti_loop_warning=anti_loop_warning,
                completed_turns=completed_turns,
                min_turns=self._current_min_turns(),
                planned_turns=self.state.planned_turns or None,
                has_interactive_blocker=has_blocker,
                early_exit_rejection_note=early_exit_rejection_note,
            )
            raw = await self._complete_max_safe(
                system_prompt=MAX_SYSTEM_PROMPT,
                user_prompt=fallback_prompt,
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
        min_turns = self._current_min_turns()
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

    def _auto_extend_on_turn_limit(self) -> bool:
        """Tur tavanı dolduğunda ürün bitmediyse otomatik olarak tur ekler.

        Yalnız `auto_turns` koşularında ve uzatma hakkı varken çalışır; sabit
        tavan seçen kullanıcıya sürpriz yapılmaz.
        """
        if not self.config.auto_turns or self._stop_requested:
            return False
        if not self.config.can_extend_turns():
            return False
        try:
            self.config.extend_turns(TURN_LIMIT_AUTO_EXTEND)
        except ValueError:
            return False
        self.state.turn_extensions = self.config.turn_extensions
        self._persist_state()
        return True

    async def _recover_from_stuck(
        self,
        decision: MaxDecision,
        *,
        completed_turns: int,
        last_sent: str,
        agent_reply: AgentMessage,
        sandbox_evidence: str,
        anti_loop_warning: str = "",
    ) -> MaxDecision | None:
        """`TAKILDI` kararını koşuyu öldürmeden kurtarmayı dener.

        Tek bir kötü karar 8 saatlik geceyi bitirmemeli. Önce `max`'a "sorunu
        küçült, farklı taktik" notuyla bir kez daha sorulur; kurtarma hakkı
        bittiyse koşu `failed` yerine `stopped` durumuna alınır (yeniden
        başlatılabilir kalır) ve `None` döner.
        """
        reason = decision.evaluation or "Max ilerleme sağlanamadığını bildirdi."
        if (
            self.state.stuck_recoveries < self.config.max_stuck_recoveries
            and not self._stop_requested
            and completed_turns < self.config.max_turns
        ):
            self.state.stuck_recoveries += 1
            self._persist_state()
            self._emit_progress(
                f"♻️ Tur {completed_turns}: Max 'TAKILDI' dedi; koşu kapatılmadan "
                f"kurtarma turu denenecek ({self.state.stuck_recoveries}/"
                f"{self.config.max_stuck_recoveries})."
            )
            note = (
                "Ajanın son turunda ilerleme sağlanamadı ve sen <DURUM>TAKILDI</DURUM> "
                "verdin. Koşuyu bitirmiyorum. Aynı hatayı tekrarlamak yerine sorunu "
                "KÜÇÜLT: tek bir küçük adım seç, gerekirse önce hatayı yeniden üretip "
                "kök nedeni oku, alternatif kütüphane/yaklaşım dene ve <DURUM>DEVAM</DURUM> "
                "ile net tek bir talimat ver."
            )
            try:
                retried = await self._ask_max_for_next_step(
                    completed_turns=completed_turns,
                    last_sent=last_sent,
                    agent_reply=agent_reply,
                    sandbox_evidence=sandbox_evidence,
                    anti_loop_warning=anti_loop_warning,
                    early_exit_rejection_note=note,
                )
            except MaxBridgeError as exc:
                logger.warning(
                    "autopilot_stuck_recovery_failed",
                    run_id=self.state.run_id,
                    error_type=type(exc).__name__,
                )
                retried = None
            if (
                retried is not None
                and retried.status == "DEVAM"
                and retried.message_to_agent.strip()
            ):
                return retried

        # Kurtarma hakkı bitti (ya da `max` yine takıldı): koşuyu öldürme, duraklat.
        self.state.status = "stopped"
        self.state.stop_reason = reason
        self._emit_progress(f"⏸️ Otopilot duraklatıldı: {_summarize_one_line(reason, 200)}")
        await self._notify_telegram(
            "\n".join(
                [
                    "⏸️ ApiWrapper Autopilot duraklatıldı (koşu silinmedi)",
                    *self._project_header_lines(),
                    f"Tur: {completed_turns}",
                    f"Neden: {_summarize_one_line(reason, 240)}",
                    "Devam: /devam <run_id> (aynı Agent sohbeti, aynı dosyalar)",
                ]
            )
        )
        return None

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

    async def _acquire_night_slot_or_wake(self) -> bool:
        """Gece sıralı yürütme kilidini bekler; `/durdur` veya `/simdi` gelirse anında uyanır.

        Dönüş:
        - ``True``: semafor alındı (iş bitince ``release()`` çağrılmalı).
        - ``False``: semafor alınmadı (ya `/durdur` geldi ya da `/simdi` ile gece kilidi kalktı).
        """
        sem = self._execution_semaphore
        if sem is None or not self.config.respect_night_window:
            return False
        if sem.locked():
            self.state.status = "waiting_window"
            self._emit_progress(
                "⏳ Önceki gece projesinin tamamlanması bekleniyor (sıralı yürütme)..."
            )
        acquire_task = asyncio.create_task(sem.acquire())
        try:
            while not acquire_task.done():
                if self._stop_requested or not self.config.respect_night_window:
                    acquire_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await acquire_task
                    return False
                self._wake_event.clear()
                wait_wake = asyncio.create_task(self._wake_event.wait())
                try:
                    await asyncio.wait(
                        {acquire_task, wait_wake},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not wait_wake.done():
                        wait_wake.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await wait_wake
            await acquire_task
            if self._stop_requested:
                sem.release()
                return False
            return True
        except BaseException:
            if (
                acquire_task.done()
                and not acquire_task.cancelled()
                and acquire_task.exception() is None
            ):
                sem.release()
            else:
                acquire_task.cancel()
            raise

    async def run(self) -> AutopilotRunState:
        """Otopilot döngüsünü baştan (veya `resume_state`'ten) sonuna dek yürütür."""
        self._verify_separate_accounts()

        # Gece penceresi aktifse ve saat henüz 22:00 olmadıysa hiçbir ağ bağlantısı
        # ve Max çağrısı yapmadan pencere açılışını bekle.
        if not await self._wait_for_night_window_if_needed(stage_note="başlangıç öncesi"):
            return await self._finish_stopped("Gece penceresini beklerken durduruldu.")

        acquired_slot = await self._acquire_night_slot_or_wake()
        if self._stop_requested:
            if acquired_slot and self._execution_semaphore is not None:
                self._execution_semaphore.release()
            return await self._finish_stopped("Sıra beklerken durduruldu.")

        try:
            return await self._run_inside_slot()
        except asyncio.CancelledError:
            if self.state.status not in {"completed", "max_turns_reached", "failed"}:
                self.state.status = "stopped"
                if not self.state.stop_reason:
                    self.state.stop_reason = "Kullanıcı tarafından zorla durduruldu."
                self._persist_state()
            raise
        finally:
            if acquired_slot and self._execution_semaphore is not None:
                self._execution_semaphore.release()

    async def _run_inside_slot(self) -> AutopilotRunState:
        if not await self._wait_for_night_window_if_needed(stage_note="sıra alındı"):
            return await self._finish_stopped("Sıra beklerken durduruldu.")

        try:
            await self.agent.startup()
            await self.max_bridge.startup()
            self.state.status = "running"
            self._emit_progress("Agent oturumuna bağlanılıyor...")
            connected_state = await self.agent.connect(wait_if_busy=True)
            self._emit_progress(f"Agent sohbetine bağlandı (chat_id={self.state.chat_id}).")

            # Eğer önceki koşuda Agent'a talimat gönderildikten sonra (ör. zaman aşımı
            # veya durdurma ile) bağlantı koptuysa ve Agent bulutta o turu bitirdiyse,
            # bitmiş yanıtı `turns` defterine kaydedip aynı turu tekrarlamadan ilerle:
            live_head = connected_state.last_assistant_message
            known_ids = {rec.agent_message_id for rec in self.state.turns if rec.agent_message_id}
            if (
                self.state.pending_turn > len(self.state.turns)
                and self.state.pending_instruction.strip()
                and live_head is not None
                and not live_head.pending
                and live_head.id not in known_ids
            ):
                rec_ev = format_sandbox_evidence(live_head)
                rec_bash = sum(1 for t in live_head.tools if t.tool_name == "bash")
                rec_err = sum(1 for t in live_head.tools if t.is_error)
                self.state.turns.append(
                    AutopilotTurnRecord(
                        turn=self.state.pending_turn,
                        phase=self.state.current_phase,
                        max_status="DEVAM",
                        max_evaluation=self.state.pending_evaluation,
                        sent_to_agent=self.state.pending_instruction,
                        agent_message_id=live_head.id,
                        agent_reply_text=live_head.text,
                        sandbox_evidence=rec_ev,
                        tool_count=len(live_head.tools),
                        bash_count=rec_bash,
                        error_tool_count=rec_err,
                        duration_seconds=live_head.duration_seconds or 0.0,
                        completed_at=_utc_iso(),
                    )
                )
                self.state.pending_turn = 0
                self.state.pending_instruction = ""
                self.state.pending_evaluation = ""
                self._persist_state()

            # Devam (resume) modunda önceki turun son ajan cevabından karar üreterek başla;
            # sıfırdan başlıyorsak 1. tur kickoff talimatını üret ve Akşam Planını gönder.
            next_decision: MaxDecision | None = None
            start_turn = len(self.state.turns) + 1

            if start_turn > self.config.max_turns:
                self.state.status = "max_turns_reached"
                self.state.stop_reason = "Mevcut tur sayısı azami sınıra zaten ulaşmış."
                self._emit_progress(self.state.stop_reason)
                return self.state

            if start_turn == 1:
                self._emit_progress("Tur 1: Max akşam proje planını ve ilk talimatı hazırlıyor...")
                next_decision = await self._ask_max_for_kickoff(
                    existing_reply=connected_state.last_assistant_message
                )
                self.state.evening_plan = (
                    next_decision.evening_plan.strip()
                    or next_decision.updated_memory.strip()
                    or next_decision.evaluation.strip()
                )
                self._persist_state()
                target_desc = (
                    f"~{self.state.planned_turns} tur (Otomatik · Tavan: {self.config.max_turns})"
                    if self.state.planned_turns
                    else str(self.config.max_turns)
                )
                plan_lines = [
                    "🌆 Plan hazır — geliştirme başlıyor",
                    *self._project_header_lines(),
                    f"Hedef: {target_desc} · Tipik tur "
                    f"{TURN_BUDGET_SOFT_MINUTES}-{TURN_BUDGET_MINUTES} dk (sınır değil)",
                ]
                if self.state.evening_plan:
                    # Plan metni artık 5 satıra zorlanıyor (AMAC/OZELLIK/MIMARI/
                    # ILK_TUR/TUR_SAYISI). Yine de uzun gelirse kırpılır: Telegram
                    # mesajı bir bakışta okunmalı.
                    plan_lines.extend(
                        ["", "📋 Ne yapacağız:", _compact_plan(self.state.evening_plan)]
                    )
                await self._notify_telegram("\n".join(plan_lines))
            else:
                await self._notify_telegram(
                    "\n".join(
                        [
                            "🔄 ApiWrapper Autopilot kaldığı yerden devam ediyor",
                            *self._project_header_lines(),
                            f"Başlangıç Turu: {start_turn}/{self.config.max_turns}",
                        ]
                    )
                )
                prev = self.state.turns[-1]
                live_msg = connected_state.last_assistant_message
                if live_msg is not None and live_msg.tools:
                    resume_msg = live_msg
                    resume_evidence = format_sandbox_evidence(live_msg)
                    prev.sandbox_evidence = resume_evidence
                    self._persist_state()
                else:
                    resume_msg = AgentMessage(
                        id=prev.agent_message_id,
                        role="assistant",
                        text=prev.agent_reply_text,
                    )
                    resume_evidence = prev.sandbox_evidence
                done_so_far = len(self.state.turns)
                self._emit_progress(f"Tur {start_turn}: Kaldığı yerden Max kararı alınıyor...")
                next_decision = await self._ask_max_for_next_step(
                    completed_turns=done_so_far,
                    last_sent=prev.sent_to_agent,
                    agent_reply=resume_msg,
                    sandbox_evidence=resume_evidence,
                )
                if next_decision.status == "TAMAMLANDI":
                    self.state.status = "completed"
                    self.state.stop_reason = next_decision.evaluation or "Proje kabul edildi."
                    self._emit_progress("✅ Proje kaldığı yerden tamamlandı ve kabul edildi!")
                    await self._collect_and_dispatch_morning_report(
                        headline="🌅 Sabah Teslim Raporu — Proje Tamamlandı"
                    )
                    return self.state
                if next_decision.status == "TAKILDI":
                    recovered = await self._recover_from_stuck(
                        next_decision,
                        completed_turns=done_so_far,
                        last_sent=prev.sent_to_agent,
                        agent_reply=resume_msg,
                        sandbox_evidence=resume_evidence,
                    )
                    if recovered is None:
                        return self.state
                    next_decision = recovered
                if start_turn > self.config.max_turns and self._auto_extend_on_turn_limit():
                    self._emit_progress(
                        f"↔️ Tur tavanı ({self.config.max_turns}) doldu; bitmemiş proje için "
                        f"{TURN_LIMIT_AUTO_EXTEND} tur eklendi."
                    )
                next_decision = await self._guard_premature_completion(
                    next_decision,
                    completed_turns=done_so_far,
                    last_sent=prev.sent_to_agent,
                    agent_reply=resume_msg,
                    sandbox_evidence=resume_evidence,
                )

            for turn in range(start_turn, self.config.max_turns + 1):
                if self._stop_requested:
                    return await self._finish_stopped("Kullanıcı tarafından durduruldu.")

                # Sabah 07:00 olduysa yeni tura girmeden durakla ve 22:00'yi bekle
                if not await self._wait_for_night_window_if_needed(
                    stage_note=f"Tur {turn} öncesi"
                ):
                    return await self._finish_stopped("Gece penceresini beklerken durduruldu.")

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
                    await self._collect_and_dispatch_morning_report(
                        headline="🌅 Sabah Teslim Raporu — Proje Tamamlandı"
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

                # Ajana gidecek mesajı hazırla:
                # 1. turda tam çalışma/sunucu kurallarını, 2..N turlarında ise her tur
                # kesin ask_user / onay penceresi yasağını otomatik ekle.
                outgoing = next_decision.message_to_agent.strip()
                if self.config.append_guardrails_on_first_turn:
                    if turn == 1:
                        outgoing = outgoing + AGENT_OPERATING_GUARDRAILS
                    elif "ask_user" not in outgoing.lower():
                        outgoing = outgoing + AGENT_FOLLOWUP_NO_ASK_GUARDRAIL

                await self._sleep_gap()
                if self._stop_requested:
                    return await self._finish_stopped("Kullanıcı tarafından durduruldu.")

                self.state.pending_turn = turn
                self.state.pending_instruction = next_decision.message_to_agent.strip()
                self.state.pending_evaluation = next_decision.evaluation
                self._persist_state()

                t0 = time.monotonic()
                self._turn_started_at = t0
                # Sessizlik nöbetçisi tur başında sıfırlanır: yeni tur "yeni sinyal".
                # Saatler `t0`'a kurulur (0.0'a değil): 0.0 "önyükleme anı" demekti;
                # makine 20 dk'dan yeniyse ilk sessizlik notu yutuluyor, 10 dk'dan
                # yeniyse ilk kalp atışı tur satırının hemen ardından (çift mesaj)
                # çıkıyordu. Tur sayaçları tura göre işlemeli.
                self._last_agent_signal = t0
                self._last_silent_notice = t0
                self._last_heartbeat = t0
                self._silence_notices_this_turn = 0
                self._last_heartbeat_signature = None
                self._turn_activity = "talimat gönderildi"
                self._emit_progress(
                    f"Tur {turn}/{self.config.max_turns} [{self.state.current_phase}]: "
                    "Talimat Agent'a gönderiliyor ve sandbox çalışması bekleniyor..."
                )
                await self._notify_telegram(
                    self._turn_start_line(turn, outgoing)
                )
                agent_reply = await self._send_turn_with_retries(
                    outgoing, turn=turn
                )
                if agent_reply is None:
                    return self.state
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
                self.state.pending_turn = 0
                self.state.pending_instruction = ""
                self.state.pending_evaluation = ""
                self._persist_state()
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
                    await self._collect_and_dispatch_morning_report(
                        headline="🌅 Sabah Teslim Raporu — Proje Tamamlandı"
                    )
                    return self.state

                if next_decision.status == "TAKILDI":
                    recovered = await self._recover_from_stuck(
                        next_decision,
                        completed_turns=done_so_far,
                        last_sent=record.sent_to_agent,
                        agent_reply=agent_reply,
                        sandbox_evidence=evidence,
                        anti_loop_warning=anti_loop_note,
                    )
                    if recovered is None:
                        return self.state
                    next_decision = recovered
                    if next_decision.updated_memory.strip():
                        self.state.memory_summary = next_decision.updated_memory.strip()
                    self.state.current_phase = next_decision.phase

            self.state.status = "max_turns_reached"
            self.state.stop_reason = (
                next_decision.evaluation
                if next_decision and next_decision.evaluation
                else f"Azami tur sınırına ({self.config.max_turns}) ulaşıldı."
            )
            self._emit_progress(f"⏹️ Azami tur sınırına ({self.config.max_turns}) ulaşıldı.")
            # `max_turns_reached` artık "ölüm" değil, "duraklama": teslim raporu
            # hazırlanır ve koşu `/devam` (ya da yeni `extend` ucu) ile aynı Agent
            # sohbetinde sürdürülebilir. Bu yüzden `failed` işaretlenmez.
            await self._collect_and_dispatch_morning_report(
                headline=(
                    "🌅 Sabah Teslim Raporu — Tur Sınırına Ulaşıldı "
                    f"({self.config.max_turns} tur)"
                )
            )
            return self.state

        except asyncio.CancelledError:
            if self.state.status not in {"completed", "max_turns_reached", "failed"}:
                self.state.status = "stopped"
                if not self.state.stop_reason:
                    self.state.stop_reason = "Kullanıcı tarafından zorla durduruldu."
                self._persist_state()
            raise
        except Exception as exc:
            safe_detail = error_excerpt(str(exc), limit=240)
            self.state.status = "failed"
            self.state.error_detail = (
                f"{type(exc).__name__}: {safe_detail}"
                if safe_detail
                else type(exc).__name__
            )
            if not self.state.stop_reason:
                self.state.stop_reason = self.state.error_detail
            self._persist_state()
            logger.error(
                "autopilot_run_failed",
                run_id=self.state.run_id,
                error_type=type(exc).__name__,
            )
            alert_lines = [
                "🔴 ApiWrapper Autopilot hata ile durdu",
                *self._project_header_lines(),
                f"Tur: {self.state.current_turn}",
                f"Hata türü: {type(exc).__name__}",
            ]
            if safe_detail:
                alert_lines.append(f"Detay: {safe_detail}")
            await self._notify_telegram("\n".join(alert_lines))
            raise
        finally:
            await self.max_bridge.shutdown()
            await self.agent.shutdown()
