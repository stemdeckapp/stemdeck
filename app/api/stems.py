from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from collections import deque
from pathlib import Path
from typing import NamedTuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.core.config import (
    CACHE_DIR,
    EXTRA_STEM_NAMES,
    JOB_ID_RE,
    JOBS_DIR,
    STEM_NAMES,
    TIMEOUT_FFMPEG,
    ffmpeg_executable,
)
from app.core.registry import get as registry_get
from app.core.settings import get_export_sample_rate
from app.pipeline.click_render import cache_key as click_cache_key
from app.pipeline.click_render import count_in_beats, render_click_wav, render_count_in_wav

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["stems"])

# Stem files served by this endpoint: the 6 demucs stems + two
# pipeline-produced extras, plus the on-demand lead/backing vocal split
# (#275, EXTRA_STEM_NAMES) when a job has requested it. "original" is the
# re-encoded source song (added when the user picked a strict subset), "mix"
# is the ffmpeg amix of the user's selected stems.
_ALLOWED_NAMES = frozenset(STEM_NAMES) | frozenset(EXTRA_STEM_NAMES) | {"original", "mix"}

# Lanes the dynamic mixdown may sum: the 6 stems plus "original" (the complement
# track shown when the user picked a subset) plus lead/backing vocals when a
# job has split them. "mix" is excluded -- it is the static pre-render this
# endpoint replaces. Gains are linear; the studio caps a lane at 2.0, so this
# generous bound just rejects abusive values.
_MIXDOWN_NAMES = frozenset(STEM_NAMES) | frozenset(EXTRA_STEM_NAMES) | {"original"}
_MIXDOWN_MAX_GAIN = 4.0
# lead_vocals/backing_vocals are a decomposition of vocals, not an independent
# signal -- summing vocals alongside either would double-count the vocal
# energy in the mix (#275).
_VOCAL_DECOMPOSITION_NAMES = frozenset(EXTRA_STEM_NAMES)

# Output encoders by container/extension, shared by the dynamic mixdown and the
# stems zip. WAV is lossless PCM, FLAC is lossless compressed, MP3 is VBR ~190 kbps,
# OGG is Vorbis VBR q6 (~192 kbps) — the quality tier matching the MP3 setting.
_ENCODE_ARGS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-q:a", "2"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
}
MIXDOWN_CODECS = {ext: [*args, "-f", ext] for ext, args in _ENCODE_ARGS.items()}

# Containers whose muxer finishes the file by seeking back to a header it wrote
# earlier, which is impossible on a pipe (#458):
#
#   wav   RIFF and data chunk sizes, left as the 0xFFFFFFFF placeholder, so the
#         file claims ~4 GB of audio and strict hardware refuses it
#   flac  STREAMINFO total samples and the MD5 signature, left as zeros
#   mp3   the Xing/Info frame, which ffmpeg simply omits rather than writing a
#         wrong one -- and these are VBR (-q:a 2), so without it there is no
#         duration and no seek table
#
# ogg is absent on purpose: a granule position rides on every page, so nothing
# is patched afterwards. mp4 is handled separately by get_video_mixdown, which
# already muxes fragmented (frag_keyframe+empty_moov) for exactly this reason.
_SEEKABLE_OUTPUT_EXTS = frozenset({"wav", "flac", "mp3"})
MIXDOWN_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}

# Mixdown render cache (#290): identical render params re-run the full ffmpeg
# graph on every request today. On a shared server, repeat downloads of the
# same mix (a common case -- re-downloading, or several listeners pulling the
# same export) burn CPU for no reason since the output is a pure function of
# the inputs. Bounded so a churny cache (many one-off region trims) can't
# grow unboundedly on a long-running server.
_MIXDOWN_CACHE_DIR = CACHE_DIR / "mixdown"
_CLICK_CACHE_DIR = CACHE_DIR / "click"
_MIXDOWN_CACHE_MAX_FILES = 20
_MIXDOWN_CACHE_MAX_BYTES = 500 * 1024 * 1024  # 500 MB


