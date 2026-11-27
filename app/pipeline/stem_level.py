"""The loudness of each stem, kept with the file it was measured on.

The studio plays a lane's MIDI at the loudness of its stem, so switching its
Sound between Stem and MIDI does not jump in level (static/js/midiNotesView.js
levelScale: the stem's RMS over the rendering's). The streaming (chunked)
engine never holds a stem whole and cannot measure it, so the level comes
from here, for both engines alike: GET /api/jobs/{id}/midi/{stem}/notes
answers it as "stem_rms".

The value is audio_stats.scan_stem's RMS: channel 0, over the whole file,
from the same streamed pass that draws the waveform. The separation's own
pass keeps it (runner._run_common) instead of throwing it away after stem
presence. Each is kept in Job.stem_rms (and metadata.json) as

    {name: {"rms": float, "file": "<name>.wav", "size": bytes, "mtime_ns": int}}

with the file it was measured on. A record whose file is not the one on disk
now (a stem re-extracted, undone, turned down, or stored as MP3 after the
scan) is not used: the stem is measured again, one streamed pass, and the new
record kept. So no path that replaces a stem file has to remember to update
it, and an old song without any is measured the first time a lane asks.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from app.pipeline.audio_stats import scan_stem

logger = logging.getLogger("stemdeck.pipeline.stem_level")

# Only the RMS is wanted. scan_stem's blocks are frames // buckets long, so
# this keeps a block a few hundred KB for any length, as the peaks pass does.
_SCAN_BUCKETS = 1500


def level_record(path: Path, rms: float) -> dict[str, Any] | None:
    """The record kept for a stem whose file `path` measured `rms`, or None
    when the file cannot be read for its size and time (it is then measured
    again when asked)."""
    if not isinstance(rms, (int, float)) or not math.isfinite(rms) or rms < 0:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return {"rms": float(rms), "file": path.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def level_records(stems_dir: Path, rms_values: dict[str, float]) -> dict[str, dict[str, Any]]:
    """Records for the RMS values a scan of stems_dir/<name>.wav returned
    (collect.compute_stem_peaks)."""
    out: dict[str, dict[str, Any]] = {}
    for name, rms in rms_values.items():
        rec = level_record(stems_dir / f"{name}.wav", rms)
        if rec is not None:
            out[name] = rec
    return out


def _clean_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    rms, file, size, mtime = (value.get(k) for k in ("rms", "file", "size", "mtime_ns"))
    if isinstance(rms, bool) or not isinstance(rms, (int, float)) or not math.isfinite(rms):
        return None
    if rms < 0 or not isinstance(file, str) or not file:
        return None
    for n in (size, mtime):
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            return None
    return {"rms": float(rms), "file": file, "size": size, "mtime_ns": mtime}


def clean_stem_rms(value: Any) -> dict[str, dict[str, Any]]:
    """Job.stem_rms as read from a record or metadata.json: the well-formed
    entries only (a damaged one is measured again rather than trusted)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, rec in value.items():
        clean = _clean_record(rec)
        if isinstance(name, str) and clean is not None:
            out[name] = clean
    return out


def kept_rms(stem_rms: Any, name: str, path: Path) -> float | None:
    """The RMS kept for stem `name` when it was measured on the file now at
    `path` (same name, size and modification time), else None."""
    rec = clean_stem_rms(stem_rms).get(name)
    if rec is None or rec["file"] != path.name:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size != rec["size"] or st.st_mtime_ns != rec["mtime_ns"]:
        return None
    return rec["rms"]


def measure(path: Path) -> dict[str, Any] | None:
    """Measure the stem file at `path` (one streamed pass) and return its
    record, or None when it cannot be read, or was replaced while it was
    read (the value would then be kept for a file it does not describe).
    Blocking: call it in a thread."""
    try:
        before = path.stat()
        _peaks, rms = scan_stem(path, _SCAN_BUCKETS)
    except Exception:
        logger.warning("could not measure the level of %s", path, exc_info=True)
        return None
    rec = level_record(path, rms)
    if rec is None or (rec["size"], rec["mtime_ns"]) != (before.st_size, before.st_mtime_ns):
        return None
    return rec
