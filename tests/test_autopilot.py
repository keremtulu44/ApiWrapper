"""Agent Mode cURL ayrıştırıcı, olay okuyucu, istemci ve otopilot testleri."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters.prompt_builder import build_prompt
from app.autopilot.agent_client import AgentSessionClient
from app.autopilot.curl_parse import extract_identity_subject, parse_agent_curl
from app.autopilot.events_parser import (
    AgentEventStreamDecoder,
    format_sandbox_evidence,
    parse_chat_state,
)
from app.autopilot.max_bridge import (
    ExternalHttpMaxBridge,
    MaxBridge,
    compose_single_turn_max_prompt,
)
from app.autopilot.orchestrator import (
    TR_TZ,
    AutopilotConfig,
    AutopilotOrchestrator,
    AutopilotRunState,
    SameAccountError,
    is_in_night_window,
    seconds_until_morning_hour,
    seconds_until_window_opens,
)
from app.autopilot.prompts import (
    AGENT_FOLLOWUP_NO_ASK_GUARDRAIL,
    AGENT_MORNING_HANDOFF_PROMPT,
    AGENT_OPERATING_GUARDRAILS,
    MAX_SYSTEM_PROMPT,
    build_kickoff_user_prompt,
    build_turn_user_prompt,
    compute_min_turns_before_completion,
    parse_max_decision,
    similarity_ratio,
)
from app.core.config import Settings
from app.main import create_app
from app.schemas.openai import ChatMessage
from app.services.alerting import TelegramAlerter


def _make_fake_supabase_cookie(sub: str) -> str:
    """Test için parçalı base64 `arena-auth-prod-v1.0` + `.1` çerezi üretir."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": sub, "role": "authenticated"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    jwt_token = f"{header}.{payload}.fakesig"
    session_json = json.dumps({"access_token": jwt_token, "token_type": "bearer"})
    b64_val = "base64-" + base64.urlsafe_b64encode(session_json.encode()).decode().rstrip("=")
    mid = len(b64_val) // 2
    return f"arena-auth-prod-v1.0={b64_val[:mid]}; arena-auth-prod-v1.1={b64_val[mid:]}"


