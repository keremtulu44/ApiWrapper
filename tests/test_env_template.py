"""`scripts/env_template.py`: tam liste eksiksiz, alfabetik ve bayatlamıyor mu?"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings
from app.telegram.bot import TelegramSettings
from scripts.env_audit import audit_env
from scripts.env_template import (
    DEFAULT,
    DOC_PATH,
    EMPTY,
    NEVER,
    WRITE,
    entry_for,
    main,
    render_env,
    render_markdown,
    rows,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _all_keys() -> set[str]:
    keys = {name.upper() for name in Settings.model_fields}
    keys |= {name.upper() for name in TelegramSettings.model_fields}
    return keys


def test_every_setting_appears_exactly_once() -> None:
    keys = [row[0] for row in rows()]

    assert sorted(keys) == sorted(_all_keys())
    assert len(keys) == len(set(keys))


def test_rows_are_alphabetical() -> None:
    keys = [row[0] for row in rows()]

    assert keys == sorted(keys)


def test_every_row_has_a_marker_and_text() -> None:
    for key, marker, text, group in rows():
        assert marker in {WRITE, EMPTY, NEVER, DEFAULT}, key
        assert text.strip(), key
        assert group.strip(), key


def test_slot_fields_are_documented_as_optional() -> None:
    """3./4. yuva alanları listeye giriyor ama 'boş bırak' diyor."""
    by_key = {row[0]: row for row in rows()}

    for key in ("TARGET_DOMAIN_3", "UPSTREAM_COOKIE_4", "RECAPTCHA_STATIC_TOKEN_3"):
        assert key in by_key
        assert by_key[key][1] == EMPTY
        assert "yuva" in by_key[key][2]


def test_per_request_values_are_never_marked_write() -> None:
    by_key = {row[0]: row for row in rows()}

    for key in ("UPSTREAM_CX", "RECAPTCHA_STATIC_TOKEN"):
        assert by_key[key][1] == NEVER


def test_documented_file_is_up_to_date() -> None:
    """Doküman `Settings` ile aynı commit'te güncellenmeli."""
    assert DOC_PATH.exists(), "docs/ENV_FULL.md yok: python -m scripts.env_template"
    assert DOC_PATH.read_text(encoding="utf-8") == render_markdown(), (
        "docs/ENV_FULL.md bayat: python -m scripts.env_template"
    )


def test_check_mode_detects_drift(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "ENV_FULL.md"
    target.write_text("eski içerik", encoding="utf-8")
    monkeypatch.setattr("scripts.env_template.DOC_PATH", target)

    assert main(["--check"]) == 1
    assert main([]) == 0
    assert main(["--check"]) == 0


def test_markdown_has_one_row_per_key_and_no_broken_cells() -> None:
    text = render_markdown()
    table_rows = [line for line in text.splitlines() if line.startswith("| `")]

    assert len(table_rows) == len(_all_keys())
    assert all(line.count("|") == 4 for line in table_rows)


def test_env_skeleton_parses_clean(tmp_path: Path) -> None:
    """`--env` çıktısı denetimden ölü anahtar/yer tutucu olmadan geçmeli."""
    path = tmp_path / ".env"
    path.write_text(render_env(), encoding="utf-8")

    report = audit_env(path)

    assert report.of_kind("dead") == []
    assert report.of_kind("placeholder") == []


def test_env_skeleton_writes_the_required_values() -> None:
    text = render_env()

    for key in ("TARGET_DOMAIN=", "API_KEYS=", "RECAPTCHA_PROVIDER=browser"):
        assert re.search(rf"^{re.escape(key)}", text, re.M), key
    # Varsayılanlar iskelete yazılmaz; gürültü olur.
    assert "BREAKER_FAILURE_THRESHOLD=" not in text


def test_unknown_defaults_fall_back_to_a_sentence() -> None:
    """Küratörlü metni olmayan alanlar için otomatik cümle üretilir."""
    field = Settings.model_fields["breaker_reset_timeout"]
    marker, text = entry_for("BREAKER_RESET_TIMEOUT", field)

    assert marker == DEFAULT
    assert "30" in text
