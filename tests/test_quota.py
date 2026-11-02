"""Upstream kota/kısıtlama tespiti testleri.

Hedef servis hesabı kilitlediğinde bunu üç farklı yoldan dile getirebiliyor;
üçü de yakalanmalı ve istek **retry edilmeden** 429 olarak yüzeye çıkmalı.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.core.errors import UpstreamQuotaError
from app.main import create_app
from app.upstream.client import UpstreamClient
from app.upstream.quota import QuotaTextScanner, find_quota_marker, normalize_markers
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


@pytest.fixture
def quota_client():
    """Düz metin kota taraması açık bir istemci."""
    app = create_app(make_settings(quota_text_scan_chars=300))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


@pytest.fixture
def retry_client():
    """Retry'ın gerçekten açık olduğu bir istemci (test varsayılanı 1 denemedir)."""
    app = create_app(make_settings(retry_max_attempts=3))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


def chat_body(prompt: str = "selam") -> dict:
    return {"model": "test-model", "messages": [{"role": "user", "content": prompt}]}


# ------------------------------------------------------------- işaretler
def test_normalize_markers_strips_and_lowercases():
    assert normalize_markers([" Upstream Limit Reached ", "", "  ", "QUOTA EXCEEDED"]) == (
        "upstream limit reached",
        "quota exceeded",
    )
    assert normalize_markers(None) == ()


def test_find_quota_marker_is_case_insensitive():
    markers = normalize_markers(["upstream limit reached"])
    assert (
        find_quota_marker("UPSTREAM LIMIT REACHED, try later", markers)
        == "upstream limit reached"
    )
    assert find_quota_marker("her şey yolunda", markers) is None
    assert find_quota_marker("", markers) is None


def test_default_markers_cover_the_observed_message():
    """Kullanıcının gözlemlediği 'upstream limit reached' varsayılanla yakalanmalı."""
    settings = make_settings()
    assert (
        find_quota_marker(
            '{"error":"upstream limit reached"}', settings.upstream_limit_markers
        )
        == "upstream limit reached"
    )


# ------------------------------------------------------------ düz metin
def test_scanner_detects_marker_in_first_delta():
    scanner = QuotaTextScanner(["upstream limit reached"], window=300)
    assert scanner.feed("upstream limit reached") == "upstream limit reached"


def test_scanner_detects_marker_split_across_deltas():
    """İşaret iki delta arasına bölünmüşse de yakalanmalı."""
    scanner = QuotaTextScanner(["upstream limit reached"], window=300)
    assert scanner.feed("upstream limit re") is None
    assert scanner.feed("ached") == "upstream limit reached"


def test_scanner_stops_after_window_exceeded():
    scanner = QuotaTextScanner(["upstream limit reached"], window=20)
    assert scanner.feed("x" * 40) is None
    assert scanner.active is False
    assert scanner.feed("upstream limit reached") is None


def test_scanner_can_be_disabled_once_content_is_emitted():
    scanner = QuotaTextScanner(["upstream limit reached"], window=300)
    scanner.disable()
    assert scanner.active is False
    assert scanner.feed("upstream limit reached") is None


def test_scanner_inactive_without_markers_or_window():
    assert QuotaTextScanner(["upstream limit reached"], window=0).active is False
    assert QuotaTextScanner([], window=300).active is False


# ------------------------------------------------------- HTTP gövde testi
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, '{"error":"upstream limit reached"}', "upstream limit reached"),
        (403, "rate limit exceeded for this account", "rate limit exceeded"),
        (400, "You have sent too many requests recently.", "too many requests"),
        (500, "internal explosion", None),
        (429, "", None),
        (200, "upstream limit reached", None),  # 2xx gövdesi taranmaz
    ],
)
def test_is_quota_body(status, body, expected):
    client = UpstreamClient(make_settings())
    assert client._is_quota_body(status, body) == expected


def test_is_quota_body_respects_custom_markers():
    client = UpstreamClient(make_settings(upstream_limit_markers=["yeter artık"]))
    assert client._is_quota_body(429, "Yeter artık, bekleyin.") == "yeter artık"
    assert client._is_quota_body(429, "upstream limit reached") is None


# ------------------------------------------------- uçtan uca: HTTP gövdesi
@respx.mock
def test_http_body_quota_becomes_429(retry_client):
    route = stream_route(respx).mock(
        return_value=httpx.Response(
            429,
            content=b'{"error":"upstream limit reached"}',
            headers={"retry-after": "120"},
        )
    )
    response = retry_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    error = response.json()["error"]
    assert error["type"] == "rate_limit_error"
    assert error["code"] == "upstream_quota_reached"
    assert "upstream limit reached" in error["message"]
    assert response.headers["retry-after"] == "120"
    assert route.call_count == 1


