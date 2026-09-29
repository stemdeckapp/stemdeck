"""Moving LRCLIB's timing onto this track, for lyrics from a version of
another length (lyrics_lookup.py saves those with "timing": "unverified"),
and for a version of the same length whose lines the vocals show start at
least LYRICS_ALIGN_EXACT_MIN_SHIFT_SEC away: a copy the track's length can
still be timed to another cut, and its length alone proved nothing.

A version of a song a few seconds longer or shorter than the track is most
often the same recording with more or less lead-in: a video's intro, a
remaster's trimmed silence, a single edit. The words are right and every line
is off by the same amount. That amount is found here without any model, from
the separated vocals stem's level over time (the envelope the Lyrics tab's
wipe follows): how sharply the voice rises at each moment is summed at the
moments the LRC's lines start, for every shift within
LYRICS_ALIGN_MAX_OFFSET_SEC, and the best shift is taken when it stands out
from the rest (LYRICS_ALIGN_MIN_Z) and beats every other shift a second or
more away (LYRICS_ALIGN_MIN_RATIO): a song's repeated rhythm lines up other
shifts too, and a different performance of the song lines up none clearly.
The stamps are rewritten by it and the lyrics marked "shifted". A fit that is
not clear leaves them as they were, still "unverified": wrong timing moved is
worse than wrong timing kept.

Cheap CPU work: one streamed read of the vocals stem, or none when the
envelope is already kept beside it, and a few thousand candidate shifts.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from app.core.config import (
    LYRICS_ALIGN_EXACT_MIN_SHIFT_SEC,
    LYRICS_ALIGN_MAX_OFFSET_SEC,
    LYRICS_ALIGN_MIN_HITS,
    LYRICS_ALIGN_MIN_LINES,
    LYRICS_ALIGN_MIN_RATIO,
    LYRICS_ALIGN_MIN_Z,
    LYRICS_ALIGN_TOLERANCE_SEC,
)
from app.core.models import Job
from app.pipeline.audio_stats import vocal_envelope

logger = logging.getLogger("stemdeck.lyrics")

# Kept beside the stems by GET .../vocal-envelope (app/api/stems.py).
_ENVELOPE_FILE = "vocal_envelope.json"
_ENVELOPE_HOP_SEC = 0.04

# What counts as singing, as the tab's wipe has it (lyricsLookup.js
# voicedPhrases): within 20 dB of the stem's loud parts, never below -50 dBFS,
# and nothing at all when its loud parts are under -45 dBFS. Its loud parts
# are its 95th percentile rather than the wipe's 90th: over a whole song the
# voice can be silent most of the time, where within one line it is not.
_VOICE_RANGE_DB = 20
_VOICE_FLOOR_DB = -50
_SILENT_DB = -45
_LOUD_PERCENTILE = 95
# A rise is measured over this many levels (120 ms at the envelope's 40 ms),
# of the level smoothed over three: a syllable's attack, not a flicker.
_RISE_FRAMES = 3
# A line counts as starting on singing when the voice rises this much near it.
_HIT_RISE_DB = 6
# Shifts this close to the best are the same answer, not a rival one.
_SAME_SHIFT_SEC = 1.0

_OFFSET_TAG = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]\s*$", re.IGNORECASE)
_LINE_STAMPS = re.compile(r"^(?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+")
_STAMP = re.compile(r"([\[<])(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)([\]>])")


def _seconds(minutes: str, rest: str) -> float:
    return int(minutes) * 60 + float(rest.replace(":", "."))


def _stamp(seconds: float) -> str:
    centis = round(max(0.0, seconds) * 100)
    return f"{centis // 6000:02d}:{(centis % 6000) / 100:05.2f}"


def _offset(text: str) -> float:
    """The LRC's own [offset:] tag, in seconds earlier (parseLrc's rule)."""
    for line in text.splitlines():
        tag = _OFFSET_TAG.match(line)
        if tag:
            return int(tag.group(1)) / 1000
    return 0.0


def line_starts(text: str) -> list[float]:
    """When each sung line starts, in seconds, earliest first: every stamp of
    every line with words, the [offset:] tag applied. Lines with no words
    mark an instrumental gap and start nothing."""
    offset = _offset(text)
    starts = []
    for line in text.splitlines():
        stamps = _LINE_STAMPS.match(line)
        if not stamps:
            continue
        # [^<>]: an unclosed "<" cannot make this quadratic.
        words = re.sub(r"<[^<>]*>", "", line[stamps.end() :]).strip()
        if not words:
            continue
        for m in _STAMP.finditer(stamps.group(0)):
            starts.append(max(0.0, _seconds(m.group(2), m.group(3)) - offset))
    return sorted(starts)


def shift_lrc(text: str, seconds: float) -> str:
    """``text`` with every line and word stamp ``seconds`` later (earlier
    when negative), never before zero. Its [offset:] tag is folded into the
    stamps and dropped, so the stamps alone say when."""
    delta = seconds - _offset(text)
    lines = [line for line in text.splitlines() if not _OFFSET_TAG.match(line)]

    def move(m: re.Match[str]) -> str:
        return f"{m.group(1)}{_stamp(_seconds(m.group(2), m.group(3)) + delta)}{m.group(4)}"

    return "\n".join(_STAMP.sub(move, line) for line in lines)


def onset_strength(db: Any) -> np.ndarray:
    """How sharply singing starts at each level of an envelope: the rise of
    the smoothed level over _RISE_FRAMES, where the voice is singing, else 0.
    All zeros for a stem with no singing."""
    levels = np.asarray(db, dtype=float)
    if levels.size < _RISE_FRAMES + 1:
        return np.zeros(levels.size)
    loud = float(np.percentile(levels, _LOUD_PERCENTILE))
    if loud < _SILENT_DB:
        return np.zeros(levels.size)
    smooth = np.convolve(levels, np.ones(3) / 3, mode="same")
    rise = np.zeros(levels.size)
    rise[_RISE_FRAMES:] = smooth[_RISE_FRAMES:] - smooth[:-_RISE_FRAMES]
    rise[smooth < max(loud - _VOICE_RANGE_DB, _VOICE_FLOOR_DB)] = 0
    return np.clip(rise, 0, None)


def estimate_offset(starts: list[float], strength: np.ndarray, hop: float) -> float | None:
    """The shift, in seconds, that puts the LRC's line starts where the
    singing starts (``strength``, onset_strength of an envelope ``hop``
    seconds a level), or None when no shift clearly does.

    Each shift within +-LYRICS_ALIGN_MAX_OFFSET_SEC scores the strongest
    rise within LYRICS_ALIGN_TOLERANCE_SEC of each moved line start, summed.
    The best is kept when it is LYRICS_ALIGN_MIN_Z standard deviations above
    the mean of all shifts, LYRICS_ALIGN_MIN_RATIO times the best shift a
    second or more away, and puts LYRICS_ALIGN_MIN_HITS of the lines on a
    rise of _HIT_RISE_DB or more: scattered singing lines a few up by chance."""
    lines = np.asarray(starts, dtype=float)
    if lines.size < LYRICS_ALIGN_MIN_LINES or hop <= 0 or not strength.any():
        return None
    k = max(1, round(LYRICS_ALIGN_TOLERANCE_SEC / hop))
    nearby = sliding_window_view(np.pad(strength, (k, k)), 2 * k + 1).max(axis=1)
    shifts = np.arange(-LYRICS_ALIGN_MAX_OFFSET_SEC, LYRICS_ALIGN_MAX_OFFSET_SEC + hop / 2, hop)
    frames = np.rint((lines[None, :] + shifts[:, None]) / hop).astype(int)
    inside = (frames >= 0) & (frames < strength.size)
    rises = np.where(inside, nearby[np.clip(frames, 0, strength.size - 1)], 0.0)
    score = rises.sum(axis=1)

    pick = int(np.argmax(score))
    spread = float(score.std())
    far = np.abs(shifts - shifts[pick]) > _SAME_SHIFT_SEC
    rival = float(score[far].max()) if far.any() else 0.0
    z = (score[pick] - score.mean()) / spread if spread > 0 else 0.0
    ratio = score[pick] / rival if rival > 0 else float("inf")
    hits = float((rises[pick] >= _HIT_RISE_DB).mean())
    if z < LYRICS_ALIGN_MIN_Z or ratio < LYRICS_ALIGN_MIN_RATIO or hits < LYRICS_ALIGN_MIN_HITS:
        logger.info(
            "lyrics timing not confident: best %+.2fs, z %.1f, %.2f times the next, %.0f%% on",
            shifts[pick],
            z,
            ratio,
            hits * 100,
        )
        return None
    # Finer than the grid: where, near each moved line start, the voice rises
    # most, less the lag the rise is measured over.
    residuals = []
    for frame in frames[pick][inside[pick]]:
        lo, hi = max(0, frame - k), min(strength.size, frame + k + 1)
        window = strength[lo:hi]
        if window.max() > 0:
            residuals.append(lo + int(np.argmax(window)) - frame)
    lag = (_RISE_FRAMES - 1) / 2
    return round(float(shifts[pick]) + (float(np.median(residuals)) - lag) * hop, 2)


def _envelope(stems_dir: Path) -> tuple[float, list[int]] | None:
    """The vocals stem's envelope: the one kept beside it when it is not
    older than the stem, else read from the stem now. None without vocals."""
    vocals = stems_dir / "vocals.wav"
    if not vocals.is_file():
        return None
    kept = stems_dir / _ENVELOPE_FILE
    try:
        if kept.is_file() and kept.stat().st_mtime_ns >= vocals.stat().st_mtime_ns:
            data = json.loads(kept.read_text(encoding="utf-8"))
            if isinstance(data.get("db"), list) and float(data.get("hop", 0)) > 0:
                return float(data["hop"]), data["db"]
    except (OSError, ValueError, TypeError, AttributeError):
        logger.info("unreadable vocal envelope; reading the stem", exc_info=True)
    return vocal_envelope(vocals, _ENVELOPE_HOP_SEC)


def detect_offset(synced: str, stems_dir: Path) -> float | None:
    """How many seconds later ``synced``'s lines are sung on this track, from
    the vocals stem in ``stems_dir``, whatever timing they were saved with:
    the Lyrics tab's Auto-detect. None when the stem does not show it clearly,
    or there is no stem. Blocking: may read the whole stem."""
    envelope = _envelope(stems_dir)
    if envelope is None:
        return None
    hop, db = envelope
    return estimate_offset(line_starts(synced), onset_strength(db), hop)


def align_lyrics(job: Job, job_dir: Path) -> str | None:
    """Move LRCLIB lyrics' timing onto this track, when the vocals stem shows
    clearly by how much: a version of another length ("unverified") by any
    amount, one of the track's length ("exact") only by at least
    LYRICS_ALIGN_EXACT_MIN_SHIFT_SEC. A transcription or the file's own
    lyrics are the track's timing already. Returns the timing the job's
    lyrics.json is left with, or None when it has none to align. Never
    raises."""
    # Imported here: lyrics_lookup imports this module.
    from app.pipeline.lyrics_lookup import _OFFSET_LOCK, read_lyrics, write_lyrics

    try:
        entry = read_lyrics(job_dir)
        if entry is None or not entry["synced"]:
            return entry.get("timing") if entry else None
        timing = entry.get("timing")
        # Aligned by hand in the Lyrics tab: the user's timing, not to guess at.
        if "offset_sec" in entry:
            return timing
        checked = timing == "unverified" or (timing == "exact" and entry["source"] == "lrclib")
        if not checked:
            return timing
        envelope = _envelope(job_dir / "stems")
        if envelope is None:
            return timing
        hop, db = envelope
        shift = estimate_offset(line_starts(entry["synced"]), onset_strength(db), hop)
        if shift is None:
            return timing
        if timing == "exact" and abs(shift) < LYRICS_ALIGN_EXACT_MIN_SHIFT_SEC:
            return timing
        # Written under the lock the Lyrics tab's own edits take, and only if
        # nothing changed the lyrics while the shift was worked out: an
        # offset or timing the user set meanwhile is theirs.
        with _OFFSET_LOCK:
            if read_lyrics(job_dir) != entry:
                return timing
            logger.info("[%s] lyrics timing moved by %+.2fs", job.id, shift)
            write_lyrics(
                job,
                job_dir,
                {**entry, "synced": shift_lrc(entry["synced"], shift), "timing": "shifted"},
            )
        return "shifted"
    except Exception:
        logger.warning("[%s] lyrics alignment failed", job.id, exc_info=True)
        return None
