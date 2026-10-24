"""Recorded takes: upload, finish, edit, delete, serve, and the video render.

Contract (agreed with the recording and video frontends):

    POST   /api/jobs/{id}/takes                              create, 201 {take_id, label}
    PUT    /api/jobs/{id}/takes/{take_id}/parts/{kind}?seq=n raw bytes, kind audio|camera
    GET    /api/jobs/{id}/takes/{take_id}/parts              upload state, to resync
    POST   /api/jobs/{id}/takes/{take_id}/finish             sidecar -> audio.wav, job.takes
    PATCH  /api/jobs/{id}/takes/{take_id}                    label, user_nudge_ms, camera_offset_ms
    DELETE /api/jobs/{id}/takes/{take_id}
    GET    /api/jobs/{id}/takes/{take_id}/audio.wav          (and HEAD)
    GET    /api/jobs/{id}/takes/{take_id}/camera
    POST   /api/jobs/{id}/takes/{take_id}/render             202, background video render
    GET    /api/jobs/{id}/takes/{take_id}/render             progress
    POST   /api/jobs/{id}/takes/{take_id}/render/cancel
    GET    /api/jobs/{id}/takes/{take_id}/render.mp4

Placement: the sidecar's timeline.first_sample_song_sec already has the
round-trip latency folded in (the browser knows it); the server applies the
user's nudge, and is the only place that does, so changing it later can
re-render from the raw capture:

    placement_sec = first_sample_song_sec - user_nudge_ms / 1000

Positive means late, subtract: the same sign as every latency in the capture
layer.

Render length (RenderBody.span): "take" (the default) covers the take's own
span, from its count-in to where it stopped, clamped to the song; "song"
covers the whole song; "range" (Streamer Mode's trim) covers range_in to
range_out, inside the take's own span. The backing plays across the whole
render either way; the camera shows where it filmed and the canvas is black
elsewhere.

Streamer Mode records its composed scene as the take's camera part and asks
for canvas 16:9, 9:16 or 1:1 at size 720 or 1080 (the short side) and
fps 24, 30 or 60, fit "fill" (the picture is already the canvas's shape),
and optionally the click as one more lane.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from app.api import stems as stems_api
from app.core.config import (
    EXTRA_STEM_NAMES,
    FILMSTRIP_FRAME_HEIGHT,
    FILMSTRIP_MAX_FRAMES,
    JOB_ID_RE,
    JOBS_DIR,
    RENDER_CLICK_GAIN,
    STEM_NAMES,
    TAKE_AUDIO_MAX_BYTES_PER_SEC,
    TAKE_CAMERA_MAX_BYTES_PER_SEC,
    TAKE_ID_RE,
    TAKE_MAX_PER_JOB,
    TAKE_MAX_SEQ,
    TAKE_PART_MAX_BYTES,
    TAKE_SLACK_SEC,
    TAKES_DIRNAME,
    TIMEOUT_FILMSTRIP,
    TIMEOUT_VIDEO_GPU_STALL,
    TIMEOUT_VIDEO_RENDER,
    TRACK_ID_RE,
    TRACK_MAX_PER_JOB,
    VIDEO_CPU_ENCODER,
    ffmpeg_executable,
    ffprobe_executable,
)
from app.core.models import Job, _set
from app.core.registry import get as registry_get
from app.core.registry import persist as registry_persist
from app.core.settings import get_export_sample_rate
from app.core.stem_files import stem_path
from app.core.takes import (
    camera_file,
    make_track,
    new_track_id,
    normalise_tracks,
    take_dir,
    take_entry,
    write_tracks,
)
from app.pipeline import video_encoder
from app.pipeline.click_render import CLICK_TO_STEREO
from app.pipeline.click_sounds import DEFAULT_CLICK_SOUND
from app.pipeline.collect import merge_stem_peaks
from app.pipeline.runner import _pipeline_lock
from app.pipeline.take_render import (
    TAKE_RATE,
    AudioLane,
    RenderCancelled,
    TakeRenderError,
    aac_available,
    assemble_raw_wav,
    build_video_render_cmd,
    canvas_size,
    libx264_available,
    render_take_audio,
    run_video_render,
    sniff_container,
    wav_frames,
)

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["takes"])

PartKind = Literal["audio", "camera"]
_PART_KINDS = ("audio", "camera")
_STATE_FILE = "parts.json"
_SIDECAR_FILE = "take.json"
_RENDER_FILE = "render.mp4"
_LABEL_MAX = 80
_SESSION_MAX_BYTES = 8 * 1024
_NUDGE_MAX_MS = 2000.0
_CAMERA_OFFSET_MAX_MS = 5000.0
_RENDER_MAX_LANES = 32
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# One lock per take: parts, finish, edit and delete of the same take never
# interleave, so the append offset and parts.json always agree. Keyed by
# "<job_id>/<take_id>"; at most TAKE_MAX_PER_JOB per job, dropped on delete.
_take_locks: dict[str, asyncio.Lock] = {}


def _lock_for(job_id: str, take_id: str) -> asyncio.Lock:
    key = f"{job_id}/{take_id}"
    lock = _take_locks.get(key)
    if lock is None:
        lock = _take_locks[key] = asyncio.Lock()
    return lock


# ---------------------------------------------------------------------------
# Validation helpers. Order, per the security skill: job id, then take id and
# kind whitelist, then the resolved path inside JOBS_DIR, then any I/O.


def _job_or_404(job_id: str) -> Job:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    return job


def _take_or_404(job_id: str, take_id: str) -> tuple[Job, Path]:
    job = _job_or_404(job_id)
    if not TAKE_ID_RE.match(take_id):
        raise HTTPException(status_code=404, detail="take not found")
    folder = take_dir(job_id, take_id, JOBS_DIR)
    if folder is None or not folder.is_dir():
        raise HTTPException(status_code=404, detail="take not found")
    return job, folder


def _entry(job: Job, take_id: str) -> dict[str, Any] | None:
    return next((t for t in job.takes if t.get("id") == take_id), None)


def _finished_or_404(job_id: str, take_id: str) -> tuple[Job, Path, dict[str, Any]]:
    job, folder = _take_or_404(job_id, take_id)
    entry = _entry(job, take_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="take not finished")
    return job, folder, entry


def _clean_label(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > _LABEL_MAX or _CONTROL_CHARS.search(value):
        raise ValueError(f"label must be 1 to {_LABEL_MAX} printable characters")
    return value


def _reject_constant(token: str) -> float:
    raise ValueError(f"{token} is not a number")


async def _parse_body(request: Request, model: type[BaseModel], *, empty_ok: bool = False):
    """Parse a JSON body into `model`, after the ids have been checked.

    Not a FastAPI body parameter, for two reasons. Order: a body parameter is
    validated before the handler runs, so a bad body on an unknown job was a
    422 instead of a 404. And NaN: json accepts the NaN and Infinity tokens,
    and FastAPI's 422 then echoes the offending input back, which it cannot
    serialise, so a NaN in a sidecar came back as a 500. Refused here, at
    parse time, and the 422 detail never repeats the input."""
    raw = await request.body()
    if not raw.strip():
        if empty_ok:
            return model()
        raise HTTPException(status_code=422, detail="request body required")
    try:
        data = json.loads(raw, parse_constant=_reject_constant)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="body must be JSON with finite numbers"
        ) from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:5]
        )
        raise HTTPException(status_code=422, detail=detail[:500]) from None


# ---------------------------------------------------------------------------
# Small file helpers, all blocking.


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("could not read %s", path, exc_info=True)
        raise HTTPException(status_code=500, detail="take is unreadable") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="take is unreadable")
    return data


def _new_state(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "created_at": time.time(),
        "finished": False,
        **{kind: {"next_seq": 0, "bytes": 0} for kind in _PART_KINDS},
    }


def _reference_stem(job_id: str) -> Path | None:
    """A stem whose length every take is cut to. Any of them: the pipeline
    writes all of them from one Demucs pass, so they share a frame count."""
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        return None
    for name in (*STEM_NAMES, *EXTRA_STEM_NAMES, "original"):
        # WAV or MP3 (#783): an MP3 stem decodes to the WAV's exact frame
        # count, which stem_storage verified before keeping it.
        path = stem_path(stems_dir, name)
        if path.is_file():
            return path
    return None


def _song_frames(job_id: str) -> int:
    ref = _reference_stem(job_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="stems not found")
    return wav_frames(ref)


def _size_cap(job: Job, kind: str) -> int:
    seconds = (job.duration_sec or 3600.0) + TAKE_SLACK_SEC
    per_sec = TAKE_AUDIO_MAX_BYTES_PER_SEC if kind == "audio" else TAKE_CAMERA_MAX_BYTES_PER_SEC
    return int(seconds * per_sec)


def _placement_sec(sidecar: dict[str, Any]) -> float:
    timeline = sidecar.get("timeline") or {}
    latency = sidecar.get("latency") or {}
    return float(timeline.get("first_sample_song_sec") or 0.0) - (
        float(latency.get("user_nudge_ms") or 0.0) / 1000.0
    )


def _scene_start_sec(sidecar: dict[str, Any], video: dict[str, Any], offset_ms: float) -> float:
    """Song time of a composed scene's first frame (Streamer Mode, fit "fill").

    A camera films the player, who plays to what was heard one round trip
    before the microphone took it: the take's audio is placed with the round
    trip and the nudge taken off (first_sample_song_sec, _placement_sec), and
    the camera shares that placement. A composed scene is not filmed: each
    frame draws the playhead heard at the moment it is drawn, so its frame 0
    is at the song time heard then, the round trip added back and no nudge.
    Measured on exports (fix pass G): with the round trip taken off, the
    picture ran a round trip early (-113 ms at 150 ms, +20 ms at 0)."""
    timeline = sidecar.get("timeline") or {}
    latency = sidecar.get("latency") or {}
    first = float(timeline.get("first_sample_song_sec") or 0.0)
    round_trip = float(latency.get("round_trip_ms") or 0.0) / 1000.0
    return (
        first + round_trip + float(video.get("firstFrameAtMs") or 0.0) / 1000.0 - offset_ms / 1000.0
    )


def _refresh_peaks(job_id: str, take_id: str, folder: Path, *, remove: bool = False) -> None:
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        return
    if remove:
        merge_stem_peaks(stems_dir, [], remove=[take_id])
    else:
        merge_stem_peaks(stems_dir, [take_id], sources={take_id: folder / "audio.wav"})


def _commit(job: Job, takes: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> None:
    """Write tracks and takes together: tracks.json first (the file wins on
    load), then the job, through _set so the SSE stream sees one change, then
    the registry. Synchronous and free of awaits on purpose: every read and
    write of job.tracks happens inside one event-loop step, so two requests
    can never interleave a read-modify-write of the track list."""
    tracks = normalise_tracks(tracks, takes)
    if tracks != job.tracks:
        write_tracks(JOBS_DIR / job.id, tracks)
    _set(job, takes=takes, tracks=tracks)
    registry_persist(JOBS_DIR)


def _replace_entry(
    job: Job, entry: dict[str, Any] | None, take_id: str, *, activate: bool = False
) -> None:
    """Swap one take's record in job.takes (None removes it) and persist.

    With `activate`, the take becomes its track's active take, creating the
    track when it is missing (a take opened before tracks existed). Removing
    the active take falls back to the track's newest remaining one."""
    takes = [t for t in job.takes if t.get("id") != take_id]
    tracks = [dict(t) for t in job.tracks]
    if entry is not None:
        takes.append(entry)
        takes.sort(key=lambda t: t.get("created_at") or 0.0)
        if activate:
            track = next((t for t in tracks if t["id"] == entry.get("track_id")), None)
            if track is None:
                track = make_track(entry["track_id"], entry["label"], len(tracks))
                tracks.append(track)
            track["active_take_id"] = take_id
    _commit(job, takes, tracks)


