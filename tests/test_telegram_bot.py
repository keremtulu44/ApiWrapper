"""Telegram botunun ağa çıkmadan çalışan sözleşme testleri."""

from __future__ import annotations

import json
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


def _sample_windows_cmd_agent_curl() -> str:
    chat_id = "019d8420-fa0b-76a8-8b2f-268c897d22a9"
    long_cookie = "a=" + ("X" * 4500) + "; arena-auth-prod-v1.0=tok_part_1; cf_clearance=ok"
    long_recaptcha = "03AFcWeA_" + ("R" * 4200)
    raw_json = json.dumps(
        {
            "kind": "message",
            "payload": {
                "chatId": f"agent-runner:{chat_id}",
                "message": {
                    "parts": [{"type": "text", "text": "Selam"}],
                    "id": "019d8420-fa0b-7000-8000-000000000001",
                    "role": "user",
                    "metadata": {
                        "lastUserMessageId": "019d8420-fa0b-7000-8000-000000000000"
                    },
                },
                "metadata": {"recaptchaV3Token": long_recaptcha, "cx": "cx_ok"},
            },
        },
        separators=(",", ":"),
    )
    # Gerçek Chrome "Copy as cURL (cmd)" kaçışları: `"` -> `\^"`, `{` -> `^{`, `}` -> `^}`
    cmd_body = (
        raw_json.replace('"', r'\^"')
        .replace("{", "^{")
        .replace("}", "^}")
    )
    return (
        f'curl ^"https://target.example/api/chat/{chat_id}/input^" ^\n'
        '  -H ^"accept: */*^" ^\n'
        f'  -b ^"{long_cookie}^" ^\n'
        '  -H ^"content-type: application/json^" ^\n'
        f'  --data-raw ^"{cmd_body}^"'
    )


