from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.collect import (
    _PEAK_POINTS,
    compute_stem_peaks,
    fill_missing_peaks,
    merge_stem_peaks,
    presence_for_split,
)


def _write_wav(path: Path, samples: list[float], sample_rate: int = 44100) -> None:
    """Write a mono 16-bit PCM WAV file."""
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        data = struct.pack(f"<{len(samples)}h", *[int(s * 32767) for s in samples])
        wf.writeframes(data)


def test_produces_peaks_json(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()

    # 1-second sine wave at 440 Hz
    sr = 44100
    t = np.linspace(0, 1, sr, endpoint=False)
    samples = (np.sin(2 * np.pi * 440 * t) * 0.5).tolist()
    _write_wav(stems_dir / "vocals.wav", samples, sr)

    compute_stem_peaks(stems_dir, ["vocals"])

    peaks_path = stems_dir / "peaks.json"
    assert peaks_path.is_file()
    data = json.loads(peaks_path.read_text())
    assert "vocals" in data
    pts = data["vocals"]
    assert len(pts) <= _PEAK_POINTS
    assert len(pts) > 0
    # each point is [min, max] with min <= 0 <= max (sine wave)
    for mn, mx in pts:
        assert mn <= mx
        assert -1.0 <= mn <= 1.0
        assert -1.0 <= mx <= 1.0


def test_multiple_stems(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    for name in ("vocals", "drums", "bass"):
        _write_wav(stems_dir / f"{name}.wav", [0.1, -0.1, 0.2, -0.2])

    compute_stem_peaks(stems_dir, ["vocals", "drums", "bass"])

    data = json.loads((stems_dir / "peaks.json").read_text())
    assert set(data.keys()) == {"vocals", "drums", "bass"}


def test_skips_missing_wav(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    _write_wav(stems_dir / "drums.wav", [0.1, -0.1])
    # "vocals.wav" intentionally absent

    compute_stem_peaks(stems_dir, ["vocals", "drums"])

    data = json.loads((stems_dir / "peaks.json").read_text())
    assert "drums" in data
    assert "vocals" not in data


def test_no_output_when_all_stems_missing(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()

    compute_stem_peaks(stems_dir, ["vocals", "drums"])

    assert not (stems_dir / "peaks.json").exists()


def test_writes_atomically(tmp_path):
    """No partial peaks.json.tmp should survive a successful run."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    _write_wav(stems_dir / "vocals.wav", [0.1, -0.1, 0.3])

    compute_stem_peaks(stems_dir, ["vocals"])

    assert (stems_dir / "peaks.json").is_file()
    assert not (stems_dir / "peaks.json.tmp").exists()


def test_non_fatal_on_corrupt_wav(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    (stems_dir / "vocals.wav").write_bytes(b"not a wav file at all")
    _write_wav(stems_dir / "drums.wav", [0.1, -0.1])

    # Should not raise; drums should still be computed
    rms_values = compute_stem_peaks(stems_dir, ["vocals", "drums"])

    data = json.loads((stems_dir / "peaks.json").read_text())
    assert "drums" in data
    assert "vocals" not in data
    assert "drums" in rms_values
    assert "vocals" not in rms_values


# ─── #287: RMS returned from the same streamed pass ──────────────────────────


def test_returns_rms_matching_full_load_reference(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    sr = 44100
    t = np.linspace(0, 2, sr * 2, endpoint=False)
    samples = (np.sin(2 * np.pi * 440 * t) * 0.6).tolist()
    _write_wav(stems_dir / "vocals.wav", samples, sr)

    rms_values = compute_stem_peaks(stems_dir, ["vocals"])

    reference, _ = sf.read(stems_dir / "vocals.wav", dtype="float32", always_2d=True)
    expected_rms = float(np.sqrt(np.mean(reference[:, 0].astype(np.float64) ** 2)))
    assert rms_values["vocals"] == pytest.approx(expected_rms, rel=1e-3)


def test_missing_stem_excluded_from_rms(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    _write_wav(stems_dir / "drums.wav", [0.1, -0.1])

    rms_values = compute_stem_peaks(stems_dir, ["vocals", "drums"])

    assert "drums" in rms_values
    assert "vocals" not in rms_values


def test_peaks_match_full_load_reference(tmp_path):
    """Golden test: the streamed implementation's peaks must match the old
    full-load (sf.read + manual chunking) implementation within float
    tolerance for a multi-tone signal."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    sr = 44100
    t = np.linspace(0, 3, sr * 3, endpoint=False)
    samples = (0.5 * np.sin(2 * np.pi * 220 * t) + 0.3 * np.sin(2 * np.pi * 1760 * t)).tolist()
    _write_wav(stems_dir / "vocals.wav", samples, sr)

    compute_stem_peaks(stems_dir, ["vocals"])
    actual = json.loads((stems_dir / "peaks.json").read_text())["vocals"]

    # Reference: the old sf.read()-then-chunk implementation.
    data, _ = sf.read(stems_dir / "vocals.wav", dtype="float32", always_2d=True)
    ch = data[:, 0]
    n = len(ch)
    chunk = max(1, n // _PEAK_POINTS)
    expected = []
    for i in range(0, n, chunk):
        block = ch[i : i + chunk]
        expected.append([float(np.min(block)), float(np.max(block))])
    expected = expected[:_PEAK_POINTS]

    assert len(actual) == len(expected)
    for (a_min, a_max), (e_min, e_max) in zip(actual, expected, strict=True):
        assert a_min == pytest.approx(e_min, abs=1e-4)
        assert a_max == pytest.approx(e_max, abs=1e-4)


def test_split_presence_lands_on_the_same_scale_as_the_base_stems(tmp_path):
    """The lead/backing cards must be comparable with the six the pipeline
    already measured. Presence is RMS against the loudest stem in the job, and
    that reference is never stored -- only the percentages are -- so it has to
    be recovered from the vocals stem rather than recomputed over every file."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    sr = 44100
    t = np.linspace(0, 1, sr, endpoint=False)
    # vocals = lead + backing, exactly as the split produces them.
    lead = 0.4 * np.sin(2 * np.pi * 440 * t)
    backing = 0.1 * np.sin(2 * np.pi * 660 * t)
    _write_wav(stems_dir / "vocals.wav", (lead + backing).tolist(), sr)
    _write_wav(stems_dir / "lead_vocals.wav", lead.tolist(), sr)
    _write_wav(stems_dir / "backing_vocals.wav", backing.tolist(), sr)

    rms_values = merge_stem_peaks(stems_dir, ["vocals", "lead_vocals", "backing_vocals"])
    # Vocals at 50 means the loudest stem in this job is twice as loud as it.
    presence = presence_for_split(rms_values, {"vocals": 50, "drums": 100})

    assert set(presence) == {"lead_vocals", "backing_vocals"}
    loudest = rms_values["vocals"] / 0.5
    for name in ("lead_vocals", "backing_vocals"):
        assert presence[name] == round(rms_values[name] / loudest * 100)
    # Lead carries most of the vocal, so it must read louder than backing and
    # neither may exceed the vocals stem they came from.
    assert presence["lead_vocals"] > presence["backing_vocals"]
    assert presence["lead_vocals"] <= 50


def test_split_presence_is_empty_without_a_reference(tmp_path):
    """No vocals presence recorded (an older job) means there is no scale to
    place the new stems on. Returning nothing leaves the cards reading "--",
    which is honest; inventing a percentage would not be."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    t = np.linspace(0, 1, 44100, endpoint=False)
    _write_wav(stems_dir / "lead_vocals.wav", (0.4 * np.sin(2 * np.pi * 440 * t)).tolist())

    rms_values = merge_stem_peaks(stems_dir, ["vocals", "lead_vocals"])
    assert presence_for_split(rms_values, None) == {}
    assert presence_for_split(rms_values, {"drums": 90}) == {}
    assert presence_for_split(rms_values, {"vocals": 0}) == {}


# --- jobs that never had peaks.json ---


def _tone(seconds: float = 0.5, amp: float = 0.5) -> list[float]:
    t = np.linspace(0, seconds, int(44100 * seconds), endpoint=False)
    return (amp * np.sin(2 * np.pi * 220 * t)).tolist()


def test_fill_missing_peaks_builds_every_stem_and_the_takes(tmp_path):
    """An old or hand-made job opens with no peaks. Building them once lets
    the studio stream it like any other song instead of swapping engines a
    moment after it opened, which lost a take started in that moment."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    for name in ("vocals", "drums", "original"):
        _write_wav(stems_dir / f"{name}.wav", _tone())
    take = tmp_path / "takes" / "t0000aaaa" / "audio.wav"
    take.parent.mkdir(parents=True)
    _write_wav(take, _tone(amp=0.25))

    gone = tmp_path / "takes" / "t0000bbbb" / "audio.wav"
    assert fill_missing_peaks(stems_dir, {"t0000aaaa": take, "t0000bbbb": gone}) is True

    peaks = json.loads((stems_dir / "peaks.json").read_text(encoding="utf-8"))
    assert set(peaks) == {"vocals", "drums", "original", "t0000aaaa"}
    assert len(peaks["vocals"]) == _PEAK_POINTS
    assert max(mx for _, mx in peaks["t0000aaaa"]) == pytest.approx(0.25, abs=0.01)
    assert not list(stems_dir.glob("*.tmp"))


def test_fill_missing_peaks_leaves_an_existing_file_alone(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    _write_wav(stems_dir / "vocals.wav", _tone())
    (stems_dir / "peaks.json").write_text('{"vocals": [[-1, 1]]}', encoding="utf-8")

    assert fill_missing_peaks(stems_dir) is True
    assert json.loads((stems_dir / "peaks.json").read_text(encoding="utf-8")) == {
        "vocals": [[-1, 1]]
    }


def test_fill_missing_peaks_without_audio_writes_nothing(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    (stems_dir / "broken.wav").write_bytes(b"RIFF")

    assert fill_missing_peaks(stems_dir) is False
    assert not (stems_dir / "peaks.json").exists()


def test_a_first_take_on_a_job_without_peaks_brings_the_stems_along(tmp_path):
    """Saving a take writes peaks.json. On a job that had none, a file with the
    take alone would tell the studio it has peaks and draw no stem."""
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    for name in ("vocals", "drums"):
        _write_wav(stems_dir / f"{name}.wav", _tone())
    take = tmp_path / "audio.wav"
    _write_wav(take, _tone(amp=0.25))

    rms = merge_stem_peaks(stems_dir, ["t0000aaaa"], sources={"t0000aaaa": take})

    peaks = json.loads((stems_dir / "peaks.json").read_text(encoding="utf-8"))
    assert set(peaks) == {"vocals", "drums", "t0000aaaa"}
    # Only what was asked for is measured for presence.
    assert set(rms) == {"t0000aaaa"}


def test_removing_a_take_from_a_job_without_peaks_writes_no_empty_file(tmp_path):
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    _write_wav(stems_dir / "vocals.wav", _tone())

    merge_stem_peaks(stems_dir, [], remove=["t0000aaaa"])

    # An empty file would read as "this job has peaks" and stop them being built.
    assert not (stems_dir / "peaks.json").exists()
