"""`POST /v1/ask` sözleşmesi: `wait` / `done` / `problem` (upstream respx ile taklit edilir).

Zamanlamaya ve istemci kopmasına dayanan davranışlar gerçek soket üzerinde
`tests/test_ask_live.py` içinde sınanır; burada içerik ve durum mantığı var.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.api.v1.ask import AskRequest, build_chat_request
from app.core.errors import InvalidRequestError
from app.main import create_app
from app.services.alerting import TelegramAlerter
from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream, make_settings

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"
QUOTA_BODY = b'{"error":"daily spend limit reached"}'


def stream_route(router=respx):
    return router.post(url__startswith=STREAM_URL)


def ok_response(*chunks: str) -> httpx.Response:
    return httpx.Response(200, content=ai_stream(*(chunks or ("Merhaba", " dünya"))))


def ask(client: TestClient, payload: dict | None = None, **kwargs):
    """İsteği atar; (HTTP cevabı, `data:` satırlarının çözülmüş hali) döndürür."""
    response = client.post(
        "/v1/ask", json=payload if payload is not None else {"prompt": "soru"}, **kwargs
    )
    lines = [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    return response, lines


def make_client(**overrides) -> Iterator[TestClient]:
    app = create_app(make_settings(**overrides))
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


@pytest.fixture
def fast_client() -> Iterator[TestClient]:
    """İşaret süreleri kısaltılmış istemci (30 sn / 15 sn yerine 50 ms / 50 ms)."""
    yield from make_client(ask_first_wait_seconds=0.05, ask_wait_interval_seconds=0.05)


class RecordingAlerter(TelegramAlerter):
    """Gerçek bildirim politikası; yalnız Telegram'a gönderim yerine kayıt tutar."""

    def __init__(self) -> None:
        super().__init__("123:test-token", 42, repeat_seconds=21600)
        self.sent: list[str] = []

    async def send(self, text: str, *, label: str | None = None) -> bool:
        self.sent.append(text)
        return True


def attach_alerter(client: TestClient) -> RecordingAlerter:
    alerter = RecordingAlerter()
    client.app.state.alerter = alerter
    return alerter


def drain(client: TestClient) -> None:
    """Arka plandaki bildirim görevlerinin bitmesini bekler."""
    deadline = time.monotonic() + 2
    while len(client.app.state.tasks) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(client.app.state.tasks) == 0


