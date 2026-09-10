"""The last guards in the pipeline stages, one per branch.

What is left after the stage-level suites: a handful of single-line refusals
that each stand between a degenerate intermediate result and a grid, a section
list, or a video export built on it. They are cheap to write off as
unreachable -- most are reached only when a helper one call up misbehaves --
and that is exactly what they are for: every one of them was placed so a bad
value stops at the stage boundary instead of reaching the editor.
"""

from __future__ import annotations

import io
import subprocess
import sys
import urllib.parse

import numpy as np
import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import analyze as an
from app.pipeline import beat_detect as bd
from app.pipeline import beatgrid as bg
from app.pipeline import download as dl
from app.pipeline import section_refine as sr
from app.pipeline import sections as sx

JOB = "abcdefabcdef"
SR = 22050


# --------------------------------------------------------------------------
# a beatgrid stage with the DSP stubbed out and the discard rules left real
# --------------------------------------------------------------------------


@pytest.fixture
def stems_dir(tmp_path):
    import soundfile as sf

    d = tmp_path / "stems"
    d.mkdir()
    t = np.linspace(0, 2.0, 16000, endpoint=False)
    sf.write(str(d / "drums.wav"), (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), 8000)
    return d


def _impulsive(seconds=30.0, interval=0.5, sr=SR):
    """Noise bursts on a regular grid: enough onset energy that the stage's
    envelope checks pass and the guard under test is what decides."""
    rng = np.random.default_rng(0)
    y = np.zeros(int(sr * seconds), dtype=np.float32)
    burst = int(sr * 0.01)
    for k in range(int(seconds / interval)):
        start = int(k * interval * sr)
        y[start : start + burst] = rng.uniform(-1.0, 1.0, size=burst).astype(np.float32)
    return y


@pytest.fixture
def stubbed_stage(monkeypatch):
    """Real discard rules, stubbed decode and detector, and a `sanitize` hook
    the tests replace to inject the degenerate lists _sanitize itself would
    never emit."""
    state: dict[str, object] = {"sanitize": bg._sanitize, "real": bg._sanitize}

    monkeypatch.setattr(
        bg,
        "_load_audio_ffmpeg",
        lambda source, sr=SR, duration=None, timeout=None: (_impulsive(), SR),
    )
    monkeypatch.setattr(
        bg, "detect_beats", lambda y, sr, onset_env: ([i * 0.5 for i in range(60)], [], "stub")
    )
    monkeypatch.setattr(bg, "_sanitize", lambda *a, **kw: state["sanitize"](*a, **kw))
    return state


# --------------------------------------------------------------------------
# analyze: a decode that produced nothing
# --------------------------------------------------------------------------


def test_a_decode_that_returned_no_samples_is_not_a_waveform(tmp_path, monkeypatch):
    """ffmpeg exits 0 having written nothing for a container it opened but
    could not find an audio stream in. Everything analyze produces is a display
    field, so the answer is None rather than an empty array the callers would
    then have to defend against."""
    # The decode refuses anything outside JOBS_DIR before it spawns ffmpeg.
    monkeypatch.setattr(an, "JOBS_DIR", tmp_path)
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")

    monkeypatch.setattr(
        an.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=b"", stderr=b""),
    )

    assert an._load_audio_ffmpeg(source) is None


# --------------------------------------------------------------------------
# beat_detect: the second half of the double-checked lock
# --------------------------------------------------------------------------


def test_a_thread_that_queued_for_the_model_lock_reuses_what_it_finds(monkeypatch):
    """Two jobs starting together both miss the unlocked check; the second one
    must not build the predictor again. Loading it twice costs a second model's
    worth of memory and, on GPU, a second allocation of the same weights."""
    built = object()

    class _LockThatLetsSomeoneElseGoFirst:
        """Stands in for the module lock, doing what the winning thread would
        have done by the time this one is admitted."""

        def __enter__(self):
            bd._model = built

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(bd, "_model", None)
    monkeypatch.setattr(bd, "_model_failed", False)
    monkeypatch.setattr(bd, "_lock", _LockThatLetsSomeoneElseGoFirst())

    assert bd._get_model() is built


# --------------------------------------------------------------------------
# beatgrid: refusals that produce no grid rather than a bad one
# --------------------------------------------------------------------------


def test_an_envelope_with_no_transient_above_the_floor_yields_no_onsets():
    """A constant envelope -- a sustained pad, or a stem that is all room tone.
    The adaptive delta sits above every sample, so peak_pick finds nothing and
    the editor gets no snap targets rather than an evenly spaced grid of
    non-events."""
    assert bg._fine_onsets(np.ones(400), 22050) == []


def test_downbeats_that_all_land_on_the_same_beat_describe_no_bars():
    """Fewer than three distinct downbeats is one span at most, and a single
    span cannot establish a modal bar length -- which is the whole point of the
    function."""
    beats = [i * 0.5 for i in range(8)]

    assert bg._downbeats_to_bars(beats, [0.0, 0.01, 0.02]) == []


def test_no_grid_when_the_detected_beats_have_no_spacing(stems_dir, stubbed_stage, caplog):
    """_sanitize normally collapses duplicates, so a zero coarse interval means
    it did not. The interval sizes the refinement search window and would
    divide by zero there instead."""
    stubbed_stage["sanitize"] = lambda beats, duration: [1.0] * 12

    assert bg.compute_beat_grid(stems_dir) is None


def test_no_grid_when_post_processing_collapsed_the_spacing(stems_dir, stubbed_stage):
    """Same guard on the other side of gap filling and the consistency pass,
    where the tempo the UI shows is computed."""
    calls = {"n": 0}
    real = stubbed_stage["real"]

    def _collapsing(beats, duration):
        # Detection is left alone; only the passes after it hand back a grid
        # whose beats all sit at the same instant.
        calls["n"] += 1
        return real(beats, duration) if calls["n"] == 1 else [1.0] * 12

    stubbed_stage["sanitize"] = _collapsing

    assert bg.compute_beat_grid(stems_dir) is None


