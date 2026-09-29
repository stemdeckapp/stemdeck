from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.core.config import (
    DISCOGS_LOOKUP_BUDGET_SEC,
    JOB_ID_RE,
    JOBS_DIR,
    LYRICS_LOOKUP_BUDGET_SEC,
    LYRICS_OFFSET_MAX_SEC,
    LYRICS_TIMED_MAX_CHARS,
    MAX_PENDING_UPLOAD_JOBS,
    MAX_PENDING_URL_JOBS,
    RETIME_DELETE_WAIT_SEC,
    STEM_NAMES,
    TIMEOUT_FETCH_TAGS,
    TIMEOUT_IDENTIFY_BACKFILL,
    TIMEOUT_LYRICS_LOOKUP,
    TIMEOUT_WORK_BACKFILL,
    ffprobe_executable,
)
from app.core.models import Job, JobCancelled, _set
from app.core.registry import all_jobs as registry_all_jobs
from app.core.registry import get as registry_get
from app.core.registry import get_proc as registry_get_proc
from app.core.registry import mark_deleted as registry_mark_deleted
from app.core.registry import pending_count as registry_pending_count
from app.core.registry import persist as registry_persist
from app.core.registry import register_if_capacity as registry_register_if_capacity
from app.core.registry import remove as registry_remove
from app.core.registry import set_trashed as registry_set_trashed
from app.core.settings import (
    get_acoustid_api_key,
    get_auto_sections,
    get_discogs_token,
    get_max_duration_sec,
)
from app.core.stems_location import is_relocating
from app.pipeline import discogs, jobqueue
from app.pipeline.artist_lookup import tagged_artist_name
from app.pipeline.audio_tags import probe_tags
from app.pipeline.collect import merge_stem_peaks, presence_for_split
from app.pipeline.download import InvalidYouTubeURL, fetch_audio_tags, validate_youtube_url
from app.pipeline.errors import classify_failure
from app.pipeline.identify import can_identify_title, identify_and_find_band
from app.pipeline.lyrics_align import detect_offset
from app.pipeline.lyrics_lookup import (
    _TEXT_MAX_CHARS,
    NotTheseLyrics,
    build_query,
    candidates_path,
    copy_lyrics,
    find_lyrics,
    keep_answer,
    lyrics_path,
    lyrics_settled,
    read_candidates,
    read_lyrics,
    saved_lyrics_belong,
    set_lyrics_offset,
    set_user_synced,
)
from app.pipeline.lyrics_retime import (
    RetimeFailed,
    RetimeRun,
    cancel_run,
    claim_run,
    current_run,
    forget_run,
    lyric_pairs,
    retime_job,
    wait_run_ended,
)
from app.pipeline.lyrics_retime import saved_view as saved_retime_view
from app.pipeline.runner import _pipeline_lock
from app.pipeline.vocal_split import split_vocals
from app.pipeline.work_lookup import find_work, might_have_work

router = APIRouter(tags=["jobs"])
logger = logging.getLogger("stemdeck.api")

_ALLOWED_EXTS = frozenset((".mp3", ".wav", ".flac", ".mp4", ".m4a", ".ogg", ".opus"))
_MAX_UPLOAD_BYTES = 400 * 1024 * 1024  # 400 MB
_WS_RE = re.compile(r"\s+")

# Now that imports queue instead of running immediately, a full queue is a
# capacity statement the user can act on, not a transient "try again".
_URL_QUEUE_FULL_DETAIL = (
    f"Queue is full ({MAX_PENDING_URL_JOBS} links waiting) - cancel a job or wait"
)
_UPLOAD_QUEUE_FULL_DETAIL = (
    f"Upload queue is full ({MAX_PENDING_UPLOAD_JOBS} waiting) - cancel a job or wait"
)


def _sanitize_title(filename: str) -> str:
    """Strip extension, normalize whitespace, cap at 120 chars."""
    stem = Path(filename).stem
    return _WS_RE.sub(" ", stem).strip()[:120]


