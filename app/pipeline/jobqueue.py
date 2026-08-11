"""Serial import queue.

Jobs used to be started with a bare `asyncio.create_task(run_pipeline(...))` and
then all blocked on `_pipeline_lock` inside the pipeline. That worked, but the
"queue" was invisible: waiting jobs were coroutines parked on a semaphore, with
no list to show the user, no stable position, and no way to cancel one before it
started. This module makes the queue explicit.

Exactly one job runs at a time, and that is a correctness requirement rather
than a tuning choice: `separate.py` keeps a single module-global demucs worker
process and `registry.set_proc` maps a job id onto it, so two concurrent jobs
would interleave requests on one stdin, misattribute progress parsed from one
stderr, and a cancel on either would terminate the worker out from under the
other. The single consumer here is what guarantees that; `_pipeline_lock` stays
in runner.py as a second line of defence for anything calling the pipeline
directly (the tests do).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections import deque
from pathlib import Path

from app.core.config import JOBS_DIR
from app.core.models import Job, _set
from app.core.registry import get as registry_get
from app.core.registry import persist as registry_persist

logger = logging.getLogger("stemdeck.queue")

# Waiting job ids, oldest first. Guarded by _lock because sync (threadpool)
# endpoints read and mutate it -- matching registry.py's threading.Lock model.
_queue: deque[str] = deque()
_running_id: str | None = None
_lock = threading.Lock()

_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None
_worker_task: asyncio.Task | None = None
_stopping = False


def _notify() -> None:
    """Wake the worker. Safe to call from a worker thread: sync FastAPI
    endpoints run in Starlette's threadpool, so touching the asyncio.Event
    directly from there would be a cross-thread mutation."""
    if _loop is None or _wake is None:
        return
    try:
        _loop.call_soon_threadsafe(_wake.set)
    except RuntimeError:
        # Loop already closed (shutdown races a late enqueue). Nothing to wake.
        pass


def enqueue(job_id: str) -> None:
    with _lock:
        if job_id not in _queue and job_id != _running_id:
            _queue.append(job_id)
    _notify()


def discard(job_id: str) -> bool:
    """Remove a job that has not started yet. Returns True if it was still
    waiting, which tells the caller it can finalise the job itself -- the
    worker will never touch it."""
    with _lock:
        if job_id in _queue:
            _queue.remove(job_id)
            return True
    return False


def snapshot() -> tuple[str | None, list[str]]:
    """(running_id, waiting_ids). One lock acquisition so the pair is coherent."""
    with _lock:
        return _running_id, list(_queue)


def running_id() -> str | None:
    with _lock:
        return _running_id


def depth() -> int:
    with _lock:
        return len(_queue)


def clear() -> None:
    """Drop every waiting job. Used by /api/reset, which has already refused to
    run if anything is in flight."""
    with _lock:
        _queue.clear()


def _pop_next() -> str | None:
    with _lock:
        return _queue.popleft() if _queue else None


def _set_running(job_id: str | None) -> None:
    global _running_id
    with _lock:
        _running_id = job_id


def _find_local_source(job_dir: Path) -> Path | None:
    """The uploaded file for a `local:` job. The extension varies, and a resumed
    job only knows its directory."""
    for candidate in sorted(job_dir.glob("source.*")):
        if candidate.is_file():
            return candidate
    return None


async def _dispatch(job: Job) -> None:
    """Run one job. The mode is derived from source_url rather than passed in,
    so a fresh submit and a post-restart resume go through the same path."""
    from app.pipeline.runner import run_local_pipeline, run_pipeline

    source_url = job.source_url or ""
    if source_url.startswith("local:"):
        source = _find_local_source(JOBS_DIR / job.id)
        if source is None:
            logger.warning("[%s] local source missing; cannot run", job.id)
            _set(
                job,
                status="error",
                stage="Error: Source file missing",
                error="The uploaded file is no longer available. Re-import it to finish.",
            )
            registry_persist(JOBS_DIR)
            return
        await run_local_pipeline(job, source, JOBS_DIR)
        return
    await run_pipeline(job, source_url, JOBS_DIR)


async def _worker_loop() -> None:
    assert _wake is not None
    while not _stopping:
        job_id = _pop_next()
        if job_id is None:
            _wake.clear()
            await _wake.wait()
            continue

        job = registry_get(job_id)
        if job is None:
            continue
        if job.cancel_requested or job.status in ("done", "error", "cancelled"):
            # Cancelled or finished while it waited; drop it silently.
            continue

        # Claim it. No await between the pop and this status write, so a job is
        # never counted as both waiting and running -- which is what lets
        # register_if_capacity keep counting only "queued" as the queue depth.
        _set_running(job_id)
        _set(job, status="processing", stage="Starting...", progress=0.0)
        try:
            await _dispatch(job)
        except Exception:
            # A crash in one job must not take the queue down with it.
            logger.exception("[%s] queue worker: job raised", job_id)
        finally:
            _set_running(None)


def start_worker() -> asyncio.Task:
    """Start the single consumer. Called from the app lifespan, where there is a
    running loop -- registry.restore() runs at import time and must not touch
    asyncio."""
    global _wake, _loop, _worker_task, _stopping
    _stopping = False
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    _wake.set()  # do one pass immediately, in case resume already enqueued work
    _worker_task = asyncio.create_task(_worker_loop())
    return _worker_task


def request_stop() -> None:
    """Stop picking up new work. Deliberately does not cancel the in-flight job:
    it is blocked in asyncio.to_thread, which cannot be cancelled, so it dies
    with the process exactly as it did before the queue existed."""
    global _stopping
    _stopping = True
    _notify()


def cleanup_job_dir(job_id: str) -> None:
    """Remove a cancelled-before-start job's directory. A queued local upload
    holds its source file (up to 400 MB) for the whole wait."""
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
