"""MIDI from a finished song's stems (app/pipeline/midi.py).

    GET    /api/transcribe/status
    POST   /api/jobs/{id}/midi                  {"stem", "detail"?}  -> 202
    POST   /api/jobs/{id}/midi/{stem}/cancel
    DELETE /api/jobs/{id}/midi/{stem}
    GET    /api/jobs/{id}/stems/{stem}.mid
    GET    /api/jobs/{id}/midi/{stem}/notes     the file's notes, for the piano roll,
                                                and the stem's level, for the sound

Its own router rather than more of jobs.py (2900 lines): the five routes, the
refusal table and the run task are one feature. Every refusal is
{"detail": <fixed sentence>, "code": <code>}, checked in this order: the job
(404 job_not_found), the body (400 stem_not_supported, bad_detail), whether
the stem's engine can run here at all (503 engine_unavailable: the one
Settings picks for the stem, transcribe_<stem>_engine), the setting (403
disabled), whether anything holds the song or the server's one MIDI slot (409
busy), the stem (409 no_stem), its length (413 too_long). docs/api.md has
the contract.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.api.jobs import _keep_saved_fields, _write_metadata_fields
from app.api.stems import _stem_download_name
from app.core.config import JOB_ID_RE, JOBS_DIR, MIDI_DETAILS, MIDI_STEMS, TRANSCRIBE_MAX_SECONDS
from app.core.models import FINISHED_STATUSES, Job, clean_midi
from app.core.registry import get as registry_get
from app.core.registry import metadata_lock
from app.core.registry import operation as registry_operation
from app.core.registry import persist as registry_persist
from app.core.settings import get_transcribe_detail, midi_stem_enabled
from app.core.stem_files import stem_path
from app.core.stems_location import is_relocating
from app.pipeline import jobqueue, midi_file, stem_level
from app.pipeline import midi as midi_run

logger = logging.getLogger("stemdeck.api.midi")

router = APIRouter()

_REFUSALS: dict[str, tuple[int, str]] = {
    "job_not_found": (404, "job not found"),
    "stem_not_supported": (400, "MIDI cannot be made from this stem"),
    "bad_detail": (400, "detail must be one of: " + ", ".join(MIDI_DETAILS)),
    "disabled": (403, "MIDI from this stem is turned off in Settings"),
    "busy": (
        409,
        "This song is busy, or StemDeck is making another MIDI file. "
        "Try again when it has finished.",
    ),
    "no_stem": (409, "this song has no finished stem to make MIDI from"),
    "too_long": (413, "this stem is too long to make MIDI from"),
    "engine_unavailable": (503, "MIDI cannot be made on this install"),
    "no_midi": (404, "this stem has no MIDI file"),
    "failed": (500, "could not remove the MIDI file"),
}


def _refusal(code: str) -> JSONResponse:
    status, detail = _REFUSALS[code]
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


class MidiBody(BaseModel):
    stem: str = Field(min_length=1, max_length=32)
    detail: str | None = Field(default=None, min_length=1, max_length=32)


def _job(job_id: str) -> tuple[Job | None, Path | None]:
    """The job ``job_id`` names and its folder, after the id checks, or
    (None, None) for anything that is not a known job."""
    if not JOB_ID_RE.match(job_id):
        return None, None
    job = registry_get(job_id)
    if job is None:
        return None, None
    root = JOBS_DIR.resolve()
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(root) or job_dir == root:
        return None, None
    return job, job_dir


def _song_busy(job: Job) -> bool:
    """Something writes into this song, or would move it: its separation,
    queued or running; a fill, split, re-extract or MIDI run on it; the
    server stopping; a library move. Or the server's one MIDI slot is taken."""
    return (
        job.status not in FINISHED_STATUSES
        or job.post_hoc_running()
        or midi_run.current() is not None
        or jobqueue.is_stopping()
        or is_relocating()
    )


def _stem_file(job: Job, job_dir: Path, stem: str) -> Path | None:
    """The finished stem MIDI is made from, inside JOBS_DIR, or None."""
    if job.status != "done":
        return None
    path = stem_path(job_dir / "stems", stem).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        return None
    return path


