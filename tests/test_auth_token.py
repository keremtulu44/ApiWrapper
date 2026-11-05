"""Cookie'den access_token ayıklama ve Authorization başlığı testleri."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote

import pytest

from app.upstream.auth import (
    decode_jwt_claims,
    extract_access_token,
    extract_refresh_token,
    is_token_expired,
    looks_like_jwt,
    merge_chunked_cookies,
    parse_cookie_header,
    resolve_bearer_token,
    token_seconds_remaining,
)
from app.upstream.headers import (
    build_authorization,
    build_page_headers,
    build_stream_headers,
    sec_ch_ua,
)
from tests.conftest import make_settings


def make_jwt(payload: dict | None = None) -> str:
    """İmzası önemsiz, biçimsel olarak geçerli bir JWT üretir."""

    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = seg({"alg": "HS256", "typ": "JWT"})
    body = seg(payload or {"sub": "user-1", "exp": int(time.time()) + 3600})
    return f"{header}.{body}.c2lnbmF0dXJlLXBsYWNlaG9sZGVy"


TOKEN = make_jwt()


# ------------------------------------------------------------------ parsing
def test_parse_cookie_header_basic():
    cookies = parse_cookie_header("a=1; b=2;  c = 3 ")
    assert cookies == {"a": "1", "b": "2", "c": "3"}


def test_parse_cookie_handles_equals_in_value():
    cookies = parse_cookie_header("t=abc==; other=x")
    assert cookies["t"] == "abc=="


def test_parse_cookie_ignores_junk():
    assert parse_cookie_header("") == {}
    assert parse_cookie_header("novalue; =orphan; ok=1") == {"ok": "1"}


def test_first_occurrence_wins():
    assert parse_cookie_header("k=first; k=second")["k"] == "first"


def test_merge_chunked_cookies():
    merged = merge_chunked_cookies({"sb.1": "world", "sb.0": "hello", "x": "y"})
    assert merged["sb"] == "helloworld"
    assert merged["x"] == "y"


def test_merge_does_not_override_existing_base():
    merged = merge_chunked_cookies({"sb": "whole", "sb.0": "part"})
    assert merged["sb"] == "whole"


# --------------------------------------------------------------- extraction
def test_extract_plain_access_token():
    assert extract_access_token(f"access_token={TOKEN}; other=1") == TOKEN


def test_extract_url_encoded_token():
    assert extract_access_token(f"access_token={quote(TOKEN)}") == TOKEN


def test_extract_from_json_cookie():
    value = json.dumps({"access_token": TOKEN, "refresh_token": "r-123"})
    assert extract_access_token(f"sb-auth-token={quote(value)}") == TOKEN


def test_extract_from_json_array_cookie():
    value = json.dumps([TOKEN, "refresh-token", None, None])
    assert extract_access_token(f"sb-auth-token={quote(value)}") == TOKEN


def test_extract_from_base64_json_cookie():
    payload = json.dumps({"access_token": TOKEN, "token_type": "bearer"}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert extract_access_token(f"sb-xyz-auth-token=base64-{encoded}") == TOKEN


def test_extract_from_chunked_base64_cookie():
    payload = json.dumps({"access_token": TOKEN}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mid = len(encoded) // 2
    header = (
        f"sb-auth-token.0=base64-{encoded[:mid]}; sb-auth-token.1={encoded[mid:]}"
    )
    assert extract_access_token(header) == TOKEN


def test_extract_from_nested_session_object():
    value = json.dumps({"currentSession": {"access_token": TOKEN}, "expiresAt": 1})
    assert extract_access_token(f"supabase.auth.token={quote(value)}") == TOKEN


def test_prefers_named_cookie_when_specified():
    other = make_jwt({"sub": "other"})
    header = f"decoy_token={other}; my_session={TOKEN}"
    assert extract_access_token(header, cookie_names=["my_session"]) == TOKEN


def test_partial_name_match_for_configured_name():
    header = f"app-my_session-v2={TOKEN}"
    assert extract_access_token(header, cookie_names=["my_session"]) == TOKEN


def test_falls_back_to_any_jwt_in_cookies():
    assert extract_access_token(f"weird_name_xyz={TOKEN}") == TOKEN


def test_returns_none_without_token():
    assert extract_access_token("theme=dark; lang=tr") is None
    assert extract_access_token("") is None


def test_extract_refresh_token_from_json_cookie():
    value = json.dumps({"access_token": TOKEN, "refresh_token": "r-123"})
    assert extract_refresh_token(f"sb-auth-token={quote(value)}") == "r-123"


def test_extract_refresh_token_from_json_array_cookie():
    value = json.dumps([TOKEN, "refresh-token", None, None])
    assert extract_refresh_token(f"sb-auth-token={quote(value)}") == "refresh-token"


def test_extract_refresh_token_from_base64_and_nested_session():
    payload = base64.urlsafe_b64encode(
        json.dumps({"currentSession": {"refreshToken": "nested-refresh"}}).encode()
    ).decode().rstrip("=")
    assert extract_refresh_token(f"sb-auth-token=base64-{payload}") == "nested-refresh"


def test_extract_refresh_token_from_chunked_cookie():
    value = base64.urlsafe_b64encode(
        json.dumps({"access_token": TOKEN, "refresh_token": "chunked-refresh"}).encode()
    ).decode().rstrip("=")
    middle = len(value) // 2
    header = f"sb-auth-token.0=base64-{value[:middle]}; sb-auth-token.1={value[middle:]}"
    assert extract_refresh_token(header) == "chunked-refresh"


def test_extract_raw_refresh_token_decodes_url_encoding():
    assert extract_refresh_token("refresh_token=refresh%3D%3D") == "refresh=="


def test_raw_chunked_refresh_token_is_joined_before_extraction():
    header = "refresh_token.0=first; refresh_token.1=second"
    assert extract_refresh_token(header) == "firstsecond"


def test_refresh_extraction_never_returns_access_token_by_mistake():
    assert extract_refresh_token(f"access_token={TOKEN}") is None
    assert extract_refresh_token("theme=dark") is None


def test_short_values_are_not_tokens():
    assert extract_access_token("access_token=abc") is None


def test_jwt_preferred_over_opaque_value():
    header = f"auth_token=short-opaque-but-long-enough; session_token={TOKEN}"
    assert extract_access_token(header) == TOKEN


def test_opaque_token_accepted_when_no_jwt():
    opaque = "a" * 40
    assert extract_access_token(f"access_token={opaque}") == opaque


# ---------------------------------------------------------------------- jwt
def test_looks_like_jwt():
    assert looks_like_jwt(TOKEN)
    assert not looks_like_jwt("not.a")
    assert not looks_like_jwt("")


def test_decode_claims_and_expiry():
    exp = int(time.time()) + 120
    token = make_jwt({"sub": "u", "exp": exp})
    assert decode_jwt_claims(token)["sub"] == "u"
    remaining = token_seconds_remaining(token)
    assert remaining is not None and 100 < remaining <= 120
    assert not is_token_expired(token)


def test_expired_token_detected():
    token = make_jwt({"exp": int(time.time()) - 60})
    assert is_token_expired(token)
    assert not is_token_expired(token, leeway=-120)


def test_token_without_exp_is_not_expired():
    token = make_jwt({"sub": "no-exp"})
    assert token_seconds_remaining(token) is None
    assert not is_token_expired(token)


def test_malformed_jwt_claims_are_empty():
    assert decode_jwt_claims("aaa.!!!notbase64!!!.ccc") == {}


# ------------------------------------------------------------------ resolve
def test_explicit_token_wins_over_cookie():
    other = make_jwt({"sub": "cookie"})
    resolved = resolve_bearer_token(
        explicit_token=TOKEN, cookie_header=f"access_token={other}"
    )
    assert resolved == TOKEN


def test_bearer_prefix_stripped_from_explicit_token():
    assert resolve_bearer_token(explicit_token=f"Bearer {TOKEN}") == TOKEN


# ------------------------------------------------------------------ headers
def test_authorization_header_built_from_cookie():
    settings = make_settings(upstream_cookie=f"access_token={TOKEN}; theme=dark",
                             upstream_auth_from_cookie=True)
    headers = build_stream_headers(settings, "chat-1")
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["cookie"] == f"access_token={TOKEN}; theme=dark"


def test_authorization_absent_when_no_token():
    settings = make_settings(upstream_cookie="theme=dark", upstream_auth_from_cookie=True)
    assert "authorization" not in build_stream_headers(settings, "chat-1")


def test_authorization_disabled_by_flag():
    settings = make_settings(
        upstream_cookie=f"access_token={TOKEN}", upstream_auth_from_cookie=False
    )
    assert build_authorization(settings) is None


def test_explicit_access_token_setting_used():
    settings = make_settings(upstream_access_token=TOKEN)
    assert build_stream_headers(settings, "c")["authorization"] == f"Bearer {TOKEN}"


def test_custom_auth_scheme():
    settings = make_settings(upstream_access_token=TOKEN, upstream_auth_scheme="Token")
    assert build_authorization(settings) == f"Token {TOKEN}"


def test_empty_scheme_sends_raw_token():
    settings = make_settings(upstream_access_token=TOKEN, upstream_auth_scheme="")
    assert build_authorization(settings) == TOKEN


def test_edge_user_agent_produces_edge_client_hints():
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36 Edg/154.0.0.0"
    )
    hints = sec_ch_ua(user_agent)
    assert '"Chromium";v="154"' in hints
    assert '"Microsoft Edge";v="154"' in hints
    assert '"Not A(Brand";v="99"' in hints
    assert "Google Chrome" not in hints


def test_extra_headers_can_override_authorization():
    settings = make_settings(
        upstream_access_token=TOKEN,
        upstream_extra_headers="authorization=Bearer manual-override",
    )
    headers = build_stream_headers(settings, "c")
    assert headers["authorization"] == "Bearer manual-override"


def test_captured_mobile_fingerprint_is_not_replaced_with_desktop_chrome():
    settings = make_settings(
        upstream_user_agent=(
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"
        ),
        upstream_extra_headers=(
            '{"sec-ch-ua":"\\"Chromium\\";v=\\"154\\", \\"Lemur\\";v=\\"154\\"",'
            '"sec-ch-ua-mobile":"?1","sec-ch-ua-platform":"\\"Android\\"",'
            '"sec-fetch-mode":"cors","accept":"*/*"}'
        ),
    )

    headers = build_stream_headers(settings, "chat-1")

    assert headers["sec-ch-ua"] == '"Chromium";v="154", "Lemur";v="154"'
    assert headers["sec-ch-ua-mobile"] == "?1"
    assert headers["sec-ch-ua-platform"] == '"Android"'
    assert headers["sec-fetch-mode"] == "cors"
    assert headers["accept"] == "*/*"

    # Aynı saklanan değerler HTML ısıtma isteğinin navigasyon kimliğini bozmamalı.
    page = build_page_headers(settings)
    assert page["sec-ch-ua-mobile"] == "?1"
    assert page["sec-fetch-mode"] == "navigate"
    assert page["accept"].startswith("text/html")


def test_configured_cookie_name_setting_is_honoured():
    decoy = make_jwt({"sub": "decoy"})
    settings = make_settings(
        upstream_cookie=f"tracking_jwt={decoy}; my_app_session={TOKEN}",
        upstream_token_cookie_names=["my_app_session"],
        upstream_auth_from_cookie=True,
    )
    assert build_authorization(settings) == f"Bearer {TOKEN}"


def test_expired_token_still_sent_with_warning(caplog):
    expired = make_jwt({"exp": int(time.time()) - 30})
    settings = make_settings(upstream_access_token=expired)
    assert build_authorization(settings) == f"Bearer {expired}"


@pytest.mark.parametrize(
    "cookie",
    [
        "access_token={t}",
        "sb-auth-token=%7B%22access_token%22%3A%22{t}%22%7D",
        "session=abc; access-token={t}; other=1",
    ],
)
def test_common_cookie_shapes(cookie):
    header = cookie.format(t=TOKEN)
    assert extract_access_token(header) == TOKEN


# --------------------------------------- gerçek hedef cURL'ünden gelen senaryolar
ARENA_COOKIE = (
    "arena-auth-prod-v1.0=base64-{blob}; cf_clearance=abcdefghijklmnop1234567890"
)


def _arena_cookie(token: str) -> str:
    blob = (
        base64.urlsafe_b64encode(json.dumps({"access_token": token}).encode())
        .decode()
        .rstrip("=")
    )
    return ARENA_COOKIE.format(blob=blob)


def test_arena_auth_cookie_is_recognised():
    assert extract_access_token(_arena_cookie(TOKEN)) == TOKEN


def test_cf_clearance_is_never_mistaken_for_token():
    """Cloudflare çerezi uzun ve opak; token sanılmamalı."""
    header = "cf_clearance=" + "Z" * 80 + "; theme=dark"
    assert extract_access_token(header) is None


def test_cf_clearance_ignored_even_with_token_in_name():
    header = "cf_clearance=" + "Q" * 60 + "; __cf_bm=" + "R" * 60
    assert extract_access_token(header) is None


def test_arena_cookie_preferred_over_cloudflare():
    header = _arena_cookie(TOKEN)
    assert "cf_clearance" in header
    assert extract_access_token(header) == TOKEN


# ------------------------- base64 çerezler (ön ekli ve ön eksiz)
def _b64_json(token: str, *, urlsafe: bool = False, prefix: str = "") -> str:
    raw = json.dumps({"access_token": token, "refresh_token": "r1"}).encode()
    encoded = (
        base64.urlsafe_b64encode(raw).decode().rstrip("=")
        if urlsafe
        else base64.b64encode(raw).decode()
    )
    return prefix + encoded


def test_base64_cookie_without_prefix_is_decoded():
    """Bazı sürümler 'base64-' ön eki koymadan doğrudan base64 gönderir."""
    header = f"arena-auth-prod-v1.0={_b64_json(TOKEN)}"
    assert extract_access_token(header) == TOKEN


def test_base64_cookie_without_prefix_alongside_cloudflare():
    header = (
        f"arena-auth-prod-v1.0={_b64_json(TOKEN)}; "
        "cf_clearance=abcdefghijklmnopqrstuvwxyz123456"
    )
    assert extract_access_token(header) == TOKEN


def test_url_encoded_base64_cookie():
    """'=' dolgusu %3D olarak kodlanmış olabilir."""
    header = f"arena-auth-prod-v1.0={quote(_b64_json(TOKEN))}"
    assert extract_access_token(header) == TOKEN


def test_urlsafe_base64_without_prefix():
    header = f"sb-auth-token={_b64_json(TOKEN, urlsafe=True)}"
    assert extract_access_token(header) == TOKEN


def test_base64_decoding_does_not_create_false_positives():
    """Rastgele base64 benzeri değerler token sanılmamalı."""
    assert extract_access_token("theme=" + "QUJDREVGR0hJSktMTU5PUFFSU1Q=") is None
    assert extract_access_token("sid=" + "Z" * 64) is None


def test_missing_token_does_not_raise():
    """Token yoksa hata değil, None dönmeli; istek Cookie ile devam eder."""
    assert extract_access_token("theme=dark; cf_clearance=" + "Q" * 40) is None


# ------------------------- sağlayıcı öneki bilinmeyen oturum çerezleri
_NOISE = (
    "__cf_bm=abc; _dd_s=rum=0; _ga=GA1.1.123; _ga_DB32ZN1WHB=x; "
    "cf_clearance=Zz0123456789abcdefghij; sidebar_state=true; "
    "user_country_code=TR; _visit_id=v1; "
    "ph_phc_LG7IJbVJqBsk584rbcKca0D5lV2vHguiijDrVji7yDM_posthog=p"
)


@pytest.mark.parametrize(
    "cookie_name",
    [
        "arena-auth-prod-v1.0",
        "lmarena-auth-prod-v1.0",
        "myapp-auth-prod-v1.0",
        "-auth-prod-v1.0",
        "sb-abcdefgh-auth-token.0",
    ],
)
def test_auth_cookie_found_regardless_of_vendor_prefix(cookie_name: str):
    """Ad öneki ne olursa olsun '...auth...' içeren çerez taranmalı.

    Gerçek dünyada çerez adı '<proje>-auth-prod-v1.0' biçiminde olabiliyor ve
    'arena-auth' gibi sabit ipuçlarına takılmıyordu.
    """
    payload = base64.b64encode(json.dumps({"access_token": TOKEN}).encode()).decode()
    header = f"{_NOISE}; {cookie_name}=base64-{payload}"
    assert extract_access_token(header) == TOKEN


def test_no_token_when_only_tracking_cookies_present():
    """Yalnızca analitik/Cloudflare çerezleri varsa token uydurulmamalı."""
    assert extract_access_token(_NOISE) is None


# ------------------------- kırpılmış (4096 baytta bölünmüş) oturum çerezleri
def _large_session_cookie() -> str:
    """4096 baytı aşan, gerçekçi bir Supabase oturum gövdesi üretir."""
    session = {
        "access_token": TOKEN,
        "token_type": "bearer",
        "refresh_token": "x" * 60,
        "user": {"id": "u" * 36, "user_metadata": {"bio": "b" * 3400}},
    }
    return "base64-" + base64.b64encode(json.dumps(session).encode()).decode()


@pytest.mark.parametrize("cut", [3000, 4096, 4515])
def test_truncated_session_cookie_is_salvaged(cut: int):
    """Tarayıcı çerezi böldüğünde kullanıcı sıklıkla yalnızca ilk parçayı kopyalar.

    Değer geçerli base64 olmadığı için tamamı çözülemez; yine de baştaki tam
    bloklardan access_token kurtarılabilmeli.
    """
    blob = _large_session_cookie()
    assert len(blob) > cut
    header = f"cf_clearance=zz; -auth-prod-v1.0={blob[:cut]}"
    assert extract_access_token(header) == TOKEN


def test_chunked_session_cookie_parts_are_joined():
    blob = _large_session_cookie()
    header = f"-auth-prod-v1.0={blob[:4096]}; -auth-prod-v1.1={blob[4096:]}"
    assert extract_access_token(header) == TOKEN


def test_undecodable_session_blob_is_not_sent_as_token():
    """Çözülemeyen base64 gövdesi Authorization başlığına konmamalı.

    Aksi hâlde upstream'e kilobaytlarca anlamsız bir Bearer değeri gider.
    """
    assert extract_access_token("-auth-prod-v1.0=base64-" + "Zm9vYmFy" * 600) is None


def _chunked_session_cookie(*, chunks: int = 2) -> str:
    import base64
    import json

    def jwt() -> str:
        head = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
        body = (
            base64.urlsafe_b64encode(json.dumps({"exp": 9999999999}).encode())
            .decode()
            .rstrip("=")
        )
        return f"{head}.{body}.sig"

    payload = {"access_token": jwt(), "refresh_token": "rt-fresh", "expires_in": 3600}
    value = "base64-" + base64.b64encode(json.dumps(payload).encode()).decode()
    size = len(value) // chunks + 1
    return "; ".join(
        f"arena-auth-prod-v1.{i}={value[i * size : (i + 1) * size]}" for i in range(chunks)
    )


def test_merge_prefers_chunked_value_when_base_lacks_refresh_token():
    """Eski kök çerez, yeni parçalı oturumun refresh token'ını gizlememeli.

    Canlı belirti: `/tani` "Çerezde token: var" + "Refresh token: yok" diyordu;
    otomatik yenileme bu yüzden hiç devreye girmiyordu.
    """
    stale_base = "base64-" + __import__("base64").b64encode(
        b'{"access_token":"eyJhbGciOiJFUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.sig"}'
    ).decode()
    combined = f"arena-auth-prod-v1={stale_base}; {_chunked_session_cookie()}"

    assert extract_refresh_token(combined) == "rt-fresh"
    assert extract_access_token(combined) is not None


def test_merge_keeps_base_when_it_already_has_refresh_token():
    """Kök zaten refresh token taşıyorsa parçalı değer onu ezmez."""
    import base64
    import json

    full = "base64-" + base64.b64encode(
        json.dumps(
            {"access_token": "eyJhbGciOiJFUzI1NiJ9.eyJleHAiOjk5OTk5OTk5OTl9.sig",
             "refresh_token": "rt-from-base"}
        ).encode()
    ).decode()
    combined = f"arena-auth-prod-v1={full}; {_chunked_session_cookie()}"

    # Kök değer refresh token taşıdığı için parçalar onun yerine geçmez.
    assert extract_refresh_token(combined) == "rt-from-base"
