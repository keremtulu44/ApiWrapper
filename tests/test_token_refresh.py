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

SECRET_REFRESH = "test-refresh-token"


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


def _anon_jwt() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    claims = {"iss": "supabase", "role": "anon", "exp": int(time.time()) + 86400}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{payload}.anon_sig"


def test_extract_anon_key_from_text_prefers_anon_role_jwt_over_user_jwt():
    from app.upstream.refresh import extract_anon_key_from_text

    user_jwt = _jwt(exp=time.time() + 3600)
    anon_jwt = _anon_jwt()
    html = f'<script>const user="{user_jwt}"; const anon="{anon_jwt}";</script>'
    assert extract_anon_key_from_text(html) == anon_jwt


@pytest.mark.asyncio
async def test_refresh_auto_discovers_apikey_from_page_and_retries():
    settings = _settings(upstream_refresh_api_key="")
    anon_jwt = _anon_jwt()
    new_access = _jwt(exp=time.time() + 3600)
    seen_apikeys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=f'<html><script>window.__env={{"anon":"{anon_jwt}"}}</script></html>',
            )
        api_key = request.headers.get("apikey", "")
        seen_apikeys.append(api_key)
        if not api_key:
            return httpx.Response(401, json={"message": "No API key found in request"})
        assert api_key == anon_jwt
        return httpx.Response(
            200,
            json={
                "access_token": new_access,
                "refresh_token": "rotated-after-discovery",
                "expires_in": 3600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await refresh_upstream_token(settings, client)

    assert outcome.ok is True
    assert seen_apikeys == ["", anon_jwt]
    assert settings.upstream_refresh_api_key == anon_jwt
    assert settings.upstream_access_token == new_access
    assert "apikey otomatik keşfedildi" in outcome.notes


@pytest.mark.asyncio
async def test_refresh_propagates_to_account_pool_and_upstream_client():
    from app.services.account import build_account_pool
    from app.upstream.client import UpstreamClient

    settings = _settings(upstream_cookie=session_cookie(expires_in=120))
    accounts = build_account_pool(settings)
    upstream = UpstreamClient(settings)
    await upstream.startup()
    try:
        # Hesap istemcisini önceden ısıt ki eski çerez jar'da bulunsun.
        _ = upstream._client_for(accounts.get(1))
        new_access = _jwt(exp=time.time() + 3600)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": new_access,
                    "refresh_token": "fresh-rotated-token",
                    "expires_in": 3600,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await refresh_if_needed(
                settings, client, accounts=accounts, upstream=upstream
            )

        assert result is not None and result["ok"] is True
        slot1 = accounts.get(1)
        assert slot1 is not None
        assert slot1.access_token == new_access
        assert extract_refresh_token(slot1.cookie) == "fresh-rotated-token"
        assert upstream._account_clients == {}, "eski hesap jar'ları sıfırlanmalı"
    finally:
        await upstream.shutdown()


def test_state_store_persists_rotated_tokens_and_respects_fresh_env(tmp_path):
    from app.upstream.state_store import (
        remember_seed,
        restore_runtime_state,
        save_runtime_state,
    )

    state_file = tmp_path / "upstream_state.json"
    initial_cookie = session_cookie(expires_in=600)

    # 1. Süreç açılır, tohum kaydedilir ve token yenilenip diske yazılır.
    s1 = _settings(
        upstream_cookie=initial_cookie,
        upstream_state_file=str(state_file),
    )
    remember_seed(s1)
    rotated_cookie, _ = rewrite_session_cookie(
        initial_cookie,
        access_token=_jwt(exp=time.time() + 3600),
        refresh_token="rotated-on-disk",
        expires_in=3600,
    )
    s1.upstream_cookie = rotated_cookie
    s1.upstream_refresh_token = "rotated-on-disk"
    assert save_runtime_state(s1, source="test") is True

    # 2. Süreç aynı `.env` (aynı tohum) ile yeniden başlar -> diskteki dönmüş
    # refresh token geri yüklenir.
    s2 = _settings(
        upstream_cookie=initial_cookie,
        upstream_state_file=str(state_file),
    )
    restored = restore_runtime_state(s2)
    assert "upstream_cookie" in restored
    assert s2.upstream_refresh_token == "rotated-on-disk"
    assert extract_refresh_token(s2.upstream_cookie) == "rotated-on-disk"

    # 3. Kullanıcı `.env` içine YEPYENİ (daha ileri `exp` süreli) bir çerez koyar
    # -> yeni `.env` çerezi kazanır, eski disk kaydı onu ezmez.
    brand_new_cookie, _ = rewrite_session_cookie(
        initial_cookie,
        access_token=_jwt(exp=time.time() + 7200),
        refresh_token="user-brand-new",
        expires_in=7200,
    )
    s3 = _settings(
        upstream_cookie=brand_new_cookie,
        upstream_state_file=str(state_file),
    )
    restored_from_new = restore_runtime_state(s3)
    assert restored_from_new == []
    assert extract_refresh_token(s3.upstream_cookie) == "user-brand-new"


def test_state_store_restores_intentionally_cleared_refresh_token(tmp_path):
    from app.upstream.state_store import remember_seed, restore_runtime_state, save_runtime_state

    state_file = tmp_path / "cleared_refresh_state.json"
    initial_cookie = session_cookie()
    source = _settings(
        upstream_cookie=initial_cookie,
        upstream_refresh_token="seed-refresh",
        upstream_state_file=str(state_file),
    )
    remember_seed(source)
    # Yeni oturum artık refresh token taşımıyor; boş değer de anlık görüntünün
    # parçası olarak saklanmalı ve aynı tohumla açılışta geri yüklenmeli.
    source.upstream_cookie = "session=opaque-session"
    source.upstream_refresh_token = ""
    assert save_runtime_state(source, source="test_cleared_refresh") is True

    restarted = _settings(
        upstream_cookie=initial_cookie,
        upstream_refresh_token="seed-refresh",
        upstream_state_file=str(state_file),
    )
    changed = restore_runtime_state(restarted)

    assert restarted.upstream_cookie == "session=opaque-session"
    assert restarted.upstream_refresh_token == ""
    assert "upstream_refresh_token" in changed


def test_state_store_persists_captured_stream_path_and_honors_env_override(tmp_path):
    from app.upstream.state_store import remember_seed, restore_runtime_state, save_runtime_state

    state_file = tmp_path / "captured_state.json"
    original_path = "/old/stream/path"
    captured_path = "/nextjs-api/stream/create-evaluation"
    cookie = session_cookie(expires_in=3600)

    source = _settings(
        upstream_cookie=cookie,
        upstream_state_file=str(state_file),
        upstream_stream_path=original_path,
    )
    remember_seed(source)
    source.upstream_stream_path = captured_path
    assert save_runtime_state(source, source="session_capture") is True

    same_env = _settings(
        upstream_cookie=cookie,
        upstream_state_file=str(state_file),
        upstream_stream_path=original_path,
    )
    restored = restore_runtime_state(same_env)
    assert "upstream_stream_path" in restored
    assert same_env.upstream_stream_path == captured_path

    explicit_env = _settings(
        upstream_cookie=cookie,
        upstream_state_file=str(state_file),
        upstream_stream_path="/operator-configured/stream",
    )
    restore_runtime_state(explicit_env)
    assert explicit_env.upstream_stream_path == "/operator-configured/stream"


@pytest.mark.asyncio
async def test_expiring_token_warning_fires_once_per_token():
    from app.services.token_refresh import maybe_warn_expiring_token

    reset_refresh_state()
    expiring_jwt = _jwt(exp=time.time() + 300)
    settings = _settings(
        upstream_cookie=f"arena-auth-prod-v1={expiring_jwt}",
        upstream_refresh_leeway_seconds=600,
    )
    sent: list[str] = []

    class FakeAlerter:
        enabled = True

        async def send(self, text: str) -> bool:
            sent.append(text)
            return True

    alerter = FakeAlerter()
    first = await maybe_warn_expiring_token(settings, alerter)
    second = await maybe_warn_expiring_token(settings, alerter)

    assert first is not None and "dk kaldı" in first
    assert second is None, "aynı token için tekrar uyarı gönderilmemeli"
    assert len(sent) == 1
    assert expiring_jwt not in sent[0]


@pytest.mark.asyncio
async def test_reactive_401_triggers_immediate_token_refresh_and_retries(respx_mock):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import ai_stream

    old_jwt = _jwt(exp=time.time() + 60)
    new_jwt = _jwt(exp=time.time() + 3600)
    cookie = session_cookie(expires_in=60)
    settings = _settings(
        upstream_cookie=cookie,
        upstream_access_token=old_jwt,
        upstream_stream_path="/nextjs-api/stream/create-evaluation",
    )

    chat_calls = 0

    def chat_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        if chat_calls == 1:
            return httpx.Response(401, json={"message": "invalid token"})
        assert request.headers.get("authorization") == f"Bearer {new_jwt}"
        return httpx.Response(200, content=ai_stream("kurtarildi"))

    respx_mock.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
        side_effect=chat_side_effect
    )
    respx_mock.post("https://auth.test/auth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": new_jwt,
                "refresh_token": "rotated-401",
                "expires_in": 3600,
            },
        )
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "merhaba"}],
                "stream": False,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "kurtarildi"
    assert chat_calls == 2