# ------------------------------------------------------------------- done
@respx.mock
def test_answer_arrives_whole_in_a_single_done_line(client):
    """Metin tek parça gelir; hızlı cevapta hiç `wait` yoktur."""
    stream_route().mock(return_value=ok_response("Alarm ", "özeti: ", "her şey yolunda."))

    response, lines = ask(client, {"prompt": "durum nedir?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [line["status"] for line in lines] == ["done"]
    assert lines[0]["text"] == "Alarm özeti: her şey yolunda."
    assert lines[0]["model"] == "test-model"
    assert lines[0]["id"]


@respx.mock
def test_unicode_and_newlines_survive_in_the_single_line(client):
    """Çok satırlı cevap tek `data:` satırına sığar (JSON kaçışlı), bölünmez."""
    answer = 'Birinci satır\nİkinci satır: "tırnak" ve ğüşiöç\n\nSon.'
    stream_route().mock(return_value=ok_response(answer))

    response, lines = ask(client)

    data_lines = [line for line in response.text.splitlines() if line.startswith("data:")]
    assert len(data_lines) == 1
    assert lines[0]["text"] == answer


@respx.mock
def test_prompt_is_forwarded_to_upstream(client):
    route = stream_route().mock(return_value=ok_response())

    ask(client, {"prompt": "  BTC alarmı tetiklendi  "})

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["userMessage"]["content"] == "BTC alarmı tetiklendi"


@respx.mock
def test_model_can_be_chosen_or_defaulted(client):
    route = stream_route().mock(return_value=ok_response())

    _, default_lines = ask(client, {"prompt": "x"})
    _, chosen_lines = ask(client, {"prompt": "x", "model": "second-model"})

    assert default_lines[0]["model"] == "test-model"
    assert chosen_lines[0]["model"] == "second-model"
    assert route.call_count == 2


@respx.mock
def test_default_model_comes_from_settings():
    stream_route().mock(return_value=ok_response())
    for client in make_client(ask_default_model="second-model"):
        _, lines = ask(client, {"prompt": "x"})
        assert lines[0]["model"] == "second-model"


@pytest.mark.parametrize("mode", [{"session_reuse": True}, {"session_stateless": False}])
@respx.mock
def test_every_question_gets_its_own_upstream_conversation(mode):
    """Sohbet yeniden kullanımı açıkken bile alarm soruları birbirine ve aynı
    anahtarı kullanan sohbet ucuna (Telegram botu) karışmaz."""
    route = stream_route().mock(side_effect=lambda _request: ok_response())
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    for client in make_client(**mode):
        client.post("/v1/chat/completions", json=chat)
        client.post("/v1/chat/completions", json=chat)
        ask(client, {"prompt": "alarm 1"})
        ask(client, {"prompt": "alarm 2"})

    chat_ids = [str(call.request.url).rsplit("/", 1)[-1] for call in route.calls]
    first_chat, second_chat, first_ask, second_ask = chat_ids
    assert first_chat == second_chat  # sohbet ucu aynı konuşmayı sürdürüyor (ayar gereği)
    assert len({first_chat, first_ask, second_ask}) == 3  # sorular ayrı, bağımsız


# ------------------------------------------------------------------- wait
@respx.mock
def test_wait_lines_come_first_then_exactly_one_done(fast_client):
    """Cevap gecikirse aralıklarla `wait`, sonunda tek `done`; `done`'dan sonra satır yok."""

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.4)
        return ok_response("geç", " gelen cevap")

    stream_route().mock(side_effect=slow)

    _, lines = ask(fast_client)

    statuses = [line["status"] for line in lines]
    assert statuses[-1] == "done"
    assert statuses.count("done") == 1
    assert set(statuses[:-1]) == {"wait"}
    assert len(statuses) - 1 >= 3  # 0,4 sn'lik beklemede 50 ms arayla birkaç işaret
    assert lines[-1]["text"] == "geç gelen cevap"


@respx.mock
def test_wait_line_carries_only_status_and_elapsed(fast_client):
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return ok_response()

    stream_route().mock(side_effect=slow)

    _, lines = ask(fast_client)

    waits = [line for line in lines if line["status"] == "wait"]
    assert waits
    assert all(set(line) == {"status", "elapsed"} for line in waits)


# ---------------------------------------------------------------- problem
FAILURES = {
    "quota_429": lambda: httpx.Response(429, content=QUOTA_BODY),
    "rate_429": lambda: httpx.Response(429, content=b'{"error":"prompt failed"}'),
    "session_401": lambda: httpx.Response(401, content=b'{"error":"unauthorized"}'),
    "server_500": lambda: httpx.Response(500, content=b"internal error"),
    "captcha_403": lambda: httpx.Response(403, content=b'{"error":"recaptcha validation failed"}'),
    "network": lambda: httpx.ConnectError("connection refused"),
    "timeout": lambda: httpx.ReadTimeout("read timed out"),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
@respx.mock
def test_every_serious_failure_looks_the_same_from_outside(client, failure):
    """Karşı sistem nedeni görmez: her arıza HTTP 200 + tek `problem` satırıdır."""
    outcome = FAILURES[failure]()
    if isinstance(outcome, Exception):
        stream_route().mock(side_effect=outcome)
    else:
        stream_route().mock(return_value=outcome)

    response, lines = ask(client)

    assert response.status_code == 200
    assert len(lines) == 1
    assert lines[0]["status"] == "problem"
    assert set(lines[0]) == {"status", "id"}
    # Gövdede arızanın türünü ele veren hiçbir şey yok. (İstek kimliği rastgele
    # harf/rakamdır; tesadüfen "429" içermesin diye karşılaştırmadan çıkarılır.)
    body = response.text.replace(lines[0]["id"], "").lower()
    assert body.split() == [":", "ok", "data:", '{"status":"problem","id":""}']
    for leak in ("quota", "429", "401", "403", "500", "limit", "captcha", "upstream",
                 "timeout", "unauthorized", "spend", "error"):
        assert leak not in body, leak


@respx.mock
def test_empty_answer_is_a_problem(client):
    """Boş cevap `done` sayılmaz: alarm sistemi boş metinle yetinmesin, yedeğe geçsin."""
    stream_route().mock(return_value=httpx.Response(200, content=ai_stream()))

    _, lines = ask(client)

    assert [line["status"] for line in lines] == ["problem"]
    assert client.app.state.upstream_health.snapshot()["code"] == "empty_response"


@respx.mock
def test_whitespace_only_answer_is_a_problem(client):
    stream_route().mock(return_value=ok_response("  \n", "\t "))

    _, lines = ask(client)

    assert [line["status"] for line in lines] == ["problem"]


@respx.mock
def test_request_deadline_turns_a_hanging_upstream_into_problem():
    """İstek başına süre sınırı (canlıda 180 sn) dolunca beklemek yerine `problem`."""

    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok_response()

    stream_route().mock(side_effect=hang)
    for client in make_client(ask_request_timeout_seconds=0.15):
        started = time.monotonic()
        _, lines = ask(client)
        elapsed = time.monotonic() - started

        assert [line["status"] for line in lines] == ["problem"]
        assert elapsed < 2.0
        assert client.app.state.upstream_health.snapshot()["code"] == "upstream_timeout"
        assert client.app.state.queue.snapshot()["active"] is None  # sıra bırakıldı


# ----------------------------------------------------- bilinen arıza (hold)
@pytest.mark.parametrize("failure", ["server_500", "session_401", "captcha_403", "timeout"])
@respx.mock
def test_known_failure_is_answered_without_going_upstream(client, failure):
    """Arıza biliniyorken yeni istek bekletilmez, upstream'e de gönderilmez.

    Kota (429) burada bilerek yok: onu hesap dinlenmesi zaten yerelde keser.
    Bekletme, öyle bir koruması olmayan arızalarda (5xx, 401, captcha, zaman
    aşımı) her isteğin aynı duvara tek tek çarpmasını önler.
    """
    outcome = FAILURES[failure]()
    if isinstance(outcome, Exception):
        route = stream_route().mock(side_effect=outcome)
    else:
        route = stream_route().mock(return_value=outcome)

    _, first = ask(client)
    started = time.monotonic()
    _, second = ask(client)
    _, third = ask(client)

    assert first[0]["status"] == second[0]["status"] == third[0]["status"] == "problem"
    assert route.call_count == 1
    assert time.monotonic() - started < 1.0


@respx.mock
def test_quota_failure_is_also_held(client):
    route = stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))

    ask(client)
    _, second = ask(client)

    assert second[0]["status"] == "problem"
    assert route.call_count == 1
    assert client.app.state.upstream_health.snapshot()["code"] == "upstream_quota_reached"