def _track_or_404(job: Job, track_id: str) -> dict[str, Any]:
    if not TRACK_ID_RE.match(track_id):
        raise HTTPException(status_code=404, detail="track not found")
    track = next((t for t in job.tracks if t.get("id") == track_id), None)
    if track is None:
        raise HTTPException(status_code=404, detail="track not found")
    return track


# ---------------------------------------------------------------------------
# Create


class CreateTakeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_id: str | None = Field(default=None, max_length=16)
    label: str | None = Field(default=None, max_length=_LABEL_MAX * 4)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _clean_label(v)


@router.post("/{job_id}/takes", status_code=201)
async def create_take(job_id: str, request: Request) -> dict[str, str]:
    """Open a new take folder on a recording track: {"track_id", "label"?}."""
    job = _job_or_404(job_id)
    body = await _parse_body(request, CreateTakeBody, empty_ok=True)
    if body.track_id is None:
        raise HTTPException(status_code=422, detail="track_id required")
    if not TRACK_ID_RE.match(body.track_id) or not any(
        t.get("id") == body.track_id for t in job.tracks
    ):
        raise HTTPException(status_code=422, detail="unknown track")
    track_id = body.track_id
    on_track = sum(1 for t in job.takes if t.get("track_id") == track_id)

    root = (JOBS_DIR / job_id / TAKES_DIRNAME).resolve()
    if not root.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")

    def make() -> tuple[str, str]:
        root.mkdir(parents=True, exist_ok=True)
        existing = [p for p in root.iterdir() if p.is_dir() and TAKE_ID_RE.match(p.name)]
        if len(existing) >= TAKE_MAX_PER_JOB:
            raise HTTPException(status_code=409, detail="too many takes for this song")
        label = body.label or f"Take {on_track + 1}"
        for _ in range(8):
            take_id = "t" + secrets.token_hex(4)
            folder = root / take_id
            try:
                folder.mkdir()
            except FileExistsError:
                continue
            _write_json_atomic(folder / _STATE_FILE, {**_new_state(label), "track_id": track_id})
            return take_id, label
        raise HTTPException(status_code=500, detail="could not create take")

    take_id, label = await asyncio.to_thread(make)
    logger.info("[%s] take %s created on track %s", job.id, take_id, track_id)
    return {"take_id": take_id, "label": label, "track_id": track_id}


# ---------------------------------------------------------------------------
# Parts


