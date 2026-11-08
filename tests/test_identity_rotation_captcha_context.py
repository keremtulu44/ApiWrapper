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

import ast
import asyncio
import base64
import json
import pathlib
import time
from typing import Any

import httpx
import pytest

import app.services.recaptcha.browser
from app.main import create_app
from app.services.recaptcha.base import RecaptchaProvider
from app.services.recaptcha.browser import BrowserRecaptchaProvider
from app.services.recaptcha.external import ExternalRecaptchaProvider
from app.services.recaptcha.static import NoopRecaptchaProvider, StaticRecaptchaProvider
from app.upstream.client import UpstreamClient
from app.upstream.exceptions import RecaptchaRejected
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
#
# Taklitler **yalnız Playwright'ın gerçekte yayınladığı yöntemi** tanımlar.
# `remove_cookies` diye bir yöntem yoktur (1.42 ve 1.49 wheel'lerinde doğrulandı);
# bağlam çerezleri `clear_cookies()` ile silinir ve filtreyi yalnız yeni
# sürümler kabul eder. Bu ikisini karıştıran test, kod gerçek tarayıcıda hiçbir
# şey yapmazken yeşil kalıyordu — aşağıdaki üç vaka o boşluğu kapatır.


class _ContextBase:
    def __init__(self, initial: list[dict[str, Any]]) -> None:
        self.cookies_list = [dict(item) for item in initial]
        self.clears: list[dict[str, Any]] = []

    async def add_cookies(self, items: list[dict[str, Any]]) -> None:
        for item in items:
            self.cookies_list = [
                c for c in self.cookies_list if c["name"] != item["name"]
            ]
            self.cookies_list.append(dict(item))

    def names(self) -> list[str]:
        return sorted(c["name"] for c in self.cookies_list)

    def value_of(self, name: str) -> Any:
        for item in self.cookies_list:
            if item["name"] == name:
                return item["value"]
        return None


class _FilteredContext(_ContextBase):
    """Playwright >=1.44: `clear_cookies(name=...)` destekli."""

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.cookies_list]

    async def clear_cookies(
        self,
        *,
        name: Any = None,
        domain: Any = None,
        path: Any = None,
    ) -> None:
        self.clears.append({"name": name, "domain": domain, "path": path})
        if name is None:
            self.cookies_list = []
            return
        self.cookies_list = [c for c in self.cookies_list if c["name"] != name]


class _LegacyContext(_ContextBase):
    """Playwright 1.42: `clear_cookies()` filtresiz — anahtar kelime kabul etmez."""

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.cookies_list]

    async def clear_cookies(self) -> None:
        self.clears.append({})
        self.cookies_list = []


class _NoCookieApiContext(_ContextBase):
    """Çerez silme yöntemi hiç olmayan bağlam (istisna değil, sayaç beklenir)."""


_INITIAL_JAR = [
    {"name": "sb-session", "value": "stale-old", "domain": "svc.example.test"},
    {"name": "legacy-flag", "value": "1", "domain": "svc.example.test"},
    {"name": "cf_clearance", "value": "from-challenge", "domain": "svc.example.test"},
]


def _browser_provider() -> BrowserRecaptchaProvider:
    settings = make_settings(
        recaptcha_provider="browser",
        recaptcha_site_key="6Lfake",
        target_domain="svc.example.test",
        upstream_cookie="sb-session=new-value; theme=dark",
    )
    provider = BrowserRecaptchaProvider(settings)
    provider._injected_cookie_names = {"sb-session", "legacy-flag"}
    return provider


def test_reset_prunes_ghost_cookies_but_keeps_challenge_cookies() -> None:
    """Filtreli API: yalnız bizim koyduklarımız silinir, `cf_clearance` kalır."""
    provider = _browser_provider()
    provider._context = _FilteredContext(_INITIAL_JAR)

    async def run() -> None:
        await provider._set_cookies_from_settings()

    asyncio.run(run())

    assert provider._context.clears == [{"name": "legacy-flag", "domain": None, "path": None}]
    names = provider._context.names()
    assert "cf_clearance" in names, "challenge çerezi korunmalı"
    assert "legacy-flag" not in names
    injected = {c["name"]: c["value"] for c in provider._context.cookies_list}
    assert injected["sb-session"] == "new-value"
    assert provider._injected_cookie_names == {"sb-session", "theme"}
    assert provider.stats["browser_cookie_prune_mode"] == "filtered"