@respx.mock
def test_after_the_hold_one_request_is_tried_and_recovery_reopens():
    route = stream_route().mock(
        side_effect=[httpx.Response(429, content=QUOTA_BODY), ok_response("düzeldi"), ok_response()]
    )
    for client in make_client(ask_problem_hold_seconds=0.2):
        _, first = ask(client)
        _, held = ask(client)
        assert route.call_count == 1
        time.sleep(0.3)  # bekletme doldu
        _, trial = ask(client)
        _, after = ask(client)

        assert [first[0]["status"], held[0]["status"]] == ["problem", "problem"]
        assert (trial[0]["status"], trial[0]["text"]) == ("done", "düzeldi")
        assert after[0]["status"] == "done"
        assert route.call_count == 3
        assert client.app.state.upstream_health.failing is False


@respx.mock
def test_zero_hold_tries_every_request():
    route = stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))
    for client in make_client(ask_problem_hold_seconds=0):
        ask(client)
        ask(client)
        assert route.call_count == 2


@respx.mock
def test_chat_success_lifts_the_hold_for_ask(client):
    """Bekletme yalnız `/v1/ask`'i etkiler; başka bir isteğin başarısı onu kaldırır."""
    route = stream_route().mock(
        side_effect=[httpx.Response(429, content=QUOTA_BODY), ok_response(), ok_response("tamam")]
    )
    _, first = ask(client)
    assert first[0]["status"] == "problem"

    chat = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert chat.status_code == 200  # sohbet ucu bekletmeye takılmaz

    _, after = ask(client)
    assert after[0]["status"] == "done" and after[0]["text"] == "tamam"
    assert route.call_count == 3


