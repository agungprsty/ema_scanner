import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)

# The event loop only keeps weak references to tasks, so a fire-and-forget task with no other
# reference can be garbage collected mid-flight (and an alert silently lost). Holding them here
# until they finish prevents that
_background_tasks: set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("Background task %s failed: %r", task.get_name(), task.exception())


def run_in_background(coro: Coroutine, name: str | None = None) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_background_tasks() -> int:
    return len(_background_tasks)
