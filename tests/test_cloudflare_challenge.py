"""Cloudflare bot duvarı, reCAPTCHA reddiyle karıştırılmamalı.

`cf_clearance`/`__cf_bm` eskidiğinde hedef HTML bir "Just a moment..."
sayfası döner. Bu sayfa "forbidden"/"bot" kelimelerini içerdiği için eski
kod onu `recaptcha_rejected` sanıyor ve operatörü yanlış yöne (CAPTCHA
token'ı) yönlendiriyordu.
"""

from __future__ import annotations

import pytest

from app.upstream.client import UpstreamClient

CF_PAGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body>Checking your browser before accessing arena.ai. "
    "Enable JavaScript and cookies to continue.</body></html>"
)

CF_ATTENTION = "<html><head><title>Attention Required! | Cloudflare</title></head></html>"


def test_cloudflare_page_is_not_treated_as_recaptcha_rejection() -> None:
    assert UpstreamClient._is_cloudflare_challenge(403, CF_PAGE)
    assert not UpstreamClient._is_captcha_body(403, CF_PAGE)


def test_cloudflare_attention_page_is_detected() -> None:
    assert UpstreamClient._is_cloudflare_challenge(403, CF_ATTENTION)
    assert not UpstreamClient._is_captcha_body(403, CF_ATTENTION)


def test_real_recaptcha_rejection_still_detected() -> None:
    body = '{"error":"recaptcha verification failed","code":"recaptcha_rejected"}'
    assert UpstreamClient._is_captcha_body(403, body)
    assert not UpstreamClient._is_cloudflare_challenge(403, body)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_non_cloudflare_status_never_matches(status: int) -> None:
    assert not UpstreamClient._is_cloudflare_challenge(status, CF_PAGE)
