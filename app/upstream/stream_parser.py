"""Vercel AI SDK "data stream" protokolü çözücüsü.

Protokol: her satır `<kod>:<json>` biçimindedir. Örnekler::

    f:{"messageId":"msg-123"}
    0:"Merhaba"
    0:" dünya"
    9:{"toolCallId":"c1","toolName":"search","args":{}}
    a:{"toolCallId":"c1","result":{}}
    e:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    d:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":4}}
    3:"upstream error text"

Ayrıştırıcı ayrıca SSE (`data: {...}`) ve düz metin yayınlarını da tolere eder,
böylece upstream protokol değiştirse bile metin akışı korunur.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.schemas.upstream import EventType, StreamEvent

logger = get_logger(__name__)

#: Kod → olay türü eşlemesi.
CODE_MAP: dict[str, EventType] = {
    "0": EventType.TEXT,
    "a0": EventType.TEXT,  # yeni AI SDK metin delta
    "f": EventType.START,
    "2": EventType.DATA,
    "8": EventType.MESSAGE_ANNOTATION,
    "9": EventType.TOOL_CALL,
    "a": EventType.TOOL_RESULT,
    "b": EventType.TOOL_CALL,
    "c": EventType.TOOL_CALL,
    "g": EventType.REASONING,
    "ag": EventType.REASONING,  # yeni AI SDK düşünme/akıl yürütme
    "i": EventType.REASONING,
    "j": EventType.REASONING,
    "3": EventType.ERROR,
    "e": EventType.STEP_FINISH,
    "d": EventType.FINISH,
    "ad": EventType.FINISH,  # yeni AI SDK metadata/bitiş
}

_FINISH_REASON_MAP = {
    "stop": "stop",
    "length": "length",
    "content-filter": "content_filter",
    "content_filter": "content_filter",
    "tool-calls": "tool_calls",
    "tool_calls": "tool_calls",
    "error": "stop",
    "other": "stop",
    "unknown": "stop",
}


def normalize_finish_reason(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _FINISH_REASON_MAP.get(value.strip().lower(), "stop")


def normalize_usage(raw: Any) -> dict[str, Any]:
    """Upstream usage sözlüğünü OpenAI alan adlarına çevirir."""
    if not isinstance(raw, dict):
        return {}
    prompt = raw.get("promptTokens", raw.get("prompt_tokens", raw.get("inputTokens")))
    completion = raw.get(
        "completionTokens", raw.get("completion_tokens", raw.get("outputTokens"))
    )
    total = raw.get("totalTokens", raw.get("total_tokens"))
    out: dict[str, Any] = {}
    if isinstance(prompt, (int, float)):
        out["prompt_tokens"] = int(prompt)
    if isinstance(completion, (int, float)):
        out["completion_tokens"] = int(completion)
    if isinstance(total, (int, float)):
        out["total_tokens"] = int(total)
    elif "prompt_tokens" in out or "completion_tokens" in out:
        out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("completion_tokens", 0)
    return out


def _extract_text(payload: Any) -> str:
    """Farklı biçimlerdeki metin taşıyıcılarından düz metni çıkarır."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "textDelta", "delta", "content", "value"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                # `delta: {"text": "..."}` gibi iç içe taşıyıcılar.
                nested = _extract_text(value)
                if nested:
                    return nested
        return ""
    if isinstance(payload, list):
        return "".join(_extract_text(item) for item in payload)
    return ""