def test_prune_still_works_on_playwright_without_name_filter() -> None:
    """Filtresiz API: anlık görüntü + geri yazma; yine de `cf_*` korunur.

    Bu vaka olmadan tek-imzaya kodlanmış budama, `playwright==1.42` kullanan
    kurulumlarda sessizce çalışmazdı.
    """
    provider = _browser_provider()
    provider._context = _LegacyContext(_INITIAL_JAR)

    async def run() -> None:
        await provider._prune_injected_cookies(["sb-session", "legacy-flag"])

    asyncio.run(run())

    context = provider._context
    assert context.clears == [{}], "tek filtresiz temizlik beklenirdi"
    names = context.names()
    assert "legacy-flag" not in names and "sb-session" not in names
    assert "cf_clearance" in names, "geri yazılanlar arasında kalmalı"
    assert provider._injected_cookie_names == set()
    assert provider.stats["browser_cookie_prune_mode"] == "rewritten"


def test_prune_reports_unsupported_instead_of_silently_doing_nothing() -> None:
    provider = _browser_provider()
    provider._context = _NoCookieApiContext(_INITIAL_JAR)

    async def run() -> None:
        await provider._prune_injected_cookies(["sb-session"])

    asyncio.run(run())

    assert provider.stats["browser_cookie_prune"] == "unsupported"
    # Silme yöntemi yoksa hiçbir çerez kaybolmaz (yanlışlıkla toplu temizlik yok).
    assert provider._context.names() == ["cf_clearance", "legacy-flag", "sb-session"]
    assert provider._injected_cookie_names == {"sb-session", "legacy-flag"}


def test_prune_only_uses_cookie_apis_playwright_actually_publishes() -> None:
    """Koruma testi: Playwright'ta olmayan bir metoda kodmak sessiz no-op'tur.

    Yorum satırları değil, **AST'deki nitelik erişimleri** incelenir: taklidin
    uydurma bir yöntemi tanımlayıp testi yeşile çekmesi burada işe yaramaz.
    """
    tree = ast.parse(
        pathlib.Path(app.services.recaptcha.browser.__file__).read_text(),
        filename="browser.py",
    )
    accessed = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    getattr_calls = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
    }
    looked_up = accessed | getattr_calls

    assert "remove_cookies" not in looked_up, "Playwright böyle bir yöntem yayınlamıyor"
    assert "clear_cookies" in looked_up, "bağlam çerezleri bununla silinir"


def test_prune_against_the_installed_playwright_package() -> None:
    """Tarayıcı extras kuruluysa yöntemi gerçek sınıftan doğrula."""
    playwright_api = pytest.importorskip("playwright.async_api")
    context_cls = playwright_api.BrowserContext
    assert not hasattr(context_cls, "remove_cookies")
    assert hasattr(context_cls, "clear_cookies")


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
            "reason",
            "status",
            "changed",
            "access_token_rotated",
            "refresh_token_rotated",
            "set_cookie_count",
            "set_cookie_names",
            "untracked_cookie_names",
            "at",
        }
        assert second["reason"] == "applied" and second["changed"] is True
        assert second["status"] == 200
        assert second["set_cookie_names"] == ["sb-session"]
        assert second["untracked_cookie_names"] == []
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

# -------------------------------------------------- bekleyen bildirimin akıbeti
def test_reset_account_clients_cancels_the_pending_flush() -> None:
    """Bekleyen bildirim teslim edilir **ve zamanlayıcı iptal edilir**.

    `reset_account_clients()` bekleyen kaydı yalnız referans düzeyinde
    temizliyordu; `loop.call_later` geri çağırması yaşadığı için aynı bildirim
    sonraki bir döngü turunda bir daha çalışıyordu (gereksiz sayfa yakma).
    """
    old_token = _fake_jwt(sub="user-1")
    settings = make_settings(upstream_cookie=f"sb-session={old_token}")
    upstream = UpstreamClient(settings)
    recorder = _Recorder()
    upstream.add_identity_listener(recorder)
    upstream._identity_quiet_seconds = 0.2

    async def run() -> None:
        await upstream.startup()
        try:
            http = upstream.client
            first = _fake_jwt(sub="user-1", iat=11)
            http.cookies.set("sb-session", first, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={first}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1, "ilk dönüş pencere dışında, gecikmesiz"

            second = _fake_jwt(sub="user-1", iat=12)
            http.cookies.set("sb-session", second, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers={"set-cookie": f"sb-session={second}; Path=/"}),
                http,
                None,
            )
            assert recorder.calls == 1, "ikinci dönüş sessiz pencereye girmeli"

            await upstream.reset_account_clients()
            assert recorder.calls == 2, "bekleyen bildirim transport yenilenirken teslim edilmeli"

            await asyncio.sleep(0.35)  # iptal edilmemiş bir zamanlayıcı burada çalar
            assert recorder.calls == 2, "iptal edilmeyen zamanlayıcı fazladan bildirim üretti"
        finally:
            await upstream.shutdown()

    asyncio.run(run())


