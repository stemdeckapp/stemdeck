from __future__ import annotations

import asyncio
import logging
import shutil
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import JOB_ID_RE, JOBS_DIR, MAX_PENDING_JOBS, STEM_NAMES
from app.core.models import Job
from app.core.registry import all_jobs as registry_all_jobs
from app.core.registry import get as registry_get
from app.core.registry import get_proc as registry_get_proc
from app.core.registry import persist as registry_persist
from app.core.registry import register as registry_register
from app.core.registry import remove as registry_remove
from app.pipeline import run_pipeline
from app.pipeline.download import InvalidYouTubeURL, validate_youtube_url

router = APIRouter(tags=["jobs"])
logger = logging.getLogger("stemdeck.api")


def _rmtree_job(job_id: str) -> None:
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        return
    try:
        shutil.rmtree(job_dir)
    except Exception:
        logger.warning("failed to remove job dir %s", job_dir, exc_info=True)


def _task_error_cb(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("pipeline task raised unhandled exception", exc_info=exc)


class JobRequest(BaseModel):
    url: str
    # Subset of stems to include in the post-processing "selected mix"
    # audio file. None = all 6 (no extra mix produced; would equal the
    # original). Unknown stem names are dropped silently rather than
    # rejected, so a future model with extra stems doesn't break older
    # clients pinning the old set.
    stems: list[str] | None = None


@router.post("")
async def create_job(payload: JobRequest) -> dict[str, str]:
    try:
        url = validate_youtube_url(payload.url)
    except InvalidYouTubeURL as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    pending = sum(1 for j in registry_all_jobs().values() if j.status == "queued")
    if pending >= MAX_PENDING_JOBS:
        raise HTTPException(status_code=503, detail="Server busy, please try again later")
    selected = [s for s in payload.stems if s in STEM_NAMES] if payload.stems else list(STEM_NAMES)
    if not selected:  # everything was unknown -- treat as full set
        selected = list(STEM_NAMES)
    job = registry_register(Job(id=uuid.uuid4().hex[:12], selected_stems=selected))
    task = asyncio.create_task(run_pipeline(job, url, JOBS_DIR))
    task.add_done_callback(_task_error_cb)
    return {"job_id": job.id}


@router.get("")
def list_jobs() -> list[dict]:
    return [
        job.to_state()
        for job in sorted(registry_all_jobs().values(), key=lambda j: j.created_at)
        if job.status == "done"
    ]


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_state()


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("done", "error", "cancelled"):
        # Already terminal -- return current state without touching anything.
        return job.to_state()
    job.cancel_requested = True
    # If Demucs is the current stage, terminate it immediately so the read
    # loop hits EOF and the runner translates that into a `cancelled` state.
    proc = registry_get_proc(job_id)
    if proc is not None and proc.poll() is None:
        proc.terminate()
    return job.to_state()


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("done", "error", "cancelled"):
        raise HTTPException(status_code=409, detail="job is still running")
    _rmtree_job(job_id)
    registry_remove(job_id)
    registry_persist(JOBS_DIR)
    return {"job_id": job_id, "status": "deleted"}