@respx.mock
def test_chat_failure_also_protects_ask(client):
    """Telegram mesajı/probe arızayı gördüyse dış istek aynı duvara çarpmaz."""
    route = stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert chat.status_code == 429
    assert chat.json()["error"]["code"] == "upstream_quota_reached"  # sohbet ucu ayrıntı verir

    _, lines = ask(client)

    assert lines[0]["status"] == "problem"
    assert route.call_count == 1


# ------------------------------------------------- çağıranın kendi hataları
def test_missing_prompt_is_rejected_before_any_stream(client):
    response = client.post("/v1/ask", json={})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "prompt" in response.json()["error"]["message"]


def test_blank_prompt_is_rejected(client):
    response = client.post("/v1/ask", json={"prompt": "   "})
    assert response.status_code == 400


def test_prompt_and_messages_together_are_rejected(client):
    response = client.post(
        "/v1/ask",
        json={"prompt": "x", "messages": [{"role": "user", "content": "y"}]},
    )
    assert response.status_code == 400


def test_unknown_model_is_rejected_with_404(client):
    response = client.post("/v1/ask", json={"prompt": "x", "model": "ghost"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_api_key_is_required(client):
    response = client.post("/v1/ask", json={"prompt": "x"}, headers={"Authorization": ""})
    assert response.status_code == 401


@respx.mock
def test_callers_own_mistakes_do_not_count_as_an_outage(client):
    """Bozuk istek arıza kaydı açmaz, bekletme başlatmaz, upstream'e de ulaşmaz."""
    route = stream_route().mock(return_value=ok_response())
    alerter = attach_alerter(client)

    client.post("/v1/ask", json={})
    client.post("/v1/ask", json={"prompt": "x", "model": "ghost"})
    drain(client)

    assert route.call_count == 0
    assert alerter.policy.state.active_key is None
    assert client.app.state.upstream_health.failing is False

    # Arıza kaydı açılmadığı için sonraki sağlıklı istek "düzeldi" demez.
    _, lines = ask(client)
    drain(client)
    assert lines[0]["status"] == "done"
    assert not any("yeniden çalışıyor" in message for message in alerter.sent)


def test_wrong_api_key_is_reported_to_the_operator_once(client):
    """Karşı sistem yanlış anahtarla gelirse her sorusu sessizce yedeğe gider;
    operatör bunu ilk istekte öğrenmeli — ama her istekte değil."""
    alerter = attach_alerter(client)

    for _ in range(3):
        response = client.post(
            "/v1/ask", json={"prompt": "x"}, headers={"Authorization": "Bearer sk-yanlis"}
        )
        assert response.status_code == 401
    drain(client)

    assert len(alerter.sent) == 1
    message = alerter.sent[0]
    assert "REDDEDİLDİ" in message
    assert "[invalid_api_key]" in message
    assert "KURULUM HATASI" in message
    assert "HTTP 401" in message
    assert "API_KEYS" in message  # ne yapılacağı
    assert "sk-yanlis" not in message  # anahtar mesaja yazılmaz


def test_each_kind_of_rejection_is_reported_separately(client):
    alerter = attach_alerter(client)

    blank = client.post("/v1/ask", json={"prompt": "   "})
    unknown = client.post("/v1/ask", json={"prompt": "x", "model": "ghost"})
    keyless = client.post("/v1/ask", json={"prompt": "x"}, headers={"Authorization": ""})
    drain(client)

    assert (blank.status_code, unknown.status_code, keyless.status_code) == (400, 404, 401)
    assert len(alerter.sent) == 3
    joined = "\n---\n".join(alerter.sent)
    assert "İstek gövdesi geçersiz (400). [invalid_request]" in joined
    assert "'prompt' must be a non-empty string" in joined  # sunucunun kendi cevabı
    assert "[model_not_found]" in joined and "ASK_DEFAULT_MODEL" in joined
    assert "[missing_api_key]" in joined
    assert all("REDDEDİLDİ" in message for message in alerter.sent)


def test_repeated_rejections_are_counted_in_the_next_report():
    for client in make_client():
        alerter = attach_alerter(client)
        blank = {"prompt": " "}
        client.post("/v1/ask", json=blank)
        client.post("/v1/ask", json=blank)
        client.post("/v1/ask", json=blank)
        drain(client)
        assert len(alerter.sent) == 1

        # Bir saat geçmiş gibi: aynı tür yeniden yazılır ve aradakileri sayar.
        for code in list(alerter._event_sent_at):
            alerter._event_sent_at[code] -= 3601
        client.post("/v1/ask", json=blank)
        drain(client)

        assert len(alerter.sent) == 2
        assert "aynı türden 2 olay daha oldu" in alerter.sent[1]


def test_rejections_on_the_chat_endpoint_are_not_reported(client):
    """Bildirim yalnız `/v1/ask` içindir; sohbet ucunun istemcisi hatayı kendi görür."""
    alerter = attach_alerter(client)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "ghost", "messages": [{"role": "user", "content": "x"}]},
    )
    drain(client)

    assert response.status_code == 404
    assert alerter.sent == []


