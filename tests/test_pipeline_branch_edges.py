"""The unexercised arm of a branch, across the pipeline stages.

Statement coverage cannot see these: every line below already runs. What was
never exercised is the *other* answer to a question the code asks -- the loop
that finds nothing, the optional that is absent, the guard whose condition
holds. A regression in one of them is silent by construction, which is exactly
what makes them worth pinning.
"""

from __future__ import annotations

import json
import threading

import numpy as np
import pytest
import soundfile as sf

from app.core.models import Job
from app.pipeline import beatgrid as bg
from app.pipeline import click_render as cr
from app.pipeline import collect as co
from app.pipeline import download as dl
from app.pipeline import jobqueue as jq
from app.pipeline import runner as rn
from app.pipeline import sections as sx
from app.pipeline import vocal_split as vs

JOB = "abcdefabcdef"


# --------------------------------------------------------------------------
# collect: a stem the peak scan produced nothing for
# --------------------------------------------------------------------------


def test_a_stem_with_no_peaks_still_contributes_its_level(tmp_path, monkeypatch):
    """scan_stem returns peaks *and* an RMS. A stem too short to fill a single
    bucket yields no peak data but still has a level, and the level is what the
    presence percentages on the new split cards are computed from -- dropping
    the whole stem because it had no waveform would leave those cards blank."""
    stems = tmp_path / "stems"
    stems.mkdir()
    sf.write(str(stems / "lead_vocals.wav"), np.zeros(400, dtype=np.float32), 8000)

    monkeypatch.setattr(co, "scan_stem", lambda path, buckets: ([], 0.42))

    rms = co.merge_stem_peaks(stems, ["lead_vocals"])

    assert rms == {"lead_vocals": 0.42}
    assert json.loads((stems / "peaks.json").read_text(encoding="utf-8")) == {}


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def test_a_progress_report_that_is_neither_a_percentage_nor_a_finish_is_ignored(
    tmp_path, monkeypatch
):
    """yt-dlp reports "error" and "processing" statuses through the same hook.
    Treating an unrecognised one as a finish would jump the bar to 100% while
    the transfer is still running."""
    job_dir = tmp_path / JOB
    job_dir.mkdir()
    (job_dir / "source.webm").write_bytes(b"\0" * 32)
    job = Job(id=JOB)
    stages: list[str | None] = []

    class _ReportsAnError:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            if download:
                for hook in self.opts["progress_hooks"]:
                    hook({"status": "error"})
            return {"title": "t", "duration": 10}

    monkeypatch.setattr(dl, "YoutubeDL", _ReportsAnError)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "get_video_max_height", lambda: 720)
    monkeypatch.setattr(dl, "_download_video_track", lambda *a, **kw: None)
    monkeypatch.setattr(dl, "_set", lambda j, **kw: stages.append(kw.get("stage")))

    dl.download(job, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", job_dir)

    assert not any(s == "Download complete" for s in stages), (
        "an unrecognised status was reported as a finished download"
    )


def test_a_soundcloud_job_never_looks_for_a_video_track(tmp_path, monkeypatch):
    """SoundCloud is audio-only. Asking yt-dlp for a video stream costs a
    failed extraction per import and marks the job video_status=failed, which
    the library then shows as a broken export."""
    job_dir = tmp_path / JOB
    job_dir.mkdir()
    (job_dir / "source.mp3").write_bytes(b"\0" * 32)
    job = Job(id=JOB)
    tried: list[str] = []

    class _Ok:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            return {"title": "A Set", "duration": 600}

    monkeypatch.setattr(dl, "YoutubeDL", _Ok)
    monkeypatch.setattr(dl, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(dl, "get_cookies_file", lambda: None)
    monkeypatch.setattr(dl, "_download_video_track", lambda *a, **kw: tried.append("video"))

    out = dl.download(job, "https://soundcloud.com/artist/a-set", job_dir)

    assert out.name == "source.mp3"
    assert tried == [], "a SoundCloud import went looking for a video stream"
    assert job.video_status is None


# --------------------------------------------------------------------------
# jobqueue: the uploaded file
# --------------------------------------------------------------------------


def test_a_directory_named_like_the_upload_is_not_mistaken_for_it(tmp_path):
    """glob("source.*") matches directories too. Handing one to ffmpeg fails
    the job with a decode error instead of using the file sitting next to it."""
    (tmp_path / "source.d").mkdir()
    (tmp_path / "source.wav").write_bytes(b"RIFF")

    assert jq._find_local_source(tmp_path).name == "source.wav"


def test_a_job_directory_with_no_upload_in_it_has_no_local_source(tmp_path):
    assert jq._find_local_source(tmp_path) is None


# --------------------------------------------------------------------------
# sections: the orphan sweep
# --------------------------------------------------------------------------


def test_a_workspace_that_could_not_be_removed_is_not_counted(tmp_path, monkeypatch, caplog):
    """The count goes into a log line the user may be reading to explain a
    disk-space complaint. Counting a directory that is still there would make
    that line a lie."""
    jobs = tmp_path / "jobs"
    stems = jobs / JOB / "stems"
    stems.mkdir(parents=True)
    (stems / f"{sx._WORK_PREFIX}abc").mkdir()

    monkeypatch.setattr(sx, "_safe_rmtree", lambda path, parent: None)  # refuses silently

    assert sx.sweep_orphaned_workspaces(jobs) == 0
    assert "removed" not in caplog.text


def test_a_workspace_that_was_removed_is_counted(tmp_path):
    """So the refusal above is about the failure and not about the sweep never
    finding anything."""
    jobs = tmp_path / "jobs"
    stems = jobs / JOB / "stems"
    stems.mkdir(parents=True)
    (stems / f"{sx._WORK_PREFIX}abc").mkdir()

    assert sx.sweep_orphaned_workspaces(jobs) == 1


# --------------------------------------------------------------------------
# click_render
# --------------------------------------------------------------------------


def test_doubling_an_empty_grid_produces_an_empty_grid():
    """The half-beat pass appends the final beat after interpolating between
    pairs; with no beats there is no final beat to append, and an IndexError
    here would fail the export rather than render a click-free file."""
    assert cr.rescale_beats([], 2.0) == []


def test_a_count_in_from_before_the_first_beat_uses_the_opening_meter():
    """Exporting a region that starts in the pre-roll silence -- common for a
    track with a long intro -- leaves the beat scan with nothing at or before
    `start`, so it must fall back to the first bar rather than the last."""
    beats = [2.0 + i * 0.5 for i in range(16)]
    bars = [{"beat": 0, "meter": 4}]

    lead_in, clicks = cr.count_in_beats(beats, bars, count_bars=1, start=0.0)

    assert lead_in > 0
    assert len(clicks) == 4


def test_a_count_in_export_leaves_out_the_clicks_outside_its_region(tmp_path):
    """A loop export carries the count-in plus only the region's own clicks.
    Including the whole song's would put audible clicks under silence that the
    region does not contain."""
    beats = [i * 0.5 for i in range(40)]
    bars = [{"beat": i * 4, "meter": 4} for i in range(10)]

    result = cr.render_count_in_wav(
        tmp_path / "count.wav",
        beats,
        bars,
        duration=20.0,
        start=5.0,
        end=7.0,
    )

    assert result is not None
    path, lead_in = result
    rendered, rate = sf.read(str(path))
    assert len(rendered) / rate == pytest.approx(lead_in + 2.0, abs=0.05)


# --------------------------------------------------------------------------
# beatgrid
# --------------------------------------------------------------------------


def test_a_transient_at_the_very_edge_of_the_envelope_is_not_interpolated():
    """Parabolic interpolation needs a sample either side of the peak. At frame
    zero or the last frame there is none, and reading past the array would be
    an IndexError on a track whose first hit is at time zero."""
    env = np.zeros(64)
    env[0] = 1.0  # the loudest frame is the first one
    beats = [0.0]

    out, _refined = bg._refine_beats(None, 22050, beats, 0.5, env=env)

    assert out == [pytest.approx(0.0, abs=0.02)]


def test_a_peak_on_a_straight_slope_is_not_interpolated():
    """Parabolic interpolation fits a curve through the peak and its
    neighbours; on a straight line the fit has no curvature and its
    denominator is exactly zero. A rising onset envelope with no local maximum
    inside the search window -- a long crescendo, a swell into a downbeat --
    is that line, and dividing would be a ZeroDivisionError inside a stage
    that is supposed to degrade rather than fail."""
    env = np.linspace(0.0, 1.0, 400)
    beat = 300 * bg.BEATGRID_REFINE_HOP / 22050

    out, refined = bg._refine_beats(None, 22050, [beat], 0.5, env=env)

    assert out == [beat], "the beat moved despite nothing being played at it"
    assert refined == 0


def test_a_gap_next_to_beats_with_no_spacing_is_left_alone():
    """The gap filler divides the hole by the local period. A neighbourhood
    whose median interval is zero would divide by zero rather than fill."""
    beats = [1.0, 1.0, 1.0, 1.0, 9.0, 9.0, 9.0, 9.0]

    out, inserted = bg._fill_interior_gaps(beats)

    assert inserted == 0
    assert out == beats


def test_irregular_material_skips_gap_filling_and_the_consistency_pass(
    stems_with_drums, monkeypatch, caplog
):
    """Both passes assume a locally regular pulse. On rubato or live material
    they fight the detector: the consistency pass drags outliers toward a
    median that does not describe the music, and the gap filler invents beats
    where a fermata is."""
    called: list[str] = []
    monkeypatch.setattr(
        bg, "_fill_interior_gaps", lambda beats: called.append("fill") or (beats, 0)
    )
    monkeypatch.setattr(
        bg, "_enforce_grid_consistency", lambda beats: called.append("fix") or (beats, 0)
    )
    # Wildly uneven spacing: well past BEATGRID_MAX_IRREGULARITY.
    wandering = [0.0]
    for step in [0.4, 0.9, 0.35, 1.1, 0.5, 0.95, 0.4, 1.2, 0.45, 0.85, 0.4, 1.0, 0.5, 0.9]:
        wandering.append(round(wandering[-1] + step, 4))
    monkeypatch.setattr(bg, "detect_beats", lambda y, sr, env: (wandering, [], "stub"))

    grid = bg.compute_beat_grid(stems_with_drums)

    assert called == [], "the regularity guard let both passes run on rubato"
    assert grid is None or grid["beats"], "the stage neither refused nor produced a grid"


@pytest.fixture
def stems_with_drums(tmp_path, monkeypatch):
    d = tmp_path / "stems"
    d.mkdir()
    t = np.linspace(0, 2.0, 16000, endpoint=False)
    sf.write(str(d / "drums.wav"), (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), 8000)

    rng = np.random.default_rng(0)
    y = np.zeros(int(22050 * 15.0), dtype=np.float32)
    burst = int(22050 * 0.01)
    for k in range(30):
        start = int(k * 0.5 * 22050)
        y[start : start + burst] = rng.uniform(-1.0, 1.0, size=burst).astype(np.float32)
    monkeypatch.setattr(
        bg, "_load_audio_ffmpeg", lambda src, sr=22050, duration=None, timeout=None: (y, 22050)
    )
    return d


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


def test_a_structure_pass_that_found_nothing_leaves_the_timeline_empty(tmp_path, monkeypatch):
    """normalize_sections returns [] for output it does not trust. Recording
    that as an automatic result would put an empty section list in the editor
    and mark it as the model's answer, which the UI then will not offer to
    re-run."""
    job_dir = tmp_path / JOB
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True)

    monkeypatch.setattr(rn, "analyze", lambda *a, **kw: None)
    monkeypatch.setattr(rn, "separate", lambda j, s, d: stems_dir)
    monkeypatch.setattr(rn, "collect", lambda j, s, d: ["vocals"])
    monkeypatch.setattr(rn, "cleanup_source", lambda d: None)
    monkeypatch.setattr(rn, "make_original_track", lambda j, d, s: None)
    monkeypatch.setattr(rn, "make_selected_mix", lambda j, s, f: None)
    monkeypatch.setattr(rn, "compute_stem_peaks", lambda d, names: {})
    monkeypatch.setattr(rn, "compute_beat_grid", lambda d: None)
    monkeypatch.setattr(rn, "detect_sections", lambda j, d, dur: [])

    job = Job(id=JOB, selected_stems=["vocals"])
    job.auto_sections = True
    job.duration_sec = 120.0

    rn._run_common(job, job_dir / "source.wav", job_dir)

    assert job.sections is None
    assert job.sections_source is None


# --------------------------------------------------------------------------
# vocal_split: the watchdog's ordinary tick
# --------------------------------------------------------------------------


def test_the_vocal_split_watchdog_goes_back_to_sleep_while_output_is_flowing(tmp_path, monkeypatch):
    """Two of the three ways out of the watchdog loop are the interesting ones.
    The third -- still running, still talking -- is what has to happen on every
    tick of a healthy forty-minute split."""
    import sys

    stems = tmp_path / "stems"
    stems.mkdir()
    sf.write(str(stems / "vocals.wav"), np.zeros(800, dtype=np.float32), 8000)

    real_wait = threading.Event.wait
    seen: dict[int, int] = {}
    ticks = {"n": 0}

    def _wait(self, timeout=None):
        if timeout is None:
            return real_wait(self, timeout)
        n = seen[id(self)] = seen.get(id(self), 0) + 1
        if n <= 2:
            ticks["n"] += 1
            return False  # expired: the watchdog inspects a live, chatty worker
        return True

    monkeypatch.setattr(threading.Event, "wait", _wait)
    monkeypatch.setattr(vs, "get_demucs_device", lambda: "cpu")
    # Prints one line, then waits for stdin that never comes: alive and not stalled.
    monkeypatch.setattr(
        vs,
        "_spawn_cmd",
        lambda *a: [
            sys.executable,
            "-c",
            "import sys,time; sys.stderr.write('working\\n'); sys.stderr.flush(); time.sleep(1)",
        ],
    )

    from app.pipeline.errors import SeparationError

    with pytest.raises(SeparationError):
        vs.split_vocals(Job(id=JOB), stems)

    assert ticks["n"] >= 1, "the watchdog never woke while the worker was healthy"


def test_a_count_in_from_past_the_last_beat_uses_the_closing_meter():
    """Exporting a region that begins in the outro -- after the drums stop, so
    past the last detected beat -- leaves the beat scan with no beat to break
    on. The meter it counts in has to be the one the track ended in."""
    beats = [i * 0.5 for i in range(16)]
    bars = [{"beat": 0, "meter": 4}]

    lead_in, clicks = cr.count_in_beats(beats, bars, count_bars=1, start=60.0)

    assert lead_in > 0
    assert len(clicks) == 4


def test_a_failure_with_no_traceback_text_gets_no_traceback_heading(tmp_path, monkeypatch):
    """The heading is a section marker the failure endpoint parses. Writing one
    with nothing under it would show the user an empty "traceback" panel and
    make the report look truncated."""
    monkeypatch.setattr(rn.traceback, "format_exception", lambda *a, **kw: [])

    job_dir = tmp_path / JOB
    job_dir.mkdir()
    jobs_dir = tmp_path

    rn._quarantine_failed_job(Job(id=JOB), job_dir, jobs_dir, RuntimeError("demucs died"))

    report = (jobs_dir / "failed" / JOB / "error.txt").read_text(encoding="utf-8")
    assert "--- traceback ---" not in report
    assert "demucs died" in report
