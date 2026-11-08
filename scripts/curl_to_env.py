#!/usr/bin/env python3
"""Tarayıcıdan kopyalanan cURL komutunu `.env` ve `models.yaml` ayarlarına çevirir.

Kullanım:
    python scripts/curl_to_env.py curl.txt
    python scripts/curl_to_env.py curl.txt --write
    type curl.txt | python scripts/curl_to_env.py -

Elle kopyala-yapıştır sırasında en sık yapılan hatalar (çerezin ortadan kesilmesi,
değerin alt satıra taşması, tırnakların yarım kalması) bu araçla tamamen ortadan
kalkar: cURL ne veriyorsa birebir o yazılır.

Çıkarılanlar:
  * ``TARGET_DOMAIN``        — URL'nin alan adı (şemasız)
  * ``UPSTREAM_STREAM_PATH`` — gerçek akış yolu (``{chat_id}`` varsa parametreleştirilir)
  * ``UPSTREAM_MODE``        — gövdedeki ``mode`` (örn. ``direct``)
  * ``UPSTREAM_CX``           — gövdedeki ``cx`` (oturuma bağlı; yoksa temizlenir)
  * ``UPSTREAM_COOKIE``      — ``cookie`` başlığının tamamı
  * ``UPSTREAM_ACCEPT_LANGUAGE``
  * ``UPSTREAM_USER_AGENT``
  * ``UPSTREAM_EXTRA_HEADERS`` — dnt/deployment başlıkları
  * ``UPSTREAM_RECAPTCHA_FIELD`` — gövdedeki captcha alanının gerçek adı
  * ``RECAPTCHA_STATIC_TOKEN``   — gövdedeki captcha değeri
  * ``models.yaml`` için ``upstream_id`` (gövdedeki ``modelAId``)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import MAX_ACCOUNT_SLOTS  # noqa: E402
from app.upstream.curl_import import (  # noqa: E402
    CAPTCHA_FIELDS,
    DEFAULT_STREAM_PATH,
    PASSTHROUGH_HEADERS,
    build_settings,
    extract_authorization,
    extract_captcha,
    extract_referer_path,
    load_body,
    parse_curl,
)
from app.upstream.session_capture import inspect_cookie  # noqa: E402

__all__ = [
    "CAPTCHA_FIELDS",
    "DEFAULT_STREAM_PATH",
    "PASSTHROUGH_HEADERS",
    "build_settings",
    "extract_authorization",
    "extract_captcha",
    "extract_referer_path",
    "load_body",
    "parse_curl",
]

#: `.env` içine yazılacak anahtarların sırası.
ENV_ORDER: tuple[str, ...] = (
    "TARGET_DOMAIN",
    "UPSTREAM_STREAM_PATH",
    "UPSTREAM_MODE",
    "UPSTREAM_CX",
    "UPSTREAM_CX_SEND",
    "UPSTREAM_RECAPTCHA_FIELD",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_EXTRA_HEADERS",
    "UPSTREAM_REFERER_PATH",
    "UPSTREAM_COOKIE",
    "UPSTREAM_AUTH_FROM_COOKIE",
    "UPSTREAM_ACCESS_TOKEN",
    "UPSTREAM_AUTH_SCHEME",
    "RECAPTCHA_PROVIDER",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Hesap başına tutulan anahtarlar. 1. hesap soneksiz yazılır (mevcut tek hesaplı
#: kurulum aynen çalışmaya devam eder); 2. hesap `_2`, 3. hesap `_3` soneksi alır.
#: Böylece cURL'ler sırayla atıldığında her biri kendi boşluğuna yazılır ve
#: hiçbir hesap diğerini ezmez.
ACCOUNT_ENV_KEYS: tuple[str, ...] = (
    "TARGET_DOMAIN",
    "UPSTREAM_REFERER_PATH",
    "UPSTREAM_COOKIE",
    "UPSTREAM_AUTH_FROM_COOKIE",
    "UPSTREAM_ACCESS_TOKEN",
    "UPSTREAM_AUTH_SCHEME",
    "UPSTREAM_USER_AGENT",
    "UPSTREAM_ACCEPT_LANGUAGE",
    "RECAPTCHA_STATIC_TOKEN",
)

#: Hesap yuvasının adını taşıyan anahtar (`UPSTREAM_ACCOUNT_NAME`, `..._2_NAME`, …).
ACCOUNT_NAME_PREFIX = "UPSTREAM_ACCOUNT"

#: `--reset-accounts` ile temizlenecek anahtarlar.
ACCOUNT_SCAN_KEYS: tuple[str, ...] = ACCOUNT_ENV_KEYS + ("UPSTREAM_RECAPTCHA_FIELD",)

#: `merge_env` yeni anahtarları bu yorum satırının altına ekler.
ENV_MARKER_COMMENT = "# scripts/curl_to_env.py tarafından eklendi"

#: Uygulamanın gerçekten okuyabildiği yuva sayısıyla aynı sınır.
MAX_SCAN_SLOTS = MAX_ACCOUNT_SLOTS

def _mask(value: str, keep: int = 6) -> str:
    """Uzun gizli değerleri günlüğe basmadan önce kısaltır."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]} ({len(value)} karakter)"


