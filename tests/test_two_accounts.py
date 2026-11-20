"""İki hesap (`UPSTREAM_CURL_2`): bölüşüm, arıza ve bildirimler — HTTP üzerinden.

Upstream respx ile taklit edilir; hesaplar `Authorization` başlığındaki token'la
ayırt edilir. Eşzamanlılık ve zamanlama (hesap başına sıra, aralık, probe)
gerçek soketlerde `tests/test_two_accounts_live.py` içinde sınanır.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import TEST_API_KEY, ai_stream, make_settings
from tests.test_ask import STREAM_URL, RecordingAlerter, ask, attach_alerter, drain

TOKENS = {"TOKEN-ONE": "bir", "TOKEN-TWO": "iki"}
#: Kota akışını tetikleyen gövde. Varsayılan işaret listesinde (canlıdakiyle aynı
#: liste) bulunan bir ifade taşır; 18 Kasım'ın `daily spend limit reached` gövdesi
#: varsayılanda kota sayılmaz (bkz. `tests/test_queue_integration.py`).
QUOTA_BODY = b'{"error":"daily limit reached"}'


def ok(text: str = "cevap") -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, content=ai_stream(text))


def fail(status: int, body: bytes = b"") -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, content=body)


class Upstream:
    """Hesaba göre cevap veren sahte upstream; hangi hesabın çağrıldığını sırayla tutar."""

    def __init__(self) -> None:
        self.responses: dict[str, Callable[[], httpx.Response]] = {
            "bir": ok("birinci hesabın cevabı"),
            "iki": ok("ikinci hesabın cevabı"),
        }
        self.seen: list[str] = []
        self.paths: list[tuple[str, str]] = []
        respx.post(url__startswith=STREAM_URL).mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        name = TOKENS[token]
        self.seen.append(name)
        self.paths.append((name, request.url.path))
        return self.responses[name]()

    def count(self, name: str) -> int:
        return self.seen.count(name)


def make_client(**overrides) -> Iterator[TestClient]:
    settings = make_settings(
        upstream_access_token="TOKEN-ONE",
        upstream_account_name="bir",
        upstream_access_token_2="TOKEN-TWO",
        upstream_account_2_name="iki",
        **overrides,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


@pytest.fixture
def duo() -> Iterator[TestClient]:
    yield from make_client()


def statuses(client: TestClient, count: int) -> list[str]:
    return [ask(client)[1][-1]["status"] for _ in range(count)]


def lane(client: TestClient, slot: int):
    return client.app.state.lanes.get(slot)


def chat(client: TestClient, **kwargs) -> httpx.Response:
    return client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
        **kwargs,
    )


# ------------------------------------------------------------------ bölüşüm
@respx.mock
def test_requests_are_split_between_the_two_accounts(duo):
    """Dört istek: ikisi bir hesaba, ikisi öbürüne; sırayla."""
    upstream = Upstream()

    assert statuses(duo, 4) == ["done"] * 4

    assert upstream.seen == ["bir", "iki", "bir", "iki"]
    assert [row["assigned_total"] for row in duo.app.state.lanes.snapshot()] == [2, 2]


@respx.mock
def test_the_caller_cannot_tell_which_account_answered(duo):
    """Sözleşme değişmez: `done` satırı hesap hakkında hiçbir şey taşımaz."""
    Upstream()

    first = ask(duo)[1][-1]
    second = ask(duo)[1][-1]

    for line in (first, second):
        assert sorted(line) == ["elapsed", "id", "model", "status", "text"]
    assert first["text"] == "birinci hesabın cevabı"
    assert second["text"] == "ikinci hesabın cevabı"


@respx.mock
def test_chat_and_stream_requests_are_split_too(duo):
    upstream = Upstream()

    assert chat(duo).status_code == 200
    with duo.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "selam"}],
        },
    ) as response:
        body = "".join(response.iter_text())

    assert "ikinci hesabın cevabı" in body and "[DONE]" in body
    assert upstream.seen == ["bir", "iki"]
    # Akış bitince hesabın sırası bırakılmış olmalı.
    assert all(row["queue"]["active"] is None for row in duo.app.state.lanes.snapshot())


# -------------------------------------------------- hesaba özgü ret: taşıma
@respx.mock
def test_dead_account_is_bypassed_without_the_caller_noticing(duo):
    """Oturumu düşen hesabın isteği öbür hesaptan cevaplanır; ayrıntı operatöre gider."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(401, b'{"error":"Unauthorized"}')
    alerter = attach_alerter(duo)

    _, lines = ask(duo)
    drain(duo)

    assert [line["status"] for line in lines] == ["done"]
    assert lines[-1]["text"] == "ikinci hesabın cevabı"
    assert upstream.seen == ["bir", "iki"]
    assert lane(duo, 1).health.code == "upstream_unauthorized"
    assert lane(duo, 2).health.failing is False

    assert len(alerter.sent) == 1
    message = alerter.sent[0]
    assert "bir hesap cevap veremedi; istek diğer hesaba taşındı" in message
    assert "Hesap: bir" in message
    assert "[upstream_unauthorized]" in message
    assert "Tür: KALICI" in message
    assert 'Upstream cevabı: HTTP 401 · {"error":"Unauthorized"}' in message
    assert 'iki hesabından cevaplandı; karşı sisteme "problem" dönmedi' in message
    assert "yeni istekler çalışan hesaptan cevaplanır" in message
    assert "UPSTREAM_CURL değişkenindedir" in message
    assert "bütün hesapların oturumunu sıfırlar" in message
    assert "Hesaplar: bir ARIZALI [upstream_unauthorized] · iki çalışıyor" in message
    # Karşı sistem "problem" görmedi: sayılmaz.
    assert alerter.policy_for("bir").state.ask_problems == 0

    # Sonraki istekler arızalı hesaba hiç gitmez; yeni mesaj da üretmez.
    assert statuses(duo, 3) == ["done"] * 3
    drain(duo)
    assert upstream.count("bir") == 1
    assert upstream.count("iki") == 4
    assert len(alerter.sent) == 1


