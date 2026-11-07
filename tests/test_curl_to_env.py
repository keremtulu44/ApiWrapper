"""`scripts/curl_to_env.py` için testler.

Bu araç, kullanıcının cURL komutunu elle kopyalarken yaptığı hataları
(çerezin ortadan kesilmesi, değerin alt satıra taşması, mükerrer anahtarlar)
ortadan kaldırmak için yazıldı; testler hem doğru ayrıştırmayı hem de bozuk
girdilerde üretilen uyarıları doğrular.
"""

from __future__ import annotations

import base64
import json
import sys
import time

import pytest

from app.upstream.header_store import parse_extra_headers
from scripts.compare_curl import parse_curl
from scripts.curl_to_env import (
    build_settings,
    count_accounts,
    extract_captcha,
    merge_env,
    next_free_slot,
    slot_has_credentials,
    update_models_yaml,
)


def _jwt() -> str:
    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = seg({"alg": "HS256", "typ": "JWT"})
    payload = seg({"sub": "user-1", "exp": int(time.time()) + 3600})
    return f"{head}.{payload}.sig"


def _session_cookie(token: str) -> str:
    session = {"access_token": token, "refresh_token": "r" * 40}
    blob = base64.b64encode(json.dumps(session).encode()).decode()
    return (
        f"__cf_bm=abc; _ga=GA1.1.9; lmarena-auth-prod-v1.0=base64-{blob}; "
        "cf_clearance=CFxyz; sidebar_state=true"
    )


def _curl(cookie: str, body: dict) -> str:
    return (
        "curl 'https://example-llm.ai/nextjs-api/stream/post-to-evaluation/c1' \\\n"
        "  -H 'accept: */*' \\\n"
        "  -H 'accept-language: tr-TR,tr;q=0.9' \\\n"
        "  -H 'user-agent: Mozilla/5.0 Chrome/128.0.0.0' \\\n"
        f"  -H 'cookie: {cookie}' \\\n"
        f"  --data-raw '{json.dumps(body)}'"
    )


@pytest.fixture()
def sample() -> dict:
    token = _jwt()
    body = {
        "id": "c1",
        "modelAId": "019f19f2-41f1-7c6d-9891-48d02fd9952c",
        "userMessage": {"content": "Merhaba dünya"},
        "modality": "chat",
        "recaptchaV2Token": "0cAFcWeA6_" + "T" * 500,
    }
    return {"token": token, "body": body, "cookie": _session_cookie(token)}


