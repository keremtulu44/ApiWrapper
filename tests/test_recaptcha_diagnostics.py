"""`/tani` CAPTCHA üretim geçmişini göstermeli.

"Token üretilemiyor" ile "token üretiliyor ama hedef reddediyor" arasındaki
fark, operatörün bakacağı yeri belirler. Önceden /tani yalnız sağlayıcı ve
site key'i gösteriyordu; token hiç üretilmemiş olsa bile çıktı aynıydı.
"""

from __future__ import annotations

import contextlib

from app.services.recaptcha.base import RecaptchaProvider


class _Counting(RecaptchaProvider):
    name = "counting"

    async def _produce_token(self) -> str:
        return "tok"


class _Failing(RecaptchaProvider):
    name = "failing"

    async def _produce_token(self) -> str:
        raise RuntimeError("chrome crashed")


def _provider(cls, settings) -> RecaptchaProvider:
    return cls(settings)


async def test_stats_start_empty(settings) -> None:
    provider = _provider(_Counting, settings)
    assert provider.stats["generated"] == 0
    assert provider.stats["rejected"] == 0
    assert provider.stats["last_error"] is None


async def test_generation_is_counted_once_per_produced_token(settings) -> None:
    provider = _provider(_Counting, settings)
    await provider.get_token()
    await provider.get_token()  # cache'ten gelir, yeni üretim değil
    assert provider.stats["generated"] == 1
    provider.invalidate()
    await provider.get_token()
    assert provider.stats["generated"] == 2
    assert provider.stats["rejected"] == 1


async def test_production_error_is_recorded(settings) -> None:
    provider = _provider(_Failing, settings)
    with contextlib.suppress(RuntimeError):
        await provider.get_token()
    assert provider.stats["last_error"] == "chrome crashed"
    assert provider.stats["generated"] == 0