async def _read_body_capped(request: Request) -> bytes:
    """The raw body, refused past TAKE_PART_MAX_BYTES while it streams in.
    The middleware already refuses a declared length over 4 MB; this covers a
    body longer than the length it declared."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > TAKE_PART_MAX_BYTES:
            raise HTTPException(status_code=413, detail="part too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.put("/{job_id}/takes/{take_id}/parts/{kind}")
async def put_part(
    job_id: str,
    take_id: str,
    kind: str,
    request: Request,
    seq: int = Query(..., ge=0, le=TAKE_MAX_SEQ),
) -> dict[str, Any]:
    """Append one part. seq must be exactly the next one; repeating the last
    one is a no-op, so a retry after a lost response is safe."""
    job, folder = _take_or_404(job_id, take_id)
    if kind not in _PART_KINDS:
        raise HTTPException(status_code=404, detail="unknown part kind")
    data = await _read_body_capped(request)
    if not data:
        raise HTTPException(status_code=422, detail="empty part")
    if kind == "audio" and len(data) % 4:
        raise HTTPException(status_code=422, detail="audio part must be whole float32 samples")

    async with _lock_for(job_id, take_id):
        state_path = folder / _STATE_FILE
        state = await asyncio.to_thread(_read_json, state_path)
        if state.get("finished"):
            raise HTTPException(status_code=409, detail="take already finished")
        slot = state.setdefault(kind, {"next_seq": 0, "bytes": 0})
        next_seq = int(slot.get("next_seq", 0))
        received = int(slot.get("bytes", 0))
        if next_seq > 0 and seq == next_seq - 1:
            return {
                "take_id": take_id,
                "kind": kind,
                "seq": seq,
                "bytes": received,
                "duplicate": True,
            }
        if seq != next_seq:
            raise HTTPException(status_code=409, detail="unexpected part sequence")
        if received + len(data) > _size_cap(job, kind):
            raise HTTPException(status_code=413, detail="take too large for this song")

        def append() -> None:
            part = folder / f"{kind}.part"
            with part.open("ab") as fh:
                # Cut back to what parts.json says arrived: a crash between
                # the write and the state update must not leave the part
                # that will be retried in the file twice.
                fh.truncate(received)
                fh.seek(received)
                fh.write(data)
            slot["next_seq"] = seq + 1
            slot["bytes"] = received + len(data)
            _write_json_atomic(state_path, state)

        await asyncio.to_thread(append)
    return {
        "take_id": take_id,
        "kind": kind,
        "seq": seq,
        "bytes": received + len(data),
        "duplicate": False,
    }


@router.get("/{job_id}/takes/{take_id}/parts")
async def get_parts(job_id: str, take_id: str) -> dict[str, Any]:
    _job, folder = _take_or_404(job_id, take_id)
    state = await asyncio.to_thread(_read_json, folder / _STATE_FILE)
    return {
        **{
            kind: {
                "next_seq": int((state.get(kind) or {}).get("next_seq", 0)),
                "bytes": int((state.get(kind) or {}).get("bytes", 0)),
            }
            for kind in _PART_KINDS
        },
        "finished": bool(state.get("finished")),
    }


# ---------------------------------------------------------------------------
# Finish: the sidecar


def _finite(lo: float, hi: float, default: float | None = None) -> Any:
    return Field(default=default, ge=lo, le=hi, allow_inf_nan=False)


class TimelineModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    first_sample_song_sec: float = Field(ge=-600, le=86_400, allow_inf_nan=False)
    start_song_sec: float | None = _finite(-86_400, 86_400)
    end_song_sec: float | None = _finite(-86_400, 86_400)
    tempo: float = _finite(0.01, 100, 1.0)
    count_in_sec: float | None = _finite(0, 600)
    ctx_time_at_frame0: float | None = _finite(-1e12, 1e12)
    perf_ms_at_frame0: float | None = _finite(-1e15, 1e15)


class LatencyModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    round_trip_ms: float | None = _finite(-10_000, 10_000)
    source: Literal["calibrated", "estimated"] | None = None
    output_latency_ms: float | None = _finite(-10_000, 10_000)
    base_latency_ms: float | None = _finite(-10_000, 10_000)
    input_latency_ms_reported: float | None = _finite(-10_000, 10_000)
    pipeline_latency_ms: float | None = _finite(-10_000, 10_000)
    user_nudge_ms: float = _finite(-_NUDGE_MAX_MS, _NUDGE_MAX_MS, 0.0)
    drift_ppm: float | None = _finite(-10_000, 10_000)


class CaptureModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rate: int = Field(ge=8000, le=192_000)
    channels: Literal[1, 2]
    device_label: str | None = Field(default=None, max_length=200)
    dropped_quanta: int | None = Field(default=None, ge=0, le=10**12)


class VideoModel(BaseModel):
    """The camera block (06-video-recording 5.5). Absent fields take their
    defaults: the browser omits what it did not report."""

    model_config = ConfigDict(extra="ignore")
    file: str | None = Field(default=None, max_length=100)  # ignored, set by the server
    mime: str | None = Field(default=None, max_length=100)
    w: int | None = Field(default=None, ge=1, le=4096)
    h: int | None = Field(default=None, ge=1, le=4096)
    fps_nominal: float | None = _finite(1, 240)
    deviceLabel: str | None = Field(default=None, max_length=200)  # noqa: N815 -- agreed wire name
    cameraOffsetMs: float = _finite(-_CAMERA_OFFSET_MAX_MS, _CAMERA_OFFSET_MAX_MS, 0.0)  # noqa: N815
    mirrorPreview: StrictBool | None = None  # noqa: N815
    startedAtAudioCtx: float | None = _finite(-1e12, 1e12)  # noqa: N815
    startedAtPerf: float | None = _finite(-1e15, 1e15)  # noqa: N815
    firstFrameAtMs: float = _finite(-600_000, 600_000, 0.0)  # noqa: N815
    # How firstFrameAtMs was taken: the first frame's capture time, or the
    # recorder's start event when the engine reports no capture times.
    firstFrameClock: Literal["capture", "recorder-start"] | None = None  # noqa: N815
    parts: int | None = Field(default=None, ge=0, le=TAKE_MAX_SEQ + 1)


class FinishBody(BaseModel):
    """take.json, version 1 (05-audio-recording 5.6). id and created_at are
    the server's and ignored when sent."""

    model_config = ConfigDict(extra="ignore")
    v: Literal[1]
    label: str | None = Field(default=None, max_length=_LABEL_MAX * 4)
    timeline: TimelineModel
    latency: LatencyModel = Field(default_factory=LatencyModel)
    capture: CaptureModel
    video: VideoModel | None = None
    session: dict[str, Any] | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _clean_label(v)

    @field_validator("session")
    @classmethod
    def _bounded_session(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        # Agent 7's block: stored, not interpreted here, so only its size and
        # JSON-ness are checked (no NaN, nothing json cannot write back).
        if v is None:
            return v
        try:
            encoded = json.dumps(v, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("session must be plain JSON") from None
        if len(encoded.encode("utf-8")) > _SESSION_MAX_BYTES:
            raise ValueError("session block too large")
        return v


@router.post("/{job_id}/takes/{take_id}/finish")
async def finish_take(job_id: str, take_id: str, request: Request) -> dict[str, Any]:
    """Close the take: build audio.raw.wav from the audio parts, name the
    camera file, render audio.wav aligned and padded to the stems, merge its
    peaks, write take.json, and add the lane to the job. Synchronous: a
    5-minute take renders in a second or two."""
    job, folder = _take_or_404(job_id, take_id)
    body: FinishBody = await _parse_body(request, FinishBody)
    if body.timeline.tempo != 1.0:
        raise HTTPException(status_code=422, detail="takes recorded at a tempo other than 1.0")
    frames = await asyncio.to_thread(_song_frames, job_id)
    song_sec = frames / TAKE_RATE
    if body.timeline.first_sample_song_sec > song_sec + 600:
        raise HTTPException(status_code=422, detail="take starts after the song")

    async with _lock_for(job_id, take_id):
        state_path = folder / _STATE_FILE
        state = await asyncio.to_thread(_read_json, state_path)
        if state.get("finished"):
            raise HTTPException(status_code=409, detail="take already finished")
        channels = body.capture.channels
        rate = body.capture.rate
        audio_part = folder / "audio.part"
        raw_wav = folder / "audio.raw.wav"
        camera_part = folder / "camera.part"

        def prepare() -> str | None:
            """Validate every input before changing any file, so a refused
            finish can be corrected and sent again."""
            if audio_part.is_file():
                size = audio_part.stat().st_size
                if size == 0:
                    raise HTTPException(status_code=422, detail="take has no audio")
                if size % (4 * channels):
                    raise HTTPException(
                        status_code=422, detail="audio size does not match the channel count"
                    )
            elif not raw_wav.is_file():
                raise HTTPException(status_code=422, detail="take has no audio")
            camera_ext = None
            # Without the video block the camera file is not used: the
            # frontend sends the block only when every camera part arrived,
            # so a missing one means a file with a hole or an unknown start,
            # which would render out of sync. It is discarded in build().
            # The block also says how many parts were sent; fewer on disk
            # means the end of the recording is missing, and the take keeps
            # its sound without the video rather than a short one.
            sent = body.video.parts if body.video is not None else None
            received = int((state.get("camera") or {}).get("next_seq", 0))
            if sent is not None and sent != received:
                logger.warning(
                    "[%s] take %s: %d camera parts sent, %d received; video dropped",
                    job_id,
                    take_id,
                    sent,
                    received,
                )
                return None
            if body.video is not None and camera_part.is_file() and camera_part.stat().st_size > 0:
                camera_ext = sniff_container(camera_part)
                if camera_ext is None:
                    raise HTTPException(status_code=422, detail="camera file is not WebM or MP4")
            return camera_ext

        camera_ext = await asyncio.to_thread(prepare)

        sidecar = body.model_dump(exclude_none=True)
        sidecar["id"] = take_id
        sidecar["created_at"] = float(state.get("created_at") or time.time())
        sidecar["label"] = body.label or str(state.get("label") or take_id)
        # The track it was opened on; a take opened before tracks existed, or
        # whose track was deleted meanwhile, gets a new track of its own.
        track_id = str(state.get("track_id") or "")
        if not any(t.get("id") == track_id for t in job.tracks):
            track_id = new_track_id({t["id"] for t in job.tracks})
        sidecar["track_id"] = track_id
        if camera_ext is not None:
            sidecar["video"]["file"] = f"camera.{camera_ext}"
        else:
            sidecar.pop("video", None)
        placement = _placement_sec(sidecar)

        def build() -> None:
            if audio_part.is_file():
                assemble_raw_wav(audio_part, raw_wav, rate, channels)
                audio_part.unlink()
            if camera_ext is not None:
                os.replace(camera_part, folder / f"camera.{camera_ext}")
            else:
                camera_part.unlink(missing_ok=True)
            render_take_audio(raw_wav, folder / "audio.wav", placement, frames, rate, channels)
            _write_json_atomic(folder / _SIDECAR_FILE, sidecar)
            _refresh_peaks(job_id, take_id, folder)
            state["finished"] = True
            _write_json_atomic(state_path, state)

        try:
            await asyncio.to_thread(build)
        except TakeRenderError:
            logger.warning("[%s] take %s render failed", job_id, take_id, exc_info=True)
            raise HTTPException(status_code=500, detail="take render failed") from None

        entry = take_entry(job_id, sidecar, camera_ext is not None)
        _replace_entry(job, entry, take_id, activate=True)
    logger.info("[%s] take %s finished (placement %.3f s)", job_id, take_id, placement)
    return entry


# ---------------------------------------------------------------------------
# Edit, delete


class PatchTakeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=_LABEL_MAX * 4)
    user_nudge_ms: float | None = _finite(-_NUDGE_MAX_MS, _NUDGE_MAX_MS)
    camera_offset_ms: float | None = _finite(-_CAMERA_OFFSET_MAX_MS, _CAMERA_OFFSET_MAX_MS)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _clean_label(v)


@router.patch("/{job_id}/takes/{take_id}")
async def patch_take(job_id: str, take_id: str, request: Request) -> dict[str, Any]:
    """Rename, nudge (re-renders audio.wav from the raw capture), or set the
    camera offset. A changed nudge or offset drops an earlier video render:
    it no longer matches what the user hears."""
    job, folder = _take_or_404(job_id, take_id)
    body: PatchTakeBody = await _parse_body(request, PatchTakeBody)
    if body.label is None and body.user_nudge_ms is None and body.camera_offset_ms is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    if _entry(job, take_id) is None:
        raise HTTPException(status_code=409, detail="take not finished")
    if _render_busy(job_id, take_id):
        raise HTTPException(status_code=409, detail="video render running")

    async with _lock_for(job_id, take_id):
        sidecar = await asyncio.to_thread(_read_json, folder / _SIDECAR_FILE)
        has_camera = await asyncio.to_thread(camera_file, folder)
        if body.camera_offset_ms is not None and has_camera is None:
            raise HTTPException(status_code=422, detail="take has no camera")
        latency = sidecar.setdefault("latency", {})
        old_nudge = float(latency.get("user_nudge_ms") or 0.0)
        video = sidecar.get("video") or {}
        old_offset = float(video.get("cameraOffsetMs") or 0.0)

        if body.label is not None:
            sidecar["label"] = body.label
        nudged = body.user_nudge_ms is not None and body.user_nudge_ms != old_nudge
        if body.user_nudge_ms is not None:
            latency["user_nudge_ms"] = body.user_nudge_ms
        offset_moved = body.camera_offset_ms is not None and body.camera_offset_ms != old_offset
        if body.camera_offset_ms is not None:
            sidecar.setdefault("video", {})["cameraOffsetMs"] = body.camera_offset_ms

        def apply() -> None:
            if nudged:
                frames = _song_frames(job_id)
                capture = sidecar.get("capture") or {}
                render_take_audio(
                    folder / "audio.raw.wav",
                    folder / "audio.wav",
                    _placement_sec(sidecar),
                    frames,
                    int(capture.get("rate") or TAKE_RATE),
                    int(capture.get("channels") or 2),
                )
                _refresh_peaks(job_id, take_id, folder)
            if nudged or offset_moved:
                (folder / _RENDER_FILE).unlink(missing_ok=True)
            _write_json_atomic(folder / _SIDECAR_FILE, sidecar)

        try:
            await asyncio.to_thread(apply)
        except TakeRenderError:
            logger.warning("[%s] take %s re-render failed", job_id, take_id, exc_info=True)
            raise HTTPException(status_code=500, detail="take render failed") from None
        if nudged or offset_moved:
            _renders.pop((job_id, take_id), None)
        entry = take_entry(job_id, sidecar, has_camera is not None)
        _replace_entry(job, entry, take_id)
    return entry


@router.delete("/{job_id}/takes/{take_id}")
async def delete_take(job_id: str, take_id: str) -> dict[str, str]:
    """Remove a take, finished or not: folder, lane and waveform."""
    job, folder = _take_or_404(job_id, take_id)
    async with _lock_for(job_id, take_id):
        # A render of this take stops first (its ffmpeg would hold the files
        # open on Windows) and is waited for.
        run = _renders.pop((job_id, take_id), None)
        if run is not None:
            await asyncio.to_thread(_stop_and_wait, run)
        removed = await asyncio.to_thread(_remove_take_folder, job_id, take_id, folder)
        if _entry(job, take_id) is not None:
            _replace_entry(job, None, take_id)
    _take_locks.pop(f"{job_id}/{take_id}", None)
    if not removed:
        raise HTTPException(status_code=500, detail="take files could not be deleted")
    return {"take_id": take_id, "status": "deleted"}


def _remove_take_folder(job_id: str, take_id: str, folder: Path) -> bool:
    """Blocking. Retried briefly: on Windows a render ffmpeg that was just
    killed can hold camera.* open for a moment after it exits."""
    for attempt in range(10):
        try:
            shutil.rmtree(folder)
            break
        except FileNotFoundError:
            break
        except OSError:
            if attempt == 9:
                logger.warning("could not remove take %s", folder, exc_info=True)
            time.sleep(0.2)
    _refresh_peaks(job_id, take_id, folder, remove=True)
    return not folder.exists()


# ---------------------------------------------------------------------------
# Tracks


class TrackInput(BaseModel):
    """Which input a track records from. Only stored for the UI: capture runs
    in the browser and the take's sidecar says what was actually captured."""

    model_config = ConfigDict(extra="forbid")
    device_id: str | None = Field(default=None, max_length=256)
    device_label: str | None = Field(default=None, max_length=200)
    channels: list[int] = Field(min_length=1, max_length=2)

    @field_validator("channels")
    @classmethod
    def _channels(cls, v: list[int]) -> list[int]:
        if any(isinstance(c, bool) or not 0 <= c <= 63 for c in v):
            raise ValueError("channel index must be 0..63")
        if len(set(v)) != len(v):
            raise ValueError("a stereo pair needs two different channels")
        return v

    @field_validator("device_id", "device_label")
    @classmethod
    def _printable(cls, v: str | None) -> str | None:
        if v is not None and _CONTROL_CHARS.search(v):
            raise ValueError("must be printable")
        return v


class CreateTrackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=_LABEL_MAX * 4)
    input: TrackInput | None = None
    # A video track films its takes with the camera as well as recording
    # sound. Only stored for the UI, like the input.
    kind: Literal["audio", "video"] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        return _clean_label(v)


