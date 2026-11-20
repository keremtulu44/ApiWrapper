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