def _probe_duration(path: Path) -> float:
    """Run ffprobe to get file duration in seconds."""
    result = subprocess.run(
        [
            ffprobe_executable(),
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        # See the note in pipeline/separate.py: text=True alone decodes with the
        # Windows locale encoding and a stray byte in ffprobe's output would
        # fail the upload outright.
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    try:
        return float(result.stdout.strip())
    except ValueError as e:
        raise RuntimeError(f"ffprobe returned non-numeric duration: {result.stdout!r}") from e


def _check_file_size(file_obj: object) -> int:
    """Seek to end, return size, rewind. Operates on the SpooledTemporaryFile
    backing a starlette UploadFile — synchronous, suitable for to_thread."""
    file_obj.seek(0, 2)  # type: ignore[union-attr]
    size = file_obj.tell()  # type: ignore[union-attr]
    file_obj.seek(0)  # type: ignore[union-attr]
    return size


def _copy_to_dest(src_file: object, dest: Path) -> None:
    """Copy SpooledTemporaryFile contents to dest. Synchronous, run in thread."""
    with dest.open("wb") as out:
        shutil.copyfileobj(src_file, out)  # type: ignore[arg-type]


def _rmtree_job(job_id: str) -> bool:
    """Remove a job's directory. False means files are still on disk.

    The outcome used to be swallowed, so delete_job dropped the registry entry
    whether or not anything was actually deleted -- and restore() then adopted
    the surviving directory on the next start, which is how deleted songs came
    back (#521).

    Retried once: on macOS the common failure is Finder or Spotlight creating
    a .DS_Store between rmtree's scan and its final rmdir, which leaves
    "Directory not empty" on a directory that is about to be empty again."""
    job_dir = JOBS_DIR / job_id
    for attempt in (1, 2):
        if not job_dir.is_dir():
            return True
        try:
            shutil.rmtree(job_dir)
            return True
        except Exception:
            logger.warning(
                "failed to remove job dir %s (attempt %d)", job_dir, attempt, exc_info=True
            )
    return not job_dir.is_dir()


def _job_files_missing(job: Job) -> bool:
    """True when a "done" job's stem files are gone from disk: the folder was
    deleted or moved outside the app, not just an in-flight relocation (#354),
    which is a known, temporary absence and must not flap the library."""
    if is_relocating():
        return False
    stems_dir = (JOBS_DIR / job.id / "stems").resolve()
    if not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        return True
    return not stems_dir.is_dir() or not any(stems_dir.iterdir())


def _job_state(job: Job) -> dict:
    """job.to_state() with "done" downgraded to "unavailable" when the stem
    files are missing from disk - ground truth for the client, replacing the
    old approach of the frontend guessing from a 404 or a disappearance from
    the job list, neither of which caught a job whose registry entry survived
    but whose stems folder did not."""
    if job.source_format is None and (job.source_url or "").startswith("local:"):
        # Uploads made before the format was recorded still have it on disk:
        # the kept source is source.<ext>, the extension it was validated
        # against. Kept on the job once found, so the disk is asked once per
        # job rather than on every state, and persisted with it next time.
        # Uploads finished before sources were kept have nothing to find and
        # stay None, which the client shows as the plain note.
        source = _retained_source(job.id)
        if source is not None:
            job.source_format = source.suffix.lower().removeprefix(".")
    state = job.to_state()
    if job.status == "done" and _job_files_missing(job):
        state["status"] = "unavailable"
    return state


class JobRequest(BaseModel):
    url: str
    # Subset of stems to include in the post-processing "selected mix"
    # audio file. None = all 6 (no extra mix produced; would equal the
    # original). Unknown stem names are dropped silently rather than
    # rejected, so a future model with extra stems doesn't break older
    # clients pinning the old set.
    stems: list[str] | None = None


@router.post("")
async def create_job(request: Request) -> dict[str, str]:
    """Submit a YouTube URL (JSON body) or upload an audio file (multipart/form-data)
    to start a stem-separation job. Returns the new job ID."""
    if is_relocating():
        # The stems folder just moved. This process still writes to the old one,
        # so anything accepted now would be orphaned by the restart.
        raise HTTPException(
            status_code=409,
            detail="Restart StemDeck to finish moving your stems folder before importing",
        )
    ct = request.headers.get("content-type", "")
    if "multipart/form-data" in ct:
        return await _create_local_job(request)
    return await _create_youtube_job(request)


async def _create_youtube_job(request: Request) -> dict[str, str]:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}") from e
    try:
        payload = JobRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        url = validate_youtube_url(payload.url)
    except InvalidYouTubeURL as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    selected = [s for s in payload.stems if s in STEM_NAMES] if payload.stems else list(STEM_NAMES)
    if not selected:
        selected = list(STEM_NAMES)

    job = Job(
        id=uuid.uuid4().hex[:12],
        selected_stems=selected,
        source_url=url,
        # Captured now, not when the sections stage is reached: that is the
        # last thing the pipeline does, and the toggle clears itself as soon
        # as the user opens another song.
        auto_sections=get_auto_sections(),
    )
    if not registry_register_if_capacity(job, MAX_PENDING_URL_JOBS):
        raise HTTPException(status_code=503, detail=_URL_QUEUE_FULL_DETAIL)
    jobqueue.enqueue(job.id)
    registry_persist(JOBS_DIR)
    return {"job_id": job.id}


async def _create_local_job(request: Request) -> dict[str, str]:
    # Fast pre-check: if already at capacity, reject before touching disk.
    # The real atomic check happens in register_if_capacity after the upload.
    # Only other uploads count here: a queue full of links costs no disk and
    # must not block a file import.
    if registry_pending_count(uploads=True) >= MAX_PENDING_UPLOAD_JOBS:
        raise HTTPException(status_code=503, detail=_UPLOAD_QUEUE_FULL_DETAIL)

    # Quick pre-check on Content-Length to fail fast for obviously oversized
    # uploads without buffering the whole body first.
    cl_header = request.headers.get("content-length")
    if cl_header:
        try:
            if int(cl_header) > _MAX_UPLOAD_BYTES + 4096:
                raise HTTPException(status_code=422, detail="File exceeds 400 MB limit")
        except ValueError:
            pass

    form = await request.form()
    upload = form.get("file")
    stems_raw = form.get("stems", "[]")

    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(status_code=422, detail="No file provided")

    filename: str = getattr(upload, "filename", "") or ""
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{ext}': accepted formats are .mp3, .wav, .flac, .mp4, .m4a, .ogg, and .opus",
        )

    # Validate stems list from form field
    try:
        stems_list = json.loads(stems_raw)
        if not isinstance(stems_list, list):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        stems_list = []
    selected = [s for s in stems_list if s in STEM_NAMES] or list(STEM_NAMES)

    # Check actual file size (SpooledTemporaryFile is already buffered at this
    # point; seek/tell are fast and don't re-read the body).
    file_obj = upload.file  # type: ignore[union-attr]
    file_size = await asyncio.to_thread(_check_file_size, file_obj)
    if file_size == 0:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if file_size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail="File exceeds 400 MB limit")

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    source_path = job_dir / f"source{ext}"

    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(_copy_to_dest, file_obj, source_path)

        # Duration check before registering the job so a violation leaves no
        # registered job and no leftover directory.
        try:
            duration = await asyncio.to_thread(_probe_duration, source_path)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not read file duration: {e}") from e

        max_duration = get_max_duration_sec()
        if duration > max_duration:
            raise HTTPException(
                status_code=422,
                detail=(f"File is {int(duration // 60)} min — limit is {max_duration // 60} min"),
            )
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    # Now, while the upload still exists: it is deleted once the pipeline is
    # done with it, and the stems carry no tags. Never fails the upload.
    audio_tags = await asyncio.to_thread(probe_tags, source_path)

    title = _sanitize_title(filename)
    local_source_url = f"local:{title}"
    job = Job(
        id=job_id,
        selected_stems=selected,
        title=title,
        duration_sec=duration,
        source_url=local_source_url,
        source_format=ext.removeprefix("."),
        audio_tags=audio_tags,
        auto_sections=get_auto_sections(),
    )
    if not registry_register_if_capacity(job, MAX_PENDING_UPLOAD_JOBS):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail=_UPLOAD_QUEUE_FULL_DETAIL)
    jobqueue.enqueue(job.id)
    registry_persist(JOBS_DIR)
    return {"job_id": job.id}


@router.get("")
def list_jobs(trashed: Literal["exclude", "include", "only"] = "exclude") -> list[dict]:
    """Completed jobs in the library, oldest first.

    Trashed jobs are left out by default, which is the whole point of the
    parameter: this endpoint is the phone UI's entire library, and before the
    Trash moved server-side it happily listed tracks the user had deleted on
    their desktop hours earlier.
    """
    jobs = sorted(registry_all_jobs().values(), key=lambda j: j.created_at)
    return [
        _job_state(job)
        for job in jobs
        if job.status == "done"
        and (trashed == "include" or (job.trashed_at is not None) == (trashed == "only"))
    ]