def test_extracts_every_setting_from_curl(sample):
    env, warnings, model_id = build_settings(parse_curl(_curl(sample["cookie"], sample["body"])))

    assert env["TARGET_DOMAIN"] == "example-llm.ai"
    assert env["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/post-to-evaluation/{chat_id}"
    assert env["UPSTREAM_COOKIE"] == sample["cookie"]
    assert env["UPSTREAM_RECAPTCHA_FIELD"] == "recaptchaV2Token"
    assert env["RECAPTCHA_STATIC_TOKEN"] == sample["body"]["recaptchaV2Token"]
    assert env["RECAPTCHA_PROVIDER"] == "static"
    assert env["UPSTREAM_ACCEPT_LANGUAGE"] == "tr-TR,tr;q=0.9"
    assert model_id == "019f19f2-41f1-7c6d-9891-48d02fd9952c"
    # Bu örnek cURL'de 'authorization' başlığı yok; cookie-only kurulum için
    # bilgilendirme uyarısı üretilir (kritik değildir, yazmayı engellemez).
    assert not [w for w in warnings if w.startswith("KRİTİK")]
    assert len(warnings) == 1
    assert "authorization" in warnings[0]
    assert not any("anonim" in w for w in warnings)


def test_import_preserves_dynamic_captcha_and_discards_static_token(sample):
    env, _, _ = build_settings(
        parse_curl(_curl(sample["cookie"], sample["body"])),
        {"RECAPTCHA_PROVIDER": "browser"},
    )
    assert env["UPSTREAM_RECAPTCHA_FIELD"] == "recaptchaV2Token"
    assert env["RECAPTCHA_PROVIDER"] == "browser"
    assert env["RECAPTCHA_STATIC_TOKEN"] == ""


def test_extracts_only_safe_static_extra_headers(sample):
    curl = (
        "curl 'https://example-llm.ai/nextjs-api/stream/create-evaluation' "
        f"-H 'cookie: {sample['cookie']}' "
        "-H 'accept: */*' "
        "-H 'cache-control: no-cache' "
        "-H 'dnt: 1' "
        "-H 'pragma: no-cache' "
        "-H 'priority: u=1, i' "
        "-H 'sec-ch-ua: \"Chromium\";v=\"154\", \"Lemur\";v=\"154\"' "
        "-H 'sec-ch-ua-mobile: ?1' "
        "-H 'sec-ch-ua-platform: \"Android\"' "
        "-H 'sec-fetch-dest: empty' "
        "-H 'sec-fetch-mode: cors' "
        "-H 'sec-fetch-site: same-origin' "
        "-H 'x-arena-web-deployment: dpl_test' "
        "-H 'x-deployment-id: dpl_test' "
        "-H 'traceparent: dynamic-value' "
        f"--data-raw '{json.dumps(sample['body'])}'"
    )
    env, _, _ = build_settings(parse_curl(curl))
    extra = parse_extra_headers(env["UPSTREAM_EXTRA_HEADERS"])
    assert extra == {
        "accept": "*/*",
        "cache-control": "no-cache",
        "dnt": "1",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "sec-ch-ua": '"Chromium";v="154", "Lemur";v="154"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-arena-web-deployment": "dpl_test",
        "x-deployment-id": "dpl_test",
    }
    assert "traceparent" not in env["UPSTREAM_EXTRA_HEADERS"]
    assert "cookie" not in env["UPSTREAM_EXTRA_HEADERS"]


def test_extracted_cookie_is_single_line(sample):
    """`.env` satır tabanlıdır; değer içinde satır sonu olursa ayar bozulur."""
    env, _, _ = build_settings(parse_curl(_curl(sample["cookie"], sample["body"])))
    assert "\n" not in env["UPSTREAM_COOKIE"]
    assert "\n" not in env["RECAPTCHA_STATIC_TOKEN"]


def test_warns_when_cookie_is_a_broken_fragment():
    """Kullanıcının çerezi ortadan kopyalaması en sık görülen hata."""
    fragment = "mdvb2dsZXVzZXJjb250ZW50LmNvbS9hL0FDZzhvY0pGMlFZ"
    curl = f"curl 'https://x.ai/api' -H 'cookie: {fragment}' --data-raw '{{}}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any("ad=değer" in w for w in warnings)


def test_warns_when_session_cookie_missing():
    """Yalnızca analitik çerezleri varsa kullanıcı giriş yapmamıştır."""
    curl = "curl 'https://x.ai/api' -H 'cookie: _ga=GA1.1.1; sidebar_state=true' --data-raw '{}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any("auth" in w for w in warnings)


@pytest.mark.parametrize(
    "field", ["recaptchaV2Token", "recaptchaV3Token", "turnstileToken"]
)
def test_detects_captcha_field_name(field: str):
    found, value = extract_captcha({field: "tok-" + "x" * 40})
    assert found == field
    assert value.startswith("tok-")


def test_merge_env_updates_in_place_and_keeps_others(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "API_KEYS=sk-keep\nTARGET_DOMAIN=localhost\n# yorum\nLOG_LEVEL=info\n",
        encoding="utf-8",
    )
    result = merge_env(path, {"TARGET_DOMAIN": "example.com", "UPSTREAM_COOKIE": "a=1"})

    assert "API_KEYS=sk-keep" in result
    assert "LOG_LEVEL=info" in result
    assert "# yorum" in result
    assert "TARGET_DOMAIN=example.com" in result
    assert "TARGET_DOMAIN=localhost" not in result
    assert result.count("TARGET_DOMAIN=") == 1


def test_merge_env_removes_duplicate_keys(tmp_path):
    """Elle düzenlenen dosyalarda aynı anahtar birden çok kez bulunabilir."""
    path = tmp_path / ".env"
    path.write_text("UPSTREAM_COOKIE=old1\nAPI_KEYS=k\nUPSTREAM_COOKIE=old2\n", encoding="utf-8")
    result = merge_env(path, {"UPSTREAM_COOKIE": "new"})

    assert result.count("UPSTREAM_COOKIE=") == 1
    assert "UPSTREAM_COOKIE=new" in result
    assert "old1" not in result and "old2" not in result


def test_merge_env_deduplicates_legacy_unrelated_keys(tmp_path):
    path = tmp_path / ".env"
    path.write_text("AUTH_DISABLED=false\nPORT=8000\nAUTH_DISABLED=true\n", encoding="utf-8")
    result = merge_env(path, {"TARGET_DOMAIN": "example.com"})
    assert result.count("AUTH_DISABLED=") == 1
    assert "AUTH_DISABLED=false" in result
    assert "AUTH_DISABLED=true" not in result


def test_merge_env_creates_file_when_absent(tmp_path):
    path = tmp_path / ".env"
    result = merge_env(path, {"TARGET_DOMAIN": "example.com"})
    assert "TARGET_DOMAIN=example.com" in result


def test_update_models_yaml_replaces_first_upstream_id(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n  - id: a\n    upstream_id: PLACEHOLDER\n  - id: b\n    upstream_id: OTHER\n",
        encoding="utf-8",
    )
    assert update_models_yaml(path, "real-id-1") is True
    text = path.read_text(encoding="utf-8")
    assert "upstream_id: real-id-1" in text
    assert "upstream_id: OTHER" in text


def test_update_models_yaml_missing_file_is_reported(tmp_path):
    assert update_models_yaml(tmp_path / "yok.yaml", "x") is False


def test_windows_cmd_curl_format_is_parsed():
    """Windows 'Copy as cURL (cmd)' çıktısı ^ ile kaçış kullanır."""
    curl = 'curl ^"https://x.ai/api^" ^\n  -H ^"cookie: a-auth-token=xyz123456789^"'
    env, _, _ = build_settings(parse_curl(curl))
    assert env["TARGET_DOMAIN"] == "x.ai"
    assert "a-auth-token=xyz123456789" in env["UPSTREAM_COOKIE"]


# ------------------------- yanlış isteğin cURL'ü kopyalandığında
def test_telemetry_host_is_flagged_as_critical():
    """Datadog/analitik isteğinin cURL'ü kopyalanırsa TARGET_DOMAIN bozulur.

    Gerçek bir raporda base_url 'browser-intake-us3-datadoghq.com' olmuştu;
    istekler LLM yerine log toplayıcıya gidiyordu.
    """
    curl = (
        "curl 'https://browser-intake-us3-datadoghq.com/api/v2/rum' "
        "-H 'cookie: a-auth-token=xyz1234567890abc' --data-raw '{}'"
    )
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any(w.startswith("KRİTİK") and "telemetri" in w for w in warnings)


def test_same_origin_posthog_capture_is_rejected():
    """PostHog aynı domainde/proxy'de olsa da sohbet isteği değildir."""
    curl = (
        "curl 'https://arena.ai/rpc/i/v0/e/?ip=0&compression=gzip-js' "
        "-H 'cookie: a-auth-token=xyz1234567890abc' --data-raw 'compressed-events'"
    )
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any(w.startswith("KRİTİK") and "telemetri" in w for w in warnings)
    assert any("/rpc/i/v0/e/" in w for w in warnings)


@pytest.mark.parametrize(
    "host",
    ["browser-intake-us3-datadoghq.com", "sentry.io", "www.google-analytics.com"],
)
def test_known_telemetry_hosts_are_rejected(host: str):
    curl = f"curl 'https://{host}/collect' -H 'cookie: a=b' --data-raw '{{}}'"
    _, warnings, _ = build_settings(parse_curl(curl))
    assert any(w.startswith("KRİTİK") for w in warnings)


def test_unexpected_path_is_warned_but_not_critical():
    """Doğru alan adı ama yanlış uç: uyar, ama yazmayı engelleme."""
    curl = "curl 'https://example-llm.ai/api/session' -H 'cookie: a-auth=b' --data-raw '{}'"
    env, warnings, _ = build_settings(parse_curl(curl))
    assert env["UPSTREAM_STREAM_PATH"] == "/api/session"
    assert any("create-evaluation" in w or "post-to-evaluation" in w for w in warnings)
    assert not any(w.startswith("KRİTİK") for w in warnings)


def test_correct_endpoint_produces_no_path_or_host_warning(sample):
    curl = _curl(sample["cookie"], sample["body"])
    _, warnings, _ = build_settings(parse_curl(curl))
    assert not any("içermiyor" in w for w in warnings)
    assert not any(w.startswith("KRİTİK") for w in warnings)


# ------------------------- akış yolu cURL'den alınmalı
def _curl_with_path(path: str, chat_id: str) -> str:
    body = {
        "id": chat_id,
        "modelAId": "019b1449-0313-7911-b836-419e2ed79b2e",
        "modality": "chat",
        "recaptchaV2Token": "tok" + "x" * 40,
    }
    return (
        f"curl 'https://target.ai{path}' "
        "-H 'cookie: a-auth-token=base64-eyJhY2Nlc3NfdG9rZW4iOiAiYS5iLmMifQ==' "
        f"--data-raw '{json.dumps(body)}'"
    )


def test_stream_path_is_taken_from_curl_not_assumed():
    """Yol sürümlenirse sabit varsayım HTTP 404 üretir (gerçek raporda görüldü)."""
    chat_id = "ffd33ec3-8749-4263-9201-fe7248be8694"
    curl = _curl_with_path(f"/api/stream/post-to-evaluation/{chat_id}", chat_id)
    env, _, _ = build_settings(parse_curl(curl))
    assert env["UPSTREAM_STREAM_PATH"] == "/api/stream/post-to-evaluation/{chat_id}"


def test_stream_path_placeholder_replaces_only_the_chat_id():
    chat_id = "ffd33ec3-8749-4263-9201-fe7248be8694"
    path = f"/nextjs-api/stream/post-to-evaluation/{chat_id}/retry"
    env, _, _ = build_settings(parse_curl(_curl_with_path(path, chat_id)))
    assert env["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/post-to-evaluation/{chat_id}/retry"


def test_stream_path_falls_back_to_uuid_detection():
    """Gövdedeki 'id' yoldaki kimlikten farklıysa UUID sezgisi devreye girer."""
    path = "/nextjs-api/stream/post-to-evaluation/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    env, _, _ = build_settings(parse_curl(_curl_with_path(path, "baska-id")))
    assert env["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/post-to-evaluation/{chat_id}"


def test_short_id_does_not_corrupt_path():
    """Kısa bir 'id' değeri yol içinde harf dizisine denk gelmemeli."""
    path = "/nextjs-api/stream/post-to-evaluation"
    env, _, _ = build_settings(parse_curl(_curl_with_path(path, "xt")))
    assert env["UPSTREAM_STREAM_PATH"] == path
    assert "{chat_id}" not in env["UPSTREAM_STREAM_PATH"]


def test_path_without_chat_id_is_kept_as_is():
    """Yeni uçlar kimliği URL'de taşımaz; yol yine de .env'e yazılmalıdır.

    Aksi halde varsayılan `.../post-to-evaluation/{chat_id}` kullanılır ve
    gerçek istek HTTP 404 ile düşer.
    """
    env, warnings, _ = build_settings(
        parse_curl(_curl_with_path("/nextjs-api/stream/create-evaluation", "x"))
    )
    assert env["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/create-evaluation"
    assert not any(w.startswith("KRİTİK") for w in warnings)
    assert not any("içermiyor" in w for w in warnings)


def test_create_evaluation_path_is_not_flagged_as_wrong_endpoint():
    chat_id = "ffd33ec3-8749-4263-9201-fe7248be8694"
    env, warnings, _ = build_settings(
        parse_curl(_curl_with_path("/nextjs-api/stream/create-evaluation", chat_id))
    )
    assert env["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/create-evaluation"
    assert "{chat_id}" not in env["UPSTREAM_STREAM_PATH"]
    assert not any("içermiyor" in w for w in warnings)


def test_query_string_is_preserved_on_stream_path():
    chat_id = "ffd33ec3-8749-4263-9201-fe7248be8694"
    env, _, _ = build_settings(
        parse_curl(_curl_with_path(f"/api/stream/{chat_id}?src=web", chat_id))
    )
    assert env["UPSTREAM_STREAM_PATH"] == "/api/stream/{chat_id}?src=web"


def test_mode_is_extracted_from_body(sample):
    body = dict(sample["body"])
    body["mode"] = "direct"
    env, _, _ = build_settings(parse_curl(_curl(sample["cookie"], body)))
    assert env["UPSTREAM_MODE"] == "direct"


# ------------------------------------------------------- Windows kullanım hataları
def test_missing_curl_file_reports_a_clear_error(tmp_path, monkeypatch, capsys):
    """Eksik dosya traceback değil, ne yapılacağını söyleyen bir mesaj vermeli."""
    from scripts import curl_to_env

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "curl_to_env.py",
            "--write",
            str(tmp_path / "curl1.txt"),
            "--env-file",
            str(tmp_path / ".env"),
        ],
    )
    assert curl_to_env.main() == 2

    err = capsys.readouterr().err
    assert "bulunamadı" in err
    assert "Get-Clipboard" in err  # dosyasız yolu gösteriyor
    assert "Traceback" not in err


def test_bom_prefixed_curl_file_still_parses(tmp_path, monkeypatch, capsys, sample):
    """PowerShell/Notepad'in yazdığı UTF-8 BOM ilk token'ı bozmamalı."""
    from scripts import curl_to_env

    curl_file = tmp_path / "curl1.txt"
    curl_file.write_bytes(
        b"\xef\xbb\xbf" + _curl(sample["cookie"], sample["body"]).encode("utf-8")
    )
    env_file = tmp_path / ".env"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "curl_to_env.py",
            "--write",
            str(curl_file),
            "--env-file",
            str(env_file),
            "--models-file",
            str(tmp_path / "models.yaml"),
        ],
    )
    assert curl_to_env.main() == 0

    written = env_file.read_text(encoding="utf-8")
    assert "TARGET_DOMAIN=example-llm.ai" in written


# --------------------------------------------------- yarım kalmış hesap yuvaları
# Cookie başlığı olmayan bir cURL kaydedildiğinde yuva adıyla birlikte yazılıyor
# ama kimliği olmuyordu. Uygulama (`load_accounts`) böyle yuvaları zaten atlar,
# fakat araç onları "hesap" sayıp bir sonraki cURL'ü yeni bir yuvaya itiyordu.
BROKEN_ENV = """\
TARGET_DOMAIN=hedef.com
UPSTREAM_COOKIE=AAAA
UPSTREAM_ACCOUNT_2_NAME=hesap-2
UPSTREAM_COOKIE_2=
UPSTREAM_ACCOUNT_3_NAME=hesap-3
UPSTREAM_COOKIE_3=
UPSTREAM_ACCOUNT_4_NAME=hesap-4
UPSTREAM_COOKIE_4=BBBB
"""


def _write_broken(tmp_path):
    path = tmp_path / ".env"
    path.write_text(BROKEN_ENV, encoding="utf-8")
    return path


def test_name_only_slots_are_not_counted_as_accounts(tmp_path):
    path = _write_broken(tmp_path)
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in BROKEN_ENV.strip().splitlines()
    }
    # 1. ve 4. yuva gerçek; 2. ve 3. yalnız adlı.
    assert count_accounts(values) == 2
    assert slot_has_credentials(values, 1) is True
    assert slot_has_credentials(values, 2) is False
    assert next_free_slot(path) == 2  # onarılacak yuva, yeni yuva değil