@respx.mock
def test_ask_also_answers_without_the_v1_prefix(client):
    """Taban adres `/v1` ile ya da `/v1`'siz verilmiş olsun, uç bulunur."""
    stream_route().mock(return_value=ok_response())

    response = client.post("/ask", json={"prompt": "x"})

    assert response.status_code == 200
    assert '"status":"done"' in response.text


def test_build_chat_request_shapes():
    settings = make_settings()
    plain = build_chat_request(AskRequest(prompt="soru"), settings)
    assert [(m.role, m.content) for m in plain.messages] == [("user", "soru")]
    assert plain.stream is False

    with_system = build_chat_request(AskRequest(prompt="soru", system="kısa yaz"), settings)
    assert [(m.role, m.content) for m in with_system.messages] == [
        ("system", "kısa yaz"),
        ("user", "soru"),
    ]

    from_messages = build_chat_request(
        AskRequest(messages=[{"role": "user", "content": "a"}]), settings
    )
    assert [(m.role, m.content) for m in from_messages.messages] == [("user", "a")]

    with pytest.raises(InvalidRequestError):
        build_chat_request(AskRequest(), settings)


# ------------------------------------------------------- Telegram bildirimi
@respx.mock
def test_operator_gets_the_details_the_caller_does_not(client):
    """Karşı tarafa yalnız `problem`; ne olduğu Telegram'a ayrıntısıyla gider."""
    stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))
    alerter = attach_alerter(client)

    _, lines = ask(client)
    drain(client)

    assert lines[0]["status"] == "problem"
    assert len(alerter.sent) == 1
    message = alerter.sent[0]
    assert 'karşı sisteme "problem" döndü' in message
    assert "[upstream_quota_reached]" in message
    assert "GEÇİCİ" in message
    # Etiket değil, upstream'in kendi cevabı (18 Kasım dersi).
    assert "HTTP 429" in message
    assert "daily spend limit reached" in message
    assert lines[0]["id"] in message
    assert "Bundan sonra" in message  # bekletmenin ne kadar süreceği


