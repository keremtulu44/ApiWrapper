"""Kalıcı tarayıcı girişi (`app/upstream/browser_login.py`) regresyonları.

Gerçek Chromium açılmaz: Playwright nesneleri sahte sınıflarla taklit edilir.
Kapsanan davranış:

* yapılandırma kapısı (kapalıyken hiçbir şey başlamaz, eksik env raporlanır),
* taze çerez gerçekten `Settings`'e yazılıyor mu,
* değişmeyen çerez için gereksiz Render yazımı tetiklenmiyor mu (`changed`),
* token eskimişse önce sayfa dürtülüyor, sonra giriş deneniyor,
* gizli değerlerin log/çıktıya sızmaması.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.upstream.auth import extract_access_token
from app.upstream.browser_login import (
    SessionKeeper,
    _has_session_cookie,
    login_configured,
    missing_login_config,
)
from tests.conftest import make_settings


#: Testlerde kullanılan sahte oturum çerezi: gerçek bir JWT taşır.
def _jwt(*, exp_offset: float = 900) -> str:
    import base64
    import json

    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_offset), "sub": "u1"}).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _session_value(token: str, refresh_token: str = "r1") -> str:
    """Çerez parçası: gerçek biçimde base64'lenmiş oturum JSON'u."""
    import base64
    import json

    blob = base64.b64encode(
        json.dumps({"access_token": token, "refresh_token": refresh_token}).encode()
    ).decode()
    return "base64-" + blob


def _session_cookies(token: str) -> list[dict[str, Any]]:
    """Geçerli oturum çerezi taşıyan çerez listesi (token okunabilir)."""
    return [
        {"name": "arena-auth-prod-v1.0", "value": _session_value(token)},
        {"name": "__cf_bm", "value": "cf"},
        {"name": "prefs", "value": "lang"},
    ]


class FakeContext:
    def __init__(self, cookies: list[dict[str, Any]]) -> None:
        self._cookies = cookies
        self.pages: list[FakePage] = []
        self.closed = False

    async def cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies)

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True

    def set_session_cookie(self, token: str) -> None:
        """Sahte site 'token yenilendi' der: çerez burada tazelenir."""
        self._cookies = [
            {"name": "arena-auth-prod-v1.0", "value": _session_value(token)},
            {"name": "__cf_bm", "value": "cf"},
        ]


class FakeLocator:
    def __init__(self, present: bool, value: str = "") -> None:
        self._present = present
        self.filled = value

    async def count(self) -> int:
        return 1 if self._present else 0

    async def is_visible(self) -> bool:
        return self._present

    async def is_enabled(self) -> bool:
        return self._present

    async def click(self, timeout: int | None = None) -> None:
        return None

    async def fill(self, value: str, timeout: int | None = None) -> None:
        self.filled = value


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.url = ""
        self.goto_calls: list[str] = []
        self.keyboard = self

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)
        self.url = url
        # Sayfa açıldığında site kendi token'ını yeniler.
        self.context.set_session_cookie(_jwt())

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def press(self, key: str) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        # Giriş formunda e-posta ve şifre alanları var sayılır.
        if "email" in selector or "mail" in selector:
            return FakeLocator(True)
        if "password" in selector:
            return FakeLocator(True)
        if selector.startswith("button") or "submit" in selector:
            return FakeLocator(True)
        return FakeLocator(False)


def _login_settings(**overrides: Any):
    base: dict[str, Any] = {
        "upstream_login_enabled": True,
        "upstream_login_email": "kisi@example.com",
        "upstream_login_password": "gizli-sifre",
        "upstream_user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        "upstream_referer_path": "/c/{chat_id}",
        "recaptcha_provider": "noop",
    }
    base.update(overrides)
    return make_settings(**base)


# ------------------------------------------------------------------ kapılar
def test_login_disabled_by_default():
    settings = make_settings()
    assert settings.upstream_login_enabled is False
    assert login_configured(settings) is False


def test_login_configured_requires_all_three():
    settings = _login_settings()
    assert login_configured(settings) is True

    settings.upstream_login_password = ""
    assert login_configured(settings) is False
    assert missing_login_config(settings) == ["UPSTREAM_LOGIN_PASSWORD"]

    settings.upstream_login_email = ""
    assert missing_login_config(settings) == [
        "UPSTREAM_LOGIN_EMAIL",
        "UPSTREAM_LOGIN_PASSWORD",
    ]


def test_login_url_falls_back_to_base_url():
    settings = _login_settings(upstream_login_path="")
    assert settings.login_url() == settings.base_url

    settings.upstream_login_path = "/login"
    assert settings.login_url().endswith("/login")


# ------------------------------------------------------------- çerez tanıma
def test_has_session_cookie_detects_auth_family():
    assert _has_session_cookie(_session_cookies("x")) is True
    assert _has_session_cookie([{"name": "prefs", "value": "x"}]) is False
    assert _has_session_cookie([]) is False


# -------------------------------------------------------------- tazeleme
@pytest.mark.asyncio
async def test_refresh_writes_fresh_cookie_to_settings():
    settings = _login_settings(upstream_cookie="", upstream_access_token="")
    keeper = SessionKeeper(settings)
    token = _jwt()
    keeper._context = FakeContext(_session_cookies(token))
    keeper._page = FakePage(keeper._context)

    status = await keeper.refresh(reason="test")

    assert status.ok is True
    assert status.changed is True
    assert status.session_cookie is True
    assert status.token_seconds is not None and status.token_seconds > 0
    # Çerez gerçekten yazıldı ve içinden token okunabiliyor.
    assert settings.upstream_cookie.startswith("arena-auth-prod-v1.0=")
    assert extract_access_token(settings.upstream_cookie) == token
    assert settings.upstream_access_token == token
    # Token zaten tazeyse sayfa dürtülmez.
    assert keeper._page.goto_calls == []


@pytest.mark.asyncio
async def test_refresh_is_noop_when_cookie_unchanged():
    """Aynı çerez ikinci kez yazılmaz: gereksiz Render env yazımı tetiklenmez."""
    settings = _login_settings()
    keeper = SessionKeeper(settings)
    token = _jwt()
    keeper._context = FakeContext(_session_cookies(token))
    keeper._page = FakePage(keeper._context)

    first = await keeper.refresh(reason="test")
    second = await keeper.refresh(reason="test")

    assert first.changed is True
    assert second.ok is True
    assert second.changed is False


@pytest.mark.asyncio
async def test_expired_token_triggers_nudge_then_uses_fresh_cookie():
    settings = _login_settings()
    keeper = SessionKeeper(settings)
    expired = _jwt(exp_offset=-60)
    keeper._context = FakeContext(_session_cookies(expired))
    keeper._page = FakePage(keeper._context)

    status = await keeper.refresh(reason="test")

    # Sayfa dürtüldü ve sahte sayfa taze token üretti.
    assert keeper._page.goto_calls, "token eskimişse sayfa dürtülmeli"
    assert "sayfa durtuldu" in status.notes
    assert status.ok is True
    assert extract_access_token(settings.upstream_cookie) != expired


@pytest.mark.asyncio
async def test_missing_config_refuses_to_start_browser():
    settings = _login_settings(upstream_login_password="")
    keeper = SessionKeeper(settings)

    status = await keeper.refresh(reason="test")

    assert status.ok is False
    assert "UPSTREAM_LOGIN_PASSWORD" in status.reason
    # Tarayıcı hiç açılmadı.
    assert keeper._context is None


def test_status_payload_has_no_secret_values():
    from app.upstream.browser_login import LoginStatus

    status = LoginStatus(
        ok=True, at=time.time(), token_seconds=812.4, session_cookie=True
    )
    payload = status.as_dict()
    assert set(payload) == {
        "ok",
        "reason",
        "at",
        "login_redirect",
        "session_cookie",
        "token_seconds",
        "changed",
        "notes",
    }
    assert payload["token_seconds"] == 812
