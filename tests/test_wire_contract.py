"""Gönderme tarafı sözleşmesi: upstream'e gidenler canlı sürümle **birebir aynı**.

Operatör kuralı (19 Kasım 2026): ana cURL'ün işlenişi ve sistemin upstream'e
gönderdikleri — `mode` dahil — değişmez. Bu dosya kuralı CI'da zorunlu kılar.

Karşılaştırılan şey `tests/data/wire_golden_03aca2c.json`'dur: canlıdaki commit'in
(`03aca2c`, 17 Kasım'dan beri yayında) **kendi kodundan** `tests/wire_capture.py`
ile üretilmiş döküm. Buradaki testler aynı girdileri bugünkü koda verir ve
sonucun o dökümle aynı olmasını ister:

* upstream'e giden isteklerin sayısı ve sırası (tekrar denemeler dahil),
* her isteğin yöntemi ve adresi,
* başlıkların adı, **sırası** ve değeri (çerez kavanozu, sürüm damgası dahil),
* gövdenin anahtarları, **sırası** ve değerleri (`mode`, captcha alanı dahil),
* ana cURL'den çıkan ayarlar (27 cURL biçimi; hatalı cURL'ün hata metni dahil).

Bir test kırıldıysa gönderme tarafı değişmiştir. Çare kodu eski hâline
döndürmektir; altın dosyayı bugünkü koddan yeniden üretmek **değildir**
(bkz. `tests/wire_capture.py` baş açıklaması, docs/LESSONS §20).

Kütüphane yükseltmesi de bu testi kırabilir (örn. httpx varsayılan başlıkları
değiştirirse). O da gerçek bir değişikliktir: yeni imaj upstream'e farklı
görünecektir; bilerek karar verilmelidir.

Yorumlayıcı da gönderme tarafının parçasıdır: `Cookie` başlığındaki **sıra**
Python sürümüne bağlıdır (aşağıda `STDLIB_SORTS_COOKIES`). Altın dosya canlı
imajın yorumlayıcı ailesinin (3.11+; imaj 3.12) davranışını taşır.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.wire_capture import (
    CURL_CASES,
    SCENARIOS,
    SECOND_USER_AGENT,
    capture,
    chat,
    normalize,
    read_curl,
    running,
    second_curl,
    session_cookie,
    unpack,
)

GOLDEN_PATH = Path(__file__).parent / "data" / "wire_golden_03aca2c.json"
GOLDEN: dict[str, Any] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

#: cURL'deki çerez sırası (`tests/wire_capture.py: session_cookie`); bilerek alfabetik değil.
CURL_COOKIE_ORDER = [
    "_ga",
    "cf_clearance",
    "__cf_bm",
    "arena-auth-prod-v1.0",
    "arena-auth-prod-v1.1",
]


def _stdlib_sorts_cookies() -> bool:
    """Bu yorumlayıcının çerez kavanozu çerezleri **ada göre sıralayarak** mı dolaşıyor?

    httpx her istekte kavanozu dolaşarak `Cookie` başlığını kurar. Python 3.10'un
    `http.cookiejar`'ı dolaşırken ada göre sıralar; 3.11 ve sonrası ekleme sırasını
    (yani cURL'deki sırayı) korur. CI'da 3.10 işi bu yüzden farklı sıra üretir —
    ilk çalıştırmada böyle görüldü. Sürüm numarasına değil davranışa bakılır.
    """
    jar = httpx.Cookies()
    jar.set("b", "1", domain="x.test", path="/")
    jar.set("a", "2", domain="x.test", path="/")
    return [cookie.name for cookie in jar.jar] == ["a", "b"]


STDLIB_SORTS_COOKIES = _stdlib_sorts_cookies()


def _portable(request: dict[str, Any]) -> dict[str, Any]:
    """Çerezleri sıralayan yorumlayıcıda `Cookie` başlığını sıradan bağımsız kıyaslanır yapar.

    Yalnız o yorumlayıcıda ve yalnız çerezlerin **sırası** için: hangi çerezin
    hangi değerle gittiği, başlığın yeri ve diğer her şey yine birebir aranır.
    Canlı imajın yorumlayıcısında (3.11+) hiçbir şey gevşetilmez.
    """
    if not STDLIB_SORTS_COOKIES:
        return request
    headers = [
        [name, "; ".join(sorted(value.split("; "))) if name.lower() == "cookie" else value]
        for name, value in request["headers"]
    ]
    return {**request, "headers": headers}


def _line(request: dict[str, Any]) -> str:
    return f"{request['method']} {request['url']}"


def _explain(name: str, index: int, got: dict[str, Any], want: dict[str, Any]) -> str:
    """İlk farkı okunur biçimde anlatır (hangi başlık, hangi gövde alanı)."""
    notes = [f"{name}: {index + 1}. istek canlı sürümden farklı ({_line(want)})"]
    if _line(got) != _line(want):
        notes.append(f"  adres/yöntem: şimdi {_line(got)!r}")
    got_names = [header[0] for header in got["headers"]]
    want_names = [header[0] for header in want["headers"]]
    if got_names != want_names:
        notes.append(f"  başlık adları/sırası:\n    canlı: {want_names}\n    şimdi: {got_names}")
    else:
        for (key, new), (_, old) in zip(got["headers"], want["headers"], strict=True):
            if new != old:
                notes.append(f"  başlık {key!r}:\n    canlı: {old!r}\n    şimdi: {new!r}")
    if got.get("body_keys") != want.get("body_keys"):
        notes.append(
            f"  gövde anahtarları/sırası:\n    canlı: {want.get('body_keys')}\n"
            f"    şimdi: {got.get('body_keys')}"
        )
    elif got.get("body") != want.get("body"):
        old_body, new_body = want.get("body") or {}, got.get("body") or {}
        for key in old_body:
            if old_body[key] != new_body.get(key):
                notes.append(
                    f"  gövde alanı {key!r}:\n    canlı: {old_body[key]!r}\n"
                    f"    şimdi: {new_body.get(key)!r}"
                )
    if got.get("body_text") != want.get("body_text"):
        notes.append("  ham gövde farklı")
    return "\n".join(notes)


def _assert_same(name: str, actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> None:
    assert [_line(item) for item in actual] == [_line(item) for item in expected], (
        f"{name}: upstream'e giden isteklerin sayısı ya da sırası canlı sürümden farklı"
    )
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        got, want = _portable(got), _portable(want)
        assert got == want, _explain(name, index, got, want)


# ------------------------------------------------------------------ altın dosya
def test_golden_file_is_the_live_commit_and_covers_every_case():
    assert GOLDEN["source_commit"] == "03aca2c"
    assert sorted(GOLDEN["scenarios"]) == sorted(SCENARIOS)
    assert sorted(GOLDEN["curl"]) == sorted(CURL_CASES)
    # Boş bir döküm her şeyi "aynı" gösterirdi.
    assert sum(len(keys) for keys in GOLDEN["scenarios"].values()) >= 100
    assert all(GOLDEN["scenarios"][name] for name in SCENARIOS)


# ------------------------------------------------------------- giden istekler
@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_requests_sent_upstream_are_identical_to_the_live_version(name: str):
    _assert_same(name, capture(name), unpack(GOLDEN, name))


# ------------------------------------------------------------------ ana cURL
@pytest.mark.parametrize("name", sorted(CURL_CASES))
def test_main_curl_is_read_exactly_like_the_live_version(name: str):
    assert read_curl(name) == GOLDEN["curl"][name]


# ---------------------------------------------------------------- yorumlayıcı
def _cookie_names(request: dict[str, Any]) -> list[str]:
    cookie = next(value for name, value in request["headers"] if name.lower() == "cookie")
    return [part.split("=", 1)[0] for part in cookie.split("; ")]


def test_cookie_order_is_the_curl_order_on_the_production_interpreter():
    """Çerezler upstream'e cURL'deki sırayla gider — canlı imajın yorumlayıcısında.

    Sıra koddan değil yorumlayıcıdan gelir; bu test hangisinde ne olduğunu
    kayda geçirir ki bir gün sıra değişirse nedeni aranacak yer belli olsun.
    """
    first = capture("01_iki_akissiz_istek")[0]
    if STDLIB_SORTS_COOKIES:
        assert _cookie_names(first) == sorted(CURL_COOKIE_ORDER)  # Python 3.10
    else:
        assert _cookie_names(first) == CURL_COOKIE_ORDER  # Python 3.11+ (canlı: 3.12)


def test_golden_file_carries_the_curl_cookie_order():
    """Altın dosya, çerez sırasını koruyan yorumlayıcıda (canlıdaki aile) üretilmiştir."""
    first = unpack(GOLDEN, "01_iki_akissiz_istek")[0]
    assert _cookie_names(first) == CURL_COOKIE_ORDER


def test_image_interpreter_keeps_the_cookie_order():
    """İmajın Python'u 3.11'den eski olamaz: eskisi çerezleri yeniden sıralar.

    `Dockerfile`'daki taban imaj değişirse (örn. 3.10'a) upstream'e giden
    `Cookie` başlığı kod değişmeden farklılaşır.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text("utf-8")
    match = re.search(r"^FROM python:(\d+)\.(\d+)", dockerfile, flags=re.MULTILINE)
    assert match, "Dockerfile'da `FROM python:X.Y` satırı bulunamadı"
    assert (int(match.group(1)), int(match.group(2))) >= (3, 11)


