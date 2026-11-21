"""Agent Mode cURL ayrıştırıcı, olay okuyucu, istemci ve otopilot testleri."""

from __future__ import annotations

import base64
import json
from pathlib import Path
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
    assert "write_file" in evidence_txt
    assert "src/app.py" in evidence_txt
    assert "pytest -q" in evidence_txt
    assert "exit_code=0" in evidence_txt
    assert "4 passed" in evidence_txt


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

    # Hem başlangıç hem de en sondaki '24 passed' satırı korunmalı
    assert "collecting test items" in evidence
    assert "24 passed in 0.42s" in evidence
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
    assert "pytest -k step_2" in bridge_part2.calls[1][1]


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
    assert any("🌆 Akşam Proje Planı Hazır" in msg for msg in immediate_alerts)
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
