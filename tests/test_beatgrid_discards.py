"""Every way compute_beat_grid decides not to write a grid.

The stage's contract is that it never raises: a missing beat grid degrades the
click track and must not fail a job whose stems are already finished. That makes
each of these a silent decision, and the only evidence a user gets is a
metronome that does nothing.

The heavy parts (the real detector, the ffmpeg decode) are stubbed, so what is
under test is the sequence of discard rules rather than the DSP -- which
test_pipeline_beatgrid.py already drives end to end against rendered audio.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
import soundfile as sf

from app.pipeline import beatgrid as bg

SR = 22050


@pytest.fixture
def stems(tmp_path):
    """A stems dir with a drums stem, which _pick_source prefers."""
    d = tmp_path / "stems"
    d.mkdir()
    t = np.linspace(0, 2.0, int(8000 * 2.0), endpoint=False)
    sf.write(str(d / "drums.wav"), (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), 8000)
    return d


def _steady(n=60, interval=0.5):
    return [i * interval for i in range(n)]


def _impulsive(seconds=30.0, interval=0.5, sr=SR):
    """Noise bursts on a regular grid -- enough onset energy for the stage's
    envelope checks to pass, so the discard rules below are what decides."""
    rng = np.random.default_rng(0)
    y = np.zeros(int(sr * seconds), dtype=np.float32)
    burst = int(sr * 0.01)
    for k in range(int(seconds / interval)):
        start = int(k * interval * sr)
        y[start : start + burst] = rng.uniform(-1.0, 1.0, size=burst).astype(np.float32)
    return y


@pytest.fixture
def stub(monkeypatch):
    """Replace the decode and the detector; leave the discard rules real."""

    state = {
        # Real transients: a short noise burst every half second. A uniform
        # waveform produces a flat onset envelope, which the stage discards
        # before it can reach any of the rules below.
        "audio": _impulsive(seconds=30.0, interval=0.5),
        "beats": _steady(),
        "downbeats": [],
    }

    def _load(source, sr=SR, duration=None, timeout=None):
        return (state["audio"], SR) if state["audio"] is not None else None

    def _detect(y, sr, onset_env):
        return state["beats"], state["downbeats"], "stub"

    monkeypatch.setattr(bg, "_load_audio_ffmpeg", _load)
    monkeypatch.setattr(bg, "detect_beats", _detect)
    return state


# --------------------------------------------------------------------------
# before anything is decoded
# --------------------------------------------------------------------------


def test_no_grid_without_librosa(monkeypatch, stems, caplog):
    """Docker's minimal image and a half-finished install both hit this. It has
    to be a warning and a None, not an ImportError out of the stage."""
    real_import = __import__

    def _no_librosa(name, *a, **kw):
        if name == "librosa":
            raise ImportError("no librosa")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "librosa", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_librosa)

    assert bg.compute_beat_grid(stems) is None
    assert "librosa not installed" in caplog.text


def test_no_grid_when_there_is_no_usable_stem(tmp_path, caplog):
    """A job that separated with fewer stems, or one whose drums file is gone."""
    empty = tmp_path / "stems"
    empty.mkdir()

    assert bg.compute_beat_grid(empty) is None
    assert "no usable source stem" in caplog.text


def test_no_grid_when_the_stem_cannot_be_decoded(stems, stub, monkeypatch):
    """_load_audio_ffmpeg already logged; this only has to stop."""
    stub["audio"] = None

    assert bg.compute_beat_grid(stems) is None


def test_no_grid_for_a_zero_length_stem(stems, stub):
    stub["audio"] = np.zeros(0, dtype=np.float32)

    assert bg.compute_beat_grid(stems) is None


def test_no_grid_for_a_silent_stem(stems, stub, caplog):
    """A flat onset envelope means there is nothing to detect against; the
    tracker would return evenly spaced noise."""
    stub["audio"] = np.zeros(SR * 4, dtype=np.float32)

    assert bg.compute_beat_grid(stems) is None
    assert "flat onset envelope" in caplog.text


# --------------------------------------------------------------------------
# after detection
# --------------------------------------------------------------------------


def test_no_grid_when_too_few_beats_were_detected(stems, stub, caplog):
    """A handful of beats cannot support a click track, and the editor has
    nothing to snap to."""
    stub["beats"] = [0.5, 1.0, 1.5]

    assert bg.compute_beat_grid(stems) is None
    assert "beats detected -- discarding" in caplog.text


def test_no_grid_when_every_beat_landed_at_the_same_instant(stems, stub):
    """A zero coarse interval would divide by zero sizing the refinement
    window."""
    stub["beats"] = [1.0] * 60

    assert bg.compute_beat_grid(stems) is None


def test_no_grid_at_an_implausible_tempo(stems, stub, caplog):
    """A grid at 2000 BPM is a tracker artefact, not a song. Clicking to it is
    worse than no click."""
    stub["beats"] = [i * 0.02 for i in range(60)]  # 3000 BPM

    assert bg.compute_beat_grid(stems) is None
    assert "implausible tempo" in caplog.text


def test_no_grid_when_the_tempo_is_impossibly_slow(stems, stub, caplog):
    """Below _MIN_BPM (30). The audio has to be long enough for these beats to
    survive the clip-to-duration, or the too-few-beats rule fires first."""
    # _MIN_BEATS is 8, so twelve beats is enough grid to reach the tempo check
    # without decoding minutes of audio.
    stub["audio"] = _impulsive(seconds=75.0, interval=6.0)
    stub["beats"] = [i * 6.0 for i in range(12)]  # 10 BPM

    assert bg.compute_beat_grid(stems) is None
    assert "implausible tempo" in caplog.text


def test_no_grid_when_post_processing_leaves_too_little(stems, stub, monkeypatch, caplog):
    """Gap filling and the consistency pass can both drop beats. Whatever is
    left still has to be a grid."""
    real = bg._sanitize
    calls = {"n": 0}

    def _shrinking(beats, duration):
        calls["n"] += 1
        # The first call feeds the detected-beats check; the post-processing
        # passes are the ones under test here.
        return real(beats, duration) if calls["n"] == 1 else [0.5, 1.0]

    monkeypatch.setattr(bg, "_sanitize", _shrinking)

    assert bg.compute_beat_grid(stems) is None
    assert "post-processing left only" in caplog.text


# --------------------------------------------------------------------------
# the stage never raises
# --------------------------------------------------------------------------


def test_an_unexpected_failure_is_contained(stems, stub, monkeypatch, caplog):
    """The guard exists so a future change inside the stage cannot take down a
    job whose stems are already correct."""

    def _boom(*a, **kw):
        raise RuntimeError("numpy exploded")

    monkeypatch.setattr(bg, "_refine_beats", _boom)

    assert bg.compute_beat_grid(stems) is None
    assert "beatgrid failed" in caplog.text


# --------------------------------------------------------------------------
# the happy path, for contrast
# --------------------------------------------------------------------------


def test_a_usable_grid_is_written_to_the_stems_dir(stems, stub):
    """So the discards above are decisions rather than the only outcome."""
    grid = bg.compute_beat_grid(stems)

    assert grid is not None
    written = json.loads((stems / "beats.json").read_text(encoding="utf-8"))
    assert written["beats"] == grid["beats"]
    assert 30 <= written["bpm"] <= 300
    assert written["source"] == "drums"
    assert written["beats"] == sorted(written["beats"])
    # Confidence and the interval CV are what the UI uses to distinguish a
    # steady song from a wandering tracker.
    assert 0 <= written["confidence"] <= 100
    assert written["duration"] > 0