def test_shutdown_cancels_a_pending_flush_without_calling_listeners() -> None:
    """Kapanışta bekleyen bildirim çalmaz; dinleyici kapanmış sayfaya dokunmaz."""
    old_token = _fake_jwt(sub="user-1")
    settings = make_settings(upstream_cookie=f"sb-session={old_token}")
    upstream = UpstreamClient(settings)
    recorder = _Recorder()
    upstream.add_identity_listener(recorder)
    upstream._identity_quiet_seconds = 5.0

    async def run() -> None:
        await upstream.startup()
        first = _fake_jwt(sub="user-1", iat=21)
        upstream._notify_identity_rotated(coalesce=True, identity=first)
        assert recorder.calls == 1  # ilki gecikmesiz teslim edilir
        second = _fake_jwt(sub="user-1", iat=22)
        upstream._notify_identity_rotated(coalesce=True, identity=second)
        assert recorder.calls == 1
        assert upstream._identity_flush_handle is not None
        await upstream.shutdown()
        assert upstream._identity_flush_handle is None

    asyncio.run(run())


# ------------------------------------------ teşhis kaydının kapsadığı/covermediği
def test_set_cookie_response_always_leaves_an_identity_record() -> None:
    """Kaydın yokluğu yalnız "`Set-Cookie` yok" demektir; ihmal edilmiş vaka kalmaz."""
    token = _fake_jwt(sub="user-1")

    async def case(
        *,
        cookie: str,
        headers: dict[str, str],
        empty_jar: bool = False,
        extra_jar: dict[str, str] | None = None,
    ) -> dict[str, object]:
        settings = make_settings(upstream_cookie=cookie)
        upstream = UpstreamClient(settings)
        await upstream.startup()
        try:
            http = upstream.client
            if empty_jar:
                http.cookies.clear()
            for name, value in (extra_jar or {}).items():
                http.cookies.set(name, value, domain="upstream.test", path="/")
            upstream._ingest_set_cookie(
                httpx.Response(200, headers=headers), http, None
            )
            return upstream.identity_update
        finally:
            await upstream.shutdown()

    # 1) Yanıtta `Set-Cookie` yok → kayıt yok (ve bu "kimlik sabit" demek değil).
    async def no_header() -> None:
        settings = make_settings(upstream_cookie=f"sb-session={token}")
        upstream = UpstreamClient(settings)
        await upstream.startup()
        try:
            upstream._ingest_set_cookie(httpx.Response(200), upstream.client, None)
            assert upstream.identity_update == {}
        finally:
            await upstream.shutdown()

    asyncio.run(no_header())

    # 2) Saklanmış çerez satırı yok → karar verilemez.
    record = asyncio.run(
        case(
            cookie="",
            headers={"set-cookie": "sb-session=x; Path=/"},
            extra_jar={"sb-session": "x"},
        )
    )
    assert record["reason"] == "no_stored_cookie"
    assert record["access_token_rotated"] is None
    assert record["untracked_cookie_names"] == ["sb-session"]

    # 3) Jar okunamıyor → yine "değerlendirilemedi".
    record = asyncio.run(
        case(
            cookie=f"sb-session={token}",
            headers={"set-cookie": "sb-session=y; Path=/"},
            empty_jar=True,
        )
    )
    assert record["reason"] == "empty_cookie_jar"
    assert record["access_token_rotated"] is None

    # 4) Çerez geldi ama değer değişmedi → `false` değil, `changed: false`.
    record = asyncio.run(
        case(
            cookie=f"sb-session={token}",
            headers={"set-cookie": f"sb-session={token}; Path=/"},
        )
    )
    assert record["reason"] == "cookie_unchanged"
    assert record["changed"] is False
    assert record["access_token_rotated"] is None

    # 5) Hedef *yeni bir ad* set etti: satıra eklenir (değişti sayılır) ama token
    #    kıyası bu adı görmez → kayıt hangi adların "izlenmiyor" olduğunu söyler.
    record = asyncio.run(
        case(
            cookie=f"sb-session={token}",
            headers={"set-cookie": "sb-next-gen=abc; Path=/"},
            extra_jar={"sb-next-gen": "abc"},
        )
    )
    assert record["reason"] == "applied"
    assert record["changed"] is True
    assert record["access_token_rotated"] is False
    assert record["untracked_cookie_names"] == ["sb-next-gen"]
    # Adlar sır değildir; değer değil yalnız ad listelenir.
    assert "abc" not in json.dumps(record)