@pytest.mark.asyncio
async def test_telegram_multipart_curl_collection_and_multi_project_flow():
    """Telegram'ın 4 parçaya böldüğü CMD cURL'ünü birleştirip arka arkaya proje başlatır."""
    from app.autopilot.curl_parse import parse_agent_curl

    agent_link = "https://target.example/agent/019d8420-fa0b-76a8-8b2f-268c897d22a9"
    full_curl = _sample_windows_cmd_agent_curl()
    assert len(full_curl) > 8500
    # 3000 karakterlik 4 parçaya böl (hem çerezin hem JSON gövdesinin tam ortasından keser)
    chunks = [full_curl[i : i + 3000] for i in range(0, len(full_curl), 3000)]
    assert len(chunks) == 4

    sent: list[str] = []
    started_runs: list[dict[str, Any]] = []
    chat_completion_calls = 0

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        nonlocal chat_completion_calls
        path = request.url.path
        if path == "/v1/chat/completions":
            chat_completion_calls += 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Normal"}}]},
            )
        if path == "/v1/autopilot/inspect-curl":
            body = json.loads(request.content)
            cfg = parse_agent_curl(body["agent_curl"])
            assert cfg.recaptcha_token.startswith("03AFcWeA_")
            assert len(cfg.recaptcha_token) == 4209
            assert "arena-auth-prod-v1.0=tok_part_1" in cfg.cookie
            assert cfg.warnings == []
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "target_domain": cfg.target_domain,
                    "chat_id": cfg.chat_id,
                    "source_endpoint_kind": cfg.source_endpoint_kind,
                    "same_account_conflict": False,
                    "warnings": [],
                },
            )
        if path == "/v1/autopilot/runs" and request.method == "POST":
            body = json.loads(request.content)
            started_runs.append(body)
            idx = len(started_runs)
            return httpx.Response(
                200,
                json={
                    "run_id": f"auto_proj{idx}",
                    "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "target_domain": "target.example",
                    "agent_url": agent_link,
                    "status": "running",
                    "max_turns": body["max_turns"],
                },
            )
        if path == "/v1/autopilot/runs" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "active_count": 1,
                    "runs": [
                        {
                            "run_id": "auto_proj2",
                            "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                            "agent_url": agent_link,
                            "project_idea": "İkinci proje: Kanban panosu",
                            "status": "running",
                            "current_turn": 2,
                            "max_turns": 8,
                            "last_note": "Tur 2 çalışıyor",
                        },
                        {
                            "run_id": "auto_proj1",
                            "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                            "agent_url": agent_link,
                            "project_idea": "Birinci proje: Kişisel finans uygulaması",
                            "status": "completed",
                            "current_turn": 6,
                            "max_turns": 10,
                            "stop_reason": "Tüm testler geçti.",
                        },
                    ],
                },
            )
        if path == "/v1/autopilot/runs/auto_proj1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "run_id": "auto_proj1",
                    "status": "completed",
                    "current_turn": 6,
                    "max_turns": 10,
                    "current_phase": "4_DOGRULAMA_CILALAMA",
                    "agent_url": agent_link,
                    "project_idea": "Birinci proje: Kişisel finans uygulaması",
                    "stop_reason": "Tüm testler geçti.",
                    "memory_summary": "FastAPI + SQLite + grafikler tamamlandı.",
                    "turns": [
                        {
                            "turn": 6,
                            "phase": "4_DOGRULAMA_CILALAMA",
                            "duration_seconds": 18.4,
                            "tool_count": 3,
                            "bash_count": 2,
                            "error_tool_count": 0,
                        }
                    ],
                },
            )
        if path == "/v1/autopilot/runs/auto_proj2/stop" and request.method == "POST":
            return httpx.Response(
                200, json={"run_id": "auto_proj2", "status": "running", "stop_requested": True}
            )
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)

        # 1. Proje: /curl -> 4 parça yapıştır -> /curl bitir -> /tur 10 -> proje fikri
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl"})
        for piece in chunks:
            await bot.handle_message({"chat": {"id": 42}, "text": piece})
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl bitir"})
        assert "Agent cURL doğrulandı (4 parça birleştirildi)" in sent[-1]

        await bot.handle_message({"chat": {"id": 42}, "text": "/tur 10"})
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "Birinci proje: Kişisel finans uygulaması"}
        )
        assert "auto_proj1" in sent[-1]
        assert started_runs[0]["max_turns"] == 10

        # 2. Proje (/curl yazmayı unutup doğrudan yapıştırma koruması + /bitir kısayolu)
        for piece in chunks:
            await bot.handle_message({"chat": {"id": 42}, "text": piece})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        await bot.handle_message({"chat": {"id": 42}, "text": "/tur 8"})
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "İkinci proje: Kanban panosu"}
        )
        assert "auto_proj2" in sent[-1]

        # Diğer projelere bakma (/projeler ve /durum auto_proj1) ve durdurma (/durdur)
        await bot.handle_message({"chat": {"id": 42}, "text": "/projeler"})
        assert "auto_proj1" in sent[-1] and "auto_proj2" in sent[-1]

        await bot.handle_message({"chat": {"id": 42}, "text": "/durum auto_proj1"})
        assert "FastAPI + SQLite" in sent[-1]

        await bot.handle_message({"chat": {"id": 42}, "text": "/durdur"})
        assert "auto_proj2 için durdurma işareti gönderildi" in sent[-1]

    # Hiçbir cURL parçası yanlışlıkla /v1/chat/completions'a gitmemeli!
    assert chat_completion_calls == 0
    assert len(started_runs) == 2


