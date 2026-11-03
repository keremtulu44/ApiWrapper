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
