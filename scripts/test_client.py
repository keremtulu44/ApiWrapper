"""Yerel API'yi uçtan uca doğrulayan elle çalıştırılabilir istemci.

Kullanım:
    # 1) (opsiyonel) sahte upstream
    python scripts/mock_upstream.py &
    # 2) servis
    make run &
    # 3) doğrulama
    python scripts/test_client.py --model gpt-4o-mini

Ortam değişkenleri: BASE_URL (vars. http://127.0.0.1:8000/v1), API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
API_KEY = os.getenv("API_KEY", "sk-local-dev-key")

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"{GREEN}  PASS{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))
    else:
        _failed += 1
        print(f"{RED}  FAIL{RESET} {label}" + (f" {DIM}{detail}{RESET}" if detail else ""))


def request(path: str, payload: dict | None = None, stream: bool = False):
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {API_KEY}")
    if data:
        req.add_header("Content-Type", "application/json")
    response = urllib.request.urlopen(req, timeout=120)
    if stream:
        return response
    return json.loads(response.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None, help="Kullanılacak model (vars. ilk model)")
    parser.add_argument("--prompt", default="Merhaba, kendini kısaca tanıt.")
    args = parser.parse_args()

    print(f"\n=== ApiWrapper doğrulama · {BASE_URL} ===\n")

    # 1) modeller
    print("[1] GET /models")
    try:
        models = request("/models")
    except urllib.error.URLError as exc:
        print(f"{RED}Servise ulaşılamadı: {exc}{RESET}")
        return 2
    ids = [m["id"] for m in models.get("data", [])]
    check("model listesi dolu", bool(ids), str(ids))
    model = args.model or (ids[0] if ids else "")

    # 1b) upstream kimlik teşhisi
    print("\n[1b] GET /admin/auth (upstream kimlik teşhisi)")
    try:
        diag = request("/admin/auth")
        token = diag.get("token", {})
        if diag.get("authorization_header_will_be_sent"):
            check("Authorization başlığı gönderilecek", True,
                  f"kaynak={diag.get('source')}, jwt={token.get('is_jwt')}, "
                  f"kalan={token.get('expires_in_seconds')}s")
            check("token süresi dolmamış", not token.get("expired"))
        else:
            print(f"{DIM}    Authorization gönderilmiyor "
                  f"(cookie/token yapılandırılmamış olabilir){RESET}")
    except urllib.error.HTTPError as exc:
        print(f"{DIM}    /admin/auth kullanılamadı: HTTP {exc.code}{RESET}")

    # 2) non-stream
    print("\n[2] POST /chat/completions (stream=false)")
    body = {"model": model, "messages": [{"role": "user", "content": args.prompt}]}
    started = time.monotonic()
    result = request("/chat/completions", body)
    elapsed = time.monotonic() - started
    content = result["choices"][0]["message"]["content"]
    check("object == chat.completion", result.get("object") == "chat.completion")
    check("içerik boş değil", bool(content), f"{len(content)} karakter, {elapsed:.2f}s")
    check("usage.total_tokens > 0", result["usage"]["total_tokens"] > 0,
          json.dumps(result["usage"]))
    check("finish_reason == stop", result["choices"][0]["finish_reason"] == "stop")
    print(f"{DIM}    -> {content[:100]}{RESET}")

    # 3) stream
    print("\n[3] POST /chat/completions (stream=true, include_usage)")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": args.prompt}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.monotonic()
    first_token_at = None
    chunks: list[dict] = []
    saw_done = False
    stream_error: dict | None = None
    text = ""
    with request("/chat/completions", body, stream=True) as response:
        check("content-type text/event-stream",
              response.headers.get("content-type", "").startswith("text/event-stream"))
        for raw in response:
            line = raw.decode("utf-8").rstrip("\n")
            if not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                saw_done = True
                break
            chunk = json.loads(payload)
            chunks.append(chunk)
            if chunk.get("error") and stream_error is None:
                error = chunk["error"]
                stream_error = error if isinstance(error, dict) else {"message": str(error)}
                continue
            if chunk.get("choices"):
                piece = chunk["choices"][0].get("delta", {}).get("content") or ""
                if piece and first_token_at is None:
                    first_token_at = time.monotonic() - started
                text += piece

    check("ilk chunk role=assistant",
          bool(chunks) and chunks[0]["choices"][0]["delta"].get("role") == "assistant")
    if stream_error is not None:
        # HTTP 200 ile başlayan SSE içindeki upstream hatasını "boş cevap" gibi
        # göstermemek için asıl kod ve mesajı doğrudan yüzeye çıkar.
        check("stream sırasında hata yok", False,
              json.dumps(stream_error, ensure_ascii=False))
    else:
        check("metin biriktirildi", bool(text), f"{len(text)} karakter")
        check("finish_reason chunk'ı var",
              any(c.get("choices") and c["choices"][0].get("finish_reason") for c in chunks))
        check("usage chunk'ı var", any(c.get("usage") for c in chunks))
    check("[DONE] alındı", saw_done)
    if first_token_at is not None:
        print(f"{DIM}    TTFT: {first_token_at * 1000:.0f} ms · "
              f"{len(chunks)} chunk{RESET}")
    print(f"{DIM}    -> {text[:100]}{RESET}")

    # 4) hata yolları
    print("\n[4] Hata yolları")
    try:
        request("/chat/completions", {"model": "___yok___",
                                      "messages": [{"role": "user", "content": "x"}]})
        check("bilinmeyen model 404", False, "hata beklenmişti")
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        check("bilinmeyen model 404", exc.code == 404,
              payload.get("error", {}).get("code", ""))

    try:
        request("/chat/completions", {"model": model, "messages": []})
        check("boş messages 400", False, "hata beklenmişti")
    except urllib.error.HTTPError as exc:
        check("boş messages 400", exc.code == 400)

    req = urllib.request.Request(f"{BASE_URL}/models")
    try:
        urllib.request.urlopen(req, timeout=30)
        check("anahtarsız istek 401", False, "hata beklenmişti")
    except urllib.error.HTTPError as exc:
        check("anahtarsız istek 401", exc.code == 401)

    print(f"\n=== {_passed} geçti, {_failed} başarısız ===\n")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