def account_name_key(slot: int) -> str:
    """Hesap yuvasının ad anahtarı: 1. hesap soneksiz, 2. hesap `_2`."""
    suffix = "" if slot <= 1 else f"_{slot}"
    return f"{ACCOUNT_NAME_PREFIX}{suffix}_NAME"


def account_key(name: str, slot: int) -> str:
    """Hesaba özgü `.env` anahtarı."""
    return name if slot <= 1 else f"{name}_{slot}"


def split_account_env(
    env: dict[str, str], slot: int, label: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Çıkarılan ayarları (paylaşılan, hesaba özgü) olarak ikiye ayırır."""
    shared: dict[str, str] = {}
    account: dict[str, str] = {account_name_key(slot): label}
    for key, value in env.items():
        # CAPTCHA sağlayıcısı globaldir. İkinci/üçüncü hesabı eklemek, önceden
        # seçilmiş browser/external/noop kipini sessizce static'e çevirmemeli.
        if key == "RECAPTCHA_PROVIDER" and slot > 1:
            continue
        if key in ACCOUNT_ENV_KEYS:
            account[account_key(key, slot)] = value
        else:
            shared[key] = value
    return shared, account


def read_env_keys(path: Path) -> dict[str, str]:
    """`.env` içindeki `ANAHTAR=değer` satırlarını okur (yorumlar atlanır)."""
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        found.setdefault(key.strip(), value.strip())
    return found


def next_free_slot(path: Path) -> int:
    """Boş olan ilk hesap yuvasını bulur (1'den başlar).

    Adı yazılmış ama kimliği olmayan yuvalar **boş sayılır**: cookie başlığı
    olmayan bir cURL'den arta kalmışlardır ve yeniden kaydetme o yuvayı
    onarmalı, yeni bir yuva açmamalı.
    """
    existing = read_env_keys(path)
    for slot in range(1, MAX_SCAN_SLOTS + 1):
        if not slot_has_credentials(existing, slot):
            return slot
    return MAX_SCAN_SLOTS + 1


def _is_account_key(key: str) -> bool:
    """Anahtar bir hesap yuvasına mı ait? (`X`, `X_2`, `X_3`, `..._NAME`)"""
    if key in ACCOUNT_SCAN_KEYS:
        return True
    if key.startswith(ACCOUNT_NAME_PREFIX) and key.endswith("_NAME"):
        return True
    base, sep, suffix = key.rpartition("_")
    return bool(sep) and base in ACCOUNT_SCAN_KEYS and suffix.isdigit()


def strip_account_keys(path: Path) -> int:
    """`.env`'den hesap anahtarlarını (tüm yuvalar) söker; kalan satır sayısını döndürür."""
    if not path.exists():
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == ENV_MARKER_COMMENT:
            continue  # tekrar birikmesin
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if _is_account_key(key):
                continue
        kept.append(line)
    # Sonda biriken boş satırları at.
    while kept and not kept[-1].strip():
        kept.pop()
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return len(kept)


def slot_has_credentials(values: dict[str, str], slot: int) -> bool:
    """Yuva, isteği kimlikle gönderebilecek bilgiye sahip mi?

    `app/services/account.py::Account.is_configured` ile aynı kural: cookie
    veya access_token boş değilse dolu. **Yalnızca adı yazılmış yuvalar hesap
    değildir** — cookie başlığı olmayan bir cURL kaydedildiğinde oluşurlar ve
    uygulama onları zaten atlar.
    """
    return bool(
        values.get(account_key("UPSTREAM_COOKIE", slot), "").strip()
        or values.get(account_key("UPSTREAM_ACCESS_TOKEN", slot), "").strip()
    )


def count_accounts(values: dict[str, str]) -> int:
    """`.env` içeriğinde kaç *kullanılabilir* hesap var?

    Tüm yuvalar taranır; aradaki boş yuvalar atlanır. Uygulama
    (`load_accounts`) da böyle saydığı için iki sayı birbirini tutar.
    """
    return sum(
        1
        for slot in range(1, MAX_SCAN_SLOTS + 1)
        if slot_has_credentials(values, slot)
    )


def merge_env(path: Path, updates: dict[str, str]) -> str:
    """Var olan `.env` içeriğini koruyarak anahtarları günceller.

    Aynı anahtardan birden çok satır varsa ilki güncellenir, kalan kopyalar
    silinir; böylece mükerrer tanımların sessizce birbirini ezmesi önlenir.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            result.append(f"{key}={remaining.pop(key)}")
            seen.add(key)
        elif key in seen:
            continue  # mükerrer satırı at
        else:
            result.append(line)
            seen.add(key)  # güncellenmeyen anahtarların da yalnız ilk satırı kalsın

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append(ENV_MARKER_COMMENT)
        for key in ENV_ORDER:
            if key in remaining:
                result.append(f"{key}={remaining.pop(key)}")
        for key, value in remaining.items():
            result.append(f"{key}={value}")

    return "\n".join(result) + "\n"


def update_models_yaml(path: Path, upstream_id: str) -> bool:
    """`models.yaml` içindeki ilk `upstream_id` değerini günceller."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"(^\s*upstream_id:\s*).*$",
        lambda m: f"{m.group(1)}{upstream_id}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count and new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "cURL komutunu .env ayarlarına çevirir. Her çalıştırma bir hesap "
            "yuvasına yazar: 1. cURL soneksiz anahtarlara, 2. cURL '_2' "
            "soneksli anahtarlara. Böylece cURL'ler sırayla atıldığında hiçbiri "
            "diğerini ezmez."
        ),
    )
    parser.add_argument("curl_file", help="cURL komutunu içeren dosya ('-' = stdin)")
    parser.add_argument(
        "--write", action="store_true", help=".env ve models.yaml dosyalarını güncelle"
    )
    parser.add_argument("--env-file", default=".env", help="Hedef .env yolu")
    parser.add_argument(
        "--models-file", default="config/models.yaml", help="Hedef models.yaml yolu"
    )
    parser.add_argument(
        "--show-secrets", action="store_true", help="Değerleri maskelemeden yazdır"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Kritik uyarılara rağmen yazmayı sürdür",
    )
    account_mode = parser.add_mutually_exclusive_group()
    account_mode.add_argument(
        "--account",
        help=(
            "Hesap yuvası: '1'/'2' gibi bir yuva numarası ya da 'hesap-1' gibi "
            "özel bir ad. Verilmezse ilk boş yuva seçilir."
        ),
    )
    account_mode.add_argument(
        "--single-account",
        action="store_true",
        help=(
            "Tek hesaplı kurulum: eski 1/2/3/... yuva kalıntılarını temizle ve "
            "yeni cURL'ü yalnızca 1. hesaba yaz."
        ),
    )
    parser.add_argument(
        "--list-accounts", action="store_true", help="Kayıtlı hesapları göster ve çık"
    )
    parser.add_argument(
        "--reset-accounts",
        action="store_true",
        help=".env içindeki tüm hesap yuvalarını temizle",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)

    if args.reset_accounts:
        strip_account_keys(env_path)
        print(f"{env_path} içindeki hesap yuvaları temizlendi.")
        return 0

    if args.list_accounts:
        values = read_env_keys(env_path)
        used = [
            slot
            for slot in range(1, MAX_SCAN_SLOTS + 1)
            if slot_has_credentials(values, slot)
            or values.get(account_name_key(slot), "").strip()
        ]
        if not used:
            print(f"{env_path} içinde kayıtlı hesap yok.")
            return 0
        total = count_accounts(values)
        print(f"{env_path} içinde {total} kullanılabilir hesap var:")
        for slot in used:
            cookie = values.get(account_key("UPSTREAM_COOKIE", slot), "")
            token = values.get(account_key("RECAPTCHA_STATIC_TOKEN", slot), "")
            name = values.get(account_name_key(slot), "?") or "?"
            flag = "" if slot_has_credentials(values, slot) else "   ← KULLANILAMIYOR"
            print(
                f"  {slot}. {name} "
                f"(cookie {len(cookie)} kr, token {len(token)} kr){flag}"
            )
        broken = [s for s in used if not slot_has_credentials(values, s)]
        if broken:
            slots = ", ".join(str(s) for s in broken)
            print(
                f"\n  {slots}. yuva(lar)ın adı var ama çerezi/token'ı yok: cookie\n"
                "  başlığı olmayan bir cURL kaydedilmiş. Uygulama bu yuvaları zaten\n"
                "  atlar, yani hesap sayısını şişirmezler. Onarmak için o hesabın\n"
                "  cURL'ünü siteye giriş yapmışken alıp yeniden kaydedin; aynı yuva\n"
                "  kullanılır, yeni yuva açılmaz."
            )
        return 0

    if args.curl_file == "-":
        raw = sys.stdin.read()
    else:
        curl_path = Path(args.curl_file)
        if not curl_path.is_file():
            print(
                f"HATA: '{args.curl_file}' bulunamadı (aranan dizin: {Path.cwd()}).\n"
                "  İki yol var:\n"
                "  1) cURL'ü panoya kopyalayıp doğrudan aktarın (dosya gerekmez):\n"
                "       Get-Clipboard | python scripts/curl_to_env.py --write -\n"
                "  2) cURL'ü bir dosyaya kaydedin (Notepad → curl1.txt, UTF-8) ve\n"
                "     dosya adını tam yol verin.",
                file=sys.stderr,
            )
            return 2
        # utf-8-sig: Windows PowerShell'in yazdığı BOM'u da tolere eder.
        raw = curl_path.read_text(encoding="utf-8-sig", errors="replace")
    if not raw.strip():
        print("HATA: cURL girdisi boş.", file=sys.stderr)
        return 2

    parsed = parse_curl(raw)
    existing = read_env_keys(env_path)
    env, warnings, model_id = build_settings(parsed, existing)

    if not env:
        print("HATA: cURL'den hiçbir ayar çıkarılamadı.", file=sys.stderr)
        return 2

    if not env.get("UPSTREAM_COOKIE") and not env.get("UPSTREAM_ACCESS_TOKEN"):
        found_headers = ", ".join(sorted(parsed.get("headers", {}))) or "(hiç yok)"
        url = parsed.get("url", "")
        print(
            "HATA: cURL'de ne 'cookie' ne de 'authorization' başlığı bulundu.\n"
            "  Böyle bir cURL kaydedilirse yuva adıyla birlikte yazılır ama kimliği\n"
            "  olmaz; uygulama o yuvayı kullanamaz.\n"
            "\n"
            "  Aracın gördüğü girdi:\n"
            f"    uzunluk     : {len(raw)} karakter\n"
            f"    url         : {url or '(çıkarılamadı)'}\n"
            f"    başlıklar   : {found_headers}\n"
            f"    gövde       : {len(parsed.get('body', ''))} karakter\n"
            f"    ilk 300 kr  : {raw.strip()[:300]!r}\n"
            "\n"
            "  Olası sebepler:\n"
            "   * İstek 'Provisional headers are shown' durumundayken kopyalandı\n"
            "     (yanıt bitmeden kopyalamayın; adblock'u kapatın, Disable cache açın).\n"
            "   * PowerShell panoyu bozdu: 'Get-Clipboard -Raw' deneyin, ya da cURL'ü\n"
            "     Notepad ile bir dosyaya kaydedip dosya adını verin.\n"
            "   * Yanlış istek kopyalandı: akış isteği POST olmalı ve gövdesinde\n"
            "     'modelAId' bulunmalı.",
            file=sys.stderr,
        )
        return 2

    try:
        if args.single_account:
            slot, label = 1, "hesap-1"
        else:
            slot, label = _resolve_slot(args.account, env_path)
    except ValueError as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 2

    print(f"=== Çıkarılan ayarlar ({slot}. hesap yuvası: {label}) ===")
    secret_keys = {
        "UPSTREAM_COOKIE",
        "RECAPTCHA_STATIC_TOKEN",
        "UPSTREAM_ACCESS_TOKEN",
    }
    for key in ENV_ORDER:
        if key not in env:
            continue
        value = env[key]
        target = account_key(key, slot) if key in ACCOUNT_ENV_KEYS else key
        shown = value if args.show_secrets or key not in secret_keys else _mask(value)
        print(f"  {target}={shown}")
    if model_id:
        if slot == 1:
            print(f"  (models.yaml) upstream_id={model_id}")
        else:
            print(
                f"  (models.yaml dokunulmadı; model kimlikleri hesap başına değil. "
                f"Bu cURL'deki modelAId={model_id})"
            )

    if env.get("UPSTREAM_COOKIE"):
        report = inspect_cookie(env["UPSTREAM_COOKIE"])
        print("\n=== Sürdürülebilirlik & Otomatik Yenileme ===")
        families = report.get("chunked_families")
        if isinstance(families, dict) and families:
            shown = ", ".join(f"{k} ({v} parça)" for k, v in families.items())
            print(f"  Parçalı oturum çerezi : {shown}")
        if report.get("access_token_found"):
            rem = report.get("access_token_expires_in_seconds")
            if isinstance(rem, int) and rem > 0:
                print(f"  Erişim token'ı        : bulundu (≈{rem // 60} dk geçerli)")
            elif report.get("access_token_expired"):
                print("  Erişim token'ı        : bulundu ama SÜRESİ DOLMUŞ")
            else:
                print("  Erişim token'ı        : bulundu")
        if report.get("refresh_token_found"):
            print(
                "  Refresh token         : ✓ VAR (wrapper her saat kendi kendine "
                "yenileyecek)"
            )
        else:
            print(
                "  Refresh token         : ⚠ YOK (oturum ~1 saat sonra sona erebilir)"
            )
        for problem in report.get("problems") or []:
            print(f"  ! {problem}")

    if warnings:
        print("\n=== Uyarılar ===")
        for item in warnings:
            print(f"  ! {item}")

    if not args.write:
        if args.single_account:
            print("\nYazmak için --write ekleyin; eski yuvalar yalnız yazma sırasında temizlenir.")
        else:
            print(f"\nYazmak için --write ekleyin (seçilen yuva: {slot}. hesap).")
        return 0

    critical = [w for w in warnings if w.startswith("KRİTİK")]
    if critical and not args.force:
        print(
            "\nYazma iptal edildi: yanlış isteğin cURL'ü kopyalanmış görünüyor.\n"
            "Doğru cURL'ü alıp tekrar deneyin ya da yine de yazmak için --force ekleyin.",
            file=sys.stderr,
        )
        return 3

    shared, account = split_account_env(env, slot, label)
    if args.single_account:
        # Önce bütün eski yuva anahtarlarını sök. `_5` gibi uygulamanın hiç
        # okumadığı eski kalıntılar da sayısal sonek kuralıyla temizlenir.
        strip_account_keys(env_path)
    env_path.write_text(merge_env(env_path, {**shared, **account}), encoding="utf-8")

    total = count_accounts(read_env_keys(env_path))
    if args.single_account:
        print(f"\n{env_path} güncellendi — tek hesaplı kurulum, eski yuvalar temizlendi.")
    else:
        print(f"\n{env_path} güncellendi — {label} {slot}. yuvaya yazıldı.")
    print(f"Kayıtlı hesap sayısı: {total}")
    if total > 1:
        print(
            "Hesap havuzu kullanılabilir yuvaların tümünü okur; "
            "değişikliklerin yüklenmesi için çalışan servisi yeniden başlatın."
        )

    if model_id and slot == 1:
        models_path = Path(args.models_file)
        if update_models_yaml(models_path, model_id):
            print(f"{models_path} güncellendi (upstream_id={model_id}).")
        else:
            print(
                f"UYARI: {models_path} güncellenemedi; upstream_id={model_id} "
                "değerini elle yazın."
            )
    return 0


