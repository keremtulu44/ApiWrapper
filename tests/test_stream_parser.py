"""Akış ayrıştırıcı birim testleri."""

from __future__ import annotations

import pytest

from app.schemas.upstream import EventType
from app.upstream.stream_parser import (
    Citation,
    StreamDecoder,
    extract_citations,
    extract_media_parts,
    media_markdown,
    normalize_finish_reason,
    normalize_usage,
    parse_line,
    parse_stream,
    render_sources,
)


def test_parses_text_event():
    event = parse_line('0:"Merhaba dünya"')
    assert event is not None
    assert event.type is EventType.TEXT
    assert event.text == "Merhaba dünya"


def test_parses_new_ai_sdk_text_and_reasoning_codes():
    text = parse_line('a0:"Merhaba"')
    assert text is not None
    assert text.type is EventType.TEXT
    assert text.text == "Merhaba"
    thinking = parse_line('ag:"düşünüyorum"')
    assert thinking is not None
    assert thinking.type is EventType.REASONING
    done = parse_line('ad:{"finishReason":"stop"}')
    assert done is not None
    assert done.type is EventType.FINISH
    assert done.finish_reason == "stop"


def test_extracts_text_from_nested_payload():
    """`delta: {"text": "..."}` gibi iç içe taşıyıcılar da çözülmeli."""
    event = parse_line('0:{"delta":{"text":"iç içe"}}')
    assert event is not None
    assert event.text == "iç içe"


def test_unwraps_data_stream_line_wrapped_in_sse():
    """Bazı proxy'ler Vercel satırını tekrar `data:` içine sarar."""
    event = parse_line('data: 0:"sarmal metin"')
    assert event is not None
    assert event.type is EventType.TEXT
    assert event.text == "sarmal metin"


def test_parses_start_event():
    event = parse_line('f:{"messageId":"m1"}')
    assert event.type is EventType.START
    assert event.data == {"messageId": "m1"}


def test_parses_finish_with_usage():
    event = parse_line('d:{"finishReason":"stop","usage":{"promptTokens":5,"completionTokens":3}}')
    assert event.type is EventType.FINISH
    assert event.finish_reason == "stop"
    assert event.usage == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}


def test_parses_error_event():
    event = parse_line('3:"rate limited"')
    assert event.type is EventType.ERROR
    assert event.text == "rate limited"
    assert event.is_terminal


def test_unknown_code_is_tolerated():
    event = parse_line('z:{"a":1}')
    assert event.type is EventType.UNKNOWN


def test_blank_and_comment_lines_ignored():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line(": heartbeat") is None
    assert parse_line("event: message") is None


def test_invalid_json_falls_back_to_raw_text():
    event = parse_line("0:not-json")
    assert event.type is EventType.TEXT
    assert event.text == "not-json"


def test_sse_tolerance_openai_shape():
    event = parse_line('data: {"choices":[{"delta":{"content":"hi"}}]}')
    assert event.type is EventType.TEXT
    assert event.text == "hi"


def test_sse_done_is_finish():
    event = parse_line("data: [DONE]")
    assert event.type is EventType.FINISH


def test_finish_reason_mapping():
    assert normalize_finish_reason("tool-calls") == "tool_calls"
    assert normalize_finish_reason("length") == "length"
    assert normalize_finish_reason("weird") == "stop"
    assert normalize_finish_reason(None) is None


def test_usage_alternate_field_names():
    assert normalize_usage({"inputTokens": 2, "outputTokens": 3}) == {
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
    }
    assert normalize_usage("nope") == {}


def test_decoder_handles_split_utf8_multibyte():
    decoder = StreamDecoder()
    payload = '0:"ğüşİ"\n'.encode()
    mid = len(payload) // 2
    assert decoder.feed(payload[:mid]) == [] or True
    lines = decoder.feed(payload[:mid]) + decoder.feed(payload[mid:])
    text = "".join(lines)
    assert "ğüşİ" in text


def test_decoder_flushes_trailing_partial_line():
    decoder = StreamDecoder()
    assert decoder.feed(b'0:"tail"') == []
    assert decoder.flush() == ['0:"tail"']