@router.post("/{job_id}/trash")
def trash_job(job_id: str) -> dict:
    """Put a job in the Trash. Reversible, and nothing on disk is touched."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_set_trashed(job_id, True)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    registry_persist(JOBS_DIR)
    return {"job_id": job.id, "trashed_at": job.trashed_at}


@router.post("/{job_id}/restore")
def restore_job(job_id: str) -> dict:
    """Take a job back out of the Trash."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_set_trashed(job_id, False)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    registry_persist(JOBS_DIR)
    return {"job_id": job.id, "trashed_at": job.trashed_at}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    """Get the current state of a job by ID."""
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_state(job)


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """Request cancellation of a running job. Idempotent for terminal jobs."""
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in ("done", "error", "cancelled"):
        # A vocal split only ever runs on a done job, so this early return made
        # it uncancellable by construction: the flag was never even set, while
        # the split held _pipeline_lock and stalled the whole import queue for
        # its full duration (#519). Terminating the worker is enough -- the
        # split's own error path marks it failed and releases the lock.
        if job.vocal_split == "running":
            job.cancel_requested = True
            proc = registry_get_proc(job_id)
            if proc is not None and proc.poll() is None:
                proc.terminate()
        # A line-by-line timing of its lyrics (POST .../lyrics/retime) stops
        # the same way: the run sees its own flag, never the job's, and
        # leaves the lyrics as they were.
        elif cancel_run(job_id):
            proc = registry_get_proc(job_id)
            if proc is not None and proc.poll() is None:
                proc.terminate()
        return job.to_state()
    job.cancel_requested = True

    # Still waiting: the worker will never pick it up, so finalise it here.
    # Previously a queued job only honoured cancel once its turn arrived, kept
    # occupying a capacity slot until then, and a queued upload held its source
    # file (up to 400 MB) for the whole wait.
    if jobqueue.discard(job_id):
        _set(job, status="cancelled", stage="Cancelled")
        jobqueue.cleanup_job_dir(job_id)
        registry_persist(JOBS_DIR)
        return job.to_state()

    # Only the running job owns the shared demucs worker. Terminating on any
    # other id would kill someone else's separation if a stale set_proc entry
    # ever survived -- cheap insurance now that many job ids are live at once.
    if job_id == jobqueue.running_id():
        proc = registry_get_proc(job_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()
    return job.to_state()


def _write_vocal_split_error(stems_dir: Path, cause: str, tail: list[str]) -> None:
    """Best-effort error record for the on-demand vocal split (#275). The job
    itself stays "done" -- this is diagnostic-only, not the quarantine path
    (which would delete the job's base stems)."""
    try:
        lines = [f"cause: {cause}", "", "--- stderr tail ---", *tail]
        (stems_dir / "vocal_split_error.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("could not write vocal_split_error.txt in %s", stems_dir, exc_info=True)


class ResplitBody(BaseModel):
    """Which stems the new run should mix down to. Same shape the importer
    takes, and bounded the same way: unknown names are dropped rather than
    trusted, and an empty result falls back to every stem."""

    stems: list[str] = Field(default_factory=list, max_length=len(STEM_NAMES))


def _retained_source(job_id: str) -> Path | None:
    """The upload kept beside a finished job, if there is one.

    Only uploads have this. A link's source is deleted once its stems exist,
    because it can be fetched again; an upload's cannot, so the runner keeps
    it (see cleanup_source). Jobs finished before that change kept nothing,
    which is why absence is an ordinary answer here rather than an error.
    """
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_dir() or not job_dir.is_relative_to(JOBS_DIR.resolve()):
        return None
    for candidate in sorted(job_dir.glob("source.*")):
        resolved = candidate.resolve()
        if (
            resolved.is_file()
            and resolved.suffix.lower() in _ALLOWED_EXTS
            and resolved.is_relative_to(JOBS_DIR.resolve())
        ):
            return resolved
    return None


def _resplit_source_url(source_url: str | None, new_id: str) -> str:
    """A source that names the same file without colliding with it.

    The library replaces any existing track that shares a sourceUrl -- that is
    how re-importing a link supersedes its old entry -- so a re-split reusing
    the original's source deleted the very track it came from. Worse than
    losing the row: the job directory stayed on disk with nothing referencing
    it, and syncWithServer re-adopted it on the next launch as a duplicate.

    Reusing the row is not an option either. Two jobs that share a source are
    collapsed again on every launch, so the pair has to differ on disk, not
    just in this session.

    The marker goes on the end. It once went before the last dot, to keep an
    extension that the client read the format from, but an upload's title has
    no extension (#690): the dot it found was in the title, so "Mr. Brightside"
    became "Mr (abc123). Brightside". The format is source_format now.
    """
    return f"{source_url or 'local:track'} ({new_id[:6]})"


def _link_or_copy(src: Path, dest: Path) -> None:
    """Hard-link the source into the new job, copying only if that fails.

    A re-split of a 300 MB upload should not cost another 300 MB. Both paths
    are inside JOBS_DIR so a link is normally available; os.link raises
    EXDEV across filesystems and EPERM on some mounts, and a copy is correct
    in every case, just larger.
    """
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


@router.post("/{job_id}/resplit")
async def resplit_job(job_id: str, body: ResplitBody) -> dict:
    """Separate a finished upload again, from the source kept beside it.

    A link-sourced track has never needed this: re-importing its URL does the
    same thing. An upload had no equivalent -- the file was handed over once,
    and the composer has nothing to submit for it -- so changing which stems
    you wanted meant finding the original file and uploading it a second time
    (#635).

    Produces a new job rather than mutating this one, which is exactly what
    re-importing a link already does. The old track keeps its stems, its
    sections and its beat grid until the person who asked for this decides
    otherwise.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not found")

    source = _retained_source(job_id)
    if source is None:
        # Not a failure of this request: the track simply has nothing to
        # re-separate from. The UI offers re-import for anything with a URL.
        raise HTTPException(status_code=409, detail="no source kept for this track")

    selected = [s for s in body.stems if s in STEM_NAMES] or list(STEM_NAMES)

    new_id = uuid.uuid4().hex[:12]
    new_dir = JOBS_DIR / new_id
    new_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(_link_or_copy, source, new_dir / f"source{source.suffix}")
    except OSError:
        shutil.rmtree(new_dir, ignore_errors=True)
        logger.exception("[%s] resplit could not stage the source", job_id)
        raise HTTPException(status_code=500, detail="could not start re-split") from None

    # The same recording, so the same lyrics, and LRCLIB's "nothing" too.
    has_lyrics = await asyncio.to_thread(copy_lyrics, _job_dir(job_id), new_dir)
    new_job = Job(
        id=new_id,
        selected_stems=selected,
        title=job.title,
        duration_sec=job.duration_sec,
        # Still a local: source, so this job keeps its own copy and can be
        # re-split in turn -- but distinct, so the library shows it beside the
        # track it came from instead of replacing it.
        source_url=_resplit_source_url(job.source_url, new_id),
        source_format=source.suffix.lower().removeprefix("."),
        # The same recording, so the same tags, identity and band.
        audio_tags=job.audio_tags,
        identity=job.identity,
        artist=job.artist,
        work=job.work,
        has_lyrics=has_lyrics,
        auto_sections=get_auto_sections(),
    )
    if not registry_register_if_capacity(new_job, MAX_PENDING_UPLOAD_JOBS):
        shutil.rmtree(new_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail=_UPLOAD_QUEUE_FULL_DETAIL)
    jobqueue.enqueue(new_job.id)
    registry_persist(JOBS_DIR)
    # source_url comes back because the client cannot derive it: it is
    # deliberately not the one it asked with, and the library keys its
    # dedup on exactly this string.
    return {"job_id": new_job.id, "source_url": new_job.source_url}


@router.post("/{job_id}/vocal-split")
async def start_vocal_split(job_id: str) -> Response:
    """Trigger the on-demand lead/backing vocal split (#275) for a completed
    job: a second model pass over the existing vocals.wav, producing
    lead_vocals.wav + backing_vocals.wav. Idempotent once done -- calling
    again returns 202 with the existing result rather than re-running the
    (expensive) model."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not found")
    if job.vocal_split == "running":
        raise HTTPException(status_code=409, detail="vocal split already running")
    if job.vocal_split == "done":
        return JSONResponse(_job_state(job), status_code=202)

    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")

    job.vocal_split = "running"
    _set(job, stage="Splitting lead/backing vocals...")
    try:
        async with _pipeline_lock:
            new_names = await asyncio.to_thread(split_vocals, job, stems_dir)
    except Exception as e:
        cause = classify_failure("\n".join([*(getattr(e, "tail", None) or []), str(e)]))
        logger.warning("[%s] vocal split failed: %s", job_id, e, exc_info=True)
        _write_vocal_split_error(stems_dir, cause, getattr(e, "tail", None) or [str(e)])
        job.vocal_split = "error"
        _set(job, stage="Done")
        registry_persist(JOBS_DIR)
        raise HTTPException(status_code=500, detail="vocal split failed") from e

    existing = {s["name"] for s in job.stems}
    for name in new_names:
        if name not in existing:
            job.stems.append({"name": name, "url": f"/api/jobs/{job_id}/stems/{name}.wav"})
    # "vocals" rides along so presence_for_split can recover the scale the base
    # stems were normalised against; without it the two new cards would have no
    # percentage to show.
    rms_values = merge_stem_peaks(stems_dir, ["vocals", *new_names])
    extra_presence = presence_for_split(rms_values, job.stem_presence)
    if extra_presence:
        job.stem_presence = {**(job.stem_presence or {}), **extra_presence}
    job.vocal_split = "done"
    _set(job, stage="Done")
    registry_persist(JOBS_DIR)
    return JSONResponse(_job_state(job))


_SECTION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SECTIONS_WRITE_LOCK = threading.Lock()


def _write_json_atomic(path: Path, data: dict) -> None:
    """Durably replace a JSON file without exposing a partial write."""
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class SectionItem(BaseModel):
    id: str
    name: str
    start: float
    end: float
    color: str
    kind: (
        Literal["intro", "outro", "break", "bridge", "inst", "solo", "verse", "chorus", "part"]
        | None
    ) = None
    # Pins a section against drag and resize in the timeline editor (#573).
    # It must be declared here to exist at all: this model takes Pydantic's
    # default extra="ignore", so an undeclared field sent by the editor is
    # dropped on save and answered 200, and the flag comes back false on the
    # next load with nothing anywhere saying why.
    locked: bool = False

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _SECTION_ID_RE.match(v):
            raise ValueError("invalid section id")
        return v

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return v.strip()[:64] or "Section"

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: str) -> str:
        if not _COLOR_RE.match(v):
            raise ValueError("invalid color")
        return v

    @field_validator("start", "end")
    @classmethod
    def _check_time(cls, v: float) -> float:
        if not (0 <= v < 86400):
            raise ValueError("time out of range")
        return round(v, 3)


# Upper bound on a section list. normalize_sections and the timeline editor
# both refuse a section shorter than 0.5 s, so the longest track StemDeck
# accepts (3600 s) cannot legitimately carry more than 7200 of them; 10000
# leaves headroom while refusing a payload sent to stall the event loop.
# Without a bound here a 33 MB body held every other request for ~4 seconds,
# and needed no valid job to do it: the body is parsed before the handler runs
# and answers 404 (#481).
_MAX_SECTIONS = 10000


class SectionsBody(BaseModel):
    sections: list[SectionItem] = Field(max_length=_MAX_SECTIONS)


@router.patch("/{job_id}/sections")
def update_sections(job_id: str, body: SectionsBody) -> dict:
    """Save named timeline sections (intro, verse, chorus, etc.) for a done job."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    validated = [s.model_dump(exclude_none=True) for s in body.sections]

    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    meta_path = job_dir / "metadata.json"

    with _SECTIONS_WRITE_LOCK:
        meta: dict = {}
        try:
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["sections"] = validated
            meta["sections_source"] = "manual"
            _write_json_atomic(meta_path, meta)
        except (OSError, json.JSONDecodeError) as exc:
            logger.exception("failed to write sections for %s: %s", job_id, exc)
            raise HTTPException(status_code=500, detail="failed to save sections") from exc

        _set(job, sections=validated, sections_source="manual")
        registry_persist(JOBS_DIR)

    return {"job_id": job_id, "sections": validated, "sections_source": "manual"}


# Jobs whose tags are being looked up right now. Only touched on the event
# loop, with no await between the check and the add, so a plain set is enough.
_TAG_LOOKUPS: set[str] = set()


def _lookup_audio_tags(job: Job) -> dict[str, str] | None:
    """Blocking: the tags a finished job's own source can still give, or None.

    An upload is asked through the source kept beside it, if one was. A link
    is asked through its metadata, and only once the stored URL has passed the
    same validator as an import: it came from the registry, not from this
    request, but nothing that fails that check is ever handed to yt-dlp.
    Raises when the fetch fails.
    """
    source_url = job.source_url or ""
    if source_url.startswith("local:"):
        source = _retained_source(job.id)
        return probe_tags(source) if source is not None else None
    try:
        url = validate_youtube_url(source_url)
    except InvalidYouTubeURL:
        return None
    return fetch_audio_tags(url)


def _write_metadata_fields(job_id: str, fields: dict[str, object]) -> None:
    """Put ``fields`` (the tags, the band) into metadata.json, if the job has
    one, and change nothing else in it: restore() rebuilds a lost registry
    entry from this file."""
    meta_path = (JOBS_DIR / job_id / "metadata.json").resolve()
    if not meta_path.is_relative_to(JOBS_DIR.resolve()):
        return
    # The same lock as the sections editor, the other read-modify-write here.
    with _SECTIONS_WRITE_LOCK:
        try:
            if not meta_path.is_file():
                return
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                return
            meta.update(fields)
            _write_json_atomic(meta_path, meta)
        except (OSError, ValueError):
            logger.warning("could not record audio tags in metadata for %s", job_id, exc_info=True)


@router.post("/{job_id}/audio-tags")
async def refresh_audio_tags(job_id: str) -> dict:
    """Fill in a finished track's tags, and the band they name (#699), for
    tracks imported before the pipeline found them.

    Answers {"audio_tags": {...} | null, "artist": {...} | null,
    "has_lyrics": bool}. A lookup that fails or times out is "nothing found",
    not an error: the page asks once per track and does not retry, and the
    reason is in the server log. The band is looked up when the tags name an
    artist and the job has no band yet, whether the tags were found just now
    or were there already, and the lyrics (lyrics.json, GET .../lyrics) when
    the job has none and anything is known to look them up by.

    Also "identity" (app/pipeline/identify.py), found here for a job that has
    none, the same way the pipeline finds it: an AcoustID fingerprint of the
    kept upload or of the stems summed, when the user set a key, else a
    MusicBrainz search by the tags, else by the title (and LRCLIB), else the
    tags. The band is then found
    through it. An added key, so older clients read the answer as before.

    And "work" (app/pipeline/work_lookup.py): the musical, film or series a
    soundtrack or cast recording is from, found here for a job that has none
    when its identity, album tag or title says it is one. Also an added key.

    Deliberately outside _pipeline_lock. This is one metadata request, and
    waiting behind a twenty-minute separation for it would leave the artist
    box empty for no reason.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "done":
        raise HTTPException(status_code=409, detail="job is not finished")
    job_dir = _job_dir(job_id)
    if (
        job.audio_tags
        and (job.artist or not tagged_artist_name(job.audio_tags.get("artist")))
        and lyrics_settled(job_dir)
        and not _wants_identity(job, job.audio_tags)
        and not _wants_work(job, job.identity, job.audio_tags)
    ):
        return {
            "audio_tags": job.audio_tags,
            "artist": job.artist,
            "has_lyrics": True,
            "identity": job.identity,
            "work": job.work,
        }
    if job_id in _TAG_LOOKUPS:
        raise HTTPException(status_code=409, detail="already looking up this track")

    _TAG_LOOKUPS.add(job_id)
    try:
        tags = job.audio_tags
        if not tags:
            try:
                # A timeout abandons the wait, not the thread: yt-dlp's own
                # socket timeout ends that, and its answer is then dropped.
                tags = await asyncio.wait_for(
                    asyncio.to_thread(_lookup_audio_tags, job), timeout=TIMEOUT_FETCH_TAGS
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] audio tag lookup timed out after %ds", job_id, TIMEOUT_FETCH_TAGS
                )
                tags = None
            except Exception:
                logger.warning("[%s] audio tag lookup failed", job_id, exc_info=True)
                tags = None
        artist = None
        identity = job.identity
        # The identity when the job has none and there is something to find
        # it by (a key to fingerprint with, or an artist tag), and the band
        # through it, only for a job with none and a name to find it by: a
        # job here for its lyrics alone may have tags naming no artist.
        want_identity = _wants_identity(job, tags)
        want_band = not job.artist and bool(
            tagged_artist_name((tags or {}).get("artist"))
            or (identity or {}).get("artist")
            or want_identity
        )
        if want_identity or want_band:
            audio = await asyncio.to_thread(_fingerprint_audio, job) if want_identity else []
            # identify_and_find_band never raises; the wait is bounded all the
            # same, since a socket timeout is per read, not per request.
            try:
                identity, artist = await asyncio.wait_for(
                    asyncio.to_thread(
                        identify_and_find_band,
                        job,
                        audio,
                        tags=tags,
                        want_band=want_band,
                        cancelled=lambda: registry_get(job_id) is not job,
                    ),
                    timeout=TIMEOUT_IDENTIFY_BACKFILL,
                )
            except asyncio.TimeoutError:
                logger.info("[%s] identification timed out", job_id)
        work = None
        # The work behind a soundtrack, once the identity says which recording
        # this is. find_work never raises; the wait is bounded as above.
        if _wants_work(job, identity, tags):
            try:
                work = await asyncio.wait_for(
                    asyncio.to_thread(
                        find_work,
                        identity,
                        tags,
                        title=job.title,
                        cancelled=lambda: registry_get(job_id) is not job,
                    ),
                    timeout=TIMEOUT_WORK_BACKFILL,
                )
            except asyncio.TimeoutError:
                logger.info("[%s] work lookup timed out", job_id)
        lyrics = None
        query = build_query(job, audio_tags=tags, band=artist, identity=identity)
        # Not asked again while LRCLIB's last "nothing" is recent.
        if query is not None and not lyrics_settled(job_dir):
            # find_lyrics never raises, and starts no request past its budget.
            try:
                lyrics = await asyncio.wait_for(
                    asyncio.to_thread(
                        find_lyrics,
                        query,
                        cancelled=lambda: registry_get(job_id) is not job,
                        fallback_title=job.title or "",
                    ),
                    timeout=LYRICS_LOOKUP_BUDGET_SEC + TIMEOUT_LYRICS_LOOKUP + 1,
                )
            except asyncio.TimeoutError:
                logger.info("[%s] lyrics lookup timed out", job_id)
    finally:
        _TAG_LOOKUPS.discard(job_id)

    # The track was deleted while the lookup ran.
    if registry_get(job_id) is not job:
        return {
            "audio_tags": None,
            "artist": None,
            "has_lyrics": False,
            "identity": None,
            "work": None,
        }
    if (
        lyrics
        and not lyrics_path(job_dir).is_file()
        and await asyncio.to_thread(keep_answer, job, job_dir, lyrics)
    ):
        registry_persist(JOBS_DIR)
    found: dict[str, object] = {}
    if tags and not job.audio_tags:
        found["audio_tags"] = tags
    if artist and not job.artist:
        found["artist"] = artist
    if identity and not job.identity:
        found["identity"] = identity
    if work and not job.work:
        found["work"] = work
    if found:
        _set(job, **found)
        registry_persist(JOBS_DIR)
        await asyncio.to_thread(_write_metadata_fields, job_id, found)
    return {
        "audio_tags": job.audio_tags,
        "artist": job.artist,
        "has_lyrics": lyrics_path(job_dir).is_file(),
        "identity": job.identity,
        "work": job.work,
    }


