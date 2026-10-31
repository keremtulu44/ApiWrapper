"""Çoklu hesap (cURL yuvaları) testleri.

Kullanıcı cURL'leri sırayla atar: 1. cURL soneksiz anahtarlara, 2. cURL `_2`
soneksli anahtarlara yazılır. Hiçbir hesap diğerini ezmemeli.
"""

from __future__ import annotations

import json
import sys

import pytest

from app.core.config import MAX_ACCOUNT_SLOTS, Settings
from app.services.account import load_accounts
from scripts.compare_curl import parse_curl
from scripts.curl_to_env import (
    _resolve_slot,
    build_settings,
    count_accounts,
    extract_authorization,
    extract_referer_path,
    merge_env,
    next_free_slot,
    read_env_keys,
    split_account_env,
    strip_account_keys,
)

DOMAIN = "llm.example.com"


def make_curl(
    *,
    cookie: str | None = "sb-auth-token=abc; other=1",
    token: str = "TOKEN-1",
    model: str = "MODEL_A_1",
    authorization: str | None = "Bearer tok-123",
    domain: str = DOMAIN,
) -> str:
    body = {
        "id": "11111111-2222-3333-4444-555555555555",
        "modelAId": model,
        "userMessageId": "m1",
        "modelAMessageId": "m2",
        "userMessage": {"content": "selam"},
        "modality": "chat",
        "recaptchaV3Token": token,
        "mode": "direct",
    }
    headers = [
        "-H 'accept: */*'",
        "-H 'accept-language: tr-TR,tr;q=0.9'",
        "-H 'user-agent: Mozilla/5.0 Chrome/152.0.0.0'",
        f"-H 'referer: https://{domain}/c/11111111-2222-3333-4444-555555555555'",
    ]
    if cookie is not None:
        headers.append(f"-H 'cookie: {cookie}'")
    if authorization is not None:
        headers.insert(0, f"-H 'authorization: {authorization}'")
    return (
        f"curl 'https://{domain}/nextjs-api/stream/create-evaluation' \\\n  "
        + " \\\n  ".join(headers)
        + f" \\\n  --data-raw '{json.dumps(body)}'"
    )


# ------------------------------------------------------- authorization
def test_authorization_header_is_extracted():
    headers = parse_curl(make_curl())["headers"]
    assert extract_authorization(headers) == ("Bearer", "tok-123")


def test_authorization_without_scheme():
    headers = parse_curl(make_curl(authorization="raw-token-value"))["headers"]
    assert extract_authorization(headers) == ("", "raw-token-value")


def test_missing_authorization_returns_none():
    headers = parse_curl(make_curl(authorization=None))["headers"]
    assert extract_authorization(headers) is None


def test_build_settings_writes_access_token_from_curl():
    """Eskiden bu başlık okunuyor ama sessizce atılıyordu."""
    env, warnings, _ = build_settings(parse_curl(make_curl()))
    assert env["UPSTREAM_ACCESS_TOKEN"] == "tok-123"
    assert env["UPSTREAM_AUTH_SCHEME"] == "Bearer"
    assert env["UPSTREAM_AUTH_FROM_COOKIE"] == "false"


def test_build_settings_warns_when_authorization_absent():
    env, warnings, _ = build_settings(parse_curl(make_curl(authorization=None)))
    # Boş değer bilinçli yazılır: aynı yuva tazelenirken eski token temizlenir.
    assert env["UPSTREAM_ACCESS_TOKEN"] == ""
    assert env["UPSTREAM_AUTH_FROM_COOKIE"] == "false"
    assert any("authorization" in w for w in warnings)


def test_cookie_only_auth_is_not_flagged_as_anonymous():
    """Oturum çerezi varsa cookie-only kurulum meşrudur; 'anonim' denmemeli."""
    _, warnings, _ = build_settings(
        parse_curl(make_curl(authorization=None, cookie="sb-auth-token=abc"))
    )
    assert not any("anonim" in w for w in warnings)


def test_neither_auth_header_nor_session_cookie_warns_loudly():
    _, warnings, _ = build_settings(
        parse_curl(make_curl(authorization=None, cookie="_ga=GA1.1; other=1"))
    )
    assert any("anonim" in w for w in warnings)


# ------------------------------------------------------------- referer
def test_referer_path_is_templated():
    headers = parse_curl(make_curl())["headers"]
    assert extract_referer_path(headers) == "/c/{chat_id}"


def test_build_settings_includes_referer_path():
    env, _, _ = build_settings(parse_curl(make_curl()))
    assert env["UPSTREAM_REFERER_PATH"] == "/c/{chat_id}"


