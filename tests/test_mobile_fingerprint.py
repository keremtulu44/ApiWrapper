"""Mobil yakalanan cURL'ün parmak izi: Client Hint başlıkları uydurulmamalı.

iPhone Safari ve Firefox `sec-ch-ua*` başlıklarını **hiç** göndermez. Bu
başlıkları "Chrome gibi görünsün" diye eklemek, yakalanan cihazın parmak izini
bozar: gerçek trafikte iPhone Safari'den `"Google Chrome";v="152"` asla gitmez.
"""

from __future__ import annotations

from app.upstream.headers import (
    build_page_headers,
    build_stream_headers,
    client_hint_headers,
    platform_from_ua,
    sends_client_hints,
)
from tests.conftest import make_settings

HINT_KEYS = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")

IOS_SAFARI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IOS_CHROME = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) CriOS/154.0.1.2 Mobile/15E148 Safari/604.1"
)
IPAD_SAFARI = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
ANDROID_CHROME = (
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
)
FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0"
DESKTOP_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
DESKTOP_EDGE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
)


def _headers(ua: str) -> dict[str, str]:
    settings = make_settings(upstream_user_agent=ua)
    return build_stream_headers(settings, "chat-1")


def test_ios_safari_sends_no_client_hints() -> None:
    headers = _headers(IOS_SAFARI)

    assert not any(key in headers for key in HINT_KEYS)
    assert headers["user-agent"] == IOS_SAFARI


def test_ipad_and_ios_chrome_also_send_no_client_hints() -> None:
    for ua in (IPAD_SAFARI, IOS_CHROME):
        headers = _headers(ua)
        assert not any(key in headers for key in HINT_KEYS), ua


def test_page_headers_match_stream_headers_for_mobile() -> None:
    """Isıtma isteği ile akış isteği aynı cihazı temsil etmeli."""
    for ua in (IOS_SAFARI, ANDROID_CHROME):
        settings = make_settings(upstream_user_agent=ua)
        stream = build_stream_headers(settings, "chat-1")
        page = build_page_headers(settings)
        for key in HINT_KEYS:
            assert stream.get(key) == page.get(key), (ua, key)


def test_android_chrome_keeps_real_brand_and_version() -> None:
    headers = _headers(ANDROID_CHROME)

    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'
    assert '"Google Chrome";v="154"' in headers["sec-ch-ua"]


def test_ios_platform_is_not_reported_as_macos() -> None:
    """iPhone UA'sı 'like Mac OS X' içerir; macOS kontrolü önce gelmemeli."""
    assert platform_from_ua(IOS_SAFARI) == '"iOS"'
    assert platform_from_ua(IPAD_SAFARI) == '"iOS"'
    assert platform_from_ua(DESKTOP_CHROME) == '"Windows"'


def test_no_invented_chrome_version_when_browser_has_none() -> None:
    """UA'da Chrome sürümü yoksa `152` gibi uydurma bir sürüm üretilmemeli."""
    assert "152" not in repr(client_hint_headers(IOS_SAFARI))
    assert "152" not in repr(client_hint_headers(FIREFOX))


def test_client_hint_support_matrix() -> None:
    assert sends_client_hints(ANDROID_CHROME) is True
    assert sends_client_hints(DESKTOP_CHROME) is True
    assert sends_client_hints(DESKTOP_EDGE) is True
    assert sends_client_hints(FIREFOX) is False
    assert sends_client_hints(IOS_SAFARI) is False
    assert sends_client_hints(IOS_CHROME) is False


def test_captured_android_brands_still_win_over_generated_ones() -> None:
    """Yakalanan cURL'de gerçek marka listesi varsa o kullanılır (Lemur/Samsung)."""
    settings = make_settings(
        upstream_user_agent=ANDROID_CHROME,
        upstream_extra_headers=(
            '{"sec-ch-ua":"\\"Chromium\\";v=\\"154\\", \\"Lemur\\";v=\\"154\\"",'
            '"sec-ch-ua-mobile":"?1"}'
        ),
    )

    headers = build_stream_headers(settings, "chat-1")

    assert headers["sec-ch-ua"] == '"Chromium";v="154", "Lemur";v="154"'
    assert headers["sec-ch-ua-mobile"] == "?1"


def test_recaptcha_browser_context_matches_the_captured_device() -> None:
    """Token üreten tarayıcı da yakalanan cihaz gibi görünmeli."""
    from app.services.recaptcha.browser import context_profile

    phone = context_profile(IOS_SAFARI)
    assert phone["is_mobile"] is True
    assert phone["has_touch"] is True
    assert phone["viewport"]["width"] < 600

    desktop = context_profile(DESKTOP_CHROME)
    assert desktop["is_mobile"] is False
    assert desktop["has_touch"] is False
    assert desktop["viewport"]["width"] > 1000


def test_context_profile_is_not_shared_state() -> None:
    """Profil sözlüğü kopyalanmalı; çağıran modül sabitini bozmamalı."""
    from app.services.recaptcha.browser import context_profile

    first = context_profile(IOS_SAFARI)
    first["viewport"]["width"] = 1

    assert context_profile(IOS_SAFARI)["viewport"]["width"] == 393
