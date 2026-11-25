"""The .mid StemDeck writes from Basic Pitch's notes (app/pipeline/midi_file.py).

Read back with the vendored mido: the tempo map, the lead-in that puts the
song's bar lines on the file's, the program and track name per stem, note
on/off pairs at the times the notes were heard, and the velocity mapping.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from app.core.config import MIDI_PPQ, MIDI_VELOCITY_MAX, MIDI_VELOCITY_MIN
from app.pipeline import midi_file as mf


@pytest.fixture
def mido():
    """The vendored mido, on sys.path for this test only. The server process
    never has it there; the worker puts it there itself."""
    path = str(mf.VENDOR_DIR)
    sys.path.insert(0, path)
    try:
        import mido as module

        assert "_vendor" in module.__file__.replace("\\", "/")
        yield module
    finally:
        sys.path.remove(path)


def _read(mido, path):
    """[(seconds, message)] of the file's one track, by mido's own clock."""
    midi = mido.MidiFile(str(path))
    assert midi.type == 0 and len(midi.tracks) == 1
    assert midi.ticks_per_beat == MIDI_PPQ
    out, now = [], 0.0
    for message in midi:  # mido applies the tempo map when iterating a file
        now += message.time
        out.append((now, message))
    return midi, out


def _notes(events):
    """[(on seconds, off seconds, pitch, velocity)] paired per pitch."""
    held, notes = {}, []
    for t, m in events:
        if m.type == "note_on" and m.velocity > 0:
            assert m.note not in held, "a pitch struck twice with no note-off between"
            held[m.note] = (t, m.velocity)
        elif m.type == "note_off" or (m.type == "note_on" and m.velocity == 0):
            start, vel = held.pop(m.note)
            notes.append((start, t, m.note, vel))
    assert not held, "a note was never released"
    return sorted(notes)


def _grid(**over):
    beats = [0.4 + 0.5 * i for i in range(40)]  # 120 BPM, first beat at 0.4 s
    grid = {
        "beats": beats,
        "bars": [{"beat": 2, "beats_per_bar": 3}],
        "bpm": 120.0,
        "irregularity": 0.03,
        "confidence": 90,
    }
    grid.update(over)
    return grid


def test_default_tempo_without_a_trusted_grid():
    assert mf.tempo_plan(None)["source"] == "default"
    assert mf.tempo_plan({"beats": [1, 2]})["source"] == "default"
    assert mf.tempo_plan(_grid(irregularity=0.5))["source"] == "default"
    assert mf.tempo_plan(_grid(confidence=10))["source"] == "default"
    assert mf.tempo_plan(_grid(beats=[3, 2, 1, 4, 5, 6, 7, 8, 9]))["source"] == "default"
    plan = mf.tempo_plan(None)
    assert plan == {"source": "default", "bpm": 120.0, "beats_per_bar": 4, "bar_start": 0.0}


def test_a_trusted_grid_gives_its_tempo_meter_and_first_bar_line():
    plan = mf.tempo_plan(_grid())
    assert plan["source"] == "song"
    assert plan["bpm"] == pytest.approx(120.0)
    assert plan["beats_per_bar"] == 3
    # bars[0].beat is beat index 2: 0.4 + 2 * 0.5.
    assert plan["bar_start"] == pytest.approx(1.4)
    # An edited grid is trusted as it is, whatever the detector's numbers.
    edited = mf.tempo_plan(_grid(irregularity=0.9, confidence=0), edited=True)
    assert edited["source"] == "song"


def test_the_tempo_map_puts_bar_lines_on_the_song_and_keeps_times():
    plan = {"source": "song", "bpm": 100.0, "beats_per_bar": 4, "bar_start": 0.37}
    tempo = mf.TempoMap(plan)
    bar = 4 * 0.6
    # 0.37 s is under half a bar, so the lead-in runs to the next bar line.
    assert tempo.lead_in == pytest.approx(0.37 + bar, abs=1e-5)
    assert tempo.tick(tempo.lead_in) == 4 * MIDI_PPQ
    # Every later bar line of the song is a bar line of the file.
    for k in range(1, 20):
        assert tempo.tick(tempo.lead_in + k * bar) == (4 + 4 * k) * MIDI_PPQ
    # The lead-in tempo stays within 2/3 and 2 times the song's.
    lead_us = tempo.segments[0][2]
    assert 0.5 * 600_000 <= lead_us <= 1.5 * 600_000
    # A bar line at 0 s needs none.
    assert mf.TempoMap({**plan, "bar_start": 0.0}).segments == [(0.0, 0, 600_000)]


def test_velocity_maps_amplitude_linearly_and_clamps():
    assert mf.velocity(0.0) == MIDI_VELOCITY_MIN
    assert mf.velocity(1.0) == MIDI_VELOCITY_MAX
    assert mf.velocity(0.5) == round((MIDI_VELOCITY_MIN + MIDI_VELOCITY_MAX) / 2)
    assert mf.velocity(-3) == MIDI_VELOCITY_MIN
    assert mf.velocity(7) == MIDI_VELOCITY_MAX
    assert mf.velocity(float("nan")) == MIDI_VELOCITY_MIN
    # Basic Pitch hands numpy scalars over.
    assert mf.velocity(np.float32(1.0)) == MIDI_VELOCITY_MAX


def test_overlapping_notes_of_one_pitch_never_stack():
    tempo = mf.TempoMap(mf.default_tempo())
    ticks = mf.note_ticks(
        [
            (0.0, 1.0, 60, 0.5, None),
            (0.5, 1.5, 60, 0.5, None),  # struck again while held
            (2.0, 2.0, 62, 0.5, None),  # zero length: one tick
            (3.0, 2.0, 64, 0.5, None),  # ends before it starts: dropped
            (1.0, 2.0, 200, 0.5, None),  # not a MIDI pitch: dropped
            (np.float64(4.0), np.float64(4.5), np.int64(65), np.float32(0.9), None),
        ],
        tempo,
    )
    assert ticks == [
        (0, 480, 60, mf.velocity(0.5)),
        (480, 1440, 60, mf.velocity(0.5)),
        (1920, 1921, 62, mf.velocity(0.5)),
        (3840, 4320, 65, mf.velocity(0.9)),
    ]


@pytest.mark.parametrize("stem,program,name", [("piano", 0, "Keys"), ("bass", 33, "Bass")])
def test_round_trip_default_tempo(mido, tmp_path, stem, program, name):
    notes = [(0.5, 1.0, 57, 0.8, [1, 2]), (1.25, 2.0, 60, 0.2, None), (1.25, 1.75, 64, 0.5, None)]
    path = tmp_path / f"{stem}.mid"
    assert mf.write_midi(notes, path, stem, mf.default_tempo()) == 3
    midi, events = _read(mido, path)
    messages = [m for _t, m in events]
    assert any(m.type == "track_name" and m.name == name for m in messages)
    assert [m.program for m in messages if m.type == "program_change"] == [program]
    assert [m.tempo for m in messages if m.type == "set_tempo"] == [500_000]
    sigs = [(m.numerator, m.denominator) for m in messages if m.type == "time_signature"]
    assert sigs == [(4, 4)]
    assert not any(m.type == "pitchwheel" for m in messages)
    got = _notes(events)
    expected = sorted((n[0], n[1], n[2], n[3]) for n in notes)
    assert [n[2] for n in got] == [57, 64, 60]
    for (on, off, pitch, vel), (s, e, p, a) in zip(got, expected, strict=True):
        assert pitch == p
        assert on == pytest.approx(s, abs=0.002) and off == pytest.approx(e, abs=0.002)
        assert vel == mf.velocity(a)
    assert all(m.channel == 0 for m in messages if m.type in ("note_on", "note_off"))


def test_round_trip_song_tempo_keeps_absolute_times(mido, tmp_path):
    plan = mf.tempo_plan(_grid())
    rng = np.random.default_rng(3)
    starts = np.sort(rng.uniform(0, 19, 60))
    notes = [(float(s), float(s) + 0.3, 40 + i % 30, 0.6, None) for i, s in enumerate(starts)]
    path = tmp_path / "bass.mid"
    mf.write_midi(notes, path, "bass", plan)
    midi, events = _read(mido, path)
    tempos = [m.tempo for _t, m in events if m.type == "set_tempo"]
    # The lead-in, then the song's tempo.
    assert len(tempos) == 2 and tempos[1] == 500_000
    assert [m.numerator for _t, m in events if m.type == "time_signature"] == [3]
    got = _notes(events)
    expected = sorted((s, e, p) for s, e, p, _a, _b in notes)
    assert len(got) == len(expected)
    for (on, off, pitch, _v), (s, e, p) in zip(got, expected, strict=True):
        # Never quantised: each note within a tick of where it was heard.
        assert pitch == p
        assert on == pytest.approx(s, abs=0.002) and off == pytest.approx(e, abs=0.002)
    # The song's bar lines (every 1.5 s from 1.4 s) are the file's.
    tempo = mf.TempoMap(plan)
    for k in range(10):
        assert tempo.tick(1.4 + 1.5 * k) % (3 * MIDI_PPQ) == 0
    assert midi.length == pytest.approx(max(e for _s, e, _p in expected), abs=0.01)


def test_the_file_is_synced_before_write_midi_returns(mido, tmp_path, monkeypatch):
    synced = []
    real = mf.os.fsync
    monkeypatch.setattr(mf.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    mf.write_midi([(0, 1, 60, 1.0)], tmp_path / "a.mid", "piano", mf.default_tempo())
    assert synced


def test_the_engine_is_described_from_its_manifest():
    assert mf.engine_version() == "0.4.0"
    assert isinstance(mf.vendor_id(), str) and len(mf.vendor_id()) == 16
    assert mf.MODEL_PATH.is_file() and mf.MODEL_PATH.name == "nmp.onnx"