def _cmd_escape_json(data: dict[str, Any]) -> str:
    """Sözlüğü Windows `Copy as cURL (cmd)` biçiminde kaçırır."""
    raw = json.dumps(data, separators=(",", ":"))
    out: list[str] = []
    for ch in raw:
        if ch == '"':
            out.append(r'\^"')
        elif ch in "{}[]|&":
            out.append("^" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _build_windows_cmd_input_curl(
    *,
    chat_id: str = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784",
    sub: str = "user-agent-account-111",
    prev_user_msg_id: str = "019da5d9-f37d-71be-9d31-aa7711223344",
) -> str:
    cookie = _make_fake_supabase_cookie(sub)
    msg_id = "019da5dd-8284-7404-a813-595e1a05d5a7"
    body_escaped = _cmd_escape_json(
        {
            "kind": "message",
            "payload": {
                "message": {
                    "parts": [{"type": "text", "text": "merhaba|test&kontrol"}],
                    "id": msg_id,
                    "role": "user",
                    "metadata": {"lastUserMessageId": prev_user_msg_id},
                },
                "chatId": f"agent-runner:{chat_id}",
                "trigger": "submit-message",
                "messageId": msg_id,
                "metadata": {
                    "timezone": "Europe/Istanbul",
                    "arenaVisitId": "visit-xyz",
                    "clientPlatform": "web",
                    "submissionSource": "chat_input",
                    "cx": "test_cx_value",
                    "recaptchaV3Token": "test_static_recaptcha_v3",
                },
            },
        }
    )
    sec_ua = (
        '  -H ^"sec-ch-ua: ^\\^"Google Chrome^\\^";v=^\\^"147^\\^", '
        '^\\^"Chromium^\\^";v=^\\^"147^\\^"^" ^\n'
    )
    return (
        f'curl ^"https://arena.example.test/api/chat/{chat_id}/input^" ^\n'
        '  -H ^"accept: */*^" ^\n'
        '  -H ^"accept-language: tr-TR,tr;q=0.9,en-US;q=0.8^" ^\n'
        '  -H ^"content-type: application/json^" ^\n'
        f'  -b ^"arena_visit_id=visit-xyz; {cookie}; '
        'g_state=^\\^{{\\^"i_l\\^":0^\\^}}^" ^\n'
        '  -H ^"origin: https://arena.example.test^" ^\n'
        f'  -H ^"referer: https://arena.example.test/agent/{chat_id}^" ^\n'
        + sec_ua
        + '  -H ^"sec-ch-ua-mobile: ?0^" ^\n'
        '  -H ^"sec-ch-ua-platform: ^\\^"Windows^\\^"^" ^\n'
        '  -H ^"user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'Chrome/147.0.0.0^" ^\n'
        '  -H ^"x-arena-web-deployment: canary^" ^\n'
        '  -H ^"x-deployment-id: dpl_TestDeploy123^" ^\n'
        f'  --data-raw ^"{body_escaped}^"'
    )


# ---------------------------------------------------------------------------
# 1. cURL ayrıştırma ve hesap kimliği testleri
# ---------------------------------------------------------------------------


def test_parse_windows_cmd_agent_input_curl() -> None:
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    prev_id = "019da5d9-f37d-71be-9d31-aa7711223344"
    # PowerShell Set-Content UTF-8 BOM (\ufeff) eklese bile sorunsuz ayrışmalı
    raw = "\ufeff" + _build_windows_cmd_input_curl(
        chat_id=chat_id,
        sub="account-agent-alpha",
        prev_user_msg_id=prev_id,
    )
    cfg = parse_agent_curl(raw)

    assert cfg.source_endpoint_kind == "input"
    assert cfg.target_domain == "arena.example.test"
    assert cfg.chat_id == chat_id
    assert cfg.effective_trigger_address == f"agent-runner:{chat_id}"
    assert cfg.last_user_message_id == prev_id
    assert cfg.recaptcha_token == "test_static_recaptcha_v3"
    assert cfg.cx == "test_cx_value"
    assert cfg.arena_visit_id == "visit-xyz"
    assert cfg.extra_headers.get("x-deployment-id") == "dpl_TestDeploy123"
    assert cfg.extra_headers.get("x-arena-web-deployment") == "canary"
    assert '"Google Chrome";v="147"' in cfg.sec_ch_ua_override
    assert cfg.sec_ch_ua_platform_override == '"Windows"'
    assert cfg.warnings == []
    assert cfg.account_identity() == "account-agent-alpha"


def test_parse_posix_bash_create_chat_curl() -> None:
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    cookie = _make_fake_supabase_cookie("account-agent-beta")
    body = json.dumps(
        {
            "id": chat_id,
            "modality": "agent",
            "recaptchaV3Token": "tok_bash_123",
            "cx": "cx_bash_456",
            "timezone": "Europe/Istanbul",
            "clientPlatform": "web",
        }
    )
    raw = (
        "curl 'https://arena.example.test/nextjs-api/stream/create-chat' \\\n"
        f"  -H 'cookie: {cookie}' \\\n"
        "  -H 'content-type: application/json' \\\n"
        f"  --data-raw '{body}'"
    )
    cfg = parse_agent_curl(raw)
    assert cfg.source_endpoint_kind == "create-chat"
    assert cfg.chat_id == chat_id
    assert cfg.recaptcha_token == "tok_bash_123"
    assert cfg.cx == "cx_bash_456"
    assert cfg.account_identity() == "account-agent-beta"
    assert cfg.warnings == []


def test_extract_identity_subject_from_chunked_cookie() -> None:
    cookie = _make_fake_supabase_cookie("google-uid-987654")
    assert extract_identity_subject(cookie, "") == "google-uid-987654"


# ---------------------------------------------------------------------------
# 2. ?recoverRun=false ve /events SSE ayrıştırıcı testleri
# ---------------------------------------------------------------------------


def test_parse_chat_state_and_sandbox_evidence() -> None:
    snapshot: dict[str, Any] = {
        "id": "019da5d9-f4fc-7691-9e1e-34b5c1c9b784",
        "taskId": "agent-runner",
        "triggerAddress": "agent-runner:019da5d9-f4fc-7691-9e1e-34b5c1c9b784",
        "lastEventId": "42",
        "messages": [
            {
                "id": "019da5d9-f37d-71be-9d31-111111111111",
                "role": "user",
                "parts": [{"type": "text", "text": "Projeyi başlat"}],
                "metadata": {"pending": False},
            },
            {
                "id": "019da5d9-f4fd-7329-988a-222222222222",
                "role": "assistant",
                "parts": [
                    {"type": "step-start"},
                    {
                        "type": "tool-write_file",
                        "toolCallId": "call_1",
                        "state": "output-available",
                        "input": {"path": "src/app.py", "content": "print('ok')"},
                        "output": {"status": "success"},
                    },
                    {
                        "type": "tool-bash",
                        "toolCallId": "call_2",
                        "state": "output-available",
                        "input": {"command": "pytest -q", "cwd": "/workspace"},
                        "output": {
                            "stdout": "4 passed in 0.12s\n",
                            "stderr": "",
                            "exit_code": 0,
                            "status": "completed",
                            "duration_ms": 120,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Uygulama iskeletini kurdum ve testleri çalıştırdım.",
                        "state": "done",
                    },
                ],
                "metadata": {"pending": False},
            },
        ],
    }

    state = parse_chat_state(snapshot)
    assert state.last_event_id == "42"
    assert state.last_user_message_id == "019da5d9-f37d-71be-9d31-111111111111"
    assert not state.has_pending_turn
    assert state.last_assistant_message is not None
    assert "iskeletini kurdum" in state.last_assistant_message.text
    assert len(state.last_assistant_message.tools) == 2

    evidence_txt = format_sandbox_evidence(state.last_assistant_message)
    assert "Sandbox özeti: toplam 2 işlem" in evidence_txt
    assert "dosya yazma/düzenleme=1" in evidence_txt
    assert "komut/test=1 başarılı / 0 hatalı" in evidence_txt
    assert "çıkış kodu 0" in evidence_txt
    # Ham bash komutu ve backtick WAF tetiklemesin diye kanıtta yer almamalı:
    assert "pytest -q" not in evidence_txt
    assert "`" not in evidence_txt


def test_agent_event_stream_decoder_tracks_seq_and_turn_complete() -> None:
    decoder = AgentEventStreamDecoder()

    ping_sse = (
        "event: ping\ndata: "
        + json.dumps({"timestamp": 1776604100000, "tail": {"seq_num": 41}})
        + "\n\n"
    )
    deltas_ping = decoder.feed(ping_sse.encode("utf-8"))
    assert len(deltas_ping) == 1
    assert deltas_ping[0].kind == "ping"

    batch_sse = (
        "event: batch\ndata: "
        + json.dumps(
            {
                "records": [
                    {
                        "seq_num": 41,
                        "headers": [["content-type", "application/json"]],
                        "body": json.dumps(
                            {
                                "data": {
                                    "type": "tool-input-available",
                                    "toolCallId": "tc_1",
                                    "toolName": "bash",
                                    "input": {"command": "npm test"},
                                },
                                "id": "e1",
                            }
                        ),
                    },
                    {
                        "seq_num": 42,
                        "headers": [["content-type", "application/json"]],
                        "body": json.dumps(
                            {"data": {"type": "text-delta", "delta": "Merhaba "}, "id": "e2"}
                        ),
                    },
                    {
                        "seq_num": 43,
                        "headers": [["content-type", "application/json"]],
                        "body": json.dumps(
                            {"data": {"type": "text-delta", "delta": "dünya!"}, "id": "e3"}
                        ),
                    },
                    {
                        "seq_num": 44,
                        "headers": [
                            ["content-type", "application/json"],
                            ["trigger-control", "turn-complete"],
                        ],
                        "body": "",
                    },
                ],
                "tail": {"seq_num": 45},
            }
        )
        + "\n\n"
    )
    deltas = decoder.feed(batch_sse.encode("utf-8"))
    deltas.extend(decoder.flush())

    collected_text = "".join(d.text for d in deltas if d.kind == "text")
    assert collected_text == "Merhaba dünya!"
    assert any(d.kind == "tool_input" and d.tool_name == "bash" for d in deltas)
    assert any(d.kind == "turn_complete" and d.seq_num == 44 for d in deltas)


# ---------------------------------------------------------------------------
# 3. AgentSessionClient protokol ve Set-Cookie / 409 testi
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agent_session_client_send_and_wait_flow() -> None:
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    initial_user_id = "019da5d9-f37d-71be-9d31-000000000001"
    raw_curl = _build_windows_cmd_input_curl(
        chat_id=chat_id,
        sub="agent-sub-001",
        prev_user_msg_id=initial_user_id,
    )

    state_calls = 0
    captured_input_body: dict[str, Any] = {}
    captured_input_headers: dict[str, str] = {}
    sent_msg_id = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal state_calls, captured_input_body, captured_input_headers, sent_msg_id
        path = request.url.path

        if request.method == "GET" and path == "/" and not request.url.query:
            return httpx.Response(200, text="<html>dpl_TestDeploy123</html>")

        if request.method == "GET" and path == f"/api/chat/{chat_id}":
            state_calls += 1
            if state_calls <= 2:
                return httpx.Response(
                    200,
                    headers={"set-cookie": "session_rot=fresh_v2; Path=/; HttpOnly"},
                    json={
                        "id": chat_id,
                        "taskId": "agent-runner",
                        "triggerAddress": f"agent-runner:{chat_id}",
                        "lastEventId": "10",
                        "messages": [
                            {
                                "id": initial_user_id,
                                "role": "user",
                                "parts": [{"type": "text", "text": "Merhaba"}],
                                "metadata": {"pending": False},
                            },
                            {
                                "id": "019da5d9-f37d-71be-9d31-000000000002",
                                "role": "assistant",
                                "parts": [{"type": "text", "text": "Hazırım."}],
                                "metadata": {"pending": False},
                            },
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": chat_id,
                    "taskId": "agent-runner",
                    "triggerAddress": f"agent-runner:{chat_id}",
                    "lastEventId": "13",
                    "messages": [
                        {
                            "id": sent_msg_id,
                            "role": "user",
                            "parts": [{"type": "text", "text": "Uygulamayı kur"}],
                            "metadata": {"pending": False},
                        },
                        {
                            "id": "019da5d9-f37d-71be-9d31-000000000099",
                            "role": "assistant",
                            "parts": [
                                {
                                    "type": "tool-bash",
                                    "state": "output-available",
                                    "input": {"command": "python -m pytest"},
                                    "output": {
                                        "stdout": "2 passed",
                                        "stderr": "",
                                        "exit_code": 0,
                                        "status": "completed",
                                    },
                                },
                                {"type": "text", "text": "Kurulum ve testler tamamlandı."},
                            ],
                            "metadata": {"pending": False},
                        },
                    ],
                },
            )

        if request.method == "POST" and path == f"/api/chat/{chat_id}/input":
            captured_input_headers = dict(request.headers)
            captured_input_body = json.loads(request.content.decode("utf-8"))
            sent_msg_id = captured_input_body["payload"]["messageId"]
            return httpx.Response(200, json={"ok": True, "seq": 11})

        if request.method == "GET" and path == f"/api/chat/{chat_id}/events":
            assert request.headers.get("last-event-id") == "10"
            sse_body = (
                "event: batch\n"
                "data: "
                + json.dumps(
                    {
                        "records": [
                            {
                                "seq_num": 12,
                                "headers": [["content-type", "application/json"]],
                                "body": json.dumps(
                                    {
                                        "data": {
                                            "type": "text-delta",
                                            "delta": "Kurulum ve testler tamamlandı.",
                                        }
                                    }
                                ),
                            },
                            {
                                "seq_num": 13,
                                "headers": [["trigger-control", "turn-complete"]],
                                "body": "",
                            },
                        ]
                    }
                )
                + "\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body.encode("utf-8"),
            )

        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with AgentSessionClient.from_curl(
        raw_curl, turn_timeout=5.0, transport=transport
    ) as client:
        await client.connect()
        reply = await client.send_and_wait("Uygulamayı kur")

    assert "session_rot=fresh_v2" in captured_input_headers.get("cookie", "")
    assert captured_input_headers.get("x-part-id")
    assert captured_input_body["kind"] == "message"
    assert (
        captured_input_body["payload"]["message"]["metadata"]["lastUserMessageId"]
        == initial_user_id
    )
    assert (
        captured_input_body["payload"]["metadata"]["recaptchaV3Token"]
        == "test_static_recaptcha_v3"
    )
    assert reply.text == "Kurulum ve testler tamamlandı."
    assert len(reply.tools) == 1
    assert reply.tools[0].exit_code == 0


# ---------------------------------------------------------------------------
# 4. Max istem kuralları ve karar ayrıştırıcı testleri
# ---------------------------------------------------------------------------


def test_max_system_prompt_enforces_human_persona_and_busy_rule() -> None:
    lowered = MAX_SYSTEM_PROMPT.lower()
    assert "müsait değilim" in lowered
    assert "yapay zekayım" in lowered
    assert "sandbox" in lowered
    assert "asla" in lowered
    assert "web'de" in lowered
    assert "web_search" in lowered
    assert "api'ye" in lowered
    assert len(MAX_SYSTEM_PROMPT) <= 3000

    kickoff = build_kickoff_user_prompt(
        project_idea="Canlı hava durumu ve döviz takip paneli",
        max_turns=8,
        auto_turns=True,
    ).lower()
    assert "web_search" in kickoff
    assert "api'ye" in kickoff

    huge_turn = build_turn_user_prompt(
        project_idea="Panel",
        turn=4,
        max_turns=8,
        current_phase="2_YARATICI_DERINLIK",
        memory_summary="Not " * 1000,
        turn_digests=[],
        last_sent_to_agent="Talimat " * 600,
        agent_reply_text=(
            "ARA_GUNLUK_ADIMI "
            + ("orta metin " * 3000)
            + "\n## ✅ Teslim Özeti\nKomut `scripts/verify.sh` && `/bin/bash` curl -fsS "
            + "SONUÇ_ÖZETİ " * 40
        ),
        sandbox_evidence=(
            "Değiştirilen dosyalar: x\n  ✅ cmd=`pytest` exit_code=0\nKanıt özeti tamam."
        ),
    )
    assert "ARA_GUNLUK_ADIMI" not in huge_turn
    assert "SONUÇ_ÖZETİ" in huge_turn
    assert "`" not in huge_turn
    assert "&&" not in huge_turn
    assert "/bin/bash" not in huge_turn
    assert "verify.sh" not in huge_turn
    assert "cmd=" not in huge_turn
    assert len(huge_turn) <= 5500


def test_events_parser_formats_web_search_and_fetch_page_evidence() -> None:
    state = parse_chat_state(
        {
            "id": "chat-web-1",
            "lastEventId": "10",
            "messages": [
                {
                    "id": "m1",
                    "role": "assistant",
                    "pending": False,
                    "parts": [
                        {
                            "type": "tool-web_search",
                            "toolCallId": "tc1",
                            "state": "output-available",
                            "input": {"query": "open-meteo api forecast docs", "depth": "2"},
                            "output": {"status": "completed"},
                        },
                        {
                            "type": "tool-fetch_page",
                            "toolCallId": "tc2",
                            "state": "output-available",
                            "input": {"url": "https://open-meteo.com/en/docs"},
                            "output": {"status": "completed"},
                        },
                    ],
                }
            ],
        }
    )
    msg = state.last_assistant_message
    assert msg is not None
    evidence = format_sandbox_evidence(msg)
    assert "web_search" in evidence
    assert "open-meteo api forecast docs" in evidence
    assert "fetch_page" in evidence
    assert "https://open-meteo.com/en/docs" in evidence


def test_parse_max_decision_structured_and_fallback() -> None:
    raw = (
        "<DURUM>DEVAM</DURUM>\n"
        "<ASAMA>2_GERCEK_ISLEV</ASAMA>\n"
        "<DEGERLENDIRME>İskelet temiz, şimdi gerçek uçları yazmalı.</DEGERLENDIRME>\n"
        "<GUNCEL_HAFIZA>\n"
        "- FastAPI iskeleti kuruldu.\n"
        "- Sıradaki adım: SQLite modelleri ve CRUD testleri.\n"
        "</GUNCEL_HAFIZA>\n"
        "<AJANA_MESAJ>\n"
        "Şu an kendi bilgisayarımda deneme yapmaya müsait değilim; SQLite katmanını kurup "
        "kendi ortamında pytest ile doğrulayarak çıktıyı paylaş.\n"
        "</AJANA_MESAJ>"
    )
    decision = parse_max_decision(raw, fallback_memory="eski")
    assert decision.status == "DEVAM"
    assert decision.phase == "2_GERCEK_ISLEV"
    assert "SQLite modelleri" in decision.updated_memory
    assert "müsait değilim" in decision.message_to_agent
    assert "<DURUM>" not in decision.message_to_agent

    # Etiket unutulursa güvenli geri dönüş
    fallback = parse_max_decision(
        "Lütfen testleri çalıştırıp sonucu göster.",
        fallback_memory="hafıza-1",
    )
    assert fallback.status == "DEVAM"
    assert fallback.updated_memory == "hafıza-1"
    assert fallback.message_to_agent == "Lütfen testleri çalıştırıp sonucu göster."
    assert similarity_ratio("aynı cümle", "aynı cümle") == 1.0

    # Kapanmamış <AJANA_MESAJ> durumunda iç değerlendirme/hafıza ASLA ajana sızmamalı
    unclosed = (
        "<DURUM>DEVAM</DURUM>\n"
        "<ASAMA>2_YARATICI_DERINLIK</ASAMA>\n"
        "<DEGERLENDIRME>GİZLİ İÇ ANALİZ: Butonlar henüz sahte.</DEGERLENDIRME>\n"
        "<GUNCEL_HAFIZA>GİZLİ HAFIZA DEFTERİ: Yol haritası 2. adım.</GUNCEL_HAFIZA>\n"
        "<AJANA_MESAJ>\n"
        "Şimdi etkileşimli senaryo simülatörünü ve gerçek filtreleri bağla."
    )
    dec_unclosed = parse_max_decision(unclosed)
    assert dec_unclosed.status == "DEVAM"
    assert "senaryo simülatörünü" in dec_unclosed.message_to_agent
    assert "GİZLİ İÇ ANALİZ" not in dec_unclosed.message_to_agent
    assert "GİZLİ HAFIZA DEFTERİ" not in dec_unclosed.message_to_agent


@pytest.mark.anyio
async def test_single_user_message_bridge_avoids_transcript_wrapper() -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    bridge = ExternalHttpMaxBridge(
        "https://wrapper.example.test",
        "sk-test",
        transport=httpx.MockTransport(handler),
    )
    await bridge.startup()
    try:
        ans = await bridge.complete(system_prompt="ROL KURALLARI", user_prompt="GÖREV")
    finally:
        await bridge.shutdown()

    assert ans == "ok"
    assert len(captured_body["messages"]) == 1
    assert captured_body["messages"][0]["role"] == "user"
    upstream_text = build_prompt(
        [ChatMessage(role="user", content=compose_single_turn_max_prompt("ROL", "GÖREV"))]
    )
    assert not upstream_text.startswith("System:")
    assert not upstream_text.endswith("Assistant:")


def test_head_tail_bash_evidence_and_interactive_blocker_recovery() -> None:
    long_stdout = (
        "collecting test items...\n"
        + ("src/module_check_line_ok\n" * 30)
        + "======================== 24 passed in 0.42s ========================\n"
    )
    snapshot: dict[str, Any] = {
        "id": "019da5d9-f4fc-7691-9e1e-34b5c1c9b784",
        "messages": [
            {
                "id": "m_asst",
                "role": "assistant",
                "parts": [
                    {
                        "type": "tool-bash",
                        "state": "output-available",
                        "input": {"command": "pytest"},
                        "output": {
                            "stdout": long_stdout,
                            "stderr": "",
                            "exit_code": 0,
                            "status": "completed",
                        },
                    },
                    {
                        "type": "tool-ask_user",
                        "state": "input-available",
                        "input": {
                            "questions": [
                                {"id": "q1", "question": "Hangi veritabanını kullanayım?"}
                            ]
                        },
                        "output": {},
                    },
                    {
                        "type": "tool-start_process",
                        "state": "output-available",
                        "input": {"name": "Website", "command": "npm run dev"},
                        "output": {
                            "alive": True,
                            "ports": [5173],
                            "warnings": ["Host header rejected by Vite preview"],
                        },
                    },
                ],
                "metadata": {"pending": False},
            }
        ],
    }
    state = parse_chat_state(snapshot)
    msg = state.last_assistant_message
    assert msg is not None
    evidence = format_sandbox_evidence(msg)

    # Ham stdout hala nesnede baş+son olarak tutulur ama dış isteme ham bash sızmaz:
    assert "collecting test items" in msg.tools[0].stdout_preview
    assert "24 passed in 0.42s" in msg.tools[0].stdout_preview
    assert "collecting test items" not in evidence
    assert "`" not in evidence
    # ask_user sorusu ve etkileşim engeli açıkça görünmeli
    assert "ETKİLEŞİM ENGELİ" in evidence
    assert "Hangi veritabanını kullanayım?" in evidence
    # start_process önizleme uyarısı hata olarak işaretlenmeli
    assert "PREVIEW_UYARISI" in evidence

    turn_prompt = build_turn_user_prompt(
        project_idea="Şu projeyi hazırla: finans panosu",
        turn=2,
        max_turns=7,
        current_phase="1_VIZYON_VE_CEKIRDEK",
        memory_summary="Adım 1 kuruldu.",
        turn_digests=[],
        last_sent_to_agent="Temeli kur.",
        agent_reply_text=" Seçiminizi bekliyorum.",
        sandbox_evidence=evidence,
        completed_turns=1,
        has_interactive_blocker=True,
    )
    assert "ERKEN BİTİRME KİLİDİ AKTİF (1/5 olgunluk turu)" in turn_prompt
    assert "[ETKİLEŞİM ENGELİ KURTARMA UYARISI]" in turn_prompt


# ---------------------------------------------------------------------------
# 5. Orkestratör: Hesap ayrılığı kilidi, çok turlu çalışma ve kayıttan devam
# ---------------------------------------------------------------------------


class _FakeMaxBridge(MaxBridge):
    """Durumsuzluğu doğrulayan sahte Max köprüsü."""

    def __init__(self, responses: list[str], *, sub: str = "max-account-999") -> None:
        self.responses = list(responses)
        self.sub = sub
        self.calls: list[tuple[str, str]] = []

    def account_identity(self) -> str:
        return self.sub

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if not self.responses:
            raise AssertionError("Beklenenden fazla Max çağrısı yapıldı")
        return self.responses.pop(0)


@pytest.mark.anyio
async def test_orchestrator_rejects_same_google_account(tmp_path: Path) -> None:
    same_sub = "shared-google-sub-777"
    raw_curl = _build_windows_cmd_input_curl(sub=same_sub)

    def fail_if_called(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("Aynı hesap tespit edildiğinde ağa çıkılmamalıydı!")

    agent = AgentSessionClient.from_curl(
        raw_curl, transport=httpx.MockTransport(fail_if_called)
    )
    bridge = _FakeMaxBridge(["<DURUM>TAMAMLANDI</DURUM>"], sub=same_sub)
    cfg = AutopilotConfig(
        project_idea="Test projesi",
        max_turns=3,
        gap_min_seconds=0.0,
        gap_max_seconds=0.0,
        state_file=tmp_path / "state.json",
    )
    orch = AutopilotOrchestrator(config=cfg, agent=agent, max_bridge=bridge)
    with pytest.raises(SameAccountError):
        await orch.run()


@pytest.mark.anyio
async def test_orchestrator_multi_turn_stateless_and_checkpoint_resume(
    tmp_path: Path,
) -> None:
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    raw_curl = _build_windows_cmd_input_curl(chat_id=chat_id, sub="agent-unique-1")
    agent_turn = 0
    last_sent_user_id = "019da5d9-f37d-71be-9d31-000000000001"
    sent_prompts: list[str] = []

    def mock_agent_http(request: httpx.Request) -> httpx.Response:
        nonlocal agent_turn, last_sent_user_id
        path = request.url.path
        if request.method == "GET" and path == "/" and not request.url.query:
            return httpx.Response(200, text="<html>dpl_TestDeploy123</html>")
        if request.method == "GET" and path == f"/api/chat/{chat_id}":
            messages: list[dict[str, Any]] = [
                {
                    "id": last_sent_user_id,
                    "role": "user",
                    "parts": [{"type": "text", "text": "prompt"}],
                    "metadata": {"pending": False},
                }
            ]
            if agent_turn > 0:
                messages.append(
                    {
                        "id": f"019da5d9-f37d-71be-9d31-00000000010{agent_turn}",
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool-bash",
                                "state": "output-available",
                                "input": {"command": f"pytest -k step_{agent_turn}"},
                                "output": {
                                    "stdout": f"step_{agent_turn} passed",
                                    "stderr": "",
                                    "exit_code": 0,
                                    "status": "completed",
                                },
                            },
                            {
                                "type": "text",
                                "text": f"Tur {agent_turn} tamamlandı ve doğrulandı.",
                            },
                        ],
                        "metadata": {"pending": False},
                    }
                )
            return httpx.Response(
                200,
                json={
                    "id": chat_id,
                    "triggerAddress": f"agent-runner:{chat_id}",
                    "lastEventId": str(agent_turn * 10),
                    "messages": messages,
                },
            )
        if request.method == "POST" and path == f"/api/chat/{chat_id}/input":
            agent_turn += 1
            body = json.loads(request.content.decode("utf-8"))
            last_sent_user_id = body["payload"]["messageId"]
            sent_prompts.append(body["payload"]["message"]["parts"][0]["text"])
            return httpx.Response(200, json={"ok": True, "seq": agent_turn * 10 + 1})
        if request.method == "GET" and path == f"/api/chat/{chat_id}/events":
            sse = (
                "event: batch\ndata: "
                + json.dumps(
                    {
                        "records": [
                            {
                                "seq_num": agent_turn * 10 + 2,
                                "headers": [["trigger-control", "turn-complete"]],
                                "body": "",
                            }
                        ]
                    }
                )
                + "\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse.encode("utf-8"),
            )
        return httpx.Response(404)

    state_file = tmp_path / "autopilot_state.json"

    # İlk koşu: 1 tur çalışsın (max_turns=1), Tur 1 sonunda değerlendirip kaydetsin
    bridge_part1 = _FakeMaxBridge(
        [
            "<DURUM>DEVAM</DURUM>\n"
            "<ASAMA>1_KURGU_ISKELET</ASAMA>\n"
            "<DEGERLENDIRME>Başlangıç planı hazır.</DEGERLENDIRME>\n"
            "<GUNCEL_HAFIZA>Adım 1: FastAPI çekirdeği kuruluyor.</GUNCEL_HAFIZA>\n"
            "<AJANA_MESAJ>Çekirdek API'yi ve SQLite şemasını kurup test et.</AJANA_MESAJ>",
            "<DURUM>DEVAM</DURUM>\n"
            "<ASAMA>2_GERCEK_ISLEV</ASAMA>\n"
            "<DEGERLENDIRME>Tur 1 testleri geçti, sırada arayüz var.</DEGERLENDIRME>\n"
            "<GUNCEL_HAFIZA>Adım 1 doğrulandı; sırada arayüz ve analitik var.</GUNCEL_HAFIZA>\n"
            "<AJANA_MESAJ>Şimdi arayüzü ekle.</AJANA_MESAJ>",
        ],
        sub="max-unique-2",
    )
    orch1 = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="Akıllı abonelik ve harcama analizi web uygulaması",
            max_turns=1,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            state_file=state_file,
        ),
        agent=AgentSessionClient.from_curl(
            raw_curl, turn_timeout=5.0, transport=httpx.MockTransport(mock_agent_http)
        ),
        max_bridge=bridge_part1,
    )
    st1 = await orch1.run()
    assert st1.status == "max_turns_reached"
    assert len(st1.turns) == 1
    assert AGENT_OPERATING_GUARDRAILS in sent_prompts[0]
    assert state_file.is_file()

    # Durum dosyasında gizli çerez/token sızıntısı olmamalı!
    saved_raw = state_file.read_text(encoding="utf-8")
    assert "arena-auth-prod" not in saved_raw
    assert "test_static_recaptcha_v3" not in saved_raw

    # İkinci koşu: Kaydedilen durumdan devam etsin ve TAMAMLANDI kararıyla sonlansın
    loaded_state = AutopilotRunState.from_dict(json.loads(saved_raw))
    bridge_part2 = _FakeMaxBridge(
        [
            "<DURUM>DEVAM</DURUM>\n"
            "<ASAMA>2_GERCEK_ISLEV</ASAMA>\n"
            "<DEGERLENDIRME>Çekirdek testleri geçmiş.</DEGERLENDIRME>\n"
            "<GUNCEL_HAFIZA>Adım 1 bitti. Adım 2: Arayüz ve grafikler.</GUNCEL_HAFIZA>\n"
            "<AJANA_MESAJ>İnteraktif arayüzü ve uyarı motorunu ekleyip test et.</AJANA_MESAJ>",
            "<DURUM>TAMAMLANDI</DURUM>\n"
            "<ASAMA>4_KABUL</ASAMA>\n"
            "<DEGERLENDIRME>Tüm testler exit_code=0 ile geçti, ürün eksiksiz.</DEGERLENDIRME>\n"
            "<GUNCEL_HAFIZA>Proje tamamlandı ve doğrulandı.</GUNCEL_HAFIZA>\n"
            "<AJANA_MESAJ></AJANA_MESAJ>",
        ],
        sub="max-unique-2",
    )
    orch2 = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea=loaded_state.project_idea,
            max_turns=3,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            state_file=state_file,
        ),
        agent=AgentSessionClient.from_curl(
            raw_curl, turn_timeout=5.0, transport=httpx.MockTransport(mock_agent_http)
        ),
        max_bridge=bridge_part2,
        resume_state=loaded_state,
    )
    st2 = await orch2.run()
    assert st2.status == "completed"
    assert len(st2.turns) == 2
    assert "Adım 1 doğrulandı" in bridge_part2.calls[0][1]
    assert "Tur 2 tamamlandı ve doğrulandı" in bridge_part2.calls[1][1]
    assert "komut/test=1 başarılı / 0 hatalı" in bridge_part2.calls[1][1]
    assert "pytest -k step_2" not in bridge_part2.calls[1][1]