@respx.mock
def test_quota_on_one_account_moves_the_request_to_the_other(duo):
    upstream = Upstream()
    upstream.responses["bir"] = fail(429, QUOTA_BODY)
    alerter = attach_alerter(duo)

    assert statuses(duo, 2) == ["done", "done"]
    drain(duo)

    assert upstream.seen == ["bir", "iki", "iki"]
    assert lane(duo, 1).health.code == "upstream_quota_reached"
    assert duo.app.state.lanes.cooling(lane(duo, 1)) > 0  # hesap dinlenmeye alındı
    assert len(alerter.sent) == 1
    assert "daily limit reached" in alerter.sent[0]
    assert "Tür: GEÇİCİ" in alerter.sent[0]
    assert "UPSTREAM_CURL değişkenindedir" not in alerter.sent[0]  # deploy notu yalnız kalıcıda


@respx.mock
def test_second_account_failing_names_its_own_variable(duo):
    upstream = Upstream()
    upstream.responses["iki"] = fail(401)
    alerter = attach_alerter(duo)

    assert statuses(duo, 2) == ["done", "done"]  # ikinci istek 2. hesaptan 1.'ye taşındı
    drain(duo)

    assert upstream.seen == ["bir", "iki", "bir"]
    assert "Hesap: iki" in alerter.sent[0]
    assert "UPSTREAM_CURL_2 değişkenindedir" in alerter.sent[0]


