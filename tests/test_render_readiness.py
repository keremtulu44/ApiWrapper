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


def test_render_blueprint_is_single_instance_free_plan_and_secret_free():
    data = yaml.safe_load(Path("render.yaml").read_text())
    assert len(data["services"]) == 1
    service = data["services"][0]
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    # Operatör kararı (19 Kasım 2026): blueprint canlı servisin gerçek planını
    # yazar. Free plan hareketsizlikte uyur; servisi dışarıdaki zamanlayıcı
    # (cron-job.org) uyanık tutar (docs/RENDER.md, ROLLBACK §12.8).
    assert service["plan"] == "free"
    assert service["numInstances"] == 1
    assert service["healthCheckPath"] == "/readyz"
    assert service["dockerfilePath"] == "./Dockerfile"

    env = {item["key"]: item for item in service["envVars"]}
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
    # Kalıcı değer `static`: sunucuda üretilen token (`browser`) hedefte 403 alıyor
    # (14 Kasım 2026 ölçümü, docs/LESSONS §3). `browser` yazılıysa cURL içe aktarımı
    # statik token'ı atar; blueprint'ten kurulan servis hiç cevap veremezdi.
    assert env["RECAPTCHA_PROVIDER"]["value"] == "static"
    # Site key public'tir; gerçek cookie/token değerleri sync:false kalır.
    assert env["RECAPTCHA_SITE_KEY"]["value"] == "6LeTGMcsAAAAALuIlkVwIxaAuZA8VledA6d3Nnb0"
    assert env["RECAPTCHA_VERSION"]["value"] == "enterprise"
    assert env["RECAPTCHA_ACTION"]["value"] == "chat_submit"


def test_telegram_worker_dockerfile_starts_bot_without_a_start_command():
    dockerfile = Path("Dockerfile.telegram").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.telegram.bot"]' in dockerfile
    assert "uvicorn" not in dockerfile
    assert "playwright install" not in dockerfile


def test_unwritable_state_dir_is_reported_instead_of_silent_data_loss(tmp_path):
    """`AUTOPILOT_STATE_DIR` yazılamıyorsa `/readyz` sessiz kalmaz."""
    from app.api.v1.health import _deployment_issues

    blocker = tmp_path / "dosya"
    blocker.write_text("x", encoding="utf-8")  # klasör yerine dosya: mkdir patlar
    settings = make_settings(
        recaptcha_provider="static",
        recaptcha_static_token="t",
        autopilot_state_dir=str(blocker / "alt"),
    )
    account = AccountPool(settings)
    issues = _deployment_issues(settings, ModelRegistry([]), account)
    assert "autopilot_state_dir_not_writable" in issues


def test_writable_state_dir_is_accepted(tmp_path):
    from app.api.v1.health import _deployment_issues

    target = tmp_path / "runs"
    settings = make_settings(
        recaptcha_provider="static",
        recaptcha_static_token="t",
        autopilot_state_dir=str(target),
    )
    issues = _deployment_issues(settings, ModelRegistry([]), AccountPool(settings))
    assert "autopilot_state_dir_not_writable" not in issues
    assert target.is_dir()  # kalıcılık klasörü hazır
