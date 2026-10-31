"""Render secret ``UPSTREAM_CURL`` için runtime regresyonları."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.logging import configure_logging
from app.main import create_app
from app.services.account import load_accounts
from app.upstream.curl_runtime import apply_upstream_curl
from tests.conftest import make_settings

MODEL_ID = "019b24bb-5caf-71c3-b854-37d0c7086f21"


def _curl(
    *,
    url: str = "https://upstream.test/nextjs-api/stream/create-evaluation",
    cookie: str | None = "sb-auth-token=test-cookie-secret",
    authorization: str | None = None,
    include_mode: bool = True,
    include_referer: bool = True,
    include_captcha: bool = True,
) -> str:
    headers = []
    if cookie is not None:
        headers.append(f"-H 'cookie: {cookie}'")
    if authorization is not None:
        headers.append(f"-H 'authorization: {authorization}'")
    headers.extend(
        [
            "-H 'user-agent: RuntimeTest/1.0'",
            "-H 'accept-language: tr-TR,tr;q=0.9'",
            "-H 'dnt: 1'",
        ]
    )
    if include_referer:
        headers.append("-H 'referer: https://upstream.test/text/direct'")
    body = {
        "id": "chat-from-curl",
        "modelAId": MODEL_ID,
        "userMessage": "selam",
    }
    if include_mode:
        body["mode"] = "direct-battle"
    if include_captcha:
        body["recaptchaV3Token"] = "short-lived-captcha-token"
    return (
        f"curl '{url}' \\\n  "
        + " \\\n  ".join(headers)
        + " \\\n  --data-raw '"
        + json.dumps(body)
        + "'"
    )


def test_runtime_curl_replaces_upstream_fields_and_preserves_browser_captcha():
    settings = make_settings(
        target_domain="old.test",
        upstream_cookie="old-cookie",
        upstream_access_token="old-token",
        upstream_cookie_2="stale-cookie-2",
        upstream_access_token_3="stale-token-3",
        upstream_account_2_name="eski-hesap",
        upstream_curl=_curl(),
        recaptcha_provider="browser",
        recaptcha_static_token="old-captcha",
    )

    updated, model_id = apply_upstream_curl(settings)

    assert updated is not settings
    assert model_id == MODEL_ID
    assert updated.target_domain == "upstream.test"
    assert updated.upstream_stream_path == "/nextjs-api/stream/create-evaluation"
    assert updated.upstream_mode == "direct-battle"
    assert updated.upstream_cookie == "sb-auth-token=test-cookie-secret"
    assert updated.upstream_access_token == ""
    assert updated.upstream_auth_from_cookie is False
    assert updated.upstream_user_agent == "RuntimeTest/1.0"
    assert updated.upstream_accept_language == "tr-TR,tr;q=0.9"
    assert updated.upstream_referer_path == "/text/direct"
    assert updated.upstream_extra_headers == "dnt=1"
    assert updated.recaptcha_provider == "browser"
    assert updated.recaptcha_static_token == ""
    # cURL dışındaki yerel API ve performans ayarları korunur.
    assert updated.api_keys == settings.api_keys
    assert updated.retry_max_attempts == settings.retry_max_attempts
    assert len(load_accounts(updated)) == 1
    assert updated.upstream_cookie_2 == ""
    assert updated.upstream_access_token_3 == ""
    assert updated.upstream_account_2_name == ""
    assert settings.target_domain == "old.test"  # girdi nesnesi mutate edilmez


def test_runtime_curl_accepts_authorization_only_and_validates_boolean():
    settings = make_settings(
        upstream_curl=_curl(cookie=None, authorization="Bearer test-access-token")
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.upstream_cookie == ""
    assert updated.upstream_access_token == "test-access-token"
    assert updated.upstream_auth_scheme == "Bearer"
    assert updated.upstream_auth_from_cookie is False
    assert type(updated.upstream_auth_from_cookie) is bool


def test_runtime_curl_rejects_telemetry_and_anonymous_requests():
    telemetry = make_settings(
        upstream_curl=_curl(url="https://upstream.test/api/v2/rum")
    )
    with pytest.raises(ValueError, match="telemetri"):
        apply_upstream_curl(telemetry)

    anonymous = make_settings(upstream_curl=_curl(cookie=None))
    with pytest.raises(ValueError, match="cookie veya authorization"):
        apply_upstream_curl(anonymous)


def test_missing_optional_curl_fields_do_not_reuse_old_custom_values():
    settings = make_settings(
        upstream_curl=_curl(
            include_mode=False, include_referer=False, include_captcha=False
        ),
        upstream_mode="old-mode",
        upstream_referer_path="/old-page/{chat_id}",
        upstream_user_agent="OldAgent/1.0",
        recaptcha_provider="static",
        recaptcha_static_token="old-captcha",
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.upstream_mode == ""
    assert updated.upstream_referer_path == "/c/{chat_id}"
    assert updated.upstream_user_agent != "OldAgent/1.0"
    assert updated.recaptcha_static_token == ""


def test_app_lifespan_uses_curl_model_id_for_default_max_model():
    app = create_app(
        make_settings(
            upstream_curl=_curl(),
            recaptcha_provider="noop",
            models_file="config/models.yaml",
        )
    )

    with TestClient(app):
        assert app.state.settings.target_domain == "upstream.test"
        assert app.state.registry.resolve(None).id == "max"
        assert app.state.registry.resolve(None).upstream_id == MODEL_ID


def test_empty_runtime_curl_keeps_legacy_settings_untouched():
    settings = make_settings(upstream_curl="")
    updated, model_id = apply_upstream_curl(settings)
    assert updated is settings
    assert model_id is None


def test_runtime_logs_do_not_contain_raw_curl_secrets(capfd):
    configure_logging("INFO", True)
    apply_upstream_curl(make_settings(upstream_curl=_curl()))
    output = capfd.readouterr().out + capfd.readouterr().err
    assert "test-cookie-secret" not in output
    assert "short-lived-captcha-token" not in output
    assert "sb-auth-token=test" not in output
