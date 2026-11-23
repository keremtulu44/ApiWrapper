#!/usr/bin/env python3
"""2. Max cURL'ünü güvenle yerleştirir ve **hiçbir şeyi ezmeden** raporlar.

Neden ayrı bir araç?
--------------------
`UPSTREAM_CURL_2` değeri 13.000+ karakterlik tek satırlık bir metindir; elle
kopyalanırken en sık yapılan hatalar: çerezin ortadan kesilmesi, `\\` satır
devamlarının kaybolması, tırnakların yarım kalması, cURL'ün PowerShell biçiminde
kopyalanması. Bu araç şunları yapar:

1. cURL'ü **olduğu gibi** okur (dosya, stdin veya panodan yapıştırılmış metin).
2. Doğrular: Max `create-evaluation` cURL'ü mü (Agent cURL'ü değil), çerez
   bütünlüğü, `modelAId`, hedef alan adı, 1. hesapla **aynı hesap mı**.
3. `--write` verildiyse `.env` dosyasındaki **yalnız** 2. yuvaya ait satırları
   değiştirir; diğer satırlara ve 1. cURL'e dokunmaz (yedek alır).

Kullanım
--------
    python scripts/put_curl2.py curl2.txt              # yalnız kontrol
    python scripts/put_curl2.py curl2.txt --write      # .env'e yaz
    pbpaste | python scripts/put_curl2.py - --write    # panodan (macOS)

Render için: komut çıktısındaki `UPSTREAM_CURL_2` satırını Render → Environment
bölümüne yapıştırın (dosya sistemi kalıcı olmadığı için .env yalnız yerelde).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.autopilot.curl_parse import (  # noqa: E402
    detect_pasted_curl_kind,
    extract_all_max_identities,
    extract_identity_subject,
    normalize_windows_cmd_curl_for_upstream,
)
from app.core.config import Settings  # noqa: E402
from app.services.account import Account  # noqa: E402
from app.upstream.curl_runtime import apply_upstream_curl  # noqa: E402

SLOT = 2
CURL_KEY = "UPSTREAM_CURL_2"
SHARED_KEYS = (
    "UPSTREAM_STREAM_PATH",
    "UPSTREAM_MODE",
    "UPSTREAM_RECAPTCHA_FIELD",
)


def _mask(value: str, keep: int = 6) -> str:
    text = (value or "").strip()
    if not text:
        return "(boş)"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]} ({len(text)} kr)"


def _read_curl(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    path = Path(arg)
    if not path.exists():
        raise SystemExit(f"Dosya bulunamadı: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _model_id_from_curl(curl_text: str) -> str:
    """cURL gövdesindeki `modelAId` (2. yuva için tek doğru kaynak).

    `apply_upstream_curl` model kimliğini yalnız **1. yuvadan** döndürür; 2. yuvanın
    gövdesini ayrıca okumak gerekir (aksi halde geçerli cURL "modelAId yok" sanılır).
    """
    match = re.search(r'modelAId"?\s*[:=]\s*"?([0-9a-zA-Z_-]{8,})', curl_text)
    return match.group(1) if match else ""


def _cookie_pieces(cookie: str) -> dict[str, str]:
    """Çerez adı → değer (parçalı `.0`/`.1` oturum çerezleri dahil)."""
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def check(
    curl_text: str,
    *,
    env: dict[str, str],
    base: Settings | None = None,
) -> tuple[bool, list[str], dict[str, str]]:
    """cURL'ü doğrular; (uygun_mu, sorunlar, çıkarılan_değerler) döndürür."""
    problems: list[str] = []
    cleaned = normalize_windows_cmd_curl_for_upstream((curl_text or "").strip())
    if not cleaned:
        return False, ["cURL metni boş."], {}

    kind = detect_pasted_curl_kind(cleaned)
    if kind == "agent":
        problems.append(
            "Bu bir Agent Mode cURL'ü (create-chat / api/chat). 2. Max yuvası için "
            "normal sohbetten 'create-evaluation' cURL'ü gerekir."
        )
    elif kind != "max":
        problems.append(
            "Bu metin Max 'create-evaluation' isteği gibi görünmüyor. Tarayıcıda "
            "normal bir sohbet mesajı gönderip Network sekmesinden 'Copy as cURL' alın."
        )

    if base is None:
        base = Settings(_env_file=None, api_keys=["check"])
    candidate = base.model_copy(update={"upstream_curl_2": cleaned})
    try:
        applied, model_id = apply_upstream_curl(candidate)
    except Exception as exc:  # noqa: BLE001 - kullanıcıya tek satır mesaj
        # `.env` içindeki 1. cURL bozuksa 2. hesabın kontrolü de patlamasın:
        # temiz bir tabanla yalnız 2. yuvayı doğrula ve durumu söyle.
        if base.upstream_curl.strip():
            clean = Settings(_env_file=None, api_keys=["check"])
            try:
                applied, model_id = apply_upstream_curl(
                    clean.model_copy(update={"upstream_curl_2": cleaned})
                )
                base = clean
                problems.append(
                    "Uyarı: .env içindeki UPSTREAM_CURL (1. hesap) ayrıştırılamıyor "
                    f"({exc}). 2. hesap tek başına doğrulandı; 1. cURL'ü de tazeleyin."
                )
            except Exception as exc2:  # noqa: BLE001
                return False, [f"cURL ayrıştırılamadı: {type(exc2).__name__}: {exc2}"], {}
        else:
            return False, [f"cURL ayrıştırılamadı: {type(exc).__name__}: {exc}"], {}

    acc2 = Account.from_env(applied, SLOT)
    if not acc2.is_configured:
        problems.append("cURL'den çerez/erişim anahtarı çıkmadı (oturum bilgisi eksik).")
    if not applied.target_domain_2.strip():
        problems.append("Hedef alan adı (target domain) cURL'den okunamadı.")
    own_model_id = _model_id_from_curl(cleaned) or (model_id or "")
    if not own_model_id:
        problems.append(
            "Gövdede 'modelAId' yok. Tamamlanmış bir sohbet isteğinin cURL'ünü kopyalayın."
        )

    # Çerez bütünlüğü: parçalı oturum çerezleri (`.0` + `.1`) tam mı?
    pieces = _cookie_pieces(acc2.cookie)
    chunked = sorted(name for name in pieces if re.search(r"\.\d+$", name))
    if chunked:
        missing = [
            name
            for name in chunked
            if not pieces.get(name, "").strip()
        ]
        if missing:
            problems.append(
                f"Çerez parçası boş: {', '.join(missing)}. Kopyalama sırasında "
                "kesilmiş olabilir; cURL'ü yeniden kopyalayın."
            )
        base_names = {re.sub(r"\.\d+$", "", name) for name in chunked}
        expected = {f"{base}.{idx}" for base in base_names for idx in (0, 1)}
        absent = sorted(expected - set(chunked))
        if absent:
            problems.append(
                f"Çerez parçası eksik: {', '.join(absent)}. Oturum çerezi iki parçaya "
                "bölünür; ikisi de cURL'de olmalı."
            )
    if "cf_clearance" not in pieces and "cf_clearance" not in (acc2.cookie or ""):
        problems.append(
            "Not: 'cf_clearance' çerezi yok. Cloudflare koruması varsa istek 403 alabilir "
            "(aynı tarayıcıdan, koruma geçtikten sonra kopyalayın)."
        )

    sub2 = extract_identity_subject(acc2.cookie, acc2.access_token)
    acc1 = Account.from_env(applied, 1)
    sub1 = extract_identity_subject(acc1.cookie, acc1.access_token)
    if sub1 and sub2 and sub1 == sub2:
        problems.append(
            "Bu cURL 1. hesapla AYNI Google hesabına ait. İki yuva aynı hesabı "
            "kullanamaz (kota bölünmez, oturumlar çakışır). Başka bir hesapla giriş yapın."
        )
    all_max = extract_all_max_identities(applied)
    if sub2 and sum(1 for s in all_max if s == sub2) > 1:
        problems.append("Bu hesap birden fazla yuvada tanımlı görünüyor.")

    values = {
        "curl_env": f"{CURL_KEY}={cleaned}",
        "target_domain": applied.target_domain_2 or "",
        "has_cookie": "evet" if acc2.cookie else "hayır",
        "has_token": "evet" if acc2.access_token else "hayır",
        "cookie_chars": str(len(acc2.cookie)),
        "identity": sub2 or "(okunamadı)",
        "model_id": own_model_id or "(yok)",
        "user_agent": _mask(applied.upstream_user_agent_2),
        "stream_path": applied.upstream_stream_path,
    }

    # Ortak biçim (akış yolu / mode) 1. hesapla aynı mı? Farklıysa uyarı.
    slot1_ready = bool(base.target_domain.strip() and base.upstream_cookie.strip())
    for key in SHARED_KEYS if slot1_ready else ():
        field = key.lower()
        if (
            hasattr(applied, field)
            and hasattr(base, field)
            and str(getattr(applied, field) or "") != str(getattr(base, field) or "")
        ):
            problems.append(
                f"Uyarı: {key} 1. hesabın varsayılanından farklı. İki cURL'ü de aynı "
                "sayfadan, aynı tür istekten alın (aksak istekler 400/404 döner)."
            )

    hard = [p for p in problems if not p.startswith("Uyarı:") and not p.startswith("Not:")]
    return (not hard), problems, values


