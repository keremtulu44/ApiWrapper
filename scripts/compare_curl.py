"""Tarayıcıdan alınan gerçek cURL ile ApiWrapper'ın ürettiği isteği karşılaştırır.

Kullanım:
    # cURL'ü bir dosyaya kaydedin (Windows ^ kaçışları veya bash \\ kaçışları olabilir)
    python scripts/compare_curl.py request.txt

Çıktı: eksik/fazla/farklı başlıklar ve gövde alanları — böylece upstream'e
gönderdiğimiz isteğin tarayıcınınkiyle örtüşüp örtüşmediğini görürsünüz.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.upstream.curl_import import (  # noqa: E402
    load_body,
    normalize_curl,
    parse_curl,
)
from app.upstream.headers import build_stream_headers  # noqa: E402

__all__ = ["load_body", "normalize_curl", "parse_curl"]

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)

#: Karşılaştırmada yok sayılacak başlıklar (bağlantıya/istemciye özgü).
IGNORED_HEADERS = {"content-length", "host", "connection", "accept-encoding"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    reference = parse_curl(Path(sys.argv[1]).read_text(encoding="utf-8"))
    ref_headers = reference["headers"]
    ref_body = load_body(reference["body"])

    settings = Settings()
    chat_id = str(ref_body.get("id") or "CHAT_ID")
    ours = build_stream_headers(settings, chat_id)
    ours = {k.lower(): v for k, v in ours.items()}
    problems = 0

    print(f"\n=== URL {DIM}(yol — 404'ün en sık nedeni){RESET} ===\n")
    ref_url = reference["url"]
    our_url = settings.stream_url(chat_id)
    ref_path = urlsplit(ref_url).path if ref_url else ""
    our_path = urlsplit(our_url).path
    ref_path_normalized = ref_path.replace(chat_id, "{chat_id}") if chat_id else ref_path
    our_path_normalized = settings.upstream_stream_path.split("?", 1)[0]
    if ref_path and ref_path.rstrip("/") == our_path.rstrip("/"):
        print(f"{GREEN}  EŞLEŞTİ {RESET}{our_path}")
    elif ref_path_normalized.rstrip("/") == our_path_normalized.rstrip("/"):
        print(f"{GREEN}  EŞLEŞTİ {RESET}{our_path_normalized}")
    else:
        print(f"{RED}  FARKLI  {RESET}akış yolu")
        print(f"{DIM}      tarayıcı: {ref_path or '(yok)'}{RESET}")
        print(f"{DIM}      bizim   : {our_path}{RESET}")
        print(
            f"{RED}      !! UPSTREAM_STREAM_PATH={settings.upstream_stream_path} "
            "tarayıcıdaki yolla uyuşmuyor; bu HTTP 404 üretir.{RESET}"
        )
        print("         python scripts/curl_to_env.py <curl.txt> --write")
        problems += 1

    print(f"\n=== Başlık karşılaştırması {DIM}(referans = tarayıcı cURL'ü){RESET} ===\n")

    names = sorted((set(ref_headers) | set(ours)) - IGNORED_HEADERS)
    for name in names:
        theirs = ref_headers.get(name)
        mine = ours.get(name)
        if theirs is None:
            print(f"{YELLOW}  FAZLA   {RESET}{name}: {DIM}{str(mine)[:70]}{RESET}")
            problems += 1
        elif mine is None:
            print(f"{RED}  EKSİK   {RESET}{name}: {DIM}{str(theirs)[:70]}{RESET}")
            problems += 1
        elif theirs.strip() == str(mine).strip():
            print(f"{GREEN}  EŞLEŞTİ {RESET}{name}")
        else:
            print(f"{YELLOW}  FARKLI  {RESET}{name}")
            print(f"{DIM}      tarayıcı: {theirs[:90]}{RESET}")
            print(f"{DIM}      bizim   : {str(mine)[:90]}{RESET}")
            problems += 1

    if ref_body:
        print(f"\n=== Gövde alanları {DIM}(--data-raw){RESET} ===\n")
        expected_fields = set(ref_body)
        our_fields = {
            "id",
            "modelAId",
            "userMessageId",
            "modelAMessageId",
            "userMessage",
            "modality",
            settings.upstream_recaptcha_field,
        }
        if settings.upstream_mode.strip():
            our_fields.add("mode")
        for field in sorted(expected_fields | our_fields):
            in_ref, in_ours = field in expected_fields, field in our_fields
            if in_ref and in_ours:
                print(f"{GREEN}  EŞLEŞTİ {RESET}{field}")
            elif in_ref:
                print(f"{RED}  EKSİK   {RESET}{field}  <-- gövdemizde yok!")
                problems += 1
            else:
                print(f"{YELLOW}  FAZLA   {RESET}{field}  <-- tarayıcı göndermiyor")
                problems += 1

        captcha_fields = [f for f in expected_fields if "recaptcha" in f.lower()]
        if captcha_fields and captcha_fields[0] != settings.upstream_recaptcha_field:
            print(
                f"\n{RED}  !! UPSTREAM_RECAPTCHA_FIELD={settings.upstream_recaptcha_field} "
                f"ancak tarayıcı '{captcha_fields[0]}' gönderiyor.{RESET}"
            )
            print(f"     .env içine yazın: UPSTREAM_RECAPTCHA_FIELD={captcha_fields[0]}")

    print(
        f"\n=== {GREEN if problems == 0 else YELLOW}{problems} fark bulundu{RESET} ===\n"
        f"{DIM}Not: 'FAZLA' işaretli sec-* başlıkları genelde zararsızdır; "
        f"'EKSİK' olanlar ve gövde farkları önemlidir.{RESET}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
