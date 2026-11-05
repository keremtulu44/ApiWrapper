"""reCAPTCHA v3 sağlayıcı arayüzü ve TTL/tek-uçuş sarmalayıcısı."""

from __future__ import annotations

import abc
import time

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.utils.cache import SingleFlightValue

logger = get_logger(__name__)


class RecaptchaProvider(abc.ABC):
    """Token üreten sağlayıcıların ortak arayüzü.

    `get_token()` çağrıları TTL cache + tek-uçuş kilidiyle korunur; yüzlerce
    eşzamanlı istek tek token üretimini paylaşır.
    """

    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: SingleFlightValue[str] = SingleFlightValue(
            ttl=max(1.0, settings.recaptcha_token_ttl)
        )
        #: Teşhis için son üretim/red bilgisi. Token'ın kendisi saklanmaz.
        self.stats: dict[str, object] = {
            "generated": 0,
            "rejected": 0,
            "last_generated_at": None,
            "last_rejected_at": None,
            "last_error": None,
        }

    async def startup(self) -> None:
        """Opsiyonel kaynak hazırlığı (alt sınıflar geçersiz kılabilir)."""
        return None

    async def shutdown(self) -> None:
        """Opsiyonel kaynak temizliği (alt sınıflar geçersiz kılabilir)."""
        return None

    @abc.abstractmethod
    async def _produce_token(self) -> str:
        """Yeni bir token üretir. Alt sınıflar uygular."""

    async def get_token(self) -> str:
        async def factory() -> str:
            try:
                token = await self._produce_token()
            except Exception as exc:
                self.stats["last_error"] = str(exc)[:300]
                raise
            self.stats["generated"] = int(str(self.stats["generated"])) + 1
            self.stats["last_generated_at"] = time.time()
            self.stats["last_error"] = None
            metrics.inc("apiwrapper_recaptcha_tokens_total", labels={"provider": self.name})
            logger.debug("recaptcha_token_generated", provider=self.name, length=len(token))
            return token

        return await self._cache.get(factory)

    def invalidate(self) -> None:
        """Token upstream tarafından reddedildiğinde çağrılır."""
        self._cache.invalidate()
        self.stats["rejected"] = int(str(self.stats["rejected"])) + 1
        self.stats["last_rejected_at"] = time.time()
        logger.info("recaptcha_token_invalidated", provider=self.name)
