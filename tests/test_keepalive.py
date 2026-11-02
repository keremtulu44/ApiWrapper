"""Opsiyonel upstream keepalive task'ının birim ve yaşam döngüsü testleri."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.services.keepalive as keepalive_module
from app.main import create_app
from app.services.keepalive import run_upstream_keepalive
from tests.conftest import make_settings


@pytest.mark.asyncio
async def test_keepalive_posts_short_nonstream_completion_to_loopback(monkeypatch):
    posted = asyncio.Event()
    calls: list[dict[str, Any]] = []
    init_kwargs: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            init_kwargs.update(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            calls.append({"url": url, **kwargs})
            posted.set()
            return FakeResponse()

    monkeypatch.setattr(keepalive_module.httpx, "AsyncClient", FakeClient)
    settings = make_settings(
        port=19003,
        upstream_keepalive_interval_seconds=0.01,
    )

    task = asyncio.create_task(run_upstream_keepalive(settings))
    await asyncio.wait_for(posted.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert init_kwargs["base_url"] == "http://127.0.0.1:19003"
    assert init_kwargs["trust_env"] is False
    assert calls
    request = calls[0]
    assert request["url"] == "/v1/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer sk-test-key"}
    assert request["json"]["model"] == "max"
    assert request["json"]["stream"] is False
    assert request["json"]["messages"][0]["role"] == "user"


def test_keepalive_starts_and_stops_with_web_service(monkeypatch):
    started = threading.Event()
    stopped = threading.Event()
    seen: dict[str, float] = {}

    async def fake_run(settings, alerter=None):
        seen["interval"] = settings.upstream_keepalive_interval_seconds
        seen["alerter"] = 1.0 if alerter is not None else 0.0
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(main_module, "run_upstream_keepalive", fake_run)
    app = create_app(make_settings(upstream_keepalive_interval_seconds=10800))

    with TestClient(app) as client:
        assert started.wait(2)
        assert client.get("/healthz").status_code == 200
    assert stopped.wait(2)
    assert seen["interval"] == 10800
