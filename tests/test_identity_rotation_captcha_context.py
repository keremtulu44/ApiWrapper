"""Kimlik döndüğünde CAPTCHA üretim bağlamı da yenilenmeli.

Kanıtlanan **kod** asimetrisi (canlı arızanın nedeni olduğu iddia edilmez):

* Yönetici uçları (`/upstream/refresh`, `/upstream/cookie`, `/upstream/curl`)
  kimliği yeniler **ve** `recaptcha.invalidate()` çağırıyordu.
* Otomatik yollar — arka plan yenileyicisi, 401 sonrası tek deneme, upstream'in
  `Set-Cookie` ile oturumu çevirmesi — yalnız `reset_account_clients()`
  yapıyordu. Yani HTTP istemi taze çerezle gider, token'ı üreten tarayıcı
  sayfası eski oturumun çerezleriyle ayakta kalabilirdi.

Çürütme (operatörün canlı gözlemi): ilk CAPTCHA reddinde `_events` zaten
`invalidate()` çağırıp sayfayı yeniden kuruyordu ve **ikinci deneme de 403
aldı**. Dolayısıyla bu bağlantı hijyen düzeltmesidir; gözlenen arızanın açıklaması
değildir. Ayrıca `Set-Cookie` logu tek başına erişim token'ının döndüğünü
kanıtlamaz — bu yüzden artık `upstream_identity_update` satırı
`access_token_rotated` bayrağını taşır.

Gerçek hedef/gerçek token kullanılmaz; çerez ve JWT'ler sentetiktir.
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
    """`reset_token_context` çağrılarını sayan sahte dinleyici."""

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
        upstream._identity_quiet_seconds = 0.0  # test: pencere beklenmesin
        try:
            http = upstream.client
            http.cookies.set("sb-session", new_token, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={new_token}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1

            # Aynı erişim token'ıyla gelen tekrarlı `Set-Cookie` gürültü üretmez.
            http.cookies.set("sb-session", new_token, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(
                    200,
                    headers={
                        "set-cookie": (
                            f"sb-session={new_token}; __cf_bm=rotated; Path=/"
                        )
                    },
                ),
                http,
                None,
            )
            assert recorder.calls == 1, "aynı token için bildirim yinelenmemeli"
        finally:
            await upstream.shutdown()

    asyncio.run(run())


def test_rapid_identity_rotations_are_merged_but_never_dropped() -> None:
    """Sessiz pencere değişimi **silmez**, pencere sonuna erteler."""

    async def run() -> None:
        token_a = _fake_jwt(sub="user-1")
        settings = make_settings(upstream_cookie=f"sb-session={token_a}")
        upstream = UpstreamClient(settings)
        await upstream.startup()
        recorder = _Recorder()
        upstream.add_identity_listener(recorder)
        upstream._identity_quiet_seconds = 0.2
        try:
            http = upstream.client
            token_b = _fake_jwt(sub="user-1", iat=1)
            http.cookies.set("sb-session", token_b, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={token_b}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1, "ilk dönüş gecikmeden teslim edilmeli"

            token_c = _fake_jwt(sub="user-1", iat=2)
            http.cookies.set("sb-session", token_c, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={token_c}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1, "ikinci dönüş pencere içinde birleştirilmeli"
            await asyncio.sleep(0.3)
            assert recorder.calls == 2, "birleştirilen bildirim atılmış"
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
def test_base_reset_token_context_is_a_noop_hook() -> None:
    class _Provider(RecaptchaProvider):
        async def _produce_token(self) -> str:  # pragma: no cover - kullanılmaz
            return "token"

    provider = _Provider(make_settings())
    assert provider.reset_token_context() is None
    provider.invalidate()
    assert provider.stats["rejected"] == 1


@pytest.mark.parametrize(
    "provider_name", ["static", "noop", "external"]
)
def test_non_browser_providers_survive_token_context_reset(provider_name: str) -> None:
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
    assert provider.reset_token_context() is None
    assert provider.stats["rejected"] == 0


def test_browser_context_reset_drops_an_unconsumed_captured_token() -> None:
    settings = make_settings(recaptcha_provider="browser", recaptcha_site_key="6Lfake")
    provider = BrowserRecaptchaProvider(settings)
    provider._captured_live_token = "one-use-token"
    provider._reset_warm_page = False

    provider.reset_token_context()

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


# -------------------------------------------------- tarayıcı bağlamının kapsamı
class _FakeContext:
    """Playwright `BrowserContext`'inin minimal taklidi (ad/value düzeyinde)."""

    def __init__(self, initial: list[dict[str, Any]]) -> None:
        self.cookies_list = initial
        self.removed: list[str] = []

    async def add_cookies(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            self.cookies_list = [
                c for c in self.cookies_list if c["name"] != item["name"]
            ]
            self.cookies_list.append(dict(item))

    async def remove_cookies(self, *, name: str, **_kw: Any) -> None:
        self.removed.append(name)
        self.cookies_list = [c for c in self.cookies_list if c["name"] != name]

    def names(self) -> list[str]:
        return sorted(c["name"] for c in self.cookies_list)


def test_reset_prunes_ghost_cookies_but_keeps_challenge_cookies() -> None:
    """`reset_token_context` gerçekten bağlamı temizler: bizim koyduklarımız silinir.

    Eski cURL'de olup yenisinde olmayan çerez, context'te kalırsa token eski
    oturumun iziyle üretilir. Sayfanın challenge ile kazandığı `cf_clearance`
    ise **korunur** — silmek her üretimde Cloudflare'ı yeniden tetiklerdi.
    """
    settings = make_settings(
        recaptcha_provider="browser",
        recaptcha_site_key="6Lfake",
        target_domain="svc.example.test",
        upstream_cookie="sb-session=new-value; theme=dark",
    )
    provider = BrowserRecaptchaProvider(settings)
    provider._context = _FakeContext(
        [
            {"name": "sb-session", "value": "stale-old", "domain": "svc.example.test"},
            {"name": "legacy-flag", "value": "1", "domain": "svc.example.test"},
            {"name": "cf_clearance", "value": "from-challenge", "domain": "svc.example.test"},
        ]
    )
    provider._injected_cookie_names = {"sb-session", "legacy-flag"}

    async def run() -> None:
        await provider._set_cookies_from_settings()

    asyncio.run(run())

    names = provider._context.names()
    # Yalnızca bizim enjekte ettiklerimiz silinir.
    assert provider._context.removed == ["legacy-flag"]
    assert "cf_clearance" in names, "challenge çerezi korunmalı"
    assert "legacy-flag" not in names
    injected = {c["name"]: c["value"] for c in provider._context.cookies_list}
    assert injected["sb-session"] == "new-value"
    assert provider._injected_cookie_names == {"sb-session", "theme"}


async def test_identity_update_reports_whether_the_access_token_rotated() -> None:
    """`Set-Cookie` geldi" ile "erişim token'ı döndü" artık ayrıştırılabilir.

    Bu ayrım olmadan "oturum döndü" iddiası logdan ne doğrulanır ne çürütülür;
    bu incelemede yanıltıcı olan tam olarak burasıydı. Karar `identity_update`
    üzerinde gözlenebilir; değerlerin kendisi hiçbir yerde tutulmaz.
    """
    old_token = _fake_jwt(sub="user-1")
    settings = make_settings(upstream_cookie=f"sb-session={old_token}; __cf_bm=old")
    upstream = UpstreamClient(settings)
    await upstream.startup()
    try:
        assert upstream.identity_update == {}
        http = upstream.client

        # 1) Yalnız CF çerezi tazeleniyor: oturum DÖNMEDİ.
        http.cookies.set("__cf_bm", "rotated", domain="upstream.test", path="/")
        upstream._ingest_set_cookie(
            httpx.Response(200, headers={"set-cookie": "__cf_bm=rotated; Path=/"}),
            http,
            None,
        )
        first = upstream.identity_update
        assert first["access_token_rotated"] is False
        assert first["refresh_token_rotated"] is False

        # 2) Şimdi gerçekten dönüyor.
        new_token = _fake_jwt(sub="user-1", iat=42)
        http.cookies.set("sb-session", new_token, domain="upstream.test", path="/")
        upstream._ingest_set_cookie(
            httpx.Response(200, headers={"set-cookie": f"sb-session={new_token}; Path=/"}),
            http,
            None,
        )
        second = upstream.identity_update
        assert second["access_token_rotated"] is True
        # Özet yalnız bayrak taşır: token değerlerinin hiçbiri saklanmaz/loglanmaz.
        assert set(second) == {
            "slot",
            "access_token_rotated",
            "refresh_token_rotated",
            "at",
        }
        assert new_token not in json.dumps(second) and old_token not in json.dumps(second)
    finally:
        await upstream.shutdown()


def test_diagnostics_expose_the_rotation_decision_without_values() -> None:
    """`/v1/admin/auth` rotasyon kararını gösterir, token değerini asla."""
    from fastapi.testclient import TestClient

    from tests.conftest import TEST_API_KEY

    old_token = _fake_jwt(sub="user-1")
    new_token = _fake_jwt(sub="user-1", iat=99)
    settings = make_settings(
        recaptcha_provider="noop", upstream_cookie=f"sb-session={old_token}"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        upstream = app.state.upstream
        http = upstream.client
        http.cookies.set("sb-session", new_token, domain="upstream.test", path="/")
        upstream._ingest_set_cookie(
            httpx.Response(200, headers={"set-cookie": f"sb-session={new_token}; Path=/"}),
            http,
            None,
        )
        response = client.get(
            "/v1/admin/auth", headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["identity_update"]["access_token_rotated"] is True
    body = json.dumps(payload)
    assert old_token not in body and new_token not in body
