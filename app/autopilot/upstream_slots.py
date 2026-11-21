"""Çalışma zamanında `max` hesap yuvalarının (Yuva 1 & Yuva 2) canlı yönetimi.

Gündüz 2 hesapla (`upstream_curl` + `upstream_curl2`) `max` yük paylaşımı yapıp,
akşam (`22:00` öncesi) `/upstream_curl2 kapat` komutuyla 2. hesabın:
- keepalive arka plan görevini,
- HTTP/2 bağlantı havuzunu ve çerez kavanozunu (`aclose()`),
- hat (`Lane`) ve hesap havuzu (`AccountPool`) kaydını,
- `Settings` üzerindeki kimlik alanlarını
kökten kapatarak aynı 2. Google hesabının gece Agent Mode (`/agent_curl`) için
çakışmasız kullanılabilmesini sağlar.

`app/upstream/*` ve `app/services/*` dosyalarına dokunmadan `FastAPI` uygulama
durumu (`app.state`) üzerinde atomik kilit (`asyncio.Lock`) ile çalışır.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable, Coroutine, Iterable
from typing import Any, Literal

from fastapi import FastAPI

from app.autopilot.curl_parse import (
    detect_pasted_curl_kind,
    extract_identity_subject,
    normalize_windows_cmd_curl_for_upstream,
)
from app.autopilot.manager import AutopilotManager
from app.core.config import Settings
from app.core.diagnostics import explain_failure
from app.core.logging import get_logger
from app.services.account import Account, AccountPool
from app.services.alerting import ProbeResult
from app.services.lanes import Lane, LaneSet
from app.services.request_queue import RequestQueue
from app.services.upstream_health import UpstreamHealth
from app.upstream.breaker import BreakerState
from app.upstream.curl_runtime import _reject_duplicate_accounts, apply_upstream_curl

logger = get_logger(__name__)

KeepaliveRunner = Callable[..., Coroutine[Any, Any, None]]

_SLOT2_PRESERVE_ATTRS: tuple[str, ...] = (
    "upstream_curl_2",
    "upstream_cookie_2",
    "target_domain_2",
    "upstream_access_token_2",
    "upstream_auth_from_cookie_2",
    "upstream_auth_scheme_2",
    "upstream_token_cookie_names_2",
    "upstream_user_agent_2",
    "upstream_accept_language_2",
    "upstream_referer_path_2",
    "recaptcha_static_token_2",
    "upstream_account_2_name",
)


def _identity_fingerprint(sub: str) -> str | None:
    """Google `sub` değerini gizli bilgi sızdırmadan kısa parmak izine çevirir."""
    cleaned = (sub or "").strip()
    if not cleaned:
        return None
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"hesap#{digest}"


class UpstreamSlotRuntimeManager:
    """`max` Yuva 1 ve Yuva 2'yi süreç yeniden başlatmadan canlı yönetir."""

    def __init__(
        self,
        app: FastAPI,
        *,
        keepalive_runner: KeepaliveRunner,
    ) -> None:
        self.app = app
        self._keepalive_runner = keepalive_runner
        self._lock = asyncio.Lock()
        self._keepalive_tasks: dict[int, asyncio.Task[None]] = {}
        self._keepalive_multi_mode: dict[int, bool] = {}

    # ------------------------------------------------- yaşam döngüsü / keepalive
    @staticmethod
    def _probe_notes(
        lanes: LaneSet,
        lane: Lane,
    ) -> Callable[[ProbeResult], tuple[str, ...]]:
        def build(result: ProbeResult) -> tuple[str, ...]:
            critical = explain_failure(result.status_code, result.code).severity == "critical"
            outlook = lanes.outlook(lane)
            return (
                *((outlook,) if outlook else ()),
                *lanes.incident_notes(lane, critical=critical),
            )

        return build

    def _spawn_keepalive_for_lane(
        self,
        lane: Lane,
        *,
        start_delay: float,
    ) -> None:
        settings: Settings = self.app.state.settings
        lanes: LaneSet = self.app.state.lanes
        alerter = self.app.state.alerter
        slot_key = lane.slot if lane.slot > 0 else 1
        pinned = lane.slot if lanes.multi else None
        task = asyncio.create_task(
            self._keepalive_runner(
                settings,
                alerter=alerter,
                health=lane.health,
                queue=lane.queue,
                account_slot=pinned,
                scope=lanes.scope(lane),
                start_delay=start_delay,
                notes=self._probe_notes(lanes, lane) if lanes.multi else None,
            ),
            name=f"upstream-keepalive-{lane.index + 1}",
        )
        self._keepalive_tasks[slot_key] = task
        self._keepalive_multi_mode[slot_key] = lanes.multi

    def start_initial_keepalives(self) -> None:
        """Uygulama açılışında yapılandırılmış hatlar için keepalive görevlerini başlatır."""
        settings: Settings = self.app.state.settings
        lanes: LaneSet = self.app.state.lanes
        keepalive_interval = settings.upstream_keepalive_interval_seconds
        if keepalive_interval <= 0:
            return
        stagger = min(60.0, keepalive_interval / len(lanes))
        for lane in lanes.lanes:
            self._spawn_keepalive_for_lane(lane, start_delay=lane.index * stagger)

    async def _cancel_keepalive(self, slot_key: int) -> bool:
        task = self._keepalive_tasks.pop(slot_key, None)
        self._keepalive_multi_mode.pop(slot_key, None)
        if task is None:
            return False
        was_running = not task.done()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return was_running

    async def _reconcile_keepalives(self, *, updated_slot: int | None) -> None:
        """Aktif hat kümesi değiştikten sonra keepalive görevlerini uyumlu hâle getirir."""
        settings: Settings = self.app.state.settings
        lanes: LaneSet = self.app.state.lanes
        interval = settings.upstream_keepalive_interval_seconds
        if interval <= 0:
            for key in list(self._keepalive_tasks):
                await self._cancel_keepalive(key)
            return

        active_keys = {(ln.slot if ln.slot > 0 else 1) for ln in lanes.lanes}
        for key in list(self._keepalive_tasks):
            if key not in active_keys:
                await self._cancel_keepalive(key)

        for lane in lanes.lanes:
            slot_key = lane.slot if lane.slot > 0 else 1
            existing = self._keepalive_tasks.get(slot_key)
            mode_changed = self._keepalive_multi_mode.get(slot_key) != lanes.multi
            needs_restart = (
                slot_key == updated_slot
                or existing is None
                or existing.done()
                or mode_changed
            )
            if not needs_restart:
                continue
            await self._cancel_keepalive(slot_key)
            # Yalnız cURL'ü yeni yüklenen yuva başlangıç gecikmesiyle yoklanır;
            # diğer yuva sadece tekli<->çoklu mod sabitlemesi için yenilendiyse
            # fazladan istek atmaması adına tam aralık kadar bekler.
            delay = (
                min(60.0, max(5.0, settings.upstream_keepalive_initial_delay_seconds))
                if slot_key == updated_slot
                else interval
            )
            self._spawn_keepalive_for_lane(lane, start_delay=delay)

    async def shutdown(self) -> None:
        """Uygulama kapanırken tüm keepalive görevlerini temizler."""
        keys = list(self._keepalive_tasks)
        for key in keys:
            task = self._keepalive_tasks.get(key)
            if task is not None:
                task.cancel()
        for key in keys:
            task = self._keepalive_tasks.pop(key, None)
            self._keepalive_multi_mode.pop(key, None)
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # ------------------------------------------------- yardımcı iç yöntemler
    def _propagate_settings(self, updated: Settings) -> None:
        """Yeni `Settings` nesnesini tüm çalışma zamanı bileşenlerine yayar."""
        st = self.app.state
        st.settings = updated
        st.upstream.settings = updated
        st.upstream.deployment.settings = updated
        st.recaptcha.settings = updated
        st.sessions.settings = updated
        st.completion_service.settings = updated
        st.accounts.settings = updated

    async def _evict_account_sessions(self, labels: Iterable[str]) -> int:
        """Verilen hesap etiketlerine bağlı önbellekli `Session` kayıtlarını temizler."""
        suffixes = tuple(f"@{lbl}" for lbl in labels if lbl)
        if not suffixes:
            return 0
        cache = self.app.state.sessions._cache
        async with cache._lock:
            keys = [
                k
                for k in cache._data
                if isinstance(k, str) and k.endswith(suffixes)
            ]
            for key in keys:
                cache._data.pop(key, None)
        return len(keys)

    # ------------------------------------------------- durum sorgulama
    def status_snapshot(self) -> dict[str, Any]:
        """Gizli değer (cookie/token/cURL) içermeyen canlı yuva durum özeti."""
        settings: Settings = self.app.state.settings
        accounts: AccountPool = self.app.state.accounts
        lanes: LaneSet = self.app.state.lanes
        upstream = self.app.state.upstream

        slots_info: list[dict[str, Any]] = []
        for slot in (1, 2):
            acc_env = Account.from_env(settings, slot)
            acc_pool = accounts.get(slot)
            lane = lanes.get(slot) or (
                lanes.primary if slot == 1 and not lanes.multi else None
            )
            configured = bool(acc_env.is_configured and settings.target_domain.strip())
            sub = (
                extract_identity_subject(acc_env.cookie, acc_env.access_token)
                if configured
                else ""
            )
            ka_task = self._keepalive_tasks.get(slot)
            ka_running = bool(ka_task is not None and not ka_task.done())
            if slot == 1:
                pool_open = 1 in upstream._account_clients or (
                    configured and upstream._client is not None
                )
            else:
                pool_open = 2 in upstream._account_clients

            slot_label = (
                (acc_pool.label if acc_pool else acc_env.label)
                if configured
                else f"hesap-{slot}"
            )
            slots_info.append(
                {
                    "slot": slot,
                    "configured": configured,
                    "label": slot_label,
                    "usable": bool(configured and lane is not None and lanes.usable(lane)),
                    "failing": bool(lane.health.failing) if (configured and lane) else False,
                    "failure_code": (lane.health.code or None) if (configured and lane) else None,
                    "cooldown_remaining_seconds": (
                        round(accounts.cooldown_remaining(slot), 1)
                        if configured and acc_pool is not None
                        else 0.0
                    ),
                    "messages_in_window": (
                        accounts.messages_in_window(slot)
                        if configured and acc_pool is not None
                        else 0
                    ),
                    "effective_budget": (
                        accounts.effective_budget(slot)
                        if configured and acc_pool is not None
                        else settings.account_msg_budget
                    ),
                    "queue_active": (
                        lane.queue.snapshot()["active"]
                        if configured and lane is not None
                        else None
                    ),
                    "queue_waiting": (
                        lane.queue.waiting if configured and lane is not None else 0
                    ),
                    "keepalive_running": ka_running,
                    "http_client_open": pool_open,
                    "dedicated_account_client_open": slot in upstream._account_clients,
                    "identity_fingerprint": _identity_fingerprint(sub),
                }
            )

        active_slots = [item["slot"] for item in slots_info if item["configured"]]
        return {
            "active_slots": active_slots,
            "multi_lane": lanes.multi,
            "slots": slots_info,
        }

    # ------------------------------------------------- Yuva 1 / Yuva 2 güncelleme
    async def update_slot(
        self,
        slot: Literal[1, 2],
        raw_curl: str,
        *,
        autopilot_manager: AutopilotManager | None = None,
    ) -> dict[str, Any]:
        """Yuva 1 (`upstream_curl`) veya Yuva 2 (`upstream_curl2`) cURL'ünü canlı uygular."""
        if slot not in (1, 2):
            raise ValueError("Yalnızca 1. veya 2. Max yuvası güncellenebilir.")

        raw_trimmed = (raw_curl or "").lstrip()
        if raw_trimmed.lower().startswith(("invoke-webrequest", "iwr ", "fetch(")):
            raise ValueError(
                "Bu metin PowerShell/fetch biçiminde kopyalanmış. Tarayıcı Network "
                "sekmesinde sağ tıklayıp 'Copy -> Copy as cURL (bash)' (veya Copy as "
                "cURL (cmd)) seçeneğini kullanın."
            )

        cleaned = normalize_windows_cmd_curl_for_upstream((raw_curl or "").strip())
        if not cleaned:
            raise ValueError("Max cURL metni boş.")

        kind = detect_pasted_curl_kind(cleaned)
        if kind == "agent":
            raise ValueError(
                "Yapıştırdığınız cURL Agent Mode sohbetine ('/api/chat/...' veya 'create-chat') "
                "ait görünüyor. Max yuvası için normal sohbetten 'create-evaluation' cURL'ü "
                "kopyalayın (Agent cURL'ü için: /agent_curl)."
            )

        async with self._lock:
            current: Settings = self.app.state.settings
            acc1_before = Account.from_env(current, 1)
            acc2_before = Account.from_env(current, 2)

            if slot == 2 and (
                not acc1_before.is_configured or not current.target_domain.strip()
            ):
                raise ValueError(
                    "Önce 1. ana Max hesabını (/upstream_curl) yapılandırın; "
                    "2. hesap ortak akış ayarlarını 1. hesaptan alır."
                )

            # Yuva 1 güncellenirken Yuva 2 daha önce ayrı env değişkenleriyle (UPSTREAM_CURL_2
            # olmadan) tanımlanmışsa alanlarını koru ve yine de tekil-hesap kontrolünden geçir.
            preserved_slot2: dict[str, Any] = {}
            if (
                slot == 1
                and acc2_before.is_configured
                and not current.upstream_curl_2.strip()
            ):
                preserved_slot2 = {
                    attr: getattr(current, attr) for attr in _SLOT2_PRESERVE_ATTRS
                }

            field_name = "upstream_curl" if slot == 1 else "upstream_curl_2"
            candidate = current.model_copy(update={field_name: cleaned})
            updated, model_id = apply_upstream_curl(candidate)

            if preserved_slot2:
                merged = updated.model_dump()
                merged.update(preserved_slot2)
                updated = Settings(**merged)
                _reject_duplicate_accounts(updated, from_curl={1, 2})

            new_acc = Account.from_env(updated, slot)
            new_sub = extract_identity_subject(new_acc.cookie, new_acc.access_token)
            if new_sub and autopilot_manager is not None:
                active_agents = autopilot_manager.active_agent_identities()
                if new_sub in active_agents:
                    conflict_run = active_agents[new_sub]
                    raise ValueError(
                        f"Hesap Çakışması: Bu Google hesabı şu anda aktif/gece sırasındaki "
                        f"'{conflict_run}' Otopilot Agent projesinde kullanılıyor! "
                        f"İki oturumun birbirini düşürmemesi için önce Agent projesinin "
                        f"tamamlanmasını bekleyin veya '/durdur {conflict_run}' yazın."
                    )

            self._propagate_settings(updated)

            upstream = self.app.state.upstream
            accounts: AccountPool = self.app.state.accounts
            lanes: LaneSet = self.app.state.lanes
            registry = self.app.state.registry

            acc1 = Account.from_env(updated, 1)
            acc2 = Account.from_env(updated, 2)
            extra_accounts = [a for a in accounts.accounts if a.slot > 2 and a.is_configured]
            accounts.accounts = [
                a for a in (acc1, acc2, *extra_accounts) if a.is_configured
            ]
            for a in accounts.accounts:
                accounts.state(a.slot)
            accounts.clear_cooldown(slot)

            if slot == 1:
                if model_id:
                    with contextlib.suppress(Exception):
                        entry = registry.resolve(None)
                        entry.upstream_id = model_id

                upstream.deployment._entries.clear()
                upstream.deployment._warned.clear()
                upstream.breaker._failures = 0
                upstream.breaker._state = BreakerState.CLOSED

                old_global = upstream._client
                upstream._client = upstream._build_client(updated)
                old_slot1 = upstream._account_clients.pop(1, None)
                upstream._account_errors.pop(1, None)
                if old_slot1 is not None:
                    await old_slot1.aclose()
                if old_global is not None:
                    await old_global.aclose()

                lane1 = lanes.get(1) or lanes.primary
                old_label1 = lane1.label
                lane1.account = acc1 if acc1.is_configured else None
                lane1.health.reset()
                lane1.queue.name = (
                    acc1.label if len(lanes.lanes) > 1 and acc1.is_configured else ""
                )
                await self._evict_account_sessions({old_label1, acc1.label, "hesap-1"})
            else:
                old_slot2 = upstream._account_clients.pop(2, None)
                upstream._account_errors.pop(2, None)
                if old_slot2 is not None:
                    await old_slot2.aclose()

                lane1 = lanes.primary
                if lane1.account is None and acc1.is_configured:
                    lane1.account = acc1

                lane2 = lanes.get(2)
                old_label2 = lane2.label if lane2 is not None else "hesap-2"
                if lane2 is not None:
                    lane2.account = acc2
                    lane2.health.reset()
                    lane2.queue.name = acc2.label
                else:
                    lane2 = Lane(
                        len(lanes.lanes),
                        acc2,
                        RequestQueue.from_settings(updated, name=acc2.label),
                        UpstreamHealth(updated.ask_problem_hold_seconds),
                    )
                    lanes.lanes.append(lane2)

                for idx, ln in enumerate(lanes.lanes):
                    ln.index = idx
                if lanes.multi and lane1.account is not None:
                    lane1.queue.name = lane1.account.label
                await self._evict_account_sessions({old_label2, acc2.label, "hesap-2"})

            lanes.failover = updated.account_failover
            lanes._cooldown = accounts.cooldown_remaining
            self.app.state.queue = lanes.primary.queue
            self.app.state.upstream_health = lanes.primary.health

            await self._reconcile_keepalives(updated_slot=slot)

            logger.info(
                "upstream_slot_hot_reloaded",
                slot=slot,
                active_accounts=accounts.size,
                multi_lane=lanes.multi,
                model_updated=bool(slot == 1 and model_id),
            )

            snap = self.status_snapshot()
            slot_view = next(s for s in snap["slots"] if s["slot"] == slot)
            return {
                "updated_slot": slot,
                "label": slot_view["label"],
                "identity_fingerprint": slot_view["identity_fingerprint"],
                "target_domain": new_acc.target_domain or updated.target_domain,
                "has_recaptcha_token": bool(
                    new_acc.recaptcha_token
                    or (slot == 1 and updated.recaptcha_static_token)
                ),
                "model_id_updated": bool(slot == 1 and model_id),
                "active_slots": snap["active_slots"],
                "multi_lane": snap["multi_lane"],
            }

    # ------------------------------------------------- Yuva 2 kökten kapatma
    async def close_slot_2(self) -> dict[str, Any]:
        """2. Max hesabını (`upstream_curl2`) ve tüm ağ/keepalive izlerini kökten kapatır."""
        async with self._lock:
            current: Settings = self.app.state.settings
            upstream = self.app.state.upstream
            accounts: AccountPool = self.app.state.accounts
            lanes: LaneSet = self.app.state.lanes

            acc2_before = Account.from_env(current, 2)
            lane2 = lanes.get(2)
            was_active = bool(
                acc2_before.is_configured
                or lane2 is not None
                or 2 in upstream._account_clients
                or 2 in self._keepalive_tasks
            )

            # 1) Keepalive 2 görevini derhal iptal et ve bitmesini bekle
            keepalive_stopped = await self._cancel_keepalive(2)

            # 2) Hat 2'yi dağıtıcıdan çıkar; sırada bekleyen istek varsa Hat 1'e aktar
            old_label2 = lane2.label if lane2 is not None else acc2_before.label
            reassigned_waiters = 0
            if lane2 is not None:
                lanes.lanes = [ln for ln in lanes.lanes if ln.slot != 2]
                if not lanes.lanes:
                    acc1_fallback = Account.from_env(current, 1)
                    lanes.lanes = [
                        Lane(
                            0,
                            acc1_fallback if acc1_fallback.is_configured else None,
                            RequestQueue.from_settings(current),
                            UpstreamHealth(current.ask_problem_hold_seconds),
                        )
                    ]
                for idx, ln in enumerate(lanes.lanes):
                    ln.index = idx
                if not lanes.multi and lanes.lanes:
                    lanes.primary.queue.name = ""

                stale_sticky = [k for k, v in lanes._sticky.items() if v == 2]
                for key in stale_sticky:
                    lanes._sticky.pop(key, None)

                # Kuyrukta bekleyen isteklerin `_wait_turn` döngüsü `should_leave()`
                # üzerinden `QueueAbandoned` fırlatıp otomatik Hat 1'e devredilir:
                for waiter in list(lane2.queue._waiters):
                    if not waiter.future.done():
                        waiter.should_leave = lambda: True
                        reassigned_waiters += 1

            # 3) Hesap 2'yi havuzdan ve kota penceresinden sil
            accounts.accounts = [a for a in accounts.accounts if a.slot != 2]
            accounts._states.pop(2, None)

            # 4) Hesap 2'nin özel HTTP/2 bağlantı havuzunu + çerez kavanozunu kapat
            client2 = upstream._account_clients.pop(2, None)
            upstream._account_errors.pop(2, None)
            http_client_closed = client2 is not None
            if client2 is not None:
                await client2.aclose()

            # 5) Settings üzerindeki tüm `*_2` kimlik alanlarını sıfırla ve yay
            cleared_updates: dict[str, Any] = {
                "upstream_curl_2": "",
                "upstream_cookie_2": "",
                "target_domain_2": "",
                "upstream_access_token_2": "",
                "upstream_auth_from_cookie_2": False,
                "upstream_auth_scheme_2": "Bearer",
                "upstream_token_cookie_names_2": [],
                "upstream_user_agent_2": "",
                "upstream_accept_language_2": "",
                "upstream_referer_path_2": "",
                "recaptcha_static_token_2": "",
                "upstream_account_2_name": "",
            }
            merged = current.model_dump()
            merged.update(cleared_updates)
            updated = Settings(**merged)
            self._propagate_settings(updated)

            # 6) Hesap 2'ye bağlı önbellekli sohbet oturumlarını sil
            evicted_sessions = await self._evict_account_sessions(
                {old_label2, "hesap-2"}
            )

            # 7) Ana hattın referanslarını ve tekli keepalive kipini güncelle
            self.app.state.queue = lanes.primary.queue
            self.app.state.upstream_health = lanes.primary.health
            await self._reconcile_keepalives(updated_slot=None)

            logger.info(
                "upstream_slot_2_closed",
                was_active=was_active,
                http_client_closed=http_client_closed,
                keepalive_stopped=keepalive_stopped,
                reassigned_waiters=reassigned_waiters,
                evicted_sessions=evicted_sessions,
            )

            snap = self.status_snapshot()
            return {
                "closed_slot": 2,
                "was_active": was_active,
                "http_client_closed": http_client_closed,
                "keepalive_stopped": keepalive_stopped,
                "reassigned_waiters": reassigned_waiters,
                "evicted_sessions": evicted_sessions,
                "active_slots": snap["active_slots"],
                "multi_lane": snap["multi_lane"],
            }
