#!/usr/bin/env python3
"""Ayakta olan servise tek komutla istek atar ve arızayı katmanıyla söyler.

Telefon/DevTools gerekmez: bu betik sunucuya karşı çalışır. Ne yaptığı:

1. ``/healthz`` — süreç ayakta mı.
2. ``/readyz``  — trafiğe hazır mı (hedef alan adı, kimlik dosyası, ...).
3. ``/v1/admin/auth`` — ``verdict``: hangi katman koptu (config/identity/deployment/captcha).
4. ``/v1/chat/completions`` — gerçek istek: durum kodu, süre, hata sınıfı.
5. ``--stream`` ile aynı istek akış olarak: ilk olay geliyor mu.

Hata kodları katmana çevrilir: 401 kimlik, 403 anti-bot/CAPTCHA, 409 sürüm
damgası, 429 kota, 404 akış yolu.

Kullanım::

    python -m scripts.smoke --base https://servis.onrender.com --key sk-...
    python -m scripts.smoke --base http://127.0.0.1:8000 --key sk-... --stream
    python -m scripts.smoke --base ... --key ... --json
    python -m scripts.smoke --base ... --key ... --skip-chat   # sadece sağlık

Gizli değer yazdırmaz; anahtarlar maskelenir.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

#: HTTP durum kodlarının ait olduğu katman. Teşhis katmanıyla aynı eşleme.
STATUS_LAYERS: dict[int, tuple[str, str]] = {
    401: ("identity", "Erişim token'ı yok ya da süresi dolmuş."),
    403: ("captcha", "Anti-bot/CAPTCHA katmanı reddetti (Cloudflare çerezi veya token)."),
    404: ("config", "Akış yolu yanlış (UPSTREAM_STREAM_PATH)."),
    409: ("deployment", "Sürüm damgası bayat (x-deployment-id)."),
    429: ("quota", "Upstream kotası/kilit; tekrar denemek kilidi uzatır."),
    502: ("config", "Upstream bağlantısı kurulamadı (hedef alan adı/şema)."),
    503: ("config", "Servis hazır değil; /readyz gövdesine bakın."),
}


@dataclass(slots=True)
class Check:
    """Tek bir duman testi adımı."""

    name: str
    status: str
    detail: str
    hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass(slots=True)
class Report:
    base: str
    checks: list[Check] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if any(check.failed for check in self.checks) else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "ok": self.exit_code == 0,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "hint": check.hint or None,
                    "data": check.data,
                }
                for check in self.checks
            ],
        }

    def render(self, ascii_only: bool = False) -> str:
        """Raporu metne çevirir.

        ``ascii_only`` Windows PowerShell/CI günlükleri içindir: emoji yerine
        ``[OK]``/``[!]``/``[X]`` yazar (cp1254 konsolunda emoji satırı bozmasın).
        """
        icons = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
        if ascii_only:
            icons = {"ok": "[OK]", "warn": "[!]", "fail": "[X]"}
        lines = [f"Duman testi — {self.base}"]
        for check in self.checks:
            marker = icons.get(check.status, "[-]" if ascii_only else "•")
            lines.append(f"  {marker} {check.name}: {check.detail}")
            if check.hint:
                lines.append(f"      → {check.hint}")
        first_fail = next((c for c in self.checks if c.failed), None)
        lines.append("")
        if first_fail:
            lines.append(f"SONUÇ: {first_fail.name} adımında takıldı.")
        else:
            lines.append("SONUÇ: servis istek atılabilir durumda.")
        return "\n".join(lines)


def mask(value: str) -> str:
    """Anahtarı loglanabilir hale getirir."""
    if not value:
        return "(yok)"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}…{value[-2:]}"


def _layer_for(status: int) -> tuple[str, str]:
    return STATUS_LAYERS.get(status, ("unknown", f"Beklenmeyen HTTP {status}."))


def _connect_error(name: str, exc: httpx.HTTPError) -> Check:
    """Ağ hatasını (yanlış adres, uyuyan servis, DNS) anlaşılır Check'e çevirir."""
    return Check(
        name,
        "fail",
        f"bağlantı kurulamadı ({type(exc).__name__})",
        hint=(
            f"{str(exc)[:160]} — adres doğru mu, servis uyanık mı (Render ücretsiz "
            "plan uyuyabilir), ağ/VPN engelliyor mu? Tarayıcıda /healthz açılıyor mu?"
        ),
    )


