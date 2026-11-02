"""Uçtan uca ağ testinde yakalanan üç hatanın regresyon testleri.

1. **Ölü oturum "kota" gibi görünüyordu.** Tek hesap yapılandırılmışken 401 alınınca
   `_pick_account(exclude_slot=...)` kendi içinde `UpstreamQuotaExceeded` fırlatıyor,
   bu da `except UpstreamAuthRejected` bloğunun içinde orijinal 401'i eziyordu. İstemci
   "kota doldu, biraz sonra tekrar dene" (429) görüyordu; gerçek sebep oturumun ölmesi.
   Test, hesap havuzu **dolu** iken (cookie/token var) 401'in hâlâ
   `upstream_unauthorized` döndüğünü doğrular — eski testlerin hepsi boş havuzla
   koştuğu için bu hata görünmüyordu.

2. **Akışta bilinmeyen model 200 + boş gövde dönüyordu.** `stream_completion` bir async
   generator olduğu için `registry.resolve` ancak HTTP 200 ve başlıklar gittikten sonra
   çalışıyor, `RuntimeError: response already started` üretiyordu. Model artık yanıt
   başlamadan çözülüyor: temiz 404.

3. **`TARGET_DOMAIN` port içerirse çerez jar'ı eşleşmiyordu.** Çerez alanı `host:port`
   olarak yazıldığı için httpx onu hiç göndermiyordu; sessiz "Cookie yok" modu.
   Test, çerez alanının yalnızca ana makine adıyla yazıldığını doğrular.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from app.core.config import Settings
from app.upstream.client import UpstreamClient
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def _stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


def _account_settings(**overrides) -> Settings:
    """Havuzda tek bir **dolu** hesap olacak şekilde ayarlar üretir."""
    base = {"upstream_cookie": "arena-auth-prod-v1.0=taze-token"}
    base.update(overrides)
    return make_settings(**base)


# ------------------------------------------- 1) ölü oturum ≠ kota
@respx.mock
def test_single_configured_account_401_is_unauthorized_not_quota():
    """Havuz dolu olsa da 401 'kota' olarak raporlanmamalı (eski hata: 429)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(_account_settings())
    _stream_route(respx).mock(
        return_value=httpx.Response(401, content=b'{"message":"User not found"}')
    )

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        assert app.state.completion_service.accounts.size == 1
        response = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 502, response.text
    error = response.json()["error"]
    assert error["code"] == "upstream_unauthorized"
    assert "quota" not in error["code"]


@respx.mock
def test_dead_account_does_not_poison_the_pool_with_cooldown():
    """401 hesabı cezalandırmamalı: sonraki istek 'kota' duvarına çarpmamalı."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(_account_settings())
    route = _stream_route(respx)
    # Sıra: önce ölü oturum, sonra çerez tazelendi ve upstream yine çalışıyor.
    route.side_effect = [
        httpx.Response(401, content=b'{"message":"User not found"}'),
        httpx.Response(200, content=b"0:merhaba\n"),
    ]

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
        first = client.post("/v1/chat/completions", json=payload)
        second = client.post("/v1/chat/completions", json=payload)

    assert first.json()["error"]["code"] == "upstream_unauthorized"
    assert second.status_code == 200, second.text


# ------------------------------------------- 2) akışta model çözümü
@respx.mock
def test_stream_unknown_model_returns_clean_404_before_stream_starts():
    from fastapi.testclient import TestClient

    from app.main import create_app

    route = _stream_route(respx).mock(return_value=httpx.Response(200, content=b""))

    with TestClient(create_app(make_settings())) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "yok-boyle-model",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "model_not_found"
    # Model çözülemediyse upstream'e hiç gidilmemeli.
    assert route.call_count == 0


@respx.mock
def test_stream_known_model_still_streams():
    from fastapi.testclient import TestClient

    from app.main import create_app

    _stream_route(respx).mock(
        return_value=httpx.Response(200, content=b'0:merhaba\n2:[{"messageId":"m"}]\n')
    )
    with TestClient(create_app(make_settings())) as client:
        client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "merhaba" in body
    assert "[DONE]" in body


# ------------------------------------------- 3) çerez alanı ve port
@pytest.mark.parametrize(
    ("target_domain", "expected_domain"),
    [
        ("upstream.test", "upstream.test"),
        ("upstream.test:8443", "upstream.test"),
    ],
)
def test_cookie_jar_uses_host_only(target_domain, expected_domain):
    """Çerez alanı porta dahil edilmemeli; yoksa çerez hiç gönderilmez."""
    settings = make_settings(
        target_domain=target_domain,
        upstream_cookie="arena-auth-prod-v1.0=taze-token",
    )
    client = UpstreamClient(settings)._build_client(settings)

    async def scenario() -> None:
        try:
            cookie = client.cookies.get("arena-auth-prod-v1.0", domain=expected_domain)
            assert cookie == "taze-token"
            domains = {cookie.domain for cookie in client.cookies.jar}
            assert domains == {expected_domain}, domains
            # Gerçek istekte de başlığa dönüşmeli (port ile eşleşme bozulmasın).
            request = client.build_request("GET", f"{settings.base_url}/")
            assert "arena-auth-prod-v1.0=taze-token" in request.headers["cookie"]
        finally:
            await client.aclose()

    asyncio.run(scenario())
