"""`scripts/compare_curl.py` — tarayıcı cURL'ü ile bizim isteğin **değer** karşılaştırması.

Alan ADI karşılaştırması üç modda da aynı adlar gittiği için yanlış modu/kimliği
yakalamaz (15 Kasım: görsel isteği "EŞLEŞTİ" diye yeşil geçiyordu). Bu testler
değer düzeyindeki kontrolleri ve sır sızmamasını kilitler.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.model_registry import ModelRegistry
from scripts.compare_curl import main

FAKE_MODEL_ID = "019b0000-1111-2222-3333-444455556666"
CX_VALUE = "AglFakeCxValueDoNotLeak"
MODELS_FILE = "config/models.yaml"


def _curl_file(tmp_path: Path, *, model_id: str, cx: str | None = CX_VALUE) -> str:
    """Gerçek biçimde ama sahte değerlerle bir görsel-modu cURL'ü."""
    body: dict[str, object] = {
        "id": "chat-1",
        "modelAId": model_id,
        "userMessage": {"content": "bir manzara ciz"},
        "mode": "direct-battle",
        "modality": "image",
        "recaptchaV3Token": "fake-captcha-token",
    }
    if cx is not None:
        body["cx"] = cx
    path = tmp_path / "curl.txt"
    path.write_text(
        "curl 'https://upstream.test/nextjs-api/stream/create-evaluation' \\\n"
        "  -H 'cookie: sb-auth-token=fake-cookie-value' \\\n"
        "  -H 'referer: https://upstream.test/image/direct' \\\n"
        "  -H 'user-agent: TestAgent/1.0' \\\n"
        f"  --data-raw '{json.dumps(body, ensure_ascii=False)}'",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Betik `Settings()` ile env'den okur; testler ortamdan etkilenmemeli."""
    monkeypatch.setenv("MODELS_FILE", MODELS_FILE)
    monkeypatch.setenv("TARGET_DOMAIN", "upstream.test")
    monkeypatch.setenv("UPSTREAM_MODE", "direct-battle")
    monkeypatch.setenv("UPSTREAM_STREAM_PATH", "/nextjs-api/stream/create-evaluation")
    monkeypatch.delenv("UPSTREAM_CX", raising=False)
    monkeypatch.delenv("UPSTREAM_CX_SEND", raising=False)


def _run(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setattr("sys.argv", ["compare_curl", path])
    main()


def test_modelaid_mismatch_is_flagged_with_an_applicable_fix(tmp_path, capsys, monkeypatch):
    """Görsel modu farklı bir model kimliği istiyorsa operatör bunu görebilmeli.

    Canlı belirti: istek 200 + `finish_reason: stop` ama akışta ne metin ne
    görsel var (content_chars=0, yalnız `a2` atıf parçaları). Hedef isteği kabul
    edip hiçbir şey üretmedi → ilk şüpheli yanlış `modelAId`.
    """
    _run(monkeypatch, _curl_file(tmp_path, model_id=FAKE_MODEL_ID))
    out = capsys.readouterr().out

    assert "FARKLI  modelAId" in out
    assert FAKE_MODEL_ID in out
    # Talimat uygulanabilir olmalı: hangi kayda, hangi satır.
    assert f"upstream_id: {FAKE_MODEL_ID}" in out
    assert "'image'" in out


def test_cx_value_is_never_printed_only_its_length(tmp_path, capsys, monkeypatch):
    """`cx` istemci bağlamıdır: çıktıya değeri değil yalnız uzunluğu yazılır."""
    _run(monkeypatch, _curl_file(tmp_path, model_id=FAKE_MODEL_ID))
    out = capsys.readouterr().out

    assert CX_VALUE not in out
    assert f"tarayıcı {len(CX_VALUE)} karakter gönderiyor" in out


def test_matching_modelaid_is_reported_as_equal(tmp_path, capsys, monkeypatch):
    """Kimlik models.yaml ile aynıysa yeşil geçmeli (yanlış alarm üretmesin)."""
    expected = ModelRegistry.from_file(MODELS_FILE).resolve("image").upstream_id
    _run(monkeypatch, _curl_file(tmp_path, model_id=expected, cx=None))
    out = capsys.readouterr().out

    assert "EŞLEŞTİ modelAId" in out
    assert "FARKLI  modelAId" not in out
