"""Gerçek soketli test altyapısı: sahte upstream + wrapper, ikisi de uvicorn'da.

`TestClient` uygulamayı bellek içinde sürer; zamanlama, gerçek HTTP parçalama,
vekil/sıkıştırma davranışı ve **istemci kopması** orada gerçeğe benzemez.
`/v1/ask` sözleşmesinin tam da bu özelliklere dayanan kısımları (işaretlerin
zamanında ulaşması, kopan istemcinin sırayı boşaltması) burada gerçek bir
sunucuya karşı sınanır.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from app.main import create_app
from tests.conftest import TEST_API_KEY, make_settings


@dataclass
class UpstreamScript:
    """Sahte upstream'in bir sonraki isteklere nasıl cevap vereceği."""

    #: Cevaptan önce beklenecek süre (saniye).
    delay: float = 0.0
    #: 200 dışı bir değer verilirse `body` ile birlikte hata olarak döner.
    status: int = 200
    body: str = ""
    #: Başarılı cevapta gönderilecek metin parçaları.
    chunks: tuple[str, ...] = ("merhaba", " dünya")
    #: Parçalar arasında beklenecek süre (saniye). 0 = cevap tek seferde gönderilir.
    chunk_delay: float = 0.0
    #: Verilirse `chunks` yerine bu satırlar olduğu gibi gönderilir (ham akış).
    raw_lines: tuple[bytes, ...] = ()
    #: Gelen isteklerin başlangıç anları (`time.monotonic()`).
    started_at: list[float] = field(default_factory=list)
    #: Aynı anda işlenen istek sayısının gördüğü en yüksek değer.
    max_concurrent: int = 0
    #: Wrapper'ın cevabı beklemeden bağlantısını kapattığı istek sayısı.
    cancelled: int = 0
    #: Gelen isteklerin ham gövdeleri (hangi sorunun hangi hesaba gittiğini görmek için).
    bodies: list[str] = field(default_factory=list)
    _active: int = 0

    @property
    def calls(self) -> int:
        return len(self.started_at)


def _ai_stream_lines(script: UpstreamScript) -> list[bytes]:
    """Vercel AI SDK data-stream satırları (her biri satır sonuyla biter)."""
    finish = {"finishReason": "stop", "usage": {"promptTokens": 3, "completionTokens": 2}}
    if script.raw_lines:
        content = list(script.raw_lines)
    else:
        content = [
            f"0:{json.dumps(chunk, ensure_ascii=False)}".encode() for chunk in script.chunks
        ]
    lines = [b'f:{"messageId":"msg-live"}', *content]
    lines.append(("e:" + json.dumps(finish)).encode("utf-8"))
    lines.append(("d:" + json.dumps(finish)).encode("utf-8"))
    return [line + b"\n" for line in lines]