def test_render_restart_prefers_fresher_synced_cookie_over_older_upstream_curl():
    from app.upstream.curl_runtime import apply_upstream_curl

    old_cookie = session_cookie(expires_in=600)
    fresher_cookie, _ = rewrite_session_cookie(
        old_cookie,
        access_token=_jwt(exp=time.time() + 3600),
        refresh_token="render-synced-refresh",
        expires_in=3600,
    )
    curl = (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {old_cookie}' "
        '--data-raw \'{"id":"1","modelAId":"m1","userMessage":"hi"}\''
    )
    settings = _settings(
        upstream_curl=curl,
        upstream_cookie=fresher_cookie,
        upstream_refresh_token="render-synced-refresh",
    )
    updated, _ = apply_upstream_curl(settings)
    assert extract_refresh_token(updated.upstream_cookie) == "render-synced-refresh"


def test_legacy_render_snapshot_does_not_replace_curl_user_agent_with_default():
    from app.upstream.curl_runtime import apply_upstream_curl

    old_cookie = session_cookie(expires_in=600)
    fresh_cookie, _ = rewrite_session_cookie(
        old_cookie,
        access_token=_jwt(exp=time.time() + 3600),
        refresh_token="legacy-refresh",
    )
    curl = (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {old_cookie}' "
        "-H 'user-agent: CapturedAgent/1.0' "
        "--data-raw '{\"id\":\"1\",\"modelAId\":\"m1\",\"userMessage\":\"hi\"}'"
    )
    settings = _settings(
        upstream_curl=curl,
        upstream_cookie=fresh_cookie,
        upstream_user_agent="UnsyncedDefaultAgent/1.0",
        upstream_snapshot_version=0,
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.upstream_cookie == fresh_cookie
    assert updated.upstream_user_agent == "CapturedAgent/1.0"


def test_full_render_snapshot_clears_stale_curl_authorization_token():
    from app.upstream.auth import extract_access_token
    from app.upstream.curl_runtime import apply_upstream_curl

    old_cookie = session_cookie(expires_in=600)
    old_access = extract_access_token(old_cookie)
    fresh_cookie, _ = rewrite_session_cookie(
        old_cookie,
        access_token=_jwt(exp=time.time() + 3600),
        refresh_token="cookie-only-refresh",
    )
    curl = (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {old_cookie}' "
        f"-H 'authorization: Bearer {old_access}' "
        "--data-raw '{\"id\":\"1\",\"modelAId\":\"m1\",\"userMessage\":\"hi\"}'"
    )
    settings = _settings(
        upstream_curl=curl,
        upstream_cookie=fresh_cookie,
        upstream_access_token="",
        upstream_snapshot_version=1,
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.upstream_cookie == fresh_cookie
    assert updated.upstream_access_token == ""


def test_render_restart_prefers_synced_refresh_when_jwt_expiry_is_equal():
    from app.upstream.auth import extract_access_token
    from app.upstream.curl_runtime import apply_upstream_curl

    old_cookie = session_cookie(expires_in=3600)
    same_access = extract_access_token(old_cookie)
    assert same_access is not None
    rotated_cookie, _ = rewrite_session_cookie(
        old_cookie,
        access_token=same_access,
        refresh_token="latest-render-refresh",
        expires_in=3600,
    )
    curl = (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {old_cookie}' "
        '--data-raw \'{"id":"1","modelAId":"m1","userMessage":"hi"}\''
    )
    settings = _settings(
        upstream_curl=curl,
        upstream_cookie=rotated_cookie,
        upstream_refresh_token="latest-render-refresh",
    )

    updated, _ = apply_upstream_curl(settings)

    assert updated.upstream_cookie == rotated_cookie
    assert extract_refresh_token(updated.upstream_cookie) == "latest-render-refresh"


def test_upstream_set_cookie_persists_into_settings_and_survives_client_reset(respx_mock):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.upstream.auth import parse_cookie_header
    from tests.conftest import ai_stream

    initial_cookie = session_cookie(expires_in=600)
    server_rotated_cookie, _ = rewrite_session_cookie(
        initial_cookie,
        access_token=_jwt(exp=time.time() + 3600),
        refresh_token="server-set-cookie-refresh",
        expires_in=3600,
    )
    rotated_parts = parse_cookie_header(server_rotated_cookie)
    set_cookie_headers = [
        ("set-cookie", f"{name}={value}; Path=/")
        for name, value in rotated_parts.items()
    ] + [("set-cookie", "__cf_bm=fresh-cf-bm; Path=/")]

    settings = _settings(
        upstream_cookie=initial_cookie,
        upstream_stream_path="/nextjs-api/stream/create-evaluation",
    )

    respx_mock.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
        return_value=httpx.Response(
            200,
            headers=set_cookie_headers,
            content=ai_stream("merhaba"),
        )
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "selam"}],
                "stream": False,
            },
        )
        assert response.status_code == 200
        assert settings.upstream_refresh_token == "server-set-cookie-refresh"
        assert "fresh-cf-bm" in settings.upstream_cookie
        assert extract_refresh_token(settings.upstream_cookie) == "server-set-cookie-refresh"