@respx.mock
def test_request_fails_when_the_other_account_rejects_it_too(duo):
    """İki hesap da reddederse istek biter: her hesap bir kez denenir, fazlası değil."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(401)
    upstream.responses["iki"] = fail(401)
    alerter = attach_alerter(duo)

    assert statuses(duo, 1) == ["problem"]
    drain(duo)

    assert upstream.seen == ["bir", "iki"]
    assert sorted(message.split("\n")[1] for message in alerter.sent) == [
        "Hesap: bir",
        "Hesap: iki",
    ]
    moved = next(message for message in alerter.sent if "Hesap: bir" in message)
    assert "iki hesabına taşındı ama orada da cevaplanamadı" in moved
    failed = next(message for message in alerter.sent if "Hesap: iki" in message)
    assert 'karşı sisteme "problem" döndü' in failed
    assert "çalışan hesap kalmadı" in failed
    assert alerter.policy_for("iki").state.ask_problems == 1
    assert alerter.policy_for("bir").state.ask_problems == 0


@respx.mock
def test_chat_request_moved_to_another_account_is_reported_to_the_operator(duo):
    """Telegram mesajı normal cevap alır; operatör yine de hesabın düştüğünü öğrenir."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(401)
    alerter = attach_alerter(duo)

    response = chat(duo)
    drain(duo)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ikinci hesabın cevabı"
    assert len(alerter.sent) == 1
    assert "Hesap: bir" in alerter.sent[0]
    assert "iki hesabından cevaplandı; istemciye hata dönmedi" in alerter.sent[0]


# ------------------------------------------ taşınmayan arıza: hesap atlanır
@respx.mock
def test_other_failures_cost_one_problem_then_the_account_is_avoided(duo):
    """Hesaba özgü olmayan arızada (5xx, zaman aşımı) istek taşınmaz; sonrakiler atlar."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(500, b"boom")
    alerter = attach_alerter(duo)

    assert statuses(duo, 4) == ["problem", "done", "done", "done"]
    drain(duo)

    assert upstream.seen == ["bir", "iki", "iki", "iki"]
    assert len(alerter.sent) == 1
    message = alerter.sent[0]
    assert 'dış istek cevaplanamadı; karşı sisteme "problem" döndü' in message
    assert "Hesap: bir" in message
    assert "yeni istekler çalışan hesaptan cevaplanır, karşı sistem etkilenmez" in message
    assert "bir yeni iş almaz" in message
    assert alerter.policy_for("bir").state.ask_problems == 1
    # Tek hesaplı kurulumun ortak kaydı kullanılmaz.
    assert alerter.policy.state.active_key is None


@respx.mock
def test_empty_answer_sidelines_the_account_that_gave_it(duo):
    upstream = Upstream()
    upstream.responses["iki"] = ok("   ")

    assert statuses(duo, 4) == ["done", "problem", "done", "done"]

    assert lane(duo, 2).health.code == "empty_response"
    assert lane(duo, 1).health.failing is False
    assert upstream.seen == ["bir", "iki", "bir", "bir"]


@respx.mock
def test_problem_at_once_when_no_account_works(duo):
    """Cevap verebilecek hesap kalmadıysa istek upstream'e gitmeden "problem" alır."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(500, b"boom")
    upstream.responses["iki"] = fail(503, b"down")
    alerter = attach_alerter(duo)

    assert statuses(duo, 4) == ["problem"] * 4
    drain(duo)

    assert upstream.seen == ["bir", "iki"]  # üçüncü ve dördüncü istek hiç gönderilmedi
    assert len(alerter.sent) == 2  # hesap başına tek mesaj; bekletilenler yalnız sayılır
    assert "çalışan hesap kalmadı" in alerter.sent[1]
    assert "Hesaplar: bir ARIZALI [upstream_failure] · iki ARIZALI" in alerter.sent[1]
    counted = sum(policy.state.ask_problems for policy in alerter.scopes.values())
    assert counted == 4


