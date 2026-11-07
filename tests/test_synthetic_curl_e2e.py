"""Tamamen uydurma cURL ile uçtan uca test; tüm HTTPX trafiği mock'lanır."""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.upstream.auth import extract_access_token, extract_refresh_token
from tests.conftest import TEST_API_KEY, ai_stream, make_settings


def _jwt(seconds: int) -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f'{encode({"alg": "none"})}.{encode({"exp": int(time.time()) + seconds})}.signature'


def _fake_curl(access: str, refresh: str) -> str:
    session = {
        "access_token": access,
        "refresh_token": refresh,
        "user": {"name": "Test 🦊"},
    }
    encoded = "base64-" + base64.urlsafe_b64encode(
        json.dumps(session, ensure_ascii=False).encode()
    ).decode().rstrip("=")
    midpoint = len(encoded) // 2
    cookie = (
        "prefs=%7B%22lang%22%3A%22tr%22%7D; "
        f"arena-auth-test.0={encoded[:midpoint]}; "
        f"arena-auth-test.1={encoded[midpoint:]}; "
        "__cf_bm=fake-bot-cookie"
    )
    body = {
        "id": "old-chat-id", "modelAId": "fake-model-123", "mode": "direct-battle",
        "cx": "fake-context", "userMessage": "hello",
        "recaptchaV3Token": "old-fake-captcha",
    }
    return (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {cookie}' "
        "-H 'referer: https://upstream.test/text/direct' "
        "-H 'user-agent: SyntheticBrowser/1.0' "
        "-H 'sec-ch-ua-mobile: ?1' "
        "-H 'sec-ch-ua-platform: \"Android\"' "
        f"--data-raw '{json.dumps(body)}'"
    )


def test_fake_curl_full_lifecycle_refresh_set_cookie_and_restart(tmp_path: Path):
    old_access = _jwt(3600)
    new_access = _jwt(7200)
    curl = _fake_curl(old_access, "fake-refresh-before")
    state_file = tmp_path / "state.json"
    settings = make_settings(
        upstream_curl=curl,
        upstream_state_file=str(state_file),
        recaptcha_provider="external",
        recaptcha_external_url="https://solver.test/token",
        recaptcha_site_key="fake-site-key",
        upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
        deployment_id_refresh_enabled=False,
        upstream_keepalive_interval_seconds=0,
    )
    seen: list[dict] = []
    solver_tokens: list[str] = []
    refresh_calls = 0

    def captcha(_request: httpx.Request) -> httpx.Response:
        token = f"fake-captcha-{len(solver_tokens) + 1}"
        solver_tokens.append(token)
        return httpx.Response(200, json={"token": token})

    def refresh(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        refresh_calls += 1
        assert json.loads(request.content) == {"refresh_token": "fake-refresh-before"}
        return httpx.Response(200, json={
            "access_token": new_access, "refresh_token": "fake-refresh-after", "expires_in": 7200,
        })

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append({
            "cookie": request.headers.get("cookie", ""),
            "authorization": request.headers.get("authorization", ""),
            "user-agent": request.headers.get("user-agent", ""),
            "mobile": request.headers.get("sec-ch-ua-mobile", ""),
            "platform": request.headers.get("sec-ch-ua-platform", ""),
            "body": json.loads(request.content),
        })
        return httpx.Response(
            200, content=ai_stream(f"yanıt-{len(seen)}"),
            headers={"set-cookie": "server_hint=updated; Path=/"},
        )

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post("https://solver.test/token").mock(side_effect=captcha)
        router.post(url__startswith="https://auth.test/auth/v1/token").mock(side_effect=refresh)
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        app = create_app(settings)
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
            request = {"model": "test-model", "messages": [{"role": "user", "content": "merhaba"}]}
            first = client.post("/v1/chat/completions", json=request)
            assert first.status_code == 200, first.text
            assert first.json()["choices"][0]["message"]["content"] == "yanıt-1"
            refresh_response = client.post("/v1/admin/upstream/refresh")
            assert refresh_response.status_code == 200, refresh_response.text
            assert refresh_response.json()["status"] == "refreshed"
            assert "fake-refresh" not in refresh_response.text
            second = client.post("/v1/chat/completions", json={**request, "stream": True})
            assert second.status_code == 200, second.text
            assert "yanıt-2" in second.text and "[DONE]" in second.text

        # Yeni süreç aynı başlangıç cURL'ünü okur; kalıcı refresh snapshot'ı korunmalı.
        restarted = create_app(make_settings(
            upstream_curl=curl,
            upstream_state_file=str(state_file),
            recaptcha_provider="external",
            recaptcha_external_url="https://solver.test/token",
            recaptcha_site_key="fake-site-key",
            upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
            deployment_id_refresh_enabled=False,
            upstream_keepalive_interval_seconds=0,
        ))
        with TestClient(restarted) as client:
            response = client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "devam"}]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "yanıt-3"

    assert refresh_calls == 1
    assert solver_tokens == ["fake-captcha-1", "fake-captcha-2", "fake-captcha-3"]
    assert len(seen) == 3
    for index, sent in enumerate(seen):
        assert sent["body"]["modelAId"] == "fake-model-123"
        assert sent["body"]["mode"] == "direct-battle"
        assert sent["body"]["cx"] == "fake-context"
        assert sent["body"]["recaptchaV3Token"] == solver_tokens[index]
        assert sent["user-agent"] == "SyntheticBrowser/1.0"
        assert sent["mobile"] == "?1" and sent["platform"] == '"Android"'
        assert "prefs=%7B%22lang%22%3A%22tr%22%7D" in sent["cookie"]
    assert extract_access_token(seen[0]["cookie"]) == old_access
    assert extract_refresh_token(seen[0]["cookie"]) == "fake-refresh-before"
    for sent in seen[1:]:
        assert extract_access_token(sent["cookie"]) == new_access
        assert extract_refresh_token(sent["cookie"]) == "fake-refresh-after"
        assert "server_hint=updated" in sent["cookie"]
        assert sent["authorization"] == f"Bearer {new_access}"


