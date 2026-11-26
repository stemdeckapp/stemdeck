"""Each stem's loudness kept with the file it was measured on
(app/pipeline/stem_level.py): the level a lane's MIDI is played at."""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from app.pipeline import stem_level
from app.pipeline.audio_stats import scan_stem


def _wav(path: Path, level: float, seconds: float = 2.0) -> float:
    """A stereo WAV whose first channel is a sine of amplitude `level`; its
    channel-0 RMS, as written in float32."""
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(44100 * seconds)) / 44100
    left = (np.sin(2 * np.pi * 220 * t) * level).astype("float32")
    right = (left * 0.1).astype("float32")
    sf.write(str(path), np.stack([left, right], axis=1), 44100, subtype="FLOAT")
    return float(np.sqrt(np.mean(left.astype(np.float64) ** 2)))


def test_measure_is_the_rms_of_the_first_channel(tmp_path: Path):
    path = tmp_path / "bass.wav"
    want = _wav(path, 0.5)
    rec = stem_level.measure(path)
    assert rec is not None
    assert math.isclose(rec["rms"], want, rel_tol=1e-6)
    # The same number the peaks pass gives, so a kept value and a measured
    # one agree.
    assert math.isclose(rec["rms"], scan_stem(path)[1], rel_tol=1e-9)
    st = path.stat()
    assert rec == {
        "rms": rec["rms"],
        "file": "bass.wav",
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def test_measure_of_an_unreadable_file_is_none(tmp_path: Path):
    bad = tmp_path / "bass.wav"
    bad.write_bytes(b"RIFF not audio")
    assert stem_level.measure(bad) is None
    assert stem_level.measure(tmp_path / "missing.wav") is None


def test_a_kept_value_is_used_only_for_the_file_it_was_measured_on(tmp_path: Path):
    path = tmp_path / "bass.wav"
    _wav(path, 0.5)
    kept = {"bass": stem_level.measure(path)}
    assert stem_level.kept_rms(kept, "bass", path) == kept["bass"]["rms"]
    # Another stem's name, another file name (the MP3 stored after the scan).
    assert stem_level.kept_rms(kept, "drums", path) is None
    mp3 = tmp_path / "bass.mp3"
    mp3.write_bytes(path.read_bytes())
    assert stem_level.kept_rms(kept, "bass", mp3) is None
    # The file replaced in place (a re-extract, an undo, a turn-down).
    _wav(path, 0.1, seconds=3.0)
    assert stem_level.kept_rms(kept, "bass", path) is None
    # Same size, another time: still not the file it was measured on.
    _wav(path, 0.5)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, kept["bass"]["mtime_ns"] + 10_000_000))
    assert stem_level.kept_rms(kept, "bass", path) is None


def test_damaged_records_are_dropped_not_trusted():
    good = {"rms": 0.1, "file": "bass.wav", "size": 10, "mtime_ns": 5}
    value = {
        "bass": good,
        "drums": {**good, "rms": "loud"},
        "piano": {**good, "rms": float("nan")},
        "guitar": {**good, "rms": -1},
        "other": {**good, "size": True},
        "vocals": {**good, "file": ""},
        "keys": "x",
        5: good,
    }
    assert stem_level.clean_stem_rms(value) == {"bass": good}
    assert stem_level.clean_stem_rms(None) == {}
    assert stem_level.clean_stem_rms([good]) == {}
    assert stem_level.kept_rms({"bass": {**good, "rms": "x"}}, "bass", Path("bass.wav")) is None


def test_records_of_a_peaks_pass(tmp_path: Path):
    a = _wav(tmp_path / "bass.wav", 0.3)
    recs = stem_level.level_records(tmp_path, {"bass": a, "drums": 0.2})
    # drums.wav is not there: nothing kept for it, it is measured when asked.
    assert list(recs) == ["bass"]
    assert recs["bass"]["file"] == "bass.wav"
    assert stem_level.kept_rms(recs, "bass", tmp_path / "bass.wav") == a
    assert stem_level.level_record(tmp_path / "bass.wav", float("inf")) is None