# -------------------------------------------------------- alan adı uyarısı
def test_domain_mismatch_is_warned(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"TARGET_DOMAIN={DOMAIN}\n", encoding="utf-8")
    existing = read_env_keys(env_file)
    _, warnings, _ = build_settings(
        parse_curl(make_curl(domain="baska.example.com")), existing
    )
    assert any("baska.example.com" in w and DOMAIN in w for w in warnings)


def test_same_domain_produces_no_mismatch_warning(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"TARGET_DOMAIN={DOMAIN}\n", encoding="utf-8")
    _, warnings, _ = build_settings(parse_curl(make_curl()), read_env_keys(env_file))
    assert not any("alan adına ait" in w for w in warnings)


# ------------------------------------------------------------ yuva seçimi
def test_next_free_slot_starts_at_one(tmp_path):
    assert next_free_slot(tmp_path / "yok.env") == 1


def test_next_free_slot_skips_occupied_slots(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UPSTREAM_ACCOUNT_NAME=hesap-1\nUPSTREAM_COOKIE=a=1\n"
        "UPSTREAM_ACCOUNT_2_NAME=hesap-2\nUPSTREAM_COOKIE_2=b=2\n",
        encoding="utf-8",
    )
    assert next_free_slot(env_file) == 3


def test_split_account_env_first_slot_has_no_suffix():
    env, _, _ = build_settings(parse_curl(make_curl()))
    shared, account = split_account_env(env, 1, "hesap-1")
    assert account["UPSTREAM_COOKIE"] == "sb-auth-token=abc; other=1"
    assert account["RECAPTCHA_STATIC_TOKEN"] == "TOKEN-1"
    assert account["UPSTREAM_ACCOUNT_NAME"] == "hesap-1"
    # Paylaşılan anahtarlar hesap soneksi almaz.
    assert shared["UPSTREAM_STREAM_PATH"] == "/nextjs-api/stream/create-evaluation"
    assert "UPSTREAM_STREAM_PATH" not in account


def test_split_account_env_second_slot_is_suffixed():
    env, _, _ = build_settings(parse_curl(make_curl(cookie="x=2", token="TOKEN-2")))
    shared, account = split_account_env(env, 2, "hesap-2")
    assert "RECAPTCHA_PROVIDER" not in shared  # Global kip 2. hesapla ezilmez.
    assert account["UPSTREAM_COOKIE_2"] == "x=2"
    assert account["RECAPTCHA_STATIC_TOKEN_2"] == "TOKEN-2"
    assert account["UPSTREAM_ACCOUNT_2_NAME"] == "hesap-2"
    assert "UPSTREAM_COOKIE" not in account


# ------------------------------------------------- sıralı yazma (asıl senaryo)
def _write(env_file, curl_text: str, slot: int, label: str) -> None:
    env, _, _ = build_settings(parse_curl(curl_text))
    shared, account = split_account_env(env, slot, label)
    env_file.write_text(merge_env(env_file, {**shared, **account}), encoding="utf-8")


def test_two_curls_in_sequence_land_in_separate_slots(tmp_path):
    """Kullanıcının istediği davranış: 1. cURL ilk boşluklara, 2. cURL sonrakine."""
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8000\n", encoding="utf-8")

    _write(env_file, make_curl(cookie="COOKIE-1", token="TOKEN-1"), 1, "hesap-1")
    _write(env_file, make_curl(cookie="COOKIE-2", token="TOKEN-2"), 2, "hesap-2")

    values = read_env_keys(env_file)
    assert values["UPSTREAM_COOKIE"] == "COOKIE-1"
    assert values["RECAPTCHA_STATIC_TOKEN"] == "TOKEN-1"
    assert values["UPSTREAM_COOKIE_2"] == "COOKIE-2"
    assert values["RECAPTCHA_STATIC_TOKEN_2"] == "TOKEN-2"
    assert values["PORT"] == "8000"  # mevcut ayarlar korunur
    assert count_accounts(values) == 2


def test_three_accounts_added_through_cli_are_all_loaded_by_the_application(
    tmp_path, monkeypatch, capsys
):
    from scripts import curl_to_env

    env_file = tmp_path / ".env"
    curl_file = tmp_path / "request.txt"
    for slot in range(1, 4):
        curl_file.write_text(
            make_curl(
                cookie=f"auth-token=COOKIE-{slot}",
                token=f"TOKEN-{slot}",
                authorization=f"Bearer ACCESS-{slot}",
            ),
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
                str(env_file),
                "--models-file",
                str(tmp_path / "models.yaml"),
            ],
        )
        assert curl_to_env.main() == 0
        capsys.readouterr()

    accounts = load_accounts(Settings(_env_file=env_file))
    assert [(a.slot, a.cookie, a.access_token, a.target_domain) for a in accounts] == [
        (1, "auth-token=COOKIE-1", "ACCESS-1", DOMAIN),
        (2, "auth-token=COOKIE-2", "ACCESS-2", DOMAIN),
        (3, "auth-token=COOKIE-3", "ACCESS-3", DOMAIN),
    ]


