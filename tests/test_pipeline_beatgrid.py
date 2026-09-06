from __future__ import annotations

import json
import shutil
import wave

import pytest
from fastapi.testclient import TestClient

from app.core.config import ffmpeg_executable
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.beatgrid import (
    _enforce_grid_consistency,
    _extend_to_track_edges,
    _grid_confidence,
    _sanitize,
    compute_beat_grid,
)

SR = 44100


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


# Resolved the way the app resolves it, so a portable install or a
# STEMDECK_FFMPEG_DIR pointing at a bundled build counts as present. Computed
# once: every test below would otherwise ask the filesystem the same question.
_FFMPEG = shutil.which(ffmpeg_executable())


@pytest.fixture
def stems_dir(tmp_path, monkeypatch):
    """A stems dir that `_load_audio_ffmpeg`'s JOBS_DIR containment check accepts.

    Skips the whole test when ffmpeg is missing rather than letting it fail
    downstream. `_decode_mono` is deliberately forgiving -- everything it feeds
    is a display field, so it logs and returns None instead of raising -- which
    means a missing binary and an undetectable beat grid arrive here as the same
    `assert grid is not None`, and the real cause is only in a log record
    nothing asserts on (#581).

    The guard covers every test taking this fixture, not only the ones that go
    red. The four `test_returns_none_*` cases passed without ffmpeg for the
    wrong reason: they got their None from the decode failing, not from the
    audio, so they could not have caught a regression in what they test.
    """
    if _FFMPEG is None:
        pytest.skip(
            "ffmpeg not found: install it, or point STEMDECK_FFMPEG_DIR at a "
            "directory containing an ffmpeg binary"
        )
    from app.pipeline import analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "JOBS_DIR", tmp_path)
    d = tmp_path / "abcdefabcdef" / "stems"
    d.mkdir(parents=True)
    return d


def _write_hits_wav(path, beat_times: list[float], seconds: float, sr: int = SR) -> list[float]:
    """Write a WAV with a drum-like hit at each given time.

    The timbre matters. An earlier version used alternating-sign bursts, which
    are broadband and transient but sound nothing like a kit -- and the shipping
    detector is a model trained on real drums, so bare bursts sit outside its
    training distribution and it performs far worse on them than on real audio
    (measured 242 ms worst error on a synthetic click ramp against 2 ms once the
    timbre was made realistic). A fixture that does not resemble the input
    domain tests the wrong thing.

    So each hit is a pitch-swept low sine with a noise component: a recognisable
    kick/snare attack. Deterministic (seeded per hit) so tests never flake.
    """

    written = [t for t in beat_times if 0 <= t < seconds]
    _write_voiced_wav(path, [(t, "kick") for t in written], seconds, sr)
    return written


