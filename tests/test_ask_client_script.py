"""`scripts/ask_client.py` — karşı sisteme örnek verilen istemci, gerçek sunucuya karşı.

Belgede (docs/ASK_API.md) "olduğu gibi kullanılabilir" denen kodun sözleşmenin
bütün kurallarını gerçekten uyguladığını doğrular: cevap, bekleme işaretleri,
problem, HTTP hatası, sessizlik ve ulaşılamayan sunucu.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from scripts.ask_client import SILENCE_SECONDS, ask, main
from tests.conftest import TEST_API_KEY
from tests.live import UpstreamScript, live_wrapper, wait_until

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ask_client.py"
FAST = {"ask_first_wait_seconds": 0.3, "ask_wait_interval_seconds": 0.2}
QUIET = {"ask_first_wait_seconds": 30.0, "ask_wait_interval_seconds": 15.0}


def test_answer_is_returned_whole_with_its_original_characters():
    answer = ("İstanbul ğüşöç 😀\nikinci satır\u2028üçüncü",)
    with live_wrapper(UpstreamScript(chunks=answer), **FAST) as wrapper:
        waits: list[dict] = []
        text = ask(wrapper.base_url, TEST_API_KEY, "soru", on_wait=waits.append)

    assert text == answer[0]
    assert waits == []


def test_wait_signals_are_reported_then_the_answer_arrives():
    with live_wrapper(UpstreamScript(delay=0.9), **FAST) as wrapper:
        waits: list[dict] = []
        text = ask(wrapper.base_url, TEST_API_KEY, "soru", on_wait=waits.append)

    assert text == "merhaba dünya"
    assert len(waits) >= 2
    assert all(event["status"] == "wait" for event in waits)


def test_optional_fields_are_sent():
    with live_wrapper(UpstreamScript(), **FAST) as wrapper:
        text = ask(
            wrapper.base_url + "/",  # sondaki eğik çizgi sorun olmamalı
            TEST_API_KEY,
            "soru",
            system="kısa yaz",
            model="second-model",
        )

    assert text == "merhaba dünya"


def test_every_failure_is_just_none():
    quota = UpstreamScript(status=429, body='{"error":"daily limit reached"}')
    with live_wrapper(quota, **FAST) as wrapper:
        assert ask(wrapper.base_url, TEST_API_KEY, "soru") is None  # upstream arızası
        assert ask(wrapper.base_url, "sk-yanlis", "soru") is None  # HTTP 401
        assert ask(wrapper.base_url, TEST_API_KEY, "   ") is None  # HTTP 400
        assert ask(wrapper.base_url, TEST_API_KEY, "soru", model="yok") is None  # HTTP 404


def test_silence_is_treated_as_a_problem_and_frees_the_server():
    """Sunucu işaret göndermezse istemci sessizlik süresinde vazgeçer (kural 4)."""
    script = UpstreamScript(delay=5.0)
    with live_wrapper(script, **QUIET) as wrapper:
        started = time.monotonic()
        text = ask(wrapper.base_url, TEST_API_KEY, "soru", silence=0.4)
        elapsed = time.monotonic() - started
        freed = wait_until(lambda: wrapper.app.state.queue.snapshot()["active"] is None)

    assert text is None
    assert 0.38 <= elapsed < 2.0
    assert freed  # vazgeçilen istek sunucuda da iptal edildi


def test_unreachable_server_is_a_problem_not_an_exception():
    assert ask("http://127.0.0.1:9", TEST_API_KEY, "soru", silence=2) is None


def test_default_silence_threshold_matches_the_contract():
    # Sunucu en geç 30 sn'de ilk işareti, sonra 15 sn'de bir yenisini gönderir.
    assert SILENCE_SECONDS == 45.0


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Betiği ayrı süreçte çalıştırır (test sunucusunun logları stdout'a karışmasın)."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def test_command_line_prints_only_the_answer_on_stdout():
    with live_wrapper(UpstreamScript(chunks=("Alarm özeti: ", "sakin.")), **FAST) as wrapper:
        answered = run_cli(wrapper.base_url, TEST_API_KEY, "durum", "nedir?")
        refused = run_cli(wrapper.base_url, "sk-yanlis", "durum")

    assert answered.returncode == 0
    assert answered.stdout == "Alarm özeti: sakin.\n"
    assert "[bitti]" in answered.stderr
    assert refused.returncode == 1
    assert refused.stdout == ""
    assert "[problem]" in refused.stderr


def test_command_line_without_arguments_shows_usage(capsys, monkeypatch):
    monkeypatch.delenv("ASK_URL", raising=False)
    monkeypatch.delenv("ASK_KEY", raising=False)

    assert main(["ask_client.py"]) == 2
    assert "Kullanım" in capsys.readouterr().err