def test_saving_repairs_a_half_written_slot(tmp_path, monkeypatch):
    """Yeniden kaydetme yarım yuvayı doldurmalı, 5. yuvayı açmamalı."""
    from scripts import curl_to_env

    path = _write_broken(tmp_path)
    body = '{"modelAId":"m","userMessage":{"content":"x"}}'
    curl_file = tmp_path / "c.txt"
    curl_file.write_text(
        "curl 'https://hedef.com/x' -H 'cookie: oturum=YENI' "
        f"--data-raw '{body}'",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "curl_to_env.py",
            "--write",
            str(curl_file),
            "--env-file",
            str(path),
            "--models-file",
            str(tmp_path / "m.yaml"),
        ],
    )
    assert curl_to_env.main() == 0

    after = path.read_text(encoding="utf-8")
    assert "UPSTREAM_COOKIE_2=oturum=YENI" in after
    assert "UPSTREAM_COOKIE_5" not in after
    assert "UPSTREAM_ACCOUNT_5_NAME" not in after


def test_curl_without_credentials_is_refused(tmp_path, monkeypatch, capsys):
    """Cookie/authorization olmayan cURL yarım yuva bırakmamalı."""
    from scripts import curl_to_env

    path = tmp_path / ".env"
    body = '{"modelAId":"m","userMessage":{"content":"x"}}'
    curl_file = tmp_path / "c.txt"
    curl_file.write_text(
        "curl 'https://hedef.com/x' -H 'accept: */*' "
        f"--data-raw '{body}'",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "curl_to_env.py",
            "--write",
            str(curl_file),
            "--env-file",
            str(path),
        ],
    )
    assert curl_to_env.main() == 2

    err = capsys.readouterr().err
    assert "cookie" in err and "authorization" in err
    # Hiçbir şey yazılmamış olmalı.
    assert not path.exists() or "UPSTREAM_ACCOUNT" not in path.read_text(encoding="utf-8")


