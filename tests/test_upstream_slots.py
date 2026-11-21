"""Canlı Max yuva yönetimi (`/upstream_curl`, `/upstream_curl2`, `kapat`, `/agent_curl`)
ve Telegram 4096-karakter otomatik bölme birleştirme testleri.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.autopilot.curl_parse import (
    extract_all_max_identities,
    extract_identity_subject,
    normalize_windows_cmd_curl_for_upstream,
    parse_agent_curl,
    reassemble_agent_curl_chunks,
    reassemble_upstream_curl_chunks,
)
from app.autopilot.orchestrator import TR_TZ
from app.core.config import Settings
from app.main import create_app
from app.telegram.bot import TelegramBot, TelegramSettings
from app.upstream.auth import parse_cookie_header
from app.upstream.curl_runtime import apply_upstream_curl


def _make_chunked_auth_cookie(sub: str, *, include_slash: bool = True) -> str:
    """Gerçekçi parçalı Supabase JWT çerezi (`.0` + `.1`) üretir."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "sub": sub,
                    "role": "authenticated",
                    "aud": "authenticated",
                    "pad": ("A/B+C=" * 120) if include_slash else ("ABCDEF" * 120),
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    jwt_token = f"{header}.{payload}.sig_{sub}_xyz987"
    # Standart base64 (`/` ve `+` içerir) veya urlsafe karışımdan oluşan uzun oturum nesnesi:
    session_json = json.dumps(
        {
            "access_token": jwt_token,
            "refresh_token": "ref/" + ("9kQ+7mP/" * 180),
            "token_type": "bearer",
        }
    )
    encoded = "base64-" + base64.b64encode(session_json.encode()).decode()
    mid = len(encoded) // 2
    return (
        f"_ga=GA1.1.998877; cf_clearance={'c' * 450}; __cf_bm={'b' * 300}; "
        f"arena-auth-prod-v1.0={encoded[:mid]}; arena-auth-prod-v1.1={encoded[mid:]}"
    )


def _build_realistic_max_curl(
    *,
    sub: str,
    model_id: str = "019c-max-model-v1",
    deployment_id: str = "dpl_LiveDeployStamp123",
    cmd_format: bool = False,
) -> str:
    """13.000+ karakterlik gerçekçi `create-evaluation` Max cURL'ü üretir."""
    cookie = _make_chunked_auth_cookie(sub)
    recaptcha = "03AFcWeA_" + ("R9xK2mP_" * 530)  # ~4250 krk, tek mesaj sınırını aşar
    body_dict = {
        "id": "019da600-1111-7000-8000-000000000001",
        "mode": "direct",
        "modelAId": model_id,
        "userMessageId": "019da600-2222-7000-8000-000000000002",
        "modelAMessageId": "019da600-3333-7000-8000-000000000003",
        "messages": [
            {
                "id": "019da600-2222-7000-8000-000000000002",
                "role": "user",
                "content": "Merhaba dünya / test mesajı",
            }
        ],
        "modality": "chat",
        "recaptchaV3Token": recaptcha,
    }
    body_json = json.dumps(body_dict, separators=(",", ":"))
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )
    if cmd_format:
        esc_body = (
            body_json.replace('"', r'\^"')
            .replace("{", "^{")
            .replace("}", "^}")
            .replace("[", "^[")
            .replace("]", "^]")
        )
        return (
            'curl ^"https://target.example/nextjs-api/stream/create-evaluation^" ^\r\n'
            '  -H ^"accept: */*^" ^\r\n'
            '  -H ^"accept-language: tr-TR,tr;q=0.9,en-US;q=0.8^" ^\r\n'
            '  -H ^"content-type: text/plain;charset=UTF-8^" ^\r\n'
            f'  -b ^"{cookie}^" ^\r\n'
            '  -H ^"referer: https://target.example/c/019da600-1111-7000-8000-000000000001^" ^\r\n'
            f'  -H ^"user-agent: {ua}^" ^\r\n'
            f'  -H ^"x-arena-web-deployment: {deployment_id}^" ^\r\n'
            f'  --data-raw ^"{esc_body}^"'
        )
    return (
        "curl 'https://target.example/nextjs-api/stream/create-evaluation' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'accept-language: tr-TR,tr;q=0.9,en-US;q=0.8' \\\n"
        "  -H 'content-type: text/plain;charset=UTF-8' \\\n"
        f"  -b '{cookie}' \\\n"
        "  -H 'referer: https://target.example/c/019da600-1111-7000-8000-000000000001' \\\n"
        f"  -H 'user-agent: {ua}' \\\n"
        f"  -H 'x-arena-web-deployment: {deployment_id}' \\\n"
        f"  --data-raw '{body_json}'"
    )