def _wants_identity(job: Job, tags: dict[str, str] | None) -> bool:
    """Whether the backfill should identify ``job``: it has no identity yet,
    and there is a key to fingerprint with, an artist tag to search by, or a
    title naming more than a song (a YouTube upload with no music metadata)."""
    if job.identity is not None:
        return False
    return (
        bool(get_acoustid_api_key())
        or bool(tagged_artist_name((tags or {}).get("artist")))
        or can_identify_title(tags, job.title)
    )


def _wants_work(job: Job, identity: dict[str, object] | None, tags: dict[str, str] | None) -> bool:
    """Whether the backfill should look for the work ``job``'s song is from:
    it has none yet, and its identity, album tag or title says it is from one."""
    return job.work is None and might_have_work(identity, tags, job.title)


def _fingerprint_audio(job: Job) -> list[Path]:
    """Blocking: what to fingerprint a finished job from. The upload kept
    beside it; else its stems, which add back up to the mix (a link's
    download is deleted once they exist). Only whitelisted stem names, each
    resolved inside the job's directory."""
    source = _retained_source(job.id)
    if source is not None:
        return [source]
    stems_root = (JOBS_DIR / job.id / "stems").resolve()
    stems = []
    for name in STEM_NAMES:
        path = (stems_root / f"{name}.wav").resolve()
        if path.is_file() and path.is_relative_to(JOBS_DIR.resolve()):
            stems.append(path)
    return stems