def _resolve_slot(requested: str | None, env_path: Path) -> tuple[int, str]:
    """`--account` değerini (yuva numarası, hesap adı) çiftine çevirir."""
    existing = read_env_keys(env_path)

    def label_for(slot: int) -> str:
        # Numarayla tazeleme veya yarım yuvayı onarma özel adı silmemeli.
        return existing.get(account_name_key(slot), "").strip() or f"hesap-{slot}"

    if not requested:
        slot = next_free_slot(env_path)
        if slot > MAX_ACCOUNT_SLOTS:
            raise ValueError(
                f"Hesap yuvaları dolu; en fazla {MAX_ACCOUNT_SLOTS} hesap destekleniyor. "
                "Tazelemek için --account 1 (veya 2/3/4) kullanın."
            )
        return slot, label_for(slot)

    text = requested.strip()
    if text.isdigit():
        slot = int(text)
        if not 1 <= slot <= MAX_ACCOUNT_SLOTS:
            raise ValueError(
                f"Geçersiz hesap yuvası {slot}; 1-{MAX_ACCOUNT_SLOTS} arasında olmalı."
            )
        return slot, label_for(slot)

    free: int | None = None
    for slot in range(1, MAX_SCAN_SLOTS + 1):
        if existing.get(account_name_key(slot), "") == text:
            return slot, text  # aynı ad -> aynı yuvayı tazele
        if free is None and not slot_has_credentials(existing, slot):
            free = slot
    if free is None:
        raise ValueError(
            f"Hesap yuvaları dolu; '{text}' için en fazla {MAX_ACCOUNT_SLOTS} hesap "
            "destekleniyor. Var olan hesabı tazelemek için --account NUMARA kullanın."
        )
    return free, text


if __name__ == "__main__":
    raise SystemExit(main())