def _build_realistic_agent_curl(
    *,
    sub: str,
    chat_id: str = "019d8420-fa0b-76a8-8b2f-268c897d22a9",
) -> str:
    """13.000+ karakterlik gerçekçi `/api/chat/<id>/input` Agent Mode cURL'ü üretir."""
    cookie = _make_chunked_auth_cookie(sub)
    recaptcha = "03AFcWeA_" + ("Z8wN4qL_" * 530)
    msg_id = "019da5dd-8284-7404-a813-595e1a05d5a7"
    prev_id = "019da5d9-f37d-71be-9d31-aa7711223344"
    body_json = json.dumps(
        {
            "kind": "message",
            "payload": {
                "message": {
                    "parts": [{"type": "text", "text": "test komutu"}],
                    "id": msg_id,
                    "role": "user",
                    "metadata": {"lastUserMessageId": prev_id},
                },
                "chatId": f"agent-runner:{chat_id}",
                "trigger": "submit-message",
                "messageId": msg_id,
                "metadata": {
                    "timezone": "Europe/Istanbul",
                    "arenaVisitId": "019da500-0000-7000-8000-000000000099",
                    "clientPlatform": "web",
                    "submissionSource": "chat_input",
                    "cx": "cx_live_fingerprint_val",
                    "recaptchaV3Token": recaptcha,
                },
            },
        },
        separators=(",", ":"),
    )
    return (
        f"curl 'https://target.example/api/chat/{chat_id}/input' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'accept-language: tr-TR,tr;q=0.9' \\\n"
        "  -H 'content-type: application/json' \\\n"
        f"  -b '{cookie}' \\\n"
        f"  -H 'referer: https://target.example/agent/{chat_id}' \\\n"
        "  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36' \\\n"
        f"  --data-raw '{body_json}'"
    )


def _simulate_telegram_hard_4096_split(text: str, limit: int = 4096) -> list[str]:
    """Telegram mobil/web ham 4096-karakter dilimlemesi."""
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _simulate_telegram_desktop_split(text: str, limit: int = 4096) -> list[str]:
    """Telegram Desktop (`tdesktop`) bölme algoritması: önce `\\n`, yoksa `' '` arar
    ve bölme noktasındaki ayırıcı karakteri tüketir; bulamazsa 4096'dan keser.
    """
    chunks: list[str] = []
    rem = text
    while len(rem) > limit:
        window = rem[:limit]
        nl = window.rfind("\n")
        if nl >= limit // 2:
            chunks.append(rem[:nl])
            rem = rem[nl + 1 :]
            continue
        sp = window.rfind(" ")
        if sp >= limit // 2:
            chunks.append(rem[:sp])
            rem = rem[sp + 1 :]
            continue
        chunks.append(rem[:limit])
        rem = rem[limit:]
    if rem:
        chunks.append(rem)
    return chunks


