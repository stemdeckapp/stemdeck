from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.collect import _PEAK_POINTS, compute_stem_peaks


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
