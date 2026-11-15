"""Modality (chat / web-search / görsel) desteğinin sözleşme testleri.

Ölçüm (15 Kasım): chat, web arama ve görsel istekleri **aynı** uca
(`create-evaluation`), aynı çerez, aynı `mode` (`direct-battle`) ve aynı
`modelAId` ile gidiyor. Fark yalnızca gövdedeki `modality` ve başlıktaki
`referer`. Bu testler o iki alanın model kaydından istemciye kadar bozulmadan
taşındığını kilitler.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from app.adapters.openai_to_upstream import build_upstream_request
from app.schemas.openai import ChatCompletionRequest, ChatMessage
from app.schemas.upstream import StreamEvent
from app.services.model_registry import ModelRegistry
from app.telegram.bot import TelegramBot, TelegramSettings
from app.upstream.headers import build_stream_headers
from app.upstream.stream_parser import PartShapeSummary, parse_line
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


def _request(text: str = "selam") -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="test-model", messages=[ChatMessage(role="user", content=text)]
    )


# ---------------------------------------------------------------- kayıt defteri
def test_entry_without_modality_stays_chat():
    """`modality` yazılmayan kayıt eski davranışta kalır (geriye dönük uyum)."""
    registry = ModelRegistry.from_dict(
        {"models": [{"id": "a", "upstream_id": "ua"}]}
    )
    entry = registry.resolve("a")
    assert entry.modality == "chat"
    assert entry.referer_path == ""


def test_entry_parses_modality_and_referer_path():
    registry = ModelRegistry.from_dict(
        {
            "models": [
                {
                    "id": "web",
                    "upstream_id": "u1",
                    "modality": "Search",
                    "referer_path": "/search/direct",
                }
            ]
        }
    )
    entry = registry.resolve("web")
    # Normalizasyon: büyük harf/boşluk upstream gövdesine sızmamalı.
    assert entry.modality == "search"
    assert entry.referer_path == "/search/direct"


def test_unknown_modality_is_passed_through_not_rejected():
    """Hedef yeni bir mod eklerse kayıt defteri onu engellemez (yalnız uyarı)."""
    registry = ModelRegistry.from_dict(
        {"models": [{"id": "v", "upstream_id": "uv", "modality": "video"}]}
    )
    assert registry.resolve("v").modality == "video"


def test_document_level_upstream_id_is_inherited():
    """Belge düzeyindeki `upstream_id` kayıtlara miras kalır.

    Üç mod aynı modelAId'i kullanıyor. Kimliği üç kayda birden yazmak,
    `curl_to_env.py` yalnız ilk satırı güncellediği için ikisini bayat bırakırdı.
    """
    registry = ModelRegistry.from_dict(
        {
            "upstream_id": "shared-id",
            "models": [
                {"id": "max", "modality": "chat"},
                {"id": "web", "modality": "search"},
                {"id": "ozel", "upstream_id": "kendi-id", "modality": "image"},
            ],
        }
    )
    assert registry.resolve("max").upstream_id == "shared-id"
    assert registry.resolve("web").upstream_id == "shared-id"
    # Kayıt düzeyindeki değer önceliklidir.
    assert registry.resolve("ozel").upstream_id == "kendi-id"


def test_new_curl_identity_reaches_every_inheriting_mode():
    """cURL'den çıkan modelAId, miras alan modların **hepsine** yazılır.

    Gövdedeki kimlik kayıt defterinden gelir (`entry.upstream_id`); yalnız
    varsayılan kayıt güncellenirse `web` ve `image` dosyadaki bayat id'yi
    gönderir. İstemci 200 görür, cevaplayan model yanlış olur — sessiz hata.
    """
    registry = ModelRegistry.from_dict(
        {
            "upstream_id": "bayat-id",
            "models": [
                {"id": "max", "modality": "chat"},
                {"id": "web", "modality": "search"},
                {"id": "image", "modality": "image"},
                {"id": "ozel", "upstream_id": "kendi-id", "modality": "chat"},
            ],
        }
    )
    assert registry.apply_shared_upstream_id("yeni-id") == ["image", "max", "web"]
    for name in ("max", "web", "image"):
        assert registry.resolve(name).upstream_id == "yeni-id"
    # Kendi kimliğini açıkça taşıyan kayıt miras almaz: dokunulmaz.
    assert registry.resolve("ozel").upstream_id == "kendi-id"


def test_identity_binding_falls_back_to_default_without_shared_id():
    """Belge düzeyinde kimlik yoksa eski davranış korunur (geriye dönük uyum)."""
    registry = ModelRegistry.from_dict(
        {
            "default": "a",
            "models": [
                {"id": "a", "upstream_id": "ua"},
                {"id": "b", "upstream_id": "ub"},
            ],
        }
    )
    assert registry.apply_shared_upstream_id("yeni") == ["a"]
    assert registry.resolve("a").upstream_id == "yeni"
    assert registry.resolve("b").upstream_id == "ub"


def test_blank_identity_is_a_noop():
    registry = ModelRegistry.from_dict({"upstream_id": "s", "models": [{"id": "m"}]})
    assert registry.apply_shared_upstream_id("   ") == []
    assert registry.resolve("m").upstream_id == "s"


def test_entry_without_any_upstream_id_is_skipped():
    registry = ModelRegistry.from_dict(
        {"models": [{"id": "a", "upstream_id": "ua"}, {"id": "kimliksiz"}]}
    )
    assert "a" in registry
    assert "kimliksiz" not in registry


def test_shipped_models_file_covers_three_modes():
    """Depoyla gelen `config/models.yaml` üç modu da taşır ve kimliği paylaşır."""
    registry = ModelRegistry.from_file("config/models.yaml")
    modes = {entry.id: entry.modality for entry in registry.list_models()}
    assert modes == {"max": "chat", "web": "search", "image": "image"}

    ids = {entry.upstream_id for entry in registry.list_models()}
    assert len(ids) == 1, "üç mod aynı modelAId'i paylaşmalı; tek satırdan güncellenir"

    referers = {entry.id: entry.referer_path for entry in registry.list_models()}
    assert referers == {
        # Sohbet referer'ı bilerek boş: cURL'den gelen `UPSTREAM_REFERER_PATH`
        # kullanılır. Buraya `/text/direct` yazmak, gerçek sohbet sayfasından
        # alınmış `/c/{chat_id}` değerini ezip 7/24 çalışan sohbeti 403'e
        # düşürmüştü (kayıt düzeyi env'i ezer). Bu assert o regresyonu kilitler.
        "max": "",
        "web": "/search/direct",
        "image": "/image/direct",
    }
    # İstemci Türkçe/İngilizce takma adlarla da modu seçebilsin.
    assert registry.resolve("search").id == "web"
    assert registry.resolve("gorsel").id == "image"


def test_shipped_chat_entry_takes_referer_from_the_operators_curl():
    """Sohbet referer'ı cURL'den gelir; kayıtta pinlenmez (403 regresyonu).

    Operatörün cURL'ü gerçek bir sohbet sayfasından kopyalandığında env'de
    `/c/{chat_id}` durur ve sohbet o parmak iziyle kesintisiz çalışıyordu.
    `max` kaydına `/text/direct` yazıldığında kayıt düzeyi env'i ezdiği için
    çalışan sohbet 403 almaya başladı. Bu test o davranışı kilitler.
    """
    registry = ModelRegistry.from_file("config/models.yaml")
    settings = make_settings(upstream_referer_path="/c/{chat_id}")

    entry = registry.resolve("max")
    assert entry.referer_path == ""
    headers = build_stream_headers(settings, "cid-9", referer_path=entry.referer_path)
    assert headers["referer"] == f"https://{UPSTREAM_DOMAIN}/c/cid-9"

    # Arama ve görsel kendi ölçülmüş yolunu korur: elimizde çalışan bir temel
    # olmadığı için o yollar doğrudan yakalamadan geldi.
    for name, path in (("web", "/search/direct"), ("image", "/image/direct")):
        assert registry.resolve(name).referer_path == path


# ---------------------------------------------------------------------- gövde
def test_modality_reaches_wire_payload():
    settings = make_settings()
    built = build_upstream_request(
        _request(),
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
        modality="search",
        referer_path="/search/direct",
    )
    wire = built.payload.to_wire(settings.upstream_recaptcha_field)
    assert wire["modality"] == "search"
    assert built.modality == "search"
    assert built.referer_path == "/search/direct"


def test_modality_defaults_to_chat_when_not_given():
    settings = make_settings()
    built = build_upstream_request(
        _request(),
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
    )
    assert built.payload.to_wire(settings.upstream_recaptcha_field)["modality"] == "chat"


def test_modality_is_normalized():
    settings = make_settings()
    built = build_upstream_request(
        _request(),
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
        modality="  IMAGE ",
    )
    assert built.modality == "image"


def test_empty_modality_falls_back_to_chat():
    settings = make_settings()
    built = build_upstream_request(
        _request(),
        settings=settings,
        upstream_model_id="u1",
        chat_id="c1",
        recaptcha_token="tok",
        modality="   ",
    )
    assert built.modality == "chat"


# -------------------------------------------------------------------- referer
def test_referer_uses_model_path_when_given():
    settings = make_settings()
    headers = build_stream_headers(settings, "c1", referer_path="/image/direct")
    assert headers["referer"] == f"https://{UPSTREAM_DOMAIN}/image/direct"


def test_referer_falls_back_to_settings_path():
    """Kayıtta yol yoksa `UPSTREAM_REFERER_PATH` geçerli kalır (eski davranış)."""
    settings = make_settings(upstream_referer_path="/text/direct")
    assert build_stream_headers(settings, "c1", referer_path=None)["referer"] == (
        f"https://{UPSTREAM_DOMAIN}/text/direct"
    )
    assert build_stream_headers(settings, "c1", referer_path="  ")["referer"] == (
        f"https://{UPSTREAM_DOMAIN}/text/direct"
    )


def test_referer_path_keeps_chat_id_template():
    settings = make_settings()
    headers = build_stream_headers(settings, "cid-9", referer_path="/c/{chat_id}")
    assert headers["referer"] == f"https://{UPSTREAM_DOMAIN}/c/cid-9"


# ------------------------------------------------------------- uçtan uca API
@respx.mock
def test_web_model_sends_search_modality_and_referer(client):
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("buldum"))
    )
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "second-model",  # fixture'da modality: search
            "messages": [{"role": "user", "content": "ara"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "buldum"

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["modality"] == "search"
    assert sent["modelAId"] == "upstream-model-b"
    assert route.calls[0].request.headers["referer"] == (
        f"https://{UPSTREAM_DOMAIN}/search/direct"
    )


@respx.mock
def test_chat_model_keeps_chat_modality_and_settings_referer(client):
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("merhaba"))
    )
    client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "selam"}],
        },
    )
    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["modality"] == "chat"
    # Kayıtta referer_path yok → ayarlardaki şablon (`/c/{chat_id}`) kullanılır.
    assert route.calls[0].request.headers["referer"].startswith(
        f"https://{UPSTREAM_DOMAIN}/c/"
    )


@respx.mock
def test_streaming_web_request_keeps_modality(client):
    """Mod, akış yolunda da aynı şekilde gider (non-stream'e özgü değil)."""
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("akış"))
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "second-model",
            "messages": [{"role": "user", "content": "ara"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        _ = b"".join(response.iter_bytes())

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["modality"] == "search"
    assert route.calls[0].request.headers["referer"].endswith("/search/direct")


@respx.mock
def test_model_not_found_still_404_for_unknown_mode(client):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "video", "messages": [{"role": "user", "content": "x"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


# ------------------------------------------------------------------ telegram
def _telegram_settings(**overrides: Any) -> TelegramSettings:
    values: dict[str, Any] = {
        "telegram_bot_token": "test-bot-token",
        "wrapper_api_key": "test-wrapper-key",
        "wrapper_base_url": "https://wrapper.test",
        "telegram_allowed_chat_ids": "42",
    }
    values.update(overrides)
    return TelegramSettings(_env_file=None, **values)


async def _run_bot(text: str, **overrides: Any) -> tuple[list[dict], list[dict]]:
    """Botu sahte taşıyıcılarla çalıştırır; (telegram, wrapper) çağrılarını döner."""
    sent: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []

    def telegram_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    def wrapper_handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "yanıt"}}]},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(telegram_handler)) as telegram,
        httpx.AsyncClient(
            base_url="https://wrapper.test",
            headers={"Authorization": "Bearer test-wrapper-key"},
            transport=httpx.MockTransport(wrapper_handler),
        ) as wrapper,
    ):
        bot = TelegramBot(_telegram_settings(**overrides), telegram, wrapper)
        await bot.handle_message({"chat": {"id": 42}, "text": text})
    return sent, received


