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


# --- Genel 403 gövdeleri reCAPTCHA reddi sayılmamalı -------------------------
# `_is_captcha_body` eskiden "forbidden" ve "bot" kelimelerini yeterli görüyordu;
# hedefin herhangi bir yetki hatası yanlışlıkla `recaptcha_rejected` oluyor ve
# operatör CAPTCHA token'ına bakmaya yönlendiriliyordu.

@pytest.mark.parametrize(
    "body",
    [
        '{"error":"verification failed"}',
        '{"detail":"bot detected"}',
        '{"message":"forbidden"}',
        '{"error":"model not found"}',
        '{"error":"quota exceeded"}',
    ],
)
def test_generic_forbidden_bodies_are_not_captcha_rejections(body: str) -> None:
    assert not UpstreamClient._is_captcha_body(403, body)


def test_empty_body_is_not_captcha_rejection() -> None:
    assert not UpstreamClient._is_captcha_body(403, "")


# --- Boş token alanı gövdeye hiç yazılmamalı --------------------------------

def test_empty_captcha_token_omits_the_field() -> None:
    """`noop` sağlayıcısında gövdede `"recaptchaV3Token": ""` gidiyordu.

    Boş değer "geçersiz token" olarak değerlendirilebildiği için alan hiç
    gönderilmemeli; doğrulamayan hedef alanı yok saysın.
    """
    from app.schemas.upstream import UpstreamPayload, UserMessage

    payload = UpstreamPayload(
        id="c", modelAId="m", userMessageId="u", modelAMessageId="a",
        userMessage=UserMessage(content="x"),
    )
    wire = payload.to_wire("recaptchaV3Token")
    assert "recaptchaV3Token" not in wire
    assert wire["id"] == "c"


def test_non_empty_captcha_token_is_written_to_configured_field() -> None:
    from app.schemas.upstream import UpstreamPayload, UserMessage

    payload = UpstreamPayload(
        id="c", modelAId="m", userMessageId="u", modelAMessageId="a",
        userMessage=UserMessage(content="x"), recaptchaV3Token="tok-1",
    )
    wire = payload.to_wire("recaptchaV2Token")
    assert wire["recaptchaV2Token"] == "tok-1"
    assert "recaptchaV3Token" not in wire


def test_noop_provider_does_not_count_rejections(settings) -> None:
    """Token hiç üretilmediyse "red" sayacı artmamalı."""
    from app.services.recaptcha.static import NoopRecaptchaProvider

    provider = NoopRecaptchaProvider(settings)
    provider.invalidate()
    assert provider.stats["rejected"] == 0
