"""Telegram botunun ağa çıkmadan çalışan sözleşme testleri."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest

from app.telegram.bot import TelegramBot, TelegramSettings, split_telegram_text


def make_settings(**overrides: Any) -> TelegramSettings:
    values: dict[str, Any] = {
        "telegram_bot_token": "test-bot-token",
        "wrapper_api_key": "test-wrapper-key",
        "wrapper_base_url": "https://wrapper.test",
        "telegram_allowed_chat_ids": "42",
    }
    values.update(overrides)
    return TelegramSettings(_env_file=None, **values)


def _jwt(*, exp: float) -> str:
    """İmzası doğrulanmayan, `exp` taşıyan sahte JWT (yalnız biçim testi)."""
    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature"


def ok_telegram(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": True})


@pytest.mark.asyncio
async def test_empty_allowlist_reveals_chat_id_without_calling_wrapper():
    sent: list[dict[str, Any]] = []
    wrapper_calls = 0

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        nonlocal wrapper_calls
        wrapper_calls += 1
        return httpx.Response(500)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(
            make_settings(telegram_allowed_chat_ids=""), telegram, wrapper
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "test"})

    assert wrapper_calls == 0
    assert "42" in sent[0]["text"]
    assert "TELEGRAM_ALLOWED_CHAT_IDS" in sent[0]["text"]


@pytest.mark.asyncio
async def test_allowed_message_becomes_non_streaming_openai_request():
    sent: list[dict[str, Any]] = []
    received: dict[str, Any] = {}

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        received.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-wrapper-key"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Merhaba"}}]},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            headers={"Authorization": "Bearer test-wrapper-key"},
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "Selam"})

    assert received == {
        "model": "max",
        "messages": [{"role": "user", "content": "Selam"}],
        "stream": False,
    }
    assert sent[0]["text"] == "Merhaba"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "invalid_api_key", "401"),
        (429, "upstream_rate_limited", "429"),
        (502, "upstream_bad_gateway", "502"),
    ],
)
async def test_wrapper_errors_are_short_and_structured(status, code, expected):
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": code, "message": "secret-looking raw body"}},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        await TelegramBot(make_settings(), telegram, wrapper).handle_message(
            {"chat": {"id": 42}, "text": "test"}
        )

    assert expected in sent[0]["text"]
    assert code in sent[0]["text"]
    assert "secret-looking raw body" not in sent[0]["text"]


@pytest.mark.asyncio
async def test_poll_advances_offset_and_processes_message():
    sent: list[dict[str, Any]] = []
    polls: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("/getUpdates"):
            polls.append(payload)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {"update_id": 100, "message": {"chat": {"id": 42}, "text": "/ping"}}
                    ],
                },
            )
        sent.append(payload)
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        next_offset = await bot.poll_once(None)

    assert next_offset == 101
    assert polls[0]["allowed_updates"] == ["message"]
    assert "Wrapper /healthz" in sent[0]["text"]


@pytest.mark.asyncio
async def test_unauthorized_chat_is_ignored():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(telegram_handler)
    ) as telegram:
        bot = TelegramBot(make_settings(), telegram, telegram)
        await bot.handle_message({"chat": {"id": 99}, "text": "kotayı harcama"})

    assert sent == []


def test_long_text_is_bounded():
    assert len(split_telegram_text("x" * 10000, limit=3500, max_chars=12000)) == 3
    shortened = split_telegram_text("x" * 20000, limit=3500, max_chars=12000)
    assert sum(len(part) for part in shortened) <= 12000
    assert "yanıt kısaltıldı" in shortened[-1]


@pytest.mark.asyncio
async def test_cookie_command_reports_token_sync():
    """/cookie yanıtı, bearer token da güncellendiyse bunu söylemeli.

    Telefonda tek geri bildirim bu mesaj: kullanıcı "çerez uygulandı" görüp
    yine 401 alırsa nedenini bilemez. Token eşitlemesi yeni davranış olduğu için
    görünür olmalı.
    """
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        if "dry_run" in request.url.query.decode():
            return httpx.Response(
                200,
                json={
                    "status": "inspected",
                    "safe_to_apply": True,
                    "cookie_names": ["arena-auth-prod-v1.0"],
                    "session_cookie_found": True,
                    "access_token_found": True,
                    "access_token_expired": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "fields": ["upstream_cookie", "upstream_access_token"],
                "accounts_updated": {"hesap-1": ["cookie"]},
                "clients_reset": 1,
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "/cookie arena-auth-prod-v1.0=deger"}
        )

    assert "Çerez uygulandı" in sent[-1]["text"]
    assert "Bearer token da güncellendi" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_yenile_failure_names_the_cookies_that_arrived():
    """`/yenile` başarısızken "çerez var" demek yetmiyor: hangi adlar geldi?"""
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "not_captured",
                "logged_in": False,
                "cookie_found": True,
                "deployment_found": True,
                "cx_found": False,
                "injected_cookies": 2,
                "cookie_names": ["__cf_bm", "_dd_s"],
                "session_cookie_found": False,
                "login_redirect": False,
                "fields": ["upstream_extra_headers"],
                "reason": "gelen çerezlerde oturum taşıyan ad yok",
                "next": "Oturum yakalanamadı: ... TÜM çerezleri /cookie ile gönderin.",
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/yenile"})

    text = sent[-1]["text"]
    assert "Oturum yakalanamadı" in text
    assert "__cf_bm, _dd_s" in text
    assert "Oturum çerezi" in text
    assert "yüklenen çerez: 2" in text


@pytest.mark.asyncio
async def test_cookie_without_session_is_refused_before_applying():
    """Oturum çerezi olmayan set, çalışan oturumun üzerine yazmamalı."""
    sent: list[dict[str, Any]] = []
    applied_calls = 0

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        nonlocal applied_calls
        if "dry_run" in request.url.query.decode():
            return httpx.Response(
                200,
                json={
                    "status": "inspected",
                    "safe_to_apply": False,
                    "cookie_names": ["__cf_bm", "_dd_s"],
                    "session_cookie_found": False,
                    "access_token_found": False,
                },
            )
        applied_calls += 1
        return httpx.Response(200, json={"status": "applied", "fields": ["upstream_cookie"]})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie __cf_bm=bm; _dd_s=rum"})

    assert applied_calls == 0, "uygunsuz çerez uygulanmamalı"
    assert "__cf_bm" in sent[-1]["text"]
    assert "/cookie!" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_cookie_bang_forces_the_swap():
    """/cookie! kontrolü atlar: oturum adı tanınmayan ama geçerli setler de geçebilsin."""
    applied_calls = 0
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        nonlocal applied_calls
        if "dry_run" in request.url.query.decode():
            return httpx.Response(
                200,
                json={"status": "inspected", "safe_to_apply": False, "cookie_names": []},
            )
        applied_calls += 1
        return httpx.Response(200, json={"status": "applied", "fields": ["upstream_cookie"]})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie! ozel_isim=deger"})

    assert applied_calls == 1
    assert "Çerez uygulandı" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_curl_command_shows_warnings_from_wrapper():
    """Yanlış isteğin cURL'ü yapıştırıldığında kullanıcı uyarıyı görmeli."""
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "fields": ["upstream_cookie"],
                "accounts_updated": {},
                "clients_reset": 0,
                "warnings": [
                    "Bu cURL sohbet akış isteği gibi görünmüyor (yol: /). "
                    "Canlı akış yolu korundu."
                ],
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "/curl curl 'https://upstream.test/' -H 'cookie: a=b'"}
        )

    assert "⚠" in sent[-1]["text"]
    assert "Canlı akış yolu korundu" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_curl_command_explains_when_same_settings_are_already_loaded():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "applied", "fields": [], "accounts_updated": {}},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "/curl curl 'https://upstream.test/'"}
        )

    text = sent[-1]["text"]
    assert "ayarlar zaten yüklü" in text
    assert "Aynı cURL'ü tekrar" in text
    assert "0 alan güncellendi" not in text


