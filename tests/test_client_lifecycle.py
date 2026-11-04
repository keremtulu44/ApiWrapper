"""HTTP client yenileme ve arka plan görevi yaşam döngüsü regresyonları."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from app.services.account import Account
from app.upstream.client import UpstreamClient
from tests.conftest import make_settings


class ControlledStream(httpx.AsyncByteStream):
    """Reset anında hâlâ açık olan bir upstream yanıtını temsil eder."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"ilk-parca"
        await self.release.wait()
        yield b"ikinci-parca"

    async def aclose(self) -> None:
        self.release.set()


@pytest.mark.asyncio
@respx.mock
async def test_credential_reset_retires_client_after_active_stream_finishes():
    settings = make_settings(
        upstream_cookie="session=current",
        deployment_id_refresh_enabled=False,
    )
    account = Account.from_env(settings, 1)
    upstream = UpstreamClient(settings)
    await upstream.startup()
    body = ControlledStream()
    respx.post(settings.stream_url("chat-1")).mock(
        return_value=httpx.Response(200, stream=body)
    )

    try:
        async with upstream.stream_completion(
            "chat-1", {"message": "test"}, account=account
        ) as chunks:
            old_client = upstream._account_clients[account.slot]
            iterator = chunks.__aiter__()
            assert await iterator.__anext__() == b"ilk-parca"

            # Saatlik token refresh'i tam akış sürerken client'ları yeniler.
            assert await upstream.reset_account_clients() == 1
            assert not old_client.is_closed, "aktif akışın client'ı hemen kapanmamalı"
            replacement = upstream._client_for(account)
            assert replacement is not old_client, "yeni istekler taze client'a geçmeli"

            body.release.set()
            assert await iterator.__anext__() == b"ikinci-parca"
            with pytest.raises(StopAsyncIteration):
                await iterator.__anext__()

        assert old_client.is_closed, "son aktif istek bitince eski client kapanmalı"
        assert old_client not in upstream._retiring_clients
        assert not replacement.is_closed
    finally:
        await upstream.shutdown()


@pytest.mark.asyncio
async def test_set_cookie_schedules_free_render_credential_sync(monkeypatch):
    from app.upstream import state_store

    synced = asyncio.Event()
    snapshots: list[str] = []

    async def fake_sync(settings) -> bool:
        snapshots.append(settings.upstream_cookie)
        synced.set()
        return True

    monkeypatch.setattr(state_store, "sync_render_env", fake_sync)
    settings = make_settings(
        upstream_cookie="session=old",
        render_api_key="render-test-key",
        render_service_id="srv-test",
    )
    upstream = UpstreamClient(settings)
    await upstream.startup()
    try:
        http = upstream.client
        http.cookies.set("session", "rotated", domain="upstream.test", path="/")
        response = httpx.Response(
            200, headers={"set-cookie": "session=rotated; Path=/"}
        )

        upstream._ingest_set_cookie(response, http, None)
        await asyncio.wait_for(synced.wait(), timeout=1)
        task = upstream._render_sync_task
        assert task is not None
        await task

        assert settings.upstream_cookie == "session=rotated"
        assert snapshots == ["session=rotated"]
    finally:
        await upstream.shutdown()


@pytest.mark.asyncio
async def test_token_refresher_checks_immediately_after_startup(monkeypatch):
    from app.services import token_refresh

    checked = asyncio.Event()
    calls = 0

    async def fake_refresh(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        checked.set()
        return None

    monkeypatch.setattr(token_refresh, "refresh_if_needed", fake_refresh)
    task = asyncio.create_task(
        token_refresh.run_token_refresher(
            make_settings(), interval_seconds=3600
        )
    )
    try:
        await asyncio.wait_for(checked.wait(), timeout=1)
        assert calls == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_failed_render_sync_is_retried_and_latest_credentials_win(monkeypatch):
    """Tek seferlik Render hatası, sonraki token rotasyonuna bırakılmamalı.

    Free Render'da disk kalıcı değil: başarısız env yazımı restart'ta tazelenmiş
    oturumun kaybolması demek. İşçi artan aralıkla yeniden denemeli ve denemede
    **en güncel** ayarları okumalı.
    """
    from app.upstream import client as client_module
    from app.upstream import state_store

    monkeypatch.setattr(
        client_module, "_RENDER_SYNC_RETRY_INITIAL_SECONDS", 0.0, raising=True
    )
    monkeypatch.setattr(
        client_module, "_RENDER_SYNC_RETRY_MAX_SECONDS", 0.0, raising=True
    )

    attempts: list[str] = []
    done = asyncio.Event()

    async def flaky_sync(settings) -> bool:
        attempts.append(settings.upstream_cookie)
        if len(attempts) == 1:
            return False  # ilk deneme başarısız (geçici Render 5xx)
        done.set()
        return True

    monkeypatch.setattr(state_store, "sync_render_env", flaky_sync)
    settings = make_settings(
        upstream_cookie="session=old",
        render_api_key="render-test-key",
        render_service_id="srv-test",
    )
    upstream = UpstreamClient(settings)
    await upstream.startup()
    try:
        upstream._schedule_render_env_sync()
        # İlk deneme başarısız olduktan sonra kimlik döndü.
        settings.upstream_cookie = "session=rotated"
        await asyncio.wait_for(done.wait(), timeout=3)
        task = upstream._render_sync_task
        assert task is not None
        await asyncio.wait_for(task, timeout=3)
    finally:
        await upstream.shutdown()

    assert len(attempts) >= 2, "başarısız yazım yeniden denenmedi"
    assert attempts[-1] == "session=rotated", "tekrar deneme eski kimlikle yapıldı"
