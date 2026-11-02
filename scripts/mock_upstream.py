"""Geliştirme için sahte upstream: AI SDK data-stream yayınlar.

Kullanım:
    python scripts/mock_upstream.py            # 9100 portunda dinler
Ardından .env içinde:
    TARGET_DOMAIN=127.0.0.1:9100
    UPSTREAM_SCHEME=http

Kota simülasyonu (hesap havuzu devrini canlı görmek için):
    MOCK_QUOTA_AFTER=3 python scripts/mock_upstream.py
Her hesap (Cookie başlığına göre ayrı sayılır) bu kadar mesajdan sonra
"upstream limit reached" döndürür; sarmalayıcı bunu yakalayıp diğer hesaba
geçmeli ve /v1/admin/accounts altında cooldown görünmelidir.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="Mock Upstream LLM")

#: True ise, gerçek hedef gibi davranıp Authorization başlığını zorunlu kılar.
REQUIRE_AUTH = os.getenv("MOCK_REQUIRE_AUTH", "1") not in ("0", "false", "False")

#: 0'dan büyükse, her hesap bu kadar mesajdan sonra kota mesajı alır.
QUOTA_AFTER = int(os.getenv("MOCK_QUOTA_AFTER", "0") or 0)
#: Kota cevabının biçimi: "text" (delta olarak) ya da "error" (3: olayı).
QUOTA_STYLE = os.getenv("MOCK_QUOTA_STYLE", "text")
QUOTA_MESSAGE = "upstream limit reached, please try again later"

#: Cookie başlığı -> gönderilen mesaj sayısı. Hesap başına ayrı tutulur ki
#: devir (failover) davranışı gözlemlenebilsin.
_message_counts: dict[str, int] = defaultdict(int)


@app.get("/c/{chat_id}")
async def chat_page(chat_id: str) -> HTMLResponse:
    return HTMLResponse(f"<html><body>chat {chat_id}</body></html>")


@app.post("/nextjs-api/stream/post-to-evaluation/{chat_id}")
@app.post("/nextjs-api/stream/create-evaluation")
async def stream(request: Request, chat_id: str = "mock"):
    if REQUIRE_AUTH:
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer ") or len(authorization) < 20:
            return JSONResponse(
                {"error": "Unauthorized: missing or invalid access token"},
                status_code=401,
            )

    raw = (await request.body()).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    prompt = payload.get("userMessage", {}).get("content", "")
    model = payload.get("modelAId", "unknown")

    # --- kota simülasyonu -------------------------------------------------
    account = request.headers.get("cookie", "") or "anonymous"
    count = 0
    if QUOTA_AFTER > 0:
        _message_counts[account] += 1
        count = _message_counts[account]
    if QUOTA_AFTER > 0 and count > QUOTA_AFTER:
        if QUOTA_STYLE == "error":
            return JSONResponse({"error": QUOTA_MESSAGE}, status_code=429)

        async def quota_gen():
            if QUOTA_STYLE == "text":
                # Kısıtlama mesajı bazen hata olayı yerine metin delta'sı gelir.
                yield f"0:{json.dumps(QUOTA_MESSAGE, ensure_ascii=False)}\n".encode()
            yield f"3:{json.dumps(QUOTA_MESSAGE, ensure_ascii=False)}\n".encode()
            yield b"d:{\"finishReason\": \"stop\"}\n"

        return StreamingResponse(quota_gen(), media_type="text/plain; charset=utf-8")

    reply = (
        f"[{model}] Merhaba! İsteğinizi aldım ({len(prompt)} karakter). "
        "Bu, sahte upstream tarafından üretilmiş akışlı bir yanıttır. "
        "Türkçe karakterler: ğüşiöçİĞÜŞÖÇ."
    )

    async def gen():
        yield f'f:{json.dumps({"messageId": chat_id})}\n'.encode()
        for word in reply.split(" "):
            await asyncio.sleep(0.02)
            yield f"0:{json.dumps(word + ' ', ensure_ascii=False)}\n".encode()
        done = {
            "finishReason": "stop",
            "usage": {"promptTokens": len(prompt) // 4, "completionTokens": len(reply) // 4},
        }
        yield f"e:{json.dumps(done)}\n".encode()
        yield f"d:{json.dumps(done)}\n".encode()

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.get("/__mock__/quota", include_in_schema=False)
async def quota_state() -> JSONResponse:
    """Kaç hesap kaç mesaj gönderdi? (simülasyonun görünürlüğü için)"""
    return JSONResponse(
        {
            "quota_after": QUOTA_AFTER,
            "accounts": {f"hesap-{i}": n for i, n in enumerate(_message_counts.values(), 1)},
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9100, log_level="warning")