def parse_line(line: str) -> StreamEvent | None:
    """Tek bir protokol satırını `StreamEvent`'e çevirir.

    Tanınmayan/boş satırlar için `None` döner.
    """
    line = line.strip()
    if not line:
        return None

    # SSE biçimi toleransı
    if line.startswith("data:"):
        line = line[5:].strip()
        if not line or line == "[DONE]":
            return StreamEvent(type=EventType.FINISH, raw_code="sse", finish_reason="stop")
        try:
            sse_payload = json.loads(line)
        except json.JSONDecodeError:
            # Bazı proxy/SSE katmanları Vercel data-stream satırını tekrar
            # `data:` içine sarar: `data: 0:"metin"`.
            if ":" in line and len(line.split(":", 1)[0].strip()) <= 2:
                nested = parse_line(line)
                if nested is not None:
                    return nested
            return StreamEvent(type=EventType.TEXT, text=line, raw_code="sse")
        return _from_sse_payload(sse_payload)

    if line.startswith((":", "event:", "id:", "retry:")):
        return None

    code, sep, rest = line.partition(":")
    if not sep or len(code) > 2:
        # Protokol dışı düz metin: içerik olarak kabul et.
        return StreamEvent(type=EventType.TEXT, text=line, raw_code="raw")

    code = code.strip()
    rest = rest.strip()
    try:
        payload: Any = json.loads(rest) if rest else None
    except json.JSONDecodeError:
        payload = rest

    etype = CODE_MAP.get(code, EventType.UNKNOWN)

    if etype is EventType.TEXT or etype is EventType.REASONING:
        return StreamEvent(type=etype, text=_extract_text(payload), raw_code=code, data=payload)

    if etype is EventType.ERROR:
        message = _extract_text(payload) or (
            json.dumps(payload, ensure_ascii=False) if payload is not None else "upstream error"
        )
        return StreamEvent(type=EventType.ERROR, text=message, raw_code=code, data=payload)

    if etype in (EventType.FINISH, EventType.STEP_FINISH):
        reason = None
        usage: dict[str, Any] = {}
        if isinstance(payload, dict):
            reason = normalize_finish_reason(payload.get("finishReason"))
            usage = normalize_usage(payload.get("usage"))
        return StreamEvent(
            type=etype,
            raw_code=code,
            data=payload,
            finish_reason=reason,
            usage=usage,
        )

    return StreamEvent(type=etype, raw_code=code, data=payload)


def _from_sse_payload(payload: Any) -> StreamEvent:
    """OpenAI benzeri SSE gövdesinden olay üretir (tolerans katmanı)."""
    if isinstance(payload, dict):
        if "error" in payload:
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            return StreamEvent(type=EventType.ERROR, text=str(message), raw_code="sse")
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") or choice.get("message") or {}
            text = delta.get("content") if isinstance(delta, dict) else ""
            reason = normalize_finish_reason(choice.get("finish_reason"))
            if reason:
                return StreamEvent(
                    type=EventType.FINISH,
                    text=text or "",
                    raw_code="sse",
                    finish_reason=reason,
                    usage=normalize_usage(payload.get("usage")),
                )
            return StreamEvent(type=EventType.TEXT, text=text or "", raw_code="sse")
    return StreamEvent(type=EventType.TEXT, text=_extract_text(payload), raw_code="sse")


class StreamDecoder:
    """Bayt parçalarını satırlara böler; çok baytlı UTF-8 sınırlarını korur."""

    def __init__(self) -> None:
        self._decoder = __import__("codecs").getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> list[str]:
        """Yeni baytları işler ve tamamlanmış satırları döndürür."""
        if chunk:
            self._buffer += self._decoder.decode(chunk)
        return self._drain_lines()

    def _drain_lines(self) -> list[str]:
        lines: list[str] = []
        while True:
            idx = self._buffer.find("\n")
            if idx == -1:
                break
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            if line.endswith("\r"):
                line = line[:-1]
            lines.append(line)
        return lines

    def flush(self) -> list[str]:
        """Akış bitince tamponda kalanları döndürür."""
        self._buffer += self._decoder.decode(b"", final=True)
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


async def parse_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[StreamEvent]:
    """Bayt akışını `StreamEvent` akışına dönüştürür."""
    decoder = StreamDecoder()
    async for chunk in chunks:
        for line in decoder.feed(chunk):
            event = parse_line(line)
            if event is not None:
                yield event
    for line in decoder.flush():
        event = parse_line(line)
        if event is not None:
            yield event


