"""Sohbet yeniden kullanımı ve rotasyon testleri.

OpenAI-uyumlu istemciler (Continue, OpenAI SDK) şema dışı `conversation_id`
gönderemediği için sohbet, API anahtarı parmak izine bağlanır; eşik aşılınca
yeni upstream sohbetine geçilir.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.security import client_fingerprint
from app.main import create_app
from app.services.session_manager import SessionManager
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


def chat_ids_from(route) -> list[str]:
    """Upstream'e giden isteklerin URL'indeki chat_id'leri sırayla döndürür."""
    return [call.request.url.path.rsplit("/", 1)[-1] for call in route.calls]


def chat_body() -> dict:
    return {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


# ------------------------------------------------------------- parmak izi
def test_client_fingerprint_is_stable_and_keyed():
    assert client_fingerprint("sk-a") == client_fingerprint("sk-a")
    assert client_fingerprint("sk-a") != client_fingerprint("sk-b")
    assert client_fingerprint("sk-a").startswith("cli_")


def test_client_fingerprint_does_not_leak_the_key():
    fp = client_fingerprint("sk-super-secret-value")
    assert "super" not in fp and "secret" not in fp


def test_client_fingerprint_distinguishes_keys_that_share_a_mask():
    """mask_value yalnız ilk 3 + son 2 karakteri tutar; parmak izi çakışmamalı."""
    a, b = "sk-aaaaaaaaaa12", "sk-bbbbbbbbbb12"
    from app.core.logging import mask_value

    assert mask_value(a) == mask_value(b)  # aynı maske
    assert client_fingerprint(a) != client_fingerprint(b)  # farklı kimlik


def test_client_fingerprint_anonymous():
    assert client_fingerprint(None) == "anonymous"
    assert client_fingerprint("anonymous") == "anonymous"


# ------------------------------------------------------- reuse davranışı
@pytest.mark.asyncio
async def test_reuse_keeps_one_chat_per_client_identity():
    manager = SessionManager(make_settings(session_reuse=True, session_stateless=True))
    ids = [(await manager.acquire(None, "cli_1")).chat_id for _ in range(5)]
    assert len(set(ids)) == 1
    session = await manager.acquire(None, "cli_1")
    assert session.message_count == 6


@pytest.mark.asyncio
async def test_reuse_overrides_stateless_flag():
    """`session_stateless=True` iken bile reuse çalışmalı (devam eden istemciler için)."""
    manager = SessionManager(make_settings(session_reuse=True, session_stateless=True))
    first = await manager.acquire(None, "cli_1")
    second = await manager.acquire(None, "cli_1")
    assert first.chat_id == second.chat_id


@pytest.mark.asyncio
async def test_reuse_separates_clients():
    manager = SessionManager(make_settings(session_reuse=True))
    a = await manager.acquire(None, "cli_1")
    b = await manager.acquire(None, "cli_2")
    assert a.chat_id != b.chat_id


@pytest.mark.asyncio
async def test_reuse_without_identity_falls_back_to_stateless():
    """Kimlik yoksa (örn. AUTH_DISABLED) her istek yeni sohbet alır."""
    manager = SessionManager(make_settings(session_reuse=True))
    first = await manager.acquire(None, None)
    second = await manager.acquire(None, None)
    assert first.chat_id != second.chat_id


@pytest.mark.asyncio
async def test_explicit_conversation_id_wins_over_identity():
    manager = SessionManager(make_settings(session_reuse=True))
    a = await manager.acquire("conv-1", "cli_1")
    b = await manager.acquire("conv-1", "cli_2")
    c = await manager.acquire("conv-2", "cli_1")
    assert a.chat_id == b.chat_id
    assert a.chat_id != c.chat_id


# ------------------------------------------------------------- rotasyon
@pytest.mark.asyncio
async def test_rotation_after_message_threshold():
    manager = SessionManager(
        make_settings(session_reuse=True, session_rotate_after_messages=3)
    )
    ids = [(await manager.acquire(None, "cli_1")).chat_id for _ in range(7)]
    # 3 mesajda bir yeni sohbet: 7 istek -> 3 ayrı sohbet
    assert len(set(ids)) == 3
    assert manager.rotations == 2


@pytest.mark.asyncio
async def test_rotation_disabled_when_threshold_is_zero():
    manager = SessionManager(
        make_settings(session_reuse=True, session_rotate_after_messages=0)
    )
    ids = [(await manager.acquire(None, "cli_1")).chat_id for _ in range(10)]
    assert len(set(ids)) == 1
    assert manager.rotations == 0


@pytest.mark.asyncio
async def test_rotation_after_age_threshold():
    manager = SessionManager(
        make_settings(
            session_reuse=True,
            session_rotate_after_messages=0,
            session_rotate_after_seconds=0.05,
        )
    )
    first = await manager.acquire(None, "cli_1")
    import asyncio

    await asyncio.sleep(0.07)
    second = await manager.acquire(None, "cli_1")
    assert first.chat_id != second.chat_id
    assert manager.rotations == 1


@pytest.mark.asyncio
async def test_invalidate_by_client_identity():
    manager = SessionManager(make_settings(session_reuse=True))
    first = await manager.acquire(None, "cli_1")
    await manager.invalidate("", client_identity="cli_1")
    second = await manager.acquire(None, "cli_1")
    assert first.chat_id != second.chat_id


# --------------------------------------------------- eski modlar korunuyor
@pytest.mark.asyncio
async def test_stateless_with_conversation_id_still_unique():
    """Eski davranış: stateless iken conversation_id de sohbeti sabitlemez."""
    manager = SessionManager(make_settings(session_stateless=True))
    first = await manager.acquire("conv")
    second = await manager.acquire("conv")
    assert first.chat_id != second.chat_id


# ------------------------------------------------------------ uçtan uca
@respx.mock
def test_same_api_key_shares_one_upstream_chat_when_reuse_enabled():
    app = create_app(make_settings(session_reuse=True))
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("selam"))
    )
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(3):
            assert test_client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert len(set(chat_ids_from(route))) == 1


@respx.mock
def test_each_request_opens_new_chat_when_reuse_disabled():
    app = create_app(make_settings(session_reuse=False))
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("selam"))
    )
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(3):
            test_client.post("/v1/chat/completions", json=chat_body())
    assert len(set(chat_ids_from(route))) == 3


@respx.mock
def test_upstream_chat_rotates_after_threshold_end_to_end():
    app = create_app(
        make_settings(session_reuse=True, session_rotate_after_messages=2)
    )
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("selam"))
    )
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(5):
            test_client.post("/v1/chat/completions", json=chat_body())
    ids = chat_ids_from(route)
    assert len(set(ids)) == 3  # 2'şer mesajlık 3 sohbet
    assert ids[0] == ids[1] != ids[2] == ids[3] != ids[4]


@respx.mock
def test_streaming_requests_also_share_the_chat():
    app = create_app(make_settings(session_reuse=True))
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("selam"))
    )
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        for _ in range(2):
            test_client.post(
                "/v1/chat/completions", json={**chat_body(), "stream": True}
            )
    assert len(set(chat_ids_from(route))) == 1


def test_admin_config_exposes_session_rotation():
    app = create_app(make_settings(session_reuse=True, session_rotate_after_messages=42))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        body = test_client.get("/v1/admin/config").json()
    assert body["session"]["reuse"] is True
    assert body["session"]["rotate_after_messages"] == 42
    assert "rotations_total" in body["session"]
