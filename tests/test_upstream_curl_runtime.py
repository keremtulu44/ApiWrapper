"""Render secret ``UPSTREAM_CURL`` (ve ``UPSTREAM_CURL_2..4``) için runtime regresyonları."""

from __future__ import annotations

import base64
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
    user_agent: str = "RuntimeTest/1.0",
    language: str = "tr-TR,tr;q=0.9",
    referer: str = "https://upstream.test/text/direct",
    captcha: str = "short-lived-captcha-token",
    mode: str = "direct-battle",
) -> str:
    headers = []
    if cookie is not None:
        headers.append(f"-H 'cookie: {cookie}'")
    if authorization is not None:
        headers.append(f"-H 'authorization: {authorization}'")
    headers.extend(
        [
            f"-H 'user-agent: {user_agent}'",
            f"-H 'accept-language: {language}'",
            "-H 'dnt: 1'",
        ]
    )
    if include_referer:
        headers.append(f"-H 'referer: {referer}'")
    body = {
        "id": "chat-from-curl",
        "modelAId": MODEL_ID,
        "userMessage": "selam",
    }
    if include_mode:
        body["mode"] = mode
    if include_captcha:
        body["recaptchaV3Token"] = captcha
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


# ------------------------------------------------- ikinci hesap: UPSTREAM_CURL_2
def _jwt(subject: str, nonce: str = "a") -> str:
    """Yalnız `sub` alanı anlamlı olan, imzası sahte bir JWT (hesap kimliği için)."""

    def part(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (part({"alg": "HS256", "typ": "JWT"}), part({"sub": subject, "n": nonce}), "c2lnbmF0dXJl")
    )


def _second_curl(**overrides) -> str:
    defaults = {
        "cookie": "sb-auth-token=second-cookie-secret",
        "user_agent": "SecondBrowser/2.0",
        "language": "en-US,en;q=0.8",
        "referer": "https://upstream.test/c/0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee",
        "captcha": "second-captcha-token",
    }
    defaults.update(overrides)
    return _curl(**defaults)


def test_second_curl_adds_a_second_account_next_to_the_first():
    settings = make_settings(
        upstream_curl=_curl(),
        upstream_curl_2=_second_curl(),
        recaptcha_provider="static",
    )

    updated, model_id = apply_upstream_curl(settings)
    accounts = load_accounts(updated)

    assert model_id == MODEL_ID
    assert [account.slot for account in accounts] == [1, 2]
    assert [account.label for account in accounts] == ["hesap-1", "hesap-2"]
    first, second = accounts
    # Her hesap kendi cURL'ünün kimliğini taşır.
    assert first.cookie == "sb-auth-token=test-cookie-secret"
    assert first.recaptcha_token == "short-lived-captcha-token"
    assert second.cookie == "sb-auth-token=second-cookie-secret"
    assert second.access_token == ""
    assert second.auth_from_cookie is False
    assert second.user_agent == "SecondBrowser/2.0"
    assert second.accept_language == "en-US,en;q=0.8"
    assert second.referer_path == "/c/{chat_id}"
    assert second.recaptcha_token == "second-captcha-token"
    assert second.target_domain == "upstream.test"
    # Ortak alanlar 1. hesabın cURL'ünden gelir; 2. cURL onları değiştirmez.
    assert updated.upstream_stream_path == "/nextjs-api/stream/create-evaluation"
    assert updated.upstream_mode == "direct-battle"
    assert updated.upstream_user_agent == "RuntimeTest/1.0"
    assert updated.upstream_referer_path == "/text/direct"
    # İstek atılırken ikinci hesap kendi başlıklarını kullanır.
    effective = second.effective_settings(updated)
    assert effective.upstream_user_agent == "SecondBrowser/2.0"
    assert effective.upstream_cookie == "sb-auth-token=second-cookie-secret"
    assert effective.recaptcha_static_token == "second-captcha-token"


def test_second_curl_with_authorization_header_and_custom_name():
    settings = make_settings(
        upstream_curl=_curl(),
        upstream_curl_2=_second_curl(cookie=None, authorization="Bearer second-access-token"),
        upstream_account_2_name="yedek-mail",
        upstream_cookie_2="stale-cookie-from-old-env",
    )

    updated, _ = apply_upstream_curl(settings)
    second = load_accounts(updated)[1]

    assert second.label == "yedek-mail"
    assert second.access_token == "second-access-token"
    assert second.auth_scheme == "Bearer"
    # Yuva, cURL'ün tam anlık görüntüsüdür: env'de kalmış eski çerez taşınmaz.
    assert second.cookie == ""
    assert type(updated.upstream_auth_from_cookie_2) is bool


