"""Give back the memory the separation models hold while StemDeck is idle (#975).

The persistent Demucs worker (separate.py, #309) and the beat_this model
(beat_detect.py) load on first use and used to stay loaded until the server
stopped. Measured on an RTX 3080, 2026-11-05: the idle CUDA worker held
1.93 GB of RAM and 1.11 GB of VRAM, the idle CPU worker 1.46 GB of RAM.

Each is an IdleResource. A pipeline step holds `in_use()` around its use, and
the timer here unloads a resource nobody has used for the configured minutes
(the Settings value `idle_unload_minutes`, 0 for never). The next use loads it
again the ordinary way, `_get_worker()` and `_get_model()`, so the only cost is
the warm start the worker exists for: 5.8 s on the first separation after an
unload, measured on the same machine.

What keeps an unload from landing under a job:

- `in_use()` and the unload take the same lock, and the unload runs only with
  no user. A job that arrives during an unload waits for it to finish, then
  loads afresh, so it is never handed a worker that is half torn down.
- The timer does not even look while a job runs or waits in the queue, the
  pipeline lock is held (fill-stems, vocal split), or the server is stopping.
  The lock above is what makes it safe; this is what keeps it from unloading a
  worker that a job queued a second ago is about to use.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.config import IDLE_UNLOAD_CHECK_SEC
from app.core.settings import get_idle_unload_minutes
from app.pipeline import jobqueue

logger = logging.getLogger("stemdeck.idle")


class IdleResource:
    """Something loaded on first use, worth unloading after a quiet spell.

    `is_loaded` and `unload` are the owner's own functions; `unload` must be
    safe to call when nothing is loaded. `after_unload`, if given, runs once
    the lock is released, for clean-up no use should wait on (a full garbage
    collection). `clock` is a seam for tests."""

    def __init__(
        self,
        name: str,
        is_loaded: Callable[[], bool],
        unload: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        after_unload: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self._is_loaded = is_loaded
        self._unload = unload
        self._after_unload = after_unload
        self._clock = clock
        self._lock = threading.Lock()
        self._users = 0
        self._last_used = clock()

    @contextmanager
    def in_use(self) -> Iterator[None]:
        """Hold around any use. Waits for an unload in progress to finish."""
        with self._lock:
            self._users += 1
        try:
            yield
        finally:
            with self._lock:
                self._users -= 1
                # From the end of the use: a forty-minute CPU separation must
                # not count as forty idle minutes the moment it finishes.
                self._last_used = self._clock()

    def touch(self) -> None:
        """Count now as a use: the idle time starts again from here.

        Called on the event loop while the pipeline is busy, so it takes no
        lock: the lock can be held for seconds by an unload, and the loop must
        not wait on it. A plain float store; whichever of this and in_use()
        writes last, both wrote "now"."""
        self._last_used = self._clock()

    def unload_if_idle(self, idle_sec: float) -> bool:
        """Unload when loaded, unused, and idle for `idle_sec`. True if it did.

        The lock is held across the unload (up to 15 s for a worker that will
        not stop: separate._kill_worker), which is what makes a use that arrives meanwhile
        wait for it rather than race it."""
        with self._lock:
            if self._users or not self._is_loaded():
                return False
            idle = self._clock() - self._last_used
            if idle < idle_sec:
                return False
            self._unload()
        logger.info("unloaded %s after %d idle minutes", self.name, round(idle / 60))
        if self._after_unload is not None:
            self._after_unload()
        return True

    def unload_now(self) -> None:
        """Unload whatever the state, for shutdown. Waits for an idle unload
        in progress, so the two never stop the same process at once."""
        with self._lock:
            self._unload()


_resources: list[IdleResource] = []


def register(resource: IdleResource) -> IdleResource:
    _resources.append(resource)
    return resource


def _pipeline_busy() -> bool:
    """A job running, a job waiting in a queue that is not paused, anything
    holding the pipeline lock, or the server stopping. Read on the event loop,
    as the queue and the lock are.

    A paused queue is idle here: restored jobs can wait for Start for days,
    and nothing runs until someone presses it. A job that starts meanwhile is
    still safe: its in_use() waits for an unload to finish."""
    from app.pipeline.runner import _pipeline_lock

    running, waiting = jobqueue.snapshot()
    queued = bool(waiting) and not jobqueue.is_paused()
    return running is not None or queued or _pipeline_lock.locked() or jobqueue.is_stopping()


def _unload_idle(idle_sec: float) -> list[str]:
    done = []
    for resource in _resources:
        try:
            if resource.unload_if_idle(idle_sec):
                done.append(resource.name)
        except Exception:
            # One resource failing to stop must not keep the other loaded,
            # nor end the timer.
            logger.exception("could not unload %s", resource.name)
    return done


async def check() -> list[str]:
    """One look: unload what has been idle long enough. Returns the names."""
    minutes = get_idle_unload_minutes()
    if _pipeline_busy():
        # The idle time counts from when the pipeline goes quiet, not from the
        # end of the separation: lyrics and sections can run for many minutes
        # after it, and an unload the moment they end would hand the next
        # import a cold start for nothing. The idle time restarts at the last
        # busy check, so an unload can come up to IDLE_UNLOAD_CHECK_SEC (30 s)
        # earlier than the minutes after the pipeline goes quiet.
        for resource in _resources:
            resource.touch()
        return []
    if minutes <= 0:
        return []
    # Off the loop: stopping the worker waits up to 15 s for it to exit.
    return await asyncio.to_thread(_unload_idle, minutes * 60)


async def run(interval_sec: float = IDLE_UNLOAD_CHECK_SEC) -> None:
    """The timer. Started by the app lifespan, cancelled at shutdown."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await check()
        except Exception:
            logger.exception("idle unload check failed")