@respx.mock
def test_session_death_is_flagged_as_permanent(client):
    stream_route().mock(return_value=httpx.Response(401, content=b'{"error":"unauthorized"}'))
    alerter = attach_alerter(client)

    ask(client)
    drain(client)

    assert len(alerter.sent) == 1
    assert "[upstream_unauthorized]" in alerter.sent[0]
    assert "KALICI" in alerter.sent[0]
    assert "🔴" in alerter.sent[0]


@respx.mock
def test_one_message_per_incident_then_a_recovery_with_the_count():
    """Olay başına tek mesaj: süren arızada istekler sayılır, düzelince tek özet gelir."""
    stream_route().mock(
        side_effect=[httpx.Response(429, content=QUOTA_BODY), ok_response(), ok_response()]
    )
    for client in make_client(ask_problem_hold_seconds=0.3):
        alerter = attach_alerter(client)

        ask(client)  # arıza → mesaj
        ask(client)  # bekletme → sayılır, mesaj yok
        ask(client)  # bekletme → sayılır, mesaj yok
        drain(client)
        assert len(alerter.sent) == 1

        time.sleep(0.4)
        _, recovered = ask(client)  # deneme başarılı → "düzeldi"
        drain(client)
        ask(client)  # sağlıklı → sessiz
        drain(client)

        assert recovered[0]["status"] == "done"
        assert len(alerter.sent) == 2
        assert "yeniden çalışıyor" in alerter.sent[1]
        assert '"problem" cevabı alan dış istek: 3' in alerter.sent[1]


@respx.mock
def test_secrets_in_the_upstream_body_never_reach_telegram(client):
    token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9Abc123Def456Ghi789Jkl012Mno345"
    stream_route().mock(
        return_value=httpx.Response(500, content=f"failure for session {token}".encode())
    )
    alerter = attach_alerter(client)

    ask(client)
    drain(client)

    assert len(alerter.sent) == 1
    assert token not in alerter.sent[0]
    assert "[gizlendi]" in alerter.sent[0]


@respx.mock
def test_healthy_requests_send_nothing(client):
    stream_route().mock(return_value=ok_response())
    alerter = attach_alerter(client)

    for _ in range(3):
        ask(client)
    drain(client)

    assert alerter.sent == []


@respx.mock
def test_without_an_alerter_the_endpoint_still_works(client):
    stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))
    assert client.app.state.alerter is None  # testlerde Telegram yapılandırılmamış

    _, lines = ask(client)

    assert lines[0]["status"] == "problem"


# --------------------------------------------------------------- yönetim
@respx.mock
def test_admin_queue_endpoint_shows_why_requests_get_problem(client):
    stream_route().mock(return_value=httpx.Response(429, content=QUOTA_BODY))
    ask(client)

    response = client.get("/v1/admin/queue")

    assert response.status_code == 200
    body = response.json()
    assert body["upstream_health"]["failing"] is True
    assert body["upstream_health"]["code"] == "upstream_quota_reached"
    assert body["upstream_health"]["hold_remaining_seconds"] > 0
    assert body["queue"]["enabled"] is True
    assert body["queue"]["active"] is None
    assert body["ask"]["request_timeout_seconds"] == 180.0
    assert body["ask"]["first_wait_seconds"] == 30.0
    assert body["ask"]["wait_interval_seconds"] == 15.0
    assert body["alerts"] == {"enabled": False}


def test_admin_queue_endpoint_requires_the_api_key(client):
    assert client.get("/v1/admin/queue", headers={"Authorization": ""}).status_code == 401


def test_metrics_describe_the_new_series(client):
    text = client.get("/metrics").text
    assert "apiwrapper_request_queue_waiting" in text
