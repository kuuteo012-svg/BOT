"""Small asyncio task-tracking helpers used by message ingress."""

from __future__ import annotations

import asyncio

from logger_setup import logger


def schedule_tracked_task(coro, task_set: set, name: str) -> asyncio.Task:
    """Create a background task and consume its exception deterministically.

    Keeping the task in a caller-owned set lets shutdown await the exact tasks
    that may still be writing to DurableInbox.
    """
    task = asyncio.create_task(coro, name=name)
    task_set.add(task)

    def _finish(done_task: asyncio.Task) -> None:
        task_set.discard(done_task)
        if done_task.cancelled():
            return
        try:
            error = done_task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("❌ Telegram ingress task lỗi: %s", error)

    task.add_done_callback(_finish)
    return task
