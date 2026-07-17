from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.core.config import JOB_ID_RE
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


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Server-Sent Events stream of job state updates. Closes when the job
    reaches a terminal status (done, error, cancelled) or after 4 hours."""
    global _sse_active
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if _sse_active >= _MAX_SSE_CONNECTIONS:
        raise HTTPException(status_code=503, detail="too many concurrent streams")
    _sse_active += 1

    async def stream() -> AsyncIterator[str]:
        global _sse_active
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
                    if snapshot["status"] in ("done", "error", "cancelled"):
                        return
                elif job.status in ("done", "error", "cancelled"):
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
            _sse_active -= 1

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