@pytest.mark.asyncio
async def test_web_command_switches_model_only():
    """`/web <soru>` yalnız model adını değiştirir; ayrı uç nokta gerekmez."""
    _, received = await _run_bot("/web güncel haberler")
    assert received == [
        {
            "model": "web",
            "messages": [{"role": "user", "content": "güncel haberler"}],
            "stream": False,
        }
    ]


@pytest.mark.asyncio
async def test_plain_message_still_uses_chat_model():
    _, received = await _run_bot("Selam")
    assert received[0]["model"] == "max"


@pytest.mark.asyncio
async def test_web_command_without_question_explains_usage():
    sent, received = await _run_bot("/web")
    assert received == []
    assert "/web <soru>" in sent[0]["text"]


@pytest.mark.asyncio
async def test_web_command_accepts_bot_suffix_and_aliases():
    for text in ("/web@ApiWrapperBot soru", "/arama soru", "/search soru"):
        _, received = await _run_bot(text)
        assert received[0]["model"] == "web", text
        assert received[0]["messages"][0]["content"] == "soru", text


@pytest.mark.asyncio
async def test_web_model_is_configurable():
    _, received = await _run_bot("/web soru", telegram_web_model="max-web")
    assert received[0]["model"] == "max-web"


@pytest.mark.asyncio
async def test_help_mentions_web_command():
    sent, _ = await _run_bot("/help")
    assert "/web" in sent[0]["text"]