# ------------------------------------------- korunan çerezler ve boşalan ayar
def test_protected_challenge_cookies_survive_even_when_we_injected_them() -> None:
    """`cf_clearance` biz enjekte etmiş olsak bile budanmaz.

    `UPSTREAM_COOKIE` içinde yarış çerezi gelebiliyor (paylaşılan cURL
    örneğinde vardı) — yani jar'daki `cf_clearance` "sayfanın kendi kazandığı"
    olmak zorunda değil. Yeni ayarda adı geçince eski sürüm onu "bizim
    enjekte ettiğimiz eski çerez" sayıp siliyordu; bu, her token üretiminde
    Cloudflare yarışını baştan tetiklerdi.
    """
    provider = _browser_provider()
    # Ayar `cf_clearance` taşımıyor (yukarıdaki fixture yalnız sb-session+theme
    # içeriyor), ama jar'da bizim enjekte ettiğimiz bir kopyası var.
    provider._context = _FilteredContext(
        [
            {"name": "sb-session", "value": "stale-old", "domain": "svc.example.test"},
            {"name": "cf_clearance", "value": "injected-by-us", "domain": "svc.example.test"},
        ]
    )
    provider._injected_cookie_names = {"sb-session", "cf_clearance"}

    async def run() -> None:
        await provider._set_cookies_from_settings()

    asyncio.run(run())

    names = provider._context.names()
    assert "cf_clearance" in names, "korunan ad silinemez"
    assert "sb-session" in names and provider._context.value_of("sb-session") == "new-value"
    assert provider.stats["browser_cookie_prune_skipped_protected"] == ["cf_clearance"]
    # Takip listesi korunanı içeride tutar: sonraki turda da korunacağı biliniyor.
    assert "cf_clearance" in provider._injected_cookie_names


def test_prune_never_targets_protected_names_even_if_asked() -> None:
    """Doğrudan çağrıda da koruma geçerli (davranış garantisi, iç çağrı değil)."""
    provider = _browser_provider()
    provider._context = _FilteredContext(
        [{"name": "cf_clearance", "value": "x", "domain": "svc.example.test"}]
    )

    async def run() -> None:
        await provider._prune_injected_cookies(["cf_clearance", "__cf_bm"])

    asyncio.run(run())
    assert provider._context.clears == []
    assert provider._context.names() == ["cf_clearance"]


def test_empty_cookie_setting_still_prunes_what_we_injected() -> None:
    """Ayar boşaldıysa budama **atlanmaz**; bizim koyduklarımız geride kalmaz.

    Eski sürüm `if not cookie_header: return` ile budamaya hiç gelmiyordu, yani
    çerez temizliği iptal edilirken hayalet çerezler jar'da kalıyordu.
    """
    settings = make_settings(
        recaptcha_provider="browser",
        recaptcha_site_key="6Lfake",
        target_domain="svc.example.test",
        upstream_cookie="",
    )
    provider = BrowserRecaptchaProvider(settings)
    provider._context = _FilteredContext(
        [
            {"name": "sb-session", "value": "old", "domain": "svc.example.test"},
            {"name": "legacy-flag", "value": "1", "domain": "svc.example.test"},
            {"name": "cf_clearance", "value": "keep", "domain": "svc.example.test"},
        ]
    )
    provider._injected_cookie_names = {"sb-session", "legacy-flag"}

    async def run() -> None:
        await provider._set_cookies_from_settings()

    asyncio.run(run())

    assert provider._context.names() == ["cf_clearance"]
    assert provider._injected_cookie_names == set()
    assert provider._context.clears == [
        {"name": "legacy-flag", "domain": None, "path": None},
        {"name": "sb-session", "domain": None, "path": None},
    ]


