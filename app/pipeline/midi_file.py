"""The note engine's files, and the .mid StemDeck writes from its notes.

Two halves with one rule each:

- Where the vendored engine lives (app/_vendor/transcribe), what it is
  (manifest.json, written when it was vendored) and the model file the worker
  loads. Read in the server process without importing any of it.
- The tempo a file is written at (tempo_plan, from the song's beat grid) and
  the file itself (write_midi). The plan is worked out in the server, which
  owns the job folder; the file is written in the worker, which has the
  vendored mido on its path. write_midi imports mido when it is called, so
  this module stays importable without it.

Notes are never moved onto the grid. The tempo map only decides where the
bar lines fall: every note keeps the time its engine heard it at, to the
tick, so the file lines up with the stems from their first sample.
"""

from __future__ import annotations

import json
import math
import numbers
import os
import statistics
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import (
    BEATGRID_MAX_IRREGULARITY,
    MIDI_DEFAULT_BPM,
    MIDI_DRUM_CHANNEL,
    MIDI_PPQ,
    MIDI_STEM_PROGRAMS,
    MIDI_TEMPO_MIN_CONFIDENCE,
    MIDI_VELOCITY_MAX,
    MIDI_VELOCITY_MIN,
)

VENDOR_DIR = Path(__file__).resolve().parent.parent / "_vendor" / "transcribe"
MODEL_PATH = VENDOR_DIR / "basic_pitch" / "saved_models" / "icassp_2022" / "nmp.onnx"
MANIFEST_PATH = VENDOR_DIR / "manifest.json"

ENGINE_ID = "basic_pitch"
ENGINE_LABEL = "Basic Pitch"
ENGINE_LICENCE = "Apache-2.0"

# A tempo this far from a plain number of beats is no tempo at all. The beat
# grid refuses anything outside 30..300 BPM (beatgrid._MIN_BPM, _MAX_BPM).
_MIN_BPM = 30.0
_MAX_BPM = 300.0
# A grid needs this many beats to say anything about a tempo, as beatgrid
# itself requires (beatgrid._MIN_BEATS).
_MIN_GRID_BEATS = 8
# Bar lengths the beat grid can report (beatgrid._MIN_BAR, _MAX_BAR).
_MIN_BAR = 2
_MAX_BAR = 16
_DEFAULT_BAR = 4


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    """manifest.json as written when the engine was vendored, or {} when it
    is missing or unreadable (the engine then reports itself unavailable)."""
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def engine_version() -> str | None:
    for package in manifest().get("packages") or []:
        if isinstance(package, dict) and package.get("name") == "basic-pitch":
            version = package.get("version")
            return version if isinstance(version, str) else None
    return None


def vendor_id() -> str | None:
    """A short id of the exact vendored wheels (a hash of their sha256s), so a
    midi.json says which build of the engine wrote it."""
    value = manifest().get("id")
    return value if isinstance(value, str) else None


# -- the tempo a file is written at


