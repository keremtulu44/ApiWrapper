"""Agent Mode `/events` (SSE/S2 batch) ve `?recoverRun=false` durum/kanıt çözücüsü.

Agent Mode iki tamamlayıcı okuma yüzeyi sunar:

1. ``GET /api/chat/{chat_id}/events``:
   SSE üzerinden ``event: batch`` / ``event: ping`` kayıtları akıtır. Her ``batch``
   içindeki ``records`` dizisinde:
   * ``seq_num``: akışın kaldığı sıra numarası (``last-event-id`` için),
   * ``body``: JSON kodlu olay (``start``, ``text-delta``, ``tool-input-available``,
     ``tool-output-available``, ``finish``, ``error``),
   * ``headers``: ``[["trigger-control", "turn-complete"]]`` ile turun kesin bitişi.

2. ``GET /api/chat/{chat_id}?recoverRun=false``:
   Sohbetin tam anlık görüntüsünü (``messages``, ``lastEventId``, ``triggerAddress``)
   verir. Asistan mesajlarının ``parts`` dizisi hem nihai metni hem de ajanın
   sandbox'ta **gerçekten** çalıştırdığı araçların (``tool-bash``, ``tool-write_file``,
   ``tool-edit_file`` vb.) girdi ve çıktılarını taşır. Bu modül o parçalardan
   durumsuz ``max``'ın "laf kalabalığı" ile "gerçek çalışan kod"u ayırt etmesini
   sağlayan objektif **Sandbox Eylem Kanıtı** özetini üretir.
"""

from __future__ import annotations

import codecs
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: Ajanın kullanıcı etkileşimi bekleyip akışı duraklatabileceği araç adları.
INTERACTIVE_TOOLS = frozenset({"ask_user", "add_voice"})

#: DevTools EventStream sekmesinden düz metin kopyalandığında satır biçimi:
#: `<id>batch{...}<saat>` veya `<id>ping{...}<saat>`
_DEVTOOLS_RECORD_RE = re.compile(
    r"(?:^|\d{2}:\d{2}:\d{2}\.\d{3})(?P<eid>[\d,]+)(?P<etype>batch|ping)(?P<data>\{.*?\})"
    r"(?=(?:\d{2}:\d{2}:\d{2}\.\d{3})|$)",
    re.DOTALL,
)


