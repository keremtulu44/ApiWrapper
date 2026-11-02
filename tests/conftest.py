"""Ortak test fixture'ları."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.telegram.bot import TelegramSettings

# Testler her makinede aynı sonucu vermeli. `Settings` varsayılan olarak depo
# kökündeki `.env` dosyasını VE ortam değişkenlerini okur; geliştiricinin `.env`'i
# (örn. UPSTREAM_STREAM_PATH, SESSION_REUSE, AUTH_DISABLED) test ayarlarının
# üzerine sızıp testleri bozuyordu. İkisi de burada kapatılır.
Settings.model_config["env_file"] = None
Settings.model_config["env_ignore_empty"] = True
for _field in Settings.model_fields:
    os.environ.pop(_field.upper(), None)

# Gömülü Telegram botu da ayrı Settings sınıfını okur; geliştiricinin gerçek
# tokenıyla testlerde ağa çıkmasını ve aynı env'lerden etkilenmesini önle.
TelegramSettings.model_config["env_file"] = None
TelegramSettings.model_config["env_ignore_empty"] = True
for _field in TelegramSettings.model_fields:
    os.environ.pop(_field.upper(), None)

TEST_API_KEY = "sk-test-key"
UPSTREAM_DOMAIN = "upstream.test"


def make_settings(**overrides) -> Settings:
    base = {
        "target_domain": UPSTREAM_DOMAIN,
        "api_keys": [TEST_API_KEY],
        "recaptcha_provider": "noop",
        "rate_limit_enabled": False,
        "log_json": True,
        "log_level": "WARNING",
        "models_file": "tests/data/models.yaml",
        "session_stateless": True,
        "upstream_http2": False,
        "retry_max_attempts": 1,
        "retry_base_delay": 0.01,
        "retry_max_delay": 0.02,
        "stream_idle_timeout": 5.0,
        "breaker_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def ai_stream(*chunks: str, finish: bool = True, usage: dict | None = None) -> bytes:
    """Vercel AI SDK data-stream gövdesi üretir."""
    lines = ['f:{"messageId":"msg-test"}']
    lines.extend(f"0:{json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks)
    if finish:
        payload = {
            "finishReason": "stop",
            "usage": usage or {"promptTokens": 11, "completionTokens": 7},
        }
        lines.append("e:" + json.dumps(payload))
        lines.append("d:" + json.dumps(payload))
    return ("\n".join(lines) + "\n").encode("utf-8")
