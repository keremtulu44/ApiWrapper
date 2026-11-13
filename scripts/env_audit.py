#!/usr/bin/env python3
"""Kendi ``.env`` dosyanı uygulamanın gerçekte okuduğu adlara göre denetler.

Neden var: bu depoda en pahalı hata sınıfı "yanlış katmana bakmak" değil, **hiç
okunmayan bir anahtarı** düzeltmeye çalışmak. ``.env`` içinde ``Settings``
alanıyla eşleşmeyen bir ad yazılıysa uygulama onu sessizce yok sayar
(``extra="ignore"``), sen değeri değiştirirsin, hiçbir şey değişmez.

Betik dört şeyi ayırır:

* ``dead``        — adı hiçbir ayarla eşleşmiyor (yakın bir ad varsa önerilir).
* ``placeholder`` — gizli alanda "boş" yer tutucusu var; yüklenirken boşaltılır.
* ``never_pin``   — istek/oturum başına üretilen değer; sabitlenmesi 401/403/409 üretir.
* ``gap``         — katman kararına göre eksik ya da zayıf kalan gerçek ayar.

Gizli değerler hiçbir zaman yazdırılmaz; yalnızca "var / yok" bilgisi çıkar.

Kullanım::

    python -m scripts.env_audit                # ./.env
    python -m scripts.env_audit prod.env --json
    python -m scripts.env_audit --strict       # bulgu varsa çıkış kodu 1
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from app.core.config import (
    _PLACEHOLDER_VALUE_RE,
    PLACEHOLDER_SECRET_FIELDS,
    Settings,
)
from app.services.layer_verdict import build_layer_verdict

#: Ayar sınıflarına ait olmayıp sürece Render'ın kendisi tarafından konan adlar.
ENV_ONLY_KEYS: frozenset[str] = frozenset({"RENDER", "PYTHON_VERSION", "PYTHONUNBUFFERED"})

#: Ad çözümlenemediğinde denenen önekler (ör. ``UPSTREAM_DEPLOYMENT_ID_TTL_SECONDS``).
_PREFIX_CANDIDATES: tuple[str, ...] = ("UPSTREAM_", "DEPLOYMENT_", "RECAPTCHA_", "TELEGRAM_")

#: İstek/oturum başına üretilen, bu yüzden ``.env``'e sabitlenmemesi gereken adlar.
NEVER_PIN: dict[str, str] = {
    "UPSTREAM_CX": (
        "`cx` istek başına üretilir; sabit değer ikinci sohbet isteğinde reddedilir."
    ),
    "RECAPTCHA_STATIC_TOKEN": (
        "statik captcha token tek kullanımlıktır ve saniyeler içinde ölür; "
        "sabitlenirse ilk istekten sonra 403 gelir."
    ),
    "RECAPTCHA_STATIC_TOKEN_2": "statik captcha token tek kullanımlıktır "
    "(bkz. RECAPTCHA_STATIC_TOKEN).",
    "RECAPTCHA_STATIC_TOKEN_3": "statik captcha token tek kullanımlıktır "
    "(bkz. RECAPTCHA_STATIC_TOKEN).",
    "RECAPTCHA_STATIC_TOKEN_4": "statik captcha token tek kullanımlıktır "
    "(bkz. RECAPTCHA_STATIC_TOKEN).",
}

#: Yakalanan cURL'ün tarayıcı parmak izini taşıyan, cihazla birebir eşleşmesi
#: gereken adlar. Boşlarsa istek gerçek tarayıcıdan ayırt edilebilir hale gelir.
DEVICE_KEYS: tuple[str, ...] = (
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "UPSTREAM_MODE",
    "UPSTREAM_REFERER_PATH",
    "UPSTREAM_STREAM_PATH",
    "UPSTREAM_CAPTURED_MODEL_ID",
    "RECAPTCHA_SITE_KEY",
    "RECAPTCHA_ACTION",
)

_SEVERITY: dict[str, int] = {"dead": 0, "never_pin": 1, "placeholder": 1, "shadow": 1, "gap": 2}

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("dead", "ÖLÜ ANAHTARLAR (uygulama bu adı okumuyor)"),
    ("placeholder", "YER TUTUCU GİZLİLER (yüklenirken boşaltılır)"),
    ("never_pin", "SABİTLENMEMESİ GEREKENLER"),
    ("shadow", "BİRBİRİNİ EZEN ANAHTARLAR"),
    ("gap", "KATMAN EKSİKLERİ"),
)


@dataclass(slots=True)
class Finding:
    """Tek bir denetim bulgusu. Gizli değer taşımaz."""

    key: str
    kind: str
    layer: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "layer": self.layer,
            "detail": self.detail,
            "fix": self.fix or None,
        }


@dataclass(slots=True)
class AuditReport:
    """Dosyanın tamamı için sonuç."""

    path: Path
    layer: str
    summary: str
    findings: list[Finding] = field(default_factory=list)
    device: dict[str, bool] = field(default_factory=dict)
    key_count: int = 0

    def of_kind(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "layer": self.layer,
            "summary": self.summary,
            "key_count": self.key_count,
            "device": self.device,
            "findings": [f.as_dict() for f in self.findings],
        }

    def render(self) -> str:
        lines = [
            f".env denetimi — {self.path} ({self.key_count} anahtar)",
            f"Katman: {self.layer} — {self.summary}",
        ]
        for kind, title in _SECTIONS:
            items = self.of_kind(kind)
            lines.append("")
            lines.append(f"{title}  [{len(items)}]")
            if not items:
                lines.append("  yok")
                continue
            for item in items:
                lines.append(f"  {item.key}: {item.detail}")
                if item.fix:
                    lines.append(f"      → {item.fix}")
        filled = sum(1 for value in self.device.values() if value)
        lines.append("")
        lines.append(f"{'CİHAZ İZİ':<32} [{filled}/{len(self.device)}]")
        missing = [key for key, value in self.device.items() if not value]
        if missing:
            lines.append(f"  boş: {', '.join(missing)}")
        return "\n".join(lines)


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=VALUE`` satırlarını sözlüğe çevirir; yorumları ve tırnakları atar."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def known_fields() -> dict[str, str]:
    """Ayar sınıflarının alan adları: ENV_ADI -> alan adı."""
    fields = {name.upper(): name for name in Settings.model_fields}
    # Telegram ayarları ayrı bir BaseSettings sınıfında; anahtarları ölü sayılmasın.
    try:
        from app.telegram.bot import TelegramSettings

        fields.update({name.upper(): name for name in TelegramSettings.model_fields})
    except Exception:  # pragma: no cover - bot modülü yoksa yalnız Settings denetlenir
        pass
    return fields


def _suggest(key: str, known: dict[str, str]) -> str:
    """Yanlış yazılmış ada en yakın gerçek adı önerir (önek ekle/kaldır)."""
    if key in known:
        return ""
    stripped = key
    for prefix in _PREFIX_CANDIDATES:
        if key.startswith(prefix):
            stripped = key[len(prefix) :]
            break
    for candidate in (stripped, f"UPSTREAM_{stripped}", f"DEPLOYMENT_{stripped}"):
        if candidate != key and candidate in known:
            return candidate
    return ""


def settings_from(values: dict[str, str], known: dict[str, str]) -> Settings:
    """Dosyadaki değerlerden ``Settings`` üretir (gerçek ortam değişkenlerini ezerek)."""
    kwargs = {
        known[key]: value for key, value in values.items() if key in known and value != ""
    }
    # Alan adları dinamik; pydantic doğrulaması çalışma anında yapılır.
    factory: Any = Settings
    return cast(Settings, factory(_env_file=None, **kwargs))


def audit_env(path: Path) -> AuditReport:
    """``path`` dosyasını okur ve denetim raporunu döner."""
    text = path.read_text(encoding="utf-8")
    values = parse_env_text(text)
    known = known_fields()

    report = AuditReport(path=path, layer="config", summary="", key_count=len(values))
    #: Ölü adlar, öneri var mı bilgisiyle (aynı adı iki kez raporlamamak için).
    seen: set[str] = set()

    for key in values:
        if key in known or key in ENV_ONLY_KEYS or key in seen:
            continue

        if key in NEVER_PIN:
            report.findings.append(
                Finding(key, "never_pin", "captcha", NEVER_PIN[key], f"{key} satırını silin.")
            )
            continue

        suggestion = _suggest(key, known)
        report.findings.append(
            Finding(
                key,
                "dead",
                "config",
                suggestion
                    and f"Bu adı uygulama okumuyor; doğru ad {suggestion}."
                    or "Bu adı hiçbir ayar alanı okumuyor; değer sessizce yok sayılıyor.",
                suggestion and f"{key} → {suggestion}" or f"{key} satırını silin.",
            )
        )
        seen.add(key)

    for key in NEVER_PIN:
        if key in values and key in known and values[key]:
            report.findings.append(
                Finding(key, "never_pin", "captcha", NEVER_PIN[key], f"{key} değerini boşaltın.")
            )

    for key in PLACEHOLDER_SECRET_FIELDS:
        env_key = key.upper()
        if values.get(env_key) and _PLACEHOLDER_VALUE_RE.match(values[env_key].strip()):
            report.findings.append(
                Finding(
                    env_key,
                    "placeholder",
                    "identity",
                    "Değer bir 'boş' yer tutucusu; yüklenirken boşaltılır.",
                    f"{env_key} satırını silin (boş değer zaten aynı işi yapar).",
                )
            )

    extra_headers = values.get("UPSTREAM_EXTRA_HEADERS", "").lower()
    if "x-deployment-id" in extra_headers or "x-arena-web-deployment" in extra_headers:
        report.findings.append(
            Finding(
                "UPSTREAM_EXTRA_HEADERS",
                "never_pin",
                "deployment",
                "Sürüm damgası elle sabitlenmiş; her deploy'da değişir.",
                "Damga başlığını buradan çıkarın; kanarya güncel değeri kendisi okur.",
            )
        )
    if values.get("UPSTREAM_CURL") and values.get("UPSTREAM_COOKIE"):
        report.findings.append(
            Finding(
                "UPSTREAM_COOKIE",
                "shadow",
                "identity",
                "Açılışta UPSTREAM_CURL, UPSTREAM_COOKIE'yi eziyor.",
                "Tek kaynak bırakın: yeni oturumu UPSTREAM_CURL ile verin, "
                "çerezi ayrıca yazmayın.",
            )
        )

    settings = settings_from(values, known)
    verdict = build_layer_verdict(settings)
    for check in verdict.checks:
        if check.status in {"ok", "unknown"}:
            continue
        env_key = _env_key_for_check(check.id)
        report.findings.append(
            Finding(
                key=env_key or check.id,
                kind="gap",
                layer=check.layer,
                detail=check.detail,
                fix=check.fix,
            )
        )

    report.layer = verdict.layer
    report.summary = verdict.summary
    report.device = {key: bool(values.get(key, "").strip()) for key in DEVICE_KEYS}
    report.findings.sort(key=lambda item: (_SEVERITY.get(item.kind, 9), item.key))
    return report


_CHECK_KEYS: dict[str, str] = {
    "target_domain": "TARGET_DOMAIN",
    "authorization_header": "UPSTREAM_AUTH_FROM_COOKIE",
    "access_token": "UPSTREAM_ACCESS_TOKEN",
    "refresh_token": "UPSTREAM_REFRESH_TOKEN",
    "refresh_api_key": "UPSTREAM_REFRESH_API_KEY",
    "refresh_leeway": "UPSTREAM_REFRESH_LEEWAY_SECONDS",
    "refresh_last_attempt": "UPSTREAM_REFRESH_URL",
    "deployment_stamp": "DEPLOYMENT_ID_TTL_SECONDS",
    "cx_send": "UPSTREAM_CX_SEND",
    "recaptcha": "RECAPTCHA_PROVIDER",
}


def _env_key_for_check(check_id: str) -> str:
    """Katman kontrolünü ilgili ortam değişkenine bağlar."""
    return _CHECK_KEYS.get(check_id, "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="`.env` dosyasını katman katman denetler.")
    parser.add_argument("path", nargs="?", default=".env", help="denetlenecek dosya (.env)")
    parser.add_argument("--json", action="store_true", help="makine okunur çıktı")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="ölü/yer tutucu/ezilen anahtar varsa çıkış kodu 1",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"Dosya yok: {path}", file=sys.stderr)
        return 2

    report = audit_env(path)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render())

    blocking = [f for f in report.findings if f.kind in {"dead", "placeholder", "shadow"}]
    if args.strict and blocking:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
