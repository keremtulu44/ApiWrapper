"""Render blueprint ve liveness/readiness ayrımı regresyonları."""

from __future__ import annotations

import asyncio
import json
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
    # Kalıcı değer `static`: `browser` hedefte 403 alıyor (14 Kasım ölçümü).
    assert env["RECAPTCHA_PROVIDER"]["value"] == "static"
    assert env["UPSTREAM_REFRESH_ENABLED"]["value"] == "false"
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


@pytest.mark.asyncio
async def test_render_env_sync_clears_empty_fields_with_delete():
    """Boş alanlar PUT ile **silinir**; niyet aynı, yöntem Render'ın kabul ettiği.

    Snapshot boş değerleri bilerek taşır: yeni cURL'de olmayan eski header/token
    restart'ta dirilmesin. Render boş değeri PUT ile reddediyor (ölçüm: 400 +
    15/30/60 sn'lik sonsuz retry), bu yüzden boş alan DELETE'e gider.
    """
    import json

    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=opaque",
        upstream_access_token="",
        upstream_refresh_token="",
    )
    calls: dict[str, tuple[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        body = request.read().decode()
        calls[key] = (request.method, json.loads(body)["value"] if body else None)
        return httpx.Response(200, json={"value": "stored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is True

    assert calls["UPSTREAM_COOKIE"] == ("PUT", "session=opaque")
    # Boş alanlar silme olarak gider; hiçbir anahtara boş değer PUT edilmez.
    for key in ("UPSTREAM_ACCESS_TOKEN", "UPSTREAM_REFRESH_TOKEN",
                "UPSTREAM_EXTRA_HEADERS", "UPSTREAM_CAPTURED_MODEL_ID"):
        assert calls[key][0] == "DELETE", key
    assert all(value != "" for _, value in calls.values())


@pytest.mark.asyncio
async def test_render_env_sync_permanent_rejection_is_logged_once_and_not_retried():
    """4xx tekrar deneme döngüsünü beslemez; süreç başına bir kez loglanır.

    Eski davranış: her sync'te `render_env_sync_failed` + `retry_scheduled` çifti
    (15/30/60 sn) — log gürültüsü gerçek arızayı gömüyordu.
    """
    from app.upstream import state_store

    state_store._PERMANENT_SYNC_REJECTIONS.clear()
    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=opaque",
    )
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(403, json={"message": "forbidden"})
        return httpx.Response(200, json={"value": "stored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await state_store.sync_render_env(settings, client)
        rejects_after_first = set(state_store._PERMANENT_SYNC_REJECTIONS)
        second = await state_store.sync_render_env(settings, client)

    # Kalıcı red: sync "yerleşti" sayılır, retry döngüsü uyanık kalmaz.
    assert (first, second) == (True, True)
    assert "DELETE" in methods
    assert rejects_after_first, "403 kalıcı red olarak işaretlenmeli"
    assert all(op == "delete" for op, _ in rejects_after_first)
    # İkinci deneme aynı anahtarları yeniden loglamaz (küme büyümez).
    assert methods.count("DELETE") > len(rejects_after_first)
    assert rejects_after_first == state_store._PERMANENT_SYNC_REJECTIONS


@pytest.mark.asyncio
async def test_render_env_sync_transient_failure_still_retries():
    """5xx geçicidir: `False` döner ki retry döngüsü yeniden denesin."""
    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=opaque",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is False


@pytest.mark.asyncio
async def test_render_env_sync_rate_limit_counts_as_transient():
    """429 geçicidir: Render API hız sınırında retry anlamlı (client.py ile aynı kural)."""
    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=opaque",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "slow down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is False


@pytest.mark.asyncio
async def test_render_env_delete_404_counts_as_cleared():
    """Anahtar zaten yoksa istenen son durum sağlanmış demektir."""
    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=opaque",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"value": "stored"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await sync_render_env(settings, client) is True


@pytest.mark.asyncio
async def test_slow_older_render_sync_cannot_overwrite_newer_credentials():
    """Ağda geç kalan eski snapshot, yeni oturumun üzerine yazılmamalı."""
    from app.upstream.state_store import sync_render_env

    settings = make_settings(
        render_api_key="render-secret",
        render_service_id="srv-free-test",
        upstream_cookie="session=old",
        upstream_access_token="old-access",
        upstream_refresh_token="old-refresh",
    )
    old_request_started = asyncio.Event()
    release_old_request = asyncio.Event()
    stored: dict[str, str] = {}

    async def async_handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", 1)[-1]
        body = request.read()
        if request.method == "DELETE":
            # Boş snapshot alanları silinir (Render boş değeri PUT ile reddediyor).
            stored[key] = ""
            return httpx.Response(200, json={"value": "deleted"})
        value = json.loads(body)["value"]
        if key == "UPSTREAM_COOKIE" and value == "session=old":
            old_request_started.set()
            await release_old_request.wait()
        stored[key] = value
        return httpx.Response(200, json={"value": "stored"})

    # httpx.MockTransport async handler desteklemediği için küçük bir async
    # transport kullanıyoruz; amaç gerçek ağ gecikmesini deterministik kurmak.
    class DelayedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return await async_handler(request)

    async with httpx.AsyncClient(transport=DelayedTransport()) as client:
        older = asyncio.create_task(sync_render_env(settings, client))
        await asyncio.wait_for(old_request_started.wait(), timeout=1)

        settings.upstream_cookie = "session=new"
        settings.upstream_access_token = "new-access"
        settings.upstream_refresh_token = "new-refresh"
        newer = asyncio.create_task(sync_render_env(settings, client))
        await asyncio.sleep(0.01)
        release_old_request.set()
        await asyncio.gather(older, newer)

    assert stored["UPSTREAM_COOKIE"] == "session=new"
    assert stored["UPSTREAM_ACCESS_TOKEN"] == "new-access"
    assert stored["UPSTREAM_REFRESH_TOKEN"] == "new-refresh"


def test_render_state_restores_when_target_domain_configured(monkeypatch, tmp_path):
    """Kalıcı durum geri yüklemesi alan adı eşleşmesine bağlıdır.

    Eski test burada `TARGET_DOMAIN` boşken kodun `arena.ai` yazdığını doğruluyordu;
    o davranış kaldırıldı (hedef alan adı koda gömülmez), bu yüzden test alan adı
    **ayarlanmış** durumu doğruluyor.
    """
    from app.core.config import get_settings
    from app.upstream.state_store import remember_seed, save_runtime_state

    state_file = tmp_path / "upstream_state.json"
    source = Settings(
        _env_file=None,
        target_domain="upstream.example",
        upstream_cookie="session=persisted",
        upstream_access_token="persistent-access-token",
        upstream_state_file=str(state_file),
    )
    remember_seed(source)
    assert save_runtime_state(source, source="test") is True

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("UPSTREAM_STATE_FILE", str(state_file))
    monkeypatch.setenv("UPSTREAM_CURL", "")
    monkeypatch.setenv("TARGET_DOMAIN", "upstream.example")
    monkeypatch.setenv("API_KEYS", "sk-render-test")
    monkeypatch.setenv("RECAPTCHA_PROVIDER", "noop")
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.target_domain == "upstream.example"
        assert app.state.settings.upstream_access_token == "persistent-access-token"
        assert app.state.settings.upstream_cookie == "session=persisted"
    finally:
        get_settings.cache_clear()


def test_render_does_not_inject_hardcoded_target_domain(monkeypatch, tmp_path):
    """`TARGET_DOMAIN` boşken koda gömülü bir alan adı seçilmemeli.

    Sabit bir alan adı, oturum çerezlerinin istenmeyen bir host'a gönderilmesi
    demektir; ayrıca maskeli alan adı kullanan kurulumu sessizce bozar.
    """
    from app.core.config import get_settings

    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("UPSTREAM_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UPSTREAM_CURL", "")
    monkeypatch.setenv("TARGET_DOMAIN", "")
    monkeypatch.setenv("API_KEYS", "sk-render-test")
    monkeypatch.setenv("RECAPTCHA_PROVIDER", "noop")
    get_settings.cache_clear()
    try:
        app = create_app()
        assert app.state.settings.target_domain in {"", "localhost", "127.0.0.1"}
        assert app.state.settings.upstream_referer_path == "/c/{chat_id}"
    finally:
        get_settings.cache_clear()


def test_telegram_worker_dockerfile_starts_bot_without_a_start_command():
    dockerfile = Path("Dockerfile.telegram").read_text(encoding="utf-8")
    assert 'CMD ["python", "-m", "app.telegram.bot"]' in dockerfile
    assert "uvicorn" not in dockerfile
    assert "playwright install" not in dockerfile


def test_render_env_boot_survives_empty_or_cookie_only_upstream_curl(monkeypatch):
    """Yarım cURL açılışı çökertmez; hedef alan adı yoksa `/readyz` dürüstçe reddeder.

    Eski davranış, hedef alan adı boşken koda gömülü bir alan adı (`arena.ai`)
    seçiyordu ve bu yüzden `/readyz` 200 dönüyordu. Sabit alan adı kaldırıldı:
    istekler yanlış host'a (ve oturum çerezleriyle birlikte) gitmesin diye
    `not_ready` doğru cevaptır (`docs/AUDIT_2026_11.md` §3 ile aynı kural).
    """
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
            health = client.get("/healthz")
            ready = client.get("/readyz")
        assert health.status_code == 200
        assert ready.status_code == 503, ready.text
        assert "target_domain_not_configured" in ready.text
        # Çerez yine de yüklendi: kullanıcı cURL'ü tamamlayınca hemen çalışır.
        assert "arena-auth-prod-v1.0" in app.state.settings.upstream_cookie
    finally:
        get_settings.cache_clear()