def test_curl_hot_swap_and_restart_use_same_new_model_snapshot(tmp_path: Path):
    old_curl = _fake_curl(_jwt(3600), "old-refresh")
    new_curl = _fake_curl(_jwt(7200), "new-refresh").replace(
        "fake-model-123", "fake-model-456"
    )
    state_file = tmp_path / "state.json"
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["modelAId"])
        return httpx.Response(200, content=ai_stream("tamam"))

    def settings():
        return make_settings(
            upstream_curl=old_curl,
            upstream_state_file=str(state_file),
            recaptcha_provider="external",
            recaptcha_external_url="https://solver.test/token",
            recaptcha_site_key="fake-site-key",
            upstream_refresh_enabled=False,
            deployment_id_refresh_enabled=False,
            upstream_keepalive_interval_seconds=0,
        )

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post("https://solver.test/token").mock(return_value=httpx.Response(
            200, json={"token": "fake-dynamic-token"}
        ))
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        app = create_app(settings())
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
            swap = client.post("/v1/admin/upstream/curl", json={"curl": new_curl})
            assert swap.status_code == 200, swap.text
            assert swap.json()["upstream_model"] == "fake-model-456"
            response = client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
        app = create_app(settings())
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
            response = client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
    assert seen == ["fake-model-456", "fake-model-456"]


def test_expired_fake_curl_is_refreshed_before_first_chat(tmp_path: Path):
    expired = _jwt(-60)
    fresh = _jwt(7200)
    settings = make_settings(
        upstream_curl=_fake_curl(expired, "fake-expired-refresh"),
        upstream_state_file=str(tmp_path / "state.json"),
        upstream_refresh_url="https://auth.test/auth/v1/token?grant_type=refresh_token",
        recaptcha_provider="external",
        recaptcha_external_url="https://solver.test/token",
        recaptcha_site_key="fake-site-key",
        deployment_id_refresh_enabled=False,
        upstream_keepalive_interval_seconds=0,
    )
    calls = {"refresh": 0, "posts": 0}

    def refresh(request: httpx.Request) -> httpx.Response:
        calls["refresh"] += 1
        assert json.loads(request.content) == {"refresh_token": "fake-expired-refresh"}
        return httpx.Response(200, json={
            "access_token": fresh, "refresh_token": "fake-refreshed", "expires_in": 7200,
        })

    def upstream(request: httpx.Request) -> httpx.Response:
        calls["posts"] += 1
        assert extract_access_token(request.headers.get("cookie", "")) == fresh
        assert extract_refresh_token(request.headers.get("cookie", "")) == "fake-refreshed"
        assert request.headers.get("authorization") == f"Bearer {fresh}"
        return httpx.Response(200, content=ai_stream("hazır"))

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(url__startswith="https://auth.test/auth/v1/token").mock(side_effect=refresh)
        router.post("https://solver.test/token").mock(
            return_value=httpx.Response(200, json={"token": "fake-dynamic-token"})
        )
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
    assert response.status_code == 200, response.text
    assert calls == {"refresh": 1, "posts": 1}


def test_hot_swap_same_access_expiry_applies_new_cookie_model_and_context(tmp_path: Path):
    # Aynı access token ile tarayıcıdaki bağlam/refresh/model değişebilir.
    shared_access = _jwt(3600)
    old_curl = _fake_curl(shared_access, "old-refresh")
    new_curl = (
        _fake_curl(shared_access, "new-refresh")
        .replace("fake-model-123", "fake-model-456")
        .replace("fake-context", "updated-context")
    )
    settings = make_settings(
        upstream_curl=old_curl, upstream_state_file=str(tmp_path / "state.json"),
        recaptcha_provider="external", recaptcha_external_url="https://solver.test/token",
        recaptcha_site_key="fake-site-key", upstream_refresh_enabled=False,
        deployment_id_refresh_enabled=False, upstream_keepalive_interval_seconds=0,
    )
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((json.loads(request.content), request.headers.get("cookie", "")))
        return httpx.Response(200, content=ai_stream("ok"))

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post("https://solver.test/token").mock(
            return_value=httpx.Response(200, json={"token": "fake-token"})
        )
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings)) as client:
            client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
            swap = client.post("/v1/admin/upstream/curl", json={"curl": new_curl})
            assert swap.status_code == 200, swap.text
            assert swap.json()["upstream_model"] == "fake-model-456"
            response = client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
        # Aynı başlangıç cURL'üyle restart, son yönetici snapshot'ını korur.
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
    assert len(seen) == 2
    for body, cookie in seen:
        assert body["modelAId"] == "fake-model-456"
        assert body["cx"] == "updated-context"
        assert extract_refresh_token(cookie) == "new-refresh"


