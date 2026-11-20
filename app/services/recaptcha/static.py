"""Statik token sağlayıcısı (varsayılan) ve noop sağlayıcı."""

from __future__ import annotations

from app.core.errors import RecaptchaError
from app.core.logging import get_logger
from app.services.recaptcha.base import RecaptchaProvider

logger = get_logger(__name__)


class StaticRecaptchaProvider(RecaptchaProvider):
    """`RECAPTCHA_STATIC_TOKEN` değerini döndürür (`UPSTREAM_CURL` gövdesinden okunur).

    Token sabit bir dizedir ve bu hedefte **saatlerce** kabul edilir; "token
    ~2 dakikada ölür" bilgisi ölçümle çürütüldü (docs/LESSONS §2, §15.3). Hedef
    onu reddederse neden token değildir: red geçicidir, yapılacak şey beklemektir.
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
                "RECAPTCHA_STATIC_TOKEN is not configured. It is read from the body of "
                "the captured browser request: update UPSTREAM_CURL (or set the variable). "
                "Use RECAPTCHA_PROVIDER=noop only if the upstream does not check captcha.",
                code="recaptcha_not_configured",
            )
        return token

    async def get_token(self) -> str:
        # Statik token cache'lemeye gerek yok; ayar anında değişebilir.
        return await self._produce_token()


class NoopRecaptchaProvider(RecaptchaProvider):
    """Boş token gönderir (upstream doğrulamıyorsa veya testlerde)."""

    name = "noop"

    async def _produce_token(self) -> str:
        return ""

    async def get_token(self) -> str:
        return ""