def _simulate_adversarial_slash_and_boundary_splits(text: str) -> list[str]:
    """Özellikle tehlikeli sınır noktalarından (`/537.36`, `/9kQ+`, `; `, ` \\\\n`) böler."""
    cuts: list[int] = []
    for needle in (" \\\n", "; arena-auth-prod-v1.0=", "/9kQ+", "/537.36", "03AFcWeA_R9xK"):
        pos = text.find(needle)
        if pos > 0:
            if needle == " \\\n":
                # Tam `\n` karakterini yutarak böl (tdesktop satır sonu davranışı)
                cuts.append(pos + 2)
            elif needle.startswith("; "):
                # `;` sonrası boşluğu yutarak böl
                cuts.append(pos + 1)
            else:
                # Tam `/` karakterinin önünden böl ki sonraki mesaj `/` ile başlasın!
                cuts.append(pos)
    cuts = sorted(set(cuts))
    if not cuts:
        return _simulate_telegram_hard_4096_split(text)

    chunks: list[str] = []
    prev = 0
    for c in cuts:
        chunk = text[prev:c]
        # `; ` ve ` \\\n` testlerinde ayırıcıyı simüle etmek için:
        if text[c : c + 1] in {"\n", " "}:
            chunks.append(chunk)
            prev = c + 1
        else:
            chunks.append(chunk)
            prev = c
    if prev < len(text):
        chunks.append(text[prev:])
    return chunks


@pytest.mark.parametrize("cmd_format", [False, True])
@pytest.mark.parametrize(
    "splitter",
    [
        _simulate_telegram_hard_4096_split,
        _simulate_telegram_desktop_split,
        _simulate_adversarial_slash_and_boundary_splits,
    ],
)
def test_reassemble_upstream_max_curl_matches_unsplit_across_all_telegram_splitters(
    cmd_format: bool,
    splitter: Any,
) -> None:
    full_curl = _build_realistic_max_curl(
        sub="google-user-test-999",
        model_id="019c-model-exact",
        cmd_format=cmd_format,
    )
    assert len(full_curl) > 9000

    expected_settings, expected_model = apply_upstream_curl(
        Settings(
            _env_file=None,
            upstream_curl=normalize_windows_cmd_curl_for_upstream(full_curl),
        )
    )
    chunks = splitter(full_curl)
    assert len(chunks) >= 3

    rejoined = reassemble_upstream_curl_chunks(chunks)
    actual_settings, actual_model = apply_upstream_curl(
        Settings(_env_file=None, upstream_curl=rejoined)
    )

    assert actual_model == expected_model == "019c-model-exact"
    assert actual_settings.target_domain == expected_settings.target_domain == "target.example"
    assert actual_settings.upstream_stream_path == expected_settings.upstream_stream_path
    assert actual_settings.upstream_mode == expected_settings.upstream_mode == "direct"
    assert (
        actual_settings.recaptcha_static_token
        == expected_settings.recaptcha_static_token
    )
    assert len(actual_settings.recaptcha_static_token) > 4200
    assert parse_cookie_header(actual_settings.upstream_cookie) == parse_cookie_header(
        expected_settings.upstream_cookie
    )
    assert (
        extract_identity_subject(
            actual_settings.upstream_cookie, actual_settings.upstream_access_token
        )
        == "google-user-test-999"
    )


@pytest.mark.parametrize(
    "splitter",
    [
        _simulate_telegram_hard_4096_split,
        _simulate_telegram_desktop_split,
        _simulate_adversarial_slash_and_boundary_splits,
    ],
)
def test_reassemble_agent_curl_matches_unsplit_across_all_telegram_splitters(
    splitter: Any,
) -> None:
    full_agent = _build_realistic_agent_curl(sub="google-agent-user-777")
    assert len(full_agent) > 9000
    expected = parse_agent_curl(full_agent)

    chunks = splitter(full_agent)
    assert len(chunks) >= 3

    rejoined = reassemble_agent_curl_chunks(chunks)
    actual = parse_agent_curl(rejoined)

    assert actual.chat_id == expected.chat_id
    assert actual.recaptcha_token == expected.recaptcha_token
    assert actual.cx == expected.cx == "cx_live_fingerprint_val"
    assert actual.account_identity() == "google-agent-user-777"
    assert actual.warnings == []
    assert parse_cookie_header(actual.cookie) == parse_cookie_header(expected.cookie)


