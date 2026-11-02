"""Erişim token'ının refresh token ile yenilenmesi (ağ çıkmadan)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

from app.services.token_refresh import (
    describe_refresh,
    record_refresh,
    refresh_if_needed,
    reset_refresh_state,
)
from app.upstream.auth import extract_refresh_token
from app.upstream.refresh import (
    derive_refresh_url,
    needs_refresh,
    refresh_upstream_token,
    rewrite_session_cookie,
)
from tests.conftest import TEST_API_KEY, make_settings

SECRET_REFRESH = "l6lb6efhioff"


def _jwt(*, exp: float, iss: str | None = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    claims: dict[str, Any] = {"exp": int(exp), "sub": "user-1"}
    if iss:
        claims["iss"] = iss
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def session_cookie(*, expires_in: int = 3600, chunks: int = 2, iss: str | None = None) -> str:
    """Gerçek biçime benzeyen parçalı `base64-` oturum çerezi."""
    payload = {
        "access_token": _jwt(exp=time.time() + expires_in, iss=iss),
        "refresh_token": SECRET_REFRESH,
        "expires_in": expires_in,
        "token_type": "bearer",
    }
    value = "base64-" + base64.b64encode(json.dumps(payload).encode()).decode()
    size = len(value) // chunks + 1
    parts = [
        f"arena-auth-prod-v1.{index}={value[index * size : (index + 1) * size]}"
        for index in range(chunks)
    ]
    parts.append("__cf_bm=abc")
    return "; ".join(parts)


def _settings(**overrides: Any):
    values: dict[str, Any] = {
        "upstream_cookie": session_cookie(),
        "target_domain": "upstream.test",
        "upstream_auth_from_cookie": True,
        "upstream_refresh_url": "https://auth.test/auth/v1/token?grant_type=refresh_token",
    }
    values.update(overrides)
    return make_settings(**values)


def test_needs_refresh_only_when_close_to_expiry():
    assert needs_refresh(_settings(upstream_cookie=session_cookie(expires_in=3600))) is False
    assert needs_refresh(_settings(upstream_cookie=session_cookie(expires_in=120))) is True


def test_needs_refresh_is_false_without_refresh_token():
    cookie = "arena-auth-prod-v1=" + _jwt(exp=time.time() + 60)
    assert needs_refresh(_settings(upstream_cookie=cookie)) is False


def test_refresh_url_derived_from_jwt_issuer():
    settings = _settings(
        upstream_cookie=session_cookie(iss="https://proj.supabase.co/auth/v1"),
        upstream_refresh_url="",
    )
    url = derive_refresh_url(settings)
    assert url == "https://proj.supabase.co/auth/v1/token?grant_type=refresh_token"


@pytest.mark.asyncio
async def test_refresh_updates_tokens_and_rewrites_chunked_cookie():
    settings = _settings()
    old_cookie = settings.upstream_cookie
    new_access = _jwt(exp=time.time() + 3600, iss="https://proj.supabase.co/auth/v1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["grant_type"] == "refresh_token"
        assert json.loads(request.content)["refresh_token"] == SECRET_REFRESH
        return httpx.Response(
            200,
            json={
                "access_token": new_access,
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "token_type": "bearer",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await refresh_upstream_token(settings, client)

    assert outcome.ok is True
    assert settings.upstream_access_token == new_access
    assert settings.upstream_refresh_token == "rotated-refresh"
    assert settings.upstream_cookie != old_cookie
    # Parçalı yapı korunur ve yeni değerler çerezden okunabilir.
    assert "arena-auth-prod-v1.0=" in settings.upstream_cookie
    assert "arena-auth-prod-v1.1=" in settings.upstream_cookie
    assert extract_refresh_token(settings.upstream_cookie) == "rotated-refresh"
    assert new_access not in json.dumps({"d": outcome.detail})


@pytest.mark.asyncio
async def test_refresh_failure_keeps_existing_token():
    settings = _settings(upstream_access_token="old-token")
    cookie_before = settings.upstream_cookie

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error_description": "invalid refresh token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await refresh_upstream_token(settings, client)

    assert outcome.ok is False
    assert "taze çerez" in outcome.detail
    assert settings.upstream_access_token == "old-token"
    assert settings.upstream_cookie == cookie_before


@pytest.mark.asyncio
async def test_refresh_missing_apikey_is_explained():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"message": "No API key found in request"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await refresh_upstream_token(settings, client)

    assert outcome.ok is False
    assert "UPSTREAM_REFRESH_API_KEY" in outcome.detail


@pytest.mark.asyncio
async def test_refresh_sends_apikey_header_when_configured():
    settings = _settings(upstream_refresh_api_key="anon-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "anon-key"
        return httpx.Response(200, json={"access_token": _jwt(exp=time.time() + 3600)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await refresh_upstream_token(settings, client)

    assert outcome.ok is True


def test_rewrite_ignores_cookies_without_session_json():
    cookie = "__cf_bm=abc; _ga=GA1.1.1"
    rewritten, changed = rewrite_session_cookie(cookie, access_token="x")
    assert changed is False
    assert rewritten == cookie


@pytest.mark.asyncio
async def test_refresh_if_needed_skips_when_token_is_fresh():
    reset_refresh_state()
    settings = _settings()
    outcome = await refresh_if_needed(settings)
    assert outcome is None


@pytest.mark.asyncio
async def test_refresh_if_needed_honours_disabled_switch():
    settings = _settings(upstream_refresh_enabled=False)
    assert await refresh_if_needed(settings) is None


def test_describe_refresh_reports_state_without_secrets():
    reset_refresh_state()
    record_refresh(ok=False, detail="refresh token geçersiz")
    text = describe_refresh()
    assert "yenilenemedi" in text
    record_refresh(ok=True)
    assert "yenilendi" in describe_refresh()


def _client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(settings))


def test_admin_refresh_endpoint_reports_when_refresh_token_missing():
    settings = _settings(
        upstream_cookie="__cf_bm=abc",  # refresh token yok
    )
    with _client(settings) as client:
        response = client.post(
            "/v1/admin/upstream/refresh",
            json={},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert "refresh token yok" in body["detail"]


def test_admin_refresh_endpoint_requires_api_key():
    with _client(_settings()) as client:
        response = client.post("/v1/admin/upstream/refresh", json={})
    assert response.status_code == 401
