"""The collect stage: moving stems into place and building the mixdowns.

test_pipeline_collect.py covers the peaks/RMS half of this module. The other
half -- the part that moves files out of the demucs output dir, deletes the
source download, and drives ffmpeg to build original.wav and mix.wav -- had no
coverage at all, despite being where a job's files are actually created and
destroyed.

The ffmpeg mixes are exercised for real (CI installs ffmpeg, as the existing
click/export tests already assume) because the thing worth pinning is the
filter graph itself: original.wav is the sum of the stems the user did *not*
pick, and getting that wrong silently doubles every selected stem in playback.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core.config import DEMUCS_MODEL, STEM_NAMES
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.collect import (
    _rmtree,
    _run_ffmpeg,
    cleanup_source,
    collect,
    make_original_track,
    make_selected_mix,
    presence_for_split,
    sweep_failed_jobs,
    sweep_old_jobs,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


def _tone(path: Path, amplitude: float = 0.25, seconds: float = 0.25, rate: int = 44100) -> None:
    """A short constant-amplitude 440 Hz tone, 16-bit stereo -- the shape demucs
    emits, so ffmpeg treats these exactly as it treats real stems."""
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    wave_data = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.column_stack([wave_data, wave_data]), rate, subtype="PCM_16")


def _peak(path: Path) -> float:
    data, _ = sf.read(str(path))
    return float(np.max(np.abs(data)))


def _job(**kw) -> Job:
    job = Job(id=kw.pop("id", "abcdefabcdef"))
    for k, v in kw.items():
        setattr(job, k, v)
    _jobs[job.id] = job
    return job


# --------------------------------------------------------------------------
# collect() -- moving demucs output into the job's stems/ dir
# --------------------------------------------------------------------------


def test_collect_moves_every_stem_into_the_jobs_stems_dir(tmp_path):
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    for name in STEM_NAMES:
        _tone(stems_root / f"{name}.wav")

    found = collect(_job(), stems_root, job_dir)

    assert sorted(found) == sorted(STEM_NAMES)
    assert sorted(p.name for p in (job_dir / "stems").iterdir()) == sorted(
        f"{n}.wav" for n in STEM_NAMES
    )


def test_collect_removes_the_demucs_intermediate_dir(tmp_path):
    """Those files are a full duplicate of the stems that were just moved --
    leaving them behind doubles the disk every job costs."""
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    _tone(stems_root / "vocals.wav")

    collect(_job(), stems_root, job_dir)

    assert not (job_dir / DEMUCS_MODEL).exists()


def test_collect_reports_only_the_stems_that_were_actually_produced(tmp_path):
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    _tone(stems_root / "vocals.wav")
    _tone(stems_root / "drums.wav")

    found = collect(_job(), stems_root, job_dir)

    assert found == ["vocals", "drums"]  # STEM_NAMES order, not directory order


def test_collect_ignores_files_that_are_not_known_stems(tmp_path):
    """demucs writes only its own sources, but the job dir is a place other
    stages also write; only the six names are stems."""
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    _tone(stems_root / "vocals.wav")
    (stems_root / "notes.txt").write_text("x")

    collect(_job(), stems_root, job_dir)

    assert [p.name for p in (job_dir / "stems").iterdir()] == ["vocals.wav"]


def test_collect_refuses_a_separation_that_produced_nothing(tmp_path):
    """An empty output dir means demucs failed in a way that did not raise.
    Letting that through would mark the job done with no audio in it."""
    job_dir = tmp_path / "job"
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="no stems produced"):
        collect(_job(), stems_root, job_dir)


def test_collect_works_when_the_stems_dir_already_exists(tmp_path):
    """A retried job arrives with the directory already there."""
    job_dir = tmp_path / "job"
    (job_dir / "stems").mkdir(parents=True)
    stems_root = job_dir / DEMUCS_MODEL / "source"
    stems_root.mkdir(parents=True)
    _tone(stems_root / "bass.wav")

    assert collect(_job(), stems_root, job_dir) == ["bass"]


# --------------------------------------------------------------------------
# cleanup_source() -- the bulk of the disk reclaim per job
# --------------------------------------------------------------------------


def test_cleanup_source_deletes_the_download_whatever_its_extension(tmp_path):
    (tmp_path / "source.webm").write_bytes(b"x")
    (tmp_path / "source.m4a").write_bytes(b"x")

    cleanup_source(tmp_path)

    assert not list(tmp_path.glob("source.*"))


def test_cleanup_source_leaves_the_stems_and_the_video_alone(tmp_path):
    """It runs after post-processing, and everything else in the job dir is the
    result the user came for."""
    (tmp_path / "source.webm").write_bytes(b"x")
    (tmp_path / "video.mp4").write_bytes(b"x")
    stems = tmp_path / "stems"
    stems.mkdir()
    (stems / "vocals.wav").write_bytes(b"x")

    cleanup_source(tmp_path)

    assert (tmp_path / "video.mp4").exists()
    assert (stems / "vocals.wav").exists()


def test_cleanup_source_is_a_no_op_when_there_is_nothing_to_delete(tmp_path):
    cleanup_source(tmp_path)  # must not raise


# --------------------------------------------------------------------------
# make_original_track() -- the complement mix
# --------------------------------------------------------------------------


def test_the_original_track_is_the_sum_of_the_stems_the_user_did_not_pick(tmp_path):
    """This is the whole reason original.wav is not just the source download:
    playing (source + isolated drums) would give drums at double amplitude."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav", amplitude=0.1)
    job = _job(selected_stems=["vocals", "drums"])

    out = make_original_track(job, tmp_path, stems)

    assert out is not None and out.name == "original.wav" and out.is_file()
    # Four unselected stems at 0.1 summed with normalize=0 -> about 0.4, not the
    # ~0.1 that a normalising amix would produce.
    assert _peak(out) == pytest.approx(0.4, abs=0.05)