class PatchTrackBody(CreateTrackBody):
    active_take_id: str | None = Field(default=None, max_length=16)
    order: int | None = Field(default=None, ge=0, le=TRACK_MAX_PER_JOB)


def _track_view(job: Job, track_id: str) -> dict[str, Any]:
    return next(t for t in job.tracks if t["id"] == track_id)


@router.post("/{job_id}/tracks", status_code=201)
async def create_track(job_id: str, request: Request) -> dict[str, Any]:
    """Add a recording track: {} or {"name"?, "input"?, "kind"?}."""
    job = _job_or_404(job_id)
    root = (JOBS_DIR / job_id / TAKES_DIRNAME).resolve()
    if not root.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")
    body = await _parse_body(request, CreateTrackBody, empty_ok=True)
    # No await from here to the commit: see _commit.
    if len(job.tracks) >= TRACK_MAX_PER_JOB:
        raise HTTPException(status_code=409, detail="too many tracks for this song")
    used = {t["id"] for t in job.tracks}
    track_id = new_track_id(used)
    names = {t.get("name") for t in job.tracks}
    n = len(job.tracks) + 1
    while f"Track {n}" in names:
        n += 1
    track = make_track(
        track_id,
        body.name or f"Track {n}",
        len(job.tracks),
        body.input.model_dump() if body.input else None,
        body.kind or "audio",
    )
    _commit(job, list(job.takes), [*job.tracks, track])
    logger.info("[%s] track %s created", job_id, track_id)
    return _track_view(job, track_id)


@router.patch("/{job_id}/tracks/{track_id}")
async def patch_track(job_id: str, track_id: str, request: Request) -> dict[str, Any]:
    """Rename, set the input (null clears it), switch the active take, or
    move the track to another position."""
    job = _job_or_404(job_id)
    _track_or_404(job, track_id)
    body = await _parse_body(request, PatchTrackBody)
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="nothing to change")
    # Re-read after the await, then no await until the commit.
    track = dict(_track_or_404(job, track_id))
    if "name" in fields:
        if body.name is None:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        track["name"] = body.name
    if "input" in fields:
        track["input"] = body.input.model_dump() if body.input else None
    if "kind" in fields:
        track["kind"] = body.kind or "audio"
    if "active_take_id" in fields:
        take = next(
            (
                t
                for t in job.takes
                if t.get("id") == body.active_take_id and t.get("track_id") == track_id
            ),
            None,
        )
        if take is None:
            raise HTTPException(status_code=422, detail="not a finished take of this track")
        track["active_take_id"] = take["id"]
    others = [dict(t) for t in job.tracks if t["id"] != track_id]
    position = track["order"]
    if "order" in fields and body.order is not None:
        position = min(body.order, len(others))
    others.insert(position, track)
    for i, t in enumerate(others):
        t["order"] = i
    _commit(job, list(job.takes), others)
    return _track_view(job, track_id)


