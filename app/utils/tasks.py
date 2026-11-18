"""Arka plan görevleri için küçük kayıt defteri.

`asyncio.create_task` ile başlatılan bir göreve başka referans tutulmazsa çöp
toplayıcı onu yarıda silebilir. Bu sınıf görevleri bitene dek tutar, hatalarını
loglar ve kapanışta hepsini iptal eder.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class TaskRegistry:
    """Bitene dek referansı tutulan arka plan görevleri."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def __len__(self) -> int:
        return len(self._tasks)

    def spawn(self, work: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
        task = asyncio.create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            # Hata metni gizli değer taşıyabilir (örn. token'lı URL): yalnız türü.
            logger.warning(
                "background_task_failed",
                task=task.get_name(),
                error_type=type(error).__name__,
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
