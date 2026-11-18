"""Yarıda biten akış yanıtı sırayı kilitlememeli; bölünmüş emoji yanıtı bozmamalı.

İki ayrı kusur, tek kök: Starlette yarıda biten bir akışın gövde üreticisini
kapatmaz. Sıradaki yer üreticinin `finally` bloğunda bırakıldığı için, üretici
bir `yield`'de askıda kalırsa sıra da onunla birlikte askıda kalır (gerçek
sunucuda ölçüldü: 6 sn sonra hâlâ kilitli; emniyet supabı 30 dk).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import anyio
import httpx
import pytest
import respx

from app.api.streaming import GuardedStreamingResponse, close_stream
from app.api.v1.ask import AskRunner
from app.main import create_app
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.utils.text import SurrogateJoiner, repair_surrogates
from tests.conftest import UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"
HIGH, LOW = "\ud83d", "\ude00"  # 😀 (U+1F600) karakterinin iki yarısı


# ------------------------------------------------------------ metin onarımı
def test_adjacent_halves_become_the_real_character():
    assert repair_surrogates(f"güle {HIGH}{LOW} bitti") == "güle 😀 bitti"


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (f"a{HIGH}b", "a\ufffdb"),
        (f"a{LOW}b", "a\ufffdb"),
        (f"ab{HIGH}", "ab\ufffd"),
        (f"{LOW}ab", "\ufffdab"),
        (f"a{LOW}{HIGH}b", "a\ufffd\ufffdb"),
        (f"a{HIGH}{HIGH}{LOW}b", "a\ufffd😀b"),
    ],
)
def test_a_half_without_its_partner_is_replaced(broken, expected):
    repaired = repair_surrogates(broken)
    assert repaired == expected
    repaired.encode("utf-8")  # artık çevrilebilir


def test_ordinary_text_is_returned_untouched():
    text = "güle güle 😀 İstanbul'dan selam"
    assert repair_surrogates(text) is text
    assert repair_surrogates("") == ""


def test_joiner_reunites_a_character_split_across_chunks():
    joiner = SurrogateJoiner()
    pieces = [joiner.feed(part) for part in ("güle ", HIGH, LOW, " bitti")]
    assert pieces == ["güle ", "", "😀", " bitti"]
    assert joiner.flush() == ""


def test_joiner_handles_a_half_that_never_gets_its_partner():
    joiner = SurrogateJoiner()
    assert joiner.feed(f"son{HIGH}") == "son"
    assert joiner.feed("x") == "\ufffdx"
    assert joiner.feed(f"y{HIGH}") == "y"
    assert joiner.flush() == "\ufffd"
    assert joiner.flush() == ""


def test_joiner_passes_ordinary_chunks_through_unchanged():
    joiner = SurrogateJoiner()
    chunk = "sıradan parça 😀"
    assert joiner.feed(chunk) is chunk
    assert joiner.feed("") == ""


# ------------------------------------------------ uçtan uca: bölünmüş emoji
def split_emoji_stream() -> bytes:
    """Upstream'in 😀 karakterini iki parçaya bölüp JSON kaçışıyla gönderdiği akış."""
    whole = ai_stream("X")
    return whole.replace(b'0:"X"', b'0:"g\\u00fcle \\ud83d"\n0:"\\ude00 bitti"')


CHAT = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}


@respx.mock
def test_split_emoji_no_longer_breaks_the_plain_answer(client):
    """Önceden: HTTP 500 (`UnicodeEncodeError`)."""
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=split_emoji_stream())
    )

    response = client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "güle 😀 bitti"


@respx.mock
def test_split_emoji_no_longer_cuts_the_stream_short(client):
    """Önceden: akış ilk parçadan sonra kesiliyor, `[DONE]` gelmiyordu."""
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=split_emoji_stream())
    )

    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True})

    assert response.status_code == 200
    chunks = [
        json.loads(line[5:])
        for line in response.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    ]
    text = "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks)
    assert text == "güle 😀 bitti"
    assert response.text.rstrip().endswith("data: [DONE]")
    assert client.app.state.queue.snapshot()["active"] is None