@respx.mock
def test_quota_is_not_retried_even_when_retry_is_enabled(retry_client):
    """Asıl düzeltme: retry açıkken bile kilitli hesapla tekrar deneme yapılmamalı."""
    route = stream_route(respx).mock(
        return_value=httpx.Response(429, content=b'{"error":"upstream limit reached"}')
    )
    retry_client.post("/v1/chat/completions", json=chat_body())
    assert route.call_count == 1


@respx.mock
def test_plain_429_without_quota_marker_is_still_retried(retry_client):
    """Kota işareti yoksa 429 geçici kabul edilip retry edilmeli (davranış korundu)."""
    route = stream_route(respx).mock(
        return_value=httpx.Response(429, content=b"slow down briefly")
    )
    response = retry_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_rate_limited"
    assert route.call_count == 3  # retry_max_attempts=3


@respx.mock
def test_quota_body_wins_over_captcha_classification(client):
    """403 + 'forbidden' normalde captcha sayılır; kota işareti öncelikli olmalı."""
    stream_route(respx).mock(
        return_value=httpx.Response(403, content=b"forbidden: message limit reached")
    )
    response = client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"


@respx.mock
def test_401_still_maps_to_upstream_unauthorized(client):
    """Kota işareti yokken 401 eski davranışında kalmalı."""
    stream_route(respx).mock(return_value=httpx.Response(401, content=b""))
    response = client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unauthorized"


# ------------------------------------------------------- uçtan uca: akış
@respx.mock
def test_stream_error_event_quota_becomes_429(client):
    body = b'f:{"messageId":"m1"}\n3:"upstream limit reached"\n'
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))
    response = client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"


@respx.mock
def test_stream_plain_text_quota_is_caught(quota_client):
    """Kısıtlama mesajı düz metin delta'sı olarak gelirse de yakalanmalı."""
    body = b'f:{"messageId":"m1"}\n0:"upstream limit reached"\n'
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))
    response = quota_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "upstream_quota_reached"


@respx.mock
def test_stream_plain_text_quota_leaks_no_content_to_client(quota_client):
    """SSE zaten başladığı için 200 döner; ama içerik delta'sı sızmamalı."""
    body = b'f:{"messageId":"m1"}\n0:"upstream limit reached"\n'
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))
    response = quota_client.post(
        "/v1/chat/completions", json={**chat_body(), "stream": True}
    )
    assert response.status_code == 200
    text = response.text
    assert "upstream_quota_reached" in text
    assert '"delta": {"content"' not in text
    assert text.rstrip().endswith("data: [DONE]")


@respx.mock
def test_normal_reply_mentioning_limits_is_not_flagged(quota_client):
    """Tarama açıkken bile normal cevap yanlış alarma yol açmamalı.

    İşaret ilk delta'da geçiyor; yine de gerçek içerik aktığı için tarayıcı
    devre dışı kalır ve cevap istemciye ulaşır.
    """
    stream_route(respx).mock(
        return_value=httpx.Response(
            200,
            content=ai_stream(
                "Bu API için rate limit ayarını ",
                "panelden değiştirebilirsiniz.",
            ),
        )
    )
    response = quota_client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert "rate limit" in response.json()["choices"][0]["message"]["content"]


@respx.mock
def test_scanning_disabled_by_default_settings(client):
    """Varsayılan ayarlarda düz metin taraması kapalıdır (0), akış normal döner."""
    settings = client.app.state.settings
    assert settings.quota_text_scan_chars == 0
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("upstream limit reached"))
    )
    response = client.post("/v1/chat/completions", json=chat_body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "upstream limit reached"


# ---------------------------------------------------------------- metrik
@respx.mock
def test_quota_detection_is_counted_in_metrics(client):
    stream_route(respx).mock(
        return_value=httpx.Response(429, content=b'{"error":"upstream limit reached"}')
    )
    client.post("/v1/chat/completions", json=chat_body())
    response = client.get("/metrics")
    assert "apiwrapper_upstream_quota_errors_total" in response.text


def test_quota_error_shape():
    error = UpstreamQuotaError("limited")
    assert error.status_code == 429
    assert error.err_type == "rate_limit_error"
    assert error.code == "upstream_quota_reached"