def write_env(env_path: Path, values: dict[str, str]) -> tuple[Path, list[str]]:
    """`.env` içinde yalnız 2. yuvaya ait satırları değiştirir; yedek alır."""
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    backup = env_path.with_suffix(env_path.suffix + ".bak")
    if env_path.exists():
        backup.write_text(text, encoding="utf-8")

    lines = text.splitlines()
    new_curl_line = values["curl_env"]
    replaced: list[str] = []
    seen = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{CURL_KEY}="):
            if not seen:
                out.append(new_curl_line)
                replaced.append(CURL_KEY)
                seen = True
            # aynı anahtarın ikinci kopyası (çoğu zaman eski deneme) atılır
            continue
        out.append(line)
    if not seen:
        if out and out[-1].strip():
            out.append("")
        out.append("# 2. Max hesabı (2. yuva) — scripts/put_curl2.py ile eklendi")
        out.append(new_curl_line)
        replaced.append(CURL_KEY)

    domain = values.get("target_domain") or ""
    if domain:
        key = "TARGET_DOMAIN_2="
        found = False
        merged: list[str] = []
        for line in out:
            if line.strip().startswith(key):
                merged.append(f"{key}{domain}")
                replaced.append("TARGET_DOMAIN_2")
                found = True
            else:
                merged.append(line)
        if not found:
            merged.append(f"{key}{domain}")
            replaced.append("TARGET_DOMAIN_2")
        out = merged

    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return backup, replaced


