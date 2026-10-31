"""Telegram botunun ayrı workersız Web Service yaşam döngüsü testleri."""

from __future__ import annotations

import asyncio
import threading

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import _embedded_telegram_settings, create_app
from app.telegram.bot import TelegramSettings
from tests.conftest import make_settings


def test_embedded_bot_reuses_web_service_port_and_first_api_key(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.delenv("WRAPPER_BASE_URL", raising=False)
    monkeypatch.setitem(TelegramSettings.model_config, "env_ignore_empty", False)
    monkeypatch.setenv("WRAPPER_API_KEY", "")

    telegram = _embedded_telegram_settings(
        make_settings(port=19001, api_keys=["sk-first", "sk-second"])
    )

    assert telegram is not None
    assert telegram.wrapper_base_url == "http://127.0.0.1:19001"
    assert telegram.wrapper_api_key == "sk-first"


def test_embedded_bot_starts_and_stops_with_web_service(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.delenv("WRAPPER_BASE_URL", raising=False)
    monkeypatch.delenv("WRAPPER_API_KEY", raising=False)
    started = threading.Event()
    stopped = threading.Event()
    seen: dict[str, str] = {}

    async def fake_run(settings):
        seen["base_url"] = settings.wrapper_base_url
        seen["api_key"] = settings.wrapper_api_key
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(main_module, "run_from_settings", fake_run)
    app = create_app(make_settings(port=19002))

    with TestClient(app) as client:
        assert started.wait(2)
        assert client.get("/healthz").status_code == 200
    assert stopped.wait(2)
    assert seen == {"base_url": "http://127.0.0.1:19002", "api_key": "sk-test-key"}
