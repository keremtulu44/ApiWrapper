"""Render blueprint ve liveness/readiness ayrımı regresyonları."""

from __future__ import annotations

from pathlib import Path

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


def test_render_blueprint_is_single_instance_always_on_and_secret_free():
    data = yaml.safe_load(Path("render.yaml").read_text())
    assert len(data["services"]) == 1
    service = data["services"][0]
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["plan"] == "starter"
    assert service["numInstances"] == 1
    assert service["healthCheckPath"] == "/readyz"
    assert service["dockerfilePath"] == "./Dockerfile"

    env = {item["key"]: item for item in service["envVars"]}
    for key in ("API_KEYS", "TARGET_DOMAIN", "UPSTREAM_COOKIE", "UPSTREAM_ACCESS_TOKEN"):
        assert env[key]["sync"] is False
        assert "value" not in env[key]
    assert env["AUTH_DISABLED"]["value"] == "false"
    assert env["UPSTREAM_STREAM_PATH"]["value"] == "/nextjs-api/stream/create-evaluation"
    assert env["UPSTREAM_REFERER_PATH"]["value"] == "/text/direct"
    assert env["UPSTREAM_MODE"]["value"] == "direct-battle"
    assert env["UPSTREAM_AUTH_FROM_COOKIE"]["value"] == "false"
    assert env["UPSTREAM_EXTRA_HEADERS"]["sync"] is False
    assert env["RECAPTCHA_PROVIDER"]["value"] == "browser"
    # Site key public'tir; gerçek cookie/token değerleri sync:false kalır.
    assert env["RECAPTCHA_SITE_KEY"]["value"] == "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
    assert env["RECAPTCHA_VERSION"]["value"] == "enterprise"
    assert env["RECAPTCHA_ACTION"]["value"] == "chat_submit"
