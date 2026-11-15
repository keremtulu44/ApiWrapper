"""Env'deki "boş değer" yer tutucularının token sanılmaması.

Canlı belirti (2026-11-13): `UPSTREAM_ACCESS_TOKEN=APIWRAPPER_EMPTY_VALUE_V1`
olan bir kurulumda çerez taze olmasına rağmen her istek 401 dönüyordu. Sebep:
`resolve_bearer_token` açık token'ı çerezden **önce** tercih eder, dolayısıyla
upstream'e `Authorization: Bearer APIWRAPPER_EMPTY_VALUE_V1` gidiyordu.
"""

from __future__ import annotations

from app.core.config import Settings
from app.services.recaptcha.static import StaticRecaptchaProvider
from app.upstream.headers import build_authorization
from tests.conftest import make_settings


def _cookie() -> str:
    """Tek parça base64 oturum çerezi (JWT'li)."""
    import base64
    import json

    head = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode()).decode().rstrip("=")
    jwt = f"{head}.{body}.sig"
    payload = {"access_token": jwt, "refresh_token": "rt-1", "expires_in": 1200}
    value = "base64-" + base64.b64encode(json.dumps(payload).encode()).decode()
    return f"arena-auth-prod-v1={value}"


def test_placeholder_access_token_is_blanked_and_cookie_token_is_used():
    settings = make_settings(
        upstream_cookie=_cookie(),
        upstream_access_token="APIWRAPPER_EMPTY_VALUE_V1",
        upstream_auth_from_cookie=True,
    )

    assert settings.upstream_access_token == ""
    authorization = build_authorization(settings)
    assert authorization is not None
    assert authorization.startswith("Bearer eyJ")
    assert "EMPTY_VALUE" not in authorization


def test_underscored_placeholder_access_token_is_blanked():
    settings = make_settings(upstream_access_token="__APIWRAPPER_EMPTY_VALUE_V1__")
    assert settings.upstream_access_token == ""


def test_placeholder_refresh_and_api_key_are_blanked():
    settings = make_settings(
        upstream_refresh_token="APIWRAPPER_EMPTY_VALUE_V1",
        upstream_refresh_api_key="__APIWRAPPER_EMPTY_VALUE_V1__",
        upstream_cx="APIWRAPPER_EMPTY_VALUE_V1",
    )
    assert settings.upstream_refresh_token == ""
    assert settings.upstream_refresh_api_key == ""
    assert settings.upstream_cx == ""


def test_real_values_are_never_blanked():
    settings = make_settings(
        upstream_access_token="eyJhbGciOiJFUzI1NiJ9.abc.def",
        upstream_refresh_token="4axwzzz4rxvx",
        upstream_refresh_api_key="sb_publishable_abcdefghijklmnop",
    )
    assert settings.upstream_access_token.startswith("eyJ")
    assert settings.upstream_refresh_token == "4axwzzz4rxvx"
    assert settings.upstream_refresh_api_key.startswith("sb_publishable_")


def test_placeholder_static_token_produces_clear_configuration_error():
    """Yer tutucu boşaltılınca `static` sağlayıcı anlaşılır hata verir."""
    import asyncio

    settings = Settings(
        recaptcha_provider="static",
        recaptcha_static_token="APIWRAPPER_EMPTY_VALUE_V1",
        auth_disabled=True,
    )
    assert settings.recaptcha_static_token == ""

    provider = StaticRecaptchaProvider(settings)
    try:
        asyncio.run(provider.get_token())
    except Exception as exc:  # noqa: BLE001 - hata metni doğrulanıyor
        assert "recaptcha_not_configured" in str(getattr(exc, "code", ""))
    else:  # pragma: no cover
        raise AssertionError("boş statik token hata üretmeliydi")
