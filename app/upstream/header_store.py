"""Ek HTTP başlıklarının geriye uyumlu ve kayıpsız metin gösterimi.

Eski ``K1=V1;K2=V2`` biçimi aynen desteklenir. ``sec-ch-ua`` gibi değerinde
noktalı virgül bulunan gerçek tarayıcı başlıklarında ise JSON nesnesi kullanılır;
aksi halde tek bir başlık birkaç sahte başlığa bölünür.
"""

from __future__ import annotations

import json


def parse_extra_headers(raw: str) -> dict[str, str]:
    """JSON veya eski noktalı virgüllü biçimi başlık sözlüğüne çevirir.

    Adlar **küçük harfe** indirgenir. HTTP başlık adları bayır büyük/küçük harf
    duyarlı değildir, ama üretilen sözlük `dict` olarak birleştiriliyor:
    `sec-ch-ua` (kodun hesapladığı) yanına `Sec-CH-UA` (kullanıcının yazdığı)
    eklenirse httpx **aynı başlıktan iki farklı değerle** iki satır gönderir
    (`X-Deployment-Id=dpl_ESKİ` taze damganın yerini alamaz). Tarayıcılar böyle
    bir istek asla üretmez; bot koruması bunu parmak izi çelişkisi olarak görür.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {
            str(key).strip().lower(): str(value).strip()
            for key, value in parsed.items()
            if str(key).strip() and value is not None
        }

    headers: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key:
            headers[key] = value
    return headers


def format_extra_headers(headers: dict[str, str]) -> str:
    """Başlıkları kayıpsız saklar; yalnız gerektiğinde JSON'a geçer."""
    clean = {
        str(key).strip(): str(value).strip()
        for key, value in headers.items()
        if str(key).strip()
    }
    if not clean:
        return ""
    if any(";" in key or ";" in value for key, value in clean.items()):
        return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
    return ";".join(f"{key}={value}" for key, value in clean.items())