@respx.mock
def test_recovery_is_reported_for_the_account_that_recovered(duo):
    upstream = Upstream()
    upstream.responses["bir"] = fail(500, b"boom")
    alerter = attach_alerter(duo)
    assert statuses(duo, 1) == ["problem"]

    # Hesap düzeldi; onu geri getiren, hesaba sabitlenmiş bir istektir (probe gibi).
    upstream.responses["bir"] = ok("yine çalışıyorum")
    assert chat(duo, headers={"x-apiwrapper-account": "1"}).status_code == 200
    assert lane(duo, 1).health.failing is False

    assert statuses(duo, 2) == ["done", "done"]
    drain(duo)

    assert upstream.seen[-2:] in (["bir", "iki"], ["iki", "bir"])  # yeniden bölüşülüyor
    recovered = [message for message in alerter.sent if message.startswith("✅")]
    assert len(recovered) == 1
    assert "hesap yeniden çalışıyor" in recovered[0]
    assert "Hesap: bir" in recovered[0]
    assert 'Arıza boyunca "problem" cevabı alan dış istek: 1' in recovered[0]
    assert alerter.policy_for("bir").state.active_key is None


@respx.mock
def test_upstream_errors_are_remembered_per_account(duo):
    """Hesaplar aynı anda çalışır: bir hesabın bildirimine öbürünün hatası karışmamalı."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(500, b"birinci hesabin hatasi")
    upstream.responses["iki"] = fail(503, b"ikinci hesabin hatasi")

    assert statuses(duo, 2) == ["problem", "problem"]

    client = duo.app.state.upstream
    first, second = client.error_since(0.0, 1), client.error_since(0.0, 2)
    assert (first.status, first.detail) == (500, "birinci hesabin hatasi")
    assert (second.status, second.detail) == (503, "ikinci hesabin hatasi")
    assert client.error_since(0.0) is second  # hesap verilmezse en son görülen hata
    assert client.error_since(0.0, 7) is None


# ------------------------------------------------------------ katı bölüşüm
@respx.mock
def test_strict_split_gives_problem_for_the_failing_accounts_share():
    """`ACCOUNT_FAILOVER=false`: arızalı hesabın payı öbür hesaba verilmez."""
    for client in make_client(account_failover=False):
        upstream = Upstream()
        upstream.responses["bir"] = fail(401)
        alerter = attach_alerter(client)

        assert statuses(client, 6) == ["problem", "done", "problem", "done", "problem", "done"]
        drain(client)

        assert upstream.seen == ["bir", "iki", "iki", "iki"]  # istek taşınmadı, hesap atlanmadı
        assert len(alerter.sent) == 1
        assert "hesabının payına düşen" in alerter.sent[0]
        assert "ACCOUNT_FAILOVER kapalı" in alerter.sent[0]
        assert alerter.policy_for("bir").state.ask_problems == 3


# ---------------------------------------------------------------- sabitleme
@respx.mock
def test_account_header_pins_a_chat_request(duo):
    upstream = Upstream()

    for _ in range(3):
        assert chat(duo, headers={"x-apiwrapper-account": "2"}).status_code == 200

    assert upstream.seen == ["iki", "iki", "iki"]


@respx.mock
def test_pinned_request_is_never_moved_to_another_account(duo):
    """Probe kendi hesabını sınar: reddedilirse öbür hesaptan cevaplanmaz."""
    upstream = Upstream()
    upstream.responses["bir"] = fail(401)

    response = chat(duo, headers={"x-apiwrapper-account": "1"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_unauthorized"
    assert upstream.seen == ["bir"]
    assert lane(duo, 1).health.failing is True


@respx.mock
def test_unknown_or_malformed_account_header_is_a_client_error(duo):
    upstream = Upstream()

    missing = chat(duo, headers={"x-apiwrapper-account": "3"})
    malformed = chat(duo, headers={"x-apiwrapper-account": "ikinci"})

    assert missing.status_code == 400
    assert missing.json()["error"]["param"] == "x-apiwrapper-account"
    assert malformed.status_code == 400
    assert upstream.seen == []
    assert all(not row["upstream_health"]["failing"] for row in duo.app.state.lanes.snapshot())


@respx.mock
def test_ask_ignores_the_account_header(duo):
    """Karşı sistem hesap seçemez: `/v1/ask` başlığı yok sayar, bölüşüm sürer."""
    upstream = Upstream()

    for _ in range(4):
        ask(duo, headers={"x-apiwrapper-account": "2"})

    assert upstream.seen == ["bir", "iki", "bir", "iki"]


# -------------------------------------------------------- sohbet bağlılığı
@respx.mock
def test_reused_conversation_stays_on_one_account():
    """`SESSION_REUSE` açıkken aynı istemcinin sohbeti tek hesapta, tek sohbette kalır."""
    for client in make_client(session_reuse=True, api_keys=[TEST_API_KEY, "sk-other-client"]):
        upstream = Upstream()

        for _ in range(3):
            assert chat(client).status_code == 200
        other = {"Authorization": "Bearer sk-other-client"}
        for _ in range(2):
            assert chat(client, headers=other).status_code == 200

        assert upstream.seen == ["bir", "bir", "bir", "iki", "iki"]
        assert len({path for name, path in upstream.paths if name == "bir"}) == 1
        assert len({path for name, path in upstream.paths if name == "iki"}) == 1
        # `/v1/ask` soruları bağımsızdır: sohbete bağlanmaz, bölüşmeye devam eder.
        ask(client)
        ask(client)
        assert sorted(upstream.seen[-2:]) == ["bir", "iki"]


# -------------------------------------------------------------- yönetim ucu
@respx.mock
def test_admin_queue_lists_each_account(duo):
    upstream = Upstream()
    upstream.responses["iki"] = fail(500, b"boom")
    attach_alerter(duo)
    statuses(duo, 3)
    drain(duo)

    body = duo.get("/v1/admin/queue").json()

    accounts = body["accounts"]
    assert accounts["count"] == 2
    assert accounts["failover"] is True
    rows = accounts["lanes"]
    assert [row["account"] for row in rows] == ["bir", "iki"]
    assert [row["usable"] for row in rows] == [True, False]
    assert [row["assigned_total"] for row in rows] == [2, 1]
    assert rows[1]["upstream_health"]["code"] == "upstream_failure"
    assert rows[0]["queue"]["served_total"] == 2
    assert body["alerts"]["accounts"]["iki"]["active_code"] == "upstream_failure"
    # Eski alanlar yerinde: ilk hesabın sırası ve durumu.
    assert body["queue"]["served_total"] == 2
    assert body["upstream_health"]["failing"] is False


@respx.mock
def test_admin_resume_puts_a_sidelined_account_back_to_work(duo):
    upstream = Upstream()
    upstream.responses["bir"] = fail(500, b"boom")
    assert statuses(duo, 2) == ["problem", "done"]
    upstream.responses["bir"] = ok()

    resumed = duo.post("/v1/admin/accounts/1/resume").json()

    assert resumed == {
        "slot": 1,
        "name": "bir",
        "was_failing": True,
        "cleared_code": "upstream_failure",
        "usable": True,
    }
    statuses(duo, 2)
    assert sorted(upstream.seen[-2:]) == ["bir", "iki"]
    assert duo.post("/v1/admin/accounts/9/resume").status_code == 400


# ------------------------------------------------------------- tek hesap
@respx.mock
def test_single_account_messages_carry_no_account_lines(client):
    """Tek hesaplı kurulumda metinler değişmez: `Hesap:` / `Hesaplar:` satırı yoktur."""
    respx.post(url__startswith=STREAM_URL).mock(return_value=httpx.Response(500, content=b"x"))
    alerter = RecordingAlerter()
    client.app.state.alerter = alerter

    assert statuses(client, 2) == ["problem", "problem"]
    drain(client)

    assert len(alerter.sent) == 1
    assert "Hesap" not in alerter.sent[0]
    assert "Bundan sonra: dış isteklere 5 dk boyunca" in alerter.sent[0]
    assert alerter.policy.state.ask_problems == 2
    body = client.get("/v1/admin/queue").json()
    assert body["accounts"]["count"] == 1
    assert "accounts" not in body["alerts"]