def test_list_accounts_flags_unusable_slots(tmp_path, monkeypatch, capsys):
    from scripts import curl_to_env

    path = _write_broken(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["curl_to_env.py", "--env-file", str(path), "--list-accounts", "-"]
    )
    assert curl_to_env.main() == 0

    out = capsys.readouterr().out
    assert "2 kullanılabilir hesap var" in out
    assert "KULLANILAMIYOR" in out


def test_curl_body_cx_field_is_captured_and_sent():
    """cURL gövdesindeki `cx` alanı okunmalı ve canlı istekte gönderilmeli.

    Canlı cURL'de `cx` gövdede geliyordu ama kod hiç okumuyordu; istek onsuz
    gidiyordu.
    """
    import json

    from app.adapters.openai_to_upstream import build_upstream_request
    from app.schemas.openai import ChatCompletionRequest
    from app.upstream.curl_import import build_settings, parse_curl

    body = {
        "id": "chat-1",
        "modelAId": "model-1",
        "userMessageId": "u1",
        "modelAMessageId": "m1",
        "modality": "chat",
        "cx": "AibRul-WjekSN8tE0xEE",
        "userMessage": {"content": "selam"},
    }
    curl = (
        "curl --url 'https://arena.ai/nextjs-api/stream/create-evaluation' "
        "-b '_ga=1; arena-auth-prod-v1="
        "base64-eyJhY2Nlc3NfdG9rZW4iOiJleUpoYkdjaU9pSkZVekkxTmlKOSJ9' "
        f"--data-raw '{json.dumps(body)}'"
    )

    env, _, _ = build_settings(parse_curl(curl))
    assert env.get("UPSTREAM_CX") == "AibRul-WjekSN8tE0xEE"
    # cURL'de cx gövdesi mevcutsa otomatik olarak gönderim de açılır: kullanıcı
    # tarayıcıdan kopyaladığı istekte bu alan varsa hedef doğruluyor demektir.
    assert env.get("UPSTREAM_CX_SEND") == "true"

    from tests.conftest import make_settings

    request = ChatCompletionRequest(
        model="test-model",
        messages=[{"role": "user", "content": "merhaba"}],
    )

    def wire_for(cfg):
        built = build_upstream_request(
            request,
            settings=cfg,
            upstream_model_id="model-1",
            chat_id="chat-1",
            recaptcha_token="token",
        )
        return built.payload.to_wire(cfg.upstream_recaptcha_field)

    # cx ayarlanmamışsa alan gönderilmez (eski varsayılan davranış korunur).
    plain = make_settings()
    assert "cx" not in wire_for(plain)

    # cURL'den uygulanan ayar (UPSTREAM_CX_SEND=true) cx'i gövdeye koyar.
    enabled = make_settings(
        upstream_cx=env["UPSTREAM_CX"], upstream_cx_send=True
    )
    assert wire_for(enabled)["cx"] == "AibRul-WjekSN8tE0xEE"


def test_empty_cx_is_not_sent():
    from app.schemas.upstream import UpstreamPayload, UserMessage

    payload = UpstreamPayload(
        id="c", modelAId="m", userMessageId="u", modelAMessageId="a",
        userMessage=UserMessage(content="x"), mode="direct-battle",
    )
    assert "cx" not in payload.to_wire()


def test_site_key_not_inferred_from_cookie_or_captcha_response(sample):
    false_key = "6L" + "x" * 38
    body = {**sample["body"], "recaptchaV2Token": false_key}
    env, _, _ = build_settings(parse_curl(_curl(f"analytics={false_key}", body)))
    assert "RECAPTCHA_SITE_KEY" not in env


def test_site_key_imported_only_from_explicit_body_field(sample):
    key = "6L" + "x" * 38
    body = {**sample["body"], "recaptchaSiteKey": key}
    env, _, _ = build_settings(parse_curl(_curl(sample["cookie"], body)))
    assert env["RECAPTCHA_SITE_KEY"] == key