def _too_long(job: Job, audio: Path) -> bool:
    """Longer than TRANSCRIBE_MAX_SECONDS by the song's duration or by the
    stem file's own header, read without decoding it. A header that cannot
    be read is left to the worker, which refuses it as no_audio."""
    if isinstance(job.duration_sec, (int, float)) and job.duration_sec > TRANSCRIBE_MAX_SECONDS:
        return True
    import soundfile as sf

    try:
        info = sf.info(str(audio))
    except Exception:
        return False
    return bool(info.samplerate) and info.frames / info.samplerate > TRANSCRIBE_MAX_SECONDS


async def _persist(job: Job) -> None:
    """Keep the run's state: registry.json, and metadata.json's copy."""
    await asyncio.to_thread(registry_persist, JOBS_DIR)
    await asyncio.to_thread(_keep_saved_fields, job)


async def _midi_task(job: Job, job_dir: Path, run: midi_run.Run, audio: Path) -> None:
    try:
        with registry_operation("midi"):
            await asyncio.to_thread(midi_run.run_blocking, job, job_dir, run, audio)
    finally:
        await _persist(job)


def _task_done(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("MIDI task raised", exc_info=task.exception())


# Held so a running task is never garbage-collected before it ends.
_tasks: set[asyncio.Task] = set()


@router.get("/transcribe/status")
async def transcribe_status() -> dict:
    """Whether MIDI can be made here, with what, from which stems. Cheap: it
    never loads an engine (app/pipeline/midi.py availability); in the thread
    pool, because the device a run would use is probed with torch."""
    return await asyncio.to_thread(midi_run.status)


@router.post("/jobs/{job_id}/midi", status_code=202)
async def start_midi(job_id: str, body: MidiBody) -> JSONResponse:
    """Make MIDI from one of the song's stems. Answers at once with
    {"state": "running"}; the run's progress and outcome are in the song's
    state, midi.<stem>."""
    job, job_dir = _job(job_id)
    if job is None or job_dir is None:
        return _refusal("job_not_found")
    stem = body.stem
    if stem not in MIDI_STEMS:
        return _refusal("stem_not_supported")
    detail = body.detail if body.detail is not None else get_transcribe_detail()
    if detail not in MIDI_DETAILS:
        return _refusal("bad_detail")
    # Read per request: a change in Settings applies to the next run.
    engine = midi_run.engine_for(stem)
    if not midi_run.engine_availability(engine)[0]:
        return _refusal("engine_unavailable")
    if not midi_stem_enabled(stem):
        return _refusal("disabled")
    if _song_busy(job):
        return _refusal("busy")
    audio = _stem_file(job, job_dir, stem)
    if audio is None:
        return _refusal("no_stem")
    if await asyncio.to_thread(_too_long, job, audio):
        return _refusal("too_long")
    # Checked again after the await above, then claimed with no await in
    # between: nothing else on the event loop can take the slot meanwhile.
    if _song_busy(job):
        return _refusal("busy")
    has_file = midi_run.midi_path(job_dir, stem).is_file()
    run = midi_run.claim(job, stem, detail, has_file, engine)
    if run is None:
        return _refusal("busy")
    task = asyncio.create_task(_midi_task(job, job_dir, run, audio))
    run.task = task
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(_task_done)
    await _persist(job)
    return JSONResponse({"state": "running"}, status_code=202)


# How long a cancel waits for the run to record its end before answering.
_CANCEL_WAIT_SEC = 15.0


@router.post("/jobs/{job_id}/midi/{stem}/cancel")
async def cancel_midi(job_id: str, stem: str) -> JSONResponse:
    """Stop the run of ``stem``: the worker and every process it started are
    killed, and the file the song had, if any, stays. Answers once the run
    has recorded it, with {"state"}: the entry's state then, "cancelled"
    for a run this stopped, null when the stem never had a run."""
    job, _job_dir = _job(job_id)
    if job is None:
        return _refusal("job_not_found")
    if stem not in MIDI_STEMS:
        return _refusal("stem_not_supported")
    run = midi_run.cancel(job_id, stem)
    if run is not None and run.task is not None:
        await asyncio.wait({run.task}, timeout=_CANCEL_WAIT_SEC)
    entry = (clean_midi(job.midi) or {}).get(stem)
    return JSONResponse({"state": entry["state"] if entry else None})


@router.delete("/jobs/{job_id}/midi/{stem}")
async def delete_midi(job_id: str, stem: str) -> JSONResponse:
    """Remove the stem's MIDI file and what is known about it. 409 busy
    while a run of it is writing, 404 no_midi when there is nothing."""
    job, job_dir = _job(job_id)
    if job is None or job_dir is None:
        return _refusal("job_not_found")
    if stem not in MIDI_STEMS:
        return _refusal("stem_not_supported")
    if job.midi_running(stem):
        return _refusal("busy")
    try:
        removed = await asyncio.to_thread(midi_run.delete_output, job, job_dir, stem)
    except OSError:
        logger.exception("[%s] could not remove the %s MIDI file", job_id, stem)
        return _refusal("failed")
    if not removed:
        return _refusal("no_midi")
    await _persist(job)
    return JSONResponse({"deleted": stem})


@router.get("/jobs/{job_id}/stems/{stem}.mid", response_model=None)
async def get_midi(job_id: str, stem: str) -> FileResponse | JSONResponse:
    """The stem's MIDI file, named after the song like a stem download."""
    job, job_dir = _job(job_id)
    if job is None or job_dir is None:
        return _refusal("job_not_found")
    if stem not in MIDI_STEMS:
        return _refusal("no_midi")
    path = midi_run.midi_path(job_dir, stem).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        return _refusal("no_midi")
    return FileResponse(
        path,
        media_type="audio/midi",
        filename=_stem_download_name(job_id, stem, "mid"),
        # A run replaces the file in place under the same URL.
        headers={"Cache-Control": "no-cache"},
    )


def _level_of(job: Job, job_dir: Path, stem: str) -> tuple[float | None, dict | None]:
    """The RMS of the stem's file (app/pipeline/stem_level.py): the one kept
    for the file on disk now, else measured now, with its new record to keep.
    (None, None) without a finished stem file to measure. Blocking."""
    if job.status != "done":
        return None, None
    path = stem_path(job_dir / "stems", stem).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        return None, None
    kept = stem_level.kept_rms(job.stem_rms, stem, path)
    if kept is not None:
        return kept, None
    rec = stem_level.measure(path)
    return (rec["rms"], rec) if rec is not None else (None, None)


def _keep_stem_rms(job: Job) -> None:
    """metadata.json's copy of Job.stem_rms, read under the lock, so of two
    lanes measured at once the later write holds both."""
    with metadata_lock:
        _write_metadata_fields(job.id, {"stem_rms": job.stem_rms})


@router.get("/jobs/{job_id}/midi/{stem}/notes", response_model=None)
async def get_midi_notes(job_id: str, stem: str) -> JSONResponse:
    """The notes of the stem's MIDI file, read back from the file itself:
    {"notes": [[start s, end s, pitch, velocity], ...], "bpm", "tempo_source",
    "stem_rms"}. The studio draws them as a piano roll and plays them
    (laneMidiRows.js). "stem_rms" is the RMS of the stem's own file, which
    the MIDI sound is matched to on either audio engine; null when the stem
    cannot be read. A song separated before it was kept has it measured here
    the first time (one streamed pass of the stem) and kept. The same checks
    and refusals as the file's own route."""
    job, job_dir = _job(job_id)
    if job is None or job_dir is None:
        return _refusal("job_not_found")
    if stem not in MIDI_STEMS:
        return _refusal("no_midi")
    path = midi_run.midi_path(job_dir, stem).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        return _refusal("no_midi")
    try:
        read = await asyncio.to_thread(midi_file.read_notes, path)
    except (OSError, midi_file.MidiReadError):
        logger.exception("[%s] could not read the %s MIDI file", job_id, stem)
        return _refusal("no_midi")
    entry = (clean_midi(job.midi) or {}).get(stem) or {}
    rms, measured = await asyncio.to_thread(_level_of, job, job_dir, stem)
    if measured is not None:
        # Merged on the event loop, so two lanes measured at once both stay.
        job.stem_rms = {**stem_level.clean_stem_rms(job.stem_rms), stem: measured}
        await asyncio.to_thread(registry_persist, JOBS_DIR)
        await asyncio.to_thread(_keep_stem_rms, job)
    return JSONResponse(
        {
            "notes": read["notes"],
            "bpm": read["bpm"],
            "tempo_source": entry.get("tempo_source"),
            "stem_rms": rms,
        },
        # A run replaces the file in place under the same URL.
        headers={"Cache-Control": "no-cache"},
    )


def cancel_all() -> None:
    """The server is stopping (main.lifespan)."""
    midi_run.cancel_all()