def _sse_hello_bytes(label: str) -> bytes:
    lines = [
        f'a0:{json.dumps(f"Merhaba ({label})")}\n',
        'ad:{"finishReason":"stop"}\n',
    ]
    return "".join(lines).encode("utf-8")


@pytest.mark.asyncio
async def test_full_day_night_morning_lifecycle_via_telegram_end_to_end() -> None:
    """Gündüz 2 hesap Max -> akşam `/upstream_curl2 kapat` -> gece Hesap 2 ile Agent ->
    ters çakışma kilidi -> sabah tekrar `/upstream_curl2` + `/upstream_curl` canlı yenileme.
    """
    curl_max_1 = _build_realistic_max_curl(
        sub="google-hesap-1", model_id="model-slot1-initial"
    )
    curl_max_2 = _build_realistic_max_curl(
        sub="google-hesap-2", model_id="model-slot2-daytime"
    )
    curl_agent_2 = _build_realistic_agent_curl(sub="google-hesap-2")
    curl_agent_1 = _build_realistic_agent_curl(sub="google-hesap-1")
    curl_max_1_refreshed = _build_realistic_max_curl(
        sub="google-hesap-1",
        model_id="model-slot1-refreshed",
        deployment_id="dpl_RefreshedDeploy999",
    )

    seen_upstream_subs: list[str] = []

    def fake_upstream_transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        cookie_hdr = request.headers.get("cookie", "")
        auth_hdr = request.headers.get("authorization", "")
        tok = auth_hdr.split(" ", 1)[1].strip() if " " in auth_hdr else ""
        sub = extract_identity_subject(cookie_hdr, tok)
        seen_upstream_subs.append(sub)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_hello_bytes(sub or "unknown"),
        )

    settings = Settings(
        _env_file=None,
        api_keys=["sk-live-secret-key"],
        upstream_curl=curl_max_1,
        upstream_keepalive_interval_seconds=3600.0,
        upstream_keepalive_initial_delay_seconds=60.0,
        request_gap_min_seconds=0.0,
        request_gap_max_seconds=0.0,
        session_stateless=True,
    )
    app = create_app(settings)

    sent_telegram: list[str] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent_telegram.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg_settings = TelegramSettings(
        _env_file=None,
        telegram_bot_token="123456:TEST_TOKEN",
        wrapper_base_url="http://wrapper.internal",
        wrapper_api_key="sk-live-secret-key",
        telegram_allowed_chat_ids="42",
    )

    async with app.router.lifespan_context(app):
        # Upstream ağ çıkışlarını sahte transport'a bağla; gerçek `UpstreamClient`
        # `_account_clients` yaşam döngüsü ve `aclose()` davranışı birebir çalışsın!
        orig_build = app.state.upstream._build_client

        def patched_build_client(slot_settings: Settings) -> httpx.AsyncClient:
            real_client = orig_build(slot_settings)
            cookies_copy = real_client.cookies
            # Gerçek havuzu kapatıp MockTransport'lu gerçek AsyncClient döndür:
            asyncio.create_task(real_client.aclose())
            mocked = httpx.AsyncClient(
                transport=httpx.MockTransport(fake_upstream_transport),
                cookies=cookies_copy,
            )
            return mocked

        app.state.upstream._build_client = patched_build_client
        old_boot_client = app.state.upstream._client
        app.state.upstream._client = patched_build_client(app.state.settings)
        if old_boot_client is not None:
            await old_boot_client.aclose()

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as tg_http,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://wrapper.internal",
                headers={"Authorization": "Bearer sk-live-secret-key"},
            ) as wrapper_http,
        ):
            bot = TelegramBot(tg_settings, tg_http, wrapper_http)

            # ------------------------------------------------------------------
            # 1) GÜNDÜZ: Kullanıcı `/upstream_curl2 <13k cURL>` tek seferde yapıştırır!
            #    Telegram 4 parçaya böler (hatta bir parça `/` ile başlar) + `/bitir`
            # ------------------------------------------------------------------
            inline_paste = f"/upstream_curl2 {curl_max_2}"
            # Hem 4096 sınırı hem de `/537.36` ile başlayan parça simülasyonu:
            slash_idx = inline_paste.find("/537.36")
            assert slash_idx > 100
            raw_pieces = (
                _simulate_telegram_hard_4096_split(inline_paste[:slash_idx])
                + _simulate_telegram_hard_4096_split(inline_paste[slash_idx:])
            )
            assert any(p.startswith("/537.36") for p in raw_pieces[1:])

            for piece in raw_pieces:
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            # Hiçbir `/` ile başlayan parça "Bilinmeyen komut" hatası üretmemeli!
            assert not any("Bilinmeyen komut" in msg for msg in sent_telegram)

            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "Max 2. Hesap (/upstream_curl2) canlı olarak kaydedildi" in sent_telegram[-1]
            assert "Çift Hat (50/50 Yük Paylaşımı)" in sent_telegram[-1]

            assert app.state.lanes.multi is True
            assert len(app.state.lanes.lanes) == 2
            assert set(app.state.slot_manager._keepalive_tasks) == {1, 2}
            assert extract_all_max_identities(app.state.settings) == frozenset(
                {"google-hesap-1", "google-hesap-2"}
            )

            # 4 Max isteği at: 2'si Hesap 1'e, 2'si Hesap 2'ye bölünmeli!
            seen_upstream_subs.clear()
            for idx in range(4):
                r = await wrapper_http.post(
                    "/v1/chat/completions",
                    json={
                        "model": "max",
                        "messages": [{"role": "user", "content": f"Soru {idx}"}],
                    },
                )
                assert r.status_code == 200
            assert seen_upstream_subs.count("google-hesap-1") == 2
            assert seen_upstream_subs.count("google-hesap-2") == 2

            client1_day = app.state.upstream._account_clients[1]
            client2_day = app.state.upstream._account_clients[2]
            assert not client1_day.is_closed
            assert not client2_day.is_closed

            # `/hesaplar` çıktısında secret sızmadan iki yuva da sağlıklı görünmeli
            await bot.handle_message({"chat": {"id": 42}, "text": "/hesaplar"})
            assert "Çift Hesap (50/50 Yük Paylaşımı)" in sent_telegram[-1]
            assert "Yuva 1 (/upstream_curl): 🟢 Sağlıklı" in sent_telegram[-1]
            assert "Yuva 2 (/upstream_curl2): 🟢 Sağlıklı" in sent_telegram[-1]
            assert "arena-auth-prod" not in sent_telegram[-1]

            # ------------------------------------------------------------------
            # 2) AKŞAM: Kullanıcı yanlışlıkla `/upstream_curl2 kapat` demeden
            #    Hesap 2'nin Agent cURL'ünü `/agent_curl` ile yapıştırır -> UYARI!
            # ------------------------------------------------------------------
            await bot.handle_message({"chat": {"id": 42}, "text": "/agent_curl"})
            for piece in _simulate_telegram_desktop_split(curl_agent_2):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "2. Max hesabıyla (upstream_curl2) AYNI Google hesabına" in sent_telegram[-1]
            assert "/upstream_curl2 kapat" in sent_telegram[-1]

            # ------------------------------------------------------------------
            # 3) AKŞAM: `/upstream_curl2 kapat` -> Bağlantı ve keepalive KÖKTEN GİDER!
            # ------------------------------------------------------------------
            ka2_task = app.state.slot_manager._keepalive_tasks[2]
            await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl2 kapat"})
            assert "2. Max hesabı (upstream_curl2) kökten kapatıldı" in sent_telegram[-1]

            # Hesap 2'nin HTTP/2 istemcisi gerçekten `.aclose()` ile kapandı ve silindi:
            assert client2_day.is_closed is True
            assert 2 not in app.state.upstream._account_clients
            # Keepalive 2 iptal edildi ve silindi:
            assert ka2_task.done()
            assert 2 not in app.state.slot_manager._keepalive_tasks
            # Settings üzerinde Hesap 2'ye dair hiçbir çerez/cURL kalmadı:
            assert app.state.settings.upstream_cookie_2 == ""
            assert app.state.settings.upstream_curl_2 == ""
            assert extract_all_max_identities(app.state.settings) == frozenset(
                {"google-hesap-1"}
            )
            # Hesap 1'in HTTP istemcisi ise HİÇ kesintiye uğramadı!
            assert client1_day.is_closed is False
            assert app.state.lanes.multi is False

            # Max trafiğinin %100'ü artık yalnız Hesap 1'e gidiyor:
            seen_upstream_subs.clear()
            for idx in range(3):
                r = await wrapper_http.post(
                    "/v1/chat/completions",
                    json={
                        "model": "max",
                        "messages": [{"role": "user", "content": f"Akşam soru {idx}"}],
                    },
                )
                assert r.status_code == 200
            assert seen_upstream_subs == [
                "google-hesap-1",
                "google-hesap-1",
                "google-hesap-1",
            ]

            # ------------------------------------------------------------------
            # 4) GECE ÖNCESİ: Artık Hesap 2 `/agent_curl` ile ÇAKIŞMASIZ doğrulanır!
            #    (Önce yanlışlıkla Hesap 1'in Agent cURL'ünü denerse yine engellenir)
            # ------------------------------------------------------------------
            await bot.handle_message({"chat": {"id": 42}, "text": "/agent_curl"})
            for piece in _simulate_telegram_hard_4096_split(curl_agent_1):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "1. ana Max hesabıyla (upstream_curl) AYNI Google hesabına" in sent_telegram[-1]

            # Şimdi Hesap 2'nin Agent cURL'ünü yapıştır -> YEŞİL!
            await bot.handle_message({"chat": {"id": 42}, "text": "/agent_curl"})
            for piece in _simulate_telegram_desktop_split(curl_agent_2):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "Agent cURL doğrulandı" in sent_telegram[-1]

            # Gündüz saatinde (15:00 TR) projeyi gece sırasına bırakalım:
            import app.autopilot.orchestrator as orch_mod

            orig_dt = orch_mod.datetime

            class _DaytimeClock:
                @staticmethod
                def now(_tz: Any = None) -> datetime:
                    return datetime(2026, 11, 20, 15, 0, tzinfo=TR_TZ)

            orch_mod.datetime = _DaytimeClock  # type: ignore[assignment]
            try:
                await bot.handle_message(
                    {
                        "chat": {"id": 42},
                        "text": "Gece yapılacak kapsamlı proje yönetimi uygulaması",
                    }
                )
                assert (
                    "gece sırasına alındı" in sent_telegram[-1]
                    or "başlatıldı" in sent_telegram[-1]
                )
                await asyncio.sleep(0.05)

                # --------------------------------------------------------------
                # 5) TERS ÇAKIŞMA KİLİDİ: Gece Agent sırasında Hesap 2 beklerken
                #    yanlışlıkla `/upstream_curl2` ile Hesap 2'yi Max'a eklemeyi dene!
                # --------------------------------------------------------------
                await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl2"})
                for piece in _simulate_telegram_hard_4096_split(curl_max_2):
                    await bot.handle_message({"chat": {"id": 42}, "text": piece})
                await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
                assert "Otopilot Agent projesinde kullanılıyor" in sent_telegram[-1]
                assert app.state.lanes.multi is False

                # Gece projesini `/durdur` ile temizce durdur:
                await bot.handle_message({"chat": {"id": 42}, "text": "/durdur"})
                await asyncio.sleep(0.05)
            finally:
                orch_mod.datetime = orig_dt  # type: ignore[assignment]

            # ------------------------------------------------------------------
            # 6) ERTESİ SABAH: Taze `/upstream_curl2` ile 2. hesabı tekrar aç +
            #    `/upstream_curl` ile 1. hesabı deploy atmadan canlı tazele!
            # ------------------------------------------------------------------
            await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl2"})
            for piece in _simulate_telegram_hard_4096_split(curl_max_2):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "Max 2. Hesap (/upstream_curl2) canlı olarak kaydedildi" in sent_telegram[-1]
            assert app.state.lanes.multi is True

            # Aynı Hesap 2'yi yanlışlıkla 1. yuvaya (`/upstream_curl`) koymayı dene -> RED!
            await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl"})
            for piece in _simulate_telegram_hard_4096_split(curl_max_2):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "aynı hesaba ait" in sent_telegram[-1]

            # 1. yuvayı kendi taze cURL'ü ile canlı güncelle -> 1. yuva yenilenir, 2. yuva korunur!
            app.state.lanes.get(1).health.record_failure("upstream_refresh_required", 300.0)
            assert app.state.lanes.get(1).health.failing is True

            await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl"})
            for piece in _simulate_telegram_desktop_split(curl_max_1_refreshed):
                await bot.handle_message({"chat": {"id": 42}, "text": piece})
            await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
            assert "Max 1. Hesap (/upstream_curl) canlı olarak kaydedildi" in sent_telegram[-1]
            assert app.state.lanes.get(1).health.failing is False
            assert app.state.registry.resolve(None).upstream_id == "model-slot1-refreshed"
            assert client1_day.is_closed is True
            assert app.state.lanes.multi is True


