"""Render blueprint ve liveness/readiness ayrımı regresyonları."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.api.v1.health import _deployment_issues
from app.core.config import Settings
from app.main import create_app
from app.services.account import AccountPool
from app.services.model_registry import ModelEntry, ModelRegistry
from tests.conftest import make_settings


def test_liveness_stays_up_while_default_development_config_is_not_ready():
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        liveness = client.get("/healthz")
        readiness = client.get("/readyz")

    assert liveness.status_code == 200
    assert liveness.json() == {"status": "ok"}
    assert readiness.status_code == 503
    issues = set(readiness.json()["issues"])
    assert {
        "target_domain_not_configured",
        "default_development_api_key",
        "recaptcha_static_token_missing",
    } <= issues
    assert "sk-local-dev-key" not in readiness.text


def test_readyz_accepts_complete_configuration_and_is_rate_limit_exempt():
    settings = make_settings(
        recaptcha_provider="static",
        recaptcha_static_token="test-captcha",
        rate_limit_enabled=True,
        rate_limit_rpm=1,
        rate_limit_burst=1,
    )
    with TestClient(create_app(settings)) as client:
        responses = [client.get("/readyz") for _ in range(3)]
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == {"status": "ok"}


def test_readiness_checks_captcha_requirements_without_network_calls():
    registry = ModelRegistry.from_file("tests/data/models.yaml")
    browser = make_settings(recaptcha_provider="browser", recaptcha_site_key="")
    assert "recaptcha_site_key_missing" in _deployment_issues(
        browser, registry, AccountPool(browser)
    )

    external = make_settings(recaptcha_provider="external", recaptcha_external_url="")
    assert "recaptcha_external_url_missing" in _deployment_issues(
        external, registry, AccountPool(external)
    )


def test_readiness_still_rejects_a_placeholder_default_model():
    settings = make_settings(recaptcha_provider="noop")
    registry = ModelRegistry(
        [ModelEntry(id="default", upstream_id="REPLACE_WITH_UPSTREAM_MODEL_ID")]
    )
    issues = _deployment_issues(settings, registry, AccountPool(settings))
    assert "default_upstream_model_placeholder" in issues


def test_static_captcha_can_come_from_the_only_configured_account():
    settings = make_settings(
        upstream_cookie="",
        upstream_cookie_2="session=two",
        recaptcha_provider="static",
        recaptcha_static_token="",
        recaptcha_static_token_2="account-captcha",
    )
    registry = ModelRegistry.from_file("tests/data/models.yaml")
    issues = _deployment_issues(settings, registry, AccountPool(settings))
    assert "recaptcha_static_token_missing" not in issues


def test_shipped_max_model_uses_the_observed_stable_upstream_id():
    registry = ModelRegistry.from_file("config/models.yaml")
    entry = registry.resolve("max")
    assert entry.upstream_id == "019b24bb-5caf-71c3-b854-37d0c7086f21"
    assert registry.resolve("max-auto") is entry
    assert registry.resolve(None) is entry


def test_render_blueprint_is_single_instance_free_and_secret_free():
    data = yaml.safe_load(Path("render.yaml").read_text())
    assert len(data["services"]) == 1
    service = data["services"][0]
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["plan"] == "free"
    assert service["numInstances"] == 1
    assert service["healthCheckPath"] == "/readyz"
    assert service["dockerfilePath"] == "./Dockerfile"
    # Free plan ücretli disk/always-on kaynağı içermemeli. Kimlik kalıcılığı
    # Render env API ile sağlanır; servis ID'si Render'da otomatik bulunur.
    assert "disk" not in service
    assert "disks" not in service

    env = {item["key"]: item for item in service["envVars"]}
    assert env["RENDER_API_KEY"]["sync"] is False
    assert "value" not in env["RENDER_API_KEY"]
    assert "RENDER_SERVICE_ID" not in env
    assert "UPSTREAM_STATE_FILE" not in env
    for key in ("API_KEYS", "UPSTREAM_CURL"):
        assert env[key]["sync"] is False
        assert "value" not in env[key]
    assert env["AUTH_DISABLED"]["value"] == "false"
    # Upstream parmak izi tek secret cURL'den çıkar; Blueprint ayrı ve birbiriyle
    # eskiyebilecek domain/cookie/yol alanları istemez.
    assert not {
        "TARGET_DOMAIN",
        "UPSTREAM_COOKIE",
        "UPSTREAM_ACCESS_TOKEN",
        "UPSTREAM_STREAM_PATH",
        "UPSTREAM_EXTRA_HEADERS",
    } & env.keys()
    assert env["RECAPTCHA_PROVIDER"]["value"] == "browser"
    # Site key public'tir; gerçek cookie/token değerleri sync:false kalır.
    assert env["RECAPTCHA_SITE_KEY"]["value"] == "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
    assert env["RECAPTCHA_VERSION"]["value"] == "enterprise"
    assert env["RECAPTCHA_ACTION"]["value"] == "chat_submit"


@pytest.mark.asyncio
async def test_free_render_persists_rotated_credentials_through_env_api():
    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=rotated",
        upstream_access_token="fresh-access-token",
        upstream_refresh_token="fresh-refresh-token",
    )
    updates: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer render-secret"
        assert request.url.path.startswith("/v1/services/srv-free-test/env-vars/")
        updates[request.url.path.rsplit("/", 1)[-1]] = request.read().decode()
        return httpx.Response(200, json={"value": "stored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is True

    assert {
        "UPSTREAM_COOKIE",
        "UPSTREAM_ACCESS_TOKEN",
        "UPSTREAM_REFRESH_TOKEN",
        "UPSTREAM_STREAM_PATH",
        "UPSTREAM_REFERER_PATH",
    } <= updates.keys()
    assert all("render-secret" not in value for value in updates.values())


def test_render_state_restores_after_target_domain_fallback(monkeypatch, tmp_path):
    from app.core.config import get_settings
    from app.upstream.state_store import remember_seed, save_runtime_state

    state_file = tmp_path / "upstream_state.json"
    source = Settings(
        _env_file=None,
        target_domain="arena.ai",
        upstream_cookie="session=persisted",
        upstream_access_token="persistent-access-token",
        upstream_state_file=str(state_file),
    )
    remember_seed(source)
    assert save_runtime_state(source, source="test") is True

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("UPSTREAM_STATE_FILE", str(state_file))
    monkeypatch.setenv("UPSTREAM_CURL", "")
    monkeypatch.setenv("TARGET_DOMAIN", "")
    monkeypatch.setenv("API_KEYS", "sk-render-test")
    monkeypatch.setenv("RECAPTCHA_PROVIDER", "noop")
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.target_domain == "arena.ai"
        assert app.state.settings.upstream_access_token == "persistent-access-token"
        assert app.state.settings.upstream_cookie == "session=persisted"
    finally:
        get_settings.cache_clear()


def test_telegram_worker_dockerfile_starts_bot_without_a_start_command():
    dockerfile = Path("Dockerfile.telegram").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.telegram.bot"]' in dockerfile
    assert "uvicorn" not in dockerfile
    assert "playwright install" not in dockerfile


def test_render_env_boot_survives_empty_or_cookie_only_upstream_curl(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RECAPTCHA_PROVIDER", "browser")
    monkeypatch.setenv(
        "RECAPTCHA_SITE_KEY", "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
    )
    monkeypatch.setenv("UPSTREAM_CURL", "arena-auth-prod-v1.0=test-cookie; __cf_bm=1")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        app = create_app()
        with TestClient(app) as client:
            ready = client.get("/readyz")
        assert ready.status_code == 200, ready.text
        assert "arena-auth-prod-v1.0" in app.state.settings.upstream_cookie
    finally:
        get_settings.cache_clear()