def _job_dir(job_id: str) -> Path:
    """A job's directory, for an id that has passed JOB_ID_RE. Refused (404)
    if it would resolve outside JOBS_DIR all the same."""
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    return job_dir


# One Discogs lookup per job at a time: a second request for the same job
# (the box opened twice, two tabs) waits for the first one's answer.
_DISCOGS_LOOKUPS: dict[str, asyncio.Task] = {}


@router.get("/{job_id}/artist-extra")
async def get_artist_extra(job_id: str) -> dict:
    """The Discogs profile of the band a track is by (app/pipeline/discogs.py),
    for the artist box, when Wikipedia has no article on it or leaves gaps.

    Answers {"id", "name", "real_name", "profile": [...], "members":
    {"current", "former"}, "groups", "links": [{"kind", "url"}], "releases":
    [{"year", "title"}], "url"}, or 404 when no Discogs token is set or no
    artist is confidently the track's. The token never leaves the server: the
    page asks here, and this asks Discogs.

    Outside _pipeline_lock, as the tag backfill is: a few metadata requests
    must not wait behind a separation. Bounded by DISCOGS_LOOKUP_BUDGET_SEC.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    token = get_discogs_token()
    if not token:
        raise HTTPException(status_code=404, detail="no artist details")

    task = _DISCOGS_LOOKUPS.get(job_id)
    if task is None:
        deadline = time.monotonic() + DISCOGS_LOOKUP_BUDGET_SEC

        def cancelled() -> bool:
            return registry_get(job_id) is not job or time.monotonic() > deadline

        task = asyncio.create_task(
            asyncio.to_thread(
                discogs.artist_for_track,
                job.identity,
                job.audio_tags,
                job.artist,
                token,
                cancelled=cancelled,
            )
        )
        _DISCOGS_LOOKUPS[job_id] = task
        task.add_done_callback(lambda _t: _DISCOGS_LOOKUPS.pop(job_id, None))
    try:
        # Shielded, so one caller giving up does not cancel another's answer.
        answer = await asyncio.wait_for(asyncio.shield(task), DISCOGS_LOOKUP_BUDGET_SEC + 1)
    except asyncio.TimeoutError:
        logger.info("[%s] Discogs lookup timed out", job_id)
        answer = None
    except Exception:
        logger.exception("[%s] Discogs lookup failed", job_id)
        answer = None
    if answer is None or registry_get(job_id) is not job:
        raise HTTPException(status_code=404, detail="no artist details")
    return answer


@router.get("/{job_id}/lyrics")
def get_lyrics(job_id: str) -> Response:
    """The track's lyrics, as found while it was separated (lyrics.json, see
    app/pipeline/lyrics_lookup.py), or 404 when it has none.

    A 404 may carry {"others": [...]}: the versions LRCLIB had when none was
    the length of the track (lyrics_candidates.json), for the tab to offer.
    A transcription with no others of its own is served with those.

    Served apart from the job's state, which carries only has_lyrics: the
    text runs to kilobytes, with every other version kept beside it, and the
    state is sent on every change to a running job.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if registry_get(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    job_dir = _job_dir(job_id)
    jobs_root = JOBS_DIR.resolve()
    path = lyrics_path(job_dir).resolve()
    found_path = candidates_path(job_dir).resolve()
    if not path.is_relative_to(jobs_root) or not found_path.is_relative_to(jobs_root):
        raise HTTPException(status_code=404, detail="no lyrics")
    entry = read_lyrics(job_dir) if path.is_file() else None
    if entry is None and path.is_file():
        logger.warning("unreadable lyrics for %s", job_id)
    found = read_candidates(job_dir) if found_path.is_file() else None
    others = found["others"] if found else []
    # Lyrics kept before a lookup held them to the track's own song and artist
    # can be another song's: checked by that rule each time they are asked for,
    # and dropped for good when they fail it.
    job = registry_get(job_id)
    query = build_query(job) if job is not None else None
    others = [o for o in others if saved_lyrics_belong(o, query)]
    if entry is not None and not saved_lyrics_belong(entry, query):
        logger.info("[%s] dropping kept lyrics of another song", job_id)
        path.unlink(missing_ok=True)
        if job is not None:
            _set(job, has_lyrics=False)
            registry_persist(JOBS_DIR)
        entry = None
    if entry is not None:
        entry["others"] = [o for o in entry["others"] if saved_lyrics_belong(o, query)]
    headers = {"Cache-Control": "no-cache"}
    if entry is not None:
        if not entry["others"]:
            entry["others"] = others
        return JSONResponse(entry, headers=headers)
    if others:
        return JSONResponse(
            {"detail": "no lyrics", "others": others}, status_code=404, headers=headers
        )
    raise HTTPException(status_code=404, detail="no lyrics")


class LyricsOffsetBody(BaseModel):
    """How many seconds later than their own timing the lyrics are shown:
    negative for earlier, 0 for their own timing again."""

    offset_sec: float = Field(
        ge=-LYRICS_OFFSET_MAX_SEC, le=LYRICS_OFFSET_MAX_SEC, allow_inf_nan=False
    )


class LyricsAlignBody(BaseModel):
    """Synced lyrics kept only in the browser (found by the tab's own
    LRCLIB lookup), to estimate against the track's vocals without saving
    anything. Omitted for the lyrics the server keeps (lyrics.json)."""

    synced: str | None = Field(default=None, max_length=_TEXT_MAX_CHARS)


def _lyrics_job(job_id: str) -> tuple[Job, Path]:
    """The job and its directory, for a lyrics edit: 404 for a malformed or
    unknown id, or one whose lyrics.json would resolve outside JOBS_DIR."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    job_dir = _job_dir(job_id)
    if not lyrics_path(job_dir).resolve().is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="no lyrics")
    return job, job_dir


def _synced_lyrics(job_dir: Path) -> dict:
    """Blocking: the job's lyrics.json when it has synced lyrics. 404 when it
    has none, 409 when they are text only and have no timing to move."""
    entry = read_lyrics(job_dir) if lyrics_path(job_dir).is_file() else None
    if entry is None:
        raise HTTPException(status_code=404, detail="no lyrics")
    if not entry["synced"]:
        raise HTTPException(status_code=409, detail="lyrics are not synced")
    return entry


# A lyrics edit's body: an offset, or at most _TEXT_MAX_CHARS of LRC, which
# as UTF-8 runs to four bytes a character.
_LYRICS_BODY_MAX_BYTES = 4 * _TEXT_MAX_CHARS + 1024


# A manual timing's body: LYRICS_TIMED_MAX_CHARS of LRC, as UTF-8.
_TIMED_BODY_MAX_BYTES = 4 * LYRICS_TIMED_MAX_CHARS + 1024


async def _lyrics_body(
    request: Request,
    model: type[BaseModel],
    empty_ok: bool = False,
    max_bytes: int = _LYRICS_BODY_MAX_BYTES,
):
    """A lyrics edit's JSON body as ``model``: None for no body at all when
    ``empty_ok``. 413 past _LYRICS_BODY_MAX_BYTES, else 422 for anything that
    is not the model, with NaN and Infinity refused while parsing (see
    _reject_non_finite) and nothing sent back of what was submitted."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise HTTPException(status_code=413, detail="body too large")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="body too large")
    if not raw.strip() and empty_ok:
        return None
    try:
        data = json.loads(raw, parse_constant=_reject_non_finite)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")
    try:
        return model(**data)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid lyrics edit") from exc


@router.post("/{job_id}/lyrics/offset")
async def set_lyrics_offset_route(job_id: str, request: Request) -> dict:
    """Keep the user's alignment of the track's synced lyrics (the Lyrics
    tab's Align panel): {"offset_sec": seconds later, negative for earlier,
    0 for their own timing}. Kept beside the synced text in lyrics.json, which
    is never rewritten, and applied by the page. Answers the offset kept."""
    job, job_dir = _lyrics_job(job_id)
    body = await _lyrics_body(request, LyricsOffsetBody)
    await asyncio.to_thread(_synced_lyrics, job_dir)
    entry = await asyncio.to_thread(set_lyrics_offset, job, job_dir, body.offset_sec)
    if entry is None:
        raise HTTPException(status_code=500, detail="could not save lyrics")
    return {"offset_sec": entry["offset_sec"]}


@router.post("/{job_id}/lyrics/align")
async def align_lyrics_route(job_id: str, request: Request) -> dict:
    """Estimate from the vocals stem how much later the lyrics are sung on
    this track than their stamps say, whatever timing they were saved with
    (the Align panel's Auto-detect): {"confident": bool, "offset_sec":
    seconds or null}.

    Without a body, for the lyrics the server keeps: a confident estimate is
    kept as their offset, as POST .../lyrics/offset would, and one that is
    not leaves them as they are. With {"synced": ...}, lyrics the browser
    keeps: only the estimate is answered, and nothing is saved.

    Outside _pipeline_lock, like GET .../vocal-envelope: at most one streamed
    read of one stem, and none when its envelope is already kept."""
    job, job_dir = _lyrics_job(job_id)
    body = await _lyrics_body(request, LyricsAlignBody, empty_ok=True)
    if job.status != "done":
        raise HTTPException(status_code=409, detail="job not ready")
    stems_dir = (job_dir / "stems").resolve()
    if not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    kept_here = body is None or body.synced is None
    if kept_here:
        synced = (await asyncio.to_thread(_synced_lyrics, job_dir))["synced"]
    else:
        synced = body.synced
    try:
        shift = await asyncio.to_thread(detect_offset, synced, stems_dir)
    except Exception:
        logger.exception("lyrics alignment failed for %s", job_id)
        raise HTTPException(status_code=500, detail="could not align lyrics") from None
    if shift is None or abs(shift) > LYRICS_OFFSET_MAX_SEC:
        return {"confident": False, "offset_sec": None}
    if kept_here:
        entry = await asyncio.to_thread(set_lyrics_offset, job, job_dir, shift)
        if entry is None:
            raise HTTPException(status_code=500, detail="could not save lyrics")
        shift = entry["offset_sec"]
    return {"confident": True, "offset_sec": shift}


# Line-by-line timing runs in flight, held so the loop does not drop them.
_retime_tasks: set[asyncio.Task] = set()


def _retime_ready(job_dir: Path, lyrics: dict | None) -> None:
    """Blocking: 404 without lyrics (kept, or given), 409 when they have no
    lines to time (an instrumental) or there is no vocals stem to time them
    to."""
    if lyrics is None:
        lyrics = read_lyrics(job_dir) if lyrics_path(job_dir).is_file() else None
        if lyrics is None:
            raise HTTPException(status_code=404, detail="no lyrics")
    if not lyric_pairs(lyrics):
        raise HTTPException(status_code=409, detail="lyrics have no lines")
    vocals = (job_dir / "stems" / "vocals.wav").resolve()
    if not vocals.is_relative_to(JOBS_DIR.resolve()) or not vocals.is_file():
        raise HTTPException(status_code=409, detail="no vocals stem")


async def _retime_task(job: Job, job_dir: Path, run: RetimeRun, lyrics: dict | None) -> None:
    """One on-demand run: behind _pipeline_lock, so it never shares the GPU
    with a separation, and stopped by a cancel or the job's deletion."""

    def cancelled() -> bool:
        return run.cancel.is_set() or registry_get(job.id) is not job

    def report(share: float) -> None:
        run.progress = share

    try:
        async with _pipeline_lock:
            run.started = True
            if cancelled():
                raise JobCancelled()
            outcome = await asyncio.to_thread(
                retime_job, job, job_dir, cancelled=cancelled, report=report, lyrics=lyrics
            )
        run.outcome = outcome
        run.state = outcome.state
    except JobCancelled:
        run.state = "idle"
    except RetimeFailed as exc:
        logger.info("[%s] lyrics not timed: %s", job.id, exc)
        run.state = "failed"
    except Exception:
        logger.exception("[%s] lyrics timing failed", job.id)
        run.state = "failed"
    finally:
        run.ended.set()


class LyricsRetimeBody(BaseModel):
    """Lyrics the browser keeps (found by the tab's own LRCLIB lookup), to
    time without saving anything. Omitted for the lyrics the server keeps."""

    synced: str | None = Field(default=None, max_length=_TEXT_MAX_CHARS)
    plain: str | None = Field(default=None, max_length=_TEXT_MAX_CHARS)


@router.post("/{job_id}/lyrics/retime")
async def start_lyrics_retime(job_id: str, request: Request) -> JSONResponse:
    """Time the track's lyrics line by line to its vocals
    (app/pipeline/lyrics_retime.py), in the background: 202 with the run's
    state, which GET .../lyrics/retime then follows.

    Without a body, the lyrics the server keeps: the result is kept in
    lyrics.json as "aligned" when it is good enough, and nothing changes
    when it is not ("unsure"). With {"synced", "plain"}, lyrics the browser
    keeps: the result comes back as "aligned" in GET's "done" answer and
    nothing is saved but the transcript.

    Runs behind the pipeline lock, after any import in progress, and takes a
    transcription of the vocals the first time (about 30 s on a GPU, a few
    minutes on a CPU; asked for here, it runs on either). One run at a time
    per track: 409 while one is running. POST .../cancel stops it."""
    job, job_dir = _lyrics_job(job_id)
    body = await _lyrics_body(request, LyricsRetimeBody, empty_ok=True)
    if job.status != "done":
        raise HTTPException(status_code=409, detail="job not ready")
    lyrics = None
    if body is not None and (body.synced or body.plain):
        lyrics = {"synced": body.synced or "", "plain": body.plain or ""}
    await asyncio.to_thread(_retime_ready, job_dir, lyrics)
    run = claim_run(job_id)
    if run is None:
        raise HTTPException(status_code=409, detail="lyrics timing already running")
    task = asyncio.create_task(_retime_task(job, job_dir, run, lyrics))
    _retime_tasks.add(task)
    task.add_done_callback(_retime_tasks.discard)
    return JSONResponse(run.view(), status_code=202)


@router.get("/{job_id}/lyrics/retime")
def get_lyrics_retime(job_id: str) -> dict:
    """Where the line-by-line timing of the track's lyrics stands:
    {"state": "idle" | "running" | "done" | "failed" | "unsure"}, with
    "progress" (0 to 1) while running and "matched" (the share of the
    lyrics heard), "lines_matched" and "lines" once there is a result; for
    lyrics the browser sent, "done" carries the timing itself, "aligned"."""
    job, job_dir = _lyrics_job(job_id)
    run = current_run(job_id)
    if run is not None and (
        run.state not in ("done", "idle")
        or (run.outcome is not None and run.outcome.aligned is not None)
    ):
        return run.view()
    entry = read_lyrics(job_dir) if lyrics_path(job_dir).is_file() else None
    saved = saved_retime_view(entry)
    if run is not None and run.state == "idle" and saved["state"] != "done":
        return run.view()
    return saved


class UserSyncedBody(BaseModel):
    """The user's own timing of the lyrics, from the sync editor: LRC whose
    lines are the lyrics' own, one to one."""

    synced: str = Field(min_length=1, max_length=LYRICS_TIMED_MAX_CHARS)


@router.put("/{job_id}/lyrics/user-synced")
async def put_user_synced(job_id: str, request: Request) -> dict:
    """Keep the user's own timing of the track's lyrics, shown before any
    other (see app/pipeline/lyrics_retime.py). 422 when it is not LRC, its
    stamps go back in time, or its lines are not exactly the lyrics' own:
    it can time them, never change them."""
    job, job_dir = _lyrics_job(job_id)
    body = await _lyrics_body(request, UserSyncedBody, max_bytes=_TIMED_BODY_MAX_BYTES)
    try:
        entry = await asyncio.to_thread(set_user_synced, job, job_dir, body.synced)
    except NotTheseLyrics as exc:
        raise HTTPException(status_code=422, detail="not a timing of these lyrics") from exc
    if entry is None:
        if not lyrics_path(job_dir).is_file():
            raise HTTPException(status_code=404, detail="no lyrics")
        raise HTTPException(status_code=500, detail="could not save lyrics")
    return {"user_synced": entry["user_synced"]}


@router.delete("/{job_id}/lyrics/user-synced")
async def delete_user_synced(job_id: str) -> dict:
    """Drop the user's own timing of the track's lyrics. Idempotent."""
    job, job_dir = _lyrics_job(job_id)
    if not lyrics_path(job_dir).is_file():
        raise HTTPException(status_code=404, detail="no lyrics")
    entry = await asyncio.to_thread(set_user_synced, job, job_dir, None)
    if entry is None:
        raise HTTPException(status_code=500, detail="could not save lyrics")
    return {"user_synced": None}


# Upper bound on an edited grid. A 20-minute track at 300 BPM is ~6000 beats;
# 20000 leaves generous headroom while refusing a payload crafted to exhaust
# memory or disk.
_MAX_EDITED_BEATS = 20000
_MAX_BARS = 2000


class BarMark(BaseModel):
    """A downbeat and the bar length that runs from it until the next mark."""

    beat: int = Field(ge=0, lt=_MAX_EDITED_BEATS)
    beats_per_bar: int = Field(ge=1, le=32)


class BeatsBody(BaseModel):
    beats: list[float] = Field(max_length=_MAX_EDITED_BEATS)
    bars: list[BarMark] = Field(default_factory=list, max_length=_MAX_BARS)

    @field_validator("beats")
    @classmethod
    def _check_beats(cls, v: list[float]) -> list[float]:
        # The client scheduler binary-searches this array and assumes it is
        # sorted and strictly increasing; enforce that at the boundary rather
        # than trusting the editor to have maintained it.
        out: list[float] = []
        for t in v:
            if not math.isfinite(t) or not (0 <= t < 86400):
                raise ValueError("beat time out of range")
            r = round(float(t), 6)
            if out and r <= out[-1]:
                raise ValueError("beat times must be strictly increasing")
            out.append(r)
        return out


def _beats_paths(job_id: str) -> tuple[Path, Path]:
    """(computed, user-edited) grid paths, both verified inside JOBS_DIR."""
    stems = (JOBS_DIR / job_id / "stems").resolve()
    if not stems.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    return stems / "beats.json", stems / "beats.user.json"


# Keys of error.txt that may be handed to the client. `title` and `source` are
# deliberately absent: this feeds a "report it on GitHub" flow whose issues are
# public, and the user adds what they were working on if they want to. The
# server is the right place to enforce that -- not the client that builds the
# report body.
_FAILURE_PUBLIC_KEYS = frozenset(
    ("time", "stage", "device", "model", "cause", "timings", "exception")
)


@router.get("/{job_id}/failure")
def get_failure(job_id: str) -> dict:
    """Return the quarantined failure evidence for a job that errored.

    _quarantine_failed_job writes jobs/failed/<id>/error.txt on every pipeline
    failure (#277) and until now nothing ever read it back: the UI had only the
    one-line `error_detail`, so a bug report could not carry the stderr tail or
    the full traceback that say *why* demucs died. Read-only, and never serves
    the whole file -- only the technical keys above, plus the tail and
    traceback (both already home-directory-redacted by the writer).
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")

    # JOB_ID_RE rejects "failed", so the quarantine dir can never be addressed
    # as a job id; join it explicitly and re-verify the result stays inside.
    failed_dir = (JOBS_DIR / "failed" / job_id).resolve()
    if not failed_dir.is_relative_to((JOBS_DIR / "failed").resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    path = failed_dir / "error.txt"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no failure evidence for this job")

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("unreadable failure evidence for %s", job_id)
        raise HTTPException(status_code=404, detail="no failure evidence for this job") from exc

    fields: dict[str, str] = {}
    tail: list[str] = []
    tb: list[str] = []
    section = "fields"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "--- stderr tail ---":
            section = "tail"
            continue
        if stripped == "--- traceback ---":
            # The writer separates sections with a blank line for readability
            # in the raw file; drop it here rather than let it show up as a
            # trailing empty entry in `tail`.
            if tail and tail[-1] == "":
                tail.pop()
            section = "traceback"
            continue
        if section == "tail":
            tail.append(line)
            continue
        if section == "traceback":
            tb.append(line)
            continue
        key, sep, value = line.partition(":")
        if sep and key in _FAILURE_PUBLIC_KEYS:
            fields[key] = value.strip()

    return {"job_id": job_id, **fields, "tail": tail, "traceback": tb}


@router.get("/{job_id}/beats")
def get_beats(job_id: str) -> Response:
    """Return the beat grid, preferring the user's edits over the detected one.

    Deliberately separate from the immutable `stems/beats.json`: that file is
    a computed artifact and is cached forever, while this response changes
    whenever the user edits and must never be cached.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    computed_path, user_path = _beats_paths(job_id)
    if not computed_path.is_file():
        raise HTTPException(status_code=404, detail="beat grid not found")
    try:
        grid = json.loads(computed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("unreadable beat grid for %s", job_id)
        raise HTTPException(status_code=404, detail="beat grid not found") from exc

    # User edits override the detected beats but keep the computed onsets,
    # which the editor still needs for snapping and which editing never changes.
    if user_path.is_file():
        try:
            edits = json.loads(user_path.read_text(encoding="utf-8"))
            if isinstance(edits.get("beats"), list) and edits["beats"]:
                grid["beats"] = edits["beats"]
                grid["bars"] = edits.get("bars") or []
                grid["edited"] = True
        except (OSError, json.JSONDecodeError):
            logger.warning("ignoring unreadable beat edits for %s", job_id)

    grid.setdefault("bars", [])
    grid.setdefault("edited", False)
    return JSONResponse(grid, headers={"Cache-Control": "no-store"})


def _reject_non_finite(_token: str) -> float:
    """`json.loads` parse_constant hook.

    Python's JSON parser accepts the non-standard `NaN`, `Infinity` and
    `-Infinity` literals. Letting them reach Pydantic produces a validation
    error whose `input` field holds the non-finite float, and FastAPI then
    cannot serialise its own 422 -- the request fails with a 500 and a
    traceback instead of a clean rejection. Refusing them at parse time keeps
    the error a plain string.
    """
    raise ValueError("non-finite numbers are not accepted")


async def _parse_beats_body(request: Request) -> BeatsBody:
    """Parse and validate a beats payload, rejecting non-finite floats first."""
    try:
        raw = await request.body()
        data = json.loads(raw, parse_constant=_reject_non_finite)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid JSON body") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="expected a JSON object")
    try:
        return BeatsBody(**data)
    except Exception as exc:
        # Message only -- never echo the submitted values back.
        raise HTTPException(status_code=422, detail="invalid beat grid") from exc


@router.patch("/{job_id}/beats")
async def update_beats(job_id: str, request: Request) -> dict:
    """Persist an edited beat grid.

    Written to a separate file from the detected grid so re-running analysis
    can never silently discard a user's corrections.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    body = await _parse_beats_body(request)
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    computed_path, user_path = _beats_paths(job_id)
    if not computed_path.is_file():
        raise HTTPException(status_code=404, detail="beat grid not found")

    payload = {
        "version": 1,
        "beats": body.beats,
        "bars": [b.model_dump() for b in body.bars],
    }
    try:
        tmp = user_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(user_path)
    except OSError as exc:
        logger.exception("failed to write beat edits for %s", job_id)
        raise HTTPException(status_code=500, detail="failed to save beat grid") from exc

    return {"job_id": job_id, "beats": len(payload["beats"]), "edited": True}


@router.delete("/{job_id}/beats")
def reset_beats(job_id: str) -> dict:
    """Discard user edits and fall back to the detected grid."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    _, user_path = _beats_paths(job_id)
    try:
        user_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.exception("failed to reset beat edits for %s", job_id)
        raise HTTPException(status_code=500, detail="failed to reset beat grid") from exc
    return {"job_id": job_id, "edited": False}


@router.delete("/{job_id}")
def delete_job(job_id: str) -> dict[str, str]:
    """Delete a completed or failed job and remove its stem files from disk."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("done", "error", "cancelled"):
        raise HTTPException(status_code=409, detail="job is still running")
    # A timing of its lyrics in flight stops rather than write into a
    # directory being removed (it also checks the job is still registered).
    if cancel_run(job_id) and job.vocal_split != "running":
        proc = registry_get_proc(job_id)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        # The worker may still have the vocals open while it stops.
        if not wait_run_ended(job_id, RETIME_DELETE_WAIT_SEC):
            logger.warning("[%s] lyrics timing still stopping; removing files anyway", job_id)
    forget_run(job_id)
    removed = _rmtree_job(job_id)
    # Recorded whether or not the files went away. The user asked for this job
    # to be gone; without the record, a directory that outlived the delete is
    # re-adopted by restore() on the next start and the track reappears.
    registry_mark_deleted(job_id)
    registry_remove(job_id)
    registry_persist(JOBS_DIR)
    if not removed:
        raise HTTPException(
            status_code=500,
            detail="Removed from the library, but its files could not be deleted.",
        )
    return {"job_id": job_id, "status": "deleted"}