def test_a_single_unselected_stem_is_copied_rather_than_mixed(tmp_path):
    """amix on one input is a no-op, but the job still needs the canonical
    original.wav to exist."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav", amplitude=0.2)
    job = _job(selected_stems=[n for n in STEM_NAMES if n != "other"])

    out = make_original_track(job, tmp_path, stems)

    assert out is not None and out.is_file()
    assert _peak(out) == pytest.approx(0.2, abs=0.02)


def test_no_original_track_when_the_user_kept_everything(tmp_path):
    """There is no complement to mix, and an empty amix would fail."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav")
    job = _job(selected_stems=list(STEM_NAMES))

    assert make_original_track(job, tmp_path, stems) is None


def test_no_original_track_when_the_unselected_stems_are_not_on_disk(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    _tone(stems / "vocals.wav")
    job = _job(selected_stems=["vocals"])

    assert make_original_track(job, tmp_path, stems) is None


def test_the_original_track_is_written_as_16_bit_pcm(tmp_path):
    """The browser decoder and the ffmpeg exports are both built for it."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav")
    job = _job(selected_stems=["vocals"])

    out = make_original_track(job, tmp_path, stems)

    with wave.open(str(out)) as handle:
        assert handle.getsampwidth() == 2


def test_a_failing_ffmpeg_yields_no_original_track(tmp_path, monkeypatch):
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav")
    monkeypatch.setattr("app.pipeline.collect._run_ffmpeg", lambda job, cmd: False)
    job = _job(selected_stems=["vocals"])

    assert make_original_track(job, tmp_path, stems) is None


# --------------------------------------------------------------------------
# make_selected_mix() -- the "Download Mix" track
# --------------------------------------------------------------------------


def test_the_selected_mix_sums_exactly_the_chosen_stems(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav", amplitude=0.15)
    job = _job(selected_stems=["vocals", "drums", "bass"])

    out = make_selected_mix(job, stems, list(STEM_NAMES))

    assert out is not None and out.name == "mix.wav"
    assert _peak(out) == pytest.approx(0.45, abs=0.05)


def test_a_one_stem_selection_points_at_the_stem_instead_of_copying_it(tmp_path):
    """Copying would spend 30 MB duplicating a file that already exists."""
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav")
    job = _job(selected_stems=["drums"])

    out = make_selected_mix(job, stems, list(STEM_NAMES))

    assert out == stems / "drums.wav"
    assert not (stems / "mix.wav").exists()


def test_no_mix_when_nothing_was_selected(tmp_path):
    stems = tmp_path / "stems"
    stems.mkdir()
    job = _job(selected_stems=[])

    assert make_selected_mix(job, stems, list(STEM_NAMES)) is None


def test_a_selection_demucs_never_produced_is_ignored(tmp_path):
    """found is what actually landed on disk; mixing a name that is not there
    would make ffmpeg fail on a missing input."""
    stems = tmp_path / "stems"
    stems.mkdir()
    _tone(stems / "vocals.wav")
    job = _job(selected_stems=["guitar", "piano"])

    assert make_selected_mix(job, stems, ["vocals"]) is None


def test_a_failing_ffmpeg_yields_no_mix(tmp_path, monkeypatch):
    stems = tmp_path / "stems"
    stems.mkdir()
    for name in STEM_NAMES:
        _tone(stems / f"{name}.wav")
    monkeypatch.setattr("app.pipeline.collect._run_ffmpeg", lambda job, cmd: False)
    job = _job(selected_stems=["vocals", "drums"])

    assert make_selected_mix(job, stems, list(STEM_NAMES)) is None


# --------------------------------------------------------------------------
# _run_ffmpeg() -- cancellation and failure reporting
# --------------------------------------------------------------------------


def test_a_running_ffmpeg_is_registered_so_cancel_can_reach_it(tmp_path, monkeypatch):
    """Without set_proc, cancelling during an amix would do nothing until the
    300 s timeout expired: the flag is set, but the runner is blocked inside
    subprocess.run and cannot see it."""
    import app.pipeline.collect as collect_mod

    job = _job()
    seen: list = []
    real_set_proc = collect_mod.set_proc

    def _spy(job_id, proc):
        seen.append((job_id, proc))
        return real_set_proc(job_id, proc)

    monkeypatch.setattr(collect_mod, "set_proc", _spy)

    assert _run_ffmpeg(job, ["ffmpeg", "-version"]) is True

    assert [s[0] for s in seen] == [job.id, job.id]
    assert seen[0][1] is not None, "the process was never registered"
    assert seen[1][1] is None, "the registration was never cleared"


def test_a_nonzero_ffmpeg_exit_is_reported_as_failure(tmp_path):
    assert _run_ffmpeg(_job(), ["ffmpeg", "-i", str(tmp_path / "nope.wav"), "-f", "null", "-"]) is (
        False
    )


def test_the_registration_is_cleared_even_when_ffmpeg_fails(tmp_path):
    """A stale proc entry would let a later cancel signal an unrelated pid."""
    from app.core.registry import _procs

    job = _job()
    _run_ffmpeg(job, ["ffmpeg", "-i", str(tmp_path / "nope.wav"), "-f", "null", "-"])

    assert _procs.get(job.id) is None


def test_an_ffmpeg_that_overruns_its_timeout_is_killed(monkeypatch):
    """The timeout is the only bound on a hung encoder, and leaving the process
    behind would hold the job's files open forever."""
    import app.pipeline.collect as collect_mod

    monkeypatch.setattr(collect_mod, "TIMEOUT_FFMPEG", 0.2)
    job = _job()

    # A process that outlives the timeout without producing output.
    result = _run_ffmpeg(job, ["ffmpeg", "-f", "lavfi", "-i", "sine=d=3600", "-f", "null", "-"])

    assert result is False


def test_a_missing_ffmpeg_binary_raises_rather_than_reporting_success(tmp_path):
    """Popen raises FileNotFoundError here. It must not be mistaken for a clean
    run -- the caller writes a URL for a file that would not exist."""
    with pytest.raises(FileNotFoundError):
        _run_ffmpeg(_job(), ["definitely-not-ffmpeg-xyz"])


# --------------------------------------------------------------------------
# smaller uncovered branches
# --------------------------------------------------------------------------


def test_rmtree_swallows_a_failure_it_cannot_do_anything_about(tmp_path, monkeypatch, caplog):
    """Sweeping is best-effort: a locked directory must not take down the hourly
    loop and stop every later job from being cleaned up."""

    def _boom(path):
        raise PermissionError("in use")

    monkeypatch.setattr("app.pipeline.collect.shutil.rmtree", _boom)
    target = tmp_path / "d"
    target.mkdir()

    _rmtree(target)  # no exception

    assert "failed to remove" in caplog.text


def test_rmtree_is_quiet_about_something_already_gone(tmp_path, caplog):
    _rmtree(tmp_path / "never-existed")

    assert "failed to remove" not in caplog.text


def test_split_presence_is_empty_when_the_reference_is_effectively_silent(tmp_path):
    """Dividing by a near-zero loudest would produce presence numbers derived
    from rounding noise."""
    out = presence_for_split(
        {"vocals": 1e-12, "lead_vocals": 1e-12},
        {"vocals": 100},
    )

    assert out == {}


def test_the_ttl_sweep_does_nothing_without_a_jobs_dir(tmp_path):
    sweep_old_jobs(tmp_path / "not-there")  # must not raise


def test_the_ttl_sweep_ignores_loose_files_next_to_the_job_dirs(tmp_path):
    """registry.json and user-data.json live at this level (#403)."""
    (tmp_path / "registry.json").write_text("{}")

    sweep_old_jobs(tmp_path, ttl_seconds=0)

    assert (tmp_path / "registry.json").exists()


def test_the_failed_sweep_survives_an_entry_it_cannot_stat(tmp_path, monkeypatch, caplog):
    failed = tmp_path / "failed"
    failed.mkdir()
    doomed = failed / "abcdefabcdef"
    doomed.mkdir()
    (failed / "fedcbafedcba").mkdir()

    real_stat = Path.stat

    def _stat(self, **kw):
        # Only the one entry is unreadable -- a directory that vanished between
        # iterdir() and stat(), or one on a disconnected volume.
        if self == doomed:
            raise OSError("gone")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", _stat)

    sweep_failed_jobs(tmp_path)  # must not raise

    monkeypatch.undo()  # .exists() below would otherwise hit the same stub
    assert "could not stat" in caplog.text
    # And the sweep carried on rather than stopping at the entry it could
    # not read: the fresh one is still there, un-expired.
    assert (failed / "fedcbafedcba").exists()


def test_the_failed_sweep_does_nothing_without_a_quarantine(tmp_path):
    sweep_failed_jobs(tmp_path)  # must not raise


def test_subprocess_output_is_not_inherited_from_the_server(tmp_path):
    """ffmpeg's stdout is discarded and stderr is captured; letting either flow
    to the server's own streams would interleave with the log."""
    job = _job()
    proc_args = {}

    real_popen = subprocess.Popen

    def _spy(cmd, **kw):
        proc_args.update(kw)
        return real_popen(cmd, **kw)

    import app.pipeline.collect as collect_mod

    orig = collect_mod.subprocess.Popen
    collect_mod.subprocess.Popen = _spy
    try:
        _run_ffmpeg(job, ["ffmpeg", "-version"])
    finally:
        collect_mod.subprocess.Popen = orig

    assert proc_args["stdout"] == subprocess.DEVNULL
    assert proc_args["stderr"] == subprocess.PIPE
