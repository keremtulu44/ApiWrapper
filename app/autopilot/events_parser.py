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
    stdout_preview = _truncate(str(out.get("stdout") or ""), 200)
    stderr_preview = _truncate(str(out.get("stderr") or ""), 200)

    summary_parts: list[str] = []
    if tool_name == "bash":
        cmd = _truncate(str(inp.get("command") or ""), 160)
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
        cmd = _truncate(str(inp.get("command") or ""), 120)
        summary_parts.append(f"name={name!r} cmd=`{cmd}`")
        if "ports" in out:
            summary_parts.append(f"ports={out.get('ports')}")
        if out.get("warnings"):
            summary_parts.append(f"warnings={_truncate(str(out.get('warnings')), 120)}")
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
    )
    is_interactive = tool_name in INTERACTIVE_TOOLS

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


def format_sandbox_evidence(message: AgentMessage) -> str:
    """Asistan mesajındaki gerçek araç kayıtlarından `max` için kanıt raporu üretir."""
    if not message.tools:
        return (
            "DİKKAT — SANDBOX KANITI: Ajan bu turda HİÇBİR araç (dosya yazma, düzenleme, "
            "bash/test komutu veya sunucu başlatma) çalıştırmadı; yalnızca düz metin "
            "yanıtı üretti. Eğer kod değiştirdiğini veya test çalıştırdığını iddia "
            "ediyorsa bu iddia gerçek değildir."
        )

    writes = [t for t in message.tools if t.tool_name in {"write_file", "edit_file"}]
    reads = [t for t in message.tools if t.tool_name == "read_file"]
    bashes = [t for t in message.tools if t.tool_name == "bash"]
    procs = [t for t in message.tools if t.tool_name in {"start_process", "get_process_output"}]
    errors = [t for t in message.tools if t.is_error]
    blockers = [t for t in message.tools if t.is_interactive_blocker]

    lines: list[str] = [
        f"Toplam araç çağrısı: {len(message.tools)} "
        f"(dosya yazma/düzenleme={len(writes)}, okuma={len(reads)}, "
        f"bash={len(bashes)}, süreç/sunucu={len(procs)}, hatalı={len(errors)})"
    ]
    if message.duration_seconds is not None:
        lines.append(f"Tur çalışma süresi: {message.duration_seconds:.1f} sn")

    if blockers:
        names = ", ".join(sorted({t.tool_name for t in blockers}))
        lines.append(
            f"⚠️ UYARI: Ajan kullanıcı etkileşimi bekleyen araç çağırdı ({names})!"
        )

    if writes:
        written_paths: list[str] = []
        for item in writes:
            if item.summary and item.summary not in written_paths:
                written_paths.append(f"{item.tool_name}({item.summary})")
        lines.append("Değiştirilen dosyalar: " + ", ".join(written_paths[:12]))
        if len(written_paths) > 12:
            lines.append(f"  … ve {len(written_paths) - 12} dosya işlemi daha.")

    if bashes:
        lines.append("Çalıştırılan bash komutları (son 8):")
        for item in bashes[-8:]:
            flag = "❌" if item.is_error else "✅"
            detail = f"  {flag} {item.summary}"
            if item.stdout_preview:
                detail += f" | stdout: {item.stdout_preview}"
            if item.stderr_preview and item.is_error:
                detail += f" | stderr: {item.stderr_preview}"
            lines.append(detail)
    else:
        lines.append(
            "NOT: Bu turda hiç bash/test/build komutu çalıştırılmadı "
            "(yalnız dosya/diğer araçlar kullanıldı)."
        )

    if procs:
        lines.append("Arka plan süreç / canlı önizleme işlemleri:")
        for item in procs[-4:]:
            lines.append(f"  • {item.tool_name}: {item.summary}")

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