# ------------------------------------------------------- kimlik bilgisi koruma
def test_modality_does_not_touch_credentials():
    """Mod değişimi yalnız `modality` + `referer`'ı değiştirir.

    Çerez/token/captcha alanlarına dokunulmaz: oturum kimliği moddan bağımsızdır
    ve üç mod aynı hesabı paylaşır.
    """
    settings = make_settings(
        upstream_cookie="a=1; b=2",
        upstream_extra_headers="x-deployment-id=dpl_test",
    )
    chat = build_stream_headers(settings, "c1", referer_path="/text/direct")
    image = build_stream_headers(settings, "c1", referer_path="/image/direct")

    assert chat["cookie"] == image["cookie"] == "a=1; b=2"
    assert chat["x-deployment-id"] == image["x-deployment-id"] == "dpl_test"
    assert chat["user-agent"] == image["user-agent"]
    assert chat["referer"] != image["referer"]
    assert "authorization" not in chat and "authorization" not in image


def test_api_key_still_required_for_mode_switch(client):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "second-model", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401
    assert TEST_API_KEY not in response.text


# ------------------------------------------------------- biçim teşhisi (log)
def _event(code: str, line: str) -> StreamEvent:
    event = parse_line(line)
    assert event is not None, line
    assert event.raw_code == code
    return event


