from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import JOB_ID_RE
from app.core.models import FINISHED_STATUSES
from app.core.registry import get as registry_get

router = APIRouter(tags=["events"])

# Close SSE connections that outlive this threshold to prevent zombie
# connections from accumulating when clients disconnect without a TCP RST.
_MAX_SSE_SECONDS = 4 * 3600  # 4 hours
# Hard cap on concurrent SSE connections to prevent resource exhaustion
# from tab leaks or aggressive reconnect loops.
_MAX_SSE_CONNECTIONS = 200
# Counter is only mutated from the async event loop (no awaits between
# check+increment), so no lock is needed.
_sse_active = 0


def claim_sse_slot() -> None:
    """Reserve one of the shared connection slots, or 503. Split out so the
    queue stream in app/api/queue.py shares one budget with this one rather
    than each getting its own."""
    global _sse_active
    if _sse_active >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(status_code=503, detail="too many concurrent streams")
    _sse_active += 1


def release_sse_slot() -> None:
    global _sse_active
    _sse_active -= 1


class SseSlot:
    """One held connection slot, released exactly once.

    Claiming has to happen in the handler so that hitting the cap can still be
    answered with a 503 -- once the generator is running the response headers
    have gone out and there is no status code left to send.

    That is what leaked slots: release lives in the stream's `finally`, and an
    async generator that is never started never runs its `finally`. If the
    client disconnects before the body begins, StreamingResponse raises inside
    `stream_response` on its first `send()` -- before `__anext__` is ever
    called -- so the generator body never executes and the slot was held
    forever. 200 of those and every progress stream 503s with nothing actually
    connected, until the process restarts (#513).

    The stream releases on its way out as before; `__del__` is the backstop for
    the never-started case, where collecting the generator collects the closure
    holding this. Release is idempotent so the two cannot double-count.
    """

    __slots__ = ("_held",)

    def __init__(self) -> None:
        # Set first: claim_sse_slot raises at the cap, and __del__ still runs on
        # a half-built object. Without this it would raise AttributeError from
        # __del__ instead of releasing nothing.
        self._held = False
        claim_sse_slot()  # may raise 503; nothing is held if it does
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            release_sse_slot()

    def __del__(self) -> None:
        self.release()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Server-Sent Events stream of job state updates. Closes when the job
    reaches a terminal status (done, error, cancelled) or after 4 hours."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    slot = SseSlot()

    async def stream() -> AsyncIterator[str]:
        try:
            last_v = -1
            keepalive_at = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _MAX_SSE_SECONDS
            while loop.time() < deadline:
                v = job.version
                if v != last_v:
                    snapshot = job.to_state()
                    if job.version != v:
                        # _set() ran mid-serialize (#285): this snapshot may mix
                        # fields from before and after the write (a torn read).
                        # Discard it and re-serialize next loop instead of
                        # sleeping, so the client never sees an inconsistent
                        # progress/stage pair.
                        continue
                    yield f"data: {json.dumps(snapshot)}\n\n"
                    last_v = v
                    keepalive_at = 0
                    if snapshot["status"] in FINISHED_STATUSES:
                        return
                elif job.status in FINISHED_STATUSES:
                    # Already-terminal with no pending change (e.g. the job was
                    # done before this connection opened) -- close promptly
                    # instead of idling on int-compares until the SSE cap.
                    return
                keepalive_at += 1
                if keepalive_at >= 75:  # ~15s
                    yield ": keepalive\n\n"
                    keepalive_at = 0
                await asyncio.sleep(0.2)
        finally:
            slot.release()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
