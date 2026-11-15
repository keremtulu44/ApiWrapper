"""Sohbet oturumu yönetimi: chat_id üretimi, yeniden kullanım ve ısıtma."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.utils.cache import LRUTTLCache
from app.utils.ids import new_chat_id

logger = get_logger(__name__)

Warmup = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class Session:
    """Tek bir upstream sohbet oturumu."""

    chat_id: str
    created_at: float = field(default_factory=time.time)
    warmed: bool = False
    message_count: int = 0

    def touch(self) -> None:
        self.message_count += 1

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.created_at


@dataclass(slots=True)
class _SessionGuard:
    """Bir sohbet anahtarındaki acquire işlemlerini sıraya alan kısa ömürlü kilit."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SessionManager:
    """İstemci → upstream `chat_id` eşlemesini yönetir.

    Üç mod vardır:

    * **Durumsuz** (varsayılan): her istek yeni bir `chat_id` alır.
    * **`conversation_id` ile**: istemci kendi sohbet kimliğini gönderir.
    * **`session_reuse` ile**: OpenAI-uyumlu istemciler şema dışı `conversation_id`
      gönderemediği için anahtar, API anahtarının parmak izi olur. Aynı istemci tek
      upstream sohbetinden ilerler; eşik aşılınca (`session_rotate_after_messages` /
      `session_rotate_after_seconds`) yeni sohbete geçilir.
    """

    def __init__(self, settings: Settings, warmup: Warmup | None = None) -> None:
        self.settings = settings
        self._warmup = warmup
        self._rotations = 0
        self._cache: LRUTTLCache[str, Session] = LRUTTLCache(
            maxsize=settings.session_cache_size, ttl=settings.session_ttl
        )
        # Cache get/set ayrı ayrı kilitlidir; ilk isteğin get → warmup → set
        # birleşimi de aynı anahtar için atomik olmalı. Guard'lar bekleyen kalmayınca
        # silinir, dolayısıyla rastgele conversation_id'ler ikinci bir cache gibi
        # sınırsız bellek büyütmez. Farklı sohbetler birbirini beklemez.
        self._guards: dict[str, _SessionGuard] = {}

    # ------------------------------------------------------------ rotation
    def _rotation_reason(self, session: Session) -> str | None:
        """Sohbet değiştirilmeli mi? Gerekçeyi döndürür, gerek yoksa `None`."""
        limit = self.settings.session_rotate_after_messages
        if limit > 0 and session.message_count >= limit:
            return f"message_count={session.message_count} >= {limit}"
        max_age = self.settings.session_rotate_after_seconds
        if max_age > 0 and session.age() >= max_age:
            return f"age={session.age():.0f}s >= {max_age:.0f}s"
        return None

    @property
    def rotations(self) -> int:
        """Süreç boyunca yapılan sohbet rotasyonu sayısı."""
        return self._rotations

    async def acquire(
        self,
        conversation_id: str | None = None,
        client_identity: str | None = None,
        account_label: str | None = None,
        warmup: Warmup | None = None,
    ) -> Session:
        """İstek için bir oturum döndürür (gerekirse oluşturur/yeniler)."""
        key = self._session_key(conversation_id, client_identity, account_label)
        if key is None:
            # Durumsuz yol: eskisi gibi her istekte yeni sohbet.
            session = Session(chat_id=new_chat_id())
            await self._maybe_warm(session, warmup)
            session.touch()
            return session

        guard = self._guards.get(key)
        if guard is None:
            guard = _SessionGuard()
            self._guards[key] = guard
        guard.users += 1
        try:
            async with guard.lock:
                # Kilidi beklerken önceki istek oturumu oluşturmuş/döndürmüş
                # olabilir; cache kontrolü özellikle kilidin İÇİNDE yapılır.
                cached = await self._cache.get(key)
                if cached is not None:
                    reason = self._rotation_reason(cached)
                    if reason is None:
                        cached.touch()
                        logger.debug(
                            "session_reused", key=key, chat_id=cached.chat_id,
                            message_count=cached.message_count,
                        )
                        return cached
                    logger.info(
                        "session_rotated", key=key, old_chat_id=cached.chat_id,
                        message_count=cached.message_count, reason=reason,
                    )
                    self._rotations += 1
                    metrics.inc("apiwrapper_sessions_rotated_total")
                    await self._cache.delete(key)

                session = Session(chat_id=new_chat_id())
                await self._maybe_warm(session, warmup)
                session.touch()
                await self._cache.set(key, session)
                logger.debug(
                    "session_created", key=key, chat_id=session.chat_id,
                    conversation_id=conversation_id,
                )
                return session
        finally:
            guard.users -= 1
            if guard.users == 0 and self._guards.get(key) is guard:
                self._guards.pop(key, None)

    def _session_key(
        self,
        conversation_id: str | None,
        client_identity: str | None,
        account_label: str | None = None,
    ) -> str | None:
        """Önbellek anahtarını seçer; `None` durumsuz modu ifade eder.

        Öncelik sırası önemlidir: `session_reuse` açıkken `session_stateless` yok
        sayılır; aksi halde `session_stateless=True` her şeyi durumsuza zorlar
        (istemci `conversation_id` gönderse bile).

        Hesap etiketi anahtara her zaman eklenir: upstream sohbeti bir hesaba
        aittir, hesap değişince başka hesabın sohbetine yazmamak gerekir.
        """
        base: str | None
        if self.settings.session_reuse:
            if conversation_id:
                base = f"conv:{conversation_id}"
            elif client_identity:
                base = f"client:{client_identity}"
            else:
                base = None
        elif self.settings.session_stateless:
            base = None
        elif conversation_id:
            base = f"conv:{conversation_id}"
        elif client_identity:
            # conversation_id bekleyen modda istemci kimliği yedek anahtar olur.
            base = f"client:{client_identity}"
        else:
            base = None
        if base is None:
            return None
        return f"{base}@{account_label}" if account_label else base

    async def invalidate(self, conversation_id: str, client_identity: str | None = None) -> None:
        """Verilen anahtarlara bağlı oturumları düşürür (sonraki istek yeni sohbet alır)."""
        await self._cache.delete(conversation_id)
        await self._cache.delete(f"conv:{conversation_id}")
        if client_identity:
            await self._cache.delete(f"client:{client_identity}")

    async def _maybe_warm(
        self, session: Session, warmup: Warmup | None = None
    ) -> None:
        callback = warmup or self._warmup
        if session.warmed or callback is None:
            return
        await callback(session.chat_id)
        session.warmed = True

    async def size(self) -> int:
        return await self._cache.size()

    async def clear(self) -> None:
        await self._cache.clear()
