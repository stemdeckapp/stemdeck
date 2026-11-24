"""Recorded takes: where they live and what the job record says about them.

A take is a folder, jobs/<job_id>/takes/<take_id>/, holding

    audio.part / camera.part   bytes received so far, while recording
    parts.json                 upload state: next sequence number and size per kind
    audio.raw.wav              exactly what was captured (float32, capture rate)
    audio.wav                  the playable take, in the stems' own format
    camera.webm | camera.mp4   the raw camera recording, when there is one
    take.json                  the sidecar
    render.mp4                 the rendered video, once asked for

Takes belong to recording tracks (takes/tracks.json): a track is one mixer
lane that owns takes and plays its active one.

It lives and dies with the job folder: deleting the job removes it, putting
the job in the Trash leaves it alone, as it does every other file.

Kept free of FastAPI and ffmpeg so the registry can rebuild entries from disk
after a restart without importing the API layer.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from app.core.config import JOB_ID_RE, JOBS_DIR, TAKE_ID_RE, TAKES_DIRNAME, TRACK_ID_RE, TRACKS_FILE

logger = logging.getLogger("stemdeck.takes")

CAMERA_EXTS = ("webm", "mp4")


def take_dir(job_id: str, take_id: str, jobs_dir: Path | None = None) -> Path | None:
    """The take's folder, resolved, or None when either id is malformed or
    the path would leave the jobs directory. Does not check that it exists."""
    if not JOB_ID_RE.match(job_id) or not TAKE_ID_RE.match(take_id):
        return None
    root = (jobs_dir or JOBS_DIR).resolve()
    path = (root / job_id / TAKES_DIRNAME / take_id).resolve()
    if not path.is_relative_to(root):
        return None
    return path


def camera_file(folder: Path) -> Path | None:
    """The take's camera recording, whichever container it turned out to be."""
    for ext in CAMERA_EXTS:
        path = folder / f"camera.{ext}"
        if path.is_file():
            return path
    return None


# ---------------------------------------------------------------------------
# Picture placement: the one rule the review and the export share.
#
# The truth clock is the take's audio timeline: raw audio frame 0 of
# audio.raw.wav, placed in song time at placement_sec(). The camera file's
# t=0 is its first video frame (the render restarts its timestamps there).
# video.sync.offsetMs is where that t=0 sits on the audio timeline, measured
# from the camera file's own sound (app/pipeline/av_sync.py) or, when that
# could not be done, the browser's estimate (video.firstFrameAtMs). Then
#
#     picture_offset_ms = offsetMs - drawAgeMs + nudgeMs - cameraOffsetMs
#     camera_start_sec  = placement_sec + picture_offset_ms / 1000
#
# drawAgeMs (video.drawAgeMs, Performance only) is how old the camera frame
# was when the canvas drew it: the recorded frames carry the draw time, the
# picture in them was captured that much earlier. nudgeMs is the Performance
# export's manual correction (positive: the picture later); cameraOffsetMs
# the studio's older one (positive: earlier).
# camera_start_sec is song time as heard: a player drawing the picture
# against AudioContext.currentTime shows it early by the output latency.

SYNC_NUDGE_MAX_MS = 2000.0
DRAW_AGE_MAX_MS = 1000.0
SYNC_REASONS = (
    "ok",
    "no-camera-audio",
    "no-camera-mic",
    "camera-mic-denied",
    "camera-mic-not-asked",
    "camera-mic-failed",
    "silent",
    "low-confidence",
    "measure-failed",
    "legacy",
)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if math.isfinite(n) else default


def placement_sec(sidecar: dict[str, Any]) -> float:
    """Song time of raw audio frame 0: the browser's placement (round trip
    folded in) less the user's nudge."""
    timeline = sidecar.get("timeline") or {}
    latency = sidecar.get("latency") or {}
    return _finite(timeline.get("first_sample_song_sec")) - (
        _finite(latency.get("user_nudge_ms")) / 1000.0
    )