@router.delete("/{job_id}/tracks/{track_id}")
async def delete_track(job_id: str, track_id: str) -> dict[str, Any]:
    """Delete a track and every take on it, finished or not, stopping any
    video render of them first."""
    job = _job_or_404(job_id)
    _track_or_404(job, track_id)
    root = (JOBS_DIR / job_id / TAKES_DIRNAME).resolve()
    if not root.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="job not found")

    def owned() -> list[str]:
        ids = {t["id"] for t in job.takes if t.get("track_id") == track_id}
        if root.is_dir():
            for folder in root.iterdir():
                if not folder.is_dir() or not TAKE_ID_RE.match(folder.name):
                    continue
                state = folder / _STATE_FILE
                try:
                    if json.loads(state.read_text(encoding="utf-8")).get("track_id") == track_id:
                        ids.add(folder.name)
                except (OSError, ValueError, AttributeError):
                    continue
        return sorted(ids)

    take_ids = await asyncio.to_thread(owned)
    # The track goes first, so a take finishing meanwhile cannot make it
    # active again; its takes follow.
    _commit(
        job,
        [t for t in job.takes if t.get("id") not in take_ids],
        [t for t in job.tracks if t["id"] != track_id],
    )
    failed = False
    for take_id in take_ids:
        run = _renders.pop((job_id, take_id), None)
        if run is not None:
            _stop_run(run)
        folder = take_dir(job_id, take_id, JOBS_DIR)
        if folder is None:
            continue
        async with _lock_for(job_id, take_id):
            if not await asyncio.to_thread(_remove_take_folder, job_id, take_id, folder):
                failed = True
        _take_locks.pop(f"{job_id}/{take_id}", None)
    if any(t.get("id") in take_ids for t in job.takes):
        _commit(job, [t for t in job.takes if t.get("id") not in take_ids], list(job.tracks))
    logger.info("[%s] track %s deleted with %d takes", job_id, track_id, len(take_ids))
    if failed:
        raise HTTPException(status_code=500, detail="take files could not be deleted")
    return {"track_id": track_id, "status": "deleted", "takes_deleted": take_ids}


# ---------------------------------------------------------------------------
# Serve


def _download_name(job: Job, label: str, ext: str) -> str:
    parts = [p for p in (stems_api._title_slug(job.title), stems_api._title_slug(label)) if p]
    return f"{'_'.join(parts) or 'take'}.{ext}"


@router.api_route(
    "/{job_id}/takes/{take_id}/audio.wav", methods=["GET", "HEAD"], response_model=None
)
async def get_take_audio(job_id: str, take_id: str) -> FileResponse:
    job, folder, entry = _finished_or_404(job_id, take_id)
    path = (folder / "audio.wav").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="take not found")
    return FileResponse(
        path, media_type="audio/wav", filename=_download_name(job, entry["label"], "wav")
    )


@router.get("/{job_id}/takes/{take_id}/camera", response_model=None)
async def get_take_camera(job_id: str, take_id: str) -> FileResponse:
    _job, folder, _entry_ = _finished_or_404(job_id, take_id)
    path = camera_file(folder)
    if path is None or not path.resolve().is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="take has no camera")
    media = "video/webm" if path.suffix == ".webm" else "video/mp4"
    return FileResponse(path.resolve(), media_type=media)


# ---------------------------------------------------------------------------
# Filmstrip: the frames a video track draws along its lane


_FILMSTRIP_IMAGE = "filmstrip.jpg"
_FILMSTRIP_META = "filmstrip.json"
_filmstrip_locks: dict[str, threading.Lock] = {}
_filmstrip_locks_guard = threading.Lock()