@pytest.mark.asyncio
async def test_telegram_rejects_same_google_account_conflict():
    sent: list[str] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": True,
                "target_domain": "target.example",
                "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                "source_endpoint_kind": "input",
                "same_account_conflict": True,
                "warnings": [],
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
        await bot.handle_message(
            {"chat": {"id": 42}, "text": 'curl "https://target.example/api/chat/1/input" -b "a=1"'}
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl bitir"})

    assert "AYNI Google hesabına" in sent[-1]


@pytest.mark.asyncio
async def test_telegram_default_night_window_auto_turns_and_simdi_wake():
    sent: list[str] = []
    started_payloads: list[dict[str, object]] = []
    start_now_called: list[str] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return ok_telegram(request)

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/autopilot/inspect-curl":
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "target_domain": "target.example",
                    "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "source_endpoint_kind": "input",
                    "same_account_conflict": False,
                    "warnings": [],
                },
            )
        if path == "/v1/autopilot/runs" and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            started_payloads.append(body)
            return httpx.Response(
                200,
                json={
                    "run_id": "auto_night1",
                    "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "agent_url": "https://target.example/agent/019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "status": "waiting_window",
                    "max_turns": body["max_turns"],
                    "auto_turns": body["auto_turns"],
                    "respect_night_window": body["respect_night_window"],
                },
            )
        if path == "/v1/autopilot/runs" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "active_count": 1,
                    "runs": [
                        {
                            "run_id": "auto_night1",
                            "status": "waiting_window",
                            "current_turn": 0,
                            "max_turns": 12,
                            "planned_turns": 0,
                            "project_idea": "Gece yapılacak proje",
                        }
                    ],
                },
            )
        if path == "/v1/autopilot/runs/auto_night1/start-now" and request.method == "POST":
            start_now_called.append("auto_night1")
            return httpx.Response(
                200,
                json={
                    "run_id": "auto_night1",
                    "status": "running",
                    "bypassed_night_window": True,
                },
            )
        if path == "/v1/autopilot/runs/auto_night1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "run_id": "auto_night1",
                    "status": "completed",
                    "agent_url": "https://target.example/agent/019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "evening_plan": "- Mimari: FastAPI + React",
                    "handoff_report": "1. Neler Yapıldı: Simülatör tamamlandı.\n3. Kontrol edin.",
                },
            )
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(make_settings(), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": "/curl"})
        sample_curl = (
            'curl "https://target.example/api/chat/'
            '019d8420-fa0b-76a8-8b2f-268c897d22a9/input" -b "a=1"'
        )
        await bot.handle_message({"chat": {"id": 42}, "text": sample_curl})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        # Hiç /tur yazmadan doğrudan proje fikri gönderildiğinde varsayılan:
        # auto_turns=True, respect_night_window=True, max_turns=12
        await bot.handle_message(
            {"chat": {"id": 42}, "text": "Akıllı envanter ve tedarik simülatörü"}
        )
        assert len(started_payloads) == 1
        assert started_payloads[0]["auto_turns"] is True
        assert started_payloads[0]["respect_night_window"] is True
        assert started_payloads[0]["max_turns"] == 12
        assert "gece sırasına alındı" in sent[-1]

        # Kullanıcı isterse /simdi yazarak bekleyen gece projesini hemen uyandırabilir
        await bot.handle_message({"chat": {"id": 42}, "text": "/simdi"})
        assert start_now_called == ["auto_night1"]

        # /rapor komutu akşam planını ve sabah Agent teslim raporunu birlikte göstermeli
        await bot.handle_message({"chat": {"id": 42}, "text": "/rapor"})
        assert "Akşam Başlangıç Planı" in sent[-1]
        assert "Sabah Teslim Raporu" in sent[-1]


@pytest.mark.asyncio
async def test_telegram_blocks_dead_agent_session_and_supports_temizle_commands() -> None:
    """Telegram `/agent_curl` + `/bitir` canlı kontrolde 401 alan ölü cURL'ü anında
    reddetmeli; geçerli oturumda `200 OK` göstermeli; `/durdur hepsi` ve `/temizle`
    komutlarını doğru API uçlarına yönlendirmelidir.
    """
    sent: list[str] = []
    api_calls: list[tuple[str, str, Any]] = []
    live_probe_should_pass = False

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        sent.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode()) if request.content else None
        api_calls.append((request.method, str(request.url), body))

        if path == "/v1/autopilot/inspect-curl":
            assert body is not None
            if not body.get("verify_live"):
                return httpx.Response(
                    200,
                    json={
                        "valid": True,
                        "target_domain": "live.io",
                        "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                        "source_endpoint_kind": "input",
                        "same_account_conflict": False,
                        "conflict_slots": [],
                        "cookie_diagnostics": {
                            "cookie_count": 4,
                            "auth_cookie_found": True,
                            "auth_chunk_indices": [0, 1],
                            "problems": [],
                        },
                        "live_checked": False,
                        "live_ok": None,
                        "warnings": [],
                    },
                )
            if not live_probe_should_pass:
                return httpx.Response(
                    200,
                    json={
                        "valid": True,
                        "target_domain": "live.io",
                        "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                        "source_endpoint_kind": "input",
                        "same_account_conflict": False,
                        "conflict_slots": [],
                        "cookie_diagnostics": {
                            "cookie_count": 2,
                            "auth_cookie_found": False,
                            "auth_chunk_indices": [],
                            "problems": ["İstek içinde oturum çerezi bulunamadı."],
                        },
                        "live_checked": True,
                        "live_ok": False,
                        "live_status_code": 401,
                        "live_error_type": "UpstreamAuthRejected",
                        "live_error_detail": "Not authenticated",
                        "warnings": [],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "target_domain": "live.io",
                    "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                    "source_endpoint_kind": "input",
                    "same_account_conflict": False,
                    "conflict_slots": [],
                    "cookie_diagnostics": {
                        "cookie_count": 4,
                        "auth_cookie_found": True,
                        "auth_chunk_indices": [0, 1],
                        "problems": [],
                    },
                    "live_checked": True,
                    "live_ok": True,
                    "live_message_count": 2,
                    "cookie_rotated_on_probe": True,
                    "warnings": [],
                },
            )
        if path == "/v1/autopilot/runs/stop-all":
            return httpx.Response(
                200,
                json={"stopped_count": 2, "stopped_run_ids": ["r1", "r2"], "active_count": 0},
            )
        if path == "/v1/autopilot/runs/clear":
            inc = bool(body and body.get("include_active", True))
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "stopped_active": 1 if inc else 0,
                    "cancelled_morning_reports": 1 if inc else 0,
                    "removed_runs": 3 if inc else 2,
                    "removed_run_ids": ["r1", "r2", "r3"] if inc else ["r2", "r3"],
                    "remaining_runs": 0 if inc else 1,
                },
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    settings = TelegramSettings(
        telegram_bot_token="123:TEST",
        wrapper_base_url="https://wrapper.test",
        wrapper_api_key="sk-test",
        telegram_allowed_chat_ids="42",
    )
    sample_curl = (
        'curl "https://live.io/api/chat/019d8420-fa0b-76a8-8b2f-268c897d22a9/input" '
        '-b "a=1"'
    )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(settings, telegram, wrapper)

        # 1) `/bitir` masaüstü ve `max` gibi ekstra upstream isteği atmadan doğrular
        await bot.handle_message({"chat": {"id": 42}, "text": "/agent_curl"})
        await bot.handle_message({"chat": {"id": 42}, "text": sample_curl})
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert "Agent cURL doğrulandı" in sent[-1]
        assert bot._session(42).mode == "awaiting_idea"

        # 2) İsteğe bağlı manuel `/agent_test` çağrısı canlı durumu raporlar (401 -> 200)
        await bot.handle_message({"chat": {"id": 42}, "text": "/agent_test"})
        assert "HTTP 401" in sent[-1]

        live_probe_should_pass = True
        await bot.handle_message({"chat": {"id": 42}, "text": "/agent_test"})
        assert "Agent oturumu hazır" in sent[-1]

        # 3) /durdur hepsi -> POST /v1/autopilot/runs/stop-all
        await bot.handle_message({"chat": {"id": 42}, "text": "/durdur hepsi"})
        assert "anında durduruldu" in sent[-1]

        # 4) /temizle bitenler -> include_active=False (açık fikir girişini bozmaz)
        await bot.handle_message({"chat": {"id": 42}, "text": "/temizle bitenler"})
        assert "2 proje kaydı silindi" in sent[-1]
        assert bot._session(42).mode == "awaiting_idea"

        # 5) /temizle -> include_active=True ve bekleyen cURL/fikir oturumu da sıfırlanır
        await bot.handle_message({"chat": {"id": 42}, "text": "/temizle"})
        assert "3 proje kaydı silindi" in sent[-1]
        assert "hesap-1" in sent[-1]
        assert bot._session(42).mode == "idle"