# ------------------------------------------------ ret yanıtlarının gözlemi
def test_rejected_response_is_recorded_but_not_applied() -> None:
    """403 `Set-Cookie` taşıyorsa iz kalır; çerez **kasıtlı olarak** uygulanmaz.

    Kaydın varlığı "reddedilen yanıtta çerez vardı" demektir; `reason`
    `rejected_not_ingested` ise o çerezin oturuma yazılmadığını ayrıca söyler.
    Böylece "her yanıt loglanıyor" cümlesinin kapsamı testte sabitlenir.
    """
    old_token = _fake_jwt(sub="user-1")
    settings = make_settings(
        upstream_cookie=f"sb-session={old_token}",
        deployment_id_refresh_enabled=False,
        retry_max_attempts=1,
    )
    seen_value = "cf-deger-42"
    rotated = _fake_jwt(sub="user-1", iat=77)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers=[
                ("set-cookie", f"__cf_bm={seen_value}; Path=/"),
                ("set-cookie", f"sb-session={rotated}; Path=/"),
            ],
            text='{"error":"recaptcha verification failed"}',
        )

    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run() -> None:
        try:
            # CAPTCHA işaretli gövde → `RecaptchaRejected`; olayın biçimi bu.
            with pytest.raises(RecaptchaRejected):
                async with upstream.stream_completion("chat", {"id": "chat"}):
                    pass  # pragma: no cover
        finally:
            await upstream.shutdown()

    asyncio.run(run())

    record = upstream.identity_update
    assert record["reason"] == "rejected_not_ingested"
    assert record["status"] == 403
    assert record["set_cookie_count"] == 2
    assert record["set_cookie_names"] == ["__cf_bm", "sb-session"]
    # Ret yanıtının çerezleri oturuma yazılmadı: teşhis gözlemi, uygulama değil.
    assert settings.upstream_cookie == f"sb-session={old_token}"
    body = json.dumps(record)
    assert seen_value not in body and rotated not in body and old_token not in body


def test_warmup_applies_cookies_only_for_non_error_responses() -> None:
    """Isıtma GET'i ile akış POST'u aynı politikayı kullanır.

    İlk sürümde warmup durumu hiç sorgulamadan `_ingest_set_cookie` çağırıyordu:
    403/challenge sayfasının çerezleri kalıcı oturum satırına yazılabiliyordu —
    azaltmak istediğimiz tam kirlenme.
    """
    old_token = _fake_jwt(sub="user-1")
    challenge_value = "warmup-cok-42"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                403,
                headers=[("set-cookie", f"__cf_bm={challenge_value}; Path=/")],
                text="error code: 1020",
            )
        return httpx.Response(200, text="ok")

    settings = make_settings(upstream_cookie=f"sb-session={old_token}")
    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://upstream.test"
    )

    async def run() -> None:
        await upstream.warmup("chat-1")

    asyncio.run(run())

    record = upstream.identity_update
    assert record["reason"] == "rejected_not_ingested"
    assert record["status"] == 403
    assert record["set_cookie_names"] == ["__cf_bm"]
    # Kalıcı satır değişmedi; jar değişti (tarayıcı gibi) — ayrım bilinçli.
    assert settings.upstream_cookie == f"sb-session={old_token}"
    assert upstream.client.cookies.get("__cf_bm") == challenge_value
    assert challenge_value not in json.dumps(record)

    # Pozitif kontrol: 200 + çerez DÖNERSE warmup kalıcı satırı günceller.
    fresh_token = _fake_jwt(sub="user-1", iat=55)

    async def ok_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=[("set-cookie", f"sb-session={fresh_token}; Path=/")],
            text="<html></html>",
        )

    ok_settings = make_settings(upstream_cookie=f"sb-session={old_token}")
    upstream_ok = UpstreamClient(ok_settings)
    upstream_ok._client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok_handler), base_url="https://upstream.test"
    )

    async def run_ok() -> None:
        await upstream_ok.warmup("chat-1")

    asyncio.run(run_ok())

    assert upstream_ok.identity_update["reason"] == "applied"
    assert ok_settings.upstream_cookie == f"sb-session={fresh_token}"