def _write_voiced_wav(path, hits, seconds: float, sr: int = SR) -> None:
    """Render (time, voice) pairs. Voices: kick, snare, hat. Deterministic."""
    import numpy as np

    total = int(seconds * sr)
    buf = np.zeros(total, dtype=np.float64)
    for t, voice in hits:
        if t >= seconds or t < 0:
            continue
        start = int(t * sr)
        dur, amp = {"kick": (0.18, 0.9), "snare": (0.14, 0.85), "hat": (0.05, 0.28)}[voice]
        length = min(int(dur * sr), total - start)
        if length <= 0:
            continue
        i = np.arange(length)
        rng = np.random.default_rng(int(t * 1000) % 9999)
        if voice == "kick":
            # Pitch sweep 110 Hz -> 45 Hz, the shape of a kick attack.
            freq = 110.0 * np.exp(-i / (0.03 * sr)) + 45.0
            body = np.sin(2 * np.pi * np.cumsum(freq) / sr) + rng.standard_normal(length) * 0.35
            decay = 0.045
        elif voice == "snare":
            body = rng.standard_normal(length) * 0.6 + np.sin(2 * np.pi * 190 * i / sr) * 0.4
            decay = 0.022
        else:
            body = rng.standard_normal(length)
            decay = 0.006
        buf[start : start + length] += body * np.exp(-i / (decay * sr)) * amp

    np.clip(buf, -1.0, 1.0, out=buf)
    pcm = (buf * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# Real audio never begins with a transient in sample 0, and spectral flux
# physically cannot detect one there: the onset envelope's first frames are
# identically zero because there is no earlier frame to difference against.
# Starting the fixture at t=0 therefore asserts something no flux-based
# implementation can satisfy (measured: the first beat lands 46 ms out while
# every other beat is within 2 ms). The lead-in keeps the fixture honest.
CLICK_LEAD_IN = 0.5


def _write_kit_wav(path, beat_times: list[float], seconds: float, sr: int = SR) -> list[float]:
    """A realistic kit pattern over the given beat grid.

    Kick on beats 1 and 3, snare on 2 and 4, and a hat on *every* eighth --
    including the on-beats. That last detail matters: with hats only between the
    beats, every eighth carries equal weight and the eighth-note pulse becomes a
    perfectly reasonable reading of the tempo (measured: a 100 BPM pattern
    detected as 199.9). Hats on the beats as well reinforce which pulse is the
    beat, which is what real playing does.

    A bare identical hit on every beat (what `_write_hits_wav` produces) is not
    music at all and leaves the phase unconstrained: on one such fixture the
    model locked onto the off-beat with zero onset support under the whole grid.
    """
    hits: list[tuple[float, str]] = []
    for i, t in enumerate(beat_times):
        hits.append((t, "kick" if i % 4 in (0, 2) else "snare"))
        hits.append((t, "hat"))
        nxt = beat_times[i + 1] if i + 1 < len(beat_times) else None
        if nxt is not None:
            hits.append(((t + nxt) / 2.0, "hat"))
    _write_voiced_wav(path, hits, seconds, sr)
    return [t for t in beat_times if 0 <= t < seconds]


def _write_click_wav(
    path, bpm: float, seconds: float, sr: int = SR, start: float = CLICK_LEAD_IN
) -> list[float]:
    """Constant-tempo click track, offset by a short lead-in."""
    interval = 60.0 / bpm
    n = int((seconds - start) / interval) + 2
    times = [start + i * interval for i in range(n)]
    return _write_hits_wav(path, times, seconds, sr)


# --- _sanitize -------------------------------------------------------------


def test_sanitize_sorts_and_clamps():
    assert _sanitize([3.0, 1.0, 2.0], duration=10.0) == [1.0, 2.0, 3.0]


def test_sanitize_drops_out_of_range_and_non_finite():
    assert _sanitize([-1.0, 1.0, float("nan"), float("inf"), 99.0], duration=10.0) == [1.0]


def test_sanitize_collapses_near_duplicates():
    # Two beats 0.5 ms apart cannot both be real; the scheduler needs strict
    # monotonicity, so the second is dropped.
    assert _sanitize([1.0, 1.0005, 2.0], duration=10.0) == [1.0, 2.0]


def test_sanitize_empty():
    assert _sanitize([], duration=10.0) == []


# --- _grid_confidence ------------------------------------------------------


def test_confidence_full_when_every_beat_has_an_onset():
    beats = [0.0, 0.5, 1.0, 1.5]
    assert _grid_confidence(beats, beats, tol=0.05) == 100


def test_confidence_zero_when_onsets_are_off_grid():
    beats = [0.0, 0.5, 1.0, 1.5]
    onsets = [0.25, 0.75, 1.25, 1.75]  # exactly off-beat
    assert _grid_confidence(beats, onsets, tol=0.05) == 0


def test_confidence_zero_without_onsets():
    assert _grid_confidence([0.0, 0.5], [], tol=0.05) == 0


def test_confidence_half_time_grid_is_penalised():
    # Onsets every 0.25 s but a grid every 0.5 s still matches (subset), while
    # the reverse -- grid finer than the onsets -- is what scores badly.
    beats = [0.0, 0.25, 0.5, 0.75, 1.0]
    onsets = [0.0, 0.5, 1.0]
    assert _grid_confidence(beats, onsets, tol=0.02) == 60


# --- _enforce_grid_consistency ---------------------------------------------


def test_consistency_leaves_a_clean_grid_alone():
    beats = [i * 0.5 for i in range(10)]
    out, corrected = _enforce_grid_consistency(beats)
    assert corrected == 0
    assert out == pytest.approx(beats)


def test_consistency_pulls_back_a_stray_interior_beat():
    beats = [i * 0.5 for i in range(10)]
    beats[4] = 2.09  # 90 ms off a 500 ms grid
    out, corrected = _enforce_grid_consistency(beats)
    assert corrected == 1
    assert out[4] == pytest.approx(2.0, abs=1e-9)


def test_consistency_pulls_back_a_stray_first_beat():
    """The real-world failure: beat_track reaching into a quiet intro."""
    beats = [i * 0.5 for i in range(10)]
    beats[0] = 0.09
    out, corrected = _enforce_grid_consistency(beats)
    assert corrected == 1
    assert out[0] == pytest.approx(0.0, abs=1e-9)


def test_consistency_preserves_human_microtiming():
    """A drummer pushing and dragging by ~20 ms is musical, not error."""
    beats = [i * 0.5 for i in range(12)]
    for i, shift in ((3, 0.018), (6, -0.021), (9, 0.015)):
        beats[i] += shift
    out, corrected = _enforce_grid_consistency(beats)
    assert corrected == 0
    assert out == pytest.approx(beats)


def test_consistency_no_op_below_three_beats():
    assert _enforce_grid_consistency([1.0, 2.0]) == ([1.0, 2.0], 0)


# --- _extend_to_track_edges ------------------------------------------------


def test_extend_fills_intro_and_outro():
    # Grid covering 5.0 - 9.0 s of a 15 s track, 0.5 s beats.
    beats = [5.0 + i * 0.5 for i in range(9)]
    out, head, tail = _extend_to_track_edges(beats, duration=15.0, irregularity=0.01)
    assert head == 10, "should reach back to 0.0"
    assert tail == 12, "should reach forward to 15.0"
    assert out[0] == pytest.approx(0.0, abs=1e-9)
    assert out[-1] <= 15.0
    # Still one uniform grid, ascending, at the original tempo.
    diffs = [b - a for a, b in zip(out, out[1:], strict=False)]
    assert max(diffs) == pytest.approx(0.5, abs=1e-9)
    assert min(diffs) == pytest.approx(0.5, abs=1e-9)


def test_extend_refuses_an_unsteady_grid():
    """Extending a wandering grid would invent beats with nothing behind them.
    Gated on IQR/median, so one drum-free hole in an otherwise steady track does
    not disqualify it."""
    beats = [5.0 + i * 0.5 for i in range(9)]
    out, head, tail = _extend_to_track_edges(beats, duration=15.0, irregularity=0.5)
    assert (head, tail) == (0, 0)
    assert out == beats


def test_extend_is_a_no_op_when_grid_already_spans_the_track():
    beats = [i * 0.5 for i in range(5)]
    out, head, tail = _extend_to_track_edges(beats, duration=2.0, irregularity=0.01)
    assert (head, tail) == (0, 0)
    assert out == beats


def test_extend_handles_too_few_beats():
    assert _extend_to_track_edges([1.0, 1.5], duration=10.0, irregularity=0.01) == (
        [1.0, 1.5],
        0,
        0,
    )


def test_grid_covers_a_track_whose_drums_start_late(stems_dir):
    """End-to-end version of the real-song gap: a click must not go silent for
    the intro just because the drummer has not come in yet."""
    bpm = 120.0
    interval = 60.0 / bpm
    # Hits only between 6 s and 20 s of a 26 s file.
    times = [t for t in (i * interval for i in range(300)) if 6.0 <= t <= 20.0]
    _write_hits_wav(stems_dir / "drums.wav", times, seconds=26.0)

    grid = compute_beat_grid(stems_dir)
    assert grid is not None
    assert grid["extrapolated_head"] > 0
    assert grid["extrapolated_tail"] > 0
    assert grid["beats"][0] < 1.0, "click should start near the top of the track"
    assert grid["beats"][-1] > 25.0, "click should run to the end of the track"
    # Extrapolated beats stay on the detected tempo.
    worst = max(abs(b - round(b / interval) * interval) for b in grid["beats"])
    assert worst <= 0.010, f"worst {worst * 1000:.1f} ms"
    # Confidence describes detection, so it must not be dragged down by the
    # extrapolated beats that have no onset under them.
    assert grid["confidence"] >= 80
    assert grid["detected"] < len(grid["beats"])


# --- compute_beat_grid: ground truth ---------------------------------------


@pytest.mark.parametrize("bpm", [90.0, 120.0, 140.0])
def test_detects_exact_tempo_of_synthetic_click(stems_dir, bpm):
    _write_click_wav(stems_dir / "drums.wav", bpm=bpm, seconds=30.0)

    grid = compute_beat_grid(stems_dir)
    assert grid is not None, "a clean synthetic click track must produce a grid"

    assert abs(grid["bpm"] - bpm) < 0.5, f"got {grid['bpm']}, expected {bpm}"
    assert grid["source"] == "drums"
    assert grid["confidence"] >= 90

    # Every beat must sit on the ideal grid. Compare against k*interval rather
    # than the emitted hit list: the tracker legitimately extrapolates a beat
    # past the final hit (the grid continues even when nothing is played), and
    # that beat is correct, not spurious.
    #
    # 5 ms is the real claim being made here. Raw librosa output is quantised
    # to the ~23 ms hop; anything near that ceiling means _refine_beats has
    # regressed and the click would flam against the drums.
    interval = 60.0 / bpm
    off = lambda b: b - CLICK_LEAD_IN - round((b - CLICK_LEAD_IN) / interval) * interval  # noqa: E731
    worst = max(abs(off(b)) for b in grid["beats"])
    assert worst <= 0.005, f"worst beat error {worst * 1000:.1f} ms exceeds the 5 ms budget"


def test_refinement_beats_raw_hop_quantisation(stems_dir):
    """Guard the refinement pass itself.

    A click placed on the raw 512-hop grid lands up to ~12 ms from the true
    transient and, because the error changes beat to beat, audibly jitters
    against a steady drummer. This asserts the refined grid is at least an
    order of magnitude tighter than that ceiling.
    """
    bpm = 132.0
    _write_click_wav(stems_dir / "drums.wav", bpm=bpm, seconds=25.0)
    grid = compute_beat_grid(stems_dir)
    assert grid is not None

    interval = 60.0 / bpm
    errors = [
        abs(b - CLICK_LEAD_IN - round((b - CLICK_LEAD_IN) / interval) * interval)
        for b in grid["beats"]
    ]
    # Refinement measures ~2 ms worst case; raw hop output is ~12 ms. A failure
    # here means the search window or FFT size regressed.
    assert max(errors) <= 0.004, f"worst {max(errors) * 1000:.1f} ms"
    assert sum(errors) / len(errors) <= 0.003

    # Jitter, not absolute offset, is what a listener hears as flam against the
    # drums. A constant offset this small is inaudible; scatter is not.
    signed = [
        b - CLICK_LEAD_IN - round((b - CLICK_LEAD_IN) / interval) * interval for b in grid["beats"]
    ]
    mean = sum(signed) / len(signed)
    variance = sum((s - mean) ** 2 for s in signed) / len(signed)
    assert variance**0.5 <= 0.001, f"jitter {variance**0.5 * 1000:.2f} ms"

    # Nearly every beat has a real transient under it in a synthetic click.
    assert grid["refined"] >= len(grid["beats"]) - 2


def test_grid_follows_a_gradual_tempo_change(stems_dir):
    """The point of persisting beat *times* rather than a single BPM.

    A fixed period extrapolated from an average tempo drifts steadily out of
    phase with a track that speeds up. Because the click plays the stored times,
    it follows the change.

    This asserts that property -- the grid accelerates with the music and is not
    a uniform lattice -- rather than per-beat millisecond accuracy. A synthetic
    tempo ramp is well outside the training distribution of the model detector
    (measured: 2 ms worst error under librosa against 242 ms under the model on
    the same fixture, while on realistic drum patterns the model is the more
    accurate of the two). Pinning tight accuracy here would be measuring the
    fixture's unrealism, and the constant-tempo tests above already cover
    placement precision.
    """
    # 100 -> 120 BPM linear accelerando over ~28 s.
    times: list[float] = []
    t, bpm = 0.5, 100.0
    while t < 28.0:
        times.append(t)
        t += 60.0 / bpm
        bpm = min(120.0, bpm + 0.45)
    _write_hits_wav(stems_dir / "drums.wav", times, seconds=30.0)

    grid = compute_beat_grid(stems_dir)
    assert grid is not None
    beats = [b for b in grid["beats"] if b <= times[-1]]
    assert len(beats) >= len(times) * 0.8, "tracker lost most of the ramp"

    # The grid must speed up: the last stretch is measurably faster than the
    # first. This is what a fixed-BPM grid could never reproduce.
    def local_bpm(seq):
        iv = [b - a for a, b in zip(seq, seq[1:], strict=False)]
        iv.sort()
        return 60.0 / iv[len(iv) // 2]

    head = local_bpm(beats[: len(beats) // 3])
    tail = local_bpm(beats[-len(beats) // 3 :])
    assert tail > head * 1.05, f"grid did not accelerate: {head:.1f} -> {tail:.1f} BPM"

    # And it must not be a uniform lattice, which is what a single fixed period
    # would produce.
    assert grid["interval_cv"] > 0.01


def test_beats_are_strictly_increasing_and_in_range(stems_dir):
    _write_click_wav(stems_dir / "drums.wav", bpm=120.0, seconds=20.0)
    grid = compute_beat_grid(stems_dir)
    assert grid is not None
    beats = grid["beats"]
    assert all(b2 > b1 for b1, b2 in zip(beats, beats[1:], strict=False))
    assert beats[0] >= 0.0
    assert beats[-1] <= grid["duration"]


def test_writes_beats_json_atomically(stems_dir):
    _write_click_wav(stems_dir / "drums.wav", bpm=120.0, seconds=20.0)
    grid = compute_beat_grid(stems_dir)
    assert grid is not None

    out = stems_dir / "beats.json"
    assert out.is_file()
    assert not (stems_dir / "beats.json.tmp").exists(), "temp file must be renamed away"
    assert json.loads(out.read_text()) == grid


# --- compute_beat_grid: source selection + failure modes -------------------


def test_prefers_drums_over_original(stems_dir):
    _write_click_wav(stems_dir / "drums.wav", bpm=120.0, seconds=20.0)
    _write_click_wav(stems_dir / "original.wav", bpm=90.0, seconds=20.0)
    grid = compute_beat_grid(stems_dir)
    assert grid is not None
    assert grid["source"] == "drums"
    assert abs(grid["bpm"] - 120.0) < 1.0


def test_falls_back_to_original_without_drums(stems_dir):
    _write_click_wav(stems_dir / "original.wav", bpm=100.0, seconds=20.0)
    grid = compute_beat_grid(stems_dir)
    assert grid is not None
    assert grid["source"] == "original"


def test_returns_none_without_any_source(stems_dir):
    assert compute_beat_grid(stems_dir) is None
    assert not (stems_dir / "beats.json").exists()


def test_returns_none_on_silence(stems_dir):
    """A silent stem has no onset envelope. Emitting a grid anyway would mean
    clicking confidently over nothing."""
    with wave.open(str(stems_dir / "drums.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"\x00\x00" * SR * 10)
    assert compute_beat_grid(stems_dir) is None


def test_returns_none_on_corrupt_file(stems_dir):
    (stems_dir / "drums.wav").write_bytes(b"not a wav file at all")
    assert compute_beat_grid(stems_dir) is None


def test_returns_none_on_too_short_a_clip(stems_dir):
    """Under _MIN_BEATS worth of audio: no usable grid."""
    _write_click_wav(stems_dir / "drums.wav", bpm=120.0, seconds=1.0)
    assert compute_beat_grid(stems_dir) is None


def test_never_raises_on_unreadable_dir(tmp_path, monkeypatch):
    from app.pipeline import analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "JOBS_DIR", tmp_path)
    assert compute_beat_grid(tmp_path / "does-not-exist") is None


def test_source_outside_jobs_dir_is_refused(tmp_path, monkeypatch):
    """Containment guard: a stems dir outside JOBS_DIR must not be decoded."""
    from app.pipeline import analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    outside = tmp_path / "elsewhere" / "stems"
    outside.mkdir(parents=True)
    _write_click_wav(outside / "drums.wav", bpm=120.0, seconds=20.0)
    assert compute_beat_grid(outside) is None


# --- endpoint --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    from app.main import app

    return TestClient(app)


def _write_grid(tmp_path, job_id: str, payload: dict) -> None:
    d = tmp_path / job_id / "stems"
    d.mkdir(parents=True, exist_ok=True)
    (d / "beats.json").write_text(json.dumps(payload), encoding="utf-8")


def test_beats_endpoint_serves_grid(client, tmp_path):
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    payload = {"version": 1, "bpm": 120.0, "beats": [0.0, 0.5, 1.0]}
    _write_grid(tmp_path, job.id, payload)

    r = client.get(f"/api/jobs/{job.id}/stems/beats.json")
    assert r.status_code == 200
    assert r.json() == payload


def test_beats_endpoint_404s_for_legacy_job(client, tmp_path):
    """Jobs separated before this stage existed have no beats.json. That is a
    normal, expected 404 -- the UI hides the click control rather than erroring."""
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    (tmp_path / job.id / "stems").mkdir(parents=True)
    r = client.get(f"/api/jobs/{job.id}/stems/beats.json")
    assert r.status_code == 404


def test_beats_endpoint_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcd-efabcdef", "%2e%2e%2fetc"):
        r = client.get(f"/api/jobs/{bad_id}/stems/beats.json")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


def test_beats_endpoint_requires_done_status(client, tmp_path):
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    _write_grid(tmp_path, job.id, {"version": 1, "beats": []})
    r = client.get(f"/api/jobs/{job.id}/stems/beats.json")
    assert r.status_code == 404


def test_beats_endpoint_404s_for_unknown_job(client):
    r = client.get("/api/jobs/abcdefabcdef/stems/beats.json")
    assert r.status_code == 404


def test_beats_route_does_not_shadow_stem_wav(client, tmp_path):
    """The literal beats.json route must not swallow /stems/{name}.wav."""
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    d = tmp_path / job.id / "stems"
    d.mkdir(parents=True)
    (d / "drums.wav").write_bytes(b"RIFF1234")
    r = client.get(f"/api/jobs/{job.id}/stems/drums.wav")
    assert r.status_code == 200