def test_only_slots_with_their_own_curl_survive():
    """cURL'ü verilmeyen eski yuvalar yine etkisizleştirilir; verilenler korunur."""
    settings = make_settings(
        upstream_curl=_curl(),
        upstream_curl_3=_second_curl(cookie="sb-auth-token=third-secret"),
        upstream_cookie_2="stale-cookie-2",
        upstream_account_2_name="eski-hesap",
    )

    updated, _ = apply_upstream_curl(settings)

    assert [account.slot for account in load_accounts(updated)] == [1, 3]
    assert updated.upstream_cookie_2 == ""
    assert updated.upstream_account_2_name == ""


def test_dynamic_captcha_provider_does_not_store_the_second_static_token():
    settings = make_settings(
        upstream_curl=_curl(), upstream_curl_2=_second_curl(), recaptcha_provider="browser"
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.recaptcha_provider == "browser"
    assert updated.recaptcha_static_token_2 == ""


@pytest.mark.parametrize(
    ("second", "message"),
    [
        (_curl(url="https://upstream.test/api/v2/rum"), "UPSTREAM_CURL_2 bir telemetri"),
        (_curl(cookie=None), "UPSTREAM_CURL_2 içinde cookie veya authorization"),
        ("curl --data-raw '{}'", "UPSTREAM_CURL_2 içinden hedef URL"),
    ],
)
def test_broken_second_curl_stops_startup_and_names_the_variable(second, message):
    """Bozuk ikinci cURL sessizce tek hesaba düşmez: hangi değişkenin bozuk olduğu söylenir."""
    settings = make_settings(upstream_curl=_curl(), upstream_curl_2=second)
    with pytest.raises(ValueError, match=message):
        apply_upstream_curl(settings)


def test_the_same_account_pasted_twice_is_rejected():
    """Aynı hesap iki yuvaya konursa bölüşüm anlamsızlaşır; token yenilenmiş olsa da yakalanır."""
    first = _curl(cookie=f"sb-auth-token={_jwt('user-1', 'eski')}")
    again = _second_curl(cookie=f"sb-auth-token={_jwt('user-1', 'yeni')}")
    with pytest.raises(ValueError, match="UPSTREAM_CURL_2 ile UPSTREAM_CURL aynı hesaba ait"):
        apply_upstream_curl(make_settings(upstream_curl=first, upstream_curl_2=again))

    # Kimliği çözülemeyen çerezde birebir aynı değer de aynı hesaptır.
    same_cookie = _second_curl(cookie="sb-auth-token=test-cookie-secret")
    with pytest.raises(ValueError, match="aynı hesaba ait"):
        apply_upstream_curl(make_settings(upstream_curl=_curl(), upstream_curl_2=same_cookie))


def test_two_different_accounts_are_accepted():
    first = _curl(cookie=f"sb-auth-token={_jwt('user-1')}")
    other = _second_curl(cookie=f"sb-auth-token={_jwt('user-2')}")

    updated, _ = apply_upstream_curl(make_settings(upstream_curl=first, upstream_curl_2=other))

    assert len(load_accounts(updated)) == 2


def test_second_curl_of_a_different_shape_is_reported(capfd):
    configure_logging("INFO", True)
    odd = _second_curl(
        url="https://other.test/nextjs-api/stream/post-to-evaluation/abc", mode="battle"
    )

    apply_upstream_curl(make_settings(upstream_curl=_curl(), upstream_curl_2=odd))

    output = capfd.readouterr().out
    event = next(
        json.loads(line)
        for line in output.splitlines()
        if '"upstream_curl_slot_mismatch"' in line
    )
    assert event["source"] == "UPSTREAM_CURL_2"
    assert event["fields"] == ["target_domain", "upstream_stream_path", "upstream_mode"]


def test_second_curl_secrets_are_not_logged(capfd):
    configure_logging("INFO", True)
    apply_upstream_curl(make_settings(upstream_curl=_curl(), upstream_curl_2=_second_curl()))
    output = capfd.readouterr().out + capfd.readouterr().err
    assert "second-cookie-secret" not in output
    assert "second-captcha-token" not in output
    assert "sb-auth-token=second" not in output


def test_app_starts_with_two_accounts_from_two_curls():
    app = create_app(
        make_settings(
            upstream_curl=_curl(),
            upstream_curl_2=_second_curl(),
            recaptcha_provider="noop",
            models_file="config/models.yaml",
        )
    )

    with TestClient(app):
        assert app.state.accounts.size == 2
        assert [lane.label for lane in app.state.lanes.lanes] == ["hesap-1", "hesap-2"]
        assert app.state.registry.resolve(None).upstream_id == MODEL_ID