def test_second_account_message_does_not_claim_only_first_account_is_read(
    tmp_path, monkeypatch, capsys
):
    from scripts import curl_to_env

    env_file = tmp_path / ".env"
    env_file.write_text(
        f"UPSTREAM_COOKIE=auth=C1\nRECAPTCHA_PROVIDER=browser\nTARGET_DOMAIN={DOMAIN}\n",
        encoding="utf-8",
    )
    curl_file = tmp_path / "request.txt"
    curl_file.write_text(make_curl(cookie="auth=C2"), encoding="utf-8")
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
    output = capsys.readouterr().out
    assert "yalnızca soneksiz" not in output
    assert "Hesap havuzu" in output
    # Ek hesap, önceden seçilmiş dinamik sağlayıcıyı static'e çevirmemeli.
    assert read_env_keys(env_file)["RECAPTCHA_PROVIDER"] == "browser"


def test_first_account_is_not_overwritten_by_second(tmp_path):
    env_file = tmp_path / ".env"
    _write(env_file, make_curl(cookie="COOKIE-1", token="TOKEN-1"), 1, "hesap-1")
    before = read_env_keys(env_file)
    _write(env_file, make_curl(cookie="COOKIE-2", token="TOKEN-2"), 2, "hesap-2")
    after = read_env_keys(env_file)
    for key in ("UPSTREAM_COOKIE", "RECAPTCHA_STATIC_TOKEN", "UPSTREAM_ACCESS_TOKEN"):
        assert after[key] == before[key]


def test_refreshing_same_slot_overwrites_only_that_slot(tmp_path):
    """Captcha token'ı tazelemek için aynı cURL yeniden atılabilir."""
    env_file = tmp_path / ".env"
    _write(env_file, make_curl(token="TOKEN-1"), 1, "hesap-1")
    _write(env_file, make_curl(cookie="COOKIE-2", token="TOKEN-2"), 2, "hesap-2")
    _write(env_file, make_curl(token="TOKEN-1-YENI"), 1, "hesap-1")

    values = read_env_keys(env_file)
    assert values["RECAPTCHA_STATIC_TOKEN"] == "TOKEN-1-YENI"
    assert values["RECAPTCHA_STATIC_TOKEN_2"] == "TOKEN-2"


def test_cookie_only_refresh_clears_the_previous_access_token(tmp_path):
    env_file = tmp_path / ".env"
    _write(env_file, make_curl(authorization="Bearer ESKI"), 1, "hesap-1")
    _write(
        env_file,
        make_curl(cookie="YENI-COOKIE", authorization=None),
        1,
        "hesap-1",
    )
    values = read_env_keys(env_file)
    assert values["UPSTREAM_COOKIE"] == "YENI-COOKIE"
    assert values["UPSTREAM_ACCESS_TOKEN"] == ""


def test_authorization_only_refresh_clears_the_previous_cookie(tmp_path):
    env_file = tmp_path / ".env"
    _write(env_file, make_curl(cookie="ESKI-COOKIE"), 1, "hesap-1")
    _write(
        env_file,
        make_curl(cookie=None, authorization="Bearer YENI"),
        1,
        "hesap-1",
    )
    values = read_env_keys(env_file)
    assert values["UPSTREAM_COOKIE"] == ""
    assert values["UPSTREAM_ACCESS_TOKEN"] == "YENI"


def test_numeric_refresh_preserves_a_custom_account_name(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UPSTREAM_ACCOUNT_2_NAME=benim-hesabim\nUPSTREAM_COOKIE_2=C2\n",
        encoding="utf-8",
    )
    assert _resolve_slot("2", env_file) == (2, "benim-hesabim")


def test_repairing_a_name_only_slot_preserves_its_name(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "UPSTREAM_COOKIE=C1\nUPSTREAM_ACCOUNT_2_NAME=onarilacak\n",
        encoding="utf-8",
    )
    assert _resolve_slot(None, env_file) == (2, "onarilacak")


def test_unsupported_account_slots_are_rejected(tmp_path):
    env_file = tmp_path / ".env"
    with pytest.raises(ValueError, match="1-4"):
        _resolve_slot(str(MAX_ACCOUNT_SLOTS + 1), env_file)
    with pytest.raises(ValueError, match="1-4"):
        _resolve_slot("0", env_file)


