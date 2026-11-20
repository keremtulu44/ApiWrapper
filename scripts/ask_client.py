#!/usr/bin/env python3
"""`POST /v1/ask` için başvuru istemcisi — yalnız standart kitaplık.

İki işe yarar:

* **Karşı sistem için örnek:** `ask()` işlevi sözleşmenin tamamını uygular
  (docs/ASK_API.md). Kopyalanıp olduğu gibi kullanılabilir; ek paket gerekmez.
* **Elle deneme aracı:** deploy sonrasında ucun çalıştığını tek komutla görmek için.

Kullanım:

    python scripts/ask_client.py https://<servis>.onrender.com sk-... "Soru metni"

    # ya da ortam değişkenleriyle:
    ASK_URL=https://<servis>.onrender.com ASK_KEY=sk-... python scripts/ask_client.py "Soru"

Çıkış kodu: 0 = cevap alındı (stdout'a yazılır) · 1 = problem (yedeğe geçilmeli).
"wait" işaretleri ve sonuç özeti stderr'e yazılır; stdout'ta yalnız cevap bulunur.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from typing import Any

#: Bu kadar süre hiçbir şey gelmezse bağlantı ölü sayılır. Sunucu en geç 30 sn'de
#: ilk "wait" satırını, sonra 15 sn'de bir yenisini gönderir; 45 sn güvenli paydır.
SILENCE_SECONDS = 45.0


def ask(
    base_url: str,
    api_key: str,
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    silence: float = SILENCE_SECONDS,
    on_wait: Callable[[dict[str, Any]], None] | None = None,
) -> str | None:
    """Soruyu gönderir. Cevap metnini döndürür; cevap alınamadıysa ``None``.

    ``None`` her zaman aynı anlama gelir: **yedek sisteme geç.** Neden ayırt
    edilmez (kota, oturum, zaman aşımı, ağ, sunucu yeniden başlıyor...) — ayrıntı
    servis sahibine ayrıca bildirilir.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if system:
        body["system"] = system
    if model:
        body["model"] = model
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/ask",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        # `timeout` her soket işlemine (bağlanma ve her okuma) ayrı ayrı uygulanır:
        # tam olarak "bu kadar süre sessizlik olursa vazgeç" kuralı.
        with urllib.request.urlopen(request, timeout=silence) as response:
            if response.status != 200:
                return None
            for raw in response:  # satır satır okur
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue  # boş satır ya da ": ok" gibi yorum satırı
                try:
                    event = json.loads(line[5:])
                except ValueError:
                    return None
                status = event.get("status") if isinstance(event, dict) else None
                if status == "wait":
                    if on_wait is not None:
                        on_wait(event)
                    continue
                if status == "done":
                    return event.get("text") or None
                return None  # "problem" ya da tanınmayan durum
    except (OSError, http.client.HTTPException, ValueError):
        # HTTP 4xx/5xx, bağlantı hatası, zaman aşımı, yarıda kesilen cevap.
        return None
    return None  # bağlantı son satır ("done"/"problem") gelmeden kapandı


def main(argv: list[str]) -> int:
    # Windows konsolunun varsayılan kodlaması Türkçe karakterde/emojide hata verebilir.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = argv[1:]
    base_url = os.environ.get("ASK_URL", "")
    api_key = os.environ.get("ASK_KEY", "")
    if len(args) >= 3:
        base_url, api_key, prompt = args[0], args[1], " ".join(args[2:])
    elif len(args) >= 1 and base_url and api_key:
        prompt = " ".join(args)
    else:
        print(__doc__, file=sys.stderr)
        return 2

    started = time.monotonic()

    def on_wait(event: dict[str, Any]) -> None:
        print(f"[bekle] {event.get('elapsed', '?')} sn", file=sys.stderr, flush=True)

    answer = ask(base_url, api_key, prompt, on_wait=on_wait)
    elapsed = time.monotonic() - started
    if answer is None:
        print(f"[problem] {elapsed:.1f} sn — yedek sisteme geçilmeli", file=sys.stderr)
        return 1
    print(answer)
    print(f"[bitti] {elapsed:.1f} sn, {len(answer)} karakter", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