def _run_step(report: Report, name: str, step: Any) -> Check:
    """Bir adımı çalıştırır; ağ/DNS hatasında raporu çökertmeden Check döner."""
    try:
        result: Check = step()
    except httpx.HTTPError as exc:
        result = _connect_error(name, exc)
    report.checks.append(result)
    return result


def _body_text(response: httpx.Response, limit: int = 300) -> str:
    text = response.text or ""
    text = " ".join(text.split())
    return text[:limit]


def check_healthz(client: httpx.Client, base: str) -> Check:
    response = client.get(f"{base}/healthz")
    if response.status_code == 200:
        return Check("healthz", "ok", "süreç ayakta", data={"status": response.status_code})
    _layer, why = _layer_for(response.status_code)
    return Check("healthz", "fail", f"HTTP {response.status_code}", hint=why)


def check_readyz(client: httpx.Client, base: str) -> Check:
    response = client.get(f"{base}/readyz")
    body = _body_text(response)
    if response.status_code == 200:
        return Check("readyz", "ok", "trafiğe hazır", data={"body": body})
    hint = body or _layer_for(response.status_code)[1]
    return Check(
        "readyz",
        "fail",
        f"HTTP {response.status_code}",
        hint=hint,
        data={"body": body},
    )


def check_admin_auth(client: httpx.Client, base: str, admin_key: str) -> Check:
    """`/v1/admin/auth` yargısını okur (katman + ilk düzeltme)."""
    if not admin_key:
        return Check("admin_auth", "warn", "admin anahtarı verilmedi; katman teşhisi atlandı")
    response = client.get(
        f"{base}/v1/admin/auth", headers={"Authorization": f"Bearer {admin_key}"}
    )
    if response.status_code == 401:
        return Check(
            "admin_auth",
            "fail",
            "admin anahtarı reddedildi",
            hint=(
                "/v1/admin önce API_KEYS doğrular: admin anahtarı API_KEYS içinde "
                "de olmalı. Değilse --admin-key yerine --key ile tekrar deneyin."
            ),
        )
    if response.status_code != 200:
        _layer, why = _layer_for(response.status_code)
        return Check("admin_auth", "fail", f"HTTP {response.status_code}", hint=why)

    try:
        payload = response.json()
    except ValueError:
        return Check(
            "admin_auth",
            "warn",
            "200 ama gövde JSON değil",
            hint=_body_text(response, 160),
        )
    verdict = payload.get("verdict") or {}
    layer = str(verdict.get("layer") or "unknown")
    summary = str(verdict.get("summary") or "(özet yok)")
    failing = [
        check
        for check in verdict.get("checks", [])
        if check.get("status") in {"fail", "warn"}
    ]
    hint = next((str(c.get("fix")) for c in failing if c.get("fix")), "")
    status = "ok" if layer == "ok" else ("warn" if layer == "unknown" else "fail")
    return Check(
        "admin_auth",
        status,
        f"{layer} — {summary}",
        hint=hint,
        data={"verdict": verdict},
    )


def _chat_body(stream: bool) -> dict[str, Any]:
    return {
        "model": "max",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": stream,
        "max_tokens": 8,
    }


def check_chat(client: httpx.Client, base: str, api_key: str, model: str = "max") -> Check:
    body = _chat_body(stream=False) | {"model": model}
    started = time.monotonic()
    response = client.post(
        f"{base}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
    )
    elapsed = round(time.monotonic() - started, 2)
    if response.status_code == 200:
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError, TypeError):
            return Check(
                "chat",
                "warn",
                f"200 ama beklenmeyen gövde ({elapsed}s)",
                hint=_body_text(response, 120),
            )
        return Check(
            "chat",
            "ok",
            f"{elapsed}s · yanıt: {str(content).strip()[:40]!r}",
            data={"elapsed": elapsed},
        )

    layer, why = _layer_for(response.status_code)
    return Check(
        "chat",
        "fail",
        f"HTTP {response.status_code} · {layer} katmanı · {elapsed}s",
        hint=why + (f" · {_body_text(response)}" if _body_text(response) else ""),
        data={"status": response.status_code, "layer": layer},
    )


