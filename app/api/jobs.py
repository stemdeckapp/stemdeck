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
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.core.config import (
    JOB_ID_RE,
    JOBS_DIR,
    MAX_PENDING_UPLOAD_JOBS,
    MAX_PENDING_URL_JOBS,
    STEM_NAMES,
    ffprobe_executable,
)
from app.core.models import Job, _set
from app.core.registry import all_jobs as registry_all_jobs
from app.core.registry import get as registry_get
from app.core.registry import get_proc as registry_get_proc
from app.core.registry import pending_count as registry_pending_count
from app.core.registry import persist as registry_persist
from app.core.registry import register_if_capacity as registry_register_if_capacity
from app.core.registry import remove as registry_remove
from app.core.settings import get_max_duration_sec
from app.core.stems_location import is_relocating
from app.pipeline import jobqueue
from app.pipeline.collect import merge_stem_peaks
from app.pipeline.download import InvalidYouTubeURL, validate_youtube_url
from app.pipeline.errors import classify_failure
from app.pipeline.runner import _pipeline_lock
from app.pipeline.vocal_split import split_vocals

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


def _rmtree_job(job_id: str) -> None:
    job_dir = JOBS_DIR / job_id
    if not job_dir.is_dir():
        return
    try:
        shutil.rmtree(job_dir)
    except Exception:
        logger.warning("failed to remove job dir %s", job_dir, exc_info=True)


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

    job = Job(id=uuid.uuid4().hex[:12], selected_stems=selected, source_url=url)
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

    title = _sanitize_title(filename)
    local_source_url = f"local:{title}"
    job = Job(
        id=job_id,
        selected_stems=selected,
        title=title,
        duration_sec=duration,
        source_url=local_source_url,
    )
    if not registry_register_if_capacity(job, MAX_PENDING_UPLOAD_JOBS):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=503, detail=_UPLOAD_QUEUE_FULL_DETAIL)
    jobqueue.enqueue(job.id)
    registry_persist(JOBS_DIR)
    return {"job_id": job.id}


@router.get("")
def list_jobs() -> list[dict]:
    """List all completed jobs in the library, sorted by creation time."""
    return [
        _job_state(job)
        for job in sorted(registry_all_jobs().values(), key=lambda j: j.created_at)
        if job.status == "done"
    ]


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
    merge_stem_peaks(stems_dir, new_names)
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


class SectionsBody(BaseModel):
    sections: list[SectionItem]


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
    _rmtree_job(job_id)
    registry_remove(job_id)
    registry_persist(JOBS_DIR)
    return {"job_id": job_id, "status": "deleted"}