def _truncate(text: str, limit: int = 240) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _truncate_head_tail(text: str, limit: int = 380) -> str:
    """Komut çıktılarının hem başını hem de asıl sonucun yazdığı sonunu korur."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    if limit < 40:
        return _truncate(cleaned, limit)
    head_len = max(80, int(limit * 0.36))
    tail_len = max(100, limit - head_len - 5)
    return f"{cleaned[:head_len].rstrip()} … {cleaned[-tail_len:].lstrip()}"


@dataclass(slots=True)
class ToolExecutionEvidence:
    """Ajanın sandbox içinde gerçekten çalıştırdığı tek bir aracın kanıt özeti."""

    tool_name: str
    tool_call_id: str = ""
    state: str = ""
    summary: str = ""
    exit_code: int | None = None
    status: str = ""
    stdout_preview: str = ""
    stderr_preview: str = ""
    is_error: bool = False
    is_interactive_blocker: bool = False


@dataclass(slots=True)
class AgentMessage:
    """`?recoverRun=false` yanıtındaki tek bir sohbet mesajı."""

    id: str
    role: str
    text: str
    pending: bool = False
    requires_review: bool = False
    manifest_node_id: str | None = None
    started_at: int | None = None
    completed_at: int | None = None
    tools: list[ToolExecutionEvidence] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.completed_at and self.completed_at >= self.started_at:
            return round((self.completed_at - self.started_at) / 1000.0, 2)
        return None


@dataclass(slots=True)
class AgentChatState:
    """`GET /api/chat/{chat_id}?recoverRun=false` yanıtının ayrıştırılmış özeti."""

    chat_id: str
    trigger_address: str
    last_event_id: str
    has_trigger_session: bool
    messages: list[AgentMessage] = field(default_factory=list)

    @property
    def last_user_message(self) -> AgentMessage | None:
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg
        return None

    @property
    def last_user_message_id(self) -> str:
        msg = self.last_user_message
        return msg.id if msg else ""

    @property
    def last_assistant_message(self) -> AgentMessage | None:
        for msg in reversed(self.messages):
            if msg.role == "assistant":
                return msg
        return None

    @property
    def has_pending_turn(self) -> bool:
        if not self.messages:
            return False
        tail = self.messages[-1]
        if tail.role == "user":
            # Son mesaj kullanıcıya aitse ajan henüz cevabı bitirmemiş olabilir.
            return tail.pending
        return tail.pending


@dataclass(slots=True)
class AgentStreamDelta:
    """`/events` akışından çözülen tekil olay."""

    # "ping" | "start" | "text" | "tool_input" | "tool_output"
    # | "finish" | "turn_complete" | "error" | "other"
    kind: str
    seq_num: int | None = None
    text: str = ""
    message_id: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    raw_data: dict[str, Any] | None = None


def _as_str_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _summarize_tool_part(part: dict[str, Any]) -> ToolExecutionEvidence:
    raw_type = str(part.get("type") or "")
    tool_name = (
        raw_type[5:]
        if raw_type.startswith("tool-")
        else str(part.get("toolName") or raw_type)
    )
    tool_call_id = str(part.get("toolCallId") or "")
    state = str(part.get("state") or "")
    inp = _as_str_dict(part.get("input"))
    out = _as_str_dict(part.get("output"))

    exit_code: int | None = None
    raw_exit = out.get("exit_code")
    if isinstance(raw_exit, int):
        exit_code = raw_exit

    status = str(out.get("status") or "")
    stdout_preview = _truncate_head_tail(str(out.get("stdout") or ""), 380)
    stderr_preview = _truncate_head_tail(str(out.get("stderr") or ""), 320)
    has_preview_warning = False

    summary_parts: list[str] = []
    if tool_name == "bash":
        cmd = _truncate(str(inp.get("command") or ""), 180)
        summary_parts.append(f"cmd=`{cmd}`" if cmd else "cmd=(boş)")
        if exit_code is not None:
            summary_parts.append(f"exit_code={exit_code}")
        elif status:
            summary_parts.append(f"status={status}")
    elif tool_name in {"write_file", "edit_file", "read_file", "present_file"}:
        path = str(inp.get("path") or "")
        summary_parts.append(f"path=`{path}`" if path else "path=(belirsiz)")
    elif tool_name == "start_process":
        name = str(inp.get("name") or "")
        cmd = _truncate(str(inp.get("command") or ""), 140)
        summary_parts.append(f"name={name!r} cmd=`{cmd}`")
        if "alive" in out:
            summary_parts.append(f"alive={out.get('alive')}")
        if "ports" in out:
            summary_parts.append(f"ports={out.get('ports')}")
        if out.get("warnings"):
            has_preview_warning = True
            summary_parts.append(
                f"PREVIEW_UYARISI={_truncate_head_tail(str(out.get('warnings')), 220)}"
            )
    elif tool_name == "get_process_output":
        proc_id = str(inp.get("process_id") or "")
        if proc_id:
            summary_parts.append(f"process_id={proc_id!r}")
        if "alive" in out:
            summary_parts.append(f"alive={out.get('alive')}")
        if "ports" in out:
            summary_parts.append(f"ports={out.get('ports')}")
        if out.get("wait_result"):
            summary_parts.append(f"wait_result={out.get('wait_result')}")
        raw_log = str(out.get("log") or out.get("output") or "")
        if raw_log.strip():
            summary_parts.append(f"log_sonu={_truncate_head_tail(raw_log, 220)}")
    elif tool_name == "web_search":
        query = _truncate(str(inp.get("query") or ""), 160)
        summary_parts.append(f"query={query!r}" if query else "query=(boş)")
        if inp.get("depth") is not None:
            summary_parts.append(f"depth={inp.get('depth')}")
    elif tool_name == "fetch_page":
        url = _truncate(str(inp.get("url") or ""), 180)
        summary_parts.append(f"url=`{url}`" if url else "url=(boş)")
    elif tool_name == "ask_user":
        raw_qs = inp.get("questions")
        q_texts: list[str] = []
        if isinstance(raw_qs, list):
            for q_item in raw_qs[:4]:
                if isinstance(q_item, dict) and q_item.get("question"):
                    q_texts.append(_truncate(str(q_item["question"]), 120))
        if q_texts:
            summary_parts.append("sorular=" + " | ".join(q_texts))
        else:
            summary_parts.append("kullanıcıdan seçim bekliyor")
    else:
        if inp:
            keys = ", ".join(sorted(str(k) for k in inp)[:5])
            summary_parts.append(f"args=({keys})")
        if status:
            summary_parts.append(f"status={status}")

    is_error = (
        (exit_code is not None and exit_code != 0)
        or status in {"error", "failed", "timeout"}
        or "error" in state.lower()
        or has_preview_warning
    )
    is_interactive = tool_name in INTERACTIVE_TOOLS or (
        tool_name == "generate_image" and bool(inp.get("offer_options"))
    )

    return ToolExecutionEvidence(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        state=state,
        summary=" ".join(summary_parts),
        exit_code=exit_code,
        status=status,
        stdout_preview=stdout_preview,
        stderr_preview=stderr_preview,
        is_error=is_error,
        is_interactive_blocker=is_interactive,
    )


def parse_chat_state(raw: dict[str, Any]) -> AgentChatState:
    """`GET /api/chat/{chat_id}?recoverRun=false` JSON gövdesini ayrıştırır."""
    chat_id = str(raw.get("id") or "").strip()
    trigger_address = str(raw.get("triggerAddress") or "").strip()
    if not trigger_address and chat_id:
        trigger_address = f"agent-runner:{chat_id}"
    last_event_id = str(raw.get("lastEventId") or "").strip()
    has_trigger_session = bool(raw.get("hasTriggerSession", True))

    messages: list[AgentMessage] = []
    raw_messages = raw.get("messages")
    if isinstance(raw_messages, list):
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("id") or "").strip()
            role = str(item.get("role") or "").strip()
            meta = _as_str_dict(item.get("metadata"))
            pending = bool(meta.get("pending", False))
            requires_review = bool(meta.get("requiresReview", False))
            manifest_node = meta.get("manifestNodeId")
            manifest_node_id = str(manifest_node) if isinstance(manifest_node, str) else None

            timing = _as_str_dict(meta.get("timing"))
            raw_started = timing.get("startedAt")
            started_at = raw_started if isinstance(raw_started, int) else None
            raw_completed = timing.get("completedAt")
            completed_at = raw_completed if isinstance(raw_completed, int) else None

            text_chunks: list[str] = []
            tools: list[ToolExecutionEvidence] = []
            parts = item.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    ptype = str(part.get("type") or "")
                    if ptype == "text":
                        txt = part.get("text")
                        if isinstance(txt, str) and txt:
                            text_chunks.append(txt)
                    elif ptype.startswith("tool-"):
                        tools.append(_summarize_tool_part(part))

            messages.append(
                AgentMessage(
                    id=msg_id,
                    role=role,
                    text="\n\n".join(chunk for chunk in text_chunks if chunk).strip(),
                    pending=pending,
                    requires_review=requires_review,
                    manifest_node_id=manifest_node_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    tools=tools,
                )
            )

    return AgentChatState(
        chat_id=chat_id,
        trigger_address=trigger_address,
        last_event_id=last_event_id,
        has_trigger_session=has_trigger_session,
        messages=messages,
    )


def _clean_plain_note(text: str, limit: int = 140) -> str:
    """Kanıt notlarından kabuk/WAF tetikleyicilerini (`backtick`, `&&`, `.sh`) temizler."""
    cleaned = (text or "").replace("`", "").replace("&&", " ve ").replace("||", " veya ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def format_sandbox_evidence(message: AgentMessage) -> str:
    """Asistan mesajındaki araç kayıtlarından kısa, WAF-güvenli Türkçe karne üretir.

    Ham `bash` komutları, dosya yolları ve `stdout`/`stderr` dökümleri bilerek
    gönderilmez: hem istemi gereksiz şişirir hem de dış HTTP güvenlik duvarlarında
    (Cloudflare WAF RCE kuralları) yanlış pozitif `403 Blocked` üretebilir.
    """
    if not message.tools:
        return (
            "DİKKAT — SANDBOX KANITI: Ajan bu turda hiçbir araç çalıştırmadı; "
            "yalnızca düz metin yanıtı üretti. Kod değiştirdiğini veya test "
            "çalıştırdığını iddia ediyorsa bu iddia gerçek değildir."
        )

    writes = [t for t in message.tools if t.tool_name in {"write_file", "edit_file"}]
    reads = [t for t in message.tools if t.tool_name == "read_file"]
    bashes = [t for t in message.tools if t.tool_name == "bash"]
    bash_ok = [t for t in bashes if not t.is_error]
    bash_err = [t for t in bashes if t.is_error]
    procs = [t for t in message.tools if t.tool_name in {"start_process", "get_process_output"}]
    web_ops = [t for t in message.tools if t.tool_name in {"web_search", "fetch_page"}]
    errors = [t for t in message.tools if t.is_error]
    blockers = [t for t in message.tools if t.is_interactive_blocker]

    lines: list[str] = [
        f"Sandbox özeti: toplam {len(message.tools)} işlem "
        f"(dosya yazma/düzenleme={len(writes)}, okuma={len(reads)}, "
        f"komut/test={len(bash_ok)} başarılı / {len(bash_err)} hatalı, "
        f"sunucu={len(procs)}, web/doküman={len(web_ops)}, toplam hata={len(errors)})"
    ]
    if message.duration_seconds is not None:
        lines.append(f"Tur süresi: {message.duration_seconds:.0f} sn")

    if blockers:
        details = "; ".join(
            f"{t.tool_name}({_clean_plain_note(t.summary, 110)})"
            if t.summary
            else t.tool_name
            for t in blockers[:3]
        )
        lines.append(
            f"🚨 ETKİLEŞİM ENGELİ: Ajan kullanıcı onayı bekleyen pencere açtı "
            f"[{details}]! Net kararını söyle ve pencere açmadan uygulamasını iste."
        )

    if bashes:
        last_bash = bashes[-1]
        tail_state = (
            "başarılı (çıkış kodu 0)"
            if not last_bash.is_error
            else f"hatalı (çıkış kodu {last_bash.exit_code})"
        )
        lines.append(
            f"Komut/test sonucu: {len(bash_ok)}/{len(bashes)} komut hatasız geçti; "
            f"son çalıştırılan komut {tail_state}."
        )
    else:
        lines.append("NOT: Bu turda hiç derleme/test komutu çalıştırılmadı.")

    if procs:
        ports_found: list[str] = []
        alive_seen = False
        preview_warn = ""
        for item in procs:
            if "alive=True" in item.summary:
                alive_seen = True
            for p in re.findall(r"ports=\[([0-9,\s]+)\]", item.summary):
                for num in re.findall(r"\d{2,5}", p):
                    if num not in ports_found:
                        ports_found.append(num)
            if "PREVIEW_UYARISI=" in item.summary:
                raw_w = item.summary.split("PREVIEW_UYARISI=", 1)[1]
                preview_warn = _clean_plain_note(raw_w, 140)
        port_txt = f", portlar: {', '.join(ports_found)}" if ports_found else ""
        state_txt = "aktif" if alive_seen else "kontrol edildi"
        lines.append(f"Canlı önizleme sunucusu: {state_txt}{port_txt}.")
        if preview_warn:
            lines.append(f"UYARI — PREVIEW_UYARISI: {preview_warn}")

    if web_ops:
        topics: list[str] = []
        for item in web_ops[-4:]:
            topics.append(f"{item.tool_name}({_clean_plain_note(item.summary, 80)})")
        lines.append("Web/API araştırması: " + ", ".join(topics))

    return "\n".join(lines)


def _decode_batch_payload(event_type: str, data_text: str) -> list[AgentStreamDelta]:
    """Tek bir `batch` veya `ping` JSON gövdesinden olay listesi çıkarır."""
    try:
        outer = json.loads(data_text)
    except json.JSONDecodeError:
        return []
    if not isinstance(outer, dict):
        return []

    out: list[AgentStreamDelta] = []
    tail = outer.get("tail")
    tail_seq: int | None = None
    if isinstance(tail, dict) and isinstance(tail.get("seq_num"), int):
        # `tail.seq_num` bir SONRAKİ yazılacak sıra numarasıdır; son görülen `tail - 1`.
        tail_seq = max(0, tail["seq_num"] - 1)

    if event_type == "ping":
        out.append(AgentStreamDelta(kind="ping", seq_num=tail_seq))
        return out

    records = outer.get("records")
    if not isinstance(records, list):
        return out

    for rec in records:
        if not isinstance(rec, dict):
            continue
        seq = rec.get("seq_num") if isinstance(rec.get("seq_num"), int) else tail_seq

        # 1) Kontrol başlıkları: `[["trigger-control", "turn-complete"], ...]`
        rec_headers = rec.get("headers")
        if isinstance(rec_headers, list):
            header_pairs = [
                (str(pair[0]), str(pair[1]))
                for pair in rec_headers
                if isinstance(pair, list) and len(pair) >= 2
            ]
            if any(k == "trigger-control" and v == "turn-complete" for k, v in header_pairs):
                out.append(AgentStreamDelta(kind="turn_complete", seq_num=seq))
                continue
            if any(v == "trim" for _, v in header_pairs):
                out.append(AgentStreamDelta(kind="other", seq_num=seq))
                continue

        # 2) JSON kodlu `body`
        body_raw = rec.get("body")
        if not isinstance(body_raw, str) or not body_raw.strip():
            out.append(AgentStreamDelta(kind="other", seq_num=seq))
            continue
        try:
            body_obj = json.loads(body_raw)
        except json.JSONDecodeError:
            out.append(AgentStreamDelta(kind="other", seq_num=seq))
            continue
        if not isinstance(body_obj, dict):
            continue

        raw_inner = body_obj.get("data")
        inner = raw_inner if isinstance(raw_inner, dict) else body_obj
        dtype = str(inner.get("type") or "")

        if dtype == "start":
            out.append(
                AgentStreamDelta(
                    kind="start",
                    seq_num=seq,
                    message_id=str(inner.get("messageId") or ""),
                    raw_data=inner,
                )
            )
        elif dtype == "text-delta":
            out.append(
                AgentStreamDelta(
                    kind="text",
                    seq_num=seq,
                    text=str(inner.get("delta") or ""),
                    raw_data=inner,
                )
            )
        elif dtype in {"tool-input-available", "tool-input-start"}:
            out.append(
                AgentStreamDelta(
                    kind="tool_input",
                    seq_num=seq,
                    tool_name=str(inner.get("toolName") or ""),
                    tool_call_id=str(inner.get("toolCallId") or ""),
                    raw_data=inner,
                )
            )
        elif dtype == "tool-output-available":
            if not inner.get("preliminary", False):
                out.append(
                    AgentStreamDelta(
                        kind="tool_output",
                        seq_num=seq,
                        tool_call_id=str(inner.get("toolCallId") or ""),
                        raw_data=inner,
                    )
                )
            else:
                out.append(AgentStreamDelta(kind="other", seq_num=seq))
        elif dtype == "finish":
            out.append(AgentStreamDelta(kind="finish", seq_num=seq, raw_data=inner))
        elif dtype == "error":
            err_msg = str(inner.get("errorText") or inner.get("message") or inner)
            out.append(AgentStreamDelta(kind="error", seq_num=seq, text=err_msg, raw_data=inner))
        else:
            out.append(AgentStreamDelta(kind="other", seq_num=seq, raw_data=inner))

    return out


class AgentEventStreamDecoder:
    """`/api/chat/{id}/events` bayt akışını artımlı olarak `AgentStreamDelta`'ya çevirir."""

    def __init__(self) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._event_type = ""
        self._event_id = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> list[AgentStreamDelta]:
        if chunk:
            self._buffer += self._utf8.decode(chunk)
        return self._drain()

    def flush(self) -> list[AgentStreamDelta]:
        self._buffer += self._utf8.decode(b"", final=True)
        deltas = self._drain()
        if self._data_lines:
            deltas.extend(self._emit_current_event())
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder:
            deltas.extend(self._parse_fallback_blob(remainder))
        return deltas

    def _drain(self) -> list[AgentStreamDelta]:
        out: list[AgentStreamDelta] = []
        while True:
            nl = self._buffer.find("\n")
            if nl == -1:
                break
            line = self._buffer[:nl]
            self._buffer = self._buffer[nl + 1 :]
            if line.endswith("\r"):
                line = line[:-1]

            if line == "":
                if self._data_lines:
                    out.extend(self._emit_current_event())
                self._event_type = ""
                self._event_id = ""
                continue

            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                self._event_type = line[6:].strip()
                continue
            if line.startswith("id:"):
                self._event_id = line[3:].strip()
                continue
            if line.startswith("retry:"):
                continue
            if line.startswith("data:"):
                self._data_lines.append(line[5:].lstrip(" "))
                continue

            # Standart SSE dışında tek satırlık JSON veya DevTools dökümü gelirse:
            out.extend(self._parse_fallback_blob(line))
        return out

    def _emit_current_event(self) -> list[AgentStreamDelta]:
        data_text = "\n".join(self._data_lines)
        etype = self._event_type or ("ping" if '"records"' not in data_text else "batch")
        self._data_lines = []
        deltas = _decode_batch_payload(etype, data_text)
        if deltas:
            return deltas
        # Düz SSE JSON olayı yedeği
        try:
            obj = json.loads(data_text)
            if isinstance(obj, dict) and obj.get("type") == "finish":
                return [AgentStreamDelta(kind="finish", raw_data=obj)]
        except json.JSONDecodeError:
            pass
        return []

    @staticmethod
    def _parse_fallback_blob(text: str) -> list[AgentStreamDelta]:
        out: list[AgentStreamDelta] = []
        for match in _DEVTOOLS_RECORD_RE.finditer(text):
            out.extend(_decode_batch_payload(match.group("etype"), match.group("data")))
        return out