def sync_block(
    *,
    estimate_ms: float,
    measured_ms: float | None = None,
    confidence: float | None = None,
    reason: str = "legacy",
    nudge_ms: float = 0.0,
    measured_at: float | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The video.sync block written to take.json. A measured offset is used
    only with reason "ok"; anything else keeps the estimate."""
    measured = reason == "ok" and measured_ms is not None and math.isfinite(measured_ms)
    nudge = max(-SYNC_NUDGE_MAX_MS, min(SYNC_NUDGE_MAX_MS, _finite(nudge_ms)))
    base = "measured" if measured else "estimated"
    conf = None
    if confidence is not None and math.isfinite(confidence):
        conf = round(max(0.0, min(1.0, float(confidence))), 3)
    return {
        "v": 1,
        "source": "manual" if nudge else base,
        "base": base,
        "offsetMs": round(float(measured_ms) if measured else _finite(estimate_ms), 3),
        "estimateMs": round(_finite(estimate_ms), 3),
        "nudgeMs": round(nudge, 3),
        "confidence": conf,
        "reason": reason if reason in SYNC_REASONS else "measure-failed",
        "measuredAt": measured_at,
        "metrics": metrics or None,
    }


def video_sync(video: dict[str, Any]) -> dict[str, Any]:
    """The take's sync as it applies now, for any take with a camera: the
    stored block, checked, or for a take finished before sync was measured
    the browser's estimate (reason "legacy", never written back). Adds
    pictureOffsetMs, the sum the picture is placed with."""
    estimate = _finite(video.get("firstFrameAtMs"))
    stored = video.get("sync")
    if isinstance(stored, dict):
        measured = stored.get("base") == "measured"
        reason = stored.get("reason")
        if measured:
            reason = "ok"
        elif reason == "ok" or reason not in SYNC_REASONS:
            reason = "measure-failed"
        at = stored.get("measuredAt")
        metrics = stored.get("metrics")
        block = sync_block(
            estimate_ms=_finite(stored.get("estimateMs"), estimate),
            measured_ms=_finite(stored.get("offsetMs"), estimate) if measured else None,
            confidence=_finite(stored.get("confidence"), math.nan),
            reason=reason,
            nudge_ms=_finite(stored.get("nudgeMs")),
            measured_at=float(at) if isinstance(at, (int, float)) and math.isfinite(at) else None,
            metrics=metrics if isinstance(metrics, dict) else None,
        )
    else:
        block = sync_block(estimate_ms=estimate)
    draw_age = max(0.0, min(DRAW_AGE_MAX_MS, _finite(video.get("drawAgeMs"))))
    block["drawAgeMs"] = round(draw_age, 3)
    block["pictureOffsetMs"] = round(
        block["offsetMs"] - draw_age + block["nudgeMs"] - _finite(video.get("cameraOffsetMs")), 3
    )
    return block


def picture_offset_ms(video: dict[str, Any], camera_offset_ms: float | None = None) -> float:
    """offsetMs - drawAgeMs + nudgeMs - cameraOffsetMs; `camera_offset_ms` replaces the
    stored cameraOffsetMs (a render request's own value)."""
    if camera_offset_ms is not None:
        video = {**video, "cameraOffsetMs": camera_offset_ms}
    return video_sync(video)["pictureOffsetMs"]


def camera_start_sec(sidecar: dict[str, Any], camera_offset_ms: float | None = None) -> float:
    """Song time of the camera file's first frame: the review and every
    export (any fit) place the picture here."""
    video = sidecar.get("video") or {}
    return placement_sec(sidecar) + picture_offset_ms(video, camera_offset_ms) / 1000.0


def take_entry(job_id: str, sidecar: dict[str, Any], has_camera: bool) -> dict[str, Any]:
    """The record a finished take gets in job.takes, built from its sidecar.

    This is what the studio reads to add the lane, so it carries everything
    the lane needs and nothing it does not: the full sidecar stays in
    take.json."""
    take_id = str(sidecar["id"])
    latency = sidecar.get("latency") or {}
    video = sidecar.get("video") or {}
    camera_offset_ms = _finite(video.get("cameraOffsetMs")) if has_camera else 0.0
    base = f"/api/jobs/{job_id}/takes/{take_id}"
    track_id = str(sidecar.get("track_id") or "")
    return {
        "id": take_id,
        "track_id": track_id if TRACK_ID_RE.match(track_id) else None,
        "label": str(sidecar.get("label") or take_id),
        "created_at": float(sidecar.get("created_at") or 0.0),
        "url": f"{base}/audio.wav",
        "user_nudge_ms": float(latency.get("user_nudge_ms") or 0.0),
        "has_camera": has_camera,
        "camera_offset_ms": camera_offset_ms,
        "camera_url": f"{base}/camera" if has_camera else None,
        "camera_start_sec": round(camera_start_sec(sidecar), 4) if has_camera else None,
        "video_sync": video_sync(video) if has_camera else None,
    }


def recover_takes(job_dir: Path) -> list[dict[str, Any]]:
    """Entries for every finished take on disk, oldest first.

    Used when a job is rebuilt from its folder (registry restore without a
    record), so a crash between finishing a take and persisting the registry
    does not lose the lane: the files are the truth. A take counts as
    finished once it has both its sidecar and its playable audio."""
    root = job_dir / TAKES_DIRNAME
    if not root.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not TAKE_ID_RE.match(folder.name):
            continue
        sidecar_path = folder / "take.json"
        if not sidecar_path.is_file() or not (folder / "audio.wav").is_file():
            continue
        # A damaged take.json costs that take, not the song: this runs while
        # the whole library loads.
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["id"] = folder.name
            entry = take_entry(job_dir.name, sidecar, camera_file(folder) is not None)
        except (OSError, ArithmeticError, AttributeError, RecursionError, TypeError, ValueError):
            logger.warning("could not read %s", sidecar_path, exc_info=True)
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e["created_at"])
    return entries


# ---------------------------------------------------------------------------
# Tracks


def new_track_id(existing: set[str]) -> str:
    while True:
        track_id = "k" + secrets.token_hex(4)
        if track_id not in existing:
            return track_id


def make_track(
    track_id: str,
    name: str,
    order: int,
    input_: dict | None = None,
    kind: str = "audio",
) -> dict[str, Any]:
    """A new track. `kind` is "audio" or "video": a video track films its
    takes with the camera as well (static/js/video/videoTake.js)."""
    return {
        "id": track_id,
        "name": name,
        "order": order,
        "created_at": time.time(),
        "active_take_id": None,
        "input": input_,
        "kind": kind,
    }


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_tracks(job_dir: Path) -> list[dict[str, Any]] | None:
    """tracks.json, or None when there is none (or it cannot be read)."""
    path = job_dir / TAKES_DIRNAME / TRACKS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("could not read %s", path, exc_info=True)
        return None
    tracks = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(tracks, list):
        return None
    return [t for t in tracks if isinstance(t, dict) and TRACK_ID_RE.match(str(t.get("id")))]


def write_tracks(job_dir: Path, tracks: list[dict[str, Any]]) -> None:
    write_json_atomic(job_dir / TAKES_DIRNAME / TRACKS_FILE, {"v": 1, "tracks": tracks})


def normalise_tracks(
    tracks: list[dict[str, Any]], takes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Sorted by order, order renumbered 0..n-1, and every active_take_id a
    finished take of its own track (else its newest one, else None)."""
    out = sorted(
        (dict(t) for t in tracks),
        key=lambda t: (t.get("order", 0), t.get("created_at", 0.0)),
    )
    for i, track in enumerate(out):
        track["order"] = i
        own = [t for t in takes if t.get("track_id") == track["id"]]
        if track.get("active_take_id") not in {t["id"] for t in own}:
            newest = max(own, key=lambda t: t.get("created_at") or 0.0, default=None)
            track["active_take_id"] = newest["id"] if newest else None
    return out


def reconcile_tracks(
    job_dir: Path, tracks: list[dict[str, Any]] | None, takes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Bring a job's tracks and takes into agreement, on load.

    tracks.json wins over the registry record when both exist: it is written
    first. A finished take with no track (recorded before tracks existed, or
    whose track entry was lost) gets a track of its own, named after it and
    playing it, and its take.json learns the track id so the next load
    agrees. Returns (tracks, takes, changed)."""
    on_disk = read_tracks(job_dir)
    current = on_disk if on_disk is not None else [dict(t) for t in tracks or []]
    known = {t["id"] for t in current}
    new_takes: list[dict[str, Any]] = []
    changed = on_disk is None and bool(current)
    for entry in sorted(takes, key=lambda e: e.get("created_at") or 0.0):
        entry = dict(entry)
        if entry.get("track_id") not in known:
            track_id = new_track_id(known)
            known.add(track_id)
            track = make_track(track_id, str(entry.get("label") or "Track"), len(current))
            track["created_at"] = float(entry.get("created_at") or 0.0)
            track["active_take_id"] = entry["id"]
            current.append(track)
            entry["track_id"] = track_id
            _stamp_sidecar(job_dir, entry["id"], track_id)
            changed = True
        new_takes.append(entry)
    normalised = normalise_tracks(current, new_takes)
    if normalised != current:
        changed = True
    if changed:
        write_tracks(job_dir, normalised)
    return normalised, new_takes, changed


def _stamp_sidecar(job_dir: Path, take_id: str, track_id: str) -> None:
    path = job_dir / TAKES_DIRNAME / take_id / "take.json"
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(sidecar, dict):
            sidecar["track_id"] = track_id
            write_json_atomic(path, sidecar)
    except (OSError, ValueError):
        logger.warning("could not record the track of take %s", path, exc_info=True)
