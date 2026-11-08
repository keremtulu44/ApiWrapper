"""Kimlik döndüğünde CAPTCHA üretim bağlamı da yenilenmeli.

Canlı belirtinin okunuşu: loglarda **otomatik** istek başarılı, hemen ardından
kullanıcının isteği (ve tekrar denemesi) CAPTCHA nedeniyle 403 almış. Kodda bu
sırayı üretebilecek kanıtlanmış bir asimetri vardı:

* Yönetici uçları (`/upstream/refresh`, `/upstream/cookie`, `/upstream/curl`)
  kimliği yeniler **ve** `recaptcha.invalidate()` çağırıyordu.
* Otomatik yollar — arka plan yenileyici, 401 sonrası tek deneme, upstream'in
  `Set-Cookie` ile oturumu çevirmesi — yalnız `reset_account_clients()`
  yapıyordu. Yani HTTP istemi taze çerezle gider, CAPTCHA token'ı üreten
  tarayıcı sayfası **ölü oturumun** çerezleriyle ayakta kalırdı.

Bu modül o bağlantıyı (ve `rejected` sayacının kimlik dönüşüyle kirlenmemesini)
kilitler. Gerçek hedef/gerçek token kullanılmaz; çerez ve JWT'ler sentetiktir.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import httpx
import pytest

from app.main import create_app
from app.services.recaptcha.base import RecaptchaProvider
from app.services.recaptcha.browser import BrowserRecaptchaProvider
from app.services.recaptcha.external import ExternalRecaptchaProvider
from app.services.recaptcha.static import NoopRecaptchaProvider, StaticRecaptchaProvider
from app.upstream.client import UpstreamClient
from tests.conftest import TEST_API_KEY, make_settings


def _fake_jwt(**claims: Any) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = {"exp": int(time.time()) + 3600, **claims}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{header}.{body}.signature"


class _Recorder:
    """`reset_context` çağrılarını sayan sahte dinleyici."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


# ----------------------------------------------------- UpstreamClient kancası
def test_reset_account_clients_notifies_identity_listeners() -> None:
    async def run() -> None:
        upstream = UpstreamClient(make_settings(upstream_cookie="session=old"))
        await upstream.startup()
        recorder = _Recorder()
        upstream.add_identity_listener(recorder)
        try:
            assert await upstream.reset_account_clients() == 0
        finally:
            await upstream.shutdown()
        assert recorder.calls == 1

    asyncio.run(run())


def test_listener_registration_is_idempotent_and_error_tolerant() -> None:
    boom = AssertionError("dinleyici patladı")

    def failing() -> None:
        raise boom

    async def run() -> None:
        upstream = UpstreamClient(make_settings())
        await upstream.startup()
        recorder = _Recorder()
        upstream.add_identity_listener(recorder)
        upstream.add_identity_listener(recorder)
        upstream.add_identity_listener(failing)
        try:
            assert await upstream.reset_account_clients() == 0
        finally:
            await upstream.shutdown()
        assert recorder.calls == 1, "aynı dinleyici iki kez kaydedilmemeli"

    asyncio.run(run())  # hata fırlatmaz: dinleyici arızası isteği kırmaz