# --------------------------------------------------------------- biçim teşhisi
#: İstemciye **iletilmeyen** her parça: görsel/arama sonucu bunlardan birinde.
#: Akış yolunda istemciye giden tek şey `0:` metin delta'sıdır; görsel modunda
#: cevap metin olarak gelmiyorsa sonuç bu kodlardan birindedir. Hangi kodda ve
#: hangi alan adlarında geldiği görülmeden doğru ayrıştırma yazılamaz.
#:
#: `REASONING` başlangıçta dışarıdaydı ("düşünme delta'ları her akışta gelir,
#: logu gürültüye boğar"). 15 Kasım canlı ölçümü bu gerekçeyi boşa düşürdü:
#: `a2` (atıf: `[{type,organization}]`) **her** akışta geliyor ve satırı zaten
#: üretiyor — düz sohbette bile. Elimizde gürültü kazancı değil, yalnız kör
#: nokta kaldı: görsel `g:` içinde gelse görünmezdi. Kural artık basit: özet,
#: istemcinin görmediği **her şeyi** sayar.
MEDIA_CANDIDATE_PARTS = frozenset(
    {
        EventType.DATA,
        EventType.MESSAGE_ANNOTATION,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.REASONING,
        EventType.UNKNOWN,
    }
)

#: Keşif amaçlı özetin inme derinliği. Gerçek görsel parçaları iç içe gelir
#: (`{"parts":[{"type":"image","data":{…}}]}`); derinlik sığ olursa alan adları
#: hiç görünmez ve teşhis işe yaramaz.
_MAX_DESCRIBE_DEPTH = 4
_MAX_DESCRIBE_KEYS = 12


def _describe_value(value: Any, depth: int = 0) -> str:
    """Bir değerin **biçimini** yazar; değerinin kendisini asla yazmaz.

    Parça görsel verisi, oturum bilgisi ya da uzun bir base64 taşıyabilir.
    Bu yüzden log'a yalnız tür + uzunluk + alan adı yazılır (`str(18432)`,
    `dict(3){url:str(64),mimeType:str(9)}`), içerik yazılmaz.
    """
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"str({len(value)})"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, list):
        if not value or depth >= _MAX_DESCRIBE_DEPTH:
            return f"list({len(value)})"
        return f"list({len(value)})[{_describe_value(value[0], depth + 1)}]"
    if isinstance(value, dict):
        if depth >= _MAX_DESCRIBE_DEPTH:
            return f"dict({len(value)})"
        items = list(value.items())[:_MAX_DESCRIBE_KEYS]
        inner = ",".join(f"{k}:{_describe_value(v, depth + 1)}" for k, v in items)
        more = ",…" if len(value) > _MAX_DESCRIBE_KEYS else ""
        return f"dict({len(value)}){{{inner}{more}}}"
    return type(value).__name__


class PartShapeSummary:
    """Bir akıştaki metin-dışı parçaların kod başına biçim özeti.

    Tek seferde bir `logger.info` satırı üretir; akış başına sabit maliyet.
    Aynı kodun **farklı** biçimleri saklanır (tekrarlar yalnız sayacı artırır),
    bu yüzden uzun bir akışta log şişmez ama çeşitlilik de kaybolmaz.

    "Kod başına ilk örnek yeter" varsayımı yanlış çıktı: `a2` çok biçimli bir
    kanal — sohbette atıf (`{type,organization}`), görsel üretiminde
    `[{"type":"heartbeat"}]`, sonunda `[{"type":"image","image":"<url>",
    "mimeType":"image/jpeg"}]` taşıyor (15 Kasım canlı ölçümü). İlk örnek atıf
    olunca görsel biçimi logda hiç görünmedi.
    """

    def __init__(self, max_codes: int = 8, max_shapes_per_code: int = 3) -> None:
        self._counts: dict[str, int] = {}
        self._shapes: dict[str, list[str]] = {}
        self._max_codes = max_codes
        self._max_shapes_per_code = max_shapes_per_code

    def add(self, event: StreamEvent) -> None:
        if event.type not in MEDIA_CANDIDATE_PARTS:
            return
        code = event.raw_code or "-"
        self._counts[code] = self._counts.get(code, 0) + 1
        seen = self._shapes.setdefault(code, [])
        if len(seen) >= self._max_shapes_per_code:
            return
        shape = _describe_value(event.data)
        if shape not in seen:
            seen.append(shape)

    def __bool__(self) -> bool:
        return bool(self._counts)

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def shapes(self) -> dict[str, str]:
        """Kod → farklı biçimler (`|` ile birleşik). Değerler yalnız tür+uzunluk."""
        return {code: " | ".join(seen) for code, seen in self._shapes.items() if seen}