@pytest.mark.anyio
async def test_orchestrator_blocks_premature_exit_on_7_turn_run(tmp_path: Path) -> None:
    """7 turluk projede 1. veya 2. turda 'TAMAMLANDI' deyip kaçmayı engellemeli."""
    assert compute_min_turns_before_completion(7) == 5
    assert compute_min_turns_before_completion(8) == 6

    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b799"
    raw_curl = _build_windows_cmd_input_curl(chat_id=chat_id, sub="agent-7turn-sub")
    agent_turn = 0
    last_sent_user_id = "019da5d9-f37d-71be-9d31-000000000001"

    def mock_agent_http(request: httpx.Request) -> httpx.Response:
        nonlocal agent_turn, last_sent_user_id
        path = request.url.path
        if request.method == "GET" and path == "/" and not request.url.query:
            return httpx.Response(200, text="<html>dpl_TestDeploy123</html>")
        if request.method == "GET" and path == f"/api/chat/{chat_id}":
            messages: list[dict[str, Any]] = [
                {
                    "id": last_sent_user_id,
                    "role": "user",
                    "parts": [{"type": "text", "text": "prompt"}],
                    "metadata": {"pending": False},
                }
            ]
            if agent_turn > 0:
                messages.append(
                    {
                        "id": f"019da5d9-f37d-71be-9d31-00000000020{agent_turn}",
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool-bash",
                                "state": "output-available",
                                "input": {"command": f"pytest -k phase_{agent_turn}"},
                                "output": {
                                    "stdout": f"phase_{agent_turn} passed",
                                    "stderr": "",
                                    "exit_code": 0,
                                    "status": "completed",
                                },
                            },
                            {"type": "text", "text": f"Aşama {agent_turn} bitti."},
                        ],
                        "metadata": {"pending": False},
                    }
                )
            return httpx.Response(
                200,
                json={
                    "id": chat_id,
                    "triggerAddress": f"agent-runner:{chat_id}",
                    "lastEventId": str(agent_turn * 10),
                    "messages": messages,
                },
            )
        if request.method == "POST" and path == f"/api/chat/{chat_id}/input":
            agent_turn += 1
            body = json.loads(request.content.decode("utf-8"))
            last_sent_user_id = body["payload"]["messageId"]
            return httpx.Response(200, json={"ok": True, "seq": agent_turn * 10 + 1})
        if request.method == "GET" and path == f"/api/chat/{chat_id}/events":
            sse = (
                "event: batch\ndata: "
                + json.dumps(
                    {
                        "records": [
                            {
                                "seq_num": agent_turn * 10 + 2,
                                "headers": [["trigger-control", "turn-complete"]],
                                "body": "",
                            }
                        ]
                    }
                )
                + "\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse.encode("utf-8"),
            )
        return httpx.Response(404)

    # Max Tur 1 sonunda erken TAMAMLANDI verip kaçmaya çalışıyor; orkestratör bunu
    # yakalayıp reddetmeli ve en az 5. tura kadar yaratıcı derinleştirmeyi sürdürmeli!
    bridge = _FakeMaxBridge(
        [
            # Kickoff -> Tur 1
            "<DURUM>DEVAM</DURUM><ASAMA>1_VIZYON_VE_CEKIRDEK</ASAMA>"
            "<GUNCEL_HAFIZA>Yol haritası kuruldu.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Çekirdek mimariyi kur.</AJANA_MESAJ>",
            # Tur 1 sonu: Max erken bitirip kaçmaya çalışır!
            "<DURUM>TAMAMLANDI</DURUM><ASAMA>5_KABUL</ASAMA>"
            "<DEGERLENDIRME>İskelet çalışıyor, bitirelim.</DEGERLENDIRME>"
            "<GUNCEL_HAFIZA>Tur 1 bitti.</GUNCEL_HAFIZA><AJANA_MESAJ></AJANA_MESAJ>",
            # Orkestratör reddedince Max yaratıcı imza özellik adımı üretir -> Tur 2
            "<DURUM>DEVAM</DURUM><ASAMA>2_YARATICI_DERINLIK</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 2: İnteraktif senaryo simülatörü.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Şimdi interaktif bütçe simülatörünü ekle.</AJANA_MESAJ>",
            # Tur 2 sonu -> Tur 3
            "<DURUM>DEVAM</DURUM><ASAMA>2_YARATICI_DERINLIK</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 3: Akıllı filtre ve dışa aktarma.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Akıllı filtreleri ve veri kalıcılığını tamamla.</AJANA_MESAJ>",
            # Tur 3 sonu -> Tur 4
            "<DURUM>DEVAM</DURUM><ASAMA>3_DENEYIM_VE_CILA</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 4: UX cilası ve uç durumlar.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Mikro-etkileşimleri ve mobil/klavye cilasını yap.</AJANA_MESAJ>",
            # Tur 4 sonu -> Tur 5
            "<DURUM>DEVAM</DURUM><ASAMA>4_DOGRULAMA_VE_SUNUCU</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 5: Uçtan uca test ve 0.0.0.0 sunucu.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Uçtan uca testleri koş ve 0.0.0.0 sunucuyu doğrula.</AJANA_MESAJ>",
            # Tur 5 sonu (artık 5/5 olgunluk kapısı aşıldı!): TAMAMLANDI kabul edilir!
            "<DURUM>TAMAMLANDI</DURUM><ASAMA>5_KABUL</ASAMA>"
            "<DEGERLENDIRME>5 aşama da eksiksiz doğrulandı.</DEGERLENDIRME>"
            "<GUNCEL_HAFIZA>Tamamlandı.</GUNCEL_HAFIZA><AJANA_MESAJ></AJANA_MESAJ>",
        ],
        sub="max-7turn-sub",
    )

    orch = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="Şu projeyi hazırla: kişisel bütçe uygulaması",
            max_turns=7,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            state_file=tmp_path / "run7.json",
        ),
        agent=AgentSessionClient.from_curl(
            raw_curl, turn_timeout=5.0, transport=httpx.MockTransport(mock_agent_http)
        ),
        max_bridge=bridge,
    )
    final_state = await orch.run()

    assert final_state.status == "completed"
    assert len(final_state.turns) == 5
    # Reddedilen erken çıkış sonrasında gönderilen istemde zorunlu derinleştirme notu olmalı
    assert "[ERKEN BİTİRME REDDİ — ZORUNLU DERİNLEŞTİRME]" in bridge.calls[2][1]


