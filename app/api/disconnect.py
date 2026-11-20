"""İstemci kopmasını algılama.

`request.is_disconnected()` bu uygulamanın ara katman yığınında (üç
`BaseHTTPMiddleware`) **akışsız** uçlarda çalışmıyor: gerçek sunucuda ölçüldü,
istemci bağlantıyı kapattıktan 3 sn sonra bile ``False`` dönüyor. ASGI
`receive()` kanalını dinlemek ise kopmayı anında bildiriyor. Bu modül o yolu
kullanır.

Neden önemli: istekler sıradan geçer (hesap başına tek tek). Cevabı beklemekten
vazgeçmiş bir istemcinin isteği **sırada** kalırsa arkasındakileri boş yere
bekletir; o yüzden henüz upstream'e gitmemiş istek iptal edilir.

Upstream'e **gönderilmiş** akışsız istek ise yarıda kesilmez: canlı sürümde
(`03aca2c`) akışsız uçta istemci kopması hiç izlenmiyordu, istek upstream'de
sonuna kadar sürüyordu (gerçek soketle ölçüldü, docs/LESSONS §20). Gönderme
tarafının davranışı değişmesin diye aynısı korunur: cevap sonuna kadar okunur
(yanıtla gelen oturum çerezi de işlenir), sonuç kimseye ulaşmaz, sıra iş bitince
boşalır.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
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


async def cancel_on_disconnect(
    request: Request,
    work: Coroutine[Any, Any, T],
    *,
    committed: Callable[[], bool] | None = None,
) -> T:
    """İşi çalıştırır; istemci beklemekten vazgeçerse **gönderilmemiş** işi iptal eder.

    ``committed`` iş upstream'e gönderildiyse True döner. İstemci koptuğunda:

    * İş henüz gönderilmediyse (sırada ya da aralık beklemesinde) iptal edilir;
      sıradaki yer hemen boşalır ve `ClientClosedRequestError` fırlatılır (cevap
      kimseye ulaşmaz; yalnız erişim logunda 499 görünür).
    * İş gönderildiyse **dokunulmaz**: sonuna kadar sürer ve sonucu (ya da
      hatası) olağan yoldan döner — canlı sürümdeki davranışın aynısı. Sunucu,
      bağlantısı kapanmış istemciye yazılamayan cevabı sessizce atar.

    ``committed`` verilmezse iş her durumda iptal edilir.
    """
    job = asyncio.ensure_future(work)
    job.add_done_callback(_retrieve_exception)
    watch = asyncio.ensure_future(wait_for_disconnect(request))
    try:
        await asyncio.wait({job, watch}, return_when=asyncio.FIRST_COMPLETED)
        if job.done():
            return job.result()
        if committed is not None and committed():
            logger.info(
                "client_disconnected",
                path=request.url.path,
                stream=False,
                upstream="in_flight",
                action="finish",
            )
            await asyncio.wait({job})
            return job.result()
        job.cancel()
        logger.info("client_disconnected", path=request.url.path, stream=False)
        raise ClientClosedRequestError("The client closed the connection before the response.")
    finally:
        watch.cancel()
        if not job.done():
            job.cancel()