@pytest.mark.asyncio
async def test_long_curl_is_buffered_and_applied_only_after_finish():
    sent: list[dict[str, Any]] = []
    uploaded: list[str] = []
    full_curl = (
        "curl --url 'https://upstream.test/nextjs-api/stream/create-evaluation' "
        "-H 'accept: */*' "
        "-b 'arena-auth-prod-v1.0=base64-part-one; arena-auth-prod-v1.1=part-two' "
        "--data-raw '{\"id\":\"chat-1\",\"userMessage\":{\"content\":\"Merhaba\"}}'"
    )
    # Bir sınır özellikle iki bayrak arasındaki boşluğun içinde: birleştirme
    # ne bu boşluğu kaybetmeli ne de token içine ek boşluk koymalı.
    cuts = [0, 95, 180, len(full_curl)]
    parts = [full_curl[cuts[i] : cuts[i + 1]] for i in range(len(cuts) - 1)]

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        uploaded.append(json.loads(request.content)["curl"])
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "fields": ["upstream_cookie"],
                "accounts_updated": {"hesap-1": True},
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl"})
        for part in parts:
            await bot.handle_message({"chat": {"id": 42}, "text": "/curl " + part})
            assert not uploaded, "parçalar bitmeden cURL uygulanmamalı"
        assert "parça modunu" in sent[0]["text"]
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl bitir"})

    assert uploaded == [full_curl]
    assert "cURL uygulandı" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_curl_chunk_mode_can_be_cancelled():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl"})
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl curl 'https://x'"})
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl iptal"})
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl bitir"})

    assert "temizlendi" in sent[-2]["text"]
    assert "Bekleyen cURL parçası yok" in sent[-1]["text"]