@pytest.mark.anyio
async def test_night_window_queue_auto_turns_and_every_turn_no_ask_guardrail(
    tmp_path: Path,
) -> None:
    """Gündüz (18:30 TR) bırakılan proje 22:00'ye dek Max'a dokunmamalı; 22:00'de
    otomatik başlayıp <HEDEF_TUR> ile ilerlemeli ve her turda ask_user yasağı eklemeli."""
    from datetime import datetime, timedelta

    t_1400 = datetime(2026, 11, 20, 14, 0, tzinfo=TR_TZ)
    t_2159 = datetime(2026, 11, 20, 21, 59, tzinfo=TR_TZ)
    t_2200 = datetime(2026, 11, 20, 22, 0, tzinfo=TR_TZ)
    t_0230 = datetime(2026, 11, 21, 2, 30, tzinfo=TR_TZ)
    t_0700 = datetime(2026, 11, 21, 7, 0, tzinfo=TR_TZ)

    assert not is_in_night_window(t_1400)
    assert not is_in_night_window(t_2159)
    assert is_in_night_window(t_2200)
    assert is_in_night_window(t_0230)
    assert not is_in_night_window(t_0700)
    assert seconds_until_window_opens(t_2159) == 60.0
    assert seconds_until_window_opens(t_2200) == 0.0

    # Sanal saat: 18:30 TR'den başlar; her sleep çağrısında 22:05 TR'ye ilerler
    current_clock = datetime(2026, 11, 20, 18, 30, tzinfo=TR_TZ)
    sleep_calls = 0
    saw_waiting_status = False

    def fake_now() -> datetime:
        return current_clock

    async def fake_sleep(_secs: float) -> None:
        nonlocal current_clock, sleep_calls
        sleep_calls += 1
        # Bekleme sırasında Max'a henüz HİÇ çağrı gitmemiş olmalı!
        assert len(bridge.calls) == 0
        assert agent_http_hits == 0
        current_clock = current_clock + timedelta(hours=3, minutes=35)  # -> 22:05 TR

    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b888"
    raw_curl = _build_windows_cmd_input_curl(chat_id=chat_id, sub="agent-night-sub")
    agent_http_hits = 0
    agent_turn = 0
    last_sent_user_id = "019da5d9-f37d-71be-9d31-000000000001"
    sent_to_agent_texts: list[str] = []

    def mock_agent_http(request: httpx.Request) -> httpx.Response:
        nonlocal agent_http_hits, agent_turn, last_sent_user_id
        agent_http_hits += 1
        path = request.url.path
        if request.method == "GET" and path == "/" and not request.url.query:
            return httpx.Response(200, text="<html>dpl_TestDeploy123</html>")
        if request.method == "GET" and path == f"/api/chat/{chat_id}":
            messages: list[dict[str, Any]] = [
                {
                    "id": last_sent_user_id,
                    "role": "user",
                    "parts": [{"type": "text", "text": "prompt"}],
                    "metadata": {"pending": False},
                }
            ]
            if agent_turn > 0:
                messages.append(
                    {
                        "id": f"019da5d9-f37d-71be-9d31-00000000030{agent_turn}",
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "tool-bash",
                                "state": "output-available",
                                "input": {"command": "pytest -q"},
                                "output": {
                                    "stdout": "8 passed",
                                    "stderr": "",
                                    "exit_code": 0,
                                    "status": "completed",
                                },
                            },
                            {"type": "text", "text": f"Tur {agent_turn} tamam."},
                        ],
                        "metadata": {"pending": False},
                    }
                )
            return httpx.Response(
                200,
                json={
                    "id": chat_id,
                    "triggerAddress": f"agent-runner:{chat_id}",
                    "lastEventId": str(agent_turn * 10),
                    "messages": messages,
                },
            )
        if request.method == "POST" and path == f"/api/chat/{chat_id}/input":
            agent_turn += 1
            body = json.loads(request.content.decode("utf-8"))
            last_sent_user_id = body["payload"]["messageId"]
            sent_to_agent_texts.append(body["payload"]["message"]["parts"][0]["text"])
            return httpx.Response(200, json={"ok": True, "seq": agent_turn * 10 + 1})
        if request.method == "GET" and path == f"/api/chat/{chat_id}/events":
            sse = (
                "event: batch\ndata: "
                + json.dumps(
                    {
                        "records": [
                            {
                                "seq_num": agent_turn * 10 + 2,
                                "headers": [["trigger-control", "turn-complete"]],
                                "body": "",
                            }
                        ]
                    }
                )
                + "\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse.encode("utf-8"),
            )
        return httpx.Response(404)

    bridge = _FakeMaxBridge(
        [
            # Kickoff: Max projeyi kompakt bulup <HEDEF_TUR>4</HEDEF_TUR> ve <AKSAM_PLANI> üretir
            "<DURUM>DEVAM</DURUM><ASAMA>1_VIZYON_VE_CEKIRDEK</ASAMA>"
            "<HEDEF_TUR>4</HEDEF_TUR>"
            "<AKSAM_PLANI>- Vizyon: Canlı formül laboratuvarı\n- İmza: Anlık grafik</AKSAM_PLANI>"
            "<GUNCEL_HAFIZA>4 turluk kompakt yol haritası.</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Çekirdek modülü kur.</AJANA_MESAJ>",
            "<DURUM>DEVAM</DURUM><ASAMA>2_YARATICI_DERINLIK</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 2</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>İmza simülasyon özelliğini ekle.</AJANA_MESAJ>",
            "<DURUM>DEVAM</DURUM><ASAMA>3_DENEYIM_VE_CILA</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 3</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>UX cilasını yap.</AJANA_MESAJ>",
            "<DURUM>DEVAM</DURUM><ASAMA>4_DOGRULAMA_VE_SUNUCU</ASAMA>"
            "<GUNCEL_HAFIZA>Tur 4</GUNCEL_HAFIZA>"
            "<AJANA_MESAJ>Canlı sunucuyu 0.0.0.0 üzerinde doğrula.</AJANA_MESAJ>",
            "<DURUM>TAMAMLANDI</DURUM><ASAMA>5_KABUL</ASAMA>"
            "<DEGERLENDIRME>Tamamlandı.</DEGERLENDIRME>"
            "<GUNCEL_HAFIZA>Bitti.</GUNCEL_HAFIZA><AJANA_MESAJ></AJANA_MESAJ>",
        ],
        sub="max-night-sub",
    )

    immediate_alerts: list[str] = []
    scheduled_morning: list[tuple[str, str, float]] = []

    class _FakeAlerter(TelegramAlerter):
        def __init__(self) -> None:
            super().__init__("fake-token", 42)

        async def send(self, text: str, *, label: str | None = None) -> bool:
            immediate_alerts.append(text)
            return True

    def capture_morning(rid: str, text: str, delay: float) -> None:
        scheduled_morning.append((rid, text, delay))

    def track_progress(st: AutopilotRunState, _note: str) -> None:
        nonlocal saw_waiting_status
        if st.status == "waiting_window":
            saw_waiting_status = True

    orch = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="Kompakt birim dönüştürücü ve formül simülatörü",
            max_turns=12,
            auto_turns=True,
            respect_night_window=True,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            state_file=tmp_path / "night.json",
        ),
        agent=AgentSessionClient.from_curl(
            raw_curl, turn_timeout=5.0, transport=httpx.MockTransport(mock_agent_http)
        ),
        max_bridge=bridge,
        alerter=_FakeAlerter(),
        on_progress=track_progress,
        now_fn=fake_now,
        sleep_fn=fake_sleep,
        morning_dispatcher=capture_morning,
    )
    st = await orch.run()

    assert saw_waiting_status is True
    assert sleep_calls == 1
    assert st.status == "completed"
    assert st.planned_turns == 4
    assert len(st.turns) == 4
    assert "Canlı formül laboratuvarı" in st.evening_plan
    assert st.handoff_report == "Tur 5 tamam."
    assert sent_to_agent_texts[-1] == AGENT_MORNING_HANDOFF_PROMPT

    # Akşam 22:05'te başlayan iş bittiğinde sabah 07:00'ye (8 sa 55 dk = 32100 sn) planlanmalı!
    assert seconds_until_morning_hour(datetime(2026, 11, 21, 1, 0, tzinfo=TR_TZ)) == 21600.0
    # Plan başlığı sadeleşti (mesaj artık "tek bakışta anlaşılır" biçimde).
    assert any("🌆 Plan hazır" in msg for msg in immediate_alerts)
    assert any("Ne yapacağız:" in msg for msg in immediate_alerts)
    assert len(scheduled_morning) == 1
    assert "🌅 Sabah Teslim Raporu" in scheduled_morning[0][1]
    assert "Tur 5 tamam." in scheduled_morning[0][1]
    assert scheduled_morning[0][2] == 32100.0

    # 1. turda tam guardrail, 2..N turların HEPSİNDE ask_user yasağı olmalı; iç etiket sızmamalı!
    assert AGENT_OPERATING_GUARDRAILS in sent_to_agent_texts[0]
    assert "AKSAM_PLANI" not in sent_to_agent_texts[0]
    for later_msg in sent_to_agent_texts[1:]:
        assert AGENT_FOLLOWUP_NO_ASK_GUARDRAIL in later_msg
        assert "HEDEF_TUR" not in later_msg
        assert "AKSAM_PLANI" not in later_msg
    # Saat/zaman bilgisi Max prompt'una karışmamalı:
    for _, usr_prompt in bridge.calls:
        assert "22:00" not in usr_prompt and "07:00" not in usr_prompt