def check_stream(client: httpx.Client, base: str, api_key: str, model: str = "max") -> Check:
    body = _chat_body(stream=True) | {"model": model}
    started = time.monotonic()
    first_event = ""
    try:
        with client.stream(
            "POST",
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        ) as response:
            if response.status_code != 200:
                layer, why = _layer_for(response.status_code)
                return Check(
                    "stream",
                    "fail",
                    f"HTTP {response.status_code} · {layer} katmanı",
                    hint=why,
                )
            for line in response.iter_lines():
                if line.strip():
                    first_event = line.strip()
                    break
    except httpx.HTTPError as exc:
        return Check("stream", "fail", f"akış açılmadı: {type(exc).__name__}", hint=str(exc)[:160])
    elapsed = round(time.monotonic() - started, 2)
    if not first_event:
        return Check("stream", "fail", f"{elapsed}s içinde hiç olay gelmedi")
    return Check("stream", "ok", f"ilk olay {elapsed}s · {first_event[:60]}")


def run(
    base: str,
    *,
    api_key: str = "",
    admin_key: str = "",
    model: str = "max",
    chat: bool = True,
    stream: bool = False,
    client: httpx.Client | None = None,
) -> Report:
    """Tüm adımları sırayla çalıştırır ve raporu döner."""
    base = base.rstrip("/")
    report = Report(base=base)
    owns_client = client is None
    client = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        health = _run_step(report, "healthz", lambda: check_healthz(client, base))
        if health.failed:
            return report
        _run_step(report, "readyz", lambda: check_readyz(client, base))
        _run_step(
            report,
            "admin_auth",
            lambda: check_admin_auth(client, base, admin_key or api_key),
        )
        if not chat:
            return report
        _run_step(report, "chat", lambda: check_chat(client, base, api_key, model))
        if stream:
            _run_step(report, "stream", lambda: check_stream(client, base, api_key, model))
    finally:
        if owns_client:
            client.close()
    return report


def _make_output_safe() -> None:
    """Konsol kod sayfası Türkçe/emoji karakterleri basamıyorsa çökmeyi önler.

    Windows PowerShell 5.1 varsayılan olarak cp1254 kullanır; `print` bu durumda
    ``UnicodeEncodeError`` ile betiği düşürebiliyordu. UTF-8'e geçilemiyorsa
    basılamayan karakter ``?`` olur ama rapor yine okunur.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # pragma: no cover - çok eski Python/boru
            continue
        with contextlib.suppress(ValueError, OSError, AttributeError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ayaktaki servise duman testi.")
    parser.add_argument("--base", required=True, help="servis adresi (https://... veya http://127.0.0.1:8000)")
    parser.add_argument("--key", default="", help="API_KEYS değeri (sohbet isteği için)")
    parser.add_argument("--admin-key", default="", help="ADMIN_API_KEYS değeri (teşhis için)")
    parser.add_argument("--model", default="max", help="models.yaml içindeki model adı")
    parser.add_argument("--skip-chat", action="store_true", help="sohbet isteği atma")
    parser.add_argument("--stream", action="store_true", help="akış isteğini de dene")
    parser.add_argument("--json", action="store_true", help="makine okunur çıktı")
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="emoji yerine [OK]/[!]/[X] (Windows PowerShell, CI günlükleri)",
    )
    args = parser.parse_args(argv)
    _make_output_safe()

    report = run(
        args.base,
        api_key=args.key,
        admin_key=args.admin_key,
        model=args.model,
        chat=not args.skip_chat,
        stream=args.stream,
    )
    output = (
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        if args.json
        else report.render(ascii_only=args.ascii)
    )
    print(output)
    if args.key or args.admin_key:
        print(f"\nanahtarlar: api={mask(args.key)} admin={mask(args.admin_key)}", file=sys.stderr)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
