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
from typing import Any

from fastapi import FastAPI

from app.autopilot.curl_parse import (
    detect_pasted_curl_kind,
    extract_identity_subject,
    normalize_windows_cmd_curl_for_upstream,
)
from app.autopilot.manager import AutopilotManager
from app.core.config import MAX_ACCOUNT_SLOTS, Settings
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

#: Bir yuva canlı güncellenirken korunacak hesaba-özgü alan adları.
#: (Eski ad `_SLOT2_PRESERVE_ATTRS` yalnız 2. yuva içindi; 3./4. yuva açılınca
#: kalıp genelleştirildi.)
_SLOT_PRESERVE_FIELDS: tuple[str, ...] = (
    "upstream_cookie",
    "target_domain",
    "upstream_access_token",
    "upstream_auth_from_cookie",
    "upstream_auth_scheme",
    "upstream_token_cookie_names",
    "upstream_user_agent",
    "upstream_accept_language",
    "upstream_referer_path",
    "recaptcha_static_token",
)
#: Geriye dönük uyumluluk: eski test/kod 2. yuva adını arıyor olabilir.
_SLOT2_PRESERVE_ATTRS: tuple[str, ...] = tuple(
    f"{name}_2" for name in _SLOT_PRESERVE_FIELDS if name != "upstream_account_name"
) + ("upstream_account_2_name",)


def _preserve_attrs(slot: int) -> tuple[str, ...]:
    """Verilen yuvanın hesaba-özgü `Settings` alan adları."""
    if slot <= 1:
        return ()
    return tuple(f"{name}_{slot}" for name in _SLOT_PRESERVE_FIELDS) + (
        f"upstream_account_{slot}_name",
    )