@pytest.mark.anyio
async def test_stop_cancels_semaphore_waiting_night_run_immediately(tmp_path: Path) -> None:
    """Semafor kuyruğunda bekleyen 2. gece projesi /durdur ile anında durmalı."""
    import asyncio
    from datetime import datetime

    sem = asyncio.Semaphore(1)
    await sem.acquire()  # 1. proje çalışıyor gibi semafor kilitli

    raw_curl = _build_windows_cmd_input_curl(sub="agent-sem-sub")

    def fail_if_called(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("Semafor beklerken durdurulan proje ağa çıkmamalı!")

    orch = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="İkinci gece projesi",
            max_turns=6,
            respect_night_window=True,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            state_file=tmp_path / "sem2.json",
        ),
        agent=AgentSessionClient.from_curl(
            raw_curl, transport=httpx.MockTransport(fail_if_called)
        ),
        max_bridge=_FakeMaxBridge([], sub="max-sem-sub"),
        now_fn=lambda: datetime(2026, 11, 20, 23, 0, tzinfo=TR_TZ),
        execution_semaphore=sem,
    )

    run_task = asyncio.create_task(orch.run())
    await asyncio.sleep(0.05)
    assert orch.state.status == "waiting_window"
    orch.request_stop()
    result = await asyncio.wait_for(run_task, timeout=1.0)
    assert result.status == "stopped"
    assert sem.locked()  # 1. projenin kilidini yanlışlıkla serbest bırakmamalı!
    sem.release()


# ---------------------------------------------------------------------------
# 6. FastAPI /v1/autopilot/* uç noktası testleri
# ---------------------------------------------------------------------------


def test_autopilot_api_inspect_curl_and_auth() -> None:
    max_cookie = _make_fake_supabase_cookie("max-server-account")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_keys="sk-test-autopilot",  # type: ignore[arg-type]
        target_domain="arena.example.test",
        upstream_cookie=max_cookie,
        session_warmup_on_startup=False,
        keepalive_enabled=False,
        models_config_path="config/models.yaml",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        unauth = client.post(
            "/v1/autopilot/inspect-curl", json={"agent_curl": "curl https://x.test"}
        )
        assert unauth.status_code == 401

        diff_curl = _build_windows_cmd_input_curl(sub="separate-agent-account")
        ok_resp = client.post(
            "/v1/autopilot/inspect-curl",
            headers={"Authorization": "Bearer sk-test-autopilot"},
            json={"agent_curl": diff_curl},
        )
        assert ok_resp.status_code == 200
        data = ok_resp.json()
        assert data["valid"] is True
        assert data["same_account_conflict"] is False
        assert data["has_recaptcha_token"] is True

        same_curl = _build_windows_cmd_input_curl(sub="max-server-account")
        conflict_resp = client.post(
            "/v1/autopilot/inspect-curl",
            headers={"Authorization": "Bearer sk-test-autopilot"},
            json={"agent_curl": same_curl},
        )
        assert conflict_resp.status_code == 200
        assert conflict_resp.json()["same_account_conflict"] is True


@pytest.mark.asyncio
async def test_external_http_max_bridge_via_real_ask_endpoint() -> None:
    """`ExternalHttpMaxBridge(endpoint_mode='ask')` gerçek `/v1/ask` ucuyla (`text` alanı)
    birebir uyumlu çalışmalıdır.
    """
    from app.autopilot.max_bridge import ExternalHttpMaxBridge

    max_cookie = _make_fake_supabase_cookie("max-ask-account")
    settings = Settings(
        _env_file=None,
        api_keys=["sk-ask-bridge"],
        target_domain="target.example",
        upstream_cookie=max_cookie,
        recaptcha_static_token="test-recaptcha-token",
        session_warmup_on_startup=False,
        keepalive_enabled=False,
        upstream_keepalive_interval_seconds=0.0,
        models_config_path="config/models.yaml",
    )
    def fake_upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        sse = '0:"<DURUM>DEVAM</DURUM>\\n<AJANA_MESAJ>Merhaba</AJANA_MESAJ>"\n'
        sse += 'd:{"finishReason":"stop"}\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    app = create_app(settings)

    async with app.router.lifespan_context(app):
        mock_tr = httpx.MockTransport(fake_upstream)
        app.state.upstream._build_client = lambda _s: httpx.AsyncClient(
            base_url="https://target.example",
            transport=mock_tr,
        )
        app.state.upstream._client = app.state.upstream._build_client(app.state.settings)
        app.state.upstream._account_clients.clear()
        bridge = ExternalHttpMaxBridge(
            "http://wrapper.internal",
            "sk-ask-bridge",
            endpoint_mode="ask",
            transport=httpx.ASGITransport(app=app),
        )
        await bridge.startup()
        try:
            reply = await bridge.complete(
                system_prompt="Sistem",
                user_prompt="Proje başlat",
            )
        finally:
            await bridge.shutdown()

    assert "<DURUM>DEVAM</DURUM>" in reply
    assert "Merhaba" in reply


