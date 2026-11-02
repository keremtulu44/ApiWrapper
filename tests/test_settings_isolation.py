"""Testlerin `.env` ve ortam değişkenlerinden yalıtıldığından emin olur.

Geliştiricinin depo kökündeki `.env` dosyası (`UPSTREAM_STREAM_PATH`,
`SESSION_REUSE`, `AUTH_DISABLED`, …) `Settings` tarafından okunduğu için test
ayarlarının üzerine sızıyor ve testler yalnızca `.env`'i olmayan makinelerde
geçiyordu. Bu testler o sızıntıyı kilitler.
"""

from __future__ import annotations

import os

from app.core.config import Settings


def test_settings_ignores_env_file_during_tests():
    assert Settings.model_config.get("env_file") is None


def test_settings_does_not_pick_up_developer_environment():
    """conftest, Settings alanlarına denk gelen ortam değişkenlerini temizlemiştir.

    Bu yüzden testlerdeki `Settings` her makinede aynı varsayılanları görür.
    """
    settings = Settings(_env_file=None, target_domain="beklenen.test")
    assert settings.target_domain == "beklenen.test"
    assert settings.session_reuse is False
    assert settings.auth_disabled is False
    assert settings.upstream_cookie == ""


def test_make_settings_is_hermetic():
    """Aynı çağrı, makinedeki .env ne olursa olsun aynı sonucu vermeli."""
    from tests.conftest import UPSTREAM_DOMAIN, make_settings

    first = make_settings()
    second = make_settings()
    assert first.target_domain == UPSTREAM_DOMAIN
    assert first.upstream_stream_path == second.upstream_stream_path
    assert first.session_reuse is False
    assert first.auth_disabled is False
    assert first.api_keys == ["sk-test-key"]


def test_known_leaky_keys_are_not_in_the_environment():
    """conftest, Settings alan adlarına denk gelen ortam değişkenlerini temizler."""
    for field in ("TARGET_DOMAIN", "SESSION_REUSE", "AUTH_DISABLED", "UPSTREAM_COOKIE"):
        assert os.environ.get(field) is None, field


def test_settings_ignores_a_dotenv_left_in_the_repo(tmp_path, monkeypatch):
    """Geliştiricinin `.env` dosyası testleri etkilememeli.

    Sorun tam olarak buydu: depo kökündeki `.env` (UPSTREAM_STREAM_PATH,
    SESSION_REUSE, AUTH_DISABLED…) `Settings`'e sızıp 40+ testi bozuyordu.
    """
    env = tmp_path / ".env"
    env.write_text(
        "TARGET_DOMAIN=gercek-hedef.com\n"
        "UPSTREAM_STREAM_PATH=/nextjs-api/stream/create-evaluation\n"
        "SESSION_REUSE=true\n"
        "AUTH_DISABLED=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None, target_domain="beklenen.test")

    assert settings.target_domain == "beklenen.test"
    assert settings.upstream_stream_path == "/nextjs-api/stream/post-to-evaluation/{chat_id}"
    assert settings.session_reuse is False
    assert settings.auth_disabled is False
