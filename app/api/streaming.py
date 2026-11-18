"""Gövde üreticisi her koşulda kapatılan akış yanıtı.

Starlette'in `StreamingResponse`'u gövde üreticisini (async generator) yalnızca
tüketir; yanıt yarıda biterse onu **kapatmaz**. Üretici bir `yield`'de askıdayken
hata olursa (parça kodlanamadı, istemciye yazılamadı, ara katman iptal etti)
üreticinin `finally` bloğu çalışmaz; çöp toplayıcı ne zaman uğrarsa o zaman çalışır.

İstekler tek sıradan geçtiği için bu burada ölümcüldür: sıradaki yer üreticinin
`finally` bloğunda bırakılır. Gerçek sunucuda ölçüldü — upstream ikiye bölünmüş
bir emoji (tek başına vekil karakter) gönderdiğinde parça UTF-8'e çevrilemedi,
üretici askıda kaldı ve sıra 6 sn sonra hâlâ o isteğe aitti; emniyet supabına
(`REQUEST_QUEUE_MAX_HOLD_SECONDS`, 30 dk) kadar bütün istekler beklerdi.

`GuardedStreamingResponse` yanıt hangi yoldan biterse bitsin üreticiyi kapatır.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Süren kapanış görevleri (çöp toplayıcı yarıda silmesin diye referans tutulur).
_closing: set[asyncio.Future[None]] = set()


async def _aclose(iterator: Any) -> None:
    try:
        await iterator.aclose()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Kapanış hatası yanıtın sonucunu değiştirmez; yalnız görünür olsun.
        logger.warning("stream_close_failed", error_type=type(exc).__name__)


async def close_stream(iterator: Any) -> None:
    """Üreticiyi kapatır; çevredeki iptal kapanışı yarıda bırakamaz.

    Kapanış ayrı bir görevde koşar: bu satıra çoğu zaman bir iptalin içinden
    gelinir ve iptal edilmiş bir kapsamda `await` yeniden iptal edilebilir
    (anyio tabanlı ara katmanlar iptali her `await`'te yineler). Görev bundan
    etkilenmez ve işini bitirir.
    """
    if getattr(iterator, "aclose", None) is None:
        return
    # Biten üreticinin çerçevesi yoktur: kapatılacak bir şey kalmamıştır.
    if hasattr(iterator, "ag_frame") and iterator.ag_frame is None:
        return
    closer = asyncio.ensure_future(_aclose(iterator))
    _closing.add(closer)
    closer.add_done_callback(_closing.discard)
    await asyncio.shield(closer)


class GuardedStreamingResponse(StreamingResponse):
    """Yanıt nasıl biterse bitsin gövde üreticisini kapatan `StreamingResponse`."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_stream(self.body_iterator)
