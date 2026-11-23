"""Hesap rotasyon motoru: *hangi hesap* sorusuna deterministik cevap.

Neden ayrı bir modül?
---------------------
Dağıtıcı (`app/services/lanes.py`) isteği **yük dengesine** göre böler: "o an en
az işi olan hat". Bu günlük trafikte doğru ve âdil; ama iki farklı ihtiyacı
karşılamıyor:

1. **Yük değil, sıra istiyorum.** "Aynı metni 40 dk önce 1. Max yazdıysa şimdi
   2. Max yazsın." Yük dengesi bunu garanti etmez: 1. hesap boşaldıysa yine o
   seçilebilir.
2. **Aynı şekli tekrar istiyorum.** Aynı isteğin tekrarı farklı hesaba düşerse
   sunucu tarafındaki davranış (kota penceresi, sohbet bağlılığı) değişebilir.

Bu modül bunun için `LaneSet.choose`'ın önüne bir **sıra kuralı** koyar. Kritik
sınır: burada yalnız "hangi hesap" seçilir. İsteğin gövdesi, başlıkları,
`create-evaluation` biçimi, akış protokolü **hiç değişmez** — upstream'e giden
bayt dizisi aynıdır (bkz. `app/upstream/client.py`, `app/adapters/*`).

Kurallar (öncelik sırası)
-------------------------
``pinned``      İstek belirli bir yuvaya sabitlendi (`x-apiwrapper-account`,
                keepalive probe'u, elle sınama). Rotasyon devre dışı.
``agent``       Kaynak bir Otopilot/Agent işi: **Agent hesabı hiçbir Max
                isteğini almaz**. Roller çakışmasın diye.
``sticky``      Aynı sohbet (`conversation_id`) daha önce bir hesaba düştüyse
                yine o hesaba gider. Aksi halde sohbet iki hesaba bölünür ve her
                iki taraf geçmişin yarısını görür.
``strict``      Yalnız `account_rotation_strict_slots` içindeki yuvalar
                kullanılır (ör. ``[1, 2]``): yedek yuva gündüz trafiğine
                karışmaz, "birinci hesap tamamen bitmesin" kuralı korunur.
``round_robin`` Kaynak+sınıf için **sıra** tutulur: sıradaki uygun yuva seçilir
                (1 → 2 → 1 …). Aynı metin ikinci kez gelirse öbür hesap yazar.
``weighted``    `account_rotation_weights` verildiyse sıra yerine ağırlık
                kullanılır (ör. 2:1 → ana hesap iki katı iş alır).
``least_loaded`` Hiçbiri uygulanamazsa dağıtıcının kendi yük dengesine dönülür.

User-Agent / cURL biçimi, çerez düzeni, gövde alanları bu modülden **hiç**
geçmez; bu yüzden upstream istek formatı değişmez.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - yalnız tip denetimi
    from app.services.lanes import Lane

logger = get_logger(__name__)

#: Aynı içeriği tanımak için kullanılan ön ek (sıra anahtarında görünür).
LOGICAL_KEY_PREFIX = "msg:"


def logical_message_key(*, messages: list[Any] | None, model: str | None = None) -> str:
    """İsteğin **içerik parmak izi** (hesaptan bağımsız).

    Aynı metin ikinci kez gönderildiğinde aynı anahtar üretilir; rotasyon bu
    sayede "bu mesajı en son kim yazdı?" sorusunu sorabilir. Yalnız özet
    (hash) tutulur: mesaj metni loglanmaz, diske yazılmaz.
    """
    digest = hashlib.sha256()
    if model:
        digest.update(str(model).encode("utf-8"))
        digest.update(b"\x00")
    for message in messages or []:
        role = ""
        content = ""
        if isinstance(message, dict):
            role = str(message.get("role") or "")
            content = message.get("content") or ""
        else:
            role = str(getattr(message, "role", "") or "")
            content = getattr(message, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)
        digest.update(role.encode("utf-8"))
        digest.update(b"\x01")
        digest.update(content.encode("utf-8"))
        digest.update(b"\x02")
    return LOGICAL_KEY_PREFIX + digest.hexdigest()[:24]


@dataclass(slots=True)
class RotationDecision:
    """Rotasyonun tek bir istek için verdiği karar (loglanabilir, secret içermez)."""

    applied: bool = False
    #: Uygulanan kural: pinned | agent | sticky | strict | round_robin |
    #: weighted | least_loaded | excluded | disabled | single_lane
    rule: str = "disabled"
    #: Seçilen yuva (``None`` = seçilemedi, dağıtıcıya bırakıldı).
    slot: int | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "rule": self.rule,
            "slot": self.slot,
            "reason": self.reason,
        }


@dataclass(slots=True)
class AccountRotator:
    """Yuva sırası tutan, durumsuz rotasyon motoru (gizli veri tutmaz)."""

    #: True ise dağıtıcının yük dengesi yerine bu modül karar verir.
    enabled: bool = False
    #: "round_robin" | "weighted" | "least_loaded"
    mode: str = "round_robin"
    #: Rotasyona katılan yuvalar (boş = tüm yapılandırılmış yuvalar).
    strict_slots: tuple[int, ...] = ()
    #: Hiçbir Max isteği almayacak yuvalar (ör. gece Agent hesabı).
    excluded_slots: tuple[int, ...] = ()
    #: ``{yuva: ağırlık}``; yalnız ``mode="weighted"`` iken kullanılır.
    weights: dict[int, int] = field(default_factory=dict)
    #: Gece Agent Mode projesinin kullandığı yuva (varsa). Bu yuva **hiçbir
    #: Max isteği almaz**: iki mod aynı hesabı paylaşırsa oturumlar birbirini
    #: düşürür (`SameAccountError` bunu ayrıca yakalar).
    agent_slot: int | None = None
    #: Global sıra sayacı. Anahtar başına sayaç **yanlış** olurdu: her yeni mesaj
    #: kendi sırasının başında (hep 1. yuva) başlar, rotasyon hiç dönmezdi.
    _cursor_global: int = 0
    #: Anahtar → son hizmet veren yuva (aynı metni öbür hesaba yönlendirmek için).
    #: (aşağıda `_last_slot`)
    _last_slot: dict[str, int] = field(default_factory=dict)
    #: Bellek tavanı: çok sayıda farklı sohbet anahtarı birikmesin.
    max_keys: int = 2048

    # ------------------------------------------------------------------ kurulum
    @classmethod
    def from_settings(cls, settings: Any) -> AccountRotator:
        """`Settings`'ten yapılandırma okur; hatalı değer rotasyonu **kapatmaz**.

        Sessizce yanlış çalışmak yerine loglanır ve güvenli varsayılana dönülür:
        rotasyon açık ama yuva listesi bozuksa tüm yuvalar kullanılır.
        """
        mode = str(getattr(settings, "account_rotation_mode", "round_robin") or "").strip()
        if mode not in {"round_robin", "weighted", "least_loaded"}:
            logger.warning("account_rotation_mode_invalid", mode=mode)
            mode = "round_robin"
        strict = tuple(int(s) for s in getattr(settings, "account_rotation_strict_slots", []) or ())
        excluded = tuple(
            int(s) for s in getattr(settings, "account_rotation_excluded_slots", []) or ()
        )
        raw_weights = str(getattr(settings, "account_rotation_weights", "") or "")
        weights: dict[int, int] = {}
        for chunk in raw_weights.replace(" ", "").split(","):
            if not chunk:
                continue
            slot_txt, _, weight_txt = chunk.partition(":")
            try:
                weights[int(slot_txt)] = max(1, int(weight_txt or 1))
            except ValueError:
                logger.warning("account_rotation_weight_invalid", chunk=chunk)
        return cls(
            enabled=bool(getattr(settings, "account_rotation_enabled", False)),
            mode=mode,
            strict_slots=strict,
            excluded_slots=excluded,
            weights=weights,
            agent_slot=(
                int(getattr(settings, "account_rotation_agent_slot", 0) or 0) or None
            ),
        )

    def describe(self) -> dict[str, Any]:
        """`/v1/admin/accounts` ve Telegram için okunur özet."""
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "strict_slots": list(self.strict_slots),
            "excluded_slots": list(self.excluded_slots),
            "weights": dict(self.weights),
            "agent_slot": self.agent_slot,
            "tracked_keys": len(self._last_slot),
            "sequence": self._cursor_global,
        }

    # ------------------------------------------------------------------ karar
    def _eligible_slots(
        self, lanes: list[Lane], allowed_slots: set[int] | None = None
    ) -> list[int]:
        """Rotasyona girebilecek yuvalar.

        Üç süzgeç üst üste biner: **rol** (yalnız `max` yuvalar gündüz trafiğine
        girer), `ACCOUNT_ROTATION_STRICT_SLOTS` ve `ACCOUNT_ROTATION_EXCLUDED_SLOTS`.
        Rol süzgeci `allowed_slots` ile canlı gelir: `/rol 2 yedek` yazıldığı an
        rotasyon 2. yuvayı aday listesinden çıkarır (yeniden başlatma gerekmez).
        """
        slots = [lane.slot for lane in lanes if lane.account is not None]
        if allowed_slots is not None:
            slots = [slot for slot in slots if slot in allowed_slots]
        if self.strict_slots:
            slots = [slot for slot in slots if slot in self.strict_slots]
        if self.excluded_slots:
            slots = [slot for slot in slots if slot not in self.excluded_slots]
        return sorted(set(slots))

    def _remember(self, key: str, slot: int) -> None:
        self._last_slot[key] = slot
        if len(self._last_slot) > self.max_keys:
            # En eski kaydı at (deterministik: dict sırası ekleme sırasıdır).
            for stale in list(self._last_slot)[: len(self._last_slot) - self.max_keys]:
                self._last_slot.pop(stale, None)

    def choose_slot(
        self,
        lanes: list[Lane],
        *,
        source: str = "api",
        pinned_slot: int | None = None,
        sticky_slot: int | None = None,
        message_key: str | None = None,
        agent_slot: int | None = None,
        allowed_slots: set[int] | None = None,
    ) -> RotationDecision:
        """Sıradaki isteğin gideceği yuvayı seçer (yalnız karar, yan etki yok)."""
        if pinned_slot is not None:
            return RotationDecision(
                applied=False, rule="pinned", slot=pinned_slot, reason="istek yuvaya sabitlendi"
            )
        if len(lanes) <= 1:
            return RotationDecision(applied=False, rule="single_lane")
        if not self.enabled:
            return RotationDecision(applied=False, rule="disabled")

        # Agent işi Max hesabına düşmez: roller birbirini düşürmesin.
        agent_excluded = source == "agent"

        eligible = self._eligible_slots(lanes, allowed_slots)
        if agent_excluded and agent_slot is not None:
            eligible = [slot for slot in eligible if slot != agent_slot]
        if not eligible:
            return RotationDecision(
                applied=False,
                rule="excluded",
                reason="rotasyon için uygun yuva kalmadı (dışlamalar çok geniş)",
            )

        # Aynı sohbet aynı hesapta kalır: geçmişin yarısını iki hesap görmesin.
        if sticky_slot is not None and sticky_slot in eligible:
            return RotationDecision(
                applied=True,
                rule="sticky",
                slot=sticky_slot,
                reason="sohbet daha önce bu hesaba bağlandı",
            )

        key = f"{source}:{message_key or '-'}"
        if self.mode == "least_loaded":
            return RotationDecision(applied=False, rule="least_loaded")

        if self.mode == "weighted" and self.weights:
            slot = self._weighted_pick(eligible, key)
        else:
            slot = self._round_robin_pick(eligible, key)
        if slot is None:
            return RotationDecision(applied=False, rule="least_loaded")
        return RotationDecision(
            applied=True,
            rule=self.mode if self.mode == "weighted" else "round_robin",
            slot=slot,
            reason="sıra: bu istek sıradaki hesaba verildi",
        )

    def _round_robin_pick(self, eligible: list[int], key: str) -> int | None:
        """Sırayla seç (**tek global sıra**) + aynı mesajda öbür hesaba geç.

        Neden global sıra: isteklerin çoğu farklı mesajlardır. Sıra anahtar
        başına tutulsaydı her yeni mesaj kendi sırasının başında (hep 1. yuva)
        başlar ve pratikte rotasyon hiç dönmezdi. Global sıra şunu garanti eder:
        art arda gelen istekler 1 → 2 → 1 → 2 gider.

        Ek kural (kullanıcının örneği): **aynı içerik** ikinci kez gelirse önceki
        hesapla aynı yuva seçilmez → "bu metni 1. Max yazdıysa şimdi 2. yazsın".
        """
        if not eligible:
            return None
        slot = eligible[self._cursor_global % len(eligible)]
        previous = self._last_slot.get(key)
        if previous is not None and slot == previous and len(eligible) > 1:
            slot = eligible[(self._cursor_global + 1) % len(eligible)]
        self._cursor_global += 1
        return slot

    def _weighted_pick(self, eligible: list[int], key: str) -> int | None:
        """Ağırlıklı seçim: `{1: 2, 2: 1}` → ana hesap iki katı iş alır.

        Sıra tabanlıdır (rastgele değil): aynı girdi dizisi aynı dağılımı verir,
        bu da davranışı test edilebilir kılar. Sıra globaldir (yukarıdaki gerekçe).
        """
        weights = [(slot, self.weights.get(slot, 1)) for slot in eligible]
        total = sum(weight for _, weight in weights)
        if total <= 0:
            return self._round_robin_pick(eligible, key)

        def pick(cursor: int) -> int:
            acc = 0
            for candidate, weight in weights:
                acc += weight
                if cursor % total < acc:
                    return candidate
            return eligible[-1]

        slot = pick(self._cursor_global)
        previous = self._last_slot.get(key)
        if previous is not None and slot == previous and len(eligible) > 1:
            slot = pick(self._cursor_global + 1)
        self._cursor_global += 1
        return slot

    # -------------------------------------------------************* gözlem
    def record(self, *, message_key: str | None, slot: int, source: str = "api") -> None:
        """Hizmet veren yuvayı kaydeder (aynı mesajın tekrarında kullanılır)."""
        if message_key:
            self._remember(f"{source}:{message_key}", slot)

    def forget_key(self, message_key: str | None, source: str = "api") -> None:
        """Kaydı temizler (test ve elle sıfırlama için)."""
        if not message_key:
            return
        key = f"{source}:{message_key}"
        self._last_slot.pop(key, None)

    def reset(self) -> None:
        self._cursor_global = 0
        self._last_slot.clear()
