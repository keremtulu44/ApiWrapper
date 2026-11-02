"""Harici çözücü servisinden reCAPTCHA v3 token'ı alan sağlayıcı."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import RecaptchaError
from app.core.logging import get_logger
from app.services.recaptcha.base import RecaptchaProvider

logger = get_logger(__name__)


class ExternalRecaptchaProvider(RecaptchaProvider):
    """Yapılandırılabilir bir HTTP uç noktasından token ister.

    Beklenen yanıt biçimleri (ilk eşleşen kullanılır)::

        {"token": "..."}          {"solution": {"gRecaptchaResponse": "..."}}
        {"data": {"token": "..."}}  {"request": "..."}   veya düz metin
    """

    name = "external"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        if not self.settings.recaptcha_external_url:
            raise RecaptchaError(
                "RECAPTCHA_EXTERNAL_URL must be set when using the 'external' provider.",
                code="recaptcha_not_configured",
            )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.recaptcha_external_timeout),
            trust_env=False,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _produce_token(self) -> str:
        if self._client is None:
            await self.startup()
        assert self._client is not None

        payload: dict[str, Any] = {
            "siteKey": self.settings.recaptcha_site_key,
            "pageUrl": self.settings.base_url,
            "action": self.settings.recaptcha_action,
            "version": self.settings.recaptcha_version,
        }
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.settings.recaptcha_external_api_key:
            headers["authorization"] = f"Bearer {self.settings.recaptcha_external_api_key}"
            payload["clientKey"] = self.settings.recaptcha_external_api_key

        try:
            response = await self._client.post(
                self.settings.recaptcha_external_url, json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise RecaptchaError(f"External captcha solver unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise RecaptchaError(
                f"External captcha solver returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        token = self._extract_token(response)
        if not token:
            raise RecaptchaError(
                "External captcha solver response did not contain a token: "
                f"{response.text[:300]}"
            )
        return token

    @staticmethod
    def _extract_token(response: httpx.Response) -> str:
        try:
            data: Any = response.json()
        except ValueError:
            return response.text.strip()

        if isinstance(data, str):
            return data.strip()
        if not isinstance(data, dict):
            return ""

        for key in ("token", "gRecaptchaResponse", "request", "code", "result"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for container in ("solution", "data", "response"):
            nested = data.get(container)
            if isinstance(nested, dict):
                for key in ("token", "gRecaptchaResponse", "text", "request"):
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            elif isinstance(nested, str) and nested.strip():
                return nested.strip()
        return ""