@pytest.mark.asyncio
async def test_autopilot_waits_patiently_on_max_429_and_ask_problem_without_hammering() -> None:
    """`max` 429 (`prompt failed`) veya `/v1/ask 'problem'` döndürdüğünde koşu
    çökmemeli; soğuma takvimini (`max_cooldown_schedule`, varsayılan 310s/600s/900s)
    bekleyip kaldığı yerden devam etmelidir. 401/409 ise anında durmalıdır.
    """
    from app.autopilot.max_bridge import MaxBridgeError

    default_cfg = AutopilotConfig(project_idea="Test")
    # Zamanlama sözleşmesi (bilinçli değişiklik, 2026-11-23):
    # * Orkestratörün tur aralığı 15-30 sn -> 0-3 sn: nazik aralık artık tele
    #   giden yolda (`RequestQueue`, REQUEST_GAP_*) uygulanıyor; ikisi birlikte
    #   tur başına ~45 sn saf bekleme üretiyordu (docs/DENGE_VE_3_HESAP_ANALIZ §2.2).
    # * Soğuma 310/600/900 -> 60/180/420/900: 429 çoğu kez geçici; 5 dk beklemek
    #   yerine 1 dk'da yoklamak daha çok tur kazandırıyor, son basamak yine sabırlı.
    assert (default_cfg.gap_min_seconds, default_cfg.gap_max_seconds) == (0.0, 3.0)
    assert default_cfg.max_cooldown_schedule == (60.0, 180.0, 420.0, 900.0)

    assert AutopilotOrchestrator._is_fatal_max_error(MaxBridgeError("HTTP 401 unauthorized"))
    assert AutopilotOrchestrator._is_fatal_max_error(MaxBridgeError("HTTP 409 refresh_required"))
    assert not AutopilotOrchestrator._is_fatal_max_error(
        MaxBridgeError("HTTP 429: prompt failed")
    )
    assert not AutopilotOrchestrator._is_fatal_max_error(
        MaxBridgeError("Dış max /v1/ask 'problem' bildirdi (reason=unknown).")
    )

    calls = 0

    class FlakyMaxBridge(MaxBridge):
        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def connect(self, **kwargs: Any) -> Any:
            return SimpleNamespace(last_assistant_message=None, has_pending_turn=False)

        def account_identity(self) -> str:
            return "max-sub"

        async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise MaxBridgeError("HTTP 429: prompt failed")
            if calls == 2:
                raise MaxBridgeError("Dış max /v1/ask 'problem' bildirdi (reason=unknown).")
            return "<DURUM>TAMAMLANDI</DURUM>\n<ASAMA>5_KABUL</ASAMA>\n<AJANA_MESAJ></AJANA_MESAJ>"

    agent_curl = _build_windows_cmd_input_curl(sub="agent-sub")
    progress_notes: list[str] = []
    orch = AutopilotOrchestrator(
        config=AutopilotConfig(
            project_idea="Soğuma testi",
            max_turns=1,
            gap_min_seconds=0.0,
            gap_max_seconds=0.0,
            max_cooldown_schedule=(0.01, 0.01, 0.01),
            enable_handoff_report=False,
        ),
        agent=AgentSessionClient.from_curl(agent_curl),
        max_bridge=FlakyMaxBridge(),
        on_progress=lambda _st, note: progress_notes.append(note),
    )
    result = await orch._complete_max_safe(system_prompt="S", user_prompt="U")
    assert "TAMAMLANDI" in result
    assert calls == 3
    assert sum("dinlenip kaldığı yerden" in n for n in progress_notes) == 2


def test_lemur_http3_multiple_cookie_headers_and_posix_adjacent_quotes_preserved() -> None:
    """Android Lemur Browser HTTP/2-HTTP/3 'Copy as cURL (bash)' çıktısında birden fazla
    `-H 'cookie: ...'` satırı geldiğinde son satır (`_dd_s`) önceki `arena-auth-prod-v1.0/.1`
    çerezlerini ezmemeli; ayrıca POSIX bitişik tırnakları (`'...'$'\''...'`, `--data-raw=$'...'`)
    tek argüman olarak birleşmelidir.
    """
    from app.autopilot.curl_parse import (
        inspect_agent_cookie_health,
        parse_raw_agent_curl,
        reassemble_agent_curl_chunks,
    )

    supa_cookie = _make_fake_supabase_cookie("lemur-http3-user-44")
    part0, part1 = [c.strip() for c in supa_cookie.split(";")]
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    msg_id = "019da5d9-f37d-71be-9d31-000000000001"

    lemur_http3_curl = (
        f"curl 'https://target.example/api/chat/{chat_id}/input' \\\n"
        "  -H 'authority: target.example' \\\n"
        "  -H ':method: POST' \\\n"
        "  -H 'sec-ch-ua-mobile: ?1' \\\n"
        "  -H 'sec-ch-ua-platform: \"Android\"' \\\n"
        "  -H 'cookie: cf_clearance=cf_lemur_ok' \\\n"
        f"  -H 'cookie: {part0}' \\\n"
        f"  -H 'cookie: {part1}' \\\n"
        "  -H 'cookie: _dd_s=rum=0&expire=1999999999' \\\n"
        + r"  -H 'x-custom-note: merhaba'$'\'''dunya' "
        + "\\\n"
        + f"  --data-raw=$'{{\"message\":{{\"id\":\"{msg_id}\"}},"
        "\"recaptchaV3Token\":\"rc_tok\"}'"
    )

    # Telegram 4096 benzeri parçalanma altında da çerez parçalarının tamamı korunmalı
    mid = len(lemur_http3_curl) // 2
    rejoined = reassemble_agent_curl_chunks([lemur_http3_curl[:mid], lemur_http3_curl[mid:]])
    raw_parsed = parse_raw_agent_curl(rejoined)
    cfg = parse_agent_curl(rejoined)

    assert cfg.account_identity() == "lemur-http3-user-44"
    assert "arena-auth-prod-v1.0=" in cfg.cookie
    assert "arena-auth-prod-v1.1=" in cfg.cookie
    assert "cf_clearance=cf_lemur_ok" in cfg.cookie
    assert "_dd_s=rum=0&expire=1999999999" in cfg.cookie
    assert ":method" not in cfg.extra_headers
    assert cfg.extra_headers.get("sec-ch-ua-mobile") == "?1"
    assert cfg.sec_ch_ua_platform_override == '"Android"'
    assert raw_parsed["headers"].get("x-custom-note") == "merhaba'dunya"
    assert cfg.last_user_message_id == msg_id
    assert cfg.recaptcha_token == "rc_tok"

    health = inspect_agent_cookie_health(cfg.cookie, cfg.access_token)
    assert health["auth_cookie_found"] is True
    assert health["auth_chunk_indices"] == [0, 1]
    assert health["incomplete_auth_chunks"] is False
    assert health["jwt_found"] is True
    assert health["problems"] == []


def test_inspect_agent_cookie_health_detects_missing_chunks_and_expired_jwt() -> None:
    """Eksik `.0` parçası, kesilmiş uzun tekil `.0` parçası ve süresi dolmuş JWT `exp`
    gizli değer sızdırmadan teşhis edilmelidir.
    """
    from app.autopilot.curl_parse import inspect_agent_cookie_health

    # 1) Yalnız `.1` var, `.0` düşmüş
    diag_missing_0 = inspect_agent_cookie_health("arena-auth-prod-v1.1= yarim_parca; _dd_s=1")
    assert diag_missing_0["auth_cookie_found"] is True
    assert diag_missing_0["incomplete_auth_chunks"] is True
    assert any("[0]" in p for p in diag_missing_0["problems"])

    # 2) Yalnız `.0` var ve >=2800 karakter (kesilmiş JSON, `.1` kopyalanmamış)
    truncated_0 = "arena-auth-prod-v1.0=base64-" + ("A" * 3000)
    diag_trunc = inspect_agent_cookie_health(truncated_0)
    assert diag_trunc["incomplete_auth_chunks"] is True
    assert diag_trunc["jwt_found"] is False

    # 3) Süresi dolmuş JWT (`exp` geçmişte)
    hdr = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    old_payload = (
        base64.urlsafe_b64encode(json.dumps({"sub": "u-exp", "exp": 1700000000}).encode())
        .decode()
        .rstrip("=")
    )
    expired_jwt = f"{hdr}.{old_payload}.sig"
    sess_b64 = "base64-" + base64.urlsafe_b64encode(
        json.dumps({"access_token": expired_jwt}).encode()
    ).decode().rstrip("=")
    diag_exp = inspect_agent_cookie_health(f"arena-auth-prod-v1={sess_b64}")
    assert diag_exp["jwt_found"] is True
    assert diag_exp["token_expired"] is True
    assert any("dolmuş" in p for p in diag_exp["problems"])


@pytest.mark.asyncio
async def test_inspect_curl_verify_live_detects_401_and_caches_rotated_cookie_without_secret_leak(
    respx_mock: Any,
) -> None:
    """`/v1/autopilot/inspect-curl` (`verify_live=True`) gerçek dışı olmayan alan adlarında
    canlı oturumu yoklamalı:
    - 401 döndüğünde `live_ok=False` bildirmeli,
    - 200 döndüğünde ve `Set-Cookie` ile çerez yenilendiğinde yeni çerezi yalnız RAM'e
      kaydetmeli (JSON çıktısına asla ham çerez sızdırmamalı).
    """
    chat_id = "019da5d9-f4fc-7691-9e1e-34b5c1c9b784"
    supa_cookie = _make_fake_supabase_cookie("agent-live-user-2")
    live_curl = (
        f"curl 'https://live-upstream.io/api/chat/{chat_id}/input' "
        f"-H 'cookie: {supa_cookie}' "
        "--data-raw '{\"id\":\"019da5d9-f37d-71be-9d31-000000000001\",\"text\":\"hi\"}'"
    )

    settings = Settings(api_keys=["sk-test-key"])
    app = create_app(settings)
    headers = {"Authorization": "Bearer sk-test-key"}

    # Senaryo A: 401 Unauthorized -> live_ok=False (ek sayfa isteği atmadan doğrudan döner)
    respx_mock.get(f"https://live-upstream.io/api/chat/{chat_id}").mock(
        return_value=httpx.Response(401, json={"error": "Not authenticated"})
    )

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.local",
        ) as client,
    ):
        r401 = await client.post(
            "/v1/autopilot/inspect-curl",
            headers=headers,
            json={"agent_curl": live_curl, "verify_live": True},
        )
        assert r401.status_code == 200
        body401 = r401.json()
        assert body401["live_checked"] is True
        assert body401["live_ok"] is False
        assert body401["live_status_code"] == 401
        assert body401["live_error_type"] == "UpstreamAuthRejected"

        # Senaryo B: 200 OK + `Domain=.live-upstream.io` Set-Cookie rotasyonu (CookieConflict yok)
        secret_rotated = "ROTATED_SUPER_SECRET_COOKIE_VALUE_999"
        respx_mock.get(f"https://live-upstream.io/api/chat/{chat_id}").mock(
            return_value=httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        f"cf_clearance={secret_rotated}; Domain=.live-upstream.io; Path=/"
                    )
                },
                json={"id": chat_id, "messages": [{"id": "m1", "role": "user", "parts": []}]},
            )
        )
        r200 = await client.post(
            "/v1/autopilot/inspect-curl",
            headers=headers,
            json={"agent_curl": live_curl, "verify_live": True},
        )
        assert r200.status_code == 200
        raw_txt = r200.text
        assert secret_rotated not in raw_txt
        body200 = r200.json()
        assert body200["live_checked"] is True
        assert body200["live_ok"] is True
        assert body200["live_message_count"] == 1
        assert body200["cookie_rotated_on_probe"] is True