def test_shape_summary_covers_everything_the_client_does_not_see():
    """Özet, istemciye gitmeyen **her** parçayı sayar; metin ve başlık hariç.

    `REASONING` başlangıçta dışarıdaydı ("her akışta gelir, gürültü olur").
    15 Kasım canlı ölçümü bunu boşa düşürdü: `a2` atıf parçası her akışta
    geldiği için satır zaten üretiliyor — elde kalan tek şey kör noktaydı
    (görsel `g:` içinde gelse görünmezdi).
    """
    summary = PartShapeSummary()
    summary.add(_event("0", '0:"merhaba"'))  # istemciye gidiyor → sayılmaz
    summary.add(_event("f", 'f:{"messageId":"m1"}'))  # akış başlığı → sayılmaz
    assert not summary

    summary.add(_event("g", 'g:"thinking"'))  # istemci görmüyor → sayılır
    assert summary.counts == {"g": 1}
    assert summary.shapes["g"] == "str(8)"


def test_shape_summary_describes_data_part_without_values():
    summary = PartShapeSummary()
    summary.add(
        _event(
            "2",
            '2:{"image":"QUJDREVG","mimeType":"image/png","url":"https://hedef/x.png"}',
        )
    )
    assert summary
    assert summary.counts == {"2": 1}
    shape = summary.shapes["2"]
    # Alan adları ve uzunluklar görünür — ayrıştırmayı yazmak için gereken bu.
    assert "image:str(8)" in shape
    assert "mimeType:str(9)" in shape
    assert "url:str(19)" in shape
    # Değerlerin kendisi loglanmaz (görsel verisi/adresi sızmasın).
    assert "QUJDREVG" not in shape
    assert "https://hedef/x.png" not in shape