# ---------------------------------------------------------------- görsel parçaları
#: Bir sözlükte görsel adresini/verisini taşıyabilecek alan adları. Ölçülen
#: alan `image`; diğerleri hedef biçimi değiştirse diye yedek olarak taranır.
_MEDIA_VALUE_KEYS = ("image", "url", "imageUrl", "src", "data")


@dataclass(frozen=True, slots=True)
class MediaPart:
    """Akışta gelen tek bir görsel.

    `url` ya http(s) adresi ya da (hedef base64 gönderirse) `data:` URI'sidir;
    ikisi de istemciye olduğu gibi verilebilir.
    """

    url: str
    mime: str = ""
    kind: str = "image"


def _as_media_url(raw: str, mime: str) -> str:
    """Ham değeri istemciye verilebilir adrese çevirir.

    `&amp;` kaçışı bilinçli olarak çözülür: imzalı R2 adresinin sorgu
    parametreleri (`X-Amz-Signature`, `X-Amz-Expires`) kaçışlı geldiğinde adres
    çalışmıyor. Aynı normalizasyon çerez içe aktarmada da var.
    """
    text = raw.strip().replace("&amp;", "&")
    if text.startswith(("http://", "https://", "data:")):
        return text
    # Adres değil de ham base64 geldiyse data URI olarak taşınır; aksi hâlde
    # markdown bağlantısı kırık bir ada işaret ederdi.
    if mime and text:
        return f"data:{mime};base64,{text}"
    return text


def _iter_dict_items(data: Any) -> Iterator[dict[str, Any]]:
    """Çok biçimli bir kanalın yükündeki sözlükleri verir (liste ya da tek dict).

    `a2` gibi kanallar modlar arasında farklı yük taşır (`heartbeat`, `image`,
    atıf) ve bazen liste bazen tek sözlük gelir. Gezinme kuralı tek yerde durur
    ki her çıkarıcı aynı toleransı göstersin — sistematiğin özü bu: **koda değil
    içeriğe bak**.
    """
    items: list[Any] = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict):
            yield item


def _first_text(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Verilen alan adlarından ilk dolu metin değerini döndürür."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _media_kind(item: dict[str, Any]) -> tuple[str, str]:
    """Parçanın `type` ve mimeType değerlerini normalize eder."""
    kind = str(item.get("type") or "").strip().lower()
    mime = str(item.get("mimeType") or item.get("mime_type") or "").strip()
    return kind, mime


def _is_media_item(item: dict[str, Any]) -> bool:
    kind, mime = _media_kind(item)
    return kind == "image" or mime.lower().startswith("image/")


def extract_media_parts(data: Any) -> list[MediaPart]:
    """Çok biçimli bir kanalın yükünden görsel parçalarını çıkarır.

    Ölçüm (15 Kasım, canlı görsel akışı — tarayıcının `create-evaluation`
    yanıtı)::

        a2:[{"type":"heartbeat"}]                      ← üretim sürerken
        a2:[{"type":"image","image":"https://…r2.cloudflarestorage.com/….jpeg
             ?X-Amz-Expires=3600&X-Amz-Signature=…","mimeType":"image/jpeg"}]
        ad:{"finishReason":"stop"}

    Aynı kod (`a2`) sohbette atıf taşıyor: `[{type,organization}]`. Bu yüzden
    **kod değil içerik** esas alınır: `type == "image"` ya da `image/` ile
    başlayan bir mimeType varsa parça görseldir; `heartbeat` ve atıf parçaları
    adres alanı taşımadığı için kendiliğinden elenir.

    Adres **imzalı ve ~1 saat ömürlü** (`X-Amz-Expires=3600`): saklanmaz,
    loglanmaz, geldiği anda istemciye iletilir.
    """
    parts: list[MediaPart] = []
    for item in _iter_dict_items(data):
        raw = _first_text(item, _MEDIA_VALUE_KEYS)
        if not raw or not _is_media_item(item):
            continue
        kind, mime = _media_kind(item)
        url = _as_media_url(raw, mime)
        if url and all(part.url != url for part in parts):
            parts.append(MediaPart(url=url, mime=mime, kind=kind or "image"))
    return parts


def media_markdown(part: MediaPart, alt: str = "görsel") -> str:
    """Görseli OpenAI şemasını bozmadan iletmek için markdown'a çevirir.

    Yeni alan/şema icat edilmez: içerik metindir, istemciler (Open WebUI,
    Continue, Cursor) markdown görseli çizer; Telegram tarafı adresi buradan
    okuyup `sendPhoto` ile gerçek fotoğrafı gönderir.
    """
    return f"\n\n![{alt}]({part.url})\n\n"


# ---------------------------------------------------------------- kaynak parçaları
#: Bir sözlükte kaynak adı taşıyabilecek alanlar. Ölçülen alan `organization`
#: (15 Kasım, canlı sohbet/arama akışı: `a2: list(1)[dict(2){type:str(12),
#: organization:str(6)}]` ve `…str(7)` → 6-7 karakterlik kaynak adları). Diğerleri
#: hedef biçimi zenginleştirirse (başlık/adres) diye yedek olarak taranır.
_SOURCE_LABEL_KEYS = ("title", "organization", "name", "source", "site", "publisher")
_SOURCE_URL_KEYS = ("url", "uri", "link", "href")


@dataclass(frozen=True, slots=True)
class Citation:
    """Akışta gelen tek bir kaynak/atıf."""

    label: str
    url: str = ""


def extract_citations(data: Any) -> list[Citation]:
    """Kaynak/atıf parçalarını çıkarır (görsel ve `heartbeat` parçaları hariç).

    `a2` kanalı görsel modunda `image`, sohbet/aramada atıf taşıdığı için burada
    da **koda değil içeriğe** bakılır: kaynak alanı taşıyan parça atıftır, adres
    alanı taşımayan `heartbeat` kendiliğinden elenir. Görsel parçaları atlanır ki
    aynı parça hem görsel hem kaynak sayılıp iki kez istemciye gitmesin.
    """
    found: list[Citation] = []
    for item in _iter_dict_items(data):
        if _is_media_item(item):
            continue
        label = _first_text(item, _SOURCE_LABEL_KEYS)
        url = _first_text(item, _SOURCE_URL_KEYS)
        if not label and not url:
            continue
        citation = Citation(label=label or url, url=url)
        if citation not in found:
            found.append(citation)
    return found


def render_sources(citations: Sequence[Citation], heading: str = "Kaynaklar") -> str:
    """Kaynakları yanıtın sonuna eklenecek metne çevirir.

    Adres varsa markdown bağlantısı, yoksa düz ad: hedef bugün yalnız kaynak adı
    gönderiyor (`organization`), ileride `url` de gönderirse biçim kendiliğinden
    zenginleşir. OpenAI şeması değişmez — ek içerik metindir.
    """
    if not citations:
        return ""
    lines = [
        f"- [{item.label}]({item.url})" if item.url else f"- {item.label}"
        for item in citations
    ]
    return f"\n\n{heading}:\n" + "\n".join(lines) + "\n"