@pytest.mark.parametrize(
    "failure, expected_status, expected_posts, expected_code",
    [
        ("captcha", 200, 2, None),
        ("server", 200, 2, None),
        ("challenge", 502, 1, "upstream_challenge"),
        ("read_timeout", 504, 1, "upstream_timeout"),
    ],
)
def test_fake_curl_failure_paths(failure, expected_status, expected_posts, expected_code):
    settings = make_settings(
        upstream_curl=_fake_curl(_jwt(3600), "fake-refresh"),
        recaptcha_provider="external", recaptcha_external_url="https://solver.test/token",
        recaptcha_site_key="fake-site-key", upstream_refresh_enabled=False,
        deployment_id_refresh_enabled=False, upstream_keepalive_interval_seconds=0,
        retry_max_attempts=2, retry_base_delay=0, retry_max_delay=0,
    )
    tokens: list[str] = []
    sent: list[str] = []

    def solver(_request: httpx.Request) -> httpx.Response:
        token = f"one-use-{len(tokens) + 1}"
        tokens.append(token)
        return httpx.Response(200, json={"token": token})

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["recaptchaV3Token"])
        if len(sent) == 1:
            if failure == "captcha":
                return httpx.Response(403, text='{"error":"recaptcha invalid"}')
            if failure == "server":
                return httpx.Response(503, text="unavailable")
            if failure == "challenge":
                return httpx.Response(403, headers={"cf-mitigated": "challenge"})
            if failure == "read_timeout":
                raise httpx.ReadTimeout("fake timeout", request=request)
        return httpx.Response(200, content=ai_stream("ok"))

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post("https://solver.test/token").mock(side_effect=solver)
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
    assert response.status_code == expected_status, response.text
    if expected_code:
        assert response.json()["error"]["code"] == expected_code
    assert len(sent) == expected_posts
    assert sent == tokens
    assert len(set(tokens)) == len(tokens)


def test_hot_swap_without_model_returns_to_configured_model_and_survives_restart(tmp_path: Path):
    initial_curl = _fake_curl(_jwt(3600), "old-refresh")
    new_curl = _fake_curl(_jwt(7200), "new-refresh").replace(
        '"modelAId": "fake-model-123", ', ""
    )
    state_file = str(tmp_path / "state.json")
    sent: list[str] = []

    def settings():
        return make_settings(
            upstream_curl=initial_curl, upstream_state_file=state_file,
            recaptcha_provider="external", recaptcha_external_url="https://solver.test/token",
            recaptcha_site_key="fake-site-key", upstream_refresh_enabled=False,
            deployment_id_refresh_enabled=False, upstream_keepalive_interval_seconds=0,
        )

    def upstream(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["modelAId"])
        return httpx.Response(200, content=ai_stream("ok"))

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post("https://solver.test/token").mock(
            return_value=httpx.Response(200, json={"token": "fake-token"})
        )
        router.post("https://upstream.test/nextjs-api/stream/create-evaluation").mock(
            side_effect=upstream
        )
        router.get(url__startswith="https://upstream.test/").mock(return_value=httpx.Response(200))
        with TestClient(create_app(settings())) as client:
            client.headers["Authorization"] = f"Bearer {TEST_API_KEY}"
            swap = client.post("/v1/admin/upstream/curl", json={"curl": new_curl})
            assert swap.status_code == 200, swap.text
            assert swap.json()["upstream_model"] is None
            response = client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
        with TestClient(create_app(settings())) as client:
            response = client.post(
                "/v1/chat/completions", headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200, response.text
    assert sent == ["upstream-model-a", "upstream-model-a"]


def test_render_style_env_restore_keeps_refreshed_curl_context():
    from app.upstream.curl_runtime import apply_curl_to_settings, apply_upstream_curl

    old_curl = _fake_curl(_jwt(3600), "old-refresh")
    new_curl = _fake_curl(_jwt(7200), "new-refresh").replace(
        "fake-context", "new-context"
    ).replace("fake-model-123", "new-model")
    initial, _ = apply_upstream_curl(make_settings(
        upstream_curl=old_curl, recaptcha_provider="external"
    ))
    apply_curl_to_settings(initial, new_curl)
    # Render env sync'inin güncel alanları ve eski UPSTREAM_CURL ile yeniden açılış.
    restored, model = apply_upstream_curl(initial.model_copy(update={"upstream_curl": old_curl}))
    assert model == "new-model"
    assert extract_refresh_token(restored.upstream_cookie) == "new-refresh"
    assert restored.upstream_cx_send is True
    assert restored.upstream_cx == "new-context"