def _curl_field(slot: int) -> str:
    return "upstream_curl" if slot <= 1 else f"upstream_curl_{slot}"


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
        """Uygulama açılışında yapılandırılmış hatlar için keepalive görevlerini başlatır.

        Rol süzgeci **burada da** uygulanır: `agent` yuvasına açılışta ısıtma
        isteği gönderilmez (o pencere Agent projesinin hakkı). Süzgeç yalnız
        yeniden uyumlamada olsaydı, her deploy'da Agent hesabına bir probe
        giderdi — belgede "agent yuvasında keepalive yok" yazıyor.
        """
        settings: Settings = self.app.state.settings
        lanes: LaneSet = self.app.state.lanes
        keepalive_interval = settings.upstream_keepalive_interval_seconds
        if keepalive_interval <= 0:
            return
        roles = lanes.roles
        eligible = [
            lane
            for lane in lanes.lanes
            if roles is None or roles.keepalive_allowed(lane.slot)
        ]
        if not eligible:
            logger.info("upstream_keepalive_skipped", reason="uygun rol yok (agent)")
            return
        stagger = min(60.0, keepalive_interval / len(eligible))
        for position, lane in enumerate(eligible):
            self._spawn_keepalive_for_lane(lane, start_delay=position * stagger)

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

        # `agent` rolündeki yuva için keepalive **çalışmaz** (o pencere Agent'ın
        # hakkı) ama yuva açık kalır: cURL, çerez ve sıra yerinde durur.
        active_keys = {
            (ln.slot if ln.slot > 0 else 1)
            for ln in lanes.lanes
            if lanes.roles is None or lanes.roles.keepalive_allowed(ln.slot)
        }
        for key in list(self._keepalive_tasks):
            if key not in active_keys:
                await self._cancel_keepalive(key)

        for lane in lanes.lanes:
            slot_key = lane.slot if lane.slot > 0 else 1
            if slot_key not in active_keys:
                continue
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
        for slot in range(1, MAX_ACCOUNT_SLOTS + 1):
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
                pool_open = slot in upstream._account_clients

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
                    # Rol: hesap **açık kalır**; yalnız ne iş yaptığını söyler.
                    "role": (
                        lanes.roles.role_of(slot) if lanes.roles is not None else "max"
                    ),
                    "keepalive_allowed": (
                        lanes.roles.keepalive_allowed(slot)
                        if lanes.roles is not None
                        else True
                    ),
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
            "roles": self.role_snapshot(slots_info),
            # Hesap rotasyonu: "hangi hesap cevaplıyor" sorusunun tek kaynağı.
            "rotation": self._rotation_snapshot(),
            "slot_roles": (
                lanes.roles.snapshot() if lanes.roles is not None else {"enabled": False}
            ),
        }

    @staticmethod
    def role_snapshot(slots_info: list[dict[str, Any]]) -> dict[str, Any]:
        """Yuvaları **rol** adlarıyla özetler (secret içermez).

        Roller yuvadan bağımsızdır: rotasyonda hangi Google hesabının hangi
        rolde olduğu değişir, `max` trafiğinin şekli değişmez.
        """
        by_slot = {int(item["slot"]): item for item in slots_info}
        primary = by_slot.get(1, {})
        secondary = by_slot.get(2, {})
        spare = by_slot.get(3, {})
        return {
            "max_primary": {
                "slot": 1,
                "configured": bool(primary.get("configured")),
                "label": primary.get("label"),
                "identity_fingerprint": primary.get("identity_fingerprint"),
            },
            "max_secondary": {
                "slot": 2,
                "configured": bool(secondary.get("configured")),
                "label": secondary.get("label"),
                "identity_fingerprint": secondary.get("identity_fingerprint"),
            },
            "max_spare": {
                "slot": 3,
                "configured": bool(spare.get("configured")),
                "label": spare.get("label"),
                "identity_fingerprint": spare.get("identity_fingerprint"),
            },
            "configured_count": sum(
                1 for item in slots_info if item.get("configured")
            ),
        }

    # ------------------------------------------------- Yuva 1 / Yuva 2 güncelleme
    async def update_slot(
        self,
        slot: int,
        raw_curl: str,
        *,
        autopilot_manager: AutopilotManager | None = None,
    ) -> dict[str, Any]:
        """Bir Max yuvasının (`UPSTREAM_CURL` / `UPSTREAM_CURL_N`) cURL'ünü canlı uygular.

        1. yuva ortak şekli (akış yolu, `mode`, model kimliği) belirler; diğer
        yuvalar yalnız hesaba-özgü alanları getirir. 3. ve 4. yuvalar da
        desteklenir (`MAX_ACCOUNT_SLOTS`); böylece üç hesap rotasyonu mümkündür.
        """
        if not 1 <= slot <= MAX_ACCOUNT_SLOTS:
            raise ValueError(
                f"Yalnızca 1..{MAX_ACCOUNT_SLOTS} arası Max yuvaları güncellenebilir."
            )

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

            if slot > 1 and (
                not acc1_before.is_configured or not current.target_domain.strip()
            ):
                raise ValueError(
                    "Önce 1. ana Max hesabını (/upstream_curl) yapılandırın; "
                    f"{slot}. hesap ortak akış ayarlarını 1. hesaptan alır."
                )

            # Yuva 1 güncellenirken diğer yuvalar ayrı env değişkenleriyle
            # (`UPSTREAM_CURL_N` olmadan) tanımlanmışsa alanlarını koru ve yine de
            # tekil-hesap kontrolünden geçir.
            preserved_others: dict[str, Any] = {}
            if slot == 1:
                for other in range(2, MAX_ACCOUNT_SLOTS + 1):
                    other_acc = Account.from_env(current, other)
                    if other_acc.is_configured and not str(
                        getattr(current, _curl_field(other), "") or ""
                    ).strip():
                        preserved_others.update(
                            {attr: getattr(current, attr) for attr in _preserve_attrs(other)}
                        )

            field_name = _curl_field(slot)
            candidate = current.model_copy(update={field_name: cleaned})
            updated, model_id = apply_upstream_curl(candidate)

            if preserved_others:
                merged = updated.model_dump()
                merged.update(preserved_others)
                updated = Settings(**merged)
                _reject_duplicate_accounts(
                    updated,
                    from_curl={1, *(s for s in range(2, MAX_ACCOUNT_SLOTS + 1))},
                )

            new_acc = Account.from_env(updated, slot)
            new_sub = extract_identity_subject(new_acc.cookie, new_acc.access_token)
            if new_sub and autopilot_manager is not None:
                active_agents = autopilot_manager.active_agent_identities()
                if new_sub in active_agents:
                    conflict_run = active_agents[new_sub]
                    if updated.allow_same_account_agent_max:
                        logger.warning(
                            "upstream_slot_same_account_guard_disabled",
                            slot=int(slot),
                            run_id=conflict_run,
                        )
                    else:
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
            acc_slot = Account.from_env(updated, slot)
            slot_accounts = [
                Account.from_env(updated, n) for n in range(2, MAX_ACCOUNT_SLOTS + 1)
            ]
            accounts.accounts = [a for a in (acc1, *slot_accounts) if a.is_configured]
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
                old_client = upstream._account_clients.pop(slot, None)
                upstream._account_errors.pop(slot, None)
                if old_client is not None:
                    await old_client.aclose()

                lane1 = lanes.primary
                if lane1.account is None and acc1.is_configured:
                    lane1.account = acc1

                target_lane = lanes.get(slot)
                old_label = (
                    target_lane.label if target_lane is not None else f"hesap-{slot}"
                )
                if target_lane is not None:
                    target_lane.account = acc_slot
                    target_lane.health.reset()
                    target_lane.queue.name = acc_slot.label
                else:
                    target_lane = Lane(
                        len(lanes.lanes),
                        acc_slot,
                        RequestQueue.from_settings(updated, name=acc_slot.label),
                        UpstreamHealth(updated.ask_problem_hold_seconds),
                    )
                    lanes.lanes.append(target_lane)

                for idx, ln in enumerate(lanes.lanes):
                    ln.index = idx
                if lanes.multi and lane1.account is not None:
                    lane1.queue.name = lane1.account.label
                await self._evict_account_sessions(
                    {old_label, acc_slot.label, f"hesap-{slot}"}
                )

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

    def _rotation_snapshot(self) -> dict[str, Any]:
        """Rotasyon motorunun gizli değer içermeyen görünümü (kapalıysa `enabled: false`)."""
        lanes = self.app.state.lanes
        rotator = getattr(lanes, "rotation", None)
        if rotator is None:
            return {"enabled": False}
        view = rotator.describe()
        view["last_decision"] = getattr(lanes, "last_decision", {}) or {}
        return view

    # ---------------------------------------------------------- rol değiştirme
    async def set_role(self, slot: int, role: str) -> dict[str, Any]:
        """Bir yuvanın **rolünü** değiştirir; hesabı kapatmaz.

        `max` → gündüz trafiği, `spare` → açık/sıcak yedek (trafik almaz),
        `agent` → Agent projesinin hesabı (`max` isteği almaz, keepalive durur).

        cURL, çerez, token ve pencere sayacı **yerinde kalır**; yalnız iş bölümü
        değişir. Etki anındadır: dağıtıcı ve keepalive bu tabloyu canlı okur.
        """
        async with self._lock:
            lanes: LaneSet = self.app.state.lanes
            accounts: AccountPool = self.app.state.accounts
            if not 1 <= int(slot) <= MAX_ACCOUNT_SLOTS:
                raise ValueError(f"Yuva 1..{MAX_ACCOUNT_SLOTS} arasında olmalıdır.")
            if accounts.get(int(slot)) is None:
                raise ValueError(
                    f"Yuva {slot}'te yapılandırılmış hesap yok. Önce "
                    f"/upstream_curl{'' if slot == 1 else slot} ile cURL ekleyin "
                    "(rol vermek hesabı yaratmaz)."
                )
            from app.services.slot_roles import SlotRoles

            if lanes.roles is None:
                lanes.roles = SlotRoles()
            new_role = lanes.roles.set_role(int(slot), role)

            # Rol değişince keepalive tablosu da güncellenir: `agent` yuvasında
            # ısıtma isteği durur (o pencere Agent'a aittir), `max`/`spare`'da sürer.
            await self._reconcile_keepalives(updated_slot=int(slot))
            logger.info("upstream_slot_role_set", slot=int(slot), role=new_role)

            snap = self.status_snapshot()
            return {
                "slot": int(slot),
                "role": new_role,
                "keepalive_allowed": lanes.roles.keepalive_allowed(int(slot)),
                "roles": snap.get("slot_roles"),
                "note": "Hesap açık kaldı; yalnız görevi değişti.",
            }

    # ------------------------------------------------------------ rol takası
    async def swap_roles(
        self,
        slot_a: int,
        slot_b: int,
        *,
        autopilot_manager: AutopilotManager | None = None,
    ) -> dict[str, Any]:
        """İki yuvanın **yerini** değiştirir (hesapları takas eder).

        Neden gerekli: üç hesapta hiçbir hesabın "tamamen tükenmemesi" için
        rollerin (max-1 / max-2 / yedek / Agent) dönüşümlü olması gerekir. Takas
        tek bir `Settings` güncellemesiyle **atomik** yapılır; ara adımda aynı
        Google hesabı iki yuvada görünüp `_reject_duplicate_accounts` hatası
        üretmesin diye.

        Güvenlik: takas edilen hesaplardan biri aktif bir Otopilot Agent
        projesinde kullanılıyorsa reddedilir (aynı hesap iki istemcide =
        token rotasyonu çakışması).
        """
        if slot_a == slot_b:
            raise ValueError("Takas için iki farklı yuva gerekir.")
        for slot in (slot_a, slot_b):
            if not 1 <= slot <= MAX_ACCOUNT_SLOTS:
                raise ValueError(f"Yuva 1..{MAX_ACCOUNT_SLOTS} arasında olmalıdır.")

        async with self._lock:
            current: Settings = self.app.state.settings
            acc_a = Account.from_env(current, slot_a)
            acc_b = Account.from_env(current, slot_b)
            if not acc_a.is_configured or not acc_b.is_configured:
                raise ValueError(
                    f"Takas için iki yuva da dolu olmalı (yuva {slot_a}: "
                    f"{'dolu' if acc_a.is_configured else 'boş'}, yuva {slot_b}: "
                    f"{'dolu' if acc_b.is_configured else 'boş'})."
                )

            sub_a = extract_identity_subject(acc_a.cookie, acc_a.access_token)
            sub_b = extract_identity_subject(acc_b.cookie, acc_b.access_token)
            if autopilot_manager is not None and sub_a and sub_b:
                active_agents = autopilot_manager.active_agent_identities()
                for sub, label in ((sub_a, acc_a.label), (sub_b, acc_b.label)):
                    if sub in active_agents:
                        if current.allow_same_account_agent_max:
                            logger.warning(
                                "upstream_slot_swap_same_account_guard_disabled",
                                label=label,
                                run_id=active_agents[sub],
                            )
                            continue
                        raise ValueError(
                            f"Hesap Çakışması: '{label}' şu anda "
                            f"'{active_agents[sub]}' Otopilot projesinde. Takas için "
                            "önce o koşuyu durdurun."
                        )

            merged = current.model_dump()
            # cURL (asıl sır) + hesaba-özgü tüm alanlar birlikte taşınır.
            curl_a = str(getattr(current, _curl_field(slot_a), "") or "")
            curl_b = str(getattr(current, _curl_field(slot_b), "") or "")
            merged[_curl_field(slot_a)] = curl_b
            merged[_curl_field(slot_b)] = curl_a
            for field in _SLOT_PRESERVE_FIELDS:
                key_a, key_b = f"{field}_{slot_a}", f"{field}_{slot_b}"
                val_a = getattr(current, key_a, None)
                val_b = getattr(current, key_b, None)
                if val_a is not None:
                    merged[key_b] = val_a
                if val_b is not None:
                    merged[key_a] = val_b
            # Hesap adları da taşınır (mesajlarda görünen etiket).
            name_a = str(getattr(current, f"upstream_account_{slot_a}_name", "") or "")
            name_b = str(getattr(current, f"upstream_account_{slot_b}_name", "") or "")
            merged[f"upstream_account_{slot_a}_name"] = name_b
            merged[f"upstream_account_{slot_b}_name"] = name_a
            updated = Settings(**merged)
            # cURL'ler takas edildikten sonra türetilmiş alanlar (çerez, token,
            # hedef alan adı, referer) cURL'ün kendisinden yeniden üretilmeli;
            # yoksa yuva eski hesabın kimliğiyle konuşmaya devam eder.
            updated, _ = apply_upstream_curl(updated)
            _reject_duplicate_accounts(
                updated, from_curl=set(range(1, MAX_ACCOUNT_SLOTS + 1))
            )
            new_a = Account.from_env(updated, slot_a)
            new_b = Account.from_env(updated, slot_b)
            sub_new_a = extract_identity_subject(new_a.cookie, new_a.access_token)
            sub_new_b = extract_identity_subject(new_b.cookie, new_b.access_token)
            # Kimlik okunamıyorsa (çerezde `sub` yok) karşılaştırma yapılamaz;
            # yanlış pozitifle geçerli bir takası engellemek yerine devam edilir.
            if sub_new_a and sub_new_b and sub_new_a == sub_new_b:
                raise ValueError("Takas sonrası iki yuva aynı hesaba düştü; iptal edildi.")

            self._propagate_settings(updated)

            upstream = self.app.state.upstream
            accounts: AccountPool = self.app.state.accounts
            lanes: LaneSet = self.app.state.lanes

            # 1) İki yuvanın özel HTTP/2 + çerez havuzunu kapat (eski kimlikler gitsin)
            for slot in (slot_a, slot_b):
                client = upstream._account_clients.pop(slot, None)
                upstream._account_errors.pop(slot, None)
                if client is not None:
                    await client.aclose()
            old_global = upstream._client
            upstream._client = upstream._build_client(updated)
            if old_global is not None:
                await old_global.aclose()

            # 2) Hesap havuzu ve hatları yeniden kur
            old_labels = {
                lane.label
                for lane in (lanes.get(slot_a), lanes.get(slot_b))
                if lane is not None and lane.label
            }
            slot_accounts = [
                Account.from_env(updated, n) for n in range(1, MAX_ACCOUNT_SLOTS + 1)
            ]
            accounts.accounts = [a for a in slot_accounts if a.is_configured]
            for a in accounts.accounts:
                accounts.state(a.slot)
            for slot in (slot_a, slot_b):
                accounts.clear_cooldown(slot)

            for slot in (slot_a, slot_b):
                acc = Account.from_env(updated, slot)
                lane = lanes.get(slot)
                if lane is not None:
                    lane.account = acc
                    lane.health.reset()
                    lane.queue.name = acc.label if lanes.multi else ""
                elif slot_a == 1 and lanes.primary.account is None:
                    lanes.primary.account = acc
            if not lanes.multi:
                lanes.primary.queue.name = ""

            self.app.state.queue = lanes.primary.queue
            self.app.state.upstream_health = lanes.primary.health
            await self._evict_account_sessions(
                {lbl for lbl in old_labels if lbl}
                | {
                    Account.from_env(updated, slot_a).label,
                    Account.from_env(updated, slot_b).label,
                    f"hesap-{slot_a}",
                    f"hesap-{slot_b}",
                }
            )
            await self._reconcile_keepalives(updated_slot=slot_a)
            await self._reconcile_keepalives(updated_slot=slot_b)

            logger.info(
                "upstream_slot_swapped",
                slot_a=slot_a,
                slot_b=slot_b,
                accounts=accounts.size,
            )
            snap = self.status_snapshot()
            return {
                "swapped": [slot_a, slot_b],
                "slot_a": next(s for s in snap["slots"] if s["slot"] == slot_a),
                "slot_b": next(s for s in snap["slots"] if s["slot"] == slot_b),
                "active_slots": snap["active_slots"],
                "roles": snap["roles"],
            }

    # ------------------------------------- Bir yuvayı kökten bırakma (2, 3, 4)
    async def close_slot_2(self) -> dict[str, Any]:
        """2. Max hesabını (`upstream_curl2`) kökten kapatır (eski ad, uyumluluk)."""
        return await self.release_slot(2)

    async def release_slot(self, slot: int) -> dict[str, Any]:
        """Bir Max yuvasını ve onun tüm ağ/keepalive izlerini kökten bırakır.

        Rotasyonun temel taşı: bir hesabı Max rollerinden çıkarıp (ör. gece
        Agent Mode'a vermek için) tamamen serbest bırakır. `upstream_curl_N`,
        çerez/token alanları, keepalive görevi, HTTP/2 havuzu ve hat kaydı
        temizlenir; sırada bekleyen istekler çalışan hatta devredilir.
        """
        if slot <= 1:
            raise ValueError(
                "1. ana Max yuvası bırakılamaz; yalnızca yeni cURL ile güncellenebilir."
            )
        if not 2 <= slot <= MAX_ACCOUNT_SLOTS:
            raise ValueError(f"Yuva 2..{MAX_ACCOUNT_SLOTS} arasında olmalıdır.")
        async with self._lock:
            current: Settings = self.app.state.settings
            upstream = self.app.state.upstream
            accounts: AccountPool = self.app.state.accounts
            lanes: LaneSet = self.app.state.lanes

            acc_target = Account.from_env(current, slot)
            lane2 = lanes.get(slot)
            was_active = bool(
                acc_target.is_configured
                or lane2 is not None
                or slot in upstream._account_clients
                or slot in self._keepalive_tasks
            )

            # 1) Keepalive görevini derhal iptal et ve bitmesini bekle
            keepalive_stopped = await self._cancel_keepalive(slot)

            # 2) Hattı dağıtıcıdan çıkar; sırada bekleyen istek varsa ana hatta aktar
            old_label2 = lane2.label if lane2 is not None else acc_target.label
            reassigned_waiters = 0
            if lane2 is not None:
                lanes.lanes = [ln for ln in lanes.lanes if ln.slot != slot]
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

            # 3) Hesabı havuzdan ve kota penceresinden sil
            accounts.accounts = [a for a in accounts.accounts if a.slot != slot]
            accounts._states.pop(slot, None)

            # 4) Hesabın özel HTTP/2 bağlantı havuzunu + çerez kavanozunu kapat
            client2 = upstream._account_clients.pop(slot, None)
            upstream._account_errors.pop(slot, None)
            http_client_closed = client2 is not None
            if client2 is not None:
                await client2.aclose()

            # 5) Settings üzerindeki tüm `*_2` kimlik alanlarını sıfırla ve yay
            cleared_updates: dict[str, Any] = {_curl_field(slot): ""}
            for attr in _preserve_attrs(slot):
                default = Settings.model_fields[attr].default
                cleared_updates[attr] = (
                    default if isinstance(default, (str, bool, list)) else ""
                )
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
                "upstream_slot_released",
                slot=slot,
                was_active=was_active,
                http_client_closed=http_client_closed,
                keepalive_stopped=keepalive_stopped,
                reassigned_waiters=reassigned_waiters,
                evicted_sessions=evicted_sessions,
            )

            snap = self.status_snapshot()
            return {
                "closed_slot": slot,
                "was_active": was_active,
                "http_client_closed": http_client_closed,
                "keepalive_stopped": keepalive_stopped,
                "reassigned_waiters": reassigned_waiters,
                "evicted_sessions": evicted_sessions,
                "active_slots": snap["active_slots"],
                "multi_lane": snap["multi_lane"],
            }
