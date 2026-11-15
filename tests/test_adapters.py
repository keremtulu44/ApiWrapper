"""Prompt oluşturma ve istek dönüştürme testleri."""

from __future__ import annotations

import pytest

from app.adapters.openai_to_upstream import build_upstream_request, validate_request
from app.adapters.prompt_builder import build_prompt, collect_attachments
from app.core.errors import InvalidRequestError, PayloadTooLargeError
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.schemas.upstream import DEFAULT_UPSTREAM_MODE
from tests.conftest import make_settings


def msg(role: str, content) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def test_single_user_message_is_passed_through():
    prompt = build_prompt([msg("user", "Merhaba")])
    assert prompt == "Merhaba"


def test_system_message_creates_system_block():
    prompt = build_prompt([msg("system", "Kısa cevap ver."), msg("user", "Selam")])
    assert prompt.startswith("System:\nKısa cevap ver.")
    assert "User: Selam" in prompt
    assert prompt.rstrip().endswith("Assistant:")


def test_multi_turn_transcript():
    prompt = build_prompt(
        [msg("user", "1+1?"), msg("assistant", "2"), msg("user", "peki 2+2?")]
    )
    assert "User: 1+1?" in prompt
    assert "Assistant: 2" in prompt
    assert prompt.rstrip().endswith("Assistant:")


def test_multimodal_content_parts_flattened():
    content = [
        {"type": "text", "text": "Bu görselde ne var?"},
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
    ]
    prompt = build_prompt([msg("user", content)])
    assert "Bu görselde ne var?" in prompt
    assert "[image: https://x.test/a.png]" in prompt


def test_collect_attachments_from_last_user_message():
    content = [
        {"type": "text", "text": "bak"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]
    attachments = collect_attachments([msg("user", content)])
    assert len(attachments) == 1
    assert attachments[0]["contentType"] == "image/jpeg"


def test_collect_attachments_preserves_images_from_earlier_turns():
    """Çok turlu sohbette önceki kullanıcı mesajlarındaki görseller kaybolmamalı."""
    img1 = [
        {"type": "text", "text": "ilk görsel"},
        {"type": "image_url", "image_url": {"url": "https://x.test/first.png"}},
    ]
    img2 = [
        {"type": "text", "text": "ikinci görsel"},
        {"type": "image_url", "image_url": {"url": "https://x.test/second.jpg"}},
    ]
    attachments = collect_attachments(
        [
            msg("user", img1),
            msg("assistant", "İlk görseli gördüm."),
            msg("user", img2),
        ]
    )
    assert len(attachments) == 2
    assert attachments[0]["url"] == "https://x.test/first.png"
    assert attachments[1]["url"] == "https://x.test/second.jpg"


def test_collect_attachments_deduplicates_same_url():
    """Aynı görsel birden çok turda tekrar edilirse mükerrer eklenmez."""
    content = [
        {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}},
    ]
    attachments = collect_attachments(
        [
            msg("user", content),
            msg("assistant", "?"),
            msg("user", content),
        ]
    )
    assert len(attachments) == 1


def test_attachment_name_extracted_from_url():
    content = [
        {"type": "image_url", "image_url": {"url": "https://cdn.test/photos/kedi.jpg?w=800"}},
    ]
    attachments = msg("user", content).attachments()
    assert len(attachments) == 1
    assert attachments[0]["name"] == "kedi.jpg"


def test_attachment_name_from_data_uri():
    content = [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]
    attachments = msg("user", content).attachments()
    assert len(attachments) == 1
    assert attachments[0]["name"] == "image-1.jpg"
    assert attachments[0]["contentType"] == "image/jpeg"


def test_tool_definitions_rendered_into_prompt():
    tools = [{"type": "function", "function": {"name": "search", "description": "web"}}]
    prompt = build_prompt([msg("user", "ara")], tools=tools)
    assert "search" in prompt
    assert "tool_call" in prompt


def test_hints_included_when_enabled():
    prompt = build_prompt(
        [msg("user", "yaz")], include_hints=True, temperature=0.1, max_tokens=50
    )
    assert "temperature" in prompt
    assert "50 tokens" in prompt


def test_validate_rejects_empty_messages():
    settings = make_settings()
    request = ChatCompletionRequest.model_construct(model="test-model", messages=[])
    with pytest.raises(InvalidRequestError):
        validate_request(request, settings)


def test_validate_rejects_n_greater_than_one():
    settings = make_settings()
    request = ChatCompletionRequest(
        model="test-model", messages=[msg("user", "hi")], n=2
    )
    with pytest.raises(InvalidRequestError):
        validate_request(request, settings)