@pytest.mark.asyncio
async def test_closing_slot_2_reassigns_queued_waiters_seamlessly_to_slot_1() -> None:
    """2. yuvanın sırasında bekleyen bir istek varken `/upstream_curl2 kapat`
    çağrıldığında bekleyen isteğin düşmeden 1. yuvaya devredilip 200 döndüğünü doğrular.
    """
    curl_max_1 = _build_realistic_max_curl(sub="slot1-user")
    curl_max_2 = _build_realistic_max_curl(sub="slot2-user")

    served_by: list[str] = []

    def fake_upstream_transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        cookie_hdr = request.headers.get("cookie", "")
        sub = extract_identity_subject(cookie_hdr, "")
        served_by.append(sub)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_hello_bytes(sub),
        )

    settings = Settings(
        _env_file=None,
        api_keys=["sk-test"],
        upstream_curl=curl_max_1,
        upstream_curl_2=curl_max_2,
        upstream_keepalive_interval_seconds=0.0,
        request_gap_min_seconds=0.0,
        request_gap_max_seconds=0.0,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        orig_build = app.state.upstream._build_client

        def patched_build(slot_settings: Settings) -> httpx.AsyncClient:
            real_c = orig_build(slot_settings)
            cookies_copy = real_c.cookies
            asyncio.create_task(real_c.aclose())
            return httpx.AsyncClient(
                transport=httpx.MockTransport(fake_upstream_transport),
                cookies=cookies_copy,
            )

        app.state.upstream._build_client = patched_build

        lane2 = app.state.lanes.get(2)
        assert lane2 is not None
        # Hat 2'yi meşgul tut ki sıradaki istek Hat 2 kuyruğunda beklemeye girsin;
        # Hat 1'de de 2 sahte yük varmış gibi gösterelim ki yeni istek Hat 2'yi seçsin:
        lane1 = app.state.lanes.get(1)
        assert lane1 is not None
        ticket2_busy = await lane2.queue.acquire("busy-holder")
        # Hat 1'i geçici olarak meşgul + 1 bekleyen yapalım, sonra bırakacağız:
        ticket1_busy = await lane1.queue.acquire("temp-slot1")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.internal",
            headers={"Authorization": "Bearer sk-test"},
        ) as client:
            # İlk atamayı Hat 1'e yapılmış say ki eşit yükte (`1 == 1`) Hat 2 seçilsin:
            lane1.last_assigned = 10
            lane2.last_assigned = 1

            req_task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json={"model": "max", "messages": [{"role": "user", "content": "Bekle"}]},
                )
            )
            await asyncio.sleep(0.05)
            assert lane2.queue.waiting == 1

            # Hat 1'i boşa çıkar ve hemen `/upstream_curl2 kapat` çalıştır:
            lane1.queue.release(ticket1_busy)
            close_resp = await client.post("/v1/autopilot/upstream-slots/2/close")
            assert close_resp.status_code == 200
            assert close_resp.json()["reassigned_waiters"] == 1

            chat_resp = await req_task
            assert chat_resp.status_code == 200
            assert served_by == ["slot1-user"]

            lane2.queue.release(ticket2_busy)