def test_rotated_session_cookie_from_upstream_resets_captcha_context() -> None:
    """`Set-Cookie` erişim token'ını çevirdiyse token bağlamı da sıfırlanmalı."""

    async def run() -> None:
        old_token = _fake_jwt(sub="user-1")
        new_token = _fake_jwt(sub="user-1", iat=int(time.time()))
        settings = make_settings(upstream_cookie=f"sb-session={old_token}")
        upstream = UpstreamClient(settings)
        await upstream.startup()
        recorder = _Recorder()
        upstream.add_identity_listener(recorder)
        try:
            http = upstream.client
            http.cookies.set("sb-session", new_token, domain="upstream.test", path="/")
            response = httpx.Response(
                200, headers={"set-cookie": f"sb-session={new_token}; Path=/"}
            )
            upstream._ingest_set_cookie(response, http, None)
            assert recorder.calls == 1

            # Aynı oturum içinde art arda `Set-Cookie` tarayıcı bağlamını
            # her yanıtta sıfırlamamalı (ilk token süresini katlardı).
            third = _fake_jwt(sub="user-1", iat=int(time.time()) + 1)
            http.cookies.set("sb-session", third, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={third}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1, "bildirim kısılmalı (throttle)"
        finally:
            await upstream.shutdown()

    asyncio.run(run())


def test_unrelated_set_cookie_does_not_reset_the_token_context() -> None:
    """Yalnız analitik/CF çerezi tazelenirse token bağlamı boşuna çöpe gitmez."""

    async def run() -> None:
        token = _fake_jwt(sub="user-1")
        settings = make_settings(upstream_cookie=f"sb-session={token}; __cf_bm=old")
        upstream = UpstreamClient(settings)
        await upstream.startup()
        recorder = _Recorder()
        upstream.add_identity_listener(recorder)
        try:
            http = upstream.client
            http.cookies.set("__cf_bm", "rotated", domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": "__cf_bm=rotated; Path=/"}),
                http,
                None,
            )
        finally:
            await upstream.shutdown()
        assert recorder.calls == 0

    asyncio.run(run())


# ------------------------------------------------------------ sağlayıcı davranışı
def test_base_reset_context_is_a_noop_hook() -> None:
    class _Provider(RecaptchaProvider):
        async def _produce_token(self) -> str:  # pragma: no cover - kullanılmaz
            return "token"

    provider = _Provider(make_settings())
    assert provider.reset_context() is None
    provider.invalidate()
    assert provider.stats["rejected"] == 1


@pytest.mark.parametrize(
    "provider_name", ["static", "noop", "external"]
)
def test_non_browser_providers_survive_context_reset(provider_name: str) -> None:
    settings = make_settings(
        recaptcha_provider=provider_name,
        recaptcha_static_token="static-token",
        recaptcha_external_url="https://solver.test/token" if provider_name == "external" else "",
    )
    provider: Any
    provider = {
        "static": StaticRecaptchaProvider,
        "noop": NoopRecaptchaProvider,
        "external": ExternalRecaptchaProvider,
    }[provider_name](settings)
    assert provider.reset_context() is None
    assert provider.stats["rejected"] == 0


def test_browser_context_reset_drops_an_unconsumed_captured_token() -> None:
    settings = make_settings(recaptcha_provider="browser", recaptcha_site_key="6Lfake")
    provider = BrowserRecaptchaProvider(settings)
    provider._captured_live_token = "one-use-token"
    provider._reset_warm_page = False

    provider.reset_context()

    assert provider._captured_live_token is None, "bayat token bir sonraki isteğe sızmasın"
    assert provider._reset_warm_page is True
    assert provider.stats["rejected"] == 0, "kimlik dönüşü token reddi sayılmamalı"


def test_browser_invalidate_still_counts_a_rejection_and_resets_context() -> None:
    settings = make_settings(recaptcha_provider="browser", recaptcha_site_key="6Lfake")
    provider = BrowserRecaptchaProvider(settings)
    provider._captured_live_token = "one-use-token"

    provider.invalidate()

    assert provider.stats["rejected"] == 1
    assert provider._captured_live_token is None
    assert provider._reset_warm_page is True


# ------------------------------------------------------------- uygulama bağlantısı
def test_application_wires_identity_rotation_to_the_provider() -> None:
    from fastapi.testclient import TestClient

    settings = make_settings(
        recaptcha_provider="browser",
        recaptcha_site_key="6Lfake",
        upstream_cookie="sb-session=opaque",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        upstream: UpstreamClient = app.state.upstream
        provider: BrowserRecaptchaProvider = app.state.recaptcha
        assert upstream._identity_listeners, "kimlik dönüşü dinleyicisi bağlanmadı"
        assert provider._reset_warm_page is False

        provider._captured_live_token = "one-use-token"
        for listener in list(upstream._identity_listeners):
            listener()
        assert provider._captured_live_token is None
        assert provider._reset_warm_page is True
        assert provider.stats["rejected"] == 0


# ------------------------------------------- yarım uygulanmış kimlik (Authorization/Cookie)
def test_refresh_warning_when_cookie_cannot_be_rewritten(capsys) -> None:
    """Taze `Authorization` + tanınmayan çerez biçimi = görünür olmalı, sessiz değil."""
    from app.upstream.refresh import RefreshOutcome, apply_refresh

    old_token = _fake_jwt(sub="user-1")
    new_token = _fake_jwt(sub="user-1", iat=int(time.time()) + 5)
    settings = make_settings(
        # Oturum JSON'u taşımayan opak bir çerez: `rewrite_session_cookie`
        # dokunmaz ve kimliğin iki yarısı farklı kuşakta kalır.
        upstream_cookie=f"opaque-blob={old_token[:20]}",
        upstream_access_token=old_token,
    )
    changed = apply_refresh(
        settings,
        RefreshOutcome(ok=True, access_token=new_token, refresh_token="fake-rt", expires_in=3600),
    )
    log = capsys.readouterr().out

    assert "upstream_access_token" in changed and "upstream_cookie" not in changed
    assert settings.upstream_access_token == new_token
    assert settings.upstream_cookie == f"opaque-blob={old_token[:20]}"
    assert "upstream_refresh_cookie_not_rewritten" in log
    # Token değerleri loga sızmaz.
    assert new_token not in log and old_token not in log