def test_error_policy_rejects_unsupported_params():
    settings = make_settings(unsupported_params="error")
    request = ChatCompletionRequest(
        model="test-model", messages=[msg("user", "hi")], temperature=0.5
    )
    with pytest.raises(InvalidRequestError):
        validate_request(request, settings)


def test_prompt_length_limit_enforced():
    settings = make_settings(max_prompt_chars=10)
    request = ChatCompletionRequest(model="test-model", messages=[msg("user", "x" * 50)])
    with pytest.raises(PayloadTooLargeError):
        build_upstream_request(
            request,
            settings=settings,
            upstream_model_id="u1",
            chat_id="c1",
            recaptcha_token="",
        )


def test_built_payload_matches_target_wire_format():
    settings = make_settings()
    request = ChatCompletionRequest(model="test-model", messages=[msg("user", "Merhaba")])
    built = build_upstream_request(
        request,
        settings=settings,
        upstream_model_id="upstream-model-a",
        chat_id="chat-1",
        recaptcha_token="tok",
    )
    wire = built.payload.to_wire()
    assert set(wire) == {
        "id",
        "modelAId",
        "userMessageId",
        "modelAMessageId",
        "userMessage",
        "modality",
        "mode",
        "recaptchaV3Token",
    }
    assert wire["id"] == "chat-1"
    assert wire["modelAId"] == "upstream-model-a"
    assert wire["modality"] == "chat"
    assert wire["recaptchaV3Token"] == "tok"
    assert wire["userMessage"]["content"] == "Merhaba"
    assert wire["userMessage"]["experimental_attachments"] == []
    assert wire["userMessage"]["metadata"] == {}
    # `mode` alanı tarayıcı cURL'ünde **her zaman** var. `UPSTREAM_MODE` boşken
    # gövdeden düşmesi canlıda 400 üretti; artık varsayılana düşer.
    assert wire["mode"] == DEFAULT_UPSTREAM_MODE


def test_built_payload_includes_mode_when_configured():
    settings = make_settings(upstream_mode="direct")
    request = ChatCompletionRequest(model="test-model", messages=[msg("user", "hi")])
    wire = build_upstream_request(
        request,
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="",
    ).payload.to_wire()
    assert wire["mode"] == "direct"
    # cx her gerçek istekte değişiyor; tahmini/sabit bir değer üretilmemeli.
    assert "cx" not in wire


def test_mode_is_never_dropped_when_env_is_empty():
    """`UPSTREAM_MODE` boşken gövdede `mode` **yok olmamalı**.

    Canlı arıza (2026-11): Render boş env değeri saklayamadığı için
    `UPSTREAM_MODE` yer tutucuya dönüşüyor, `Settings` onu boşa çeviriyor,
    adapter `None` geçiyor ve `to_wire(exclude_none=True)` alanı gövdeden
    siliyordu. Tarayıcı cURL'ünde alan her zaman var; eksikliği hedefte 400
    üretiyordu. Bu test o zincirin geri dönmesini engeller.
    """
    settings = make_settings()  # upstream_mode varsayılanı boş
    request = ChatCompletionRequest(model="test-model", messages=[msg("user", "ping")])
    wire = build_upstream_request(
        request,
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
    ).payload.to_wire()
    assert wire["mode"] == DEFAULT_UPSTREAM_MODE


def test_mode_is_never_dropped_when_env_holds_render_sentinel():
    """Render'ın boş-değer işareti de gövdeyi `mode`suz bırakmamalı.

    Panoda boş değer yazılamadığı için yer tutucu saklanıyor; `Settings` onu
    boşa çeviriyor. Aynı zincir, aynı sonuç: alan varsayılana düşmeli.
    """
    from app.core.config import RENDER_EMPTY_VALUE

    settings = make_settings(upstream_mode=RENDER_EMPTY_VALUE)
    request = ChatCompletionRequest(model="test-model", messages=[msg("user", "ping")])
    wire = build_upstream_request(
        request,
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
    ).payload.to_wire()
    assert wire["mode"] == DEFAULT_UPSTREAM_MODE


def test_blank_value_in_payload_schema_falls_back_to_default_mode():
    """Doğrudan şema üzerinden boş `mode` verilse bile alan düşmez."""
    from app.schemas.upstream import UpstreamPayload, UserMessage

    payload = UpstreamPayload(
        id="c1",
        modelAId="u1",
        userMessageId="m1",
        modelAMessageId="m2",
        userMessage=UserMessage(content="hi"),
        mode="",
    )
    assert payload.to_wire()["mode"] == DEFAULT_UPSTREAM_MODE
