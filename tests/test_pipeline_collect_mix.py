"""collect(), make_selected_mix(), make_original_track() and _run_ffmpeg()
against a real ffmpeg on tiny generated WAV stems.

The mixes are checked by their samples, not by the file existing: a stem at
0.1 and a stem at 0.2 mixed with normalize=0 must read back near 0.3, so a
change to the filter graph that halves or drops an input shows up here.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core import registry
from app.core.config import DEMUCS_MODEL, STEM_NAMES, ffmpeg_executable
from app.core.models import Job
from app.pipeline import collect as collect_mod
from app.pipeline.collect import collect, make_original_track, make_selected_mix
from tests.ffmpeg_probe import skip_without_ffmpeg

_RATE = 8000


@pytest.fixture(autouse=True)
def _needs_ffmpeg():
    skip_without_ffmpeg()


def _write_dc(path: Path, level: float, seconds: float = 0.25) -> None:
    """A mono PCM16 WAV holding one constant level, so a mix is checkable by value."""
    n = int(_RATE * seconds)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_RATE)
        wf.writeframes(struct.pack(f"<{n}h", *([int(level * 32767)] * n)))


def _level(path: Path) -> float:
    data, _ = sf.read(str(path), dtype="float32")
    return float(np.median(np.abs(data)))


def _job(selected: list[str]) -> Job:
    return Job(id="c011ec7ab1e0", selected_stems=selected)


@pytest.fixture
def ffmpeg_calls(monkeypatch) -> list[list[str]]:
    """Every command handed to _run_ffmpeg, still run for real."""
    calls: list[list[str]] = []
    real = collect_mod._run_ffmpeg

    def spy(job, cmd):
        calls.append(cmd)
        return real(job, cmd)

    monkeypatch.setattr(collect_mod, "_run_ffmpeg", spy)
    return calls


# --- collect ---------------------------------------------------------------


def test_collect_moves_every_stem_and_removes_the_demucs_dir(tmp_path):
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    for name in ("vocals", "drums"):
        _write_dc(stems_root / f"{name}.wav", 0.1)

    found = collect(_job([]), stems_root, job_dir)

    assert found == ["vocals", "drums"]
    assert (job_dir / "stems" / "vocals.wav").is_file()
    assert (job_dir / "stems" / "drums.wav").is_file()
    assert not (job_dir / DEMUCS_MODEL).exists()


def test_collect_skips_a_missing_stem(tmp_path):
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    _write_dc(stems_root / "bass.wav", 0.1)

    found = collect(_job([]), stems_root, job_dir)

    assert found == ["bass"]
    assert sorted(p.name for p in (job_dir / "stems").iterdir()) == ["bass.wav"]


def test_collect_without_any_stem_raises_and_still_cleans_up(tmp_path):
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="no stems"):
        collect(_job([]), stems_root, job_dir)
    assert not (job_dir / DEMUCS_MODEL).exists()


# --- make_selected_mix -------------------------------------------------------


def test_selected_mix_sums_two_stems_without_normalising(tmp_path):
    _write_dc(tmp_path / "vocals.wav", 0.1)
    _write_dc(tmp_path / "drums.wav", 0.2)

    out = make_selected_mix(_job(["vocals", "drums"]), tmp_path, ["vocals", "drums", "bass"])

    assert out == tmp_path / "mix.wav"
    assert _level(out) == pytest.approx(0.3, abs=0.01)


def test_selected_mix_of_one_stem_points_at_the_stem_itself(tmp_path, ffmpeg_calls):
    _write_dc(tmp_path / "bass.wav", 0.1)

    out = make_selected_mix(_job(["bass"]), tmp_path, ["bass", "drums"])

    assert out == tmp_path / "bass.wav"
    assert not (tmp_path / "mix.wav").exists()
    assert ffmpeg_calls == []


def test_selected_mix_ignores_a_selected_stem_that_was_not_produced(tmp_path):
    _write_dc(tmp_path / "vocals.wav", 0.1)

    # "piano" was asked for but demucs did not produce it: one stem remains.
    out = make_selected_mix(_job(["vocals", "piano"]), tmp_path, ["vocals"])

    assert out == tmp_path / "vocals.wav"


def test_selected_mix_with_nothing_selected_is_none(tmp_path, ffmpeg_calls):
    assert make_selected_mix(_job([]), tmp_path, ["vocals"]) is None
    assert ffmpeg_calls == []


def test_selected_mix_returns_none_when_ffmpeg_fails(tmp_path, caplog):
    (tmp_path / "vocals.wav").write_bytes(b"not audio at all")
    (tmp_path / "drums.wav").write_bytes(b"not audio either")

    with caplog.at_level(logging.WARNING, logger="stemdeck.collect"):
        out = make_selected_mix(_job(["vocals", "drums"]), tmp_path, ["vocals", "drums"])

    assert out is None
    assert "ffmpeg exit" in caplog.text


# --- make_original_track -------------------------------------------------------


def test_original_track_is_the_sum_of_the_unselected_stems(tmp_path):
    _write_dc(tmp_path / "vocals.wav", 0.4)  # selected: must stay out of the mix
    _write_dc(tmp_path / "drums.wav", 0.1)
    _write_dc(tmp_path / "bass.wav", 0.15)

    out = make_original_track(_job(["vocals"]), tmp_path.parent, tmp_path)

    assert out == tmp_path / "original.wav"
    assert _level(out) == pytest.approx(0.25, abs=0.01)


def test_original_track_with_one_unselected_stem_is_a_copy(tmp_path, ffmpeg_calls):
    _write_dc(tmp_path / "vocals.wav", 0.4)
    _write_dc(tmp_path / "guitar.wav", 0.2)
    cmds = ffmpeg_calls

    selected = [s for s in STEM_NAMES if s != "guitar"]
    out = make_original_track(_job(selected), tmp_path.parent, tmp_path)

    assert out == tmp_path / "original.wav"
    assert _level(out) == pytest.approx(0.2, abs=0.01)
    # One input is a straight re-encode: no amix graph over a single stream.
    assert len(cmds) == 1 and "-filter_complex" not in cmds[0]


def test_original_track_is_skipped_when_every_stem_was_kept(tmp_path, ffmpeg_calls):
    for name in STEM_NAMES:
        _write_dc(tmp_path / f"{name}.wav", 0.1)

    assert make_original_track(_job(list(STEM_NAMES)), tmp_path.parent, tmp_path) is None
    assert not (tmp_path / "original.wav").exists()
    assert ffmpeg_calls == []


def test_original_track_is_skipped_when_the_unselected_stems_are_missing(tmp_path, ffmpeg_calls):
    _write_dc(tmp_path / "vocals.wav", 0.1)

    assert make_original_track(_job(["vocals"]), tmp_path.parent, tmp_path) is None
    assert ffmpeg_calls == []


def test_original_track_returns_none_when_ffmpeg_fails(tmp_path):
    (tmp_path / "drums.wav").write_bytes(b"garbage")

    assert make_original_track(_job(["vocals"]), tmp_path.parent, tmp_path) is None


# --- _run_ffmpeg: registration, timeout, cancellation -------------------------------


def _slow_cmd() -> list[str]:
    # -re paces the null source at real time, so this runs for about 30 s.
    return [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=8000:cl=mono",
        "-t",
        "30",
        "-f",
        "null",
        "-",
    ]


def test_run_ffmpeg_success_deregisters_the_process(tmp_path):
    job = _job([])
    src = tmp_path / "a.wav"
    _write_dc(src, 0.1)
    cmd = [ffmpeg_executable(), "-y", "-nostdin", "-i", str(src), str(tmp_path / "b.wav")]

    assert collect_mod._run_ffmpeg(job, cmd) is True
    assert (tmp_path / "b.wav").is_file()
    assert registry.get_proc(job.id) is None


def test_run_ffmpeg_can_be_cancelled_through_the_registry():
    """The cancel API terminates the registered proc; the runner must get
    False well before the ffmpeg would have finished on its own."""
    job = _job([])
    terminated = threading.Event()

    def cancel():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            proc = registry.get_proc(job.id)
            if proc is not None:
                proc.terminate()
                terminated.set()
                return
            time.sleep(0.05)

    t = threading.Thread(target=cancel, daemon=True)
    t.start()
    started = time.monotonic()
    ok = collect_mod._run_ffmpeg(job, _slow_cmd())
    elapsed = time.monotonic() - started
    t.join(5)

    assert terminated.is_set(), "the ffmpeg process was never registered for cancel"
    assert ok is False
    assert elapsed < 20
    assert registry.get_proc(job.id) is None


def test_run_ffmpeg_timeout_kills_and_returns_false(monkeypatch, caplog):
    monkeypatch.setattr(collect_mod, "TIMEOUT_FFMPEG", 0.5)
    job = _job([])

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="stemdeck.collect"):
        ok = collect_mod._run_ffmpeg(job, _slow_cmd())

    assert ok is False
    assert time.monotonic() - started < 20
    assert "timed out" in caplog.text
    assert registry.get_proc(job.id) is None
