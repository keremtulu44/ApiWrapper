"""Yuva rolleri: üç hesabın **hiçbiri kapatılmadan** iş bölümü yapması.

Kullanıcı sözleşmesi: üç ayrı hesap var ve **üçü de açık kalır**; kimse cURL
silmez, kimse "kapatılmaz". Değişen tek şey **rol**:

``max``     Gündüz `max` trafiğini alır (rotasyonun üyesi). Keepalive açık.
``spare``   Gündüz trafiği almaz ama **açık ve sıcak** durur: birincil hesap
            tükenirse/kota yerse tek adımda devreye girer. Keepalive açık.
``agent``   Otopilot/Agent Mode projesinin hesabı. `max` trafiği **almaz**,
            böylece iki mod aynı hesabı paylaşıp oturumu düşürmez. Yuvası ve
            cURL'ü yerinde kalır (silmeye gerek yok); yalnız keepalive
            yapılmaz, çünkü o pencere Agent'ın hakkıdır.

Rol bir **anahtar** değildir: cURL, çerez, token, pencere sayacı yerinde kalır.
Değiştirmek tek satırdır (`/rol 3 max`) ve anında etkilidir; upstream isteğinin
biçimine dokunulmaz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

ROLE_MAX = "max"
ROLE_SPARE = "spare"
ROLE_AGENT = "agent"
VALID_ROLES: tuple[str, ...] = (ROLE_MAX, ROLE_SPARE, ROLE_AGENT)

#: Telegram/API'de kabul edilen Türkçe eş adlar.
ROLE_ALIASES: dict[str, str] = {
    "max": ROLE_MAX,
    "asil": ROLE_MAX,
    "ana": ROLE_MAX,
    "spare": ROLE_SPARE,
    "yedek": ROLE_SPARE,
    "bekleme": ROLE_SPARE,
    "agent": ROLE_AGENT,
    "ajan": ROLE_AGENT,
    "otopilot": ROLE_AGENT,
}

ROLE_LABELS: dict[str, str] = {
    ROLE_MAX: "max (gündüz trafiği)",
    ROLE_SPARE: "yedek (açık, sıcak bekliyor)",
    ROLE_AGENT: "agent (Agent projesi)",
}


def normalize_role(value: str | None) -> str | None:
    """Serbest metni role çevirir; tanınmazsa ``None``."""
    key = (value or "").strip().lower()
    return ROLE_ALIASES.get(key)


@dataclass(slots=True)
class SlotRoles:
    """Yuva → rol eşlemesi (varsayılan: herkes ``max``).

    Çalışma zamanında değiştirilebilir: `LaneSet` ve keepalive bu nesneyi canlı
    okur, bu yüzden rol değişikliği anında etkilidir (yeniden başlatma yok).
    """

    #: `{yuva: rol}`; tanımsız yuvalar ``max`` sayılır.
    roles: dict[int, str] = field(default_factory=dict)
    #: Rol sistemi kullanılıyor mu? Yalnız `account_slot_roles` verilirse açılır;
    #: kapalıyken davranış eski (tüm yuvalar eşit) kalır.
    enabled: bool = False

    # ------------------------------------------------------------------ kurulum
    @classmethod
    def from_settings(cls, settings: Any) -> SlotRoles:
        """`ACCOUNT_SLOT_ROLES="1:max,2:max,3:agent"` biçimini okur."""
        raw = str(getattr(settings, "account_slot_roles", "") or "")
        roles: dict[int, str] = {}
        for chunk in raw.replace(" ", "").split(","):
            if not chunk:
                continue
            slot_txt, _, role_txt = chunk.partition(":")
            try:
                slot = int(slot_txt)
            except ValueError:
                logger.warning("slot_role_invalid_slot", chunk=chunk)
                continue
            role = normalize_role(role_txt)
            if role is None:
                logger.warning("slot_role_invalid_role", chunk=chunk)
                continue
            roles[slot] = role
        return cls(roles=roles, enabled=bool(roles))

    # ------------------------------------------------------------------ sorgu
    def role_of(self, slot: int | None) -> str:
        """Yuvanın rolü; bilinmeyen yuva ``max`` (eski davranış)."""
        if slot is None:
            return ROLE_MAX
        return self.roles.get(int(slot), ROLE_MAX)

    def is_max(self, slot: int | None) -> bool:
        return self.role_of(slot) == ROLE_MAX

    def keepalive_allowed(self, slot: int | None) -> bool:
        """Bu yuva için `max` keepalive çalışsın mı?

        `agent` yuvasında **hayır**: o pencere Agent projesinin hakkı; ısıtma
        isteği göndermek hem kotayı yer hem aynı hesabı iki modda kullanma
        riskini büyütür. `spare` yuvada **evet**: yedek sıcak durmalı.
        """
        return self.role_of(slot) != ROLE_AGENT

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "roles": {str(slot): role for slot, role in sorted(self.roles.items())},
            "agent_slots": sorted(s for s, r in self.roles.items() if r == ROLE_AGENT),
            "spare_slots": sorted(s for s, r in self.roles.items() if r == ROLE_SPARE),
        }

    # ------------------------------------------------------------------ değiştir
    def set_role(self, slot: int, role: str) -> str:
        """Rolü değiştirir ve yeni rolü döndürür (geçersiz rol → ``ValueError``)."""
        normalized = normalize_role(role)
        if normalized is None:
            raise ValueError(
                f"Geçersiz rol: '{role}'. Geçerli roller: "
                + ", ".join(VALID_ROLES)
                + " (Türkçe: yedek/ajan da olur)."
            )
        if slot < 1:
            raise ValueError("Yuva numarası 1 veya daha büyük olmalıdır.")
        self.roles[int(slot)] = normalized
        self.enabled = True
        logger.info("slot_role_changed", slot=slot, role=normalized)
        return normalized

    def clear(self) -> None:
        self.roles.clear()
        self.enabled = False

    def describe_line(self) -> str:
        """`/hesaplar` için tek satırlık özet."""
        if not self.enabled:
            return "tanımsız — tüm yuvalar `max` (eşit)"
        parts = [
            f"{slot}:{self.roles[slot]}" for slot in sorted(self.roles)
        ]
        return " · ".join(parts)