@pytest.mark.asyncio
async def test_inline_agent_curl_paste_and_wrong_curl_type_guards() -> None:
    """1) `/agent_curl <13k cURL>` tek kutuda yapıştırılıp Telegram tarafından bölündüğünde,
    2) Komutsuz Max cURL yapıştırılıp ardından `/upstream_curl2 bitir` dendiğinde,
    3) Agent cURL yanlışlıkla `/upstream_curl2`ye yapıştırıldığında veya PowerShell kopyalandığında
    tüm akışın kusursuz çalıştığını doğrular.
    """
    curl_max_1 = _build_realistic_max_curl(sub="main-user-1")
    curl_max_2 = _build_realistic_max_curl(sub="second-user-2")
    curl_agent_3 = _build_realistic_agent_curl(sub="agent-user-3")

    settings = Settings(
        _env_file=None,
        api_keys=["sk-test"],
        upstream_curl=curl_max_1,
        upstream_keepalive_interval_seconds=0.0,
    )
    app = create_app(settings)
    sent: list[str] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    tg_settings = TelegramSettings(
        _env_file=None,
        telegram_bot_token="123456:TEST_TOKEN",
        wrapper_base_url="http://wrapper.internal",
        wrapper_api_key="sk-test",
        telegram_allowed_chat_ids="42",
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as tg_http,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.internal",
            headers={"Authorization": "Bearer sk-test"},
        ) as wrapper_http,
    ):
        bot = TelegramBot(tg_settings, tg_http, wrapper_http)

        # A) Yanlışlıkla Agent cURL'ünü `/upstream_curl2`ye yapıştırma koruması:
        for piece in _simulate_telegram_hard_4096_split(f"/upstream_curl2 {curl_agent_3}"):
            await bot.handle_message({"chat": {"id": 42}, "text": piece})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert "Agent Mode sohbetine" in sent[-1]

        # B) Yanlışlıkla PowerShell `Invoke-WebRequest` kopyalama koruması:
        await bot.handle_message(
            {
                "chat": {"id": 42},
                "text": "/upstream_curl2 Invoke-WebRequest -Uri 'https://target.example/x'",
            }
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert "PowerShell" in sent[-1]

        # C) Komut yazmayı unutup doğrudan 13k Max cURL yapıştırma -> `/upstream_curl2 bitir`:
        await bot.handle_message({"chat": {"id": 42}, "text": "/iptal"})
        for piece in _simulate_telegram_hard_4096_split(curl_max_2):
            await bot.handle_message({"chat": {"id": 42}, "text": piece})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert "hangi yuvaya yazılacağını seçin" in sent[-1]

        await bot.handle_message({"chat": {"id": 42}, "text": "/upstream_curl2 bitir"})
        assert "Max 2. Hesap (/upstream_curl2) canlı olarak kaydedildi" in sent[-1]
        assert app.state.lanes.multi is True

        # D) Tek mesaj kutusunda `/agent_curl <13k Agent cURL>` + `/bitir`:
        for piece in _simulate_telegram_hard_4096_split(f"/agent_curl {curl_agent_3}"):
            await bot.handle_message({"chat": {"id": 42}, "text": piece})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert "Agent cURL doğrulandı" in sent[-1]