def _number(value: Any) -> float | None:
    # numbers.Real, not (int, float): Basic Pitch's notes carry numpy scalars
    # (float32 amplitudes, int64 pitches), and np.float32 is not a float.
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _beats(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < _MIN_GRID_BEATS:
        return None
    beats: list[float] = []
    for item in value:
        t = _number(item)
        if t is None or t < 0 or (beats and t <= beats[-1]):
            return None
        beats.append(t)
    return beats


def default_tempo() -> dict[str, Any]:
    """120 BPM in 4/4 from the first sample: the file when no grid is trusted."""
    return {
        "source": "default",
        "bpm": MIDI_DEFAULT_BPM,
        "beats_per_bar": _DEFAULT_BAR,
        "bar_start": 0.0,
    }


def tempo_plan(grid: dict[str, Any] | None, *, edited: bool = False) -> dict[str, Any]:
    """The tempo a song's file is written at, from its beat grid (beats.json,
    with the user's edits already applied when ``edited``).

    {"source": "song", "bpm", "beats_per_bar", "bar_start"} when the grid is
    trusted: the grid's tempo (the median beat interval over its beats), its
    first meter, and the time of its first bar line, so the file's bar lines
    fall on the song's. Trusted means edited by the user, or steady
    (BEATGRID_MAX_IRREGULARITY) with enough of its beats on an onset
    (MIDI_TEMPO_MIN_CONFIDENCE). Anything else is default_tempo()."""
    if not isinstance(grid, dict):
        return default_tempo()
    beats = _beats(grid.get("beats"))
    if beats is None:
        return default_tempo()
    if not edited:
        irregularity = _number(grid.get("irregularity"))
        confidence = _number(grid.get("confidence"))
        if irregularity is None or irregularity > BEATGRID_MAX_IRREGULARITY:
            return default_tempo()
        if confidence is None or confidence < MIDI_TEMPO_MIN_CONFIDENCE:
            return default_tempo()
    interval = statistics.median(b - a for a, b in zip(beats, beats[1:], strict=False))
    if interval <= 0:
        return default_tempo()
    bpm = 60.0 / interval
    if not _MIN_BPM <= bpm <= _MAX_BPM:
        return default_tempo()
    beats_per_bar = _DEFAULT_BAR
    first = 0
    bars = grid.get("bars")
    if isinstance(bars, list) and bars and isinstance(bars[0], dict):
        n = bars[0].get("beats_per_bar")
        at = bars[0].get("beat")
        if isinstance(n, int) and not isinstance(n, bool) and _MIN_BAR <= n <= _MAX_BAR:
            beats_per_bar = n
        if isinstance(at, int) and not isinstance(at, bool) and 0 <= at < len(beats):
            first = at
    return {
        "source": "song",
        "bpm": round(bpm, 3),
        "beats_per_bar": beats_per_bar,
        "bar_start": round(beats[first], 6),
    }


class TempoMap:
    """Seconds to ticks under a plan: one lead-in bar from the first sample to
    a bar line of the song, then the song's tempo.

    The lead-in is what puts the song's bar lines on the file's: the first bar
    line is rarely at 0 s, and without it every bar line in the file would sit
    off the song's by that much. Its tempo is set so that one bar of the
    song's meter ends exactly on a bar line of the song, taken between half a
    bar and one and a half bars in, so it stays within 2/3 and 2 times the
    song's tempo. A bar line within half a tick of 0 s needs no lead-in.

    Tempos are whole microseconds per quarter note, as a MIDI file stores
    them, and ticks are worked out from those stored values, so the file
    reads back to the same times: at most half a tick off (0.5 ms at 120 BPM),
    never cumulative."""

    def __init__(self, plan: dict[str, Any]) -> None:
        bpm = _number(plan.get("bpm")) or MIDI_DEFAULT_BPM
        if not _MIN_BPM <= bpm <= _MAX_BPM:
            bpm = MIDI_DEFAULT_BPM
        n = plan.get("beats_per_bar")
        if not isinstance(n, int) or isinstance(n, bool) or not _MIN_BAR <= n <= _MAX_BAR:
            n = _DEFAULT_BAR
        self.beats_per_bar = n
        self.tempo_us = round(60_000_000 / bpm)
        quarter = self.tempo_us / 1_000_000
        bar = quarter * n
        start = _number(plan.get("bar_start")) or 0.0
        start = math.fmod(max(0.0, start), bar)
        # (start seconds, start tick, microseconds per quarter)
        self.segments: list[tuple[float, int, int]]
        if start < quarter / MIDI_PPQ / 2 or bar - start < quarter / MIDI_PPQ / 2:
            self.segments = [(0.0, 0, self.tempo_us)]
            self.lead_in = 0.0
            return
        if start < bar / 2:
            start += bar
        lead_us = round(start * 1_000_000 / n)
        self.lead_in = lead_us * n / 1_000_000
        self.segments = [(0.0, 0, lead_us), (self.lead_in, n * MIDI_PPQ, self.tempo_us)]

    def tick(self, seconds: float) -> int:
        seconds = max(0.0, seconds)
        start, first_tick, us = self.segments[0]
        for segment in self.segments[1:]:
            if seconds >= segment[0]:
                start, first_tick, us = segment
        return first_tick + round((seconds - start) * 1_000_000 / us * MIDI_PPQ)


# -- the file


def velocity(amplitude: float) -> int:
    """A note's amplitude (Basic Pitch's 0..1) as a MIDI velocity in
    MIDI_VELOCITY_MIN..MIDI_VELOCITY_MAX, linear, clamped."""
    a = _number(amplitude)
    a = 0.0 if a is None else min(1.0, max(0.0, a))
    return round(MIDI_VELOCITY_MIN + a * (MIDI_VELOCITY_MAX - MIDI_VELOCITY_MIN))


def note_ticks(notes: Iterable[Sequence[Any]], tempo: TempoMap) -> list[tuple[int, int, int, int]]:
    """(start tick, end tick, pitch, velocity) for each usable note, in order.

    A note is (start s, end s, MIDI pitch, amplitude, ...): Basic Pitch's
    note events, whose pitch bends are not written. A note shorter than a
    tick after rounding is given one tick. A note that starts while the same
    pitch still sounds ends that earlier note there: one key cannot be held
    twice, and two note-ons of a pitch with one note-off between them are
    read differently by every program that opens the file."""
    out: list[tuple[int, int, int, int]] = []
    for note in notes:
        if len(note) < 4:
            continue
        start, end, pitch, amplitude = note[0], note[1], note[2], note[3]
        s, e = _number(start), _number(end)
        if s is None or e is None or isinstance(pitch, bool):
            continue
        if not isinstance(pitch, numbers.Integral) or not 0 <= pitch <= 127 or e < s:
            continue
        pitch = int(pitch)
        a, b = tempo.tick(s), tempo.tick(e)
        out.append((a, max(b, a + 1), pitch, velocity(amplitude)))
    out.sort()
    last: dict[int, int] = {}
    for i, (a, b, pitch, vel) in enumerate(out):
        j = last.get(pitch)
        if j is not None and out[j][1] > a:
            pa = out[j][0]
            if a > pa:
                out[j] = (pa, a, pitch, out[j][3])
            else:
                # Two notes of one pitch at the same tick: one note.
                out[i] = (a, max(b, out[j][1]), pitch, max(vel, out[j][3]))
                out[j] = (pa, pa, pitch, 0)
        last[pitch] = i
    return [n for n in out if n[1] > n[0]]


def build_midi(
    notes: Iterable[Sequence[Any]],
    stem: str,
    plan: dict[str, Any],
    program: int | None = None,
):
    """The file as a mido.MidiFile: format 0, one track, MIDI_PPQ ticks per
    quarter, the track named for ``stem``, the plan's tempo map and meter,
    then every note. The drums' notes are General MIDI percussion on channel
    10 (MIDI_DRUM_CHANNEL), which takes no program; any other stem's are on
    channel 1 under ``program`` (0..127), or the stem's own
    (MIDI_STEM_PROGRAMS) when the engine named none."""
    import mido

    default_program, name = MIDI_STEM_PROGRAMS[stem]
    drums = stem == "drums"
    channel = MIDI_DRUM_CHANNEL if drums else 0
    if not isinstance(program, int) or isinstance(program, bool) or not 0 <= program <= 127:
        program = default_program
    tempo = TempoMap(plan)
    events: list[tuple[int, int, Any]] = []
    for at, _first, us in tempo.segments:
        events.append((tempo.tick(at), 0, mido.MetaMessage("set_tempo", tempo=us, time=0)))
    events.append(
        (
            0,
            1,
            mido.MetaMessage(
                "time_signature", numerator=tempo.beats_per_bar, denominator=4, time=0
            ),
        )
    )
    for start, end, pitch, vel in note_ticks(notes, tempo):
        # At one tick, note-offs before note-ons: a key released and struck
        # again on the same tick sounds again.
        events.append(
            (start, 3, mido.Message("note_on", note=pitch, velocity=vel, channel=channel, time=0))
        )
        events.append(
            (end, 2, mido.Message("note_off", note=pitch, velocity=0, channel=channel, time=0))
        )
    events.sort(key=lambda e: (e[0], e[1]))

    track = mido.MidiTrack()
    track.append(mido.MetaMessage("track_name", name=name, time=0))
    if not drums:
        track.append(mido.Message("program_change", program=program, channel=channel, time=0))
    now = 0
    for at, _order, message in events:
        track.append(message.copy(time=at - now))
        now = at
    track.append(mido.MetaMessage("end_of_track", time=0))
    midi = mido.MidiFile(type=0, ticks_per_beat=MIDI_PPQ)
    midi.tracks.append(track)
    return midi


def write_midi(
    notes: Iterable[Sequence[Any]],
    path: Path,
    stem: str,
    plan: dict[str, Any],
    program: int | None = None,
) -> int:
    """Write the file to ``path``, flushed and synced to disk before this
    returns, so a rename of it afterwards never publishes a short file.
    Returns the number of notes written."""
    notes = list(notes)
    midi = build_midi(notes, stem, plan, program)
    with open(path, "wb") as handle:
        midi.save(file=handle)
        handle.flush()
        os.fsync(handle.fileno())
    return sum(1 for m in midi.tracks[0] if m.type == "note_on")


# -- reading a file back


class MidiReadError(ValueError):
    """The file is not a Standard MIDI File this module can read."""


def _varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if pos >= len(data):
            raise MidiReadError("truncated variable-length number")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise MidiReadError("variable-length number over four bytes")


def _read_events(data: bytes) -> tuple[int, list[tuple[int, int]], list[tuple[int, int, int, int]]]:
    """(ticks per quarter, [(tick, us per quarter)], [(tick, on, pitch, velocity)])."""
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiReadError("not a MIDI file")
    header_len = int.from_bytes(data[4:8], "big")
    if header_len < 6:
        raise MidiReadError("short header")
    division = int.from_bytes(data[12:14], "big")
    if division & 0x8000 or division == 0:
        raise MidiReadError("SMPTE time is not supported")
    pos = 8 + header_len
    tempos: list[tuple[int, int]] = []
    events: list[tuple[int, int, int, int]] = []
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        length = int.from_bytes(data[pos + 4 : pos + 8], "big")
        pos += 8
        end = pos + length
        if end > len(data):
            raise MidiReadError("truncated track")
        if tag != b"MTrk":
            pos = end
            continue
        tick, status = 0, 0
        while pos < end:
            delta, pos = _varlen(data, pos)
            tick += delta
            if pos >= end:
                raise MidiReadError("truncated event")
            if data[pos] & 0x80:
                status = data[pos]
                pos += 1
            elif not status:
                raise MidiReadError("running status with no status")
            if status == 0xFF:
                if pos >= end:
                    raise MidiReadError("truncated meta event")
                kind = data[pos]
                size, pos = _varlen(data, pos + 1)
                if kind == 0x51 and size == 3:
                    tempos.append((tick, int.from_bytes(data[pos : pos + 3], "big")))
                pos += size
                status = 0
            elif status in (0xF0, 0xF7):
                size, pos = _varlen(data, pos)
                pos += size
                status = 0
            else:
                high = status & 0xF0
                width = 1 if high in (0xC0, 0xD0) else 2
                if pos + width > end:
                    raise MidiReadError("truncated channel event")
                if high in (0x80, 0x90):
                    pitch, vel = data[pos], data[pos + 1]
                    events.append((tick, 1 if high == 0x90 and vel > 0 else 0, pitch, vel))
                pos += width
        pos = end
    return division, tempos, events


def read_notes(path: Path) -> dict[str, Any]:
    """The notes of a .mid as {"notes": [[start s, end s, pitch, velocity]],
    "bpm": the file's last tempo}, for the studio's piano roll and its MIDI
    sound (GET /api/jobs/{id}/midi/{stem}/notes).

    Read in the server without mido, which only the worker has on its path:
    a small reader of Standard MIDI Files with metrical time, which is what
    write_midi writes. Times follow the file's tempo map, so they come back
    within half a tick of the times the engine heard (TempoMap). A note-on
    of velocity 0 is a note-off; a note still held at the end of the file
    ends at its last event. Raises MidiReadError on a file it cannot read."""
    division, tempos, events = _read_events(Path(path).read_bytes())
    tempos.sort()
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0, 500_000))
    # (start tick, start seconds, seconds per tick); a later tempo at the same
    # tick replaces an earlier one.
    segments: list[tuple[int, float, float]] = []
    for at, us in tempos:
        start = 0.0
        if segments:
            prev_at, prev_start, per = segments[-1]
            start = prev_start + (at - prev_at) * per
            if at == prev_at:
                segments.pop()
        segments.append((at, start, us / 1_000_000 / division))

    def seconds(tick: int) -> float:
        at, start, per = segments[0]
        for segment in segments[1:]:
            if tick >= segment[0]:
                at, start, per = segment
        return start + (tick - at) * per

    events.sort(key=lambda e: (e[0], e[1]))
    held: dict[int, tuple[int, int]] = {}
    notes: list[tuple[int, int, int, int]] = []
    last = events[-1][0] if events else 0
    for tick, on, pitch, vel in events:
        began = held.pop(pitch, None)
        if began is not None and tick > began[0]:
            notes.append((began[0], tick, pitch, began[1]))
        if on:
            held[pitch] = (tick, vel)
    for pitch, (tick, vel) in held.items():
        if last > tick:
            notes.append((tick, last, pitch, vel))
    notes.sort()
    return {
        "notes": [[round(seconds(a), 4), round(seconds(b), 4), p, v] for a, b, p, v in notes],
        "bpm": round(60_000_000 / tempos[-1][1], 3) if tempos[-1][1] else None,
    }
