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
    assert request["headers"]["Authorization"] == "Bearer sk-test-key"
    # Probe, istek sırasında tanınabilsin diye kaynağını bildirir.
    assert request["headers"]["x-apiwrapper-source"] == "keepalive"
    assert request["json"]["model"] == "max"
    assert request["json"]["stream"] is False
    assert request["json"]["messages"][0]["role"] == "user"


def test_keepalive_starts_and_stops_with_web_service(monkeypatch):
    started = threading.Event()
    stopped = threading.Event()
    seen: dict[str, float] = {}

    async def fake_run(settings, alerter=None, **wiring):
        seen["interval"] = settings.upstream_keepalive_interval_seconds
        seen["alerter"] = 1.0 if alerter is not None else 0.0
        # Probe, servisle aynı sırayı ve sağlık kaydını paylaşmalı.
        seen["shared_queue"] = 1.0 if wiring["queue"] is app.state.queue else 0.0
        seen["shared_health"] = 1.0 if wiring["health"] is app.state.upstream_health else 0.0
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
    assert seen["shared_queue"] == 1.0
    assert seen["shared_health"] == 1.0


def test_each_account_gets_its_own_keepalive_loop(monkeypatch):
    """İki hesapta iki döngü: her biri kendi hesabına sabitli, kendi sırası ve kaydıyla."""
    started = threading.Event()
    loops: list[dict[str, Any]] = []

    async def fake_run(settings, alerter=None, **wiring):
        loops.append(wiring)
        if len(loops) == 2:
            started.set()
        await asyncio.Future()

    monkeypatch.setattr(main_module, "run_upstream_keepalive", fake_run)
    app = create_app(
        make_settings(
            upstream_cookie="A",
            upstream_cookie_2="B",
            upstream_keepalive_interval_seconds=3600,
        )
    )

    with TestClient(app):
        assert started.wait(2)
        lanes = app.state.lanes.lanes
        assert [loop["account_slot"] for loop in loops] == [1, 2]
        assert [loop["scope"] for loop in loops] == ["hesap-1", "hesap-2"]
        assert [loop["queue"] for loop in loops] == [lane.queue for lane in lanes]
        assert [loop["health"] for loop in loops] == [lane.health for lane in lanes]
        # İlk probe'lar bir dakika arayla; her döngü bildirimine hesap notu ekleyebilir.
        assert [loop["start_delay"] for loop in loops] == [0.0, 60.0]
        assert all(callable(loop["notes"]) for loop in loops)


def test_single_account_keepalive_loop_is_not_pinned(monkeypatch):
    started = threading.Event()
    loops: list[dict[str, Any]] = []

    async def fake_run(settings, alerter=None, **wiring):
        loops.append(wiring)
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(main_module, "run_upstream_keepalive", fake_run)
    app = create_app(
        make_settings(upstream_cookie="A", upstream_keepalive_interval_seconds=3600)
    )

    with TestClient(app):
        assert started.wait(2)
        assert len(loops) == 1
        assert loops[0]["account_slot"] is None
        assert loops[0]["scope"] == ""
        assert loops[0]["notes"] is None
        assert loops[0]["start_delay"] == 0.0