# ---------------------------------------------------------------------- mode
def _posts(name: str) -> list[dict[str, Any]]:
    return [item for item in capture(name) if item["method"] == "POST"]


def test_mode_is_sent_exactly_as_it_was_copied_from_the_browser():
    """`mode` cURL'deki değeriyle, gövdede aynı yerde gider; yoksa hiç gitmez."""
    battle = _posts("01_iki_akissiz_istek")
    assert [post["body"]["mode"] for post in battle] == ["direct-battle", "direct-battle"]
    assert battle[0]["body_keys"] == [
        "id",
        "modelAId",
        "userMessageId",
        "modelAMessageId",
        "userMessage",
        "modality",
        "mode",
        "recaptchaV3Token",
    ]

    assert [post["body"]["mode"] for post in _posts("17_mode_direct")] == ["direct", "direct"]

    # cURL'de `mode` yoksa env'deki UPSTREAM_MODE onu geri getirmez; alan gövdede
    # yer almaz. Canlı sürüm de böyle davranır; açılışta denetim ya da "düzeltme"
    # eklenmez (operatör talimatı).
    without = _posts("16_mode_suz_curl")
    assert without and all("mode" not in post["body"] for post in without)


def test_internal_headers_are_never_forwarded_upstream():
    """Servisin iç başlıkları (`x-apiwrapper-*`) upstream'e hiçbir istekte gitmez."""
    for name in SCENARIOS:
        for request in unpack(GOLDEN, name):
            names = [header[0].lower() for header in request["headers"]]
            assert not [item for item in names if item.startswith("x-apiwrapper")], name


