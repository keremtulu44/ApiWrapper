"""`/v1/ask` ve istek sırası — gerçek sunucu, gerçek soket, gerçek zaman.

Buradaki her test `tests/live.py` ile iki uvicorn sunucusu (sahte upstream +
wrapper) ayağa kaldırır. Süreler canlıdaki değerlerin küçültülmüş halidir
(30 sn → 0,3 sn gibi); oranlar ve sıralama aynıdır. Zaman karşılaştırmaları
yavaş CI makineleri için bilerek geniş tutulmuştur: alt sınırlar kesin (erken
gelmemeli), üst sınırlar gevşektir.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import app.services.request_queue as queue_module
from app.services.alerting import TelegramAlerter
from tests.live import LiveWrapper, UpstreamScript, live_wrapper, wait_until

#: Kısaltılmış işaret süreleri: ilk `wait` 0,3 sn'de, sonra 0,2 sn'de bir.
SIGNALS = {"ask_first_wait_seconds": 0.3, "ask_wait_interval_seconds": 0.2}
#: İşaret istemeyen testler için: `wait` hiç gelmesin.
QUIET = {"ask_first_wait_seconds": 30.0, "ask_wait_interval_seconds": 15.0}


def ask(
    wrapper: LiveWrapper,
    prompt: str = "soru",
    *,
    give_up_after: float | None = None,
    path: str = "/v1/ask",
    payload: dict | None = None,
) -> list[tuple[float, dict]]:
    """İsteği atar; her `data:` satırını geliş anıyla (saniye) birlikte döndürür.

    `give_up_after` verilirse istemci o kadar sessizlikten sonra bağlantıyı
    kapatır (karşı sistemin "beklemekten vazgeçmesi") ve son öğe olarak
    `{"status": "<gave-up>"}` eklenir.
    """
    started = time.monotonic()
    seen: list[tuple[float, dict]] = []
    read_timeout = give_up_after if give_up_after is not None else 15.0
    body = payload if payload is not None else {"prompt": prompt}
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(read_timeout, connect=5.0)) as client,
            client.stream(
                "POST", wrapper.base_url + path, json=body, headers=wrapper.headers
            ) as response,
        ):
            assert response.status_code == 200, response.read()
            assert "content-encoding" not in response.headers  # sıkıştırma = tamponlama
            for line in response.iter_lines():
                if line.startswith("data:") and line[5:].strip() != "[DONE]":
                    seen.append((time.monotonic() - started, json.loads(line[5:])))
    except httpx.ReadTimeout:
        seen.append((time.monotonic() - started, {"status": "<gave-up>"}))
    return seen


def statuses(lines: list[tuple[float, dict]]) -> list[str]:
    return [line["status"] for _, line in lines]


def together(wrapper: LiveWrapper, count: int, stagger: float = 0.04, **kwargs):
    """`count` isteği arka arkaya (geliş sırası belli olacak aralıkla) başlatır."""
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = []
        for index in range(count):
            futures.append(pool.submit(ask, wrapper, f"soru {index}", **kwargs))
            time.sleep(stagger)
        return [future.result(timeout=30) for future in futures]


def queue_idle(wrapper: LiveWrapper) -> bool:
    snapshot = wrapper.app.state.queue.snapshot()
    return snapshot["active"] is None and snapshot["waiting"] == 0


class RecordingAlerter(TelegramAlerter):
    def __init__(self) -> None:
        super().__init__("123:test-token", 42)
        self.sent: list[str] = []

    async def send(self, text: str, *, label: str | None = None) -> bool:
        self.sent.append(text)
        return True


# ------------------------------------------------------------- işaretler
def test_fast_answer_arrives_without_any_wait_line():
    """Cevap ilk işaret süresinden önce gelirse tek satır gelir: `done`."""
    with live_wrapper(
        UpstreamScript(delay=0.05), ask_first_wait_seconds=3.0, ask_wait_interval_seconds=1.0
    ) as wrapper:
        lines = ask(wrapper)

    assert statuses(lines) == ["done"]
    assert lines[0][1]["text"] == "merhaba dünya"


def test_wait_lines_arrive_on_schedule_while_the_answer_is_pending():
    """30 sn'de ilk `wait`, sonra 15 sn'de bir (burada 0,3 sn ve 0,2 sn).

    İstemci varsayılan olarak `Accept-Encoding: gzip` gönderir; satırlar yine de
    tamponlanmadan, üretildikleri anda ulaşmalıdır.
    """
    with live_wrapper(UpstreamScript(delay=1.0), **SIGNALS) as wrapper:
        lines = ask(wrapper)

    kinds = statuses(lines)
    assert kinds[-1] == "done" and kinds.count("done") == 1
    waits = [at for at, line in lines if line["status"] == "wait"]
    assert len(waits) >= 3
    # İlki ayarlanan süreden erken gelmez.
    assert waits[0] >= 0.28
    # Aralıklar ayarlanan süreden kısa olamaz.
    gaps = [later - earlier for earlier, later in zip(waits, waits[1:], strict=False)]
    assert all(gap >= 0.15 for gap in gaps), gaps
    # Tamponlanmadı: ilk işaret, cevaptan belirgin biçimde önce ulaştı.
    done_at = lines[-1][0]
    assert done_at >= 0.95
    assert waits[0] < done_at - 0.4


def test_time_spent_queued_also_produces_wait_lines():
    """Sırada bekleyen istek de sessiz kalmaz: karşı sistem "asıldı" sanmasın."""
    with live_wrapper(UpstreamScript(delay=0.6), **SIGNALS) as wrapper:
        first, second = together(wrapper, 2)

    assert statuses(first)[-1] == "done"
    assert statuses(second)[-1] == "done"
    # İkinci istek ~0,6 sn sırada + 0,6 sn işlemde: işaret almış olmalı.
    assert statuses(second).count("wait") >= 2


# ------------------------------------------------------------------ sıra
def test_four_requests_in_a_row_are_served_one_by_one_with_a_gap():
    """Kullanıcının tarifi: ilki alınır, diğerleri sırada; her cevaptan sonra
    rastgele bekleme (canlıda 5–10 sn, burada 0,2–0,3 sn), sonra sıradaki."""
    script = UpstreamScript(delay=0.15)
    with live_wrapper(
        script, request_gap_min_seconds=0.2, request_gap_max_seconds=0.3, **QUIET
    ) as wrapper:
        results = together(wrapper, 4)

    assert [statuses(lines) for lines in results] == [["done"]] * 4
    assert script.calls == 4
    assert script.max_concurrent == 1  # hiçbir an iki istek birden işlenmedi
    # Geliş sırasıyla cevaplandılar.
    finished = [lines[-1][0] + index * 0.04 for index, lines in enumerate(results)]
    assert finished == sorted(finished)
    # Ardışık iki upstream isteği arasında: işlem (0,15) + en az 0,2 sn bekleme.
    starts = script.started_at
    spacing = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert all(space >= 0.34 for space in spacing), spacing
    assert all(space <= 1.5 for space in spacing), spacing


def test_chat_endpoint_shares_the_same_queue():
    """Herkes aynı sırada: `/v1/chat/completions` istekleri de tek tek işlenir."""
    script = UpstreamScript(delay=0.3)
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    with live_wrapper(script, **QUIET) as wrapper, ThreadPoolExecutor(max_workers=3) as pool:
        plain = pool.submit(
            httpx.post,
            wrapper.base_url + "/v1/chat/completions",
            json=chat,
            headers=wrapper.headers,
            timeout=15,
        )
        time.sleep(0.05)
        streamed = pool.submit(
            ask, wrapper, path="/v1/chat/completions", payload={**chat, "stream": True}
        )
        time.sleep(0.05)
        asked = pool.submit(ask, wrapper)

        assert plain.result(timeout=15).status_code == 200
        assert streamed.result(timeout=15)  # içerik parçaları geldi
        assert statuses(asked.result(timeout=15)) == ["done"]

    assert script.calls == 3
    assert script.max_concurrent == 1


def test_queue_full_is_a_problem_for_the_caller_and_one_note_for_the_operator():
    script = UpstreamScript(delay=0.6)
    with live_wrapper(script, request_queue_max_waiting=1, **QUIET) as wrapper:
        alerter = RecordingAlerter()
        wrapper.app.state.alerter = alerter
        results = together(wrapper, 4, stagger=0.08)
        assert wait_until(lambda: len(wrapper.app.state.tasks) == 0)
        health = wrapper.app.state.upstream_health.snapshot()

    kinds = [statuses(lines) for lines in results]
    assert kinds[0] == ["done"] and kinds[1] == ["done"]
    assert kinds[2] == ["problem"] and kinds[3] == ["problem"]
    # Geri çevrilenler beklemeden (öndeki istek bitmeden) cevap aldı.
    assert results[2][-1][0] < 0.5 and results[3][-1][0] < 0.5
    assert script.calls == 2
    # Yük sınırı bir upstream arızası değildir: bekletme başlamaz.
    assert health["failing"] is False
    # İki olay, tek bildirim (aynı tür saatte bir yazılır).
    assert len(alerter.sent) == 1
    assert "[queue_full]" in alerter.sent[0]


# ------------------------------------------------------------ süre sınırı
def test_deadline_is_per_request_and_does_not_include_queue_time():
    """Süre sınırı (canlıda 180 sn) sıra gelince başlar: bekleyen istek cezalandırılmaz."""
    script = UpstreamScript(delay=0.35)
    with live_wrapper(script, ask_request_timeout_seconds=0.7, **QUIET) as wrapper:
        results = together(wrapper, 3)

    # Üçüncü istek ~0,7 sn sırada bekledi + 0,35 sn işlendi: toplamı sınırı aşar,
    # ama kendi işlem süresi sınırın altındadır.
    assert [statuses(lines) for lines in results] == [["done"]] * 3
    assert results[2][-1][0] > 0.7


def test_request_over_its_deadline_becomes_problem_and_upstream_is_dropped():
    script = UpstreamScript(delay=5.0)
    with live_wrapper(script, ask_request_timeout_seconds=0.4, **SIGNALS) as wrapper:
        lines = ask(wrapper)
        dropped = wait_until(lambda: script.cancelled == 1)
        idle = wait_until(lambda: queue_idle(wrapper))
        health = wrapper.app.state.upstream_health.snapshot()

    assert statuses(lines)[-1] == "problem"
    assert 0.38 <= lines[-1][0] <= 1.5
    assert "wait" in statuses(lines)  # 0,3 sn'de bir işaret gelmişti
    assert dropped  # wrapper upstream bağlantısını kapattı; istek arkada sürmüyor
    assert idle
    assert health["code"] == "upstream_timeout"


# ----------------------------------------------------- arıza anında sıra
def test_queued_requests_get_problem_at_once_when_the_one_ahead_fails():
    """Öndeki istek arızayı ortaya çıkarınca bekleyenler tek tek denenmez."""
    script = UpstreamScript(delay=0.4, status=500, body="internal error")
    with live_wrapper(script, **QUIET) as wrapper:
        results = together(wrapper, 3)

    assert [statuses(lines) for lines in results] == [["problem"]] * 3
    assert script.calls == 1
    # Bekleyenler, öndekinin hatasından hemen sonra (yeniden denenmeden) cevap aldı.
    assert results[1][-1][0] < 1.2 and results[2][-1][0] < 1.2


def test_only_one_request_probes_upstream_after_the_hold():
    """Bekletme dolunca tek istek denenir; o sürerken gelenler hemen `problem` alır."""
    script = UpstreamScript(status=500, body="internal error")
    with live_wrapper(script, ask_problem_hold_seconds=0.3, **QUIET) as wrapper:
        assert statuses(ask(wrapper)) == ["problem"]
        assert script.calls == 1
        time.sleep(0.4)  # bekletme doldu
        script.status, script.delay = 200, 0.5  # upstream düzeldi ama yavaş

        trial, bystander = together(wrapper, 2, stagger=0.1)
        after = ask(wrapper)

    assert statuses(trial) == ["done"]
    assert statuses(bystander) == ["problem"]
    assert bystander[-1][0] < 0.35  # deneme (0,5 sn) bitmeden, beklemeden
    assert statuses(after) == ["done"]  # deneme başarılı: kapı herkese açık
    assert script.calls == 3


# ------------------------------------------------------ istemci vazgeçerse
def test_caller_that_gives_up_frees_the_queue_and_upstream():
    """Karşı sistem beklemekten vazgeçip yedeğine geçtiyse iş arkada sürmez."""
    script = UpstreamScript(delay=5.0)
    with live_wrapper(script, **QUIET) as wrapper:
        lines = ask(wrapper, give_up_after=0.3)
        assert statuses(lines) == ["<gave-up>"]
        freed = wait_until(lambda: queue_idle(wrapper))
        dropped = wait_until(lambda: script.cancelled == 1)
        health = wrapper.app.state.upstream_health.snapshot()
        # Sıra gerçekten boş: sonraki istek hemen işlenir.
        script.delay = 0.05
        following = ask(wrapper)

    assert freed and dropped
    assert health["failing"] is False  # istemcinin vazgeçmesi arıza değildir
    assert statuses(following) == ["done"]


def test_caller_that_gives_up_while_queued_is_never_sent_upstream():
    script = UpstreamScript(delay=0.8)
    with live_wrapper(script, **QUIET) as wrapper, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(ask, wrapper, "birinci")
        time.sleep(0.1)
        second = ask(wrapper, "ikinci", give_up_after=0.3)
        assert statuses(second) == ["<gave-up>"]
        assert wait_until(lambda: wrapper.app.state.queue.snapshot()["waiting"] == 0)

        assert statuses(first.result(timeout=15)) == ["done"]
        assert wait_until(lambda: queue_idle(wrapper))
        time.sleep(0.2)
        calls = script.calls

    assert calls == 1  # vazgeçilen istek sırası gelse de işlenmedi


def test_non_stream_chat_caller_that_gives_up_frees_the_queue():
    """Akışsız sohbet ucu: bağlantısını kapatan istemcinin isteği iptal edilir."""
    script = UpstreamScript(delay=5.0)
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    with live_wrapper(script, **QUIET) as wrapper:
        with pytest.raises(httpx.ReadTimeout):
            httpx.post(
                wrapper.base_url + "/v1/chat/completions",
                json=chat,
                headers=wrapper.headers,
                timeout=httpx.Timeout(0.3, connect=5.0),
            )
        freed = wait_until(lambda: queue_idle(wrapper))
        dropped = wait_until(lambda: script.cancelled == 1)
        health = wrapper.app.state.upstream_health.snapshot()

    assert freed and dropped
    assert health["failing"] is False


def test_non_stream_chat_still_answers_normally():
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    with live_wrapper(UpstreamScript(delay=0.05), **QUIET) as wrapper:
        response = httpx.post(
            wrapper.base_url + "/v1/chat/completions",
            json=chat,
            headers=wrapper.headers,
            timeout=15,
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "merhaba dünya"


def test_chat_endpoint_reports_a_full_queue_with_its_own_error_code():
    """Sohbet ucu ayrıntı verir: sıra doluysa HTTP 429 `queue_full` + `Retry-After`."""
    script = UpstreamScript(delay=0.6)
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}

    def post(wrapper: LiveWrapper) -> httpx.Response:
        return httpx.post(
            wrapper.base_url + "/v1/chat/completions",
            json=chat,
            headers=wrapper.headers,
            timeout=15,
        )

    with (
        live_wrapper(script, request_queue_max_waiting=1, **QUIET) as wrapper,
        ThreadPoolExecutor(max_workers=3) as pool,
    ):
        futures = []
        for _ in range(3):
            futures.append(pool.submit(post, wrapper))
            time.sleep(0.08)
        responses = [future.result(timeout=15) for future in futures]

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[2].json()["error"]["code"] == "queue_full"
    assert responses[2].headers["retry-after"] == "30"
    assert script.calls == 2


def test_queue_view_names_the_source_of_each_request():
    """Yönetim ucu sırada kimin olduğunu söyler: ask / api / keepalive / telegram."""
    script = UpstreamScript(delay=0.5)
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}

    def post(wrapper: LiveWrapper, source: str | None) -> httpx.Response:
        headers = dict(wrapper.headers)
        if source is not None:
            headers["x-apiwrapper-source"] = source
        return httpx.post(
            wrapper.base_url + "/v1/chat/completions", json=chat, headers=headers, timeout=15
        )

    with live_wrapper(script, **QUIET) as wrapper, ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(ask, wrapper)]
        time.sleep(0.08)
        for source in ("keepalive", "telegram", None, "uydurma-kaynak"):
            futures.append(pool.submit(post, wrapper, source))
            time.sleep(0.08)
        view = httpx.get(
            wrapper.base_url + "/v1/admin/queue", headers=wrapper.headers, timeout=5
        ).json()["queue"]
        for future in futures:
            future.result(timeout=15)

    assert view["active"] == "ask"
    # Tanınmayan başlık değeri etiket olarak kabul edilmez ("api" olur).
    assert view["waiting_labels"] == ["keepalive", "telegram", "api", "api"]
    assert view["waiting"] == 4


def test_stream_caller_that_leaves_mid_answer_frees_the_queue_and_upstream():
    """Akışlı sohbet: cevabın ortasında kopan istemci sırayı ve upstream'i bırakır."""
    script = UpstreamScript(chunks=tuple(f"parça{i} " for i in range(40)), chunk_delay=0.1)
    chat = {
        "model": "test-model",
        "stream": True,
        "messages": [{"role": "user", "content": "selam"}],
    }
    with live_wrapper(script, **QUIET) as wrapper:
        received = 0
        with (
            httpx.Client(timeout=5) as client,
            client.stream(
                "POST",
                wrapper.base_url + "/v1/chat/completions",
                json=chat,
                headers=wrapper.headers,
            ) as response,
        ):
            for line in response.iter_lines():
                if line.startswith("data:") and "parça" in line:
                    received += 1
                    if received == 3:
                        break  # bağlantıyı cevabın ortasında kapat
        freed = wait_until(lambda: queue_idle(wrapper))
        dropped = wait_until(lambda: script.cancelled == 1)
        health = wrapper.app.state.upstream_health.snapshot()

    assert received == 3
    assert freed and dropped
    assert health["failing"] is False


