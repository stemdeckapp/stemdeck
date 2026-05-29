from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.core.config import JOB_ID_RE, JOBS_DIR, STEM_NAMES, TIMEOUT_FFMPEG, ffmpeg_executable
from app.core.registry import get as registry_get

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["stems"])

# Stem files served by this endpoint: the 6 demucs stems + two
# pipeline-produced extras. "original" is the re-encoded source song
# (added when the user picked a strict subset), "mix" is the ffmpeg
# amix of the user's selected stems.
_ALLOWED_NAMES = frozenset(STEM_NAMES) | {"original", "mix"}


def _validate_stem_path(job_id: str, name: str):
    """Shared guard: validate job_id, name, job state, and path. Returns resolved Path."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if name not in _ALLOWED_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / f"{name}.wav").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")
    return path


async def _stream_ffmpeg(cmd: list[str]):
    """Yield ffmpeg stdout in 64 KB chunks; kill process on client disconnect."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@router.get("/jobs/{job_id}/stems/peaks.json")
async def get_stem_peaks(job_id: str) -> Response:
    """Return pre-computed waveform peaks for all stems."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / "peaks.json").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="peaks not found")
    return FileResponse(
        path,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.api_route("/jobs/{job_id}/stems/{name}.wav", methods=["GET", "HEAD"], response_model=None)
async def get_stem(
    job_id: str,
    name: str,
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
) -> FileResponse | StreamingResponse:
    """Download a WAV stem. Optional ?start=&end= trims to a time region."""
    path = _validate_stem_path(job_id, name)

    if start is None and end is None:
        return FileResponse(path, media_type="audio/wav", filename=f"{name}.wav")

    if start is None or end is None or start >= end:
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-t",
        str(end - start),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]
    return StreamingResponse(
        _stream_ffmpeg(cmd),
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{name}_region.wav"'},
    )


@router.get("/jobs/{job_id}/stems/{name}.mp3")
async def get_stem_mp3(
    job_id: str,
    name: str,
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
) -> StreamingResponse:
    """Stream a stem as MP3 (VBR ~190 kbps). Optional ?start=&end= trims to a time region."""
    path = _validate_stem_path(job_id, name)

    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )

    pre_seek = ["-ss", str(start)] if start is not None else []
    post_seek = ["-t", str(end - start)] if start is not None else []

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        *pre_seek,
        "-i",
        str(path),
        *post_seek,
        "-q:a",
        "2",  # VBR ~190 kbps
        "-f",
        "mp3",
        "pipe:1",
    ]
    filename = f"{name}_region.mp3" if start is not None else f"{name}.mp3"
    return StreamingResponse(
        _stream_ffmpeg(cmd),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _safe_title(title: str | None) -> str:
    """Sanitize a song title into a filename-safe slug (matches the frontend)."""
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", title or "")
    safe = re.sub(r"_{2,}", "_", safe).strip("_")[:80].strip("_")
    return safe or "stems"


def _build_stems_zip(sources: list[tuple[str, Path]], fmt: str, dest: Path) -> None:
    """Blocking: write the stems into a ZIP. WAV files are stored as-is; MP3 is
    transcoded per stem via ffmpeg. ZIP_STORED throughout — audio doesn't
    meaningfully compress, and STORED keeps the build fast. Runs in a thread."""
    if fmt == "wav":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            for name, p in sources:
                zf.write(p, arcname=f"{name}.wav")
        return
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for name, p in sources:
            out = os.path.join(td, f"{name}.mp3")
            cmd = [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(p),
                "-q:a",
                "2",  # VBR ~190 kbps, matches the per-stem mp3 endpoint
                "-f",
                "mp3",
                out,
            ]
            proc = subprocess.run(  # noqa: S603 — list args, no shell, trusted ffmpeg
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT_FFMPEG,
            )
            if proc.returncode != 0:
                tail = proc.stderr[-2000:].decode("utf-8", "replace")
                raise RuntimeError(f"ffmpeg failed for {name}: {tail}")
            zf.write(out, arcname=f"{name}.mp3")


@router.get("/jobs/{job_id}/stems/all.zip")
async def get_all_stems_zip(
    job_id: str,
    fmt: str = Query(default="wav", alias="format"),
    stems: str | None = Query(default=None, description="Comma-separated stems; default all"),
) -> FileResponse:
    """Bundle the requested stems into a single ZIP, named after the song.

    `stems` is the active subset selected in the DAW (whitelisted). When omitted,
    every available stem is included."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if fmt not in ("wav", "mp3"):
        raise HTTPException(status_code=422, detail="format must be 'wav' or 'mp3'")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    # Resolve the requested subset (whitelisted) or fall back to all stems.
    if stems:
        requested = {s for s in stems.split(",") if s}
        if not requested <= set(STEM_NAMES):
            raise HTTPException(status_code=422, detail="unknown stem requested")
        wanted = [name for name in STEM_NAMES if name in requested]
    else:
        wanted = list(STEM_NAMES)

    jobs_root = JOBS_DIR.resolve()
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(jobs_root):
        raise HTTPException(status_code=404, detail="stems not found")

    sources: list[tuple[str, Path]] = []
    for name in wanted:
        p = (stems_dir / f"{name}.wav").resolve()
        if p.is_file() and p.is_relative_to(jobs_root):
            sources.append((name, p))
    if not sources:
        raise HTTPException(status_code=404, detail="no stems found")

    fd, tmp = tempfile.mkstemp(prefix="stemdeck_zip_", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        await asyncio.to_thread(_build_stems_zip, sources, fmt, tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        logger.exception("failed to build stems zip for job %s", job_id)
        raise HTTPException(status_code=500, detail="failed to build archive") from None

    filename = f"{_safe_title(job.title)}_stems.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )
