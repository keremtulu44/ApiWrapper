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


def test_cloudflare_header_identifies_challenge_without_body() -> None:
    assert UpstreamClient._is_cloudflare_challenge(403, "", "challenge")
    assert UpstreamClient._is_cloudflare_challenge(200, "", "Challenge")
    assert not UpstreamClient._is_cloudflare_challenge(403, "", "other")


@pytest.mark.asyncio
async def test_cloudflare_header_precedes_auth_and_captcha_classification():
    import httpx

    from app.upstream.exceptions import UpstreamChallenge
    from tests.conftest import make_settings

    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            403, headers={"cf-mitigated": "challenge"},
            text='{"error":"session expired and recaptcha invalid"}',
        )

    settings = make_settings(deployment_id_refresh_enabled=False, retry_max_attempts=3)
    upstream = UpstreamClient(settings)
    upstream._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamChallenge, match="Cloudflare") as exc:
            async with upstream.stream_completion("chat", {"id": "chat"}):
                pass
        assert "expired" not in str(exc.value).lower()
        assert requests == 1
    finally:
        await upstream.shutdown()


def test_api_reports_challenge_without_refresh_or_retry(monkeypatch):
    import httpx
    import respx
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import TEST_API_KEY, make_settings
    from tests.test_token_refresh import session_cookie

    settings = make_settings(
        upstream_cookie=session_cookie(expires_in=3600),
        upstream_refresh_enabled=True,
        deployment_id_refresh_enabled=False,
        retry_max_attempts=3,
    )
    called = []

    async def no_refresh(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("challenge must not rotate credentials")

    monkeypatch.setattr("app.services.completion_service.refresh_upstream_token", no_refresh)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith="https://upstream.test/").mock(
            return_value=httpx.Response(403, headers={"cf-mitigated": "challenge"})
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {TEST_API_KEY}"},
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_challenge"
        assert route.call_count == 1
        assert not called


def test_challenge_has_specific_diagnostics() -> None:
    from app.core.diagnostics import explain_failure

    hint = explain_failure(502, "upstream_challenge")
    assert "challenge" in hint.title.lower()
    assert "süresinin dolduğunu göstermez" in hint.action