@pytest.mark.asyncio
async def test_autopilot_stop_all_and_clear_runs_cancels_active_and_preserves_slot_1() -> None:
    """`POST /v1/autopilot/runs/clear` ve `/runs/stop-all` bekleyen/çalışan otopilot
    görevlerini anında iptal etmeli, gece semaforunu serbest bırakmalı ve 1. ana Max
    hesabına (`slot_manager`) kesinlikle dokunmamalıdır.
    """
    max_cookie_1 = _make_fake_supabase_cookie("max-primary-slot-1")
    max_curl_1 = (
        "curl 'https://target.example/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {max_cookie_1}' "
        "-H 'referer: https://target.example/c/1' "
        "--data-raw '{\"id\":\"1\",\"mode\":\"direct\",\"modelAId\":\"m1\","
        "\"userMessage\":\"hi\",\"recaptchaV3Token\":\"rc-slot1\"}'"
    )
    settings = Settings(api_keys=["sk-test-key"], upstream_curl=max_curl_1)
    app = create_app(settings)
    headers = {"Authorization": "Bearer sk-test-key"}
    agent_curl = _build_windows_cmd_input_curl(sub="agent-slot-2-distinct")

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://wrapper.local",
        ) as client,
    ):
        # Gece penceresi bekleyen (veya çalışan) bir görev başlat
        start_resp = await client.post(
            "/v1/autopilot/runs",
            headers=headers,
            json={
                "project_idea": "Temizlenecek deneme projesi",
                "agent_curl": agent_curl,
                "respect_night_window": True,
                "night_start_hour": 4,
                "night_end_hour": 5,
            },
        )
        assert start_resp.status_code == 200
        run_id = start_resp.json()["run_id"]

        list_before = (await client.get("/v1/autopilot/runs", headers=headers)).json()
        assert len(list_before["runs"]) == 1

        clear_resp = await client.post(
            "/v1/autopilot/runs/clear",
            headers=headers,
            json={"include_active": True},
        )
        assert clear_resp.status_code == 200
        clear_data = clear_resp.json()
        assert clear_data["removed_runs"] == 1
        assert run_id in clear_data["removed_run_ids"]
        assert clear_data["remaining_runs"] == 0
        assert not app.state.autopilot_manager._night_semaphore.locked()

        list_after = (await client.get("/v1/autopilot/runs", headers=headers)).json()
        assert list_after["runs"] == []
        assert list_after["active_count"] == 0

        # 1. ana Max hesabı sapasağlam açık kalmalı
        slots_resp = await client.get("/v1/autopilot/upstream-slots", headers=headers)
        assert slots_resp.status_code == 200
        assert slots_resp.json()["active_slots"] == [1]


def test_startup_upstream_curl_mobile_ansi_c_quoting_preserves_recaptcha_token() -> None:
    """Render Environment -> `UPSTREAM_CURL` içine mobil Lemur `--data-raw $'...'`
    cURL'ü yapıştırıldığında `create_app()` ön normalizasyonu sayesinde `recaptchaV3Token`
    kayıpsız okunmalıdır (`curl_import.py`'ye dokunmadan).
    """
    supa_cookie = _make_fake_supabase_cookie("mobile-render-env-1")
    mobile_max_curl = (
        "curl 'https://target.example/nextjs-api/stream/create-evaluation' \\\n"
        f"  -H 'cookie: {supa_cookie}' \\\n"
        "  -H 'user-agent: Mozilla/5.0 (Linux; Android 10; K) Mobile Safari/537.36' \\\n"
        "  -H 'referer: https://target.example/c/123' \\\n"
        "  --data-raw $'{\"id\":\"chat-1\",\"mode\":\"direct\",\"modelAId\":\"m-1\","
        "\"userMessage\":\"merhaba\\ndunya\",\"recaptchaV3Token\":\"mobile_recaptcha_ok_777\"}'"
    )
    settings = Settings(api_keys=["sk-test"], upstream_curl=mobile_max_curl)
    app = create_app(settings)
    assert app.state.settings.recaptcha_static_token == "mobile_recaptcha_ok_777"
    assert app.state.settings.target_domain == "target.example"


def test_lemur_eruda_curl_with_markdown_artifacts_and_vercel_connector_preserved() -> None:
    """Lemur Browser mobil DevTools (`curl --url '...'`, boş satırlı veya Markdown `\\_`,
    `\\[`, `\\]`, `&amp;` kaçışlı) cURL çıktısı, `enabledConnectors: ["vercel"]` ve
    `refresh_token` varlığıyla birlikte kayıpsız çözülmelidir.
    """
    from app.autopilot.curl_parse import inspect_agent_cookie_health

    hdr = base64.urlsafe_b64encode(b'{"alg":"ES256","typ":"JWT"}').decode().rstrip("=")
    pld = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "lemur-real-shape-sub", "exp": 1895289179}).encode()
        )
        .decode()
        .rstrip("=")
    )
    jwt_tok = f"{hdr}.{pld}.sig"
    sess_json = json.dumps(
        {
            "access_token": jwt_tok,
            "token_type": "bearer",
            "expires_in": 1200,
            "refresh_token": "synth_refresh_tok_123",
        }
    )
    b64_val = "base64-" + base64.urlsafe_b64encode(sess_json.encode()).decode().rstrip("=")
    mid = len(b64_val) // 2
    c0, c1 = b64_val[:mid], b64_val[mid:]
    chat_id = "01a1ff8c-6fc1-7fc5-ae74-a206595a1300"

    eruda_md_curl = (
        f"curl --url 'https://target.example/api/chat/{chat_id}/input'   \n\n"
        " -H 'accept: */*'   \n\n"
        " -b 'arena\\_device\\_id=dev1; "
        f"arena-auth-prod-v1.0={c0}; arena-auth-prod-v1.1={c1}; "
        "cf\\_clearance=cf1; \\_\\_cf\\_bm=bm1; "
        "\\_dd\\_s=aid=1&amp;rum=2&amp;id=2&amp;logs=1'   \n\n"
        ' -H \'sec-ch-ua: "Not A(Brand";v="99", "Chromium";v="154", '
        '"Microsoft Edge Simulate";v="154", "Lemur";v="154"\'   \n\n'
        " -H 'sec-ch-ua-mobile: ?1'   \n\n"
        ' -H \'sec-ch-ua-platform: "Android"\'   \n\n'
        " --data-raw '{\"kind\":\"message\",\"payload\":{\"message\":{\"parts\":"
        r'\[{"type":"text","text":"Tamam"}\],'
        "\"id\":\"01a1ff8c-bd28-7a14-b239-75c096d97a83\",\"role\":\"user\","
        "\"metadata\":{\"lastUserMessageId\":\"01a1ff8c-2c6c-7df1-8f55-e69710dfaadc\"}},"
        f"\"chatId\":\"agent-runner:{chat_id}\",\"trigger\":\"submit-message\","
        "\"metadata\":{\"timezone\":\"Europe/Istanbul\","
        r'"enabledConnectors":\["vercel"\],'
        r'"submissionSource":"chat\_input",'
        "\"cx\":\"AiBuibR\",\"recaptchaV3Token\":\"rc_lemur_ok\"}}}'"
    )

    cfg = parse_agent_curl(eruda_md_curl)
    assert cfg.chat_id == chat_id
    assert cfg.account_identity() == "lemur-real-shape-sub"
    assert cfg.metadata_template.get("enabledConnectors") == ["vercel"]
    assert cfg.submission_source == "chat_input"
    assert cfg.warnings == []

    diag = inspect_agent_cookie_health(cfg.cookie, cfg.access_token)
    assert diag["has_refresh_token"] is True
    assert diag["incomplete_auth_chunks"] is False







# ---------------------------------------------------------------------------
# 7. Süreklilik: tur tavanı uzatma, geçici hata yeniden denemesi, arşivden devam
# ---------------------------------------------------------------------------


class _FlakyAgent:
    """İlk N turda geçici hata fırlatan, sonra normal yanıt veren sahte istemci."""

    def __init__(self, failures: int) -> None:
        from app.autopilot.events_parser import AgentMessage
        from app.upstream.exceptions import UpstreamTimeout

        self._message_cls = AgentMessage
        self._timeout_cls = UpstreamTimeout
        self.remaining = failures
        self.calls = 0
        self.config = SimpleNamespace(chat_id="chat-continuity", target_domain="proje.test")

    def account_identity(self) -> str:
        return "agent-flaky-sub"

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def connect(self, **kwargs: Any) -> Any:
        return SimpleNamespace(last_assistant_message=None, has_pending_turn=False)

    async def send_and_wait(self, text: str, *, on_delta: Any = None) -> Any:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self._timeout_cls("olay akışı koptu", url="https://agent.test/stream")
        return self._message_cls(
            id=f"m{self.calls}",
            role="assistant",
            text="Sunucu 0.0.0.0:8000 üzerinde çalışıyor; GET / 200 döndü.",
            tools=[],
        )