@pytest.mark.asyncio
async def test_env_single_line_flattened_multi_cookie_and_telegram_document_paste() -> None:
    """1) Render tek satırlık env kutusunda `\\n` silinmiş ve çoklu `-H 'cookie:'`
          taşıyan `UPSTREAM_CURL` açılışta eksiksiz birleştirilmeli;
       2) Telegram'da `'` ile `\\` arasından bölünen veya `.txt` dosyası olarak
          gelen Agent cURL'ü kayıpsız okunmalıdır.
    """
    import base64
    import time

    from app.autopilot.curl_parse import parse_agent_curl, reassemble_agent_curl_chunks
    from app.core.config import Settings
    from app.main import create_app

    exp = int(time.time()) + 1200
    jwt_hdr = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    jwt_pay = (
        base64.urlsafe_b64encode(f'{{"sub":"u-mob-9","exp":{exp}}}'.encode())
        .decode()
        .rstrip("=")
    )
    tok = f"{jwt_hdr}.{jwt_pay}.sig"
    sess_json = json.dumps(
        {"access_token": tok, "refresh_token": "rt_valid_mobile_999", "user": {"id": "u-mob-9"}}
    )
    b64_full = "base64-" + base64.urlsafe_b64encode(sess_json.encode()).decode().rstrip("=")
    mid = len(b64_full) // 2

    # 1) Render Env tek satır kutusuna yapıştırılmış (`\n` -> boşluk olmuş) + 2 ayrı `-H 'cookie:'`
    flattened_env_curl = (
        "curl 'https://live.example/nextjs-api/stream/create-evaluation' \\   "
        "-H 'accept: */*' \\   "
        f"-H 'cookie: arena-auth-prod-v1.0={b64_full[:mid]}' \\   "
        f"-H 'cookie: arena-auth-prod-v1.1={b64_full[mid:]}; cf_clearance=ok' \\   "
        "--data-raw '{\"id\":\"019d8420-fa0b-76a8-8b2f-268c897d22a9\","
        "\"mode\":\"direct\",\"modelAId\":\"m-1\",\"recaptchaV3Token\":\"rc-1\"}'"
    )
    app = create_app(
        Settings(
            _env_file=None,
            api_keys=["sk-test"],
            upstream_curl=flattened_env_curl,
        )
    )
    loaded_cookie = app.state.settings.upstream_cookie
    assert f"arena-auth-prod-v1.0={b64_full[:mid]}" in loaded_cookie
    assert f"arena-auth-prod-v1.1={b64_full[mid:]}" in loaded_cookie
    assert "cf_clearance=ok" in loaded_cookie

    # 2) Telegram `'` ve `\` arasındaki boşluktan + base64 ortasından böldüğünde:
    part1 = (
        "curl 'https://live.example/api/chat/019d8420-fa0b-76a8-8b2f-268c897d22a9/input'"
    )
    part2 = (
        f"\\ -H 'accept: */*' -H 'cookie: arena-auth-prod-v1.0={b64_full[:mid]}; "
        f"arena-auth-prod-v1.1={b64_full[mid:mid + 12]}"
    )
    part3 = (
        f"{b64_full[mid + 12:]}' "
        "--data-raw '{\"kind\":\"message\","
        "\"payload\":{\"metadata\":{\"recaptchaV3Token\":\"rc\"}}}'"
    )
    joined_agent = reassemble_agent_curl_chunks([part1, part2, part3])
    parsed_agent = parse_agent_curl(joined_agent)
    assert parsed_agent.account_identity() == "u-mob-9"
    assert not parsed_agent.warnings

    # 3) Telegram uzun yapıştırmayı `clipboard.txt` (`document`) olarak gönderirse:
    sent: list[str] = []
    inspected: list[str] = []
    file_bytes = joined_agent.encode("utf-8")

    def tg_doc_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "documents/clipboard.txt"}}
            )
        if "/file/bot" in request.url.path:
            return httpx.Response(200, content=file_bytes)
        sent.append(json.loads(request.content.decode())["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def wrap_doc_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        inspected.append(body["agent_curl"])
        return httpx.Response(
            200,
            json={
                "valid": True,
                "target_domain": "live.example",
                "chat_id": "019d8420-fa0b-76a8-8b2f-268c897d22a9",
                "source_endpoint_kind": "input",
                "same_account_conflict": False,
                "conflict_slots": [],
                "cookie_diagnostics": {
                    "cookie_count": 2,
                    "auth_cookie_found": True,
                    "auth_chunk_indices": [0, 1],
                    "token_remaining_seconds": 1080,
                    "problems": [],
                },
                "warnings": [],
            },
        )

    settings = TelegramSettings(
        telegram_bot_token="123:TEST",
        wrapper_base_url="https://wrapper.test",
        wrapper_api_key="sk-test",
        telegram_allowed_chat_ids="42",
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(tg_doc_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrap_doc_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(settings, telegram, wrapper)
        await bot.handle_message(
            {
                "chat": {"id": 42},
                "caption": "/agent_curl",
                "document": {
                    "file_id": "doc-1",
                    "file_name": "clipboard.txt",
                    "mime_type": "text/plain",
                    "file_size": len(file_bytes),
                },
            }
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        assert len(inspected) == 1
        assert "parçalar: [0, 1]" in sent[-1]
        assert "JWT ~18 dk" in sent[-1]


@pytest.mark.asyncio
async def test_one_hour_agent_timeout_and_telegram_devam_resume_flow() -> None:
    """1) Varsayılan Agent tur zaman aşımı 3600 sn (1 saat) olmalı;
       2) Yarıda kesilen 1. tur bulutta bittiğinde `resume` aynı turu tekrarlamadan
          2. turdan devam etmeli ve mevcut sohbet ilerlemesini okumalı;
       3) Telegram `/devam simdi` taze cURL ile ilgili koşuyu sürdürmelidir.
    """
    from app.api.v1.autopilot import ResumeAutopilotRequest, StartAutopilotRequest
    from app.autopilot.agent_client import AgentSessionClient
    from app.autopilot.max_bridge import MaxBridge
    from app.autopilot.orchestrator import (
        AutopilotConfig,
        AutopilotOrchestrator,
        AutopilotRunState,
    )
    from app.autopilot.prompts import build_kickoff_user_prompt

    req_start = StartAutopilotRequest(
        project_idea="Mobil komuta merkezi",
        agent_curl=(
            'curl "https://live.example/api/chat/019d8420-fa0b-76a8-8b2f-268c897d22a9/input" '
            '-b "a=1"'
        ),
    )
    req_resume = ResumeAutopilotRequest(
        agent_curl=(
            'curl "https://live.example/api/chat/019d8420-fa0b-76a8-8b2f-268c897d22a9/input" '
            '-b "a=1"'
        ),
    )
    assert req_start.agent_turn_timeout is None
    assert req_resume.agent_turn_timeout is None

    # Deploy sonrası mevcut Agent sohbetinden devam istemi kontrolü:
    kickoff_with_existing = build_kickoff_user_prompt(
        project_idea="Mobil komuta merkezi",
        max_turns=8,
        auto_turns=True,
        existing_agent_reply="Arayüz ve /api/proxy kuruldu.",
        existing_sandbox_evidence="Sandbox özeti: toplam 12 işlem (dosya yazma=8)",
    )
    assert "ÖNCEDEN YAPILAN ÇALIŞMA VAR" in kickoff_with_existing
    assert "toplam 12 işlem" in kickoff_with_existing

    # Bulutta tamamlanmış 1. turun resume sırasında otomatik kurtarılması:
    chat_id = "019d8420-fa0b-76a8-8b2f-268c897d22a9"
    turn2_sent_inputs: list[str] = []

    def agent_mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/chat/{chat_id}":
            if not turn2_sent_inputs:
                return httpx.Response(
                    200,
                    json={
                        "id": chat_id,
                        "triggerAddress": f"agent-runner:{chat_id}",
                        "lastEventId": "10",
                        "messages": [
                            {
                                "id": "u-1",
                                "role": "user",
                                "parts": [{"type": "text", "text": "t1"}],
                            },
                            {
                                "id": "a-1",
                                "role": "assistant",
                                "metadata": {"pending": False},
                                "parts": [
                                    {"type": "tool-write_file", "input": {"path": "src/App.tsx"}},
                                    {"type": "text", "text": "1. tur bulutta tamamlandı."},
                                ],
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": chat_id,
                    "triggerAddress": f"agent-runner:{chat_id}",
                    "lastEventId": "20",
                    "messages": [
                        {"id": "u-1", "role": "user", "parts": [{"type": "text", "text": "t1"}]},
                        {
                            "id": "a-1",
                            "role": "assistant",
                            "metadata": {"pending": False},
                            "parts": [],
                        },
                        {"id": "u-2", "role": "user", "parts": [{"type": "text", "text": "t2"}]},
                        {
                            "id": "a-2",
                            "role": "assistant",
                            "metadata": {"pending": False},
                            "parts": [
                                {
                                    "type": "tool-bash",
                                    "input": {"command": "pytest"},
                                    "output": {"exit_code": 0},
                                },
                                {"type": "text", "text": "2. tur ve Vercel deploy bitti."},
                            ],
                        },
                    ],
                },
            )
        if path == f"/api/chat/{chat_id}/input":
            payload = json.loads(request.content.decode())
            msg_txt = payload["payload"]["message"]["parts"][0]["text"]
            turn2_sent_inputs.append(msg_txt)
            return httpx.Response(200, json={"ok": True, "seq": 11})
        if path == f"/api/chat/{chat_id}/events":
            sse = (
                'event: batch\n'
                'data: {"records":[{"seq_num":20,"headers":[["trigger-control","turn-complete"]],'
                '"body":"{}"}]}\n\n'
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse.encode(),
            )
        return httpx.Response(404)

    class _DummyMaxBridge(MaxBridge):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def account_identities(self) -> set[str]:
            return {"max-acc-1"}

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            self.calls.append(user_prompt)
            if len(self.calls) == 1:
                return (
                    "<DURUM>DEVAM</DURUM>\n"
                    "<ASAMA>4_DOGRULAMA_VE_SUNUCU</ASAMA>\n"
                    "<DEGERLENDIRME>1. tur bulutta bitmiş, doğrulamaya geçiliyor.</DEGERLENDIRME>\n"
                    "<GUNCEL_HAFIZA>Arayüz hazır, Vercel deploy yapılacak.</GUNCEL_HAFIZA>\n"
                    "<AJANA_MESAJ>Testleri çalıştır ve Vercel deploy tamamla.</AJANA_MESAJ>"
                )
            return (
                "<DURUM>TAMAMLANDI</DURUM>\n"
                "<ASAMA>5_KABUL</ASAMA>\n"
                "<DEGERLENDIRME>Proje tamamlandı.</DEGERLENDIRME>\n"
                "<GUNCEL_HAFIZA>Canlıda.</GUNCEL_HAFIZA>\n"
                "<AJANA_MESAJ></AJANA_MESAJ>"
            )

    prev_state = AutopilotRunState(
        run_id="auto_resume1",
        chat_id=chat_id,
        project_idea="Mobil komuta merkezi",
        target_domain="live.example",
        status="failed",
        current_turn=1,
        max_turns=2,
        pending_turn=1,
        pending_instruction="İlk çekirdek yapıyı kur.",
        pending_evaluation="Kickoff planı",
    )

    agent_client = AgentSessionClient.from_curl(
        f'curl "https://live.example/api/chat/{chat_id}/input" -b "a=1"',
        transport=httpx.MockTransport(agent_mock_handler),
    )
    assert agent_client.turn_timeout is None
    bridge = _DummyMaxBridge()
    orch = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="Mobil komuta merkezi",
            max_turns=2,
            min_turns_before_complete=2,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            enable_handoff_report=False,
            run_id="auto_resume1",
        ),
        agent=agent_client,
        max_bridge=bridge,
        resume_state=prev_state,
    )
    final_state = await orch.run()
    assert final_state.status == "completed"
    # 1. tur tekrar gönderilmedi; buluttaki a-1 kurtarılıp doğrudan 2. tur gönderildi!
    assert len(turn2_sent_inputs) == 1
    assert "Vercel deploy tamamla" in turn2_sent_inputs[0]
    assert len(final_state.turns) == 2
    assert final_state.turns[0].agent_message_id == "a-1"

    # Telegram `/devam simdi` akışı:
    sent_tg: list[str] = []
    resume_calls: list[dict[str, Any]] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        sent_tg.append(json.loads(request.content.decode())["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def wrap_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/autopilot/inspect-curl":
            return httpx.Response(
                200,
                json={
                    "valid": True,
                    "target_domain": "live.example",
                    "chat_id": chat_id,
                    "source_endpoint_kind": "input",
                    "same_account_conflict": False,
                    "conflict_slots": [],
                    "cookie_diagnostics": {"cookie_count": 2, "auth_cookie_found": True},
                    "warnings": [],
                },
            )
        if path == "/v1/autopilot/runs":
            return httpx.Response(
                200,
                json={
                    "active_count": 0,
                    "runs": [
                        {
                            "run_id": "auto_X7bzGssz4vD0",
                            "chat_id": chat_id,
                            "project_idea": "Mobil komuta merkezi",
                            "status": "failed",
                        }
                    ],
                },
            )
        if path == "/v1/autopilot/runs/auto_X7bzGssz4vD0/resume":
            body = json.loads(request.content.decode())
            resume_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "resumed": True,
                    "run_id": "auto_X7bzGssz4vD0",
                    "chat_id": chat_id,
                    "agent_url": f"https://live.example/agent/{chat_id}",
                    "status": "running",
                    "current_turn": 1,
                    "turn_count": 1,
                    "max_turns": 12,
                    "respect_night_window": False,
                },
            )
        return httpx.Response(404)

    tg_settings = TelegramSettings(
        telegram_bot_token="123:TEST",
        wrapper_base_url="https://wrapper.test",
        wrapper_api_key="sk-test",
        telegram_allowed_chat_ids="42",
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(tg_handler)) as tg_http,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            transport=httpx.MockTransport(wrap_handler),
        ) as wrap_http,
    ):
        bot = TelegramBot(tg_settings, tg_http, wrap_http)
        await bot.handle_message({"chat": {"id": 42}, "text": "/devam"})
        assert "taze bir `/input` cURL" in sent_tg[-1]

        await bot.handle_message(
            {
                "chat": {"id": 42},
                "text": (
                    f'/agent_curl curl "https://live.example/api/chat/{chat_id}/input" '
                    '-b "a=1"'
                ),
            }
        )
        await bot.handle_message({"chat": {"id": 42}, "text": "/bitir"})
        await bot.handle_message({"chat": {"id": 42}, "text": "/devam simdi"})
        assert len(resume_calls) == 1
        assert resume_calls[0]["respect_night_window"] is False
        assert "kaldığı yerden sürdürülüyor" in sent_tg[-1]
        assert "süre sınırı yok" in sent_tg[-1]