@pytest.mark.asyncio
async def test_concurrent_token_refresh_deduplicates_single_use_refresh_token():
    settings = _settings(upstream_cookie=session_cookie(expires_in=120))
    new_access = _jwt(exp=time.time() + 3600)
    auth_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        auth_calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={
                "access_token": new_access,
                "refresh_token": "single-use-rotated",
                "expires_in": 3600,
            },
        )

    import asyncio

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcomes = await asyncio.gather(
            *(refresh_upstream_token(settings, client) for _ in range(5))
        )

    assert all(o.ok for o in outcomes)
    assert auth_calls == 1
    assert settings.upstream_refresh_token == "single-use-rotated"


@pytest.mark.asyncio
async def test_concurrent_refresh_deduplicates_when_initial_access_token_is_empty(
    monkeypatch: pytest.MonkeyPatch,
):
    import asyncio

    import app.upstream.refresh as refresh_module

    settings = _settings(
        upstream_cookie="",
        upstream_access_token="",
        upstream_refresh_token="non-rotating-refresh",
    )
    fresh_access = _jwt(exp=time.time() + 3600)

    async def unexpected_network_refresh(*_args: Any, **_kwargs: Any):
        raise AssertionError("ikinci görev yeniden ağa çıkmamalı")

    monkeypatch.setattr(
        refresh_module, "_refresh_upstream_token_locked", unexpected_network_refresh
    )
    lock = refresh_module._get_refresh_lock(1)
    await lock.acquire()
    task = asyncio.create_task(refresh_upstream_token(settings))
    await asyncio.sleep(0)  # görev token_before'ı okuyup kilitte beklesin
    settings.upstream_access_token = fresh_access
    lock.release()

    outcome = await asyncio.wait_for(task, timeout=1)

    assert outcome.ok is True
    assert outcome.access_token == fresh_access
    assert "deduplicated_concurrent_refresh" in outcome.notes


