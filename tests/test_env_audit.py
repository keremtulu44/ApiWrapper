"""`scripts/env_audit.py` davranışı: ölü ad, yer tutucu, sabitlenmemesi gerekenler."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.env_audit import (
    ENV_ONLY_KEYS,
    audit_env,
    known_fields,
    main,
    parse_env_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def _kinds(report, kind: str) -> list[str]:
    return [finding.key for finding in report.of_kind(kind)]


def test_parse_env_text_strips_quotes_comments_and_export(tmp_path: Path) -> None:
    values = parse_env_text(
        """
        # yorum
        export TARGET_DOMAIN=example.com
        UPSTREAM_COOKIE="a=1; b=2"
        BOŞ SATIR

        UPSTREAM_MODE='direct-battle'
        """
    )
    assert values == {
        "TARGET_DOMAIN": "example.com",
        "UPSTREAM_COOKIE": "a=1; b=2",
        "UPSTREAM_MODE": "direct-battle",
    }


def test_prefixed_deployment_key_is_dead_and_suggested(tmp_path: Path) -> None:
    """`.env.example` bir dönem `UPSTREAM_DEPLOYMENT_*` yazıyordu; uygulama okumuyor."""
    path = _write(tmp_path, "TARGET_DOMAIN=example.com\nUPSTREAM_DEPLOYMENT_ID_TTL_SECONDS=60\n")

    report = audit_env(path)

    dead = report.of_kind("dead")
    assert [finding.key for finding in dead] == ["UPSTREAM_DEPLOYMENT_ID_TTL_SECONDS"]
    assert "DEPLOYMENT_ID_TTL_SECONDS" in dead[0].fix
    assert "UPSTREAM_" not in dead[0].fix.split("→")[1]


def test_unknown_key_without_near_name_still_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "UPSTREAM_HEADER_STYLE=bearer\nTARGET_DOMAIN=example.com\n")

    report = audit_env(path)

    assert _kinds(report, "dead") == ["UPSTREAM_HEADER_STYLE"]
    assert "sessizce yok sayılıyor" in report.of_kind("dead")[0].detail


def test_placeholder_secret_is_flagged_and_never_printed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "TARGET_DOMAIN=example.com\nUPSTREAM_ACCESS_TOKEN=APIWRAPPER_EMPTY_VALUE_V1\n",
    )

    report = audit_env(path)

    assert _kinds(report, "placeholder") == ["UPSTREAM_ACCESS_TOKEN"]
    rendered = report.render()
    assert "APIWRAPPER_EMPTY_VALUE_V1" not in rendered


def test_cx_and_static_captcha_are_never_pin(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "TARGET_DOMAIN=example.com\nUPSTREAM_CX=abc123\nRECAPTCHA_STATIC_TOKEN=xyz\n",
    )

    report = audit_env(path)

    assert sorted(_kinds(report, "never_pin")) == ["RECAPTCHA_STATIC_TOKEN", "UPSTREAM_CX"]


def test_empty_cx_is_not_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "TARGET_DOMAIN=example.com\nUPSTREAM_CX=\n")

    assert audit_env(path).of_kind("never_pin") == []


def test_deployment_header_inside_extra_headers_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        'TARGET_DOMAIN=example.com\nUPSTREAM_EXTRA_HEADERS="x-deployment-id=dpl_abc"\n',
    )

    report = audit_env(path)

    finding = report.of_kind("never_pin")[0]
    assert finding.key == "UPSTREAM_EXTRA_HEADERS"
    assert finding.layer == "deployment"


def test_curl_shadowing_cookie_is_reported(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "TARGET_DOMAIN=example.com\n"
        "UPSTREAM_CURL=curl 'https://example.com/api'\n"
        "UPSTREAM_COOKIE=session=abc\n",
    )

    report = audit_env(path)

    assert _kinds(report, "shadow") == ["UPSTREAM_COOKIE"]


def test_layer_gap_comes_from_verdict(tmp_path: Path) -> None:
    """Hedef alan adı yoksa katman `config` olmalı ve gap listesinde görünmeli."""
    path = _write(tmp_path, "UPSTREAM_MODE=direct-battle\n")

    report = audit_env(path)

    assert report.layer == "config"
    assert "TARGET_DOMAIN" in _kinds(report, "gap")
    assert "TARGET_DOMAIN" in report.summary


def test_clean_env_only_reports_device_gaps(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "TARGET_DOMAIN=example.com\n"
        "UPSTREAM_AUTH_FROM_COOKIE=true\n"
        "UPSTREAM_COOKIE=session=abc\n"
        "UPSTREAM_ACCESS_TOKEN=eyJhbGciOiJIUzI1NiJ9.x.y\n",
    )

    report = audit_env(path)

    assert report.of_kind("dead") == []
    assert report.of_kind("placeholder") == []
    assert report.of_kind("never_pin") == []
    assert report.of_kind("shadow") == []


def test_render_and_json_are_consistent(tmp_path: Path) -> None:
    path = _write(tmp_path, "TARGET_DOMAIN=example.com\nUPSTREAM_HEADER_STYLE=bearer\n")

    report = audit_env(path)
    payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))

    assert payload["key_count"] == 2
    assert payload["findings"][0]["kind"] == "dead"
    assert "ÖLÜ ANAHTARLAR" in report.render()


def test_main_missing_file_returns_2(tmp_path: Path, capsys) -> None:
    assert main([str(tmp_path / "yok.env")]) == 2
    assert "Dosya yok" in capsys.readouterr().err


def test_main_strict_fails_only_on_blocking_findings(tmp_path: Path) -> None:
    dead = _write(tmp_path, "TARGET_DOMAIN=example.com\nNOPE_SETTING=1\n")
    clean = tmp_path / "clean.env"
    clean.write_text("TARGET_DOMAIN=example.com\n", encoding="utf-8")

    assert main([str(dead), "--strict"]) == 1
    assert main([str(clean), "--strict"]) == 0


def test_env_example_contains_no_dead_keys() -> None:
    """Örnek dosyadaki her ad (yorumlu satırlar dahil) gerçekten okunmalı."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    known = known_fields()
    keys = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", text, re.M))

    dead = sorted(key for key in keys if key not in known and key not in ENV_ONLY_KEYS)

    assert dead == [], f"`.env.example` okunmayan adlar içeriyor: {dead}"


