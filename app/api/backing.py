"""A song's backing track without one stem (Streamer Mode's "I play"), and
the rest of a song for any set of stems (a studio mode's Original).

    GET|HEAD /api/jobs/{id}/backing/{stem}.wav   the backing, built on first ask
    GET|HEAD /api/jobs/{id}/rest/{set}.wav       the sum of `set`, built on first ask

`set` is base stems joined by "-", sorted, each once (app/pipeline/backing.py
parse_rest_set): one set has one spelling, so one cached file.

`stem` is one of the six Demucs stems. The file is mixed from the stems on
disk the first time it is asked for and kept (app/pipeline/backing.py); later
requests are served from it, Range requests included (the audio engine reads
WAVs in windows). Errors say what is wrong, never where on disk.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import JOB_ID_RE, JOBS_DIR, TIMEOUT_BACKING
from app.core.registry import get as registry_get
from app.pipeline.backing import (
    REMOVABLE,
    BackingError,
    ensure_backing,
    ensure_rest,
    parse_rest_set,
)

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["jobs"])


@router.api_route("/{job_id}/backing/{stem}.wav", methods=["GET", "HEAD"], response_model=None)
async def get_backing(job_id: str, stem: str) -> FileResponse:
    # Validation order (security skill): the job id, then the stem against
    # the whitelist, then the resolved path inside the jobs folder, then I/O.
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if stem not in REMOVABLE:
        raise HTTPException(status_code=422, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    root = JOBS_DIR.resolve()
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(root) or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="job not found")
    try:
        path, built = await asyncio.wait_for(
            asyncio.to_thread(ensure_backing, job_id, job_dir, stem), timeout=TIMEOUT_BACKING
        )
    except BackingError:
        logger.warning("[%s] backing without %s not made", job_id, stem, exc_info=True)
        raise HTTPException(status_code=404, detail="no stems to make a backing from") from None
    except TimeoutError:
        logger.warning("[%s] backing without %s timed out", job_id, stem)
        raise HTTPException(status_code=500, detail="the backing took too long") from None
    except OSError:
        logger.warning("[%s] backing without %s failed", job_id, stem, exc_info=True)
        raise HTTPException(status_code=500, detail="the backing could not be made") from None
    if built:
        logger.info("[%s] backing without %s built", job_id, stem)
    if not path.resolve().is_relative_to(root):
        raise HTTPException(status_code=404, detail="job not found")
    return FileResponse(path, media_type="audio/wav")


@router.api_route("/{job_id}/rest/{stem_set}.wav", methods=["GET", "HEAD"], response_model=None)
async def get_rest(job_id: str, stem_set: str) -> FileResponse:
    """The sum of the stems in `stem_set`: a studio mode's Original when its
    lanes differ from the song's own selection. Same validation order and
    errors as the backing."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    try:
        names = parse_rest_set(stem_set)
    except ValueError:
        raise HTTPException(status_code=422, detail="unknown stem set") from None
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    root = JOBS_DIR.resolve()
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(root) or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="job not found")
    try:
        path, built = await asyncio.wait_for(
            asyncio.to_thread(ensure_rest, job_id, job_dir, names), timeout=TIMEOUT_BACKING
        )
    except BackingError:
        logger.warning("[%s] rest %s not made", job_id, stem_set, exc_info=True)
        raise HTTPException(status_code=404, detail="the stems for this set are missing") from None
    except TimeoutError:
        logger.warning("[%s] rest %s timed out", job_id, stem_set)
        raise HTTPException(status_code=500, detail="the mix took too long") from None
    except OSError:
        logger.warning("[%s] rest %s failed", job_id, stem_set, exc_info=True)
        raise HTTPException(status_code=500, detail="the mix could not be made") from None
    if built:
        logger.info("[%s] rest %s built", job_id, stem_set)
    if not path.resolve().is_relative_to(root):
        raise HTTPException(status_code=404, detail="job not found")
    return FileResponse(path, media_type="audio/wav")
