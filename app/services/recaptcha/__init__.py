"""reCAPTCHA sağlayıcı fabrikası."""

from __future__ import annotations

from app.core.config import Settings
from app.services.recaptcha.base import RecaptchaProvider
from app.services.recaptcha.browser import BrowserRecaptchaProvider
from app.services.recaptcha.external import ExternalRecaptchaProvider
from app.services.recaptcha.static import NoopRecaptchaProvider, StaticRecaptchaProvider

PROVIDERS: dict[str, type[RecaptchaProvider]] = {
    "static": StaticRecaptchaProvider,
    "noop": NoopRecaptchaProvider,
    "browser": BrowserRecaptchaProvider,
    "external": ExternalRecaptchaProvider,
}


def create_recaptcha_provider(settings: Settings) -> RecaptchaProvider:
    """Ayarlarda seçilen sağlayıcıyı örnekler."""
    provider_cls = PROVIDERS.get(settings.recaptcha_provider, StaticRecaptchaProvider)
    return provider_cls(settings)


__all__ = [
    "BrowserRecaptchaProvider",
    "ExternalRecaptchaProvider",
    "NoopRecaptchaProvider",
    "PROVIDERS",
    "RecaptchaProvider",
    "StaticRecaptchaProvider",
    "create_recaptcha_provider",
]
