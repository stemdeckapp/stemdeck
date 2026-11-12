"""Effects chain prints: a lane rendered by the browser through its effects
chain, stored in the cache folder and used by the export in place of the
stem (Advanced mode; static/js/fx/fxPrint.js).

Why a print and not an ffmpeg filter: the chain's delay, modulation and
saturation are Web Audio graphs. ffmpeg has look-alikes with other
topologies, and the export must sound like what was heard
(app/pipeline/fx_filters.py states the rule for the EQ). The browser renders
the lane with the same modules it plays, so the two cannot drift apart.

A print's id is a hash of everything that decides its audio: the job, the
lane, the exact source file (size and modification time, so a re-rendered
take misses), the source rate and the browser's canonical chain string. The
same chain on the same file is printed once and reused by every export.

Layout under ``root`` (CACHE_DIR / "fx-print"):

    <id>.wav     the print, 24-bit PCM, peak-normalised to -1 dBFS
    <id>.json    its metadata: job, lane, source identity, rate, frames, gain
    .<id>.part   an upload in progress (dot-prefixed: never served, swept)
    .<id>.state  its progress

Nothing here raises HTTPException: callers map PrintError to a safe status.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from app.core.config import (
    FX_PRINT_CACHE_MAX_BYTES,
    FX_PRINT_CACHE_MAX_FILES,
    FX_PRINT_SESSION_TTL_SEC,
    FX_PRINT_SLACK_SEC,
    FX_PRINT_TAIL_MAX_SEC,
)

logger = logging.getLogger("stemdeck.fx_prints")

PRINT_ID_RE = re.compile(r"^[0-9a-f]{40}\Z")
# Bump when what a print holds changes for the same inputs.
PRINT_VERSION = "1"
CHANNELS = 2
BYTES_PER_SAMPLE = 3
WAV_HEADER_BYTES = 44
# A print's gain undoes its normalisation: the chain can exceed full scale
# before the fader (Web Audio is float inside), and the file cannot. -1 dBFS
# with a 24-bit file is about 1.12; a +24 dB trim on a full-scale stem with a
# pedal on top stays far below this ceiling.
GAIN_MIN = 1e-6
GAIN_MAX = 64.0

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class PrintError(Exception):
    """A refusal with a message safe to show (no paths, no ids)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Source:
    """The file a lane plays, as a print is keyed by it."""

    path: Path
    identity: str
    rate: int
    frames: int


def source_info(path: Path, label: str) -> Source:
    """The source's identity (its label, size and modification time) and its
    rate and length, read from the file's header."""
    st = path.stat()
    info = sf.info(str(path))
    return Source(
        path=path,
        identity=f"{label}@{st.st_size}:{st.st_mtime_ns}",
        rate=int(info.samplerate),
        frames=int(info.frames),
    )


def print_id(job_id: str, lane: str, source: Source, chain: str) -> str:
    raw = "|".join(["fxp", PRINT_VERSION, job_id, lane, source.identity, str(source.rate), chain])
    # A cache key, not a security context.
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def max_frames(source: Source) -> int:
    return source.frames + (FX_PRINT_TAIL_MAX_SEC + FX_PRINT_SLACK_SEC) * source.rate


def max_bytes(source: Source) -> int:
    return WAV_HEADER_BYTES + max_frames(source) * CHANNELS * BYTES_PER_SAMPLE


def _paths(root: Path, pid: str) -> tuple[Path, Path, Path, Path]:
    # Every path here is built from the id, so the id is checked here too, not
    # only by the routes: a caller that skipped it cannot name a file outside
    # the print folder.
    if not isinstance(pid, str) or not PRINT_ID_RE.match(pid):
        raise PrintError(404, "no upload in progress")
    return (
        root / f"{pid}.wav",
        root / f"{pid}.json",
        root / f".{pid}.part",
        root / f".{pid}.state",
    )