# ------------------------------------------------------------------ iki hesap
def _by_account(requests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], ...]:
    def agent(request: dict[str, Any]) -> str:
        return next(value for key, value in request["headers"] if key.lower() == "user-agent")

    main = [item for item in requests if agent(item) != SECOND_USER_AGENT]
    second = [item for item in requests if agent(item) == SECOND_USER_AGENT]
    return main, second


def _session(cookie_header: str) -> list[str]:
    """Çerez dizisindeki oturum parçaları (`arena-auth-prod-v1.N=...`), sırasıyla."""
    return [
        part.strip() for part in cookie_header.split(";") if part.strip().startswith("arena-auth-")
    ]


def test_adding_a_second_account_does_not_change_what_the_main_account_sends():
    """`UPSTREAM_CURL_2` eklenince ana cURL'ün istekleri bayt bayt aynı kalır.

    Dört istek hesaplar arasında sırayla bölünür (1, 2, 1, 2). Ana hesabın payına
    düşen iki istek, tek hesaplı canlı sürümün aynı iki isteğiyle karşılaştırılır.
    """
    with running(upstream_curl_2=second_curl()) as (client, upstream):
        for content in ("Merhaba", "öbür hesaba", "İkinci soru 🙂", "öbür hesaba"):
            assert chat(client, content) == 200
    main, second = _by_account(upstream.requests)

    _assert_same("iki hesap / ana hesap", normalize(main), unpack(GOLDEN, "01_iki_akissiz_istek"))

    # İkinci hesap kendi kimliğiyle, ana cURL'ün biçimiyle (yol, mode, alan adı) gider.
    posts = [item for item in normalize(second) if item["method"] == "POST"]
    assert len(posts) == 2
    for post in posts:
        headers = {key.lower(): value for key, value in post["headers"]}
        assert post["url"].endswith("/nextjs-api/stream/create-evaluation")
        assert post["body"]["mode"] == "direct-battle"
        assert post["body"]["recaptchaV3Token"].endswith("KKKK")
        assert headers["accept-language"] == "en-US,en;q=0.9"
        assert _session(headers["cookie"]) == _session(session_cookie("user-2"))
    main_posts = [item for item in normalize(main) if item["method"] == "POST"]
    assert [post["body"]["recaptchaV3Token"][-4:] for post in main_posts] == ["TTTT", "TTTT"]
    for post in main_posts:
        headers = {key.lower(): value for key, value in post["headers"]}
        assert _session(headers["cookie"]) == _session(session_cookie("user-1"))