@respx.mock
def test_split_emoji_reaches_the_ask_caller_intact(client):
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=split_emoji_stream())
    )

    response = client.post("/v1/ask", json={"prompt": "x"})

    lines = [json.loads(line[5:]) for line in response.text.splitlines() if line[:5] == "data:"]
    assert lines[-1]["status"] == "done"
    assert lines[-1]["text"] == "güle 😀 bitti"


# ------------------------------------------- /v1/ask satırı hiçbir yerde bölünmez
@respx.mock
def test_ask_lines_are_pure_ascii_and_survive_any_line_reader(client):
    """U+2028 / U+2029 / U+0085, `splitlines()` ile okuyan istemcide satırı bölerdi."""
    answer = "ilk satır\u2028ikinci satır\u2029üçüncü\u0085dördüncü\nİstanbul ğüşöç 😀"
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=ai_stream(answer))
    )

    with client.stream("POST", "/v1/ask", json={"prompt": "x"}) as response:
        raw = response.read()
    with client.stream("POST", "/v1/ask", json={"prompt": "x"}) as response:
        lines = list(response.iter_lines())  # httpx: `str.splitlines()` tabanlı okuyucu

    assert raw.isascii()
    data = [line for line in lines if line.startswith("data:")]
    assert len(data) == 1  # tek satır: bölünmedi
    assert json.loads(data[0][5:])["text"] == answer
    # Kaba bir okuyucu da (gövdeyi alıp satırlara bölen) aynı sonucu alır.
    naive = [line for line in raw.decode("ascii").splitlines() if line.startswith("data:")]
    assert json.loads(naive[0][5:])["text"] == answer


# ------------------------------------------------------- korumalı akış yanıtı
def http_scope(spec_version: str = "2.4") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "method": "POST",
        "path": "/",
        "headers": [],
    }


async def never_disconnects() -> dict[str, Any]:
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


def tracked_body(*chunks: str):
    """Parçaları veren üretici ve `finally` bloğunun çalıştığını gösteren bayrak."""
    closed = asyncio.Event()

    async def body():
        try:
            for chunk in chunks:
                yield chunk
        finally:
            closed.set()

    return body(), closed


async def test_generator_is_closed_when_a_chunk_cannot_be_encoded():
    """Ölçülen kusurun kendisi: kodlanamayan parça üreticiyi askıda bırakıyordu."""
    body, closed = tracked_body("ilk", HIGH, "hiç ulaşılmayan")
    response = GuardedStreamingResponse(body, media_type="text/event-stream")

    async def send(_message: dict[str, Any]) -> None:
        return None

    with pytest.raises(UnicodeEncodeError):
        await response(http_scope(), never_disconnects, send)

    assert closed.is_set()


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_generator_is_closed_when_the_client_cannot_be_written_to(spec_version):
    body, closed = tracked_body("ilk", "ikinci", "üçüncü")
    response = GuardedStreamingResponse(body, media_type="text/event-stream")
    sent: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            if sent:
                raise OSError("bağlantı koptu")
            sent.append(message["body"])

    # Dışarıya çıkan istisnanın türü Starlette/ASGI sürümüne göre değişir
    # (OSError, ClientDisconnect ya da bir istisna grubu); önemli olan üretici.
    try:
        await response(http_scope(spec_version), never_disconnects, send)
    except Exception as exc:
        failure: Exception | None = exc
    else:
        failure = None

    assert failure is not None, "yazma hatası sessizce yutuldu"
    assert sent == [b"ilk"]
    assert closed.is_set()


