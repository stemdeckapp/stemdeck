"""Playlist import: expand a playlist URL into one queued job per track.

Two steps on purpose. Expansion is a network round trip whose result the user
has to agree to -- "this is 47 tracks, still want it?" -- and a single endpoint
would either queue 47 jobs with no warning or make the client re-post a list of
URLs it was handed. The client never supplies the track list: preview and
create both expand server-side, so nothing outside the SSRF allowlist can be
smuggled into the pipeline between the two calls.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.core.config import JOBS_DIR, MAX_PENDING_URL_JOBS, STEM_NAMES
from app.core.models import Job
from app.core.registry import pending_count as registry_pending_count
from app.core.registry import persist as registry_persist
from app.core.registry import register_if_capacity as registry_register_if_capacity
from app.core.settings import get_max_duration_sec, get_playlist_max_items
from app.core.stems_location import is_relocating
from app.pipeline import jobqueue
from app.pipeline.download import InvalidPlaylistURL, expand_playlist

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["playlist"])


class PlaylistRequest(BaseModel):
    url: str
    stems: list[str] | None = None


def _capacity_left() -> int:
    # Playlist tracks are links, so only other waiting links count against
    # them -- a backlog of file uploads is bounded separately and holds disk
    # rather than a registry record.
    return max(0, MAX_PENDING_URL_JOBS - registry_pending_count(uploads=False))


async def _expand(url: str) -> dict[str, Any]:
    """yt-dlp is blocking and a large playlist takes seconds. Off the event loop
    it goes, or every SSE stream (including the running job's progress) stalls
    for the duration."""
    try:
        return await asyncio.to_thread(expand_playlist, url, get_playlist_max_items())
    except InvalidPlaylistURL as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("playlist expansion failed")
        raise HTTPException(status_code=502, detail="Could not read that playlist") from e


def _partition(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Split out tracks that are longer than the configured limit. They would be
    accepted here and then fail one by one in the pipeline, so the dialog is a
    better place to say so."""
    max_duration = get_max_duration_sec()
    cap = get_playlist_max_items()
    keep, too_long = [], 0
    for item in items:
        duration = item.get("duration")
        if isinstance(duration, int | float) and duration > max_duration:
            too_long += 1
            continue
        keep.append(item)
    return keep[:cap], too_long


@router.post("/preview")
async def preview_playlist(request: Request) -> dict[str, Any]:
    """Expand a playlist and report what importing it would do. Creates nothing."""
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}") from e
    try:
        payload = PlaylistRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    result = await _expand(payload.url)
    items, too_long = _partition(result["items"])
    capacity = _capacity_left()

    return {
        "playlist_title": result["playlist_title"],
        "total_found": len(result["items"]) + result["unavailable"],
        "will_queue": min(len(items), capacity),
        "skipped_unavailable": result["unavailable"],
        "skipped_too_long": too_long,
        "capacity_left": capacity,
        "cap": get_playlist_max_items(),
        # The playlist has more tracks than the cap allows us to look at, so
        # total_found is a floor, not the real total.
        "truncated": result["truncated"],
        "items": [{"title": i["title"], "duration": i["duration"]} for i in items],
    }


@router.post("")
async def create_playlist_jobs(request: Request) -> dict[str, Any]:
    """Queue one job per playlist track, in playlist order.

    Fills whatever capacity is available rather than failing the whole import:
    the preview already told the user how many would be queued, and a partial
    fill with an honest count beats a 503 after they agreed to it.
    """
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}") from e
    try:
        payload = PlaylistRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if is_relocating():
        raise HTTPException(
            status_code=409,
            detail="Restart StemDeck to finish moving your stems folder before importing",
        )

    selected = [s for s in payload.stems if s in STEM_NAMES] if payload.stems else list(STEM_NAMES)
    if not selected:
        selected = list(STEM_NAMES)

    result = await _expand(payload.url)
    items, too_long = _partition(result["items"])
    if not items:
        raise HTTPException(status_code=422, detail="No importable tracks in that playlist")
    if _capacity_left() == 0:
        raise HTTPException(status_code=503, detail="Queue is full - wait or cancel a job")

    created: list[dict[str, Any]] = []
    for item in items:
        job = Job(
            id=uuid.uuid4().hex[:12],
            selected_stems=list(selected),
            source_url=item["url"],
            # Pre-filled from the flat metadata so queue rows carry real names
            # immediately, instead of a URL until each download starts.
            title=item["title"] or None,
            thumbnail=item.get("thumbnail"),
        )
        if not registry_register_if_capacity(job, MAX_PENDING_URL_JOBS):
            break  # queue filled up mid-loop; report what did land
        jobqueue.enqueue(job.id)
        created.append({"job_id": job.id, "title": job.title or "", "source_url": item["url"]})

    registry_persist(JOBS_DIR)
    return {
        "playlist_title": result["playlist_title"],
        "jobs": created,
        "queued": len(created),
        "skipped_unavailable": result["unavailable"],
        "skipped_too_long": too_long,
        "skipped_no_capacity": len(items) - len(created),
    }