def test_adding_a_fifth_account_is_rejected_instead_of_pretending_to_save(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            f"UPSTREAM_COOKIE{'' if slot == 1 else f'_{slot}'}=C{slot}"
            for slot in range(1, MAX_ACCOUNT_SLOTS + 1)
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="yuvaları dolu"):
        _resolve_slot(None, env_file)


def test_strip_account_keys_clears_all_slots(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=8000\n", encoding="utf-8")
    _write(env_file, make_curl(), 1, "hesap-1")
    _write(env_file, make_curl(cookie="COOKIE-2", token="TOKEN-2"), 2, "hesap-2")
    _write(env_file, make_curl(cookie="COOKIE-3", token="TOKEN-3"), 3, "hesap-3")
    assert count_accounts(read_env_keys(env_file)) == 3

    strip_account_keys(env_file)
    text = env_file.read_text(encoding="utf-8")
    values = read_env_keys(env_file)
    assert count_accounts(values) == 0
    assert values["PORT"] == "8000"
    assert next_free_slot(env_file) == 1
    # Soneksli ve soneksiz tüm hesap anahtarları silinmiş olmalı.
    for leftover in ("UPSTREAM_COOKIE", "UPSTREAM_COOKIE_2", "RECAPTCHA_STATIC_TOKEN_3",
                     "UPSTREAM_ACCOUNT_2_NAME", "UPSTREAM_ACCESS_TOKEN_2"):
        assert leftover not in text, leftover


def test_single_account_cli_replaces_all_legacy_slots(tmp_path, monkeypatch, capsys):
    from scripts import curl_to_env

    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_KEYS=local-test\nAUTH_DISABLED=false\nAUTH_DISABLED=true\n"
        "RECAPTCHA_PROVIDER=browser\nRECAPTCHA_SITE_KEY=public-site-key\n"
        "RECAPTCHA_VERSION=enterprise\nRECAPTCHA_ACTION=chat_submit\n"
        "UPSTREAM_COOKIE=OLD-1\nUPSTREAM_COOKIE_2=OLD-2\n"
        "UPSTREAM_COOKIE_3=OLD-3\nUPSTREAM_COOKIE_4=OLD-4\n"
        "UPSTREAM_COOKIE_5=OLD-5\nUPSTREAM_ACCOUNT_5_NAME=eski-5\n",
        encoding="utf-8",
    )
    curl_file = tmp_path / "request.txt"
    curl_file.write_text(
        make_curl(cookie="auth-token=NEW-1", authorization=None),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "curl_to_env.py",
            "--single-account",
            "--write",
            str(curl_file),
            "--env-file",
            str(env_file),
            "--models-file",
            str(tmp_path / "models.yaml"),
        ],
    )

    assert curl_to_env.main() == 0
    capsys.readouterr()
    values = read_env_keys(env_file)
    assert count_accounts(values) == 1
    assert values["UPSTREAM_COOKIE"] == "auth-token=NEW-1"
    assert values["RECAPTCHA_PROVIDER"] == "browser"
    assert values["RECAPTCHA_STATIC_TOKEN"] == ""
    assert values["RECAPTCHA_VERSION"] == "enterprise"
    assert values["RECAPTCHA_ACTION"] == "chat_submit"
    assert values["UPSTREAM_AUTH_FROM_COOKIE"] == "false"
    assert values["API_KEYS"] == "local-test"
    text = env_file.read_text(encoding="utf-8")
    assert text.count("AUTH_DISABLED=") == 1
    assert "AUTH_DISABLED=false" in text
    for stale in ("OLD-2", "OLD-3", "OLD-4", "OLD-5", "UPSTREAM_ACCOUNT_5_NAME"):
        assert stale not in text

    accounts = load_accounts(Settings(_env_file=env_file))
    assert len(accounts) == 1
    assert accounts[0].cookie == "auth-token=NEW-1"


def test_is_account_key_recognizes_suffixed_names():
    from scripts.curl_to_env import _is_account_key

    assert _is_account_key("UPSTREAM_COOKIE")
    assert _is_account_key("UPSTREAM_COOKIE_2")
    assert _is_account_key("RECAPTCHA_STATIC_TOKEN_3")
    assert _is_account_key("UPSTREAM_ACCOUNT_2_NAME")
    assert not _is_account_key("PORT")
    assert not _is_account_key("UPSTREAM_STREAM_PATH")
    assert not _is_account_key("RETRY_MAX_ATTEMPTS_2")  # hesap anahtarı olmayan soneks
