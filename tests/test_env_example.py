"""Ortam şablonları uygulamanın gerçekten okuduğu adları ve varsayılanları taşımalı.

`Settings` bilmediği ortam değişkenini **sessizce yok sayar** (`extra="ignore"`).
Şablonda yanlış bir ad durursa operatör onu kopyalar, değeri değiştirir ve hiçbir
şey olmaz. Bu oldu: `.env.example` 19 Kasım 2026'ya dek sürüm damgası kanaryasının
ayarlarını `UPSTREAM_DEPLOYMENT_...` adlarıyla veriyordu; kod `DEPLOYMENT_...`
okur (`docs/LESSONS_2026_11_14.md` §19). Bu testler aynı sürüklenmeyi yakalar.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.telegram.bot import TelegramSettings

ROOT = Path(__file__).resolve().parent.parent

#: Bu sürümün okumadığı, şablonda yalnız kayıt olarak duran satırların işareti.
ABSENT_MARK = "[bu sürümde yok]"

_ACTIVE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_COMMENTED = re.compile(r"^#\s*(?:⛔\s*)?(\[bu sürümde yok\]\s*)?([A-Z][A-Z0-9_]*)=")

#: `.env.example` içinde koddaki varsayılandan BİLEREK farklı olan satırlar: hepsi
#: gözlenen hedefe özgü örnek değerlerdir (kodun varsayılanı hedef-bağımsız kalır).
_TARGET_SPECIFIC = frozenset(
    {
        "TARGET_DOMAIN",
        "UPSTREAM_STREAM_PATH",
        "UPSTREAM_REFERER_PATH",
        "UPSTREAM_MODE",
        "UPSTREAM_ACCEPT_LANGUAGE",
        "UPSTREAM_USER_AGENT",
        "UPSTREAM_EXTRA_HEADERS",
        "UPSTREAM_ACCOUNT_NAME",
        "RECAPTCHA_SITE_KEY",
        "RECAPTCHA_VERSION",
        "RECAPTCHA_ACTION",
    }
)


def _known_fields() -> dict[str, object]:
    """Ortam değişkeni adı → koddaki varsayılan değer."""
    known: dict[str, object] = {}
    for cls in (Settings, TelegramSettings):
        for name, field in cls.model_fields.items():
            default = field.default_factory() if field.default_factory else field.default  # type: ignore[call-arg]
            known[name.upper()] = default
    return known


def _as_env_text(value: object) -> str:
    """Varsayılan değerin `.env` dosyasında yazılacağı biçim."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(item) for item in value)
    return str(value)


def _lines(name: str) -> list[tuple[int, str]]:
    text = (ROOT / name).read_text(encoding="utf-8")
    return list(enumerate(text.splitlines(), 1))


@pytest.mark.parametrize("template", [".env.example", "env.mobile.example"])
def test_template_sets_only_variables_the_app_reads(template: str) -> None:
    known = _known_fields()
    unknown = [
        f"{template}:{number}: {match.group(1)}"
        for number, line in _lines(template)
        if (match := _ACTIVE.match(line)) and match.group(1) not in known
    ]
    assert not unknown, (
        "Şablon, uygulamanın okumadığı adlar ayarlıyor (değer sessizce yok sayılır):\n"
        + "\n".join(unknown)
    )


@pytest.mark.parametrize("template", [".env.example", "env.mobile.example"])
def test_commented_examples_name_real_variables_unless_marked_absent(template: str) -> None:
    """Yorum satırındaki `# AD=` örnekleri de kopyalanır; onlar da gerçek olmalı.

    Bu sürümde olmayan bir ad kayıt olarak tutulacaksa satır açıkça
    ``[bu sürümde yok]`` diye işaretlenir.
    """
    known = _known_fields()
    problems: list[str] = []
    for number, line in _lines(template):
        match = _COMMENTED.match(line)
        if not match:
            continue
        marked_absent, name = bool(match.group(1)), match.group(2)
        if name in known and marked_absent:
            problems.append(f"{template}:{number}: {name} okunuyor ama '{ABSENT_MARK}' işaretli")
        if name not in known and not marked_absent:
            problems.append(f"{template}:{number}: {name} okunmuyor ve işaretlenmemiş")
    assert not problems, "\n".join(problems)


def test_env_example_defaults_match_the_code() -> None:
    """Şablondaki değer koddaki varsayılanla aynı olmalı (hedefe özgü örnekler hariç).

    Fark, ya şablonun bayatladığını ya da kodda varsayılanın değiştiğini gösterir;
    ikisinde de şablonu kopyalayan kurulum belgelenenden farklı davranır.
    """
    known = _known_fields()
    drift: list[str] = []
    seen: set[str] = set()
    for number, line in _lines(".env.example"):
        match = _ACTIVE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        assert name not in seen, f".env.example:{number}: {name} iki kez tanımlı"
        seen.add(name)
        if name in _TARGET_SPECIFIC or name not in known:
            continue
        expected = _as_env_text(known[name])
        if value != expected:
            drift.append(f".env.example:{number}: {name}={value!r}, kod varsayılanı {expected!r}")
    assert not drift, "\n".join(drift)
    stale = sorted(_TARGET_SPECIFIC - seen)
    assert not stale, f"_TARGET_SPECIFIC içinde şablonda olmayan ad var: {stale}"


def test_env_example_keeps_the_measured_recaptcha_provider() -> None:
    """`static` kalıcı değerdir: `browser` hedefte 403 alıyor (docs/LESSONS §3).

    Şablonda `browser` yazılıysa cURL içe aktarımı statik token'ı atar
    (`curl_import.build_settings`) ve kurulum hiç cevap veremez.
    """
    values = {
        match.group(1): match.group(2).strip()
        for _, line in _lines(".env.example")
        if (match := _ACTIVE.match(line))
    }
    assert values["RECAPTCHA_PROVIDER"] == "static"
    assert Settings.model_fields["recaptcha_provider"].default == "static"


def test_deployment_canary_settings_have_no_upstream_prefix() -> None:
    """Belgelerde en uzun süre yanlış yazılan adlar: başlarında `UPSTREAM_` yok."""
    known = _known_fields()
    for name in (
        "DEPLOYMENT_ID_REFRESH_ENABLED",
        "DEPLOYMENT_ID_TTL_SECONDS",
        "DEPLOYMENT_PROBE_PATH",
        "DEPLOYMENT_PROBE_TIMEOUT",
        "DEPLOYMENT_ID_AUTO_RETRY",
    ):
        assert name in known
        assert f"UPSTREAM_{name}" not in known
