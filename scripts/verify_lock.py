#!/usr/bin/env python
"""Kurulu Python paketleri `requirements.lock` ile birebir aynı mı?

İmaj derlenirken (`Dockerfile`, `Dockerfile.telegram`) ve CI'da çalışır. Amaç:
upstream'e konuşan yığın (Python, OpenSSL, httpx/httpcore/h2 …) bir derlemeden
ötekine **sessizce** değişemesin. Uyuşmazlıkta çıkış kodu 1'dir ve derleme durur;
Render o durumda yeni sürümü yayına almaz, çalışan sürüm sürer.

Kullanım::

    python scripts/verify_lock.py requirements.lock
    python scripts/verify_lock.py requirements.lock --subset
    python scripts/verify_lock.py requirements.lock --allow-extra
    python scripts/verify_lock.py requirements.lock --expect-python 3.12.16 --expect-openssl 3.5.7

* Seçeneksiz (üretim imajı): kurulu her paket kilitte ve aynı sürümde; kilitteki
  her paket kurulu.
* ``--subset`` (Telegram işçi imajı): kilidin yalnız bir kısmı kurulur; kurulu
  olanların hepsi kilitte ve aynı sürümde olmalı.
* ``--allow-extra`` (CI: test araçları da kurulu): kilitte olmayan ek paketlere
  (pytest, ruff …) izin verir; kilitteki her paket yine kurulu ve aynı sürümde
  olmalı.
* ``--expect-python`` / ``--expect-openssl``: yorumlayıcının ve onun kullandığı
  OpenSSL'in sürümü de beklenenle aynı olmalı (taban imaj özetle sabit; bu iki
  değer onun içeriğidir ve TLS el sıkışmasını belirler).

Yalnız standart kütüphaneyi kullanır: imajda başka hiçbir şeye bağımlı olmamalı.
"""

from __future__ import annotations

import argparse
import platform
import re
import ssl
import sys
from importlib import metadata

#: Kilidin konusu olmayan dağıtımlar: taban imajla gelen kurulum araçları ve
#: uygulamanın kendisi.
IGNORED = frozenset({"pip", "setuptools", "wheel", "apiwrapper"})

#: Upstream'e konuşan yığın; sürümleri derleme loguna ayrıca yazılır.
WIRE_STACK = ("httpx", "httpcore", "h2", "hpack", "hyperframe", "h11", "anyio", "certifi", "idna")

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;#]+)\s*(?:#.*)?$")


def canonical(name: str) -> str:
    """PEP 503: adlar büyük/küçük harf ve `-_.` ayrımı gözetmeden karşılaştırılır."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(text: str) -> dict[str, str]:
    """Kilit metnini ``ad → sürüm`` sözlüğüne çevirir.

    Yorum ve boş satırlar atlanır. Geri kalan her satır ``ad==sürüm`` olmalıdır;
    aralık, işaretçi ya da yinelenen ad kilidin anlamını bozar ve hata sayılır.
    """
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        if match is None:
            raise ValueError(f"satır {number}: 'ad==sürüm' biçiminde değil: {raw!r}")
        name = canonical(match.group(1))
        if name in pins:
            raise ValueError(f"satır {number}: {name} iki kez sabitlenmiş")
        pins[name] = match.group(2)
    if not pins:
        raise ValueError("kilit boş")
    return pins


def installed_versions() -> dict[str, str]:
    """Bu yorumlayıcıda kurulu dağıtımlar (``ad → sürüm``), yok sayılanlar hariç.

    Aynı ad iki yerde kuruluysa `sys.path`'te önce gelen — yani gerçekten içe
    aktarılan — sayılır.
    """
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        key = canonical(name)
        if key not in IGNORED:
            found.setdefault(key, dist.version)
    return found


def openssl_version() -> str:
    """Yorumlayıcının kullandığı OpenSSL'in sürümü (``3.5.7`` gibi)."""
    match = re.search(r"(\d+\.\d+\.\d+)", ssl.OPENSSL_VERSION)
    return match.group(1) if match else ssl.OPENSSL_VERSION