def test_no_grid_when_extending_to_the_track_edges_left_too_little(
    stems_dir, stubbed_stage, monkeypatch
):
    """The extension only ever adds beats, so this is the guard that keeps a
    future change to it from shipping a two-beat grid."""
    monkeypatch.setattr(bg, "_extend_to_track_edges", lambda beats, duration, irr: ([0.5], 0, 0))

    assert bg.compute_beat_grid(stems_dir) is None


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------


def test_a_section_that_rounds_below_the_minimum_discards_the_whole_timeline():
    """Section times are rounded to milliseconds for the editor, and rounding
    is not length-preserving: a span half a second long to full precision can
    come out 499 ms once both ends are rounded independently. A section the UI
    cannot represent means the timeline is not the one that was analyzed, so
    none of it is offered."""
    # 500.0000000000001 ms to full precision -- long enough that _merge_short
    # keeps it -- and 626 ms to 1126 ms once each end is rounded.
    start, end = 0.6257, 1.1257000000000001
    raw = [
        {"label": "verse", "start": 0.0, "end": start},
        {"label": "chorus", "start": start, "end": end},
        {"label": "verse", "start": end, "end": 30.0},
    ]

    assert sx.normalize_sections(raw, 30.0) == []


def test_a_cancel_that_lands_as_the_worker_exits_is_still_a_cancellation(monkeypatch):
    """POST /cancel terminates the process, and the poll loop can see the exit
    before it sees the flag. Without the second check the stage would return a
    nonzero exit code and the job would be reported as failed rather than
    cancelled."""

    class _AlreadyGone:
        """A process that exited before the poll loop ran a single iteration."""

        returncode = 0
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(sx.subprocess, "Popen", lambda *a, **kw: _AlreadyGone())
    job = Job(id=JOB)
    job.cancel_requested = True

    with pytest.raises(JobCancelled):
        sx._run_registered_process(job, [sys.executable, "-c", "pass"])


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def test_a_url_the_parser_itself_chokes_on_is_a_clean_refusal(monkeypatch):
    """urlsplit raises on a handful of malformed authorities rather than
    returning an empty result. The caller's contract is InvalidYouTubeURL and a
    422, never a stack trace out of the import endpoint."""

    def _boom(_url):
        raise ValueError("Invalid IPv6 URL")

    monkeypatch.setattr(urllib.parse, "urlparse", _boom)

    with pytest.raises(dl.InvalidYouTubeURL, match="could not parse URL"):
        dl.validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_cancelling_stops_the_video_fetch_at_its_next_progress_callback(tmp_path, monkeypatch):
    """The video track is fetched after the audio, so a cancel pressed during
    the export-quality download has only the progress hook to act on. Raising
    from inside it is what makes yt-dlp abandon the transfer."""
    job_dir = tmp_path / JOB
    job_dir.mkdir()
    job = Job(id=JOB)
    job.cancel_requested = True

    class _CallsTheHook:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "total_bytes": 100, "downloaded_bytes": 10})
            return {}

    monkeypatch.setattr(dl, "YoutubeDL", _CallsTheHook)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)

    with pytest.raises(JobCancelled):
        dl._download_video_track(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", job_dir)


def test_the_video_progress_hook_reports_percentages_while_it_runs(tmp_path, monkeypatch):
    """The other side of the same hook: without it the stage sits on "Fetching
    video..." for the whole transfer."""
    job_dir = tmp_path / JOB
    job_dir.mkdir()
    job = Job(id=JOB)
    stages: list[str] = []

    class _CallsTheHook:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            for hook in self.opts["progress_hooks"]:
                hook({"status": "downloading", "total_bytes": 200, "downloaded_bytes": 50})
            return {}

    monkeypatch.setattr(dl, "YoutubeDL", _CallsTheHook)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)
    monkeypatch.setattr(dl, "_set", lambda j, **kw: stages.append(kw.get("stage", "")))

    dl._download_video_track(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", job_dir)

    assert "Fetching video 25%" in stages


# --------------------------------------------------------------------------
# section_refine: the track-edge rule, reached only when the spans do not
# start at zero
# --------------------------------------------------------------------------


_LABELS = ["intro", "verse", "chorus", "part", "start", "end"]


def _peak_at(frames, frame):
    boundary = np.full(frames, 0.01)
    boundary[frame - 1] = 0.2
    boundary[frame] = 0.9
    boundary[frame + 1] = 0.2
    labels = np.full((len(_LABELS), frames), 0.01)
    labels[1, :] = 0.9
    feat = np.ones((1, frames, 4))
    feat[0, frame:, :] *= -1
    return {"segment": boundary.tolist(), "label": labels.tolist()}, feat.tolist()


@pytest.mark.parametrize(
    "spans, peak_frame, why",
    [
        ([{"start": 40.0, "end": 120.0, "label": "verse"}], 30, "three seconds in"),
        ([{"start": 0.0, "end": 80.0, "label": "verse"}], 1180, "two seconds from the end"),
    ],
)
def test_a_candidate_at_the_very_edge_of_the_track_is_dropped(spans, peak_frame, why):
    """The rule is measured against the track, not against the neighbouring
    spans, because the model's own timeline can start late or stop early -- and
    a three-second opening section is not a section however far it sits from
    anything the model drew."""
    activations, embeddings = _peak_at(1200, peak_frame)

    out = sr.refine_segments(spans, activations, embeddings, 10.0, None, _LABELS)

    assert len(out) == 1, why