@pytest.mark.asyncio
async def test_proactive_expired_token_refresh_before_upstream_request(respx_mock):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import ai_stream

    expired_jwt = _jwt(exp=time.time() - 120)
    fresh_jwt = _jwt(exp=time.time() + 3600)
    cookie = session_cookie(expires_in=-120)
    settings = _settings(
        upstream_cookie=cookie,
        upstream_access_token=expired_jwt,
        upstream_stream_path="/nextjs-api/stream/create-evaluation",
    )

    chat_calls = 0

    def chat_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        chat_calls += 1
        assert request.headers.get("authorization") == f"Bearer {fresh_jwt}"
        return httpx.Response(200, content=ai_stream("ilk-denemede-taze"))

    respx_mock.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
        side_effect=chat_side_effect
    )
    respx_mock.post("https://auth.test/auth/v1/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": fresh_jwt,
                "refresh_token": "proactive-rotated",
                "expires_in": 3600,
            },
        )
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "merhaba"}],
                "stream": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ilk-denemede-taze"
    assert chat_calls == 1






@pytest.mark.asyncio
async def test_apikey_endpoint_refreshes_immediately_and_reports_result():
    """/apikey yalnız kaydetmemeli; aynı istekte yenilemeyi deneyip sonucu söylemeli."""
    new_access = _jwt(exp=time.time() + 3600)
    settings = _settings(
        upstream_cookie=session_cookie(expires_in=600),
        upstream_refresh_api_key="",
        upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/v1/token"):
            assert request.headers.get("apikey") == "anon-key-manual"
            return httpx.Response(
                200,
                json={
                    "access_token": new_access,
                    "refresh_token": "rotated-after-apikey",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(404)

    import respx

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://auth.test/auth/v1/token").mock(side_effect=handler)
        with _client(settings) as client:
            response = client.post(
                "/v1/admin/upstream/apikey",
                json={"apikey": "anon-key-manual"},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["refreshed"] is True
    assert body["expires_in"] == 3600
    assert settings.upstream_access_token == new_access


@pytest.mark.asyncio
async def test_apikey_endpoint_reports_when_refresh_still_fails():
    """Yanlış anahtar 'kaydedildi' diye yeşil görünmemeli."""
    settings = _settings(
        upstream_cookie=session_cookie(expires_in=600),
        upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid API key"})

    import respx

    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://auth.test/auth/v1/token").mock(side_effect=handler)
        with _client(settings) as client:
            response = client.post(
                "/v1/admin/upstream/apikey",
                json={"apikey": "wrong-key"},
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["refreshed"] is False
    assert body["refresh_detail"]


def test_warning_message_includes_deadline_and_reason():
    import asyncio

    from app.services.token_refresh import maybe_warn_expiring_token

    settings = _settings(upstream_cookie=session_cookie(expires_in=540))
    message = asyncio.run(
        maybe_warn_expiring_token(settings, reason="Upstream `apikey` istiyor")
    )
    assert message is not None
    assert "UTC" in message
    assert "apikey" in message


@pytest.mark.asyncio
async def test_apikey_discovery_reports_why_it_failed():
    """Keşif başarısızsa 'bulunamadı' değil, **neden** dönmeli."""
    from app.upstream.refresh import discover_refresh_api_key_report

    settings = _settings(
        upstream_refresh_api_key="",
        target_domain="upstream.test",
        upstream_cookie="",
        upstream_access_token="",
        upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked by bot protection")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await discover_refresh_api_key_report(settings, client)

    assert report.ok is False
    assert report.home_status == 403
    assert "403" in report.detail
    assert "/apikey <anahtar>" in report.detail


@pytest.mark.asyncio
async def test_apikey_discovery_scans_more_than_four_scripts():
    """Anahtar geç yüklenen chunk'taysa da bulunmalı (eski sınır 4 parçaydı)."""
    from app.upstream.refresh import discover_refresh_api_key_report

    anon = _anon_jwt()
    scripts = "".join(
        f'<script src="/_next/static/chunks/{index}.js"></script>' for index in range(8)
    )
    settings = _settings(
        upstream_refresh_api_key="",
        target_domain="upstream.test",
        upstream_cookie="",
        upstream_access_token="",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200, text=f"<html><head>{scripts}</head></html>")
        if request.url.path.endswith("/7.js"):
            return httpx.Response(200, text=f'anonKey: "{anon}"')
        return httpx.Response(200, text="// nothing")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await discover_refresh_api_key_report(settings, client)

    assert report.ok is True
    assert report.key == anon
    assert report.found_in == "script"
    assert settings.upstream_refresh_api_key == anon
