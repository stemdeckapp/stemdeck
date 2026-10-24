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


def take_entry(job_id: str, sidecar: dict[str, Any], has_camera: bool) -> dict[str, Any]:
    """The record a finished take gets in job.takes, built from its sidecar.

    This is what the studio reads to add the lane, so it carries everything
    the lane needs and nothing it does not: the full sidecar stays in
    take.json."""
    take_id = str(sidecar["id"])
    latency = sidecar.get("latency") or {}
    video = sidecar.get("video") or {}
    timeline = sidecar.get("timeline") or {}
    camera_offset_ms = float(video.get("cameraOffsetMs") or 0.0) if has_camera else 0.0
    # Where the camera's first frame falls in the song, as the video render
    # places it (app/api/takes.py): the take's placement, the first frame's
    # time in the take, less the sync nudge.
    camera_start_sec = (
        float(timeline.get("first_sample_song_sec") or 0.0)
        - float(latency.get("user_nudge_ms") or 0.0) / 1000.0
        + float(video.get("firstFrameAtMs") or 0.0) / 1000.0
        - camera_offset_ms / 1000.0
    )
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
        "camera_start_sec": round(camera_start_sec, 4) if has_camera else None,
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
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read %s", sidecar_path, exc_info=True)
            continue
        if not isinstance(sidecar, dict):
            continue
        sidecar["id"] = folder.name
        entries.append(take_entry(job_dir.name, sidecar, camera_file(folder) is not None))
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
