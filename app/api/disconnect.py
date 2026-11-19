"""İstemci kopmasını algılama.

`request.is_disconnected()` bu uygulamanın ara katman yığınında (üç
`BaseHTTPMiddleware`) **akışsız** uçlarda çalışmıyor: gerçek sunucuda ölçüldü,
istemci bağlantıyı kapattıktan 3 sn sonra bile ``False`` dönüyor. ASGI
`receive()` kanalını dinlemek ise kopmayı anında bildiriyor. Bu modül o yolu
kullanır.

Neden önemli: istekler sıradan geçer (hesap başına tek tek). Cevabı beklemekten vazgeçmiş bir
istemcinin isteği sırada kalır ya da işlenmeye devam ederse hem kotayı boşa
harcar hem arkasındaki istekleri bekletir.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from fastapi import Request

from app.core.errors import ClientClosedRequestError
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def wait_for_disconnect(request: Request) -> None:
    """İstemci bağlantıyı kapatana dek bekler (gövde okunmuş olmalı)."""
    while True:
        try:
            message = await request.receive()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Kanal beklenmedik biçimde bozuldu: bunu "koptu" saymak çalışan bir
            # isteği haksız yere iptal ederdi. İptal edilene dek bekle.
            logger.debug("disconnect_watch_failed", error_type=type(exc).__name__)
            await asyncio.Event().wait()
            return
        if message.get("type") == "http.disconnect":
            return


def _retrieve_exception(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def cancel_on_disconnect(request: Request, work: Coroutine[Any, Any, T]) -> T:
    """İşi çalıştırır; istemci beklemekten vazgeçerse iptal eder.

    İptalde sıradaki yer / işlem izni hemen boşalır ve `ClientClosedRequestError`
    fırlatılır (cevap kimseye ulaşmaz; yalnız erişim logunda 499 görünür).
    """
    job = asyncio.ensure_future(work)
    job.add_done_callback(_retrieve_exception)
    watch = asyncio.ensure_future(wait_for_disconnect(request))
    try:
        await asyncio.wait({job, watch}, return_when=asyncio.FIRST_COMPLETED)
        if job.done():
            return job.result()
        job.cancel()
        logger.info("client_disconnected", path=request.url.path, stream=False)
        raise ClientClosedRequestError("The client closed the connection before the response.")
    finally:
        watch.cancel()
        if not job.done():
            job.cancel()
