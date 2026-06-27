from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
import uuid
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

# Lanes the dynamic mixdown may sum: the 6 stems plus "original" (the complement
# track shown when the user picked a subset). "mix" is excluded -- it is the
# static pre-render this endpoint replaces. Gains are linear; the studio caps a
# lane at 2.0, so this generous bound just rejects abusive values.
_MIXDOWN_NAMES = frozenset(STEM_NAMES) | {"original"}
_MIXDOWN_MAX_GAIN = 4.0

# Output encoders by container/extension, shared by the dynamic mixdown and the
# stems zip. WAV is lossless PCM, FLAC is lossless compressed, MP3 is VBR ~190 kbps.
_ENCODE_ARGS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-q:a", "2"],
    "flac": ["-c:a", "flac"],
}
MIXDOWN_CODECS = {ext: [*args, "-f", ext] for ext, args in _ENCODE_ARGS.items()}
MIXDOWN_MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac"}


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


def _parse_lane_gains(stems: str, gains: str) -> tuple[list[str], list[float]]:
    """Parse and validate parallel comma-separated lane names and linear gains.
    Shared by the audio mixdown and the MP4 video mux. Raises HTTPException
    on malformed input, unknown lanes, or out-of-range gains."""
    names = [s for s in stems.split(",") if s]
    raw_gains = [g for g in gains.split(",") if g]
    if not names or len(names) != len(raw_gains):
        raise HTTPException(
            status_code=422, detail="stems and gains must be non-empty and equal length"
        )
    try:
        parsed_gains = [float(g) for g in raw_gains]
    except ValueError:
        raise HTTPException(status_code=422, detail="gains must be numbers") from None
    if any(g < 0 or g > _MIXDOWN_MAX_GAIN for g in parsed_gains):
        raise HTTPException(status_code=422, detail="gain out of range")
    if not set(names) <= _MIXDOWN_NAMES:
        raise HTTPException(status_code=422, detail="unknown stem requested")
    return names, parsed_gains


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


async def _ensure_cached_mp3(src: Path) -> Path:
    """Transcode `src` (a stem WAV) to a sibling `<name>.mp3`, cached on disk.
    Re-encoding a full song on every request is the slow part of loading a track
    on mobile (≈3s/stem × 6 in parallel); caching makes repeat loads instant.
    Written atomically (temp + rename) so concurrent fetches can't serve a
    partial file."""
    dest = src.with_suffix(".mp3")
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-q:a",
        "2",  # VBR ~190 kbps
        "-f",
        "mp3",
        str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_FFMPEG)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=504, detail="mp3 transcode timed out") from None
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        logger.warning(
            "mp3 transcode failed for %s: %s", src.name, (stderr or b"").decode("utf-8", "replace")
        )
        raise HTTPException(status_code=500, detail="mp3 transcode failed")
    os.replace(tmp, dest)
    return dest


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
) -> Response:
    """Stem as MP3 (VBR ~190 kbps). Full stems are cached to disk; ?start=&end=
    streams a freshly-trimmed region (uncached)."""
    path = _validate_stem_path(job_id, name)

    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )

    # Full-stem requests (no trim) are cached to disk so repeat loads — the
    # common case for the mobile player — are instant instead of re-encoding.
    if start is None:
        cached = await _ensure_cached_mp3(path)
        return FileResponse(
            cached,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f'attachment; filename="{name}.mp3"',
                # Stems are immutable once a job is done — let the phone cache
                # them so a re-load is instant and offline-friendly.
                "Cache-Control": "public, max-age=31536000, immutable",
            },
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


