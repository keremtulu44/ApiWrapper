"""Açık cookie-only modu refresh/snapshot boyunca Authorization yollamaz."""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.auth import extract_access_token, extract_refresh_token
from app.upstream.headers import build_authorization, build_page_headers, build_stream_headers
from app.upstream.refresh import RefreshOutcome, apply_refresh
from tests.conftest import TEST_API_KEY, ai_stream, make_settings
from tests.test_synthetic_curl_e2e import _fake_curl, _jwt


def test_cookie_only_suppresses_all_upstream_authorization_without_deleting_tokens():
    token = _jwt(3600)
    settings = make_settings(
        upstream_auth_header_mode="cookie_only", upstream_auth_from_cookie=True,
        upstream_access_token=token, upstream_cookie="auth=value",
        upstream_extra_headers='{"Authorization":"Bearer extra-secret","x-extra":"kept"}',
    )
    assert build_authorization(settings) is None
    for headers in (build_page_headers(settings), build_stream_headers(settings, "chat")):
        assert not any(name.lower() == "authorization" for name in headers)
        assert headers["x-extra"] == "kept"
    assert settings.upstream_access_token == token


def test_configured_mode_keeps_existing_authorization_behavior():
    settings = make_settings(upstream_access_token="explicit-token")
    assert build_authorization(settings) == "Bearer explicit-token"


@pytest.mark.parametrize("mode", ["configured", "cookie_only"])
@respx.mock
def test_cookie_only_mode_survives_refresh_and_restart(mode, tmp_path):
    old_access = _jwt(3600)
    new_access = _jwt(7200)
    curl = _fake_curl(old_access, "fake-refresh-before")
    state_file = str(tmp_path / "state.json")
    seen = []

    def settings():
        return make_settings(
            upstream_curl=curl, upstream_auth_header_mode=mode,
            upstream_state_file=state_file, recaptcha_provider="external",
            recaptcha_external_url="https://solver.test/token", recaptcha_site_key="fake-key",
            upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
            upstream_refresh_api_key="fake-refresh-api-key",
            deployment_id_refresh_enabled=False, upstream_keepalive_interval_seconds=0,
        )

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers.get("authorization"), request.headers.get("cookie", "")))
        return httpx.Response(200, content=ai_stream("ok"))

    respx.post("https://solver.test/token").mock(
        return_value=httpx.Response(200, json={"token": "fake-captcha"})
    )
    def refresh(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fake-refresh-api-key"
        return httpx.Response(200, json={
            "access_token": new_access, "refresh_token": "fake-refresh-after", "expires_in": 7200,
        })

    respx.post(url__startswith="https://auth.test/auth/v1/token").mock(side_effect=refresh)
    respx.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(side_effect=upstream)
    respx.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(create_app(settings())) as client:
        client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
        assert client.post("/v1/admin/upstream/refresh").status_code == 200
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
        diagnosis = client.get("/v1/admin/auth").json()
        assert diagnosis["authorization_header_will_be_sent"] == (mode == "configured")
    with TestClient(create_app(settings())) as client:
        client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
        assert client.post("/v1/chat/completions", json=payload).status_code == 200
    assert [extract_access_token(cookie) for _, cookie in seen] == [
        old_access, new_access, new_access,
    ]
    assert extract_refresh_token(seen[-1][1]) == "fake-refresh-after"
    assert [auth for auth, _ in seen] == (
        [f"Bearer {old_access}", f"Bearer {new_access}", f"Bearer {new_access}"]
        if mode == "configured" else [None, None, None]
    )


def test_cookie_only_blocks_newly_refreshed_explicit_token():
    settings = make_settings(upstream_auth_header_mode="cookie_only", upstream_cookie="session=x")
    apply_refresh(settings, RefreshOutcome(ok=True, access_token="refreshed-token"))
    assert settings.upstream_access_token == "refreshed-token"
    assert build_authorization(settings) is None


def test_explicit_curl_authorization_is_preserved_but_cookie_only_suppresses_it():
    from app.upstream.curl_runtime import apply_upstream_curl

    curl = _fake_curl(_jwt(3600), "fake-refresh").replace(
        "-H 'referer:", "-H 'authorization: Bearer explicit-test-token' -H 'referer:"
    )
    settings, _ = apply_upstream_curl(make_settings(
        upstream_curl=curl, upstream_auth_header_mode="cookie_only"
    ))
    assert settings.upstream_access_token == "explicit-test-token"
    assert build_authorization(settings) is None
    settings.upstream_auth_header_mode = "configured"
    assert build_authorization(settings) == "Bearer explicit-test-token"


def test_curl_import_warning_reflects_cookie_only_mode():
    from app.upstream.curl_import import build_settings, parse_curl

    curl = _fake_curl(_jwt(3600), "fake-refresh")
    _, warnings, _ = build_settings(
        parse_curl(curl), existing={"UPSTREAM_AUTH_HEADER_MODE": "cookie_only"}
    )
    assert any("cookie_only" in warning and "göndermeyecek" in warning for warning in warnings)
    assert not any("gönderilecek" in warning for warning in warnings)


def test_auth_diagnostics_includes_extra_header_unless_disabled():
    for mode, expected in [("configured", True), ("cookie_only", False)]:
        settings = make_settings(
            upstream_auth_header_mode=mode, upstream_access_token="",
            upstream_extra_headers='{"Authorization":"Bearer extra-token"}',
        )
        with TestClient(create_app(settings)) as client:
            response = client.get(
                "/v1/admin/auth", headers={"Authorization": f"Bearer {TEST_API_KEY}"}
            )
        assert response.json()["authorization_header_will_be_sent"] is expected