def test_env_example_audit_is_clean() -> None:
    report = audit_env(REPO_ROOT / ".env.example")

    assert report.of_kind("dead") == []
    assert report.of_kind("placeholder") == []
    assert report.of_kind("shadow") == []


def test_inline_comment_is_parsed_like_runtime(tmp_path: Path) -> None:
    """`KEY=false  # not` runtime'da `false` demek; denetim de öyle görmeli."""
    path = _write(
        tmp_path,
        "TARGET_DOMAIN=example.com\nUPSTREAM_CX_SEND=false  # gönderilmesin\n",
    )

    report = audit_env(path)

    assert report.of_kind("broken") == []
    values = parse_env_text(path.read_text(encoding="utf-8"))
    assert values["UPSTREAM_CX_SEND"] == "false"


def test_unparseable_value_is_reported_instead_of_crashing(tmp_path: Path) -> None:
    """Bozuk değer denetimi patlatmamalı; 'dosya açılmıyor' demeli."""
    path = _write(tmp_path, "TARGET_DOMAIN=example.com\nUPSTREAM_CX_SEND=belki\n")

    report = audit_env(path)

    assert _kinds(report, "broken") == ["UPSTREAM_CX_SEND"]
    assert report.layer == "config"
    assert "açılmıyor" in report.summary


def test_wrapping_quotes_are_stripped_for_every_entry_point() -> None:
    """Render panosu ve compose tırnakları silmez; başlığa tırnak gitmemeli."""
    from app.core.config import Settings
    from app.upstream.headers import build_stream_headers

    settings = Settings(
        _env_file=None,
        target_domain='"example.com"',
        upstream_accept_language='"tr,en;q=0.9,en-GB;q=0.8"',
        api_keys='"k1,k2"',
        upstream_user_agent="Mozilla/5.0 (Windows NT 10.0) Chrome/154",
    )

    assert build_stream_headers(settings, "c1")["accept-language"] == "tr,en;q=0.9,en-GB;q=0.8"
    assert settings.target_domain == "example.com"
    assert settings.api_keys == ["k1", "k2"]


def test_quotes_inside_a_value_are_left_alone() -> None:
    from app.core.config import Settings

    settings = Settings(_env_file=None, target_domain='exa"mple.com')

    assert settings.target_domain == 'exa"mple.com'
