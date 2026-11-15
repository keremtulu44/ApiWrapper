"""Statik token sağlayıcısı (varsayılan) ve noop sağlayıcı."""

from __future__ import annotations

from app.core.errors import RecaptchaError
from app.core.logging import get_logger
from app.services.recaptcha.base import RecaptchaProvider

logger = get_logger(__name__)


class StaticRecaptchaProvider(RecaptchaProvider):
    """`.env` içindeki `RECAPTCHA_STATIC_TOKEN` değerini döndürür.

    Token gerçek bir tarayıcı oturumundan alınır; ölçümde **2+ saat** geçerli
    kaldı ve hedef token yaşını denetlemiyor (`docs/LESSONS_2026_11_14.md` §2).
    Reddedilmenin sebebi yaş değil, token'ın yanlış/eksik olması ya da gövdedeki
    alan adının (`UPSTREAM_RECAPTCHA_FIELD`) tarayıcıyla eşleşmemesidir.
    """

    name = "static"

    async def startup(self) -> None:
        if not self.settings.recaptcha_static_token:
            logger.warning(
                "recaptcha_static_token_missing",
                hint="Set RECAPTCHA_STATIC_TOKEN or switch RECAPTCHA_PROVIDER.",
            )

    async def _produce_token(self) -> str:
        token = self.settings.recaptcha_static_token.strip()
        if not token:
            raise RecaptchaError(
                "RECAPTCHA_STATIC_TOKEN is not configured. Set it in the environment "
                "or switch RECAPTCHA_PROVIDER to 'noop'/'browser'/'external'.",
                code="recaptcha_not_configured",
            )
        return token

    async def get_token(self) -> str:
        # Statik token cache'lemeye gerek yok; ayar anında değişebilir.
        return await self._produce_token()


class NoopRecaptchaProvider(RecaptchaProvider):
    """CAPTCHA alanını hiç göndermez (hedef doğrulamıyorsa veya testlerde)."""

    name = "noop"

    async def _produce_token(self) -> str:
        return ""

    async def get_token(self) -> str:
        return ""

    def invalidate(self) -> None:
        """Token yok; "red" sayacı artmamalı — reddedilecek bir şey yoktu."""
        return None