# Folded into every cache key. Bump it whenever a render's *output* changes for
# inputs that are otherwise identical, so entries written by an older build can
# never be served by a newer one.
#
# "2": everything cached before #458 was streamed through a pipe and carries an
# unpatched container header. Those files are wrong, the key that produced them
# is still reachable, and _prune_mixdown_cache evicts by age rather than
# validity -- so without this a user who exported before upgrading would be
# handed the same broken file back for the same parameters indefinitely.
_RENDER_CACHE_VERSION = "2"


def _mixdown_cache_key(
    job_id: str,
    ext: str,
    names: list[str],
    gains: list[float],
    start: float | None,
    end: float | None,
    click_lane: tuple[Path, float] | None = None,
) -> str:
    """Every render input is in the key, so a different mixer state, region,
    or a Settings change (export sample rate) misses cleanly -- never a stale
    hit. Stems are immutable once a job reaches "done", so job_id alone pins
    the underlying audio; gains are formatted to match the %.6f precision
    already used in the ffmpeg volume filter, so cosmetically-different but
    ffmpeg-equivalent gain strings (e.g. "1" vs "1.0") share a cache entry."""
    raw = "|".join(
        [
            _RENDER_CACHE_VERSION,
            job_id,
            ext,
            ",".join(names),
            ",".join(f"{g:.6f}" for g in gains),
            "" if start is None else f"{start:.6f}",
            "" if end is None else f"{end:.6f}",
            str(get_export_sample_rate()),
            # The click's own cache key already encodes the grid, rate and
            # accent mode, so its filename plus gain fully identifies it.
            "" if click_lane is None else f"{click_lane[0].name}:{click_lane[1]:.6f}",
        ]
    )
    # Cache key, not a security context -- usedforsecurity=False documents
    # that for both readers and static analysis (bandit flags bare sha1()).
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _prune_mixdown_cache(cache_dir: Path) -> None:
    """Evict oldest-first once either the file-count or total-size budget is
    exceeded. Best-effort: a failed prune just means the cache grows past
    budget until the next successful render, not a broken export."""
    try:
        entries = sorted(
            (p for p in cache_dir.iterdir() if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in entries)
    except OSError:
        return
    while entries and (len(entries) > _MIXDOWN_CACHE_MAX_FILES or total > _MIXDOWN_CACHE_MAX_BYTES):
        oldest = entries.pop(0)
        try:
            total -= oldest.stat().st_size
            oldest.unlink()
        except OSError:
            logger.debug("mixdown cache prune: could not remove %s", oldest, exc_info=True)


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


class _ClickLane(NamedTuple):
    """The extra click/count-in audio input handed to the ffmpeg graph.

    `lead_in` is the seconds of count-in baked into the front of the WAV (0 for
    a plain click). `count_in` marks the file as living in *output* coordinates
    -- already region-trimmed and lead-in-prefixed -- so the graph must not
    `-ss` it and must delay the stems by `lead_in` to match.
    """

    path: Path
    gain: float
    lead_in: float
    count_in: bool


def _click_lane(
    job_id: str,
    enabled: bool,
    multiplier: float,
    accent_mode: int,
    gain: float,
    count_in_bars: int = 0,
    start: float | None = None,
    end: float | None = None,
) -> _ClickLane | None:
    """Render (or reuse) the click / count-in track for this job as an extra
    ffmpeg input, or None when both are off or the job has no beat grid.

    The click is synthesised in the browser during playback and never reaches
    the server, so an export can only include it by rendering an equivalent WAV
    here. A plain click spans the whole track and is lined up by the region trim
    (`-ss` before every input); a count-in is baked in output coordinates
    instead (see render_count_in_wav) and the caller delays the stems to match.
    """
    if not enabled and count_in_bars <= 0:
        return None
    grid = _read_beat_grid(job_id)
    if grid is None:
        return None
    beats = grid.get("beats") or []
    if not beats:
        return None
    bars = grid.get("bars") or []
    duration = float(grid.get("duration") or 0.0)
    sample_rate = get_export_sample_rate()
    g = max(0.0, min(4.0, gain))

    key = click_cache_key(
        job_id,
        beats,
        bars,
        duration,
        sample_rate,
        multiplier,
        accent_mode,
        count_in_bars=count_in_bars,
        include_click=enabled,
        start=start,
        end=end,
    )
    path = _CLICK_CACHE_DIR / f"{key}.wav"

    if count_in_bars > 0:
        # lead_in is a pure function of the grid; recompute it even on a cache
        # hit so the caller can delay the stems without re-reading the WAV.
        lead_in, _ = count_in_beats(
            beats, bars, count_in_bars, multiplier, accent_mode, start=start or 0.0
        )
        if not path.is_file():
            try:
                rendered = render_count_in_wav(
                    path,
                    beats,
                    bars,
                    duration,
                    sample_rate=sample_rate,
                    multiplier=multiplier,
                    accent_mode=accent_mode,
                    count_in_bars=count_in_bars,
                    include_click=enabled,
                    start=start or 0.0,
                    end=end,
                )
            except Exception:
                logger.exception("count-in render failed for %s", job_id)
                return None
            if rendered is None:
                return None
        _prune_mixdown_cache(_CLICK_CACHE_DIR)
        return _ClickLane(path, g, lead_in, True)

    if not path.is_file():
        try:
            rendered = render_click_wav(
                path,
                beats,
                bars,
                duration,
                sample_rate=sample_rate,
                multiplier=multiplier,
                accent_mode=accent_mode,
            )
        except Exception:
            logger.exception("click render failed for %s", job_id)
            return None
        if rendered is None:
            return None
    _prune_mixdown_cache(_CLICK_CACHE_DIR)
    return _ClickLane(path, g, 0.0, False)


def _read_beat_grid(job_id: str) -> dict | None:
    """The grid an export should click to: the user's edits when present, the
    detected grid otherwise. Mirrors GET /api/jobs/{id}/beats."""
    stems = (JOBS_DIR / job_id / "stems").resolve()
    if not stems.is_relative_to(JOBS_DIR.resolve()):
        return None
    computed = stems / "beats.json"
    if not computed.is_file():
        return None
    try:
        grid = json.loads(computed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    user = stems / "beats.user.json"
    if user.is_file():
        try:
            edits = json.loads(user.read_text(encoding="utf-8"))
            if isinstance(edits.get("beats"), list) and edits["beats"]:
                grid["beats"] = edits["beats"]
                grid["bars"] = edits.get("bars") or []
        except (OSError, json.JSONDecodeError):
            # Fall back to the detected grid rather than failing the export,
            # but say so: silently ignoring this is how a user ends up asking
            # why their grid edits disappeared.
            logger.warning("ignoring unreadable beat edits for %s", job_id)
    return grid


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
    if "vocals" in names and _VOCAL_DECOMPOSITION_NAMES & set(names):
        raise HTTPException(
            status_code=422,
            detail="vocals cannot be combined with lead_vocals/backing_vocals (same signal)",
        )
    return names, parsed_gains


async def _drain_stderr(stream: asyncio.StreamReader, sink: deque[str]) -> None:
    """Collect ffmpeg stderr lines into a bounded deque. Draining is mandatory
    once stderr is a pipe -- an undrained full pipe would deadlock ffmpeg."""
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace").rstrip())


async def _stream_ffmpeg(cmd: list[str], context: str = "", cache_path: Path | None = None):
    """Yield ffmpeg stdout in 64 KB chunks; kill process on client disconnect.

    stderr is captured (bounded tail) and logged at WARNING when ffmpeg exits
    non-zero (#280): the HTTP status is already committed mid-stream, so a
    failed render reaches the client as a truncated file -- the log entry is
    the only place the failure can surface. Kills we initiated (client
    disconnect) are expected and not logged as failures.

    When `cache_path` is given, every chunk is also teed to a per-request
    temp file alongside it (#290). A clean finish atomically renames the temp
    file into place as the cache entry and prunes the cache to its budget;
    any failure or client disconnect removes the temp file instead -- a
    render the client didn't get in full never becomes a cache hit for the
    next request."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: deque[str] = deque(maxlen=30)
    drain_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_tail))
    # Whether stdout reached EOF. proc.returncode stays None until wait()
    # reaps the child even after it exited, so EOF -- not returncode -- is
    # what distinguishes "ffmpeg finished on its own" from "client
    # disconnected mid-stream and we killed it".
    finished = False

    tmp_path: Path | None = None
    tmp_file = None
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_file = open(tmp_path, "wb")  # noqa: SIM115 -- closed in finally, not a context manager here
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                finished = True
                break
            if tmp_file is not None:
                tmp_file.write(chunk)
            yield chunk
    finally:
        if tmp_file is not None:
            tmp_file.close()
        if not finished and proc.returncode is None:
            proc.kill()
        await proc.wait()
        try:
            await asyncio.wait_for(drain_task, timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            drain_task.cancel()
        if finished and proc.returncode != 0:
            logger.warning(
                "stream ffmpeg exit %s [%s]: %s",
                proc.returncode,
                context,
                " | ".join(list(stderr_tail)[-8:]) or "(no stderr)",
            )
        if tmp_path is not None:
            if finished and proc.returncode == 0:
                os.replace(tmp_path, cache_path)
                _prune_mixdown_cache(cache_path.parent)
            else:
                tmp_path.unlink(missing_ok=True)


async def _render_to_file(
    cmd: list[str], suffix: str, context: str = "", cache_path: Path | None = None
) -> Path:
    """Run ffmpeg to completion against a real file and return the path.

    The sibling of _stream_ffmpeg, for the containers in _SEEKABLE_OUTPUT_EXTS
    whose muxer finishes the file by seeking back to a header it wrote earlier
    (#458). A pipe cannot seek, so those headers were never patched and every
    exported WAV claimed roughly 4 GB of audio.

    Returning a path rather than yielding chunks is what lets the caller answer
    with FileResponse, and that is worth more than the extra buffering costs.
    A streamed render commits HTTP 200 before ffmpeg has exited, so a failure
    reaches the client as a truncated file that only the server log records;
    here the exit code is known while the response is still ours to choose, so
    a failed render is an honest 500. It also brings Content-Length and range
    requests, which chunked encoding cannot offer at all.

    `cmd` must not carry an output path -- this appends one. With `cache_path`
    the temp file is created beside it and a clean render is renamed into place
    as the cache entry (#290); the caller then serves the cache entry and must
    not delete it. Without one the caller owns the returned temp file and is
    responsible for removing it once the response has been sent.
    """
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp{suffix}")
    else:
        _MIXDOWN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _MIXDOWN_CACHE_DIR / f".render.{uuid.uuid4().hex}.tmp{suffix}"
    # Both names start with a dot, which is what keeps an in-flight render out
    # of _prune_mixdown_cache's eviction list and out of the cache-hit path.

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        "-y",
        str(tmp_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: deque[str] = deque(maxlen=30)
    drain_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_tail))
    ok = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=TIMEOUT_FFMPEG)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
        try:
            await asyncio.wait_for(drain_task, timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            drain_task.cancel()
        if proc.returncode != 0:
            logger.warning(
                "render ffmpeg exit %s [%s]: %s",
                proc.returncode,
                context,
                " | ".join(list(stderr_tail)[-8:]) or "(no stderr)",
            )
            raise HTTPException(status_code=500, detail="export failed")
        ok = True
    finally:
        # A cancelled request (client gone) unwinds through here too, so the
        # process is never left running and the temp file is never orphaned.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not ok:
            tmp_path.unlink(missing_ok=True)

    if cache_path is not None:
        os.replace(tmp_path, cache_path)
        _prune_mixdown_cache(cache_path.parent)
        return cache_path
    return tmp_path


def _unlink_later(path: Path) -> None:
    """Drop a rendered temp file once its response has been sent.

    Only ever attached to an uncached render. A cached one returns the cache
    entry itself, and deleting that would throw away the render the cache
    exists to keep."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove rendered export %s", path, exc_info=True)


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


@router.get("/jobs/{job_id}/stems/beats.json")
async def get_beat_grid(job_id: str) -> Response:
    """Return the pre-computed beat grid driving the click track.

    404s for jobs separated before this stage existed; the UI treats that as
    "no click track available" rather than an error.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / "beats.json").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="beat grid not found")
    return FileResponse(
        path,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _stem_download_name(job_id: str, name: str, ext: str, suffix: str = "") -> str:
    """Filename offered for a single stem, prefixed with the song (#336).

    Content-Disposition wins over an <a download> attribute for same-origin
    requests, so the server has to carry the name for the browser to honour it.
    """
    job = registry_get(job_id)
    slug = _title_slug(job.title if job else None)
    stem = f"{name}{suffix}"
    return f"{slug}_{stem}.{ext}" if slug else f"{stem}.{ext}"


@router.api_route("/jobs/{job_id}/stems/{name}.wav", methods=["GET", "HEAD"], response_model=None)
async def get_stem(
    job_id: str,
    name: str,
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
) -> FileResponse | StreamingResponse:
    """Download a WAV stem. Optional ?start=&end= trims to a time region."""
    path = _validate_stem_path(job_id, name)

    if start is None and end is None:
        return FileResponse(
            path, media_type="audio/wav", filename=_stem_download_name(job_id, name, "wav")
        )

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
    ]
    rendered = await _render_to_file(cmd, ".wav", context=f"stem-region job={job_id} stem={name}")
    return FileResponse(
        rendered,
        media_type="audio/wav",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_stem_download_name(job_id, name, "wav", "_region")}"'
            )
        },
        background=BackgroundTask(_unlink_later, rendered),
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
                "Content-Disposition": (
                    f'attachment; filename="{_stem_download_name(job_id, name, "mp3")}"'
                ),
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
    ]
    # Only the trimmed branch reaches here; the untrimmed one returned above.
    filename = _stem_download_name(job_id, name, "mp3", "_region")
    # Rendered rather than streamed for the same reason as the WAV region
    # above: on a pipe ffmpeg omits the Xing frame entirely, and these are VBR,
    # so the result has no duration and no seek table (#458).
    rendered = await _render_to_file(cmd, ".mp3", context=f"stem-mp3 job={job_id} stem={name}")
    return FileResponse(
        rendered,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(_unlink_later, rendered),
    )


def _mixdown_download_name(job_id: str, ext: str, is_region: bool) -> str:
    """Filename offered for a mixdown, prefixed with the song like the stems
    (#336). Mirrors the names the frontend builds, because Content-Disposition
    overrides an <a download> attribute and the two must not disagree."""
    job = registry_get(job_id)
    slug = _title_slug(job.title if job else None)
    kind = "region" if is_region else "exported_mix"
    return f"{slug}_{kind}.{ext}" if slug else f"{kind}.{ext}"


@router.get("/jobs/{job_id}/mixdown.{ext}", response_model=None)
async def get_mixdown(
    job_id: str,
    ext: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
    count_in: int = Query(
        default=0, ge=0, le=2, description="Count-in bars before the audio (0 off)"
    ),
) -> FileResponse | StreamingResponse:
    """Render a mixdown of the given lanes at the given gains, streamed as WAV,
    MP3, FLAC, or OGG. Mirrors the studio mixer (per-stem volume, mute, solo) so the
    exported file matches what is heard. The master fader is intentionally not
    applied -- it is a monitoring level, not part of the mix. Optional ?start=&end=
    trims to a loop region.

    `count_in` prepends N bars of click before the audio (issue #269): the stems
    are delayed and the count-in is baked into the click WAV, so the exported
    file leads in like a drummer's count. It works with or without the running
    click track (`click`), so a clean backing track can still carry a count-in.

    Identical params (including start/end and the current export sample rate)
    hit a render cache instead of re-running ffmpeg (#290) -- a cheap win on a
    shared server where the same export gets re-downloaded."""
    if ext not in ("wav", "mp3", "flac", "ogg"):
        raise HTTPException(status_code=404, detail="not found")

    names, parsed_gains = _parse_lane_gains(stems, gains)
    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )

    # Validates job_id (404), job done (404), and path traversal (404) per
    # stem -- deliberately before the cache lookup below, so a deleted or
    # not-yet-done job 404s the same way it always has instead of serving a
    # stale cache entry from before the job was removed.
    paths = [_validate_stem_path(job_id, name) for name in names]

    media_type = MIXDOWN_MEDIA_TYPES[ext]
    click_lane = _click_lane(
        job_id,
        click,
        click_mult,
        click_accent,
        click_gain,
        count_in_bars=count_in,
        start=start,
        end=end,
    )
    cache_key = _mixdown_cache_key(job_id, ext, names, parsed_gains, start, end, click_lane)
    cache_path = _MIXDOWN_CACHE_DIR / f"{cache_key}.{ext}"
    if cache_path.is_file():
        return FileResponse(
            cache_path,
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_mixdown_download_name(job_id, ext, start is not None)}"'
                )
            },
        )

    pre_seek = ["-ss", str(start)] if start is not None else []

    # A count-in shifts the whole timeline: the stems are delayed by the lead-in
    # and the click WAV already carries it, in output coordinates, so it is not
    # `-ss`-trimmed like a plain click. Everything else is generic over the input
    # list. lead_in is 0 for a plain click, collapsing this to the old graph.
    lead_in = click_lane.lead_in if click_lane else 0.0
    count_in_mode = bool(click_lane and click_lane.count_in)
    delay_ms = int(round(lead_in * 1000))
    # Output length is the region plus the lead-in prepended in front of it.
    post_seek = ["-t", str(lead_in + (end - start))] if start is not None else []

    stem_count = len(paths)
    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for p in paths:
        cmd += [*pre_seek, "-i", str(p)]
    if click_lane is not None:
        # The count-in WAV is already in output coordinates; a plain click is
        # full-length and lines up under the same -ss as the stems.
        click_pre = [] if count_in_mode else pre_seek
        cmd += [*click_pre, "-i", str(click_lane.path)]

    # Delay each stem by the lead-in (silent front-padding), apply its gain, then
    # sum with amix (normalize=0 keeps levels faithful, matching collect.py). The
    # click lane is never delayed -- its lead-in is baked in. A single audible
    # lane skips amix (a 1-input amix is a no-op).
    filters = []
    for i, g in enumerate(parsed_gains):
        delay = f"adelay={delay_ms}:all=1," if delay_ms > 0 else ""
        filters.append(f"[{i}:a]{delay}volume={g:.6f}[a{i}]")
    if click_lane is not None:
        filters.append(f"[{stem_count}:a]volume={click_lane.gain:.6f}[a{stem_count}]")
    n = stem_count + (1 if click_lane is not None else 0)
    if n > 1:
        labels = "".join(f"[a{i}]" for i in range(n))
        filters.append(f"{labels}amix=inputs={n}:normalize=0[mix]")
        out_label = "[mix]"
    else:
        out_label = "[a0]"
    codec = MIXDOWN_CODECS[ext]
    # Resample to the user's chosen export rate (default 44.1 kHz = the stem rate,
    # so a no-op unless changed). Applies to every audio container -- some hardware
    # samplers reject anything but a specific rate.
    rate = ["-ar", str(get_export_sample_rate())]
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        out_label,
        *post_seek,
        *codec,
        *rate,
    ]

    context = f"mixdown job={job_id} ext={ext} stems={stems}"
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{_mixdown_download_name(job_id, ext, start is not None)}"'
        )
    }
    if ext in _SEEKABLE_OUTPUT_EXTS:
        # Rendered to a file so the muxer can seek back and finish its header
        # (#458). It lands in the cache, so nothing is deleted afterwards --
        # this is the same file a later identical request will be served from
        # by the cache-hit branch above.
        rendered = await _render_to_file(cmd, f".{ext}", context=context, cache_path=cache_path)
        return FileResponse(rendered, media_type=media_type, headers=headers)
    # ogg needs no back-patching, so it keeps streaming and the client sees
    # bytes as soon as ffmpeg produces them.
    return StreamingResponse(
        _stream_ffmpeg([*cmd, "pipe:1"], context=context, cache_path=cache_path),
        media_type=media_type,
        headers=headers,
    )


def _title_slug(title: str | None) -> str:
    """Sanitize a song title into a filename-safe slug (matches the frontend).

    Returns "" for a title that survives sanitizing as empty, so callers can
    decide whether to substitute a fallback or drop the prefix entirely. The
    output is restricted to [A-Za-z0-9_], which also makes it safe to use as a
    ZIP member name -- no separators, no traversal.
    """
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", title or "")
    return re.sub(r"_{2,}", "_", safe).strip("_")[:80].strip("_")


def _safe_title(title: str | None) -> str:
    """Slug for a whole-archive filename, where an empty title still needs a name."""
    return _title_slug(title) or "stems"


@router.get("/jobs/{job_id}/video.mp4", response_model=None)
async def get_video_mixdown(
    job_id: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
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

    # Click is one more audio input. It must be appended before the video input
    # so the audio indices the filter graph references stay contiguous from 0.
    click_lane = _click_lane(job_id, click, click_mult, click_accent, click_gain)
    if click_lane is not None:
        paths = [*paths, click_lane[0]]
        parsed_gains = [*parsed_gains, click_lane[1]]

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
        _stream_ffmpeg(cmd, context=f"video-mux job={job_id} stems={stems}"),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _arcname(prefix: str, name: str, ext: str) -> str:
    """ZIP member name for one stem. Prefixed with the song slug so the files stay
    identifiable once extracted into a project folder alongside other songs'
    stems (#336); unprefixed when the song has no usable title."""
    return f"{prefix}_{name}.{ext}" if prefix else f"{name}.{ext}"


def _build_stems_zip(
    sources: list[tuple[str, Path]], fmt: str, dest: Path, prefix: str = ""
) -> None:
    """Blocking: write the stems into a ZIP. WAV files are stored as-is; MP3,
    FLAC, and OGG are transcoded per stem via ffmpeg. ZIP_STORED throughout - audio doesn't
    meaningfully compress, and STORED keeps the build fast. Runs in a thread."""
    if fmt == "wav":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            for name, p in sources:
                zf.write(p, arcname=_arcname(prefix, name, "wav"))
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
            zf.write(out, arcname=_arcname(prefix, name, fmt))


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
    if fmt not in ("wav", "mp3", "flac", "ogg"):
        raise HTTPException(status_code=422, detail="format must be 'wav', 'mp3', 'flac', or 'ogg'")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    # Resolve the requested subset (whitelisted) or fall back to all stems.
    all_names = (*STEM_NAMES, *EXTRA_STEM_NAMES)
    if stems:
        requested = {s for s in stems.split(",") if s}
        if not requested <= set(all_names):
            raise HTTPException(status_code=422, detail="unknown stem requested")
        if "vocals" in requested and _VOCAL_DECOMPOSITION_NAMES & requested:
            raise HTTPException(
                status_code=422,
                detail="vocals cannot be combined with lead_vocals/backing_vocals (same signal)",
            )
        wanted = [name for name in all_names if name in requested]
    else:
        wanted = list(all_names)

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
        await asyncio.to_thread(_build_stems_zip, sources, fmt, tmp_path, _title_slug(job.title))
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
