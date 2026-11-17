"""reCAPTCHA v3 sağlayıcı arayüzü ve TTL/tek-uçuş sarmalayıcısı."""

from __future__ import annotations

import abc

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
            token = await self._produce_token()
            metrics.inc("apiwrapper_recaptcha_tokens_total", labels={"provider": self.name})
            logger.debug("recaptcha_token_generated", provider=self.name, length=len(token))
            return token

        return await self._cache.get(factory)

    def invalidate(self) -> None:
        """Token upstream tarafından reddedildiğinde çağrılır."""
        self._cache.invalidate()
        logger.info("recaptcha_token_invalidated", provider=self.name)