def test_shape_summary_records_every_distinct_shape_per_code():
    """Kod başına **farklı** biçimlerin hepsi tutulur; tekrarlar yalnız sayılır.

    "Kod başına ilk örnek yeter" varsayımı görseli gizledi: `a2` hem atıf hem
    `heartbeat` hem `image` taşıyor, ilk gelen atıf olunca görsel biçimi logda
    hiç görünmedi ve `content_chars: 0` gizemi çözülemedi (15 Kasım ölçümü).
    """
    summary = PartShapeSummary()
    summary.add(_event("a2", 'a2:[{"type":"citation","organization":"X"}]'))
    summary.add(_event("a2", 'a2:[{"type":"heartbeat"}]'))
    summary.add(_event("a2", 'a2:[{"type":"heartbeat"}]'))  # tekrar → yalnız sayaç
    summary.add(_event("8", '8:[{"cite":"k"}]'))
    assert summary.counts == {"a2": 3, "8": 1}
    shapes = summary.shapes["a2"].split(" | ")
    assert len(shapes) == 2, shapes
    assert "type:str(8)" in shapes[0] and "organization:str(1)" in shapes[0]
    assert shapes[1] == "list(1)[dict(1){type:str(9)}]"
    # Uzun akışta log şişmesin: kod başına biçim sayısı sınırlı.
    for index in range(10):
        summary.add(_event("9", f'9:{{"k{index}":{index}}}'))
    assert len(summary.shapes["9"].split(" | ")) == 3


def test_shape_summary_describes_nested_and_long_values():
    summary = PartShapeSummary()
    summary.add(_event("a", 'a:{"result":{"items":[{"b64":"' + "A" * 5000 + '"}]}}'))
    shape = summary.shapes["a"]
    assert "list(1)" in shape
    assert "str(5000)" in shape
    assert "A" * 100 not in shape


@respx.mock
def test_image_part_is_delivered_to_the_client(client):
    """Görsel parçası istemciye markdown olarak iletilir (eski "bilinen boşluk").

    Hedef görseli TEXT delta'sı olarak göndermiyor; `content_chars: 0` bunun
    sonucuydu. Yeni şema/endpoint icat edilmeden, içerik metnine markdown
    görsel ekleniyor — OpenAI uyumu korunur, istemciler çizer.
    """
    body = (
        b'f:{"messageId":"m-img"}\n'
        b'2:{"image":"QUJDREVG","mimeType":"image/png"}\n'
    ) + ai_stream("")
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "second-model",
            "messages": [{"role": "user", "content": "görsel"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == (
        "\n\n![görsel](data:image/png;base64,QUJDREVG)\n\n"
    )


#: 15 Kasım canlı ölçümü: `create-evaluation` yanıtında görsel `a2` kanalında,
#: imzalı ve 1 saat ömürlü bir R2 adresi olarak geliyor (`X-Amz-Expires=3600`).
#: Buradaki değerler sahte — gerçek imza depoya yazılmaz.
_SIGNED_IMAGE_LINE = (
    'a2:[{"type":"image","image":"https://messages-prod.test.r2.cloudflarestorage.com'
    "/chat-1/17-uuid.jpeg?X-Amz-Algorithm=AWS4-HMAC-SHA256&amp;X-Amz-Expires=3600"
    '&amp;X-Amz-Signature=sahte","mimeType":"image/jpeg"}]'
)
_SIGNED_IMAGE_URL = (
    "https://messages-prod.test.r2.cloudflarestorage.com/chat-1/17-uuid.jpeg"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=3600&X-Amz-Signature=sahte"
)


@respx.mock
def test_signed_image_url_survives_the_stream_path(client):
    """Ölçülen gerçek biçim: `heartbeat` elenir, imzalı adres bozulmadan geçer.

    `&amp;` kaçışı çözülmezse imza geçersiz kalır ve adres 403 verir — bu
    yüzden normalizasyon şart. Heartbeat parçası adres alanı taşımadığı için
    istemciye gitmez (yoksa içerik `![görsel]()` çöpleriyle dolardı).
    """
    body = (
        'f:{"messageId":"m-img"}\n'
        'a2:[{"type":"heartbeat"}]\n'
        'a2:[{"type":"heartbeat"}]\n'
        f"{_SIGNED_IMAGE_LINE}\n"
        'ad:{"finishReason":"stop"}\n'
    ).encode()
    stream_route(respx).mock(return_value=httpx.Response(200, content=body))

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "second-model",
            "messages": [{"role": "user", "content": "bir manzara çiz"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        raw = b"".join(response.iter_bytes()).decode()

    text = "".join(
        json.loads(line[len("data:") :].strip())["choices"][0]["delta"].get("content", "")
        for line in raw.splitlines()
        if line.startswith("data:") and "[DONE]" not in line
    )
    assert text.count("![") == 1, text
    assert _SIGNED_IMAGE_URL in text
    assert "&amp;" not in text
    assert "heartbeat" not in text