@router.get("/jobs/{job_id}/mixdown.{ext}", response_model=None)
async def get_mixdown(
    job_id: str,
    ext: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
) -> StreamingResponse:
    """Render a fresh mixdown of the given lanes at the given gains, streamed as
    WAV or MP3. Mirrors the studio mixer (per-stem volume, mute, solo) so the
    exported file matches what is heard. The master fader is intentionally not
    applied -- it is a monitoring level, not part of the mix. Optional ?start=&end=
    trims to a loop region."""
    if ext not in ("wav", "mp3", "flac"):
        raise HTTPException(status_code=404, detail="not found")

    names, parsed_gains = _parse_lane_gains(stems, gains)
    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )

    # Validates job_id (404), job done (404), and path traversal (404) per stem.
    paths = [_validate_stem_path(job_id, name) for name in names]

    pre_seek = ["-ss", str(start)] if start is not None else []
    post_seek = ["-t", str(end - start)] if start is not None else []

    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for p in paths:
        cmd += [*pre_seek, "-i", str(p)]
    # Apply each lane's gain, then sum with amix (normalize=0 keeps levels faithful,
    # matching collect.py). A single audible lane skips amix (a 1-input amix is a no-op).
    filters = [f"[{i}:a]volume={g:.6f}[a{i}]" for i, g in enumerate(parsed_gains)]
    n = len(paths)
    if n > 1:
        labels = "".join(f"[a{i}]" for i in range(n))
        filters.append(f"{labels}amix=inputs={n}:normalize=0[mix]")
        out_label = "[mix]"
    else:
        out_label = "[a0]"
    codec = MIXDOWN_CODECS[ext]
    cmd += ["-filter_complex", ";".join(filters), "-map", out_label, *post_seek, *codec, "pipe:1"]

    media_type = MIXDOWN_MEDIA_TYPES[ext]
    return StreamingResponse(
        _stream_ffmpeg(cmd),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="mixdown.{ext}"'},
    )


def _safe_title(title: str | None) -> str:
    """Sanitize a song title into a filename-safe slug (matches the frontend)."""
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", title or "")
    safe = re.sub(r"_{2,}", "_", safe).strip("_")[:80].strip("_")
    return safe or "stems"


@router.get("/jobs/{job_id}/video.mp4", response_model=None)
async def get_video_mixdown(
    job_id: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
) -> StreamingResponse:
    """Mux a fresh audio mixdown of the current mixer state with the job's preserved
    video into an MP4 (issue #219). Mirrors get_mixdown's audio graph (encoded
    as AAC) and stream-copies video.mp4 -- the silent video kept from an .mp4 upload
    or the real video stream downloaded for a YouTube job. 404 when the job has no
    video (SoundCloud / plain audio uploads).

    Streamed as fragmented MP4 (frag_keyframe+empty_moov) since the output pipe is
    not seekable -- +faststart would require a seekable file. The full song is
    exported; no region trim, to avoid A/V drift from stream-copy seeking."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    video_path = (JOBS_DIR / job_id / "video.mp4").resolve()
    if not video_path.is_file() or not video_path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="no video track for this job")

    names, parsed_gains = _parse_lane_gains(stems, gains)
    # Validates job_id (404), job done (404), and path traversal (404) per stem.
    paths = [_validate_stem_path(job_id, name) for name in names]

    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for p in paths:
        cmd += ["-i", str(p)]
    cmd += ["-i", str(video_path)]
    video_idx = len(paths)
    # Per-lane gain then amix (normalize=0 keeps levels faithful). A single audible
    # lane skips amix (a 1-input amix is a no-op), matching get_mixdown.
    filters = [f"[{i}:a]volume={g:.6f}[a{i}]" for i, g in enumerate(parsed_gains)]
    n = len(paths)
    if n > 1:
        labels = "".join(f"[a{i}]" for i in range(n))
        filters.append(f"{labels}amix=inputs={n}:normalize=0[mix]")
        out_label = "[mix]"
    else:
        out_label = "[a0]"
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        out_label,
        "-map",
        f"{video_idx}:v",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
    ]

    filename = f"{_safe_title(job.title)}_video.mp4"
    return StreamingResponse(
        _stream_ffmpeg(cmd),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_stems_zip(sources: list[tuple[str, Path]], fmt: str, dest: Path) -> None:
    """Blocking: write the stems into a ZIP. WAV files are stored as-is; MP3 and
    FLAC are transcoded per stem via ffmpeg. ZIP_STORED throughout - audio doesn't
    meaningfully compress, and STORED keeps the build fast. Runs in a thread."""
    if fmt == "wav":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            for name, p in sources:
                zf.write(p, arcname=f"{name}.wav")
        return
    encode = _ENCODE_ARGS[fmt]
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for name, p in sources:
            out = os.path.join(td, f"{name}.{fmt}")
            cmd = [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(p),
                *encode,
                "-f",
                fmt,
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
            zf.write(out, arcname=f"{name}.{fmt}")


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
    if fmt not in ("wav", "mp3", "flac"):
        raise HTTPException(status_code=422, detail="format must be 'wav', 'mp3', or 'flac'")
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