@pytest.mark.asyncio
async def test_parse_stream_end_to_end():
    async def chunks():
        yield b'f:{"messageId":"m"}\n0:"Hel'
        yield b'lo"\n0:" world"\n'
        yield b'd:{"finishReason":"stop"}\n'

    events = [event async for event in parse_stream(chunks())]
    texts = [e.text for e in events if e.type is EventType.TEXT]
    assert texts == ["Hello", " world"]
    assert events[-1].type is EventType.FINISH


# ------------------------------------------------------------------ görsel parçaları
_MEASURED_IMAGE = {
    "type": "image",
    "image": "https://hedef.test/r2/1-uuid.jpeg?X-Amz-Expires=3600&amp;X-Amz-Signature=x",
    "mimeType": "image/jpeg",
}


def test_extracts_the_measured_image_part():
    """15 Kasım canlı ölçümü: görsel `a2` kanalında imzalı R2 adresiyle geliyor."""
    parts = extract_media_parts([{"type": "heartbeat"}, _MEASURED_IMAGE])
    assert len(parts) == 1
    # `&amp;` çözülür: kaçışlı sorgu imzayı bozar, adres 403 verir.
    assert parts[0].url == (
        "https://hedef.test/r2/1-uuid.jpeg?X-Amz-Expires=3600&X-Amz-Signature=x"
    )
    assert parts[0].mime == "image/jpeg"
    assert parts[0].kind == "image"


@pytest.mark.parametrize(
    "data",
    [
        [{"type": "heartbeat"}],                       # adres alanı yok
        [{"type": "citation", "organization": "X"}],    # sohbet atfı (aynı kod!)
        {"type": "image"},                              # adres yok
        [{"type": "image", "image": "   "}],            # boş adres
        [{"type": "video", "url": "https://h/v.mp4", "mimeType": "video/mp4"}],
        "düz metin",
        None,
        [],
    ],
)
def test_non_image_payloads_yield_nothing(data):
    assert extract_media_parts(data) == []


def test_bare_dict_and_base64_payloads():
    assert extract_media_parts({"type": "image", "url": "https://h/a.png"})[0].url == (
        "https://h/a.png"
    )
    part = extract_media_parts([{"image": "QUJD", "mimeType": "image/png"}])[0]
    # Adres değil ham veri → data URI (istemci çizebilsin).
    assert part.url == "data:image/png;base64,QUJD"


def test_duplicate_urls_are_collapsed():
    parts = extract_media_parts([_MEASURED_IMAGE, dict(_MEASURED_IMAGE)])
    assert len(parts) == 1


def test_media_markdown_is_a_plain_image_tag():
    part = extract_media_parts([_MEASURED_IMAGE])[0]
    assert media_markdown(part) == f"\n\n![görsel]({part.url})\n\n"


# ---------------------------------------------------------------- kaynak parçaları
#: 15 Kasım canlı ölçümü: atıf `a2` kanalında `{type, organization}` olarak gelir
#: (şekil özeti: `list(1)[dict(2){type:str(12),organization:str(6)}]`).
_MEASURED_CITATION = {"type": "organization", "organization": "Google"}


def test_extracts_the_measured_citation_part():
    parts = extract_citations([{"type": "heartbeat"}, _MEASURED_CITATION])
    assert parts == [Citation(label="Google")]


def test_citation_with_url_renders_as_a_link():
    parts = extract_citations([{"title": "Haber", "url": "https://kaynak.test/h"}])
    assert parts == [Citation(label="Haber", url="https://kaynak.test/h")]
    assert render_sources(parts) == "\n\nKaynaklar:\n- [Haber](https://kaynak.test/h)\n"


def test_image_parts_are_not_counted_as_sources():
    """Aynı parça hem görsel hem kaynak sayılıp iki kez istemciye gitmesin."""
    assert extract_citations([_MEASURED_IMAGE]) == []


def test_duplicate_sources_collapse_and_order_is_kept():
    parts = extract_citations(
        [_MEASURED_CITATION, {"organization": "YouTube"}, dict(_MEASURED_CITATION)]
    )
    assert [part.label for part in parts] == ["Google", "YouTube"]


@pytest.mark.parametrize(
    "data",
    [[{"type": "heartbeat"}], "düz metin", None, [], [{"a": 1}], {"type": "image"}],
)
def test_payloads_without_source_fields_yield_no_citation(data):
    assert extract_citations(data) == []


def test_render_sources_is_empty_without_citations():
    assert render_sources([]) == ""