def compare(
    pins: dict[str, str],
    installed: dict[str, str],
    *,
    subset: bool = False,
    allow_extra: bool = False,
) -> list[str]:
    """Paket uyuşmazlıklarını okunur satırlar olarak döndürür (boş liste = uyumlu)."""
    problems: list[str] = []
    for name in sorted(installed):
        version = installed[name]
        if name not in pins:
            if not allow_extra:
                problems.append(f"KİLİTTE YOK   {name}=={version} kurulu ama sabitlenmemiş")
        elif pins[name] != version:
            problems.append(f"SÜRÜM FARKLI  {name}: kurulu {version}, kilit {pins[name]}")
    if not subset:
        for name in sorted(set(pins) - set(installed)):
            problems.append(f"KURULU DEĞİL  {name}=={pins[name]} kilitte var ama kurulmamış")
    return problems


def compare_runtime(
    *,
    python: str,
    openssl: str,
    expect_python: str | None = None,
    expect_openssl: str | None = None,
) -> list[str]:
    """Yorumlayıcı / OpenSSL uyuşmazlıklarını döndürür (beklenti verilmediyse boş)."""
    problems: list[str] = []
    if expect_python is not None and python != expect_python:
        problems.append(f"PYTHON FARKLI   çalışan {python}, beklenen {expect_python}")
    if expect_openssl is not None and openssl != expect_openssl:
        problems.append(f"OPENSSL FARKLI  çalışan {openssl}, beklenen {expect_openssl}")
    return problems


def summary_line(installed: dict[str, str], *, python: str, openssl: str) -> str:
    """Tek satırlık kayıt: derleme logunda ve CI notunda aynı biçimde görünür."""
    parts = [f"python={python}", f"openssl={openssl}"]
    parts += [f"{name}={installed[name]}" for name in WIRE_STACK if name in installed]
    return "GÖNDERME YIĞINI " + " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Kurulu paketleri requirements.lock ile karşılaştırır."
    )
    parser.add_argument("lock", help="kilit dosyasının yolu (requirements.lock)")
    parser.add_argument(
        "--subset", action="store_true", help="kilitteki her paketin kurulu olması gerekmez"
    )
    parser.add_argument(
        "--allow-extra", action="store_true", help="kilitte olmayan ek paketlere izin ver"
    )
    parser.add_argument("--expect-python", metavar="X.Y.Z", help="beklenen Python sürümü")
    parser.add_argument("--expect-openssl", metavar="X.Y.Z", help="beklenen OpenSSL sürümü")
    args = parser.parse_args(argv)

    with open(args.lock, encoding="utf-8") as handle:
        pins = parse_lock(handle.read())
    installed = installed_versions()
    python, openssl = platform.python_version(), openssl_version()

    problems = compare_runtime(
        python=python,
        openssl=openssl,
        expect_python=args.expect_python,
        expect_openssl=args.expect_openssl,
    )
    problems += compare(pins, installed, subset=args.subset, allow_extra=args.allow_extra)

    # Derleme logunda kayıt: imajın içinde tam olarak ne var.
    print(summary_line(installed, python=python, openssl=openssl))
    print(f"                ({ssl.OPENSSL_VERSION})")
    if problems:
        print(f"\nKİLİT UYUŞMUYOR ({len(problems)} sorun) — {args.lock}:")
        for line in problems:
            print("  " + line)
        print(
            "\nİmajın içi beklenenle aynı değil. Bu, upstream'e giden isteği kod "
            "değişmeden değiştirebilir; derleme bilerek durduruldu. Ne yapılacağı: "
            "docs/LESSONS_2026_11_14.md §20.6."
        )
        return 1
    print(f"kilit uyumlu: {len(installed)} kurulu paket, {len(pins)} sabitlenmiş sürüm")
    return 0


if __name__ == "__main__":
    sys.exit(main())