def test_emoji_split_by_upstream_arrives_whole_over_a_real_connection():
    """Upstream 😀 karakterini iki parçaya bölerse: önceden akış kesiliyor ve sıra
    kilitli kalıyordu (ölçüldü). Şimdi cevap eksiksiz gelir, sıra boşalır."""
    script = UpstreamScript(raw_lines=(b'0:"g\\u00fcle \\ud83d"', b'0:"\\ude00 bitti"'))
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    with live_wrapper(script, **QUIET) as wrapper:
        streamed = ask(wrapper, path="/v1/chat/completions", payload={**chat, "stream": True})
        plain = httpx.post(
            wrapper.base_url + "/v1/chat/completions",
            json=chat,
            headers=wrapper.headers,
            timeout=5,
        )
        asked = ask(wrapper)
        idle = wait_until(lambda: queue_idle(wrapper))

    text = "".join(
        chunk["choices"][0]["delta"].get("content") or "" for _, chunk in streamed
    )
    assert text == "güle 😀 bitti"
    assert plain.status_code == 200
    assert plain.json()["choices"][0]["message"]["content"] == "güle 😀 bitti"
    assert asked[-1][1]["status"] == "done"
    assert asked[-1][1]["text"] == "güle 😀 bitti"
    assert idle


def test_probe_stuck_behind_a_long_request_is_turned_away_not_left_to_time_out(monkeypatch):
    """Probe sırada en çok 60 sn bekler (burada 0,3 sn): geç başlayıp işlenirken
    kendi süresini doldurmasın ve sağlıklı upstream "zaman aşımı" diye raporlanmasın."""
    monkeypatch.setattr(queue_module, "KEEPALIVE_MAX_QUEUE_WAIT_SECONDS", 0.3)
    script = UpstreamScript(delay=1.0)
    chat = {"model": "test-model", "messages": [{"role": "user", "content": "selam"}]}
    with live_wrapper(script, **QUIET) as wrapper, ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(ask, wrapper)
        time.sleep(0.1)
        started = time.monotonic()
        probe = httpx.post(
            wrapper.base_url + "/v1/chat/completions",
            json=chat,
            headers={**wrapper.headers, "x-apiwrapper-source": "keepalive"},
            timeout=5,
        )
        waited = time.monotonic() - started
        health = wrapper.app.state.upstream_health.snapshot()
        assert statuses(slow.result(timeout=15)) == ["done"]

    assert probe.status_code == 503
    assert probe.json()["error"]["code"] == "queue_timeout"
    assert 0.28 <= waited < 0.9  # öndeki istek (1 sn) bitmeden geri çevrildi
    assert health["failing"] is False  # sıra reddi upstream arızası değildir
    assert script.calls == 1  # probe upstream'e hiç gitmedi