def _auth_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cookie_count": 3,
        "cookie_names_present": ["arena-auth-prod-v1.0", "__cf_bm", "_dd_s"],
        "session_cookie_candidates": [{"cookie": "arena-auth-prod-v1.0"}],
        "token": {"found": True, "expired": False, "expires_in_seconds": 3600},
        "cookie_token": {
            "found": True,
            "is_jwt": True,
            "expired": False,
            "expires_in_seconds": 3600,
            "used_for_authorization": True,
        },
        "extract_from_cookie_enabled": True,
        "refresh_token": {"found": False, "length": 0},
        "authorization_header_will_be_sent": True,
    }
    payload.update(overrides)
    return payload


async def _run_diagnose(payload: dict[str, Any]) -> str:
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/auth"
        return httpx.Response(200, json=payload)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/tani"})
    return str(sent[-1]["text"])


@pytest.mark.asyncio
async def test_diagnose_reports_healthy_identity():
    text = await _run_diagnose(_auth_payload())
    assert "Çerez: 3 ad" in text
    assert "arena-auth-prod-v1.0" in text
    assert "Çerezde token: var" in text
    assert "Kullanılan token: var" in text
    assert "sağlam" in text


@pytest.mark.asyncio
async def test_diagnose_does_not_claim_cookie_extraction_failed_when_auth_is_disabled():
    payload = _auth_payload(
        token={"found": False, "expired": False},
        cookie_token={
            "found": True,
            "is_jwt": True,
            "expired": False,
            "expires_in_seconds": 3480,
            "used_for_authorization": False,
        },
        extract_from_cookie_enabled=False,
        refresh_token={"found": True, "length": 12},
        authorization_header_will_be_sent=False,
        cookie_inspection={
            "chunked_families": {"arena-auth-prod-v1": 2},
            "problems": [],
        },
    )

    text = await _run_diagnose(payload)
    advice = TelegramBot._diagnose_advice(payload, "BAŞARISIZ (HTTP 401)")

    assert "Parçalı oturum: arena-auth-prod-v1: 2 parça" in text
    assert "Refresh token: var (12 karakter)" in text
    assert "ayıklanamadı" not in advice
    assert "access token başarıyla okunabiliyor" in advice
    assert "cookie-only" in advice


@pytest.mark.asyncio
async def test_diagnose_shows_live_cookie_integrity_problem():
    text = await _run_diagnose(
        _auth_payload(
            cookie_inspection={
                "chunked_families": {"arena-auth-prod-v1": 2},
                "problems": [
                    "`arena-auth-prod-v1` parçalarının birleşimi çözülemedi (base64 bozuk)"
                ],
            }
        )
    )

    assert "arena-auth-prod-v1: 2 parça" in text
    assert "birleşimi çözülemedi" in text


@pytest.mark.asyncio
async def test_diagnose_points_to_cookie_when_no_credentials():
    text = await _run_diagnose(
        _auth_payload(
            cookie_count=0,
            cookie_names_present=[],
            session_cookie_candidates=[],
            token={"found": False, "expired": False},
            cookie_token={"found": False, "expired": False},
            authorization_header_will_be_sent=False,
        )
    )
    assert "hiç yok" in text
    assert "/cookie" in text


@pytest.mark.asyncio
async def test_diagnose_explains_expired_token():
    text = await _run_diagnose(
        _auth_payload(
            token={"found": True, "expired": True, "expires_in_seconds": -60},
            cookie_token={
                "found": True,
                "is_jwt": True,
                "expired": True,
                "expires_in_seconds": -60,
            },
        )
    )
    assert "süresi dolmuş" in text
    assert "/cookie" in text
    assert "çözmez" in text


@pytest.mark.asyncio
async def test_diagnose_reports_refresh_token_when_access_token_expired():
    """Süresi dolmuş erişim token'ı + duran refresh token ayrı anlatılmalı."""
    text = await _run_diagnose(
        _auth_payload(
            token={"found": True, "expired": True, "expires_in_seconds": -60},
            cookie_token={
                "found": True,
                "is_jwt": True,
                "expired": True,
                "expires_in_seconds": -60,
            },
            refresh_token={"found": True, "length": 42},
        )
    )
    assert "Refresh token: var (42 karakter)" in text
    assert "refresh token duruyor" in text


