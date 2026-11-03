"""Baştan sona denetimde çıkan dört zayıflığın regresyon testleri.

1. **Boş havuzda kimlik tazeleme yok sayılıyordu.** Süreç kimlik bilgisi olmadan
   açıldıysa (`UPSTREAM_COOKIE` boş) havuz boş kalıyordu; `/upstream/cookie`,
   `/upstream/curl` ve `/upstream/capture` "applied" diyor ama
   `reload_credentials` boş liste üzerinde döndüğü için istekler kimliksiz
   gidiyordu. Havuz artık kendini doldurur.

2. **Yeni çerez eski bearer token'ı yenmiyordu.** `UPSTREAM_ACCESS_TOKEN` doluysa
   `build_authorization` onu çerezden önce tercih eder; yalnızca çerez
   güncellendiğinde eski token header'da kalmaya devam ediyor ve taze oturum yok
   sayılıyordu (telefondan `/cookie` akışının sessiz hatası).

3. **Log'a token sızabiliyordu.** Maskeleme yalnızca anahtar adına bakıyordu;
   upstream hata gövdesi `body=...` altında token taşırsa maskelenmiyordu.

4. **Metrik kardinalitesi sınırsızdı.** `/metrics` etiketi istek yolunu taşıdığı
   için rastgele yol deneyen bir tarayıcı her yol için kalıcı bir seri bırakırdı.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from app.core.logging import redact_secrets
from app.core.metrics import MAX_SERIES_PER_METRIC, MetricsRegistry
from app.core.middleware import metric_path
from app.services.account import AccountPool
from app.upstream.curl_runtime import apply_curl_to_settings
from app.upstream.session_capture import apply_cookie_to_settings
from tests.conftest import make_settings


def _jwt(tag: str = "taze", exp_offset: int = 3600) -> str:
    def segment(payload: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    return (
        f"{segment({'alg': 'HS256'})}."
        f"{segment({'sub': 'u', 'tag': tag, 'exp': int(time.time()) + exp_offset})}."
        "signature"
    )


# ------------------------------------------- 1) boş havuz kendini doldurur
def test_empty_pool_is_seeded_when_credentials_arrive_at_runtime():
    settings = make_settings()  # kimlik yok → havuz boş
    pool = AccountPool(settings)
    assert pool.size == 0

    # Telefondan gelen çerez: ayarlara yazılır, sonra havuza bildirilir.
    apply_cookie_to_settings(settings, f"arena-auth-prod-v1.0={_jwt()}; __cf_bm=bm")
    changed = pool.reload_credentials(settings)

    assert pool.size == 1, "kimlik geldiği hâlde havuz boş kaldı"
    assert list(changed) == ["hesap-1"]
    picked = pool.pick()
    assert picked is not None
    assert picked.cookie.strip(), "havuzdaki hesap kimliksiz"
    # İkinci çağrı yeni hesap uydurmamalı.
    assert pool.reload_credentials(settings) == {}


def test_empty_pool_without_credentials_stays_empty():
    settings = make_settings()
    pool = AccountPool(settings)
    assert pool.reload_credentials(settings) == {}
    assert pool.size == 0


def test_runtime_curl_swap_seeds_an_empty_pool():
    """`/upstream/curl` de aynı yolu kullanır: boş havuz sonrası istek kimlikli gitsin."""
    settings = make_settings()
    pool = AccountPool(settings)
    curl = (
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: arena-auth-prod-v1.0={_jwt()}' --data-raw '{{}}'"
    )
    result = apply_curl_to_settings(settings, curl)
    assert "upstream_cookie" in result["fields"]

    pool.reload_credentials(settings)
    assert pool.size == 1
    assert settings.upstream_cookie.strip()


# ------------------------------------------- 2) çerez bearer token'ı ezer
def test_new_cookie_replaces_stale_access_token():
    stale = _jwt("eski")
    settings = make_settings(upstream_access_token=stale, upstream_cookie="eski=1")
    fresh = _jwt("yeni")

    changed = apply_cookie_to_settings(settings, f"arena-auth-prod-v1.0={fresh}")

    assert "upstream_cookie" in changed
    assert "upstream_access_token" in changed
    assert settings.upstream_access_token == fresh, "eski token header'da kalırdı"


def test_cookie_without_token_leaves_explicit_token_alone():
    """Token'ı çerezden bağımsız veren kurulumlar bozulmamalı."""
    token = _jwt("elle")
    settings = make_settings(upstream_access_token=token, upstream_cookie="eski=1")

    changed = apply_cookie_to_settings(settings, "sessionid=abc123; tema=koyu")

    assert "upstream_cookie" in changed
    assert "upstream_access_token" not in changed
    assert settings.upstream_access_token == token


# ------------------------------------------- 3) log'da token maskesi
@pytest.mark.parametrize(
    "text",
    [
        '{"error":"invalid token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1In0.c2lnbmF0dXJl"}',
        "Authorization: Bearer sk-live-abcdef1234567890",
    ],
)
def test_redact_secrets_masks_tokens_in_free_text(text):
    redacted = redact_secrets(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "sk-live-abcdef1234567890" not in redacted
    assert "***" in redacted


def test_redact_secrets_keeps_normal_text_intact():
    text = "Upstream 401 döndü: {\"message\":\"User not found\"} (hesap: hesap-1)"
    assert redact_secrets(text) == text


# ------------------------------------------- 4) metrik kardinalitesi
def test_metric_path_collapses_unknown_routes():
    assert metric_path("/v1/chat/completions") == "/v1/chat/completions"
    assert metric_path("/v1/admin/upstream/cookie") == "/v1/admin/*"
    assert metric_path("/wp-admin.php") == "other"
    assert metric_path("/rastgele-123") == "other"


def test_metric_registry_caps_series_per_metric():
    registry = MetricsRegistry()
    for index in range(MAX_SERIES_PER_METRIC + 25):
        registry.inc("deneme_total", labels={"path": f"/{index}"})

    rendered = registry.render()
    assert rendered.count('deneme_total{') == MAX_SERIES_PER_METRIC
    assert registry.dropped_series() >= 25
    assert "apiwrapper_metrics_series_dropped" in rendered


def test_unhandled_requests_use_normalized_path_and_record_latency(monkeypatch):
    """İstisna yolundaki erişim metrikleri de sınırlı kardinalitede kalmalı."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.core.middleware as middleware

    registry = MetricsRegistry()
    monkeypatch.setattr(middleware, "metrics", registry)
    app = FastAPI()
    app.add_middleware(middleware.AccessLogMiddleware)

    @app.get("/dynamic/{value}")
    async def fail(value: str):
        raise RuntimeError(f"failure for {value}")

    secret_path = "/dynamic/secret-path-identifier"
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(secret_path)

    assert response.status_code == 500
    rendered = registry.render()
    assert "secret-path-identifier" not in rendered
    assert 'apiwrapper_requests_total{path="other",status="500"} 1' in rendered
    assert 'apiwrapper_request_duration_seconds_count{path="other"} 1' in rendered
