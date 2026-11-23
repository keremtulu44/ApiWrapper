"""Hesap hatları: istekleri hesaplar arasında **eşit bölen** dağıtıcı.

Her hesabın kendi *hattı* vardır: kendi sırası (`RequestQueue` — aynı anda bir
istek, istekler arası rastgele bekleme) ve kendi sağlık kaydı (`UpstreamHealth`).
Gelen istek upstream'e gitmeden önce bir hatta atanır ve o hesabın işi olur;
hatlar birbirini beklemez. Tek hesapta (ya da hesap havuzu yokken) tek hat vardır
ve davranış tek sıralı kurulumla aynıdır.

Bölüşüm kuralı
    İstek, o an **en az işi olan** hatta gider (işlenen + bekleyen); eşitlikte
    **en uzun süredir iş almamış** hatta. Tek tek gelen isteklerde bu sırayla
    değişim demektir (1, 2, 1, 2 …): günlük trafik hesaplar arasında yarı yarıya
    bölünür. Aynı anda gelen dört istek ikişer ikişer ayrılır.

Arıza kuralı (`ACCOUNT_FAILOVER`, varsayılan açık)
    Bu bir "asıl hesap + yedek hesap" düzeni **değildir**: iki hesap da sürekli
    çalışır. Ama biri arıza verirse o hat yeni iş almaz, işler çalışan hatta
    gider; karşı sistem bunu fark etmez. Arızalı hat **kendi keepalive denemesi**
    başarılı olunca geri döner. Gerçek istekle yeniden deneme yalnız iki durumda
    yapılır: çalışan hat kalmadıysa (tek hesaptaki yarı-açık deneme) ya da
    keepalive kapalıysa (`retry_failing_after` saniyede bir).

    Kapalıyken (`ACCOUNT_FAILOVER=false`) bölüşüm katıdır: hatlar sağlığa
    bakılmadan sırayla seçilir, arızalı hattın payı hata/"problem" alır.

Sohbet bağlılığı
    Sohbet yeniden kullanılıyorsa (`SESSION_REUSE` / `conversation_id`) upstream
    sohbeti bir hesaba aittir. Aynı sohbetin istekleri, o hat kullanılabilir
    oldukça aynı hatta gider; yoksa konuşma iki hesaba bölünür ve her biri
    geçmişin yarısını görürdü.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.services.account import Account, AccountPool
from app.services.alerting import human_duration
from app.services.request_queue import RequestQueue
from app.services.upstream_health import Hold, UpstreamHealth

logger = get_logger(__name__)

#: Keepalive kapalıyken arızalı bir hattın gerçek istekle yeniden denenme aralığı
#: (saniye). Keepalive açıkken kullanılmaz: hattı kendi probe'u dener.
RETRY_FAILING_WITHOUT_PROBE_SECONDS = 1800.0

#: Servise gelen bir isteği belirli bir hesaba sabitleyen başlık (değer: yuva
#: numarası). Keepalive her hesabı kendi probe'uyla dener; operatör de tek bir
#: hesabı elle sınayabilir. Yalnız tam yetkili uçlarda (`/v1/chat/completions`)
#: dikkate alınır; `/v1/ask` yok sayar.
ACCOUNT_HEADER = "x-apiwrapper-account"


@dataclass(slots=True)
class Lane:
    """Bir hesabın hattı: sırası, sağlık kaydı ve aldığı iş sayısı."""

    index: int
    #: Hesap havuzu yokken (kimlik tek tek ayarlardan okunuyorsa) ``None``.
    account: Account | None
    queue: RequestQueue
    health: UpstreamHealth
    #: Bölüşümle bu hatta atanan istek sayısı (taşınanlar dahil; hesaba
    #: sabitlenen probe ve elle sınama istekleri hariç).
    assigned: int = 0
    #: Son atamanın sıra numarası; eşitlikte en küçüğü (en eski) seçilir.
    last_assigned: int = 0

    @property
    def slot(self) -> int:
        return self.account.slot if self.account is not None else 0

    @property
    def label(self) -> str:
        return self.account.label if self.account is not None else ""


class LaneUnavailable(Exception):
    """Hiçbir hat isteği şu an kabul etmiyor: arıza biliniyor, bekletme sürüyor."""

    def __init__(self, hold: Hold, lane: Lane | None = None) -> None:
        super().__init__(f"no upstream account available ({hold.code})")
        self.hold = hold
        self.lane = lane


class LaneSet:
    """Hatların tamamı ve bir isteğin hangi hatta gideceği kararı."""

    def __init__(
        self,
        lanes: Iterable[Lane],
        *,
        failover: bool = True,
        cooldown: Callable[[int], float] | None = None,
        retry_failing_after: float = 0.0,
        sticky_size: int = 1024,
        rotation: Any | None = None,
        roles: Any | None = None,
    ) -> None:
        self.lanes: list[Lane] = list(lanes)
        if not self.lanes:
            raise ValueError("LaneSet en az bir hat ister")
        self.failover = bool(failover)
        #: Yuva → hesabın kalan kota dinlenmesi (saniye). Dinlenen hesaba istek
        #: göndermek kilidi uzatır; o hat kullanılabilir sayılmaz.
        self._cooldown = cooldown
        self.retry_failing_after = max(0.0, float(retry_failing_after))
        self._sequence = 0
        self._sticky: OrderedDict[str, int] = OrderedDict()
        self._sticky_size = max(1, int(sticky_size))
        #: Hesap rotasyon motoru (`app/services/account_rotation.py`). `None` ise
        #: davranış **tam olarak** eski yük dengesidir; rotasyon yalnız açıkken
        #: devreye girer (varsayılan kapalı).
        self.rotation = rotation
        #: Yuva rolleri (`app/services/slot_roles.py`). `None`/kapalıyken her yuva
        #: eşittir (eski davranış). Roller hesabı **kapatmaz**: yalnız o yuvanın
        #: gündüz `max` trafiği alıp almayacağını belirler.
        self.roles = roles
        #: Son rotasyon kararı (gözlemlenebilirlik; secret içermez).
        self.last_decision: dict[str, Any] = {}

    @classmethod
    def from_settings(cls, settings: Settings, accounts: AccountPool) -> LaneSet:
        """Yapılandırılmış her hesap için bir hat kurar (hesap yoksa tek adsız hat)."""
        configured = [account for account in accounts.accounts if account.is_configured]
        hold = settings.ask_problem_hold_seconds
        lanes: list[Lane]
        if len(configured) <= 1:
            # Tek hat: sıra adsızdır, metrik ve log adları tek sıralı kurulumla aynı kalır.
            lanes = [
                Lane(
                    0,
                    configured[0] if configured else None,
                    RequestQueue.from_settings(settings),
                    UpstreamHealth(hold),
                )
            ]
        else:
            lanes = [
                Lane(
                    index,
                    account,
                    RequestQueue.from_settings(settings, name=account.label),
                    UpstreamHealth(hold),
                )
                for index, account in enumerate(configured)
            ]
        probes = settings.upstream_keepalive_interval_seconds > 0
        # Rotasyon motoru yalnız açıksa kurulur; kapalıyken `None` kalır ve
        # `choose` tamamen eski yük dengesiyle çalışır (davranış değişmez).
        rotation = None
        if getattr(settings, "account_rotation_enabled", False):
            from app.services.account_rotation import AccountRotator

            rotation = AccountRotator.from_settings(settings)
        from app.services.slot_roles import SlotRoles

        roles = SlotRoles.from_settings(settings)
        return cls(
            lanes,
            failover=settings.account_failover,
            cooldown=accounts.cooldown_remaining,
            retry_failing_after=0.0 if probes else RETRY_FAILING_WITHOUT_PROBE_SECONDS,
            rotation=rotation,
            roles=roles,
        )

    # ------------------------------------------------------------- durum
    @property
    def primary(self) -> Lane:
        return self.lanes[0]

    @property
    def multi(self) -> bool:
        return len(self.lanes) > 1

    def __len__(self) -> int:
        return len(self.lanes)

    def get(self, slot: int) -> Lane | None:
        return next((lane for lane in self.lanes if lane.slot == slot), None)

    def scope(self, lane: Lane | None) -> str:
        """Bildirimlerde hattı ayırt eden ad; tek hatta boştur (mesajlar değişmez)."""
        return lane.label if lane is not None and self.multi else ""

    def cooling(self, lane: Lane) -> float:
        """Hesabın kalan kota dinlenmesi (saniye)."""
        if self._cooldown is None or lane.account is None:
            return 0.0
        return max(0.0, float(self._cooldown(lane.slot)))

    def _healthy(self, lane: Lane) -> bool:
        """Hesabın kendisi iş alabilir mi (rolüne bakmadan): arıza yok, kota beklemiyor."""
        return not lane.health.failing and self.cooling(lane) <= 0

    def promoted_spares(self) -> list[Lane]:
        """Devreye alınmış **yedek** yuvalar: hiçbir `max` yuvası çalışmıyorsa.

        Kural: yedek normalde trafik almaz (birincil hesaplar tükenmesin), ama
        iki `max` hesabı da arızalıysa/uykudaysa **otomatik devreye girer**. Böylece
        "ikinci hesap bitti" durumunda istek hata dönmez, üçüncü hesap işi üstlenir
        — hiçbir hesap kapatılmadan.
        """
        roles = self.roles
        if roles is None or not roles.enabled:
            return []
        max_lanes = [lane for lane in self.lanes if roles.is_max(lane.slot)]
        if any(self._healthy(lane) for lane in max_lanes):
            return []
        return [
            lane
            for lane in self.lanes
            if roles.role_of(lane.slot) == "spare" and self._healthy(lane)
        ]

    def usable(self, lane: Lane) -> bool:
        """Hat yeni iş alabilir mi?

        Rol kuralı:
        * ``max``   → arızası yoksa ve beklemiyorsa iş alır.
        * ``spare`` → **normalde almaz**; yalnız hiçbir ``max`` yuvası çalışmıyorsa
          devreye girer (bkz. `promoted_spares`).
        * ``agent`` → gündüz `max` trafiği almaz (o pencere Agent projesinin).
        """
        roles = self.roles
        if roles is None or not roles.enabled:
            return self._healthy(lane)
        role = roles.role_of(lane.slot)
        if role == "max":
            return self._healthy(lane)
        if role == "spare":
            return any(candidate is lane for candidate in self.promoted_spares())
        return False

    def others_usable(self, lane: Lane) -> bool:
        """Bu hattın dışında iş alabilecek bir hat var mı?"""
        return any(self.usable(other) for other in self.lanes if other is not lane)

    def _retry_due(self, lane: Lane) -> bool:
        """Probe yokken: arızalı hat gerçek istekle yeniden denenecek kadar bekledi mi?"""
        if self.retry_failing_after <= 0:
            return False
        since = lane.health.since_last_failure()
        return since is not None and since >= self.retry_failing_after

    # -------------------------------------------------------------- seçim
    def choose(
        self,
        owner: object,
        *,
        respect_hold: bool = False,
        pinned_slot: int | None = None,
        sticky_key: str | None = None,
        rotation_source: str = "api",
        rotation_message_key: str | None = None,
    ) -> Lane:
        """İsteğin gideceği hattı seçer ve atamayı kaydeder.

        ``respect_hold`` (`/v1/ask`): arızası bilinen ve bekletmesi süren hatta
        gidilmez; hiçbir hat kabul etmiyorsa `LaneUnavailable` fırlar ve çağıran
        beklemeden "problem" der. Diğer istekler (sohbet uçları) her zaman denenir.

        ``pinned_slot``: istek o hesaba sabitlenir, sağlığa bakılmaz (probe,
        elle sınama). Bilinmeyen yuva `LookupError` fırlatır. Sabitlenen istek
        bölüşümün parçası değildir: ne sayaca ne sıraya dokunur (probe atıldı diye
        bir sonraki gerçek istek öbür hesaba kaymaz).
        """
        if pinned_slot is not None:
            pinned = self.get(pinned_slot)
            if pinned is None:
                raise LookupError(pinned_slot)
            self.last_decision = {
                "applied": False,
                "rule": "pinned",
                "slot": pinned_slot,
                "reason": "istek yuvaya sabitlendi",
            }
            return pinned

        lanes = self.lanes
        rotation_slot = self._rotation_slot(
            rotation_source,
            sticky_key=sticky_key,
            sticky_slot=self._sticky.get(sticky_key, None) if sticky_key else None,
            message_key=rotation_message_key,
        )
        if not self.failover and len(lanes) > 1:
            # Katı bölüşüm: sağlığa bakılmaz, her hat yalnız kendi payını işler.
            # Atama, istek geri çevrilse de sayılır: arızalı hat kendi payını
            # "harcar", sıradaki istek öbür hatta gider (yoksa hepsi ona yığılırdı).
            lane = self._assign(
                self._pick(lanes, sticky_key, preferred_slot=rotation_slot), sticky_key
            )
            if respect_hold:
                hold = lane.health.admit(owner)
                if hold is not None:
                    raise LaneUnavailable(hold, lane)
            return lane

        usable = [lane for lane in lanes if self.usable(lane)]
        if not respect_hold:
            candidates = usable + [
                lane for lane in lanes if lane not in usable and self._retry_due(lane)
            ]
            return self._assign(
                self._pick(candidates or lanes, sticky_key, preferred_slot=rotation_slot),
                sticky_key,
            )

        # Arızalı hatlardan denemeye açık olanlar: bekletmesi dolmuş, denemesi
        # sürmüyor, hesabı dinlenmiyor. Çalışan hat varken yalnız "probe yok ve
        # yeterince beklendi" ise aday olurlar; yoksa hattı kendi probe'u dener.
        trial = [
            lane
            for lane in lanes
            if lane not in usable
            and self.cooling(lane) <= 0
            and lane.health.peek(owner) is None
            and (not usable or self._retry_due(lane))
        ]
        candidates = usable + trial
        if not candidates:
            hold, blocked = self._soonest_hold(owner)
            raise LaneUnavailable(hold, blocked)
        lane = self._pick(candidates, sticky_key, preferred_slot=rotation_slot)
        if lane.health.failing:
            lane.health.admit(owner)  # deneme hakkını al: aynı anda tek deneme
        return self._assign(lane, sticky_key)

    def alternative(self, tried: Iterable[int]) -> Lane | None:
        """İşi yarıda kalan isteğin taşınabileceği **çalışan** hat (yoksa ``None``).

        Yalnız kullanılabilir hatlar adaydır: istek bir arızalı hattan diğerine
        taşınmaz. Katı bölüşümde taşıma yapılmaz.
        """
        if not self.failover or len(self.lanes) < 2:
            return None
        skip = set(tried)
        candidates = [
            lane for lane in self.lanes if lane.slot not in skip and self.usable(lane)
        ]
        if not candidates:
            return None
        return self._assign(self._pick(candidates, None), None)

    def _rotation_slot(
        self,
        source: str,
        *,
        sticky_key: str | None,
        sticky_slot: int | None,
        message_key: str | None,
    ) -> int | None:
        """Rotasyon motorundan yuvayı ister; motor yoksa/kapalıysa ``None``.

        Yalnız **hangi hesap** sorusunu yanıtlar; isteğin gövdesine/başlığına
        dokunmaz. Sağlık ve kota kontrolleri yine dağıtıcıda kalır: rotasyon
        arızalı bir hesabı zorla seçmez, uygun değilse yük dengesine döner.
        """
        rotator = self.rotation
        if rotator is None:
            return None
        # Agent koşusunun kendi yuvası (varsa) Max isteklerinden dışlanır.
        agent_slot = getattr(rotator, "agent_slot", None) if source == "agent" else None
        # Rol süzgeci **canlı**: `/rol 2 yedek` yazıldığı an 2. yuva rotasyondan
        # çıkar; rol ile rotasyon birbirini ezmez (tek doğru kaynak: rol tablosu).
        allowed = None
        if self.roles is not None and self.roles.enabled:
            allowed = {lane.slot for lane in self.lanes if self.roles.is_max(lane.slot)}
        decision = rotator.choose_slot(
            self.lanes,
            source=source,
            sticky_slot=sticky_slot,
            message_key=message_key,
            agent_slot=agent_slot,
            allowed_slots=allowed,
        )
        self.last_decision = decision.as_dict()
        return decision.slot if decision.applied else None

    def _pick(
        self,
        candidates: list[Lane],
        sticky_key: str | None,
        *,
        preferred_slot: int | None = None,
    ) -> Lane:
        # Rotasyonun seçtiği yuva adaylar arasında varsa ona gidilir; yoksa
        # (arızalı/dinleniyor) aşağıdaki yük dengesi devreye girer.
        if preferred_slot is not None:
            for lane in candidates:
                if lane.slot == preferred_slot:
                    return lane
        if sticky_key:
            slot = self._sticky.get(sticky_key)
            if slot is not None:
                for lane in candidates:
                    if lane.slot == slot:
                        return lane
        return min(candidates, key=lambda lane: (lane.queue.load, lane.last_assigned, lane.index))

    def _assign(self, lane: Lane, sticky_key: str | None) -> Lane:
        self._sequence += 1
        lane.last_assigned = self._sequence
        lane.assigned += 1
        if sticky_key:
            self._sticky[sticky_key] = lane.slot
            self._sticky.move_to_end(sticky_key)
            while len(self._sticky) > self._sticky_size:
                self._sticky.popitem(last=False)
        if self.multi:
            metrics.inc("apiwrapper_account_assigned_total", labels={"account": lane.label})
        return lane

    def _soonest_hold(self, owner: object) -> tuple[Hold, Lane]:
        """Hiçbir hat kabul etmiyorken: en erken açılacak hattın bekletmesi."""
        best: tuple[Hold, Lane] | None = None
        for lane in self.lanes:
            hold = lane.health.peek(owner)
            cooling = self.cooling(lane)
            if hold is None or (not hold.trial_in_flight and cooling > hold.remaining):
                # Bekletme dolmuş ama hesap hâlâ dinleniyor: kalan süre dinlenmedir.
                since = lane.health.since_last_failure()
                hold = Hold(
                    lane.health.code or "upstream_quota_reached",
                    cooling,
                    hold.failing_for if hold is not None else (since or 0.0),
                )
            if best is None or hold.remaining < best[0].remaining:
                best = (hold, lane)
        assert best is not None  # en az bir hat var
        return best

    # ------------------------------------------------------- operatör notları
    def outlook(self, lane: Lane) -> str | None:
        """Çok hesaplı kurulumda "bu arızadan sonra ne olacak" satırı (tek hatta ``None``)."""
        if not self.multi:
            return None
        hold = lane.health.blocked()
        held = (
            f"dış isteklere {human_duration(hold.remaining + 0.999)} boyunca "
            'upstream\'e gidilmeden "problem" denir; sonra tek istek denenir.'
            if hold is not None
            else None
        )
        name = lane.label
        if not self.failover:
            if held is None:
                return None
            return (
                f"Bundan sonra: {name} hesabının payına düşen {held} Diğer hesap kendi "
                "payını işlemeye devam eder (ACCOUNT_FAILOVER kapalı)."
            )
        if self.others_usable(lane):
            if self.retry_failing_after > 0:
                back = (
                    f"{human_duration(self.retry_failing_after)} sonra gerçek bir istekle "
                    "yeniden denenir (keepalive kapalı)"
                )
            else:
                back = "kendi keepalive denemesi başarılı olunca geri döner"
            return (
                "Bundan sonra: yeni istekler çalışan hesaptan cevaplanır, karşı sistem "
                f"etkilenmez. {name} yeni iş almaz; {back}."
            )
        return f"Bundan sonra: çalışan hesap kalmadı; {held}" if held else None

    def incident_notes(self, lane: Lane, *, critical: bool) -> tuple[str, ...]:
        """Çok hesaplı kurulumda arıza mesajına eklenen satırlar (tek hatta boş).

        ``critical``: arıza elle müdahale gerektiriyor (ör. oturum düştü). O
        durumda operatöre hangi değişkenin tazeleneceği ve yeniden deploy'un
        **çalışan hesabı da** sıfırlayacağı hatırlatılır.
        """
        if not self.multi:
            return ()
        notes: list[str] = []
        if critical:
            variable = "UPSTREAM_CURL" if lane.slot <= 1 else f"UPSTREAM_CURL_{lane.slot}"
            waiting = (
                " Acelesi yok: servis çalışan hesapla sürüyor."
                if self.failover and self.others_usable(lane)
                else ""
            )
            notes.append(
                f"Not: bu hesabın cURL'ü {variable} değişkenindedir. Yeniden deploy bütün "
                "hesapların oturumunu sıfırlar; deploy edecekseniz hepsinin cURL'ünü "
                f"birlikte tazeleyin.{waiting}"
            )
        notes.append(self.describe())
        # Arıza hangi role düştü? Operatör "bu hesap sıcak yedekti, gündüz trafiği
        # almıyordu" bilgisini mesajda görsün (yoksa gereksiz panik olur).
        role = self.role_of(lane)
        if role != "max":
            notes.append(
                f"Rol: bu hesap `{role}` — gündüz `max` trafiğine girmiyordu; "
                "arıza gündüz isteklerini etkilemez."
            )
        return tuple(notes)

    # ------------------------------------------------------- gözlemlenebilirlik
    def role_of(self, lane: Lane) -> str:
        """Hattın rolü (`max` / `spare` / `agent`). Rol sistemi kapalıysa `max`."""
        if self.roles is None:
            return "max"
        return self.roles.role_of(lane.slot)

    def describe(self) -> str:
        """Hesapların tek satırlık durumu (operatör bildirimleri için)."""
        parts: list[str] = []
        for lane in self.lanes:
            role = self.role_of(lane)
            if lane.health.failing:
                health = f"ARIZALI [{lane.health.code or 'bilinmiyor'}]"
            elif self.cooling(lane) > 0:
                health = "dinleniyor (kota)"
            else:
                health = "sağlıklı"
            if self.usable(lane):
                state = "çalışıyor"
            elif role == "spare" and not lane.health.failing:
                # Sağlıklı ama rol gereği trafik almıyor: "kota" demek yanıltıcı.
                state = "beklemede (rol gereği trafik almıyor)"
            elif role == "agent" and not lane.health.failing:
                state = "kullanımda (Agent projesi; max trafiği almaz)"
            else:
                state = health
            name = lane.label or "hesap"
            if role == "max":
                parts.append(f"{name} {state}")
            elif role == "agent":
                parts.append(f"{name} [agent] {state}")
            else:
                parts.append(f"{name} [yedek] {state}")
        return "Hesaplar: " + " · ".join(parts)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "account": lane.label or None,
                "slot": lane.slot or None,
                "usable": self.usable(lane),
                "role": self.role_of(lane),
                "keepalive": (
                    self.roles.keepalive_allowed(lane.slot)
                    if self.roles is not None
                    else True
                ),
                "assigned_total": lane.assigned,
                "cooldown_remaining_seconds": round(self.cooling(lane), 1),
                "queue": lane.queue.snapshot(),
                "upstream_health": lane.health.snapshot(),
            }
            for lane in self.lanes
        ]
