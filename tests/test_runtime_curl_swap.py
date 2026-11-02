"""Oturum düştüğünde taze cURL'ün çalışan servise uygulanması (deploy'suz)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.account import Account, AccountPool, load_accounts
from app.upstream.curl_runtime import apply_curl_to_settings
from tests.conftest import TEST_API_KEY, make_settings

SECRET_COOKIE = "sb-auth-token=very-secret-session-value"
SECRET_JWT = "eyJhbGciOiJIUzI1NiJ9.secret-payload.signature"


def fresh_curl(
    *,
    url: str = "https://upstream.test/nextjs-api/stream/create-evaluation",
    cookie: str = SECRET_COOKIE,
) -> str:
    """Gerçek cURL'e benzeyen, testlerde kullanılan bir örnek."""
    body = {
        "id": "chat-1",
        "modelAId": "019b24bb-5caf-71c3-b854-37d0c7086f21",
        "userMessage": "selam",
        "mode": "direct-battle",
    }
    return (
        f"curl '{url}' \\\n"
        f"  -H 'cookie: {cookie}' \\\n"
        "  -H 'user-agent: RuntimeTest/1.0' \\\n"
        "  -H 'referer: https://upstream.test/text/direct' \\\n"
        f"  --data-raw '{json.dumps(body)}'"
    )


# ------------------------------------------------------------ ayarlara uygulama
def test_apply_curl_to_settings_updates_fields_in_place_and_forgets_raw_curl():
    settings = make_settings(upstream_cookie="old=1", upstream_access_token="")
    result = apply_curl_to_settings(settings, fresh_curl())

    assert settings.upstream_cookie == SECRET_COOKIE
    assert settings.upstream_referer_path == "/text/direct"
    assert settings.upstream_mode == "direct-battle"
    assert "upstream_cookie" in result["fields"]
    # Ham cURL bellekte tutulmaz.
    assert settings.upstream_curl == ""
    assert SECRET_COOKIE not in json.dumps(result["fields"])


def test_apply_curl_refuses_requests_without_credentials():
    settings = make_settings(upstream_cookie="old=1")
    anonymous = "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' --data-raw '{}'"
    with pytest.raises(ValueError):
        apply_curl_to_settings(settings, anonymous)
    # Reddedilen cURL mevcut oturumu bozmamalı.
    assert settings.upstream_cookie == "old=1"


# --------------------------------------------------------------- hesap havuzu
def test_account_pool_reloads_credentials_in_place():
    settings = make_settings(upstream_cookie="old=1")
    pool = AccountPool(settings, accounts=[Account(slot=1, name="hesap-1", cookie="old=1")])

    settings.upstream_cookie = SECRET_COOKIE
    changed = pool.reload_credentials(settings)

    assert pool.accounts[0].cookie == SECRET_COOKIE
    assert "cookie" in changed["hesap-1"]


def test_reload_keeps_existing_captcha_token_when_curl_has_none():
    settings = make_settings(upstream_cookie="old=1", recaptcha_static_token="static-token")
    account = load_accounts(settings)[0]
    assert account.recaptcha_token == "static-token"

    settings.recaptcha_static_token = ""
    pool = AccountPool(settings, accounts=[account])
    pool.reload_credentials(settings)
    assert pool.accounts[0].recaptcha_token == "static-token", "boş değer eskisini silmemeli"


# ------------------------------------------------------------------ admin ucu
def test_admin_endpoint_applies_fresh_curl_without_redeploy():
    settings = make_settings(upstream_cookie="old=1")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/upstream/curl",
            json={"curl": fresh_curl()},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "applied"
        assert body["accounts_updated"]
        # Gizli değerler yanıta sızmaz.
        assert SECRET_COOKIE not in response.text
        # Canlı ayarlar gerçekten değişti (servis nesnesi aynı).
        assert app.state.settings.upstream_cookie == SECRET_COOKIE
        assert app.state.accounts.accounts[0].cookie == SECRET_COOKIE


def test_admin_endpoint_rejects_garbage_curl():
    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.post(
            "/v1/admin/upstream/curl",
            json={"curl": "bu bir curl degil ama yeterince uzun bir metin"},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_admin_endpoint_requires_api_key():
    app = create_app(make_settings())
    with TestClient(app) as client:
        response = client.post("/v1/admin/upstream/curl", json={"curl": fresh_curl()})
        assert response.status_code == 401


# --------------------------------------------------------------- telegram /curl
@pytest.mark.asyncio
async def test_telegram_curl_command_posts_to_admin_and_confirms():
    from app.telegram.bot import TelegramBot, TelegramSettings

    sent: list[dict[str, Any]] = []
    wrapper_calls: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        wrapper_calls.append(
            {
                "path": request.url.path,
                "auth": request.headers.get("authorization"),
                "json": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "fields": ["upstream_cookie", "upstream_referer_path"],
                "accounts_updated": {"hesap-1": ["cookie"]},
                "upstream_model": "019b24bb-5caf-71c3-b854-37d0c7086f21",
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            headers={"Authorization": "Bearer test-wrapper-key"},
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(
            TelegramSettings(
                _env_file=None,
                telegram_bot_token="test-bot-token",
                wrapper_api_key="test-wrapper-key",
                wrapper_base_url="https://wrapper.test",
                telegram_allowed_chat_ids="42",
            ),
            telegram,
            wrapper,
        )
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "/curl " + fresh_curl()}
        )

    assert wrapper_calls[0]["path"] == "/v1/admin/upstream/curl"
    assert wrapper_calls[0]["auth"] == "Bearer test-wrapper-key"
    assert "curl" in wrapper_calls[0]["json"]
    text = sent[-1]["text"]
    assert "cURL uygulandı" in text
    assert "hesap-1" in text
    assert SECRET_COOKIE not in text, "cURL/çerez yanıta yansımamalı"


@pytest.mark.asyncio
async def test_telegram_curl_without_argument_shows_usage():
    from app.telegram.bot import TelegramBot, TelegramSettings

    sent: list[dict[str, Any]] = []
    called = {"wrapper": 0}

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(_request: httpx.Request) -> httpx.Response:
        called["wrapper"] += 1
        return httpx.Response(200, json={})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(
            TelegramSettings(
                _env_file=None,
                telegram_bot_token="test-bot-token",
                wrapper_api_key="test-wrapper-key",
                wrapper_base_url="https://wrapper.test",
                telegram_allowed_chat_ids="42",
            ),
            telegram,
            wrapper,
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl"})

    assert called["wrapper"] == 0
    assert "Kullanım" in sent[-1]["text"]
