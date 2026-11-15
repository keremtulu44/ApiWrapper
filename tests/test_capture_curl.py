"""`scripts/capture_curl.py` — yakalama ve teslimat mantığının regresyonları.

Gerçek tarayıcı çalıştırılmaz: Playwright nesneleri yerine küçük sahte sınıflar
kullanılır. Kapsanan davranış:

* cURL üretimi (taşınmaması gereken başlıklar, gövde korunur),
* gizli değer maskeleme (çıktıda token sızmaz),
* gövde alanı tanıma (sohbet isteği ≠ telemetri POST'u),
* akış ucu filtresi (yanlış uç uygulanmaz),
* `send_to_wrapper` sözleşmesi (uç yolu, başlık, gövde).
"""

from __future__ import annotations

import json

import httpx
import respx

from scripts.capture_curl import (
    BODY_MARKERS,
    CapturedRequest,
    _body_field_names,
    build_curl,
    mask,
    send_to_wrapper,
)

CHAT_URL = "https://upstream.test/nextjs-api/stream/create-evaluation"


# --------------------------------------------------------------- cURL üretimi
def test_build_curl_keeps_headers_and_body():
    curl = build_curl(
        CHAT_URL,
        {
            "cookie": "a=b; arena-auth-prod-v1.0=xyz",
            "user-agent": "Mozilla/5.0 Test",
            "referer": "https://upstream.test/c/abc",
        },
        '{"modelAId":"m1","userMessage":{"content":"hi"}}',
    )
    assert curl.startswith("curl https://upstream.test/nextjs-api/stream/create-evaluation")
    assert "-H 'cookie: a=b; arena-auth-prod-v1.0=xyz'" in curl
    assert "-H 'user-agent: Mozilla/5.0 Test'" in curl
    assert "--data-raw '{&quot;" not in curl
    assert '"modelAId":"m1"' in curl


def test_build_curl_drops_length_and_host_headers():
    """`content-length`/`host` taşınırsa elle gönderilen gövde isteği bozar."""
    curl = build_curl(
        CHAT_URL,
        {
            "content-length": "999",
            "host": "upstream.test",
            "content-encoding": "gzip",
            "connection": "keep-alive",
            "cookie": "a=b",
        },
        "{}",
    )
    lowered = curl.lower()
    assert "content-length" not in lowered
    assert "host:" not in lowered
    assert "content-encoding" not in lowered
    assert "connection:" not in lowered
    # Kaldırılmayan başlık korunur.
    assert "-H 'cookie: a=b'" in curl


def test_build_curl_quotes_single_quotes_in_body():
    """Gövdede tek tırnak varsa cURL komutu bozulmamalı."""
    curl = build_curl(CHAT_URL, {"cookie": "a=b"}, '{"content":"it\'s fine"}')
    assert "--data-raw" in curl
    # shlex.quote tek tırnağı `'"'"'` dizisiyle kaçırır: gövde kabuk için güvenli
    # kalır ve tek tırnak kaybolmaz.
    assert "\'\"\'\"" in curl


# ------------------------------------------------------------------- maskeleme
def test_mask_hides_short_values_entirely():
    assert mask("") == ""
    assert mask("kisa") == "***"


def test_mask_keeps_only_prefix_and_suffix():
    masked = mask("sk-abcdefghijklmnop")
    assert masked.startswith("sk-")
    assert masked.endswith("op")
    assert "abcdefghij" not in masked


# ------------------------------------------------------------ gövde tanıma
def test_body_field_names_returns_keys_only():
    body = json.dumps(
        {
            "modelAId": "secret-model-id",
            "userMessage": {"content": "gizli metin"},
            "recaptchaV3Token": "token-degeri",
        }
    )
    fields = _body_field_names(body)
    assert fields == {"modelAId", "userMessage", "recaptchaV3Token"}
    # Değerler dönmez.
    assert all("secret" not in name for name in fields)


def test_body_field_names_tolerates_garbage():
    assert _body_field_names("bu json degil") == set()
    assert _body_field_names("[1,2,3]") == set()
    assert _body_field_names("") == set()


def test_body_markers_cover_v2_and_v3_captcha_fields():
    assert "recaptchaV3Token" in BODY_MARKERS
    assert "recaptchaV2Token" in BODY_MARKERS
    assert "modelAId" in BODY_MARKERS


# ------------------------------------------------------------- akış ucu filtresi
def test_captured_request_detects_stream_path():
    stream = CapturedRequest(
        url=CHAT_URL, method="POST", curl="c", path="/nextjs-api/stream/create-evaluation"
    )
    agent = CapturedRequest(
        url="https://upstream.test/nextjs-api/stream/create-chat",
        method="POST",
        curl="c",
        path="/nextjs-api/stream/create-chat",
    )
    telemetry = CapturedRequest(
        url="https://upstream.test/api/v2/rum", method="POST", curl="c", path="/api/v2/rum"
    )
    assert stream.looks_like_stream is True
    assert agent.looks_like_stream is True
    assert telemetry.looks_like_stream is False


# ------------------------------------------------------------------ teslimat
@respx.mock
def test_send_to_wrapper_posts_curl_to_admin_endpoint():
    route = respx.post("https://wrapper.test/v1/admin/upstream/curl").mock(
        return_value=httpx.Response(200, json={"status": "applied", "fields": ["upstream_cookie"]})
    )

    result = send_to_wrapper(
        "curl 'https://upstream.test/x' -H 'cookie: a=b'",
        wrapper="https://wrapper.test/",
        api_key="admin-key",
    )

    assert result["status_code"] == 200
    assert result["response"]["status"] == "applied"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer admin-key"
    payload = json.loads(request.content.decode())
    assert payload["curl"].startswith("curl 'https://upstream.test/x'")
    assert payload["allow_path_change"] is False


@respx.mock
def test_send_to_wrapper_surfaces_rejected_request():
    respx.post("https://wrapper.test/v1/admin/upstream/curl").mock(
        return_value=httpx.Response(400, json={"error": {"message": "Yol uyuşmuyor"}})
    )

    result = send_to_wrapper(
        "curl 'https://upstream.test/x'",
        wrapper="https://wrapper.test",
        api_key="admin-key",
        allow_path_change=True,
    )

    assert result["status_code"] == 400
    assert result["response"]["error"]["message"] == "Yol uyuşmuyor"


@respx.mock
def test_send_to_wrapper_handles_non_json_response():
    respx.post("https://wrapper.test/v1/admin/upstream/curl").mock(
        return_value=httpx.Response(502, text="<html>bad gateway</html>")
    )

    result = send_to_wrapper(
        "curl 'https://upstream.test/x'", wrapper="https://wrapper.test", api_key="k"
    )

    assert result["status_code"] == 502
    assert "bad gateway" in result["response"]["raw"]
