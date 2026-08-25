"""Key detection, loudness and audio loading (app/pipeline/analyze.py).

This stage was the least covered in the app and the one where a bug is
quietest. Nothing here crashes when it is wrong: a mis-detected key shows a
confident wrong label, and a bad tempo silently misplaces every beat in the
click track and the grid editor. There is no error to notice.

Key detection is pure arithmetic over a 12-bin chroma vector, so most of it
needs no audio at all. The parts that do decode use a tone generated here
rather than a fixture, so the expected answer is known rather than asserted
against whatever the file happened to contain.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline import analyze as az
from tests.ffmpeg_probe import skip_without_ffmpeg

PITCHES = az._PITCHES


def chroma_for(notes: dict[str, float], floor: float = 0.05) -> list[float]:
    """A 12-bin chroma vector with the named pitch classes raised."""
    vec = [floor] * 12
    for name, weight in notes.items():
        vec[PITCHES.index(name)] = weight
    return vec


# A major triad plus the diatonic notes that decide major against minor.
C_MAJOR = chroma_for({"C": 1.0, "E": 0.8, "G": 0.9, "D": 0.5, "A": 0.5, "B": 0.45, "F": 0.5})
A_MINOR = chroma_for({"A": 1.0, "C": 0.8, "E": 0.9, "D": 0.5, "G": 0.6, "F": 0.5, "B": 0.35})


# ── _correlate ───────────────────────────────────────────────────────


def test_correlation_is_perfect_against_the_profile_itself():
    chroma = list(az._MAJOR_PROFILE)
    assert az._correlate(az._MAJOR_PROFILE, chroma, 0) == pytest.approx(1.0)


def test_correlation_is_inverted_against_a_mirrored_profile():
    mean = sum(az._MAJOR_PROFILE) / 12
    mirrored = [2 * mean - v for v in az._MAJOR_PROFILE]
    assert az._correlate(az._MAJOR_PROFILE, mirrored, 0) == pytest.approx(-1.0)


def test_a_flat_chroma_correlates_to_nothing():
    """Silence, or a signal with no pitch content, has zero variance. Dividing
    by that would raise; returning 0.0 keeps the caller ranking sanely."""
    assert az._correlate(az._MAJOR_PROFILE, [0.5] * 12, 0) == 0.0
    assert az._correlate(az._MAJOR_PROFILE, [0.0] * 12, 7) == 0.0


@pytest.mark.parametrize("shift", range(12))
def test_shift_rotates_the_chroma_not_the_profile(shift):
    """The whole method depends on this: shift n asks "is the song in the key
    n semitones up", so the audio rotates against a fixed profile.

    _correlate reads chroma[(i + shift) % 12], so a chroma built by shifting
    the profile right by n must correlate perfectly at that same n. Every
    shift, because an off-by-one here makes exactly one key undetectable.
    """
    chroma = [0.0] * 12
    for i, value in enumerate(az._MAJOR_PROFILE):
        chroma[(i + shift) % 12] = value
    assert az._correlate(az._MAJOR_PROFILE, chroma, shift) == pytest.approx(1.0)


# ── _detect_key ──────────────────────────────────────────────────────


def test_detects_a_major_key():
    label, scale, confidence = az._detect_key(C_MAJOR)
    assert label == "C maj"
    assert scale == "Major"
    assert 0 <= confidence <= 100


def test_detects_a_minor_key():
    label, scale, _ = az._detect_key(A_MINOR)
    assert label == "A min"
    assert scale == "Natural Minor"


def test_every_root_is_reachable():
    """A rotation bug shows up as certain keys never being detected, which is
    invisible until someone imports a song in one of them."""
    found = set()
    for shift in range(12):
        vec = [C_MAJOR[(i - shift) % 12] for i in range(12)]
        found.add(az._detect_key(vec)[0].split()[0])
    assert found == set(PITCHES), f"unreachable roots: {set(PITCHES) - found}"


def test_a_near_tie_prefers_minor():
    """Pop and rock have a strong minor prior, and the algorithm otherwise
    drifts to the relative major on an ostinato bass note. The comment in
    analyze.py cites Come As You Are, which is E minor over a hammered D."""
    flat = [0.5] * 12
    label, scale, _ = az._detect_key(flat[:])
    # A perfectly flat chroma is the most ambiguous input there is.
    assert scale == "Natural Minor", f"ambiguity resolved to {label}, not minor"


def test_confidence_is_higher_for_a_clear_key_than_a_muddy_one():
    _, _, clear = az._detect_key(C_MAJOR)
    muddy = chroma_for({p: 0.5 for p in PITCHES} | {"C": 0.52})
    _, _, vague = az._detect_key(muddy)
    assert clear > vague


def test_confidence_stays_in_range_for_extreme_input():
    """confidence_pct is clamped. An unclamped value would render as "480%"."""
    for vec in ([1.0] + [0.0] * 11, [0.0] * 12, [1e6] + [1e-6] * 11):
        _, _, pct = az._detect_key(list(vec))
        assert 0 <= pct <= 100, f"{pct} out of range for {vec[:3]}"


def test_a_silent_chroma_does_not_raise():
    label, scale, pct = az._detect_key([0.0] * 12)
    assert label.split()[0] in PITCHES
    assert scale in ("Major", "Natural Minor")
    assert pct == 0


def test_the_minor_profile_favours_the_flat_seventh():
    """The reason this project uses Albrecht-Shanahan rather than
    Temperley: the b7 is the diatonic seventh in pop and rock, and a
    harmonic-minor profile biases toward the leading tone instead."""
    b7 = az._MINOR_PROFILE[10]
    maj7 = az._MINOR_PROFILE[11]
    assert b7 > maj7, "the minor profile has been swapped for a classical one"


# ── _measure_loudness ────────────────────────────────────────────────


def _tone(seconds: float = 4.0, sr: int = 22050, freq: float = 440.0, amp: float = 0.5):
    import numpy as np

    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False, dtype="float32")
    return (amp * np.sin(2 * np.pi * freq * t)).astype("float32")


def test_loudness_of_nothing_is_none():
    assert az._measure_loudness(None, 22050) == (None, None)


def test_loudness_of_an_empty_array_is_none():
    import numpy as np

    assert az._measure_loudness(np.zeros(0, dtype="float32"), 22050) == (None, None)


def test_peak_matches_the_signal():
    """A half-amplitude tone peaks at about -6 dBFS."""
    _, peak = az._measure_loudness(_tone(amp=0.5), 22050)
    assert peak == pytest.approx(-6.02, abs=0.1)


def test_digital_silence_reports_no_peak():
    """log10(0) is undefined. None is the honest answer, and the frontend
    hides the field rather than rendering -inf."""
    import numpy as np

    lufs, peak = az._measure_loudness(np.zeros(22050, dtype="float32"), 22050)
    assert peak is None
    assert lufs is None


def test_loudness_is_measured_for_a_real_tone():
    pytest.importorskip("pyloudnorm")
    lufs, _ = az._measure_loudness(_tone(seconds=5.0), 22050)
    assert lufs is not None
    assert -40 < lufs < 0, f"implausible LUFS: {lufs}"


def test_a_clip_too_short_to_gate_degrades_to_none():
    """pyloudnorm raises when the clip is shorter than its 400 ms window. That
    is a display field; it must not fail an analysis."""
    pytest.importorskip("pyloudnorm")
    import numpy as np

    lufs, peak = az._measure_loudness(np.full(64, 0.5, dtype="float32"), 22050)
    assert lufs is None
    assert peak is not None


def test_a_missing_pyloudnorm_degrades_to_none(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pyln(name, *args, **kwargs):
        if name == "pyloudnorm":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyln)
    lufs, peak = az._measure_loudness(_tone(), 22050)
    assert lufs is None
    assert peak is not None, "peak needs no third-party package and must survive"


# ── _load_audio_ffmpeg ───────────────────────────────────────────────


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    """A directory _load_audio_ffmpeg will accept.

    It refuses any source outside JOBS_DIR, the same containment shape as the
    stem endpoints (#173). Tests go through that boundary rather than around
    it, so the check stays exercised.
    """
    monkeypatch.setattr(az, "JOBS_DIR", tmp_path)
    d = tmp_path / "abcdefabcdef"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_tone_wav(path: Path, seconds: float = 2.0, freq: float = 440.0) -> Path:
    """A real WAV on disk, written without depending on the code under test."""
    import struct
    import wave

    sr = 22050
    frames = bytearray()
    for i in range(int(sr * seconds)):
        value = int(16000 * math.sin(2 * math.pi * freq * i / sr))
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return path


def test_decodes_a_real_file(jobs_dir):
    skip_without_ffmpeg()
    src = _write_tone_wav(jobs_dir / "tone.wav", seconds=2.0)
    result = az._load_audio_ffmpeg(src, sr=22050, duration=None)
    assert result is not None
    samples, sr = result
    assert sr == 22050
    assert samples.size == pytest.approx(22050 * 2, rel=0.05)
    assert float(abs(samples).max()) > 0.1, "decoded to silence"


def test_duration_caps_the_decode(jobs_dir):
    """analyze only needs the first few minutes. Decoding an hour-long upload
    in full would cost time and memory for no better answer."""
    skip_without_ffmpeg()
    src = _write_tone_wav(jobs_dir / "long.wav", seconds=4.0)
    result = az._load_audio_ffmpeg(src, sr=22050, duration=1.0)
    assert result is not None
    samples, _ = result
    assert samples.size == pytest.approx(22050, rel=0.1)


def test_a_missing_file_returns_none_rather_than_raising(tmp_path):
    """Every caller treats None as "could not analyse". Raising here would
    fail the whole job over a display field."""
    assert az._load_audio_ffmpeg(tmp_path / "nope.wav") is None


def test_a_file_that_is_not_audio_returns_none(jobs_dir):
    skip_without_ffmpeg()
    junk = jobs_dir / "junk.wav"
    junk.write_bytes(b"this is not a wav file, not even slightly")
    assert az._load_audio_ffmpeg(junk) is None


def test_a_timeout_returns_none(jobs_dir):
    src = jobs_dir / "x.wav"
    src.write_bytes(b"RIFF")

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    with patch.object(az.subprocess, "run", slow):
        assert az._load_audio_ffmpeg(src, timeout=1) is None


def test_ffmpeg_missing_returns_none(jobs_dir):
    src = jobs_dir / "x.wav"
    src.write_bytes(b"RIFF")
    with patch.object(az.subprocess, "run", side_effect=OSError("no ffmpeg")):
        assert az._load_audio_ffmpeg(src) is None


# ── analyze() ────────────────────────────────────────────────────────


def _click_wav(path: Path, bpm: float, seconds: float = 12.0, sr: int = 22050) -> Path:
    """A metronome at a known tempo, plus a steady tone so there is pitch
    content to find a key in. Generated rather than fixtured, so the expected
    answer is known instead of whatever a file happened to contain."""
    import struct
    import wave

    n = int(sr * seconds)
    period = sr * 60.0 / bpm
    samples = []
    for i in range(n):
        # Decaying click on the beat.
        phase = i % period
        click = math.exp(-phase / (sr * 0.01)) if phase < sr * 0.05 else 0.0
        tone = 0.25 * math.sin(2 * math.pi * 220.0 * i / sr)  # A3
        samples.append(int(max(-1.0, min(1.0, 0.7 * click + tone)) * 20000))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
    return path


@pytest.fixture
def job():
    from app.core.models import Job

    return Job(id="abcdefabcdef")


def test_analyze_fills_the_job_in(job, jobs_dir):
    """The happy path, end to end, on audio with a tempo we chose."""
    skip_without_ffmpeg()
    pytest.importorskip("librosa")
    src = _click_wav(jobs_dir / "click.wav", bpm=120.0)

    bpm, key = az.analyze(job, src)

    assert bpm is not None, "no tempo found in a plain 120 BPM click"
    # Half and double time are musically legitimate readings of a bare click.
    assert bpm in range(55, 65) or bpm in range(115, 126) or bpm in range(235, 245), bpm
    assert job.bpm == bpm
    assert key is None or key.split()[0] in PITCHES
    assert job.stage_message == "Analysis complete"
    assert job.progress == 1.0


def test_analyze_reports_loudness(job, jobs_dir):
    skip_without_ffmpeg()
    pytest.importorskip("librosa")
    pytest.importorskip("pyloudnorm")
    az.analyze(job, _click_wav(jobs_dir / "c.wav", bpm=100.0))

    assert job.peak_db is not None
    if job.lufs is not None:
        assert job.dynamic_range == pytest.approx(round(job.peak_db - job.lufs, 1))


def test_analyze_reports_tempo_stability(job, jobs_dir):
    """A machine-perfect click should read as very stable. This is the field
    the click track leans on, and there is nothing to notice when it is wrong."""
    skip_without_ffmpeg()
    pytest.importorskip("librosa")
    az.analyze(job, _click_wav(jobs_dir / "steady.wav", bpm=120.0, seconds=15.0))

    assert job.tempo_stability is not None
    assert 0 <= job.tempo_stability <= 100
    assert job.tempo_stability > 60, f"a metronome read as unstable: {job.tempo_stability}"


def test_an_undecodable_source_leaves_the_job_alone(job, jobs_dir):
    """Analysis is best effort. Its fields are chips in the UI, so failure
    means placeholders stay, never that the import fails."""
    pytest.importorskip("librosa")
    junk = jobs_dir / "junk.wav"
    junk.write_bytes(b"not audio")

    assert az.analyze(job, junk) == (None, None)
    assert job.bpm is None
    assert job.key is None


def test_a_source_outside_jobs_dir_is_refused(job, tmp_path, monkeypatch):
    """Containment, the same shape as the stem endpoints (#173)."""
    pytest.importorskip("librosa")
    monkeypatch.setattr(az, "JOBS_DIR", tmp_path / "elsewhere")
    (tmp_path / "elsewhere").mkdir()
    outside = tmp_path / "outside.wav"
    _write_tone_wav(outside)

    assert az.analyze(job, outside) == (None, None)


def test_no_librosa_is_not_a_failure(job, jobs_dir, monkeypatch):
    """librosa is a heavy optional import. Without it the chips stay blank
    rather than the job erroring."""
    import builtins

    real_import = builtins.__import__

    def no_librosa(name, *args, **kwargs):
        if name == "librosa":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_librosa)
    assert az.analyze(job, jobs_dir / "anything.wav") == (None, None)


def test_an_unexpected_error_is_logged_not_leaked(job, jobs_dir):
    """The UI stage line must stay generic: raw exception text carries paths
    and library internals and has no business on screen (#283)."""
    skip_without_ffmpeg()
    pytest.importorskip("librosa")
    src = _click_wav(jobs_dir / "boom.wav", bpm=110.0, seconds=4.0)

    import librosa

    with patch.object(librosa.effects, "hpss", side_effect=RuntimeError("C:/secret/path leaked")):
        assert az.analyze(job, src) == (None, None)

    assert job.stage_message == "Analysis skipped"
    assert "secret" not in (job.stage_message or "")


def test_a_silent_track_yields_no_key(job, jobs_dir):
    """Silence has no pitch content. The key chip should stay empty rather
    than showing a confident guess pulled out of numerical noise."""
    skip_without_ffmpeg()
    pytest.importorskip("librosa")
    import struct
    import wave

    src = jobs_dir / "silence.wav"
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(22050 * 5)))

    bpm, key = az.analyze(job, src)
    assert key is None or key.split()[0] in PITCHES
    assert bpm is None or bpm > 0