def build_upstream(
    script: UpstreamScript, accounts: dict[str, UpstreamScript] | None = None
) -> Starlette:
    """Hedef servisin akış ucunu taklit eden küçük uygulama.

    ``accounts`` verilirse istek, `Authorization`/`Cookie` başlığında geçen
    işarete göre o hesabın senaryosuyla cevaplanır (hesap başına ayrı sayaç);
    ``script`` bu durumda bütün isteklerin toplam sayacıdır.
    """

    def tracked(request: Request) -> tuple[UpstreamScript, list[UpstreamScript]]:
        if not accounts:
            return script, [script]
        identity = request.headers.get("authorization", "") + request.headers.get("cookie", "")
        for marker, candidate in accounts.items():
            if marker in identity:
                return candidate, [script, candidate]
        return script, [script]

    async def stream(request: Request) -> Response:
        body = (await request.body()).decode("utf-8", errors="replace")
        current, counters = tracked(request)
        now = time.monotonic()
        for counter in counters:
            counter.bodies.append(body)
            counter.started_at.append(now)
            counter._active += 1
            counter.max_concurrent = max(counter.max_concurrent, counter._active)
        try:
            return await respond(request, current)
        finally:
            for counter in counters:
                counter._active -= 1

    async def respond(request: Request, script: UpstreamScript) -> Response:
        remaining = script.delay
        while remaining > 0:
            if await request.is_disconnected():
                # Wrapper bağlantıyı kapattı (süre sınırı ya da istemcisi koptu).
                script.cancelled += 1
                return Response(status_code=499)
            step = min(0.02, remaining)
            await asyncio.sleep(step)
            remaining -= step
        if script.status != 200:
            return PlainTextResponse(script.body, status_code=script.status)

        lines = _ai_stream_lines(script)
        pause = script.chunk_delay

        async def body() -> Any:
            if pause <= 0:
                yield b"".join(lines)
                return
            try:
                for line in lines:
                    yield line
                    await asyncio.sleep(pause)
            except asyncio.CancelledError:
                # Wrapper akışın ortasında bağlantıyı kapattı.
                script.cancelled += 1
                raise

        return StreamingResponse(body(), media_type="text/plain")

    async def page(_request: Request) -> Response:
        return PlainTextResponse("<html></html>")

    async def health(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/nextjs-api/stream/post-to-evaluation/{chat_id}", stream, methods=["POST"]),
            Route("/c/{chat_id}", page),
            Route("/", health),
        ]
    )


def free_port() -> int:
    """Şu an boş olan bir TCP portu (wrapper kendi adresini önceden bilmeli)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def serve(app: Any, port: int = 0) -> Iterator[int]:
    """Uygulamayı gerçek uvicorn ile çalıştırır (port 0 = rastgele); portu verir."""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
        # `auto` uvloop'u süreç genelinde olay döngüsü politikası yapabilir (eski
        # uvicorn sürümleri); diğer testleri etkilemesin.
        loop="asyncio",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test sunucusu başlamadı")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@dataclass
class LiveWrapper:
    """Çalışan wrapper + sahte upstream çifti."""

    base_url: str
    script: UpstreamScript
    app: Any
    #: Çok hesaplı kurulumda hesap işareti → o hesabın senaryosu/sayaçları.
    accounts: dict[str, UpstreamScript] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TEST_API_KEY}"}


def wait_until(predicate: Any, timeout: float = 3.0, interval: float = 0.01) -> bool:
    """Koşul doğru olana dek (en çok `timeout` sn) bekler; sonucu döndürür."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@contextmanager
def live_wrapper(
    script: UpstreamScript | None = None,
    accounts: dict[str, UpstreamScript] | None = None,
    **overrides: Any,
) -> Iterator[LiveWrapper]:
    """Sahte upstream'i ve ona bağlı wrapper'ı gerçek soketlerde ayağa kaldırır."""
    script = script if script is not None else UpstreamScript()
    with serve(build_upstream(script, accounts)) as upstream_port:
        # Probe kendi servisine `settings.port` üzerinden bağlanır: port, uygulama
        # kurulmadan önce belli olmalı.
        own_port = free_port()
        settings = make_settings(
            target_domain=f"127.0.0.1:{upstream_port}",
            upstream_scheme="http",
            port=own_port,
            **overrides,
        )
        app = create_app(settings)
        with serve(app, own_port) as port:
            yield LiveWrapper(f"http://127.0.0.1:{port}", script, app, accounts or {})


#: İki hesaplı canlı testlerde hesapları ayırt eden erişim token'ları.
TOKEN_ONE = "TOKEN-ONE"
TOKEN_TWO = "TOKEN-TWO"


def two_accounts(**overrides: Any) -> dict[str, Any]:
    """`live_wrapper` için iki hesaplı ayarlar (hesaplar token'la ayırt edilir)."""
    base: dict[str, Any] = {
        "upstream_access_token": TOKEN_ONE,
        "upstream_account_name": "bir",
        "upstream_access_token_2": TOKEN_TWO,
        "upstream_account_2_name": "iki",
    }
    base.update(overrides)
    return base