@pytest.mark.anyio
async def test_transient_agent_error_is_retried_then_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ağ hatası koşuyu öldürmemeli: tur yeniden denenip akış sürmeli."""
    import app.autopilot.orchestrator as orch_mod

    # Bekleme sürelerini testte gerçekten uyumayalım:
    async def _no_wait(_self: Any, _seconds: float) -> bool:
        return True

    monkeypatch.setattr(AutopilotOrchestrator, "_sleep_interruptible", _no_wait)
    assert orch_mod.MAX_TRANSIENT_TURN_RETRIES >= 1

    bridge = _FakeMaxBridge(
        [
            # 1. tur talimatı
            "<DURUM>DEVAM</DURUM><MESAJ>Sunucuyu kur ve çalıştır.</MESAJ>"
            "<HAFIZA>Sunucu kuruldu.</HAFIZA>",
            # 1. tur değerlendirmesi: bitmedi
            "<DURUM>DEVAM</DURUM><MESAJ>Arayüzü ekle.</MESAJ><HAFIZA>Arayüz eklendi.</HAFIZA>",
            "<DURUM>TAMAMLANDI</DURUM><DEGERLENDIRME>Bitti.</DEGERLENDIRME>",
        ],
        sub="max-flaky-sub",
    )
    agent = _FlakyAgent(failures=2)
    cfg = AutopilotConfig(
        project_idea="Geçici hata dayanıklılığı",
        max_turns=2,
        gap_min_seconds=0.0,
        gap_max_seconds=0.0,
        state_file=tmp_path / "flaky.json",
    )
    orch = AutopilotOrchestrator(config=cfg, agent=agent, max_bridge=bridge)  # type: ignore[arg-type]
    state = await orch.run()

    # 2 hata + 1 başarılı tur: yeniden deneme gerçekten devreye girdi.
    assert agent.calls >= 3
    assert state.status == "completed"
    assert state.turns and state.turns[0].agent_reply_text


@pytest.mark.anyio
async def test_non_transient_error_pauses_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Geçici **olmayan** hata (kimlik reddi) yeniden denenmez; koşu /devam'a açık kalır."""
    from app.upstream.exceptions import UpstreamAuthRejected

    class _AuthFailAgent:
        config = SimpleNamespace(chat_id="chat-auth", target_domain="proje.test")
        calls = 0

        def account_identity(self) -> str:
            return "agent-auth-sub"

        async def startup(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

        async def connect(self, **kwargs: Any) -> Any:
            return SimpleNamespace(last_assistant_message=None, has_pending_turn=False)

        async def send_and_wait(self, text: str, *, on_delta: Any = None) -> Any:
            type(self).calls += 1
            raise UpstreamAuthRejected("401 yetkisiz", status_code=401)

    async def _no_wait(_self: Any, _seconds: float) -> bool:
        return True

    monkeypatch.setattr(AutopilotOrchestrator, "_sleep_interruptible", _no_wait)

    bridge = _FakeMaxBridge(
        ["<DURUM>DEVAM</DURUM><MESAJ>Başla.</MESAJ><HAFIZA>Başladı.</HAFIZA>"],
        sub="max-auth-sub",
    )
    cfg = AutopilotConfig(
        project_idea="Kimlik hatası",
        max_turns=2,
        gap_min_seconds=0.0,
        gap_max_seconds=0.0,
        state_file=tmp_path / "auth.json",
    )
    orch = AutopilotOrchestrator(
        config=cfg, agent=_AuthFailAgent(), max_bridge=bridge  # type: ignore[arg-type]
    )
    state = await orch.run()

    assert _AuthFailAgent.calls == 1  # tek deneme, boşa dövme yok
    assert state.status == "stopped"  # "failed" değil: /devam ile sürer
    assert "/devam" in state.stop_reason


def test_extend_turns_respects_lifetime_cap() -> None:
    """Tur uzatma sınırsız değil: ömür boyu tavan aşılamaz (sonsuz döngü koruması)."""
    from app.autopilot.orchestrator import MAX_TURN_EXTENSIONS

    cfg = AutopilotConfig(project_idea="Uzatma", max_turns=4)
    assert cfg.can_extend_turns() is True
    grown = cfg.extend_turns(MAX_TURN_EXTENSIONS)
    assert grown == 4 + MAX_TURN_EXTENSIONS
    assert cfg.turn_extensions == MAX_TURN_EXTENSIONS
    assert cfg.can_extend_turns() is False
    assert cfg.extend_turns(2) == grown  # tavan aşılmıyor


def test_autopilot_run_state_survives_disk_roundtrip(tmp_path: Path) -> None:
    """Koşu durumu diske yazılıp geri okunabilmeli (süreç yeniden başlasa da sürsün)."""
    state = AutopilotRunState(
        run_id="run-persist",
        chat_id="chat-persist",
        project_idea="Kalıcılık",
        max_turns=6,
    )
    state.status = "stopped"
    state.turn_extensions = 2
    state.stuck_recoveries = 1
    state.memory_summary = "Sunucu ayakta."
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")

    restored = AutopilotRunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert restored.run_id == "run-persist"
    assert restored.turn_extensions == 2
    assert restored.stuck_recoveries == 1
    assert restored.memory_summary == "Sunucu ayakta."


# ---------------------------------------------------------------------------
# 8. Okunabilirlik + tekrar yasağı: 1.5 saatlik tur, "ne oldu / ne kaldı"
# ---------------------------------------------------------------------------


def _roadmap_memory() -> str:
    return (
        "- [x] iskelet kuruldu · test exit_code=0\n"
        "- [x] bütçe grafiği · canlı 200\n"
        "- [ ] hedef takibi ekranı · mobil uyumlu\n"
        "- [ ] bildirimler · uçtan uca test"
    )


def test_turn_prompt_has_done_ledger_and_single_next_step() -> None:
    """`max` durumsuz: biten işler açıkça yazılmalı ki aynı işi tekrar istemesin."""
    prompt = build_turn_user_prompt(
        project_idea="Kişisel finans",
        turn=3,
        max_turns=8,
        current_phase="3_IMZA_OZELLIKLER",
        memory_summary=_roadmap_memory(),
        turn_digests=[],
        last_sent_to_agent="Bütçe grafiği ekle",
        agent_reply_text="Grafik hazır.",
        sandbox_evidence="bash: npm test → exit_code=0",
        completed_turns=2,
        min_turns=4,
        planned_turns=8,
    )
    assert "[ŞİMDİYE KADAR YAPILDI — TEKRAR İSTEME]" in prompt
    assert "✓ iskelet kuruldu" in prompt
    assert "✓ bütçe grafiği" in prompt
    # Biten iş "sıradaki" bölümünde tekrar edilmemeli.
    next_block = prompt.split("[SIRADAKİ", 1)[1].split("[SON TURLAR]", 1)[0]
    assert "iskelet kuruldu" not in next_block
    assert "hedef takibi ekranı" in next_block


def test_turn_prompt_states_scale_without_hard_limit() -> None:
    """Tur ölçeği yazılmalı ama **sınır** olarak değil (sistem sürekli olmalı)."""
    from app.autopilot.prompts import TURN_BUDGET_MINUTES, TURN_BUDGET_SOFT_MINUTES

    assert TURN_BUDGET_MINUTES == 90
    assert TURN_BUDGET_SOFT_MINUTES < TURN_BUDGET_MINUTES
    prompt = build_turn_user_prompt(
        project_idea="Kişisel finans",
        turn=2,
        max_turns=8,
        current_phase="2_TEMEL",
        memory_summary=_roadmap_memory(),
        turn_digests=[],
        last_sent_to_agent="x",
        agent_reply_text="y",
        sandbox_evidence="z",
        completed_turns=1,
        min_turns=4,
        planned_turns=8,
    )
    assert "TUR ÖLÇEĞİ" in prompt
    assert "60-90 dk" in prompt
    assert "üst sınır değil" in prompt
    assert "böl" in prompt  # büyük işi parçalama ÖNERİSİ
    assert "turu uzat, kesme" in prompt


def test_kickoff_plan_is_forced_to_five_named_lines() -> None:
    """Plan 5 etiketli satıra zorlanmalı: 'ne olacağım' tek bakışta anlaşılsın."""
    kickoff = build_kickoff_user_prompt(project_idea="Kişisel finans", max_turns=8)
    for token in ("AMAC:", "OZELLIK:", "MIMARI:", "ILK_TUR:", "TUR_SAYISI:"):
        assert token in kickoff
    assert "en fazla 4 madde" in kickoff
    assert "- [ ] aşama adı · kabul ölçütü" in kickoff


def test_compact_plan_limits_evening_plan_message() -> None:
    """Uzun plan mesajı 6 satıra/620 karaktere indirilmeli."""
    from app.autopilot.orchestrator import PLAN_MAX_LINES, _compact_plan

    long_plan = "\n".join(f"SATIR {i} " + ("x" * 200) for i in range(20))
    compact = _compact_plan(long_plan)
    assert compact.count("\n") + 1 <= PLAN_MAX_LINES
    assert len(compact) <= 700


@pytest.mark.anyio
async def test_turn_start_and_heartbeat_lines_explain_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tur başında ve sürerken 'ne oldu / ne kadar kaldı' satırı üretilmeli."""
    import app.autopilot.orchestrator as orch_mod

    alerts: list[str] = []

    class _Alerter:
        enabled = True

        async def send(self, text: str, label: str = "") -> None:
            alerts.append(text)

    async def _no_wait(_self: Any, _seconds: float) -> bool:
        return True

    monkeypatch.setattr(AutopilotOrchestrator, "_sleep_interruptible", _no_wait)
    # Heartbeat'i testte hemen tetikle:
    monkeypatch.setattr(orch_mod, "TURN_HEARTBEAT_SECONDS", 0.0)

    bridge = _FakeMaxBridge(
        [
            "<DURUM>DEVAM</DURUM><MESAJ>Sunucuyu kur.</MESAJ><HAFIZA>- [ ] sunucu</HAFIZA>",
            "<DURUM>DEVAM</DURUM><MESAJ>Testi yaz.</MESAJ><HAFIZA>- [ ] test</HAFIZA>",
            "<DURUM>TAMAMLANDI</DURUM><DEGERLENDIRME>Bitti.</DEGERLENDIRME>",
        ],
        sub="max-readable-sub",
    )
    agent = _FlakyAgent(failures=0)
    cfg = AutopilotConfig(
        project_idea="Okunabilirlik",
        max_turns=2,
        gap_min_seconds=0.0,
        gap_max_seconds=0.0,
        state_file=tmp_path / "readable.json",
    )
    orch = AutopilotOrchestrator(
        config=cfg, agent=agent, max_bridge=bridge, alerter=_Alerter()  # type: ignore[arg-type]
    )
    st = await orch.run()

    assert st.status == "completed"
    joined = "\n".join(alerts)
    assert "▶️ Tur 1 başladı" in joined
    assert "🎯 Bu turun işi:" in joined
    # Tur sürerken kalan süre bilgisi (heartbeat kapalıysa da başlangıç satırında var):
    assert "dk'da bir durum güncellemesi" in joined or "kaldı" in joined


# ---------------------------------------------------------------------------
# 9. "Sınır koymayın" sözleşmesi: tur süreyle kesilmez, plan kilit değildir
# ---------------------------------------------------------------------------


def test_turn_budget_is_advisory_never_a_timeout() -> None:
    """Tur ölçeği hiçbir yerde zaman aşımına dönüşmemeli.

    Kullanıcı sözleşmesi: "o bir süre sınırı değil, değil mi? sistem sürekli
    olmalı". Bu test, ileride birinin `turn_timeout`'u bu bütçeye bağlamasını
    engeller (bağlanırsa tur 90. dakikada kesilir ve süreklilik ölür).
    """
    from app.autopilot.agent_client import AgentSessionClient
    from app.autopilot.curl_parse import parse_agent_curl

    curl = _build_windows_cmd_input_curl(sub="agent-timeout-contract")
    client = AgentSessionClient.from_curl(curl)
    assert client.turn_timeout is None
    # Bütçe sabitleri yalnız metin/raporlama içindir; istemciye sızmamalı.
    assert "turn_timeout" not in (AgentSessionClient.from_curl.__doc__ or "")
    assert parse_agent_curl(curl) is not None


def test_next_step_block_is_guidance_not_a_lock() -> None:
    """Yol haritası maddesi kilit değil rehber olmalı; gerekçeli sapma serbest."""
    prompt = build_turn_user_prompt(
        project_idea="Kişisel finans",
        turn=4,
        max_turns=8,
        current_phase="3_IMZA",
        memory_summary="- [ ] hedef takibi\n- [ ] bildirim",
        turn_digests=[],
        last_sent_to_agent="x",
        agent_reply_text="y",
        sandbox_evidence="z",
        completed_turns=3,
        min_turns=4,
        planned_turns=8,
    )
    block = prompt.split("[SIRADAKİ", 1)[1].split("[SON TURLAR]", 1)[0]
    assert "rehber — daha iyisi varsa gerekçesiyle değiştir" in block
    assert "kilit değil" in block
    assert "gerekçesiz sapma yasak" in block  # serbest ama gerekçeli


def test_strict_plan_mode_is_opt_in_only() -> None:
    """Katı plan modu varsayılan KAPALI olmalı (yeni fikirlere yer kalsın)."""
    common: dict[str, Any] = {
        "project_idea": "X",
        "turn": 2,
        "max_turns": 8,
        "current_phase": "2",
        "memory_summary": "- [ ] tek madde",
        "turn_digests": [],
        "last_sent_to_agent": "a",
        "agent_reply_text": "b",
        "sandbox_evidence": "c",
        "completed_turns": 1,
        "min_turns": 4,
    }
    loose = build_turn_user_prompt(**common)
    strict = build_turn_user_prompt(**common, strict_plan=True)
    assert "kilit değil" in loose
    assert "kilit değil" not in strict
    assert "- tek madde" in strict