def _lock(pid: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(pid)
        if lock is None:
            lock = threading.Lock()
            _locks[pid] = lock
        return lock


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.debug("unreadable fx print file %s", path.name, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _complete(root: Path, pid: str, job_id: str, lane: str, source: Source) -> dict | None:
    wav, meta_path, _part, _state = _paths(root, pid)
    meta = _read_json(meta_path)
    if not meta or not wav.is_file():
        return None
    if (
        meta.get("job_id") != job_id
        or meta.get("lane") != lane
        or meta.get("source") != source.identity
    ):
        return None
    return meta


def begin(root: Path, job_id: str, lane: str, source: Source, chain: str) -> dict:
    """Start (or resume) the upload of a print, or report that it exists."""
    root.mkdir(parents=True, exist_ok=True)
    sweep_sessions(root)
    pid = print_id(job_id, lane, source, chain)
    info = {
        "id": pid,
        "rate": source.rate,
        "frames": source.frames,
        "channels": CHANNELS,
        "max_bytes": max_bytes(source),
    }
    with _lock(pid):
        if _complete(root, pid, job_id, lane, source):
            return {**info, "done": True, "next_seq": 0}
        _wav, _meta, part, state_path = _paths(root, pid)
        state = _read_json(state_path)
        if not state or state.get("source") != source.identity or not part.is_file():
            state = {
                "job_id": job_id,
                "lane": lane,
                "source": source.identity,
                "rate": source.rate,
                "frames": source.frames,
                "next_seq": 0,
                "bytes": 0,
                "created": time.time(),
            }
            part.write_bytes(b"")
            _write_json(state_path, state)
        return {**info, "done": False, "next_seq": int(state["next_seq"])}


def append_part(root: Path, job_id: str, pid: str, seq: int, data: bytes) -> dict:
    """Append one part. seq must be the next one; repeating the last one is a
    no-op, so a retry after a lost response is safe."""
    _wav, _meta, part, state_path = _paths(root, pid)
    with _lock(pid):
        state = _read_json(state_path)
        if not state or state.get("job_id") != job_id or not part.is_file():
            raise PrintError(404, "no upload in progress")
        next_seq = int(state.get("next_seq", 0))
        received = int(state.get("bytes", 0))
        if next_seq > 0 and seq == next_seq - 1:
            return {"bytes": received, "duplicate": True}
        if seq != next_seq:
            raise PrintError(409, "unexpected part sequence")
        limit = (
            WAV_HEADER_BYTES
            + (
                int(state["frames"])
                + (FX_PRINT_TAIL_MAX_SEC + FX_PRINT_SLACK_SEC) * int(state["rate"])
            )
            * CHANNELS
            * BYTES_PER_SAMPLE
        )
        if received + len(data) > limit:
            raise PrintError(413, "print too large for this lane")
        with part.open("ab") as fh:
            # Cut back to what the state says arrived, so a crash between the
            # write and the state update cannot leave a part in twice.
            fh.truncate(received)
            fh.seek(received)
            fh.write(data)
        state["next_seq"] = seq + 1
        state["bytes"] = received + len(data)
        _write_json(state_path, state)
        return {"bytes": state["bytes"], "duplicate": False}


def finish(root: Path, job_id: str, pid: str, gain: float, source: Source, lane: str) -> dict:
    """Check the uploaded file and make it the print."""
    if not math.isfinite(gain) or not (GAIN_MIN <= gain <= GAIN_MAX):
        raise PrintError(422, "gain out of range")
    wav, meta_path, part, state_path = _paths(root, pid)
    with _lock(pid):
        existing = _complete(root, pid, job_id, lane, source)
        if existing:
            return existing
        state = _read_json(state_path)
        if (
            not state
            or state.get("job_id") != job_id
            or state.get("lane") != lane
            or not part.is_file()
        ):
            raise PrintError(404, "no upload in progress")
        if state.get("source") != source.identity:
            # The lane's file changed while the print uploaded.
            _discard(part, state_path)
            raise PrintError(409, "the lane changed while the print uploaded")
        try:
            with part.open("rb") as fh:
                head = fh.read(12)
            info = sf.info(str(part))
        except Exception:
            _discard(part, state_path)
            raise PrintError(422, "not a WAV file") from None
        if (
            head[:4] != b"RIFF"
            or head[8:12] != b"WAVE"
            or info.format != "WAV"
            or info.subtype != "PCM_24"
        ):
            _discard(part, state_path)
            raise PrintError(422, "a print must be a 24-bit PCM WAV")
        if int(info.samplerate) != source.rate or int(info.channels) != CHANNELS:
            _discard(part, state_path)
            raise PrintError(422, "the print does not match the lane's format")
        if not (source.frames <= int(info.frames) <= max_frames(source)):
            _discard(part, state_path)
            raise PrintError(422, "the print does not match the lane's length")
        os.replace(part, wav)
        state_path.unlink(missing_ok=True)
        meta = {
            "job_id": job_id,
            "lane": lane,
            "source": source.identity,
            "rate": source.rate,
            "frames": int(info.frames),
            "gain": float(gain),
        }
        _write_json(meta_path, meta)
    prune(root, keep=pid)
    return meta


def resolve(root: Path, job_id: str, lane: str, pid: str, source: Source) -> tuple[Path, float]:
    """The print of `lane` named `pid`, checked against the lane's current
    file, as (path, gain)."""
    if not PRINT_ID_RE.match(pid):
        raise PrintError(422, "prints: malformed id")
    meta = _complete(root, pid, job_id, lane, source)
    if meta is None:
        raise PrintError(409, "an effects print is missing or out of date")
    wav = (root / f"{pid}.wav").resolve()
    if not wav.is_file() or not wav.is_relative_to(root.resolve()):
        raise PrintError(409, "an effects print is missing or out of date")
    gain = float(meta.get("gain", 1.0))
    if not math.isfinite(gain) or not (GAIN_MIN <= gain <= GAIN_MAX):
        raise PrintError(409, "an effects print is missing or out of date")
    try:
        # Used now: the budget evicts oldest first.
        os.utime(wav)
    except OSError:
        logger.debug("could not touch fx print %s", pid, exc_info=True)
    return wav, gain


def _discard(*paths: Path) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


def prune(root: Path, keep: str | None = None) -> None:
    """Evict whole prints, oldest first, past the folder's budget. `keep` is
    never evicted (the print that was just made)."""
    try:
        wavs = sorted(
            (p for p in root.glob("*.wav") if p.is_file() and p.stem != keep),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in wavs)
        kept = root / f"{keep}.wav" if keep else None
        if kept is not None and kept.is_file():
            total += kept.stat().st_size
    except OSError:
        return
    count = len(wavs) + (1 if kept is not None else 0)
    while wavs and (count > FX_PRINT_CACHE_MAX_FILES or total > FX_PRINT_CACHE_MAX_BYTES):
        oldest = wavs.pop(0)
        try:
            total -= oldest.stat().st_size
            oldest.unlink()
            oldest.with_suffix(".json").unlink(missing_ok=True)
        except OSError:
            logger.debug("fx print prune: could not remove %s", oldest.name, exc_info=True)
        count -= 1


def sweep_sessions(root: Path, now: float | None = None) -> None:
    """Remove uploads that never finished."""
    now = time.time() if now is None else now
    try:
        states = list(root.glob(".*.state"))
    except OSError:
        return
    for state_path in states:
        state = _read_json(state_path) or {}
        created = float(state.get("created", 0) or 0)
        if now - created < FX_PRINT_SESSION_TTL_SEC:
            continue
        pid = state_path.name[1 : -len(".state")]
        _discard(state_path, root / f".{pid}.part")