def main() -> int:
    parser = argparse.ArgumentParser(
        description="2. Max cURL'ünü doğrular ve .env'de yalnız 2. yuvaya yazar."
    )
    parser.add_argument("curl_file", help="cURL dosyası ('-' = stdin)")
    parser.add_argument("--write", action="store_true", help=".env dosyasına yaz")
    parser.add_argument("--env-file", default=".env", help="Hedef .env yolu")
    parser.add_argument(
        "--show-curl",
        action="store_true",
        help="Tam cURL'ü ekrana yaz (varsayılan: gizli değerler maskelenir)",
    )
    args = parser.parse_args()

    curl_text = _read_curl(args.curl_file)
    env_path = Path(args.env_file)
    try:
        base = Settings(_env_file=env_path if env_path.exists() else None)
    except Exception:  # noqa: BLE001 - bozuk .env kontrolü engellemesin
        base = Settings(_env_file=None, api_keys=["check"])
    ok, problems, values = check(curl_text, env={}, base=base)

    print("=" * 68)
    print("2. MAX cURL KONTROLÜ" + (" — UYGUN" if ok else " — DÜZELTİLMELİ"))
    print("=" * 68)
    for label, value in values.items():
        if label == "curl_env":
            continue
        print(f"  {label:14}: {value}")
    if problems:
        print("\nBulunanlar:")
        for item in problems:
            print(f"  • {item}")
    else:
        print("\nSorun bulunmadı.")

    if not ok:
        print("\nYazılmadı: önce yukarıdaki hataları giderin.")
        return 1
    if not args.write:
        print(
            "\nYazmak için: python scripts/put_curl2.py <dosya> --write\n"
            "Render için: cURL dosyasının içeriğini olduğu gibi kopyalayıp "
            "Environment → UPSTREAM_CURL_2 alanına yapıştırın "
            "(tek satır olması gerekmez)."
        )
        if args.show_curl:
            print("\n" + values["curl_env"])
        else:
            print(
                f"\n(Değer gizlendi: {len(values['curl_env'])} kr. Görmek için --show-curl.)"
            )
        return 0

    backup, replaced = write_env(env_path, values)
    print(f"\n✅ {env_path} güncellendi ({', '.join(replaced)}). Yedek: {backup.name}")
    print("Render'da: Environment → UPSTREAM_CURL_2 alanına aynı değeri yapıştırın.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
