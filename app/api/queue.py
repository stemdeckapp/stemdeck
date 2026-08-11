"""Aggregate view of the import queue.

One stream for every waiting job rather than one per job. A Map of per-job
EventSources would be simpler on the client, but browsers cap concurrent
connections per origin at around six on HTTP/1.1, and the studio already spends
most of that budget fetching stem WAVs -- twenty queue streams would starve
audio loading long before hitting the server-side connection cap.

The payload is deliberately compact (to_queue_state, not to_state). The
foreground import keeps its own /api/jobs/{id}/events stream, which is what
carries the full completion state the studio needs.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.events import _MAX_SSE_SECONDS, claim_sse_slot, release_sse_slot
from app.core.config import JOB_ID_RE, JOBS_DIR, MAX_PENDING_UPLOAD_JOBS, MAX_PENDING_URL_JOBS
from app.core.registry import get as registry_get
from app.core.registry import pending_count as registry_pending_count
from app.core.registry import persist as registry_persist
from app.pipeline import jobqueue

router = APIRouter(tags=["queue"])


def _snapshot() -> dict[str, Any]:
    """Current queue as the client sees it. Position is derived here rather than
    stored on the Job: it changes for every waiting job whenever the head is
    dequeued, so writing it through _set() would bump N versions and wake N
    streams for a value that is just a list index."""
    running_id, waiting = jobqueue.snapshot()

    running = None
    if running_id is not None:
        job = registry_get(running_id)
        if job is not None:
            running = job.to_queue_state()

    queued = []
    for position, job_id in enumerate(waiting):
        job = registry_get(job_id)
        if job is None:
            continue
        rec = job.to_queue_state()
        rec["position"] = position
        queued.append(rec)

    # Capacity is per kind: a waiting upload holds its source file on disk, a
    # waiting link holds nothing, so they are bounded separately.
    uploads_pending = registry_pending_count(uploads=True)
    urls_pending = registry_pending_count(uploads=False)
    return {
        "running": running,
        "queued": queued,
        # True when jobs were restored from a previous session and are waiting
        # for the user to start them.
        "paused": jobqueue.is_paused(),
        "max_pending_uploads": MAX_PENDING_UPLOAD_JOBS,
        "max_pending_urls": MAX_PENDING_URL_JOBS,
        "capacity_left_uploads": max(0, MAX_PENDING_UPLOAD_JOBS - uploads_pending),
        "capacity_left_urls": max(0, MAX_PENDING_URL_JOBS - urls_pending),
    }


def _fingerprint() -> tuple:
    """Cheap change detector: the ids in order plus each job's version counter,
    which _set() already bumps on every field write."""
    running_id, waiting = jobqueue.snapshot()
    ids = ([running_id] if running_id else []) + waiting
    out: list[object] = [jobqueue.is_paused()]
    for job_id in ids:
        job = registry_get(job_id)
        out.append((job_id, job.version if job is not None else -1))
    return tuple(out)


@router.post("/start")
def start_queue() -> dict[str, Any]:
    """Begin working through a queue restored from a previous session."""
    jobqueue.resume()
    return _snapshot()


class ReorderRequest(BaseModel):
    job_id: str
    # The job it should sit directly after. None means "move to the front",
    # which is also what a Move to top control sends.
    after: str | None = None


@router.post("/reorder")
def reorder_queue(payload: ReorderRequest) -> dict[str, Any]:
    """Move a waiting job. Returns the resulting queue, so a client whose drag
    raced a job finishing re-syncs from the answer instead of guessing."""
    if not JOB_ID_RE.match(payload.job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if payload.after is not None and not JOB_ID_RE.match(payload.after):
        raise HTTPException(status_code=404, detail="job not found")
    if payload.job_id == payload.after:
        raise HTTPException(status_code=422, detail="a job cannot follow itself")
    if not jobqueue.reorder(payload.job_id, payload.after):
        # Already started or finished. Not an error the user can act on -- the
        # snapshot below tells the client what is true now.
        raise HTTPException(status_code=409, detail="that job is no longer waiting")
    registry_persist(JOBS_DIR)
    return _snapshot()


@router.get("")
def get_queue() -> dict[str, Any]:
    """The queue right now. Used for first paint and as the polling fallback
    when the stream cannot connect."""
    return _snapshot()


@router.get("/events")
async def queue_events() -> StreamingResponse:
    """SSE stream of the whole queue.

    Unlike the per-job stream this never self-closes on a terminal status: it
    outlives any individual job and is expected to stay open for the session,
    so only the 4 h ceiling ends it.
    """
    claim_sse_slot()

    async def stream() -> AsyncIterator[str]:
        try:
            last_fp: tuple | None = None
            keepalive_at = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _MAX_SSE_SECONDS
            while loop.time() < deadline:
                fp = _fingerprint()
                if fp != last_fp:
                    snapshot = _snapshot()
                    if _fingerprint() != fp:
                        # _set() landed mid-serialize; the snapshot could mix
                        # pre- and post-write fields. Re-read next tick rather
                        # than emit a torn frame (same guard as job_events).
                        continue
                    yield f"data: {json.dumps(snapshot)}\n\n"
                    last_fp = fp
                    keepalive_at = 0
                keepalive_at += 1
                if keepalive_at >= 60:  # ~15 s at the poll interval below
                    yield ": keepalive\n\n"
                    keepalive_at = 0
                await asyncio.sleep(0.25)
        finally:
            release_sse_slot()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