async def test_generator_is_closed_when_the_response_is_cancelled_mid_write():
    """Ara katman yanıtı yazma sırasında iptal ederse üretici `yield`'de askıdadır."""
    body, closed = tracked_body("ilk", "ikinci")
    response = GuardedStreamingResponse(body, media_type="text/event-stream")
    writing = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            writing.set()
            await asyncio.Event().wait()  # yazma hiç bitmiyor

    task = asyncio.create_task(response(http_scope(), never_disconnects, send))
    await asyncio.wait_for(writing.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(closed.wait(), timeout=1)


async def test_cleanup_finishes_even_inside_a_cancelled_anyio_scope():
    """Starlette'in ara katmanları anyio kullanır: iptal edilmiş kapsamda her
    `await` yeniden iptal edilir. Üreticinin kapanışı (upstream bağlantısını
    kapatmak gibi beklemeli işler) buna rağmen sonuna kadar gitmelidir."""
    cleaned = asyncio.Event()

    async def body():
        try:
            yield "ilk"
            yield "ikinci"
        finally:
            await asyncio.sleep(0.02)  # beklemeli temizlik
            cleaned.set()

    response = GuardedStreamingResponse(body(), media_type="text/event-stream")
    writing = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            writing.set()
            await asyncio.Event().wait()

    async def cancel_when_writing(scope: anyio.CancelScope) -> None:
        await writing.wait()
        scope.cancel()

    async with anyio.create_task_group() as group:
        group.start_soon(cancel_when_writing, group.cancel_scope)
        await response(http_scope(), never_disconnects, send)

    await asyncio.wait_for(cleaned.wait(), timeout=1)


async def test_completed_stream_is_sent_whole_and_closed_once():
    body, closed = tracked_body("ilk", "ikinci")
    response = GuardedStreamingResponse(body, media_type="text/event-stream")
    sent: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            sent.append(message.get("body", b""))

    await response(http_scope(), never_disconnects, send)

    assert b"".join(sent) == b"ilkikinci"
    assert closed.is_set()


async def test_close_stream_tolerates_plain_iterators_and_failing_generators():
    await close_stream(iter(["a"]))  # `aclose` yok: sessizce geçer

    async def stubborn():
        try:
            yield "a"
        finally:
            raise RuntimeError("kapanışta hata")

    generator = stubborn()
    await generator.__anext__()
    await close_stream(generator)  # hata yutulur, loglanır


# --------------------------------------- servis: askıdaki akış kapatılınca sıra
@respx.mock
async def test_closing_a_stream_at_a_yield_releases_the_queue():
    """Koruma üreticiyi kapattığında sıradaki yer anında boşalmalı."""
    respx.post(url__startswith=STREAM_URL).mock(
        return_value=httpx.Response(200, content=ai_stream("bir", "iki", "üç"))
    )
    app = create_app(make_settings())
    request = ChatCompletionRequest(
        model="test-model", messages=[ChatMessage(role="user", content="selam")], stream=True
    )
    async with app.router.lifespan_context(app):
        service, queue = app.state.completion_service, app.state.queue
        stream = service.stream_completion(request)
        await stream.__anext__()  # rol parçası
        first = await stream.__anext__()  # ilk içerik parçası: sıra bu isteğin
        assert "bir" in first
        assert queue.snapshot()["active"] == "api"

        await stream.aclose()  # yanıt yarıda bitti

        assert queue.snapshot()["active"] is None
        assert queue.snapshot()["served_total"] == 1
        # Sıra gerçekten boş: sıradaki istek beklemeden işlenir.
        follow_up = await asyncio.wait_for(
            service.create_completion(request.model_copy(update={"stream": False})), timeout=2
        )
        assert follow_up.choices[0].message.content == "birikiüç"


# ------------------------------------------- uçlar korumalı yanıtı kullanıyor mu
def unencodable_stream(closed: Any):
    """İkinci parçası UTF-8'e çevrilemeyen gövde: yanıt yarıda biter."""

    async def stream(*_args: Any, **_kwargs: Any):
        try:
            yield "data: ilk\n\n"
            yield HIGH
            yield "data: ulaşılmaz\n\n"
        finally:
            closed.set()

    return stream


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/v1/chat/completions", {**CHAT, "stream": True}),
        ("/v1/completions", {"model": "test-model", "prompt": "selam", "stream": True}),
    ],
)
def test_streaming_routes_close_their_generator_when_the_response_breaks(
    client, monkeypatch, path, payload
):
    closed = threading.Event()
    monkeypatch.setattr(
        client.app.state.completion_service, "stream_completion", unencodable_stream(closed)
    )

    with pytest.raises(UnicodeEncodeError):
        client.post(path, json=payload)

    assert closed.wait(timeout=1), "akış üreticisi kapatılmadı: sıra kilitli kalırdı"


def test_ask_route_closes_its_generator_when_the_response_breaks(client, monkeypatch):
    closed = threading.Event()
    monkeypatch.setattr(AskRunner, "events", unencodable_stream(closed))

    with pytest.raises(UnicodeEncodeError):
        client.post("/v1/ask", json={"prompt": "x"})

    assert closed.wait(timeout=1), "ask üreticisi kapatılmadı: iş arkada sürerdi"
