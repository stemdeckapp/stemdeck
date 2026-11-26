"""The app's configuration, and Settings > Models.

    GET  /api/config
    GET  /api/models                         every built-in model, with "download"
    POST /api/models/{model_id}/download     202 {"state": "running"}
    POST /api/models/download/cancel         200 the download's state
    GET  /api/models/download                the download's state

The Download button (app/core/model_get.py has the rules): one download at
a time. A refusal is {"detail": <fixed sentence>, "code": <code>}, checked in
this order: the model (404 not_found, an id that is not a model with a
Download button here), whether it is there already (409 downloaded),
whether a download holds the slot or anything that may read the model's
files runs (409 busy).
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import model_get, model_inventory
from app.core.config import EXTRA_STEM_NAMES, STEM_NAMES

logger = logging.getLogger("stemdeck.api.models")

router = APIRouter()

_REFUSALS: dict[str, tuple[int, str]] = {
    "not_found": (404, "There is no model to download by that name."),
    "downloaded": (409, "This model is downloaded already."),
    "busy": (
        409,
        "StemDeck is downloading another model, separating a song or making MIDI. "
        "Try again when it has finished.",
    ),
}

# How long POST /cancel waits for the run to settle, so its answer already
# says "cancelled". The worker is reaped within about 15 s at worst.
_CANCEL_SETTLE_SEC = 20.0

_task: asyncio.Task | None = None


def _refusal(code: str) -> JSONResponse:
    status, detail = _REFUSALS[code]
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


@router.get("/config")
def get_config() -> dict:
    # extra_stem_names (#275) are produced only when a job's on-demand
    # lead/backing vocal split has run -- kept separate from stem_names so
    # existing clients that assume "every job produces exactly these stems"
    # are unaffected; new clients merge it into their lane vocab (see
    # syncStemNamesFromAPI in static/js/constants.js).
    return {"stem_names": list(STEM_NAMES), "extra_stem_names": list(EXTRA_STEM_NAMES)}


@router.get("/models")
def list_models() -> dict:
    """Settings, Models: every built-in model, its purpose, and its state and
    size on disk (app/core/model_inventory.py), with the Download button's
    run. No path in the answer. A plain def: FastAPI runs it in the thread
    pool, since it looks at files and may import torch to find torch.hub's
    folder."""
    return model_inventory.inventory()


@router.get("/models/download")
def get_download() -> dict:
    """The Download button's run, as the page polls it while one runs."""
    return model_get.snapshot()


def _pipeline_busy() -> bool:
    """Anything that may read a pipeline model's files: a separation running,
    one waiting in a queue that is not paused, the pipeline lock held (a
    split, a fill, a re-extract, a speed test), or the server stopping. Read
    on the event loop, as the queue and the lock are."""
    from app.api import speed_test
    from app.pipeline import jobqueue
    from app.pipeline.runner import _pipeline_lock

    running, waiting = jobqueue.snapshot()
    queued = bool(waiting) and not jobqueue.is_paused()
    return (
        running is not None
        or queued
        or _pipeline_lock.locked()
        or jobqueue.is_stopping()
        or speed_test.is_running()
    )


def _midi_busy() -> bool:
    """A MIDI run in progress: it may be reading an engine's weights, or
    fetching them itself."""
    from app.pipeline import midi as midi_run

    return midi_run.current() is not None


def _complete(plan: model_get.Plan) -> bool:
    """Whether every file of ``plan`` is there at its pinned size: by size
    only, as GET /api/models says it (the worker checks each SHA-256)."""
    from app.pipeline import ymt3

    if plan.convert_to is not None:
        return ymt3.model_state() == "downloaded"
    try:
        return all(f.dest.stat().st_size == f.size for f in plan.files)
    except OSError:
        return False


def _available(model_id: str) -> bool:
    entry = next(
        (m for m in model_inventory.inventory()["models"] if m.get("id") == model_id), None
    )
    return bool(entry and entry.get("downloadable"))


async def _run(plan: model_get.Plan, token: str, cancel, holds_lock: bool) -> None:
    from app.pipeline.runner import _pipeline_lock

    try:
        await asyncio.to_thread(model_get.run_blocking, plan, token, cancel)
    except Exception:
        logger.exception("models: the download of %s failed", plan.model_id)
    finally:
        if holds_lock:
            _pipeline_lock.release()
        snap = model_get.snapshot()
        if snap["state"] == "running":
            # run_blocking always settles the state; this is the backstop.
            model_get._update(state="failed", phase=None, error_code="failed")
            await asyncio.to_thread(model_get.remove_staging, plan, token)


def _task_done(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("models: the download task raised", exc_info=task.exception())


@router.post("/models/{model_id}/download", status_code=202)
async def start_download(model_id: str) -> JSONResponse:
    """Download one built-in model now, the way the first job that needs it
    would. Answers at once; GET /api/models/download has the progress."""
    global _task
    from app.pipeline.runner import _pipeline_lock

    if not model_get.MODEL_ID_RE.match(model_id):
        return _refusal("not_found")
    # Off the loop: the plan and the inventory look at files, and may import torch.
    plan = await asyncio.to_thread(model_get.plan_for, model_id)
    if plan is None or not await asyncio.to_thread(_available, model_id):
        return _refusal("not_found")
    if await asyncio.to_thread(_complete, plan):
        return _refusal("downloaded")
    # No await from here to the claim and the lock: nothing else on the event
    # loop can start a job, a MIDI run or another download in between, and an
    # unlocked semaphore is acquired without suspending.
    pipeline = plan.family == "pipeline"
    if model_get.is_running() or (_pipeline_busy() if pipeline else _midi_busy()):
        return _refusal("busy")
    cancel = model_get.claim(model_id, plan.total)
    if cancel is None:
        return _refusal("busy")
    if pipeline:
        await _pipeline_lock.acquire()
    token = model_get.new_token()
    _task = asyncio.create_task(_run(plan, token, cancel, pipeline))
    _task.add_done_callback(_task_done)
    logger.info("models: downloading %s", model_id)
    return JSONResponse({"state": "running"}, status_code=202)


@router.post("/models/download/cancel")
async def cancel_download() -> dict:
    """Stop the download: its process ends and what it left is removed.
    Answers with the state, "cancelled" once it has settled."""
    model_get.cancel_running()
    deadline = time.monotonic() + _CANCEL_SETTLE_SEC
    while time.monotonic() < deadline and model_get.is_running():
        await asyncio.sleep(0.05)
    return model_get.snapshot()


def cancel_all() -> None:
    """The server is stopping (main.lifespan)."""
    model_get.cancel_running()