def _export_json(chunk_count: int = 2) -> list[dict[str, Any]]:
    """Cookie-Editor JSON dışa aktarımına benzeyen gerçek biçim (parçalı çerez)."""
    payload = {
        "access_token": _jwt(exp=time.time() + 3000),
        "refresh_token": "test-refresh-token",
        "expires_in": 3600,
    }
    value = "base64-" + base64.b64encode(json.dumps(payload).encode()).decode()
    size = len(value) // chunk_count + 1
    items: list[dict[str, Any]] = [
        {
            "domain": "arena.ai",
            "hostOnly": True,
            "httpOnly": False,
            "name": f"arena-auth-prod-v1.{index}",
            "path": "/",
            "sameSite": "lax",
            "secure": False,
            "session": False,
            "value": value[index * size : (index + 1) * size],
        }
        for index in range(chunk_count)
    ]
    items.append(
        {
            "domain": ".arena.ai",
            "hostOnly": False,
            "httpOnly": True,
            "name": "__cf_bm",
            "path": "/",
            "sameSite": "no_restriction",
            "secure": True,
            "session": False,
            "value": "bm-value",
        }
    )
    return items


@pytest.mark.asyncio
async def test_cookie_json_uploaded_in_parts_is_joined_then_applied():
    """Telegram 4 KB sınırı: uzun JSON parça parça gönderilip birleştirilmeli."""
    calls: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append({"dry": "dry_run" in request.url.query.decode(), "cookie": body.get("cookie")})
        if "dry_run" in request.url.query.decode():
            return httpx.Response(
                200,
                json={
                    "status": "inspected",
                    "applied": False,
                    "safe_to_apply": True,
                    "cookie_names": ["arena-auth-prod-v1.0", "arena-auth-prod-v1.1"],
                    "session_cookie_found": True,
                    "access_token_found": True,
                    "access_token_expired": False,
                    "access_token_expires_in_seconds": 3000,
                    "refresh_token_found": True,
                    "chunked_families": {"arena-auth-prod-v1": 2},
                    "problems": [],
                },
            )
        return httpx.Response(
            200, json={"status": "applied", "fields": ["upstream_cookie"]}
        )

    export = json.dumps(_export_json())
    first, second = export[: len(export) // 2], export[len(export) // 2 :]

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie " + first})
        assert not calls, "yarım JSON uygulanmamalı"
        assert "Parça alındı" in sent[-1]["text"]
        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie " + second})

    assert calls[0]["dry"] is True
    assert calls[-1]["dry"] is False
    assert json.loads(calls[-1]["cookie"]) == json.loads(export), "parçalar birebir birleşmeli"
    assert "Çerez uygulandı" in sent[-1]["text"]
    assert "Refresh token: var" in sent[-1]["text"]
    assert "≈50 dk" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_cookie_finish_and_cancel_commands():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie iptal"})
        assert "temizlendi" in sent[-1]["text"].lower()

        await bot.handle_message({"chat": {"id": 42}, "text": "/cookie bitir"})
        assert "Bekleyen parça yok" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_token_command_reports_refresh_success():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/upstream/refresh"
        return httpx.Response(
            200, json={"status": "refreshed", "expires_in": 3600}
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/token"})

    assert "Token yenilendi" in sent[-1]["text"]
    assert "60 dk" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_token_command_explains_failure():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "failed",
                "detail": "Refresh token geçersiz/süresi dolmuş görünüyor; taze çerez gerekir.",
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/token"})

    assert "yenilenemedi" in sent[-1]["text"]
    assert "taze çerez" in sent[-1]["text"]


@pytest.mark.asyncio
async def test_apikey_command_reports_that_refresh_happened():
    """/apikey 'kaydedildi' deyip susmamalı; token gerçekten tazelendiyse söylemeli."""
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/upstream/apikey"
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "source": "manual",
                "has_api_key": True,
                "length": 11,
                "refreshed": True,
                "refresh_detail": "Erişim token'ı yenilendi.",
                "expires_in": 3600,
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram=telegram, wrapper=wrapper)
        await bot.set_refresh_apikey(42, "/apikey anon-manual")

    text = sent[-1]["text"]
    assert "kaydedildi" in text
    assert "Token yenilendi" in text
    assert "~60 dk" in text


@pytest.mark.asyncio
async def test_apikey_command_warns_when_refresh_still_fails():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "refreshed": False,
                "refresh_detail": "Upstream `apikey` istiyor.",
                "length": 9,
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram=telegram, wrapper=wrapper)
        await bot.set_refresh_apikey(42, "/apikey wrong-key")

    text = sent[-1]["text"]
    assert "token hâlâ yenilenemedi" in text
    assert "/cookie" in text


@pytest.mark.asyncio
async def test_apikey_discovery_failure_shows_why_and_how_many_scripts_were_scanned():
    sent: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "not_found",
                "detail": "Hedefin ana sayfası HTTP 403 döndü.",
                "home_status": 403,
                "scripts_seen": 0,
                "scripts_fetched": 0,
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram=telegram, wrapper=wrapper)
        await bot.set_refresh_apikey(42, "/apikey")

    text = sent[-1]["text"]
    assert "403" in text
    assert "/apikey <anahtar>" in text