def _probe_video(path: Path) -> tuple[float, int, int]:
    """(duration in seconds, width, height) of the first video stream."""
    proc = subprocess.run(
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_FILMSTRIP,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:] or "ffprobe failed")
    data = json.loads(proc.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        # A WebM written as MediaRecorder streams it has no duration in its
        # header: decode it and read how far the decoder got.
        duration = _decoded_duration(path)
    return duration, int(stream.get("width") or 0), int(stream.get("height") or 0)


_DECODE_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _decoded_duration(path: Path) -> float:
    """How long the first video stream runs: the end of its last packet.

    Packet timestamps are read first because the decoder's "time=" is not the
    same on every ffmpeg: 5.1 and 7.1 print the end of the last frame, 6.1
    (Ubuntu 24.04) its start, one frame short, which cost a 5 s take its fifth
    filmstrip frame. The decoder is only asked when no packet has a time."""
    end = _last_packet_end(path)
    if end > 0:
        return end
    proc = subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_FILMSTRIP,
    )
    times = _DECODE_TIME_RE.findall(proc.stderr or "")
    if not times:
        return 0.0
    h, m, sec = times[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def _last_packet_end(path: Path) -> float:
    """The latest pts + duration over the first video stream's packets, in
    seconds, or 0.0 when ffprobe fails or no packet carries both."""
    try:
        proc = subprocess.run(
            [
                ffprobe_executable(),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "packet=pts_time,duration_time",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_FILMSTRIP,
        )
    except (subprocess.SubprocessError, OSError):
        return 0.0
    if proc.returncode != 0:
        return 0.0
    end = 0.0
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        try:
            value = float(parts[0]) + float(parts[1])
        except ValueError:
            continue
        if math.isfinite(value):
            end = max(end, value)
    # WebM keeps whole milliseconds, so 15 fps frames last 0.066 s and a 5 s
    # take ends at 4.999: rounded to 10 ms, it ends at 5.0 again.
    return round(end, 2)


def build_filmstrip(camera: Path, folder: Path) -> dict[str, Any]:
    """The take's filmstrip, built once and kept beside the camera file
    until the camera file changes: {interval, count, frame_width,
    frame_height, duration}. One JPEG row of `count` frames, one every
    `interval` seconds from the first frame."""
    image = folder / _FILMSTRIP_IMAGE
    meta_path = folder / _FILMSTRIP_META
    with _filmstrip_locks_guard:
        lock = _filmstrip_locks.setdefault(str(folder), threading.Lock())
    with lock:
        cam_mtime = camera.stat().st_mtime
        if image.is_file() and meta_path.is_file() and image.stat().st_mtime >= cam_mtime:
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("unreadable %s, building it again", meta_path)
        duration, width, height = _probe_video(camera)
        if duration <= 0 or width <= 0 or height <= 0:
            raise RuntimeError("the camera file has no readable video")
        frame_h = FILMSTRIP_FRAME_HEIGHT
        frame_w = max(2, round(width * frame_h / height / 2) * 2)
        interval = max(1.0, duration / FILMSTRIP_MAX_FRAMES)
        # Whole intervals only: ffmpeg gives no frame for a last part-interval,
        # and the tile filter would pad its place in black.
        count = max(1, min(FILMSTRIP_MAX_FRAMES, math.floor(duration / interval)))
        tmp = folder / f".{_FILMSTRIP_IMAGE}.{uuid.uuid4().hex}.jpg"
        try:
            proc = subprocess.run(
                [
                    ffmpeg_executable(),
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(camera),
                    "-vf",
                    f"fps=1/{interval:.4f},scale={frame_w}:{frame_h},tile={count}x1",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "5",
                    str(tmp),
                ],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_FILMSTRIP,
            )
            if proc.returncode != 0 or not tmp.is_file():
                raise RuntimeError(proc.stderr.strip()[-400:] or "ffmpeg failed")
            os.replace(tmp, image)
        finally:
            tmp.unlink(missing_ok=True)
        meta = {
            "interval": round(interval, 4),
            "count": count,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "duration": round(duration, 4),
        }
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        return meta


@router.get("/{job_id}/takes/{take_id}/filmstrip")
async def get_take_filmstrip(job_id: str, take_id: str) -> dict[str, Any]:
    """How to draw the take's frames along its lane: the filmstrip's layout
    and where it is (filmstrip.jpg). Built on first request."""
    _job, folder, _entry_ = _finished_or_404(job_id, take_id)
    camera = camera_file(folder)
    if camera is None or not camera.resolve().is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="take has no camera")
    try:
        meta = await asyncio.to_thread(build_filmstrip, camera.resolve(), folder)
    except subprocess.TimeoutExpired as err:
        logger.warning("[%s] filmstrip for %s timed out", job_id, take_id)
        raise HTTPException(status_code=504, detail="the filmstrip took too long") from err
    except (OSError, RuntimeError, ValueError):
        logger.exception("[%s] filmstrip for %s failed", job_id, take_id)
        raise HTTPException(status_code=500, detail="could not read the camera file") from None
    return {**meta, "url": f"/api/jobs/{job_id}/takes/{take_id}/filmstrip.jpg"}


@router.get("/{job_id}/takes/{take_id}/filmstrip.jpg", response_model=None)
async def get_take_filmstrip_image(job_id: str, take_id: str) -> FileResponse:
    _job, folder, _entry_ = _finished_or_404(job_id, take_id)
    path = (folder / _FILMSTRIP_IMAGE).resolve()
    if not path.is_relative_to(JOBS_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="no filmstrip yet")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# Video render


@dataclass
class _GpuPlan:
    """What a GPU attempt needs: the command for (trim_db, alt_aac, encoder,
    variant), and the geometry the encoder is probed and checked at."""

    build: Any  # Callable[[float, bool, str, int], list[str]]
    width: int
    height: int
    fps: int


@dataclass
class _RenderRun:
    status: str = "queued"  # queued | running | done | error
    progress: float = 0.0
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    # The encoder that made (or is making) the file, and why a GPU attempt
    # was given up for libx264 ("exit", "stall", "timeout", "invalid-<check>"),
    # else None. `gpu` is None for a render that never tries the GPU.
    encoder: str | None = None
    variant: int = 0
    fallback: str | None = None
    gpu: _GpuPlan | None = None


_renders: dict[tuple[str, str], _RenderRun] = {}
_render_tasks: set[asyncio.Task] = set()


def _render_busy(job_id: str, take_id: str) -> bool:
    run = _renders.get((job_id, take_id))
    return run is not None and run.status in ("queued", "running")


def _stop_run(run: _RenderRun) -> None:
    run.cancel.set()
    proc = run.proc
    if proc is not None and proc.poll() is None:
        proc.kill()


def _stop_and_wait(run: _RenderRun, timeout: float = 10.0) -> None:
    """_stop_run, then wait (blocking) until its ffmpeg has exited."""
    _stop_run(run)
    proc = run.proc
    if proc is not None:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("video render did not stop within %.0f s", timeout)


def cancel_all_renders() -> None:
    """Stop every render (the server is shutting down): no ffmpeg is left
    behind holding a take's files."""
    for key, run in list(_renders.items()):
        _stop_run(run)
        _renders.pop(key, None)


def cancel_job_renders(job_id: str) -> None:
    """Stop every render of a job, for the job's deletion: its folder is
    about to go, and on Windows a running ffmpeg would hold files open."""
    for (jid, tid), run in list(_renders.items()):
        if jid == job_id:
            _stop_run(run)
            _renders.pop((jid, tid), None)


def _render_view(job_id: str, take_id: str, folder: Path) -> dict[str, Any]:
    run = _renders.get((job_id, take_id))
    url = f"/api/jobs/{job_id}/takes/{take_id}/render.mp4"
    # What made the file: known while this process remembers the run (the
    # Streamer's "Encoded with" note reads it right after the export).
    made = {
        "encoder": run.encoder if run is not None else None,
        "fallback": bool(run is not None and run.fallback),
        "fallback_reason": run.fallback if run is not None else None,
    }
    if run is not None and run.status != "done":
        return {
            "status": run.status,
            "progress": run.progress,
            "error": run.error,
            "url": None,
            **made,
        }
    if (folder / _RENDER_FILE).is_file():
        return {"status": "done", "progress": 1.0, "error": None, "url": url, **made}
    return {"status": "none", "progress": 0.0, "error": None, "url": None, **made}


class RenderBody(BaseModel):
    """Single-tile subset of the layout descriptor (06-video-recording 5.3)."""

    model_config = ConfigDict(extra="forbid")
    stems: list[str] = Field(default_factory=list, max_length=_RENDER_MAX_LANES)
    gains: list[float] = Field(default_factory=list, max_length=_RENDER_MAX_LANES)
    pitches: list[int] | None = Field(default=None, max_length=_RENDER_MAX_LANES)
    take_gain: float = Field(default=1.0, ge=0, le=4, allow_inf_nan=False)
    camera_offset_ms: float | None = _finite(-_CAMERA_OFFSET_MAX_MS, _CAMERA_OFFSET_MAX_MS)
    canvas: Literal["16:9", "9:16", "1:1"] = "16:9"
    # "fill": the picture is already the canvas's shape (Streamer's scene).
    fit: Literal["cover", "contain", "fill"] = "cover"
    mirror: StrictBool = False
    # "take": from the take's count-in to where it stopped; "song": all of it;
    # "range": range_in to range_out (Streamer's trim), inside the take's span.
    span: Literal["take", "song", "range"] = "take"
    range_in: float | None = _finite(0, stems_api.MAX_TRIM_SECONDS)
    range_out: float | None = _finite(0, stems_api.MAX_TRIM_SECONDS)
    # The output: the canvas's short side and the frame rate.
    size: Literal["720", "1080"] = "1080"
    fps: Literal[24, 30, 60] = 30
    # The click as one more lane (the export's default click: the song's grid,
    # every beat, accents by the detected bars).
    click: StrictBool = False
    # Its sound (#805): a preset id from static/js/clickSounds.js, checked
    # against that whitelist in the handler (422 for any other).
    click_sound: str = Field(default=DEFAULT_CLICK_SOUND, max_length=32)
    # Streamer Mode's "I play": the stem the musician played, left out of the
    # backing exactly as it was left out of what they heard. None: the whole song.
    remove_stem: Literal["vocals", "drums", "bass", "guitar", "piano", "other"] | None = None
    # The mode's own lanes, the export's lanes= value: every base stem shown as
    # a lane, muted or not, so Original is rebuilt from every other stem as
    # playback builds it. None: Original is the job's own file, as before.
    lanes: list[str] | None = Field(default=None, max_length=6)
    # The studio EQ, the export's fx= value (app/pipeline/fx_filters.py). The
    # take gets its track's EQ; "" means no EQ.
    fx: str = Field(default="", max_length=stems_api.FX_PARAM_MAX_CHARS)
    # The master, the export's master= value (app/pipeline/mastering.py);
    # "" means no master.
    master: str = Field(default="", max_length=stems_api.MASTER_PARAM_MAX_CHARS)

    @field_validator("stems")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        for name in v:
            if len(name) > 32:
                raise ValueError("unknown stem requested")
        return v


class _RenderAudio(NamedTuple):
    lanes: list[AudioLane]
    take_fx: str
    master_fx: str
    names: list[str]  # the backing lanes' mixer names (for the master's cache key)


def _render_lanes(
    job_id: str,
    take_id: str,
    body: RenderBody,
    take_audio: Path,
    track_id: str | None = None,
) -> _RenderAudio:
    """Validate and resolve the backing lanes with the export's own rules,
    with each lane's EQ, the take's (its track's EQ) and the master's."""
    fx_req = stems_api.parse_fx_query(body.fx)
    lanes = _backing_lanes(job_id, take_id, body)
    # The take is one more input, named by its track so it gets that EQ.
    names = [name for _, name in lanes] + [track_id or ""]
    paths = [lane.path for lane, _ in lanes] + [take_audio]
    render_fx = stems_api.build_render_fx(names, paths, fx_req)
    out = [lane._replace(fx_filter=render_fx.lanes[i]) for i, (lane, _) in enumerate(lanes)]
    return _RenderAudio(out, render_fx.lanes[-1], render_fx.master, names[:-1])


def removed_stems(remove: str | None) -> frozenset[str]:
    """The lanes and components a removed stem takes out: Voice is the
    vocals stem and, after a lead/backing split, both of its halves."""
    if not remove:
        return frozenset()
    if remove == "vocals":
        return frozenset({"vocals", "lead_vocals", "backing_vocals"})
    return frozenset({remove})


def _backing_lanes(job_id: str, take_id: str, body: RenderBody) -> list[tuple[AudioLane, str]]:
    """The backing lanes with their mixer lane names."""
    stems_api.check_click_sound(body.click_sound)
    if len(body.gains) != len(body.stems):
        raise HTTPException(status_code=422, detail="stems and gains must be equal length")
    if any(not (0 <= g <= stems_api._MIXDOWN_MAX_GAIN) for g in body.gains):
        raise HTTPException(status_code=422, detail="gain out of range")
    active = {t["id"]: t.get("active_take_id") for t in registry_get(job_id).tracks}
    if take_id in body.stems or any(active.get(n) == take_id for n in body.stems):
        raise HTTPException(status_code=422, detail="the take is already in the video")
    if not body.stems:
        return []
    if len(set(body.stems)) != len(body.stems):
        raise HTTPException(status_code=422, detail="a lane is listed twice")
    names, gains = stems_api._parse_lane_gains(
        ",".join(body.stems), ",".join(f"{g:.6f}" for g in body.gains)
    )
    pitches = stems_api._parse_pitches(
        ",".join(str(p) for p in body.pitches) if body.pitches else "", names
    )
    if any(pitches) and not stems_api._rubberband_available():
        raise HTTPException(
            status_code=422, detail="this ffmpeg build cannot transpose (no rubberband filter)"
        )
    lane_set = None if body.lanes is None else stems_api.parse_lanes_param(",".join(body.lanes))
    lanes, _unpitched = stems_api._expand_render_lanes(
        job_id, names, gains, pitches, removed_stems(body.remove_stem), lanes=lane_set
    )
    return [
        (AudioLane(lane.path, lane.gain, stems_api._pitch_filter(lane.pitch)), lane.name)
        for lane in lanes
    ]


# The shortest take-span video: a take stopped right after its count-in
# still gets a second of picture rather than an empty file.
_SPAN_MIN_SEC = 1.0


def _render_span(
    sidecar: dict[str, Any],
    song_sec: float,
    span: str,
    range_in: float | None = None,
    range_out: float | None = None,
) -> tuple[float, float]:
    """(start, duration) of the video in song time.

    "range" is range_in to range_out cut to the take's own span (a trim wider
    than what was recorded would only add black), at least a second long.

    The take's span starts where its count-in started (start_song_sec minus
    count_in_sec; the take's first sample when an older sidecar has neither)
    and ends at its last sample, nudge included, both clamped to the song:
    before song 0 there is no backing and audio.wav starts at 0."""
    if span == "song" or song_sec <= 0:
        return 0.0, song_sec
    if span == "range":
        t_start, t_len = _render_span(sidecar, song_sec, "take")
        t_end = t_start + t_len
        lo = max(t_start, float(range_in or 0.0))
        hi = min(t_end, float(range_out if range_out is not None else song_sec))
        # A range past the take's end keeps its last second rather than black.
        lo = min(lo, max(t_start, t_end - _SPAN_MIN_SEC))
        hi = min(song_sec, max(hi, lo + _SPAN_MIN_SEC))
        lo = max(0.0, min(lo, hi - _SPAN_MIN_SEC))
        return lo, hi - lo
    timeline = sidecar.get("timeline") or {}
    placement = _placement_sec(sidecar)
    first = float(timeline.get("first_sample_song_sec") or 0.0)
    start_song = timeline.get("start_song_sec")
    if isinstance(start_song, (int, float)):
        start = float(start_song) - float(timeline.get("count_in_sec") or 0.0)
    else:
        start = placement
    end_song = timeline.get("end_song_sec")
    end = placement + (float(end_song) - first) if isinstance(end_song, (int, float)) else song_sec
    start = min(max(0.0, start), song_sec)
    end = min(max(start, end), song_sec)
    if end - start < _SPAN_MIN_SEC:
        end = min(song_sec, start + _SPAN_MIN_SEC)
        start = max(0.0, end - _SPAN_MIN_SEC)
    return start, end - start


class _MasterRetry(NamedTuple):
    """How to render a mastered video again when its AAC decoded over the
    ceiling: the command for a (trim_db, alt_aac) pair, and the ceiling."""

    build: Any  # Callable[[float, bool], list[str]]
    ceiling: float


async def _verify_master(render, retry: _MasterRetry, dest: Path, job_id: str) -> None:
    """Decode the rendered video's audio and render again, trimmed or with
    the alternative AAC coder, while it is over the master's ceiling (the
    rule of stems._render_master_verified). `render(trim_db, alt_aac)` makes
    the file again with the render's encoder (and its CPU fallback)."""
    trim, alt = 0.0, False
    for attempt in range(stems_api.LOSSY_RETRIES + 1):
        try:
            out = await stems_api.measure_file(str(dest))
        except stems_api.MeasureError as exc:
            logger.warning("[%s] mastered video not measured: %s", job_id, exc)
            return
        if out.tp is None or out.tp <= retry.ceiling:
            return
        if attempt == stems_api.LOSSY_RETRIES:
            logger.warning(
                "[%s] mastered video still %.2f dB over its ceiling",
                job_id,
                out.tp - retry.ceiling,
            )
            return
        if not alt:
            alt = True
        else:
            trim += out.tp - retry.ceiling + stems_api.LOSSY_RETRY_MARGIN_DB
        await render(trim, alt)


async def _render_task(
    run: _RenderRun,
    job: Job,
    cmd: list[str],
    dest: Path,
    duration: float,
    retry: _MasterRetry | None = None,
) -> None:
    def cancelled() -> bool:
        return run.cancel.is_set() or registry_get(job.id) is not job

    def report(share: float) -> None:
        run.progress = share

    def on_proc(proc: subprocess.Popen | None) -> None:
        run.proc = proc

    gpu = run.gpu
    try:
        # Behind the pipeline lock like every other heavy on-demand run: an
        # x264 encode and a Demucs pass on the same CPU slow each other down
        # for no gain. "queued" until an import in progress has finished.
        async with _pipeline_lock:
            if cancelled():
                raise RenderCancelled()
            run.status = "running"
            run.encoder = VIDEO_CPU_ENCODER
            if gpu is not None:
                # May run a 2 s test encode (cached afterwards, see
                # video_encoder.probe); never raises. A cancel stops it (#790).
                choice = await asyncio.to_thread(
                    video_encoder.choose, gpu.width, gpu.height, gpu.fps, cancelled
                )
                if cancelled():
                    raise RenderCancelled()
                run.encoder, run.variant = choice.encoder, choice.variant
                logger.info(
                    "[%s] video render encoder %s%s",
                    job.id,
                    choice.encoder,
                    f" ({choice.reason})" if choice.reason else "",
                )

            async def run_cmd(c: list[str], check: Any = None, stall: float | None = None) -> None:
                await asyncio.to_thread(
                    run_video_render,
                    c,
                    dest,
                    duration,
                    cancelled=cancelled,
                    report=report,
                    on_proc=on_proc,
                    timeout=TIMEOUT_VIDEO_RENDER,
                    check=check,
                    stall_timeout=stall,
                )

            async def render(trim: float = 0.0, alt: bool = False) -> None:
                """One render at (trim, alt): on the GPU encoder while it
                holds, else (or after it failed) the CPU command, which is
                exactly the one this render had before the GPU path."""
                if gpu is not None and run.encoder != VIDEO_CPU_ENCODER:
                    encoder = run.encoder

                    def check(path: Path) -> None:
                        video_encoder.validate_output(
                            path,
                            gpu.width,
                            gpu.height,
                            gpu.fps,
                            duration,
                            encoder,
                            cancelled=cancelled,
                            on_proc=on_proc,
                        )

                    try:
                        await run_cmd(
                            gpu.build(trim, alt, encoder, run.variant),
                            check=check,
                            stall=TIMEOUT_VIDEO_GPU_STALL,
                        )
                        return
                    except TakeRenderError as exc:
                        # The temp file is already gone (run_video_render).
                        logger.warning(
                            "[%s] GPU render with %s failed (%s), rendering on the CPU: %s",
                            job.id,
                            encoder,
                            exc.code,
                            exc,
                        )
                        # Only the encoder's own faults count toward turning
                        # it off: never a full disk or a missing file (#791).
                        if video_encoder.blames_encoder(encoder, exc.code, exc.stderr):
                            await asyncio.to_thread(video_encoder.record_failure, encoder, exc.code)
                        else:
                            logger.info("[%s] %s not held against the encoder", job.id, exc.code)
                        run.encoder, run.fallback = VIDEO_CPU_ENCODER, exc.code
                        run.progress = 0.0
                if trim == 0.0 and not alt:
                    await run_cmd(cmd)
                elif retry is not None:
                    await run_cmd(retry.build(trim, alt))

            await render()
            if retry is not None:
                await _verify_master(render, retry, dest, job.id)
        run.status = "done"
        run.progress = 1.0
    except RenderCancelled:
        run.status = "none"
    except TakeRenderError:
        logger.warning("[%s] video render failed", job.id, exc_info=True)
        run.status = "error"
        run.error = "video render failed"
    except Exception:
        logger.exception("[%s] video render failed", job.id)
        run.status = "error"
        run.error = "video render failed"


async def _prepare_render(
    job_id: str,
    take_id: str,
    folder: Path,
    body: RenderBody,
    camera: Path,
    audio: _RenderAudio,
) -> tuple[list[str], float, _MasterRetry | None, list[AudioLane], float, _GpuPlan]:
    """The render's command, length, master retry, lanes, start and GPU plan,
    from the take's sidecar and the request (the slow part of start_render).
    `cmd` is the CPU (libx264) command; the GPU plan builds the others."""
    sidecar = await asyncio.to_thread(_read_json, folder / _SIDECAR_FILE)
    video = sidecar.get("video") or {}
    offset_ms = (
        body.camera_offset_ms
        if body.camera_offset_ms is not None
        else float(video.get("cameraOffsetMs") or 0.0)
    )
    camera_start = (
        _placement_sec(sidecar) + float(video.get("firstFrameAtMs") or 0.0) / 1000.0
    ) - offset_ms / 1000.0
    if body.fit == "fill":
        camera_start = _scene_start_sec(sidecar, video, offset_ms)
    frames = await asyncio.to_thread(_song_frames, job_id)
    song_sec = frames / TAKE_RATE
    if body.span == "range" and _range_outside_take(
        sidecar, song_sec, body.range_in, body.range_out
    ):
        # Nothing recorded there: refused, never clamped to another second.
        raise HTTPException(status_code=422, detail="the trim is outside what this take recorded")
    start, duration = _render_span(sidecar, song_sec, body.span, body.range_in, body.range_out)
    lanes = audio.lanes
    if body.click:
        click = await asyncio.to_thread(
            stems_api._click_lane,
            job_id,
            True,
            1.0,
            -1,
            RENDER_CLICK_GAIN,
            sound=body.click_sound,
        )
        if click is None:
            # No beat grid: the render goes ahead without it, as the export does.
            logger.info("[%s] take %s render: no click (no beat grid)", job_id, take_id)
        else:
            # The mono click to stereo at unity, as the export and the app (#833).
            lanes = [*lanes, AudioLane(click.path, click.gain, "", f"{CLICK_TO_STEREO},")]
    # The master, measured on the whole song with the take as one more lane
    # (audio.wav is aligned to song 0), replaces the lossy export limiter.
    master_settings = stems_api.parse_master_query(body.master)
    master_chain = ""
    retry: _MasterRetry | None = None
    if master_settings is not None:
        take_audio = folder / "audio.wav"
        st = await asyncio.to_thread(take_audio.stat)
        plan, rate_in = await stems_api.master_plan(
            job_id,
            [lane.path for lane in lanes] + [take_audio],
            [f"{lane.fx_filter}{lane.pitch_filter}volume={lane.gain:.6f}" for lane in lanes]
            + [f"{audio.take_fx}volume={body.take_gain:.6f}"],
            audio.master_fx,
            master_settings,
            True,
            [
                *stems_api._take_versions(job_id, audio.names),
                f"{take_id}@{st.st_size}:{st.st_mtime_ns}",
                # A backing without a stem is another mix to measure.
                *([f"without:{body.remove_stem}"] if body.remove_stem else []),
            ],
        )
        master_chain = stems_api.plan_chain(plan, rate_in)

    def make_cmd(
        trim_db: float = 0.0,
        alt_aac: bool = False,
        encoder: str = VIDEO_CPU_ENCODER,
        variant: int = 0,
    ) -> list[str]:
        return build_video_render_cmd(
            lanes,
            folder / "audio.wav",
            body.take_gain,
            camera,
            camera_start,
            duration,
            body.canvas,
            body.fit,
            body.mirror,
            get_export_sample_rate(),
            start_sec=start,
            take_fx=audio.take_fx,
            master_fx=audio.master_fx,
            master_chain=master_chain,
            trim_db=trim_db,
            alt_aac=alt_aac,
            size=body.size,
            fps=body.fps,
            encoder=encoder,
            encoder_variant=variant,
        )

    cmd = make_cmd()
    if master_settings is not None:
        retry = _MasterRetry(make_cmd, master_settings.ceiling)
    width, height = canvas_size(body.canvas, body.size)
    gpu = _GpuPlan(make_cmd, width, height, body.fps)
    return cmd, duration, retry, lanes, start, gpu


@router.post("/{job_id}/takes/{take_id}/render", status_code=202)
async def start_render(job_id: str, take_id: str, request: Request) -> JSONResponse:
    """Render the take's camera over the backing mix plus the take to MP4,
    in the background: 202, then GET .../render follows it."""
    job, folder, entry = _finished_or_404(job_id, take_id)
    body: RenderBody = await _parse_body(request, RenderBody, empty_ok=True)
    camera = await asyncio.to_thread(camera_file, folder)
    if camera is None:
        raise HTTPException(status_code=404, detail="take has no camera")
    if not await asyncio.to_thread(libx264_available):
        raise HTTPException(
            status_code=422, detail="this ffmpeg build cannot encode H.264 video (no libx264)"
        )
    if not await asyncio.to_thread(aac_available):
        raise HTTPException(
            status_code=422, detail="this ffmpeg build cannot encode H.264 video (no AAC encoder)"
        )
    if body.span == "range" and (
        body.range_in is None or body.range_out is None or body.range_out <= body.range_in
    ):
        raise HTTPException(status_code=422, detail="range needs range_in before range_out")
    audio = await asyncio.to_thread(
        _render_lanes, job_id, take_id, body, folder / "audio.wav", entry.get("track_id") or None
    )
    key = (job_id, take_id)
    run = _RenderRun()
    async with _lock_for(job_id, take_id):
        # Under the take's lock: a delete of the take (or of its job) that ran
        # while the body was read is seen here, not by ffmpeg later.
        job, folder, entry = _finished_or_404(job_id, take_id)
        if _render_busy(job_id, take_id):
            raise HTTPException(status_code=409, detail="video render already running")
        # The last render is set aside first: a cancel then reports "none",
        # never "done" with an older file, and the new file has its name free.
        # It is only deleted once the new render is accepted (#789).
        kept = await asyncio.to_thread(_set_aside_old_render, folder)
        if kept is False:
            raise HTTPException(
                status_code=409,
                detail="the last video is still being saved: try again when it ends",
            )
        # Registered (queued) before the lock is let go, so a second POST
        # gets the same 409, and Cancel (or a delete) can reach it.
        _renders[key] = run
    try:
        cmd, duration, retry, lanes, start, run.gpu = await _prepare_render(
            job_id, take_id, folder, body, camera, audio
        )
    except BaseException:
        # Refused (or failed) before anything rendered: the earlier video
        # comes back, unless a cancel, a delete or an edit took the run.
        if _renders.get(key) is run:
            if kept is not None and not run.cancel.is_set():
                await asyncio.to_thread(_restore_old_render, kept, folder)
            _renders.pop(key, None)
        elif kept is not None:
            await asyncio.to_thread(_discard_old_render, kept)
        raise
    if kept is not None:
        await asyncio.to_thread(_discard_old_render, kept)
    if run.cancel.is_set():
        # Cancelled while it was being prepared.
        return JSONResponse(_render_view(job_id, take_id, folder), status_code=202)
    task = asyncio.create_task(_render_task(run, job, cmd, folder / _RENDER_FILE, duration, retry))
    _render_tasks.add(task)
    task.add_done_callback(_render_tasks.discard)
    width, height = canvas_size(body.canvas, body.size)
    logger.info(
        "[%s] take %s video render queued (%d lanes, %s span %.3f s + %.3f s, %dx%d at %d fps)",
        job_id,
        take_id,
        len(lanes),
        body.span,
        start,
        duration,
        width,
        height,
        body.fps,
    )
    return JSONResponse(_render_view(job_id, take_id, folder), status_code=202)


def _range_outside_take(
    sidecar: dict[str, Any], song_sec: float, range_in: float | None, range_out: float | None
) -> bool:
    """Whether a trim lies wholly before or after the take's own span."""
    t_start, t_len = _render_span(sidecar, song_sec, "take")
    return float(range_out or 0.0) <= t_start or float(range_in or 0.0) >= t_start + t_len


def _set_aside_old_render(folder: Path) -> Path | None | Literal[False]:
    """Move the take's last render out of its name, to a temp name the
    startup sweep removes. The new path, None when there was none, False
    when it cannot move (Windows: a download of it still has it open)."""
    src = folder / _RENDER_FILE
    kept = folder / f".{_RENDER_FILE}.{uuid.uuid4().hex}.tmp.mp4"
    try:
        os.replace(src, kept)
    except FileNotFoundError:
        return None
    except PermissionError:
        return False
    return kept


def _restore_old_render(kept: Path, folder: Path) -> None:
    """Put a set-aside render back under its name (the new render was
    refused before it started)."""
    try:
        os.replace(kept, folder / _RENDER_FILE)
    except OSError:
        logger.warning("earlier video not restored: %s", kept.name)
        _discard_old_render(kept)


def _discard_old_render(kept: Path) -> None:
    """Delete a set-aside render (the new one was accepted, or cancelled).
    One that cannot go now is removed by the startup sweep."""
    try:
        kept.unlink(missing_ok=True)
    except OSError:
        logger.warning("earlier video not removed: %s", kept.name)


@router.get("/{job_id}/takes/{take_id}/render")
async def get_render(job_id: str, take_id: str) -> dict[str, Any]:
    _job, folder, _entry_ = _finished_or_404(job_id, take_id)
    return _render_view(job_id, take_id, folder)


@router.post("/{job_id}/takes/{take_id}/render/cancel")
async def cancel_render(job_id: str, take_id: str) -> dict[str, Any]:
    _job, folder, _entry_ = _finished_or_404(job_id, take_id)
    run = _renders.pop((job_id, take_id), None)
    if run is not None:
        _stop_run(run)
    return _render_view(job_id, take_id, folder)


# What a download name may not contain on any file system.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def _requested_name(name: str | None) -> str | None:
    """A download name the page asked for (Streamer's "<Title> - <Artist>
    (Streamer).mp4"), made safe, or None to use the default. Only ever a
    Content-Disposition value: it names nothing on the server.

    Control characters and Unicode format characters (category Cf: the bidi
    overrides that can make "gpj.exe" read as "exe.jpg", zero-width marks)
    go, then the file-system characters, then spaces and dots at the ends."""
    if not name:
        return None
    visible = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf"))
    clean = re.sub(r"\s+", " ", _UNSAFE_NAME.sub(" ", visible)).strip().strip(".").strip()
    if not clean.lower().endswith(".mp4") or len(clean) <= 4 or len(clean) > 160:
        return None
    return clean


@router.get("/{job_id}/takes/{take_id}/render.mp4", response_model=None)
async def get_render_file(
    job_id: str, take_id: str, name: str | None = Query(default=None, max_length=200)
) -> FileResponse:
    job, folder, entry = _finished_or_404(job_id, take_id)
    if _render_busy(job_id, take_id):
        # The file is about to be replaced: never half of one, or the old one.
        raise HTTPException(status_code=409, detail="video render running")
    path = (folder / _RENDER_FILE).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="no video render")
    filename = _requested_name(name) or _download_name(job, entry["label"], "mp4")
    return FileResponse(path, media_type="video/mp4", filename=filename)
