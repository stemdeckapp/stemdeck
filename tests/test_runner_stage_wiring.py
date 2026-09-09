"""What _run_common wires onto the job after separation, and what it swallows.

test_pipeline_runner.py covers the error/quarantine paths; the post-separation
stage itself -- the lane list, the Original lane, the mix URL and the presence
map -- was only exercised through the error cases, so the wiring that decides
what the player renders was untested.

It is all bookkeeping, which is exactly why it is worth pinning: a lane list
missing "original", or a mix_url pointing at a file the mix step did not
produce, is a broken player rather than a failed job, so nothing raises.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.models import Job
from app.pipeline import runner


@pytest.fixture
def job():
    return Job(id="abcdefabcdef", selected_stems=["vocals", "drums"])


@pytest.fixture
def staged(tmp_path, monkeypatch, job):
    """A job dir past the separation stage, with every heavy step stubbed."""
    job_dir = tmp_path / job.id
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True)
    for name in ("vocals", "drums"):
        (stems_dir / f"{name}.wav").write_bytes(b"RIFF")

    monkeypatch.setattr(runner, "separate", lambda j, s, d: stems_dir)
    monkeypatch.setattr(runner, "collect", lambda j, s, d: ["vocals", "drums"])
    monkeypatch.setattr(runner, "cleanup_source", lambda d: None)
    monkeypatch.setattr(runner, "analyze", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "compute_beat_grid", lambda d: None)
    monkeypatch.setattr(runner, "compute_stem_peaks", lambda d, names: {})
    monkeypatch.setattr(runner, "make_original_track", lambda j, d, s: None)
    monkeypatch.setattr(runner, "make_selected_mix", lambda j, s, f: None)
    monkeypatch.setattr(runner, "detect_sections", lambda *a, **kw: None)
    return job_dir, stems_dir


def _run(job, job_dir):
    runner._run_common(job, job_dir / "source.wav", job_dir)


# --------------------------------------------------------------------------
# the lane list
# --------------------------------------------------------------------------


def test_every_produced_stem_becomes_a_lane(job, staged):
    job_dir, _ = staged

    _run(job, job_dir)

    assert [s["name"] for s in job.stems] == ["vocals", "drums"]
    for stem in job.stems:
        assert stem["url"] == f"/api/jobs/{job.id}/stems/{stem['name']}.wav"


def test_the_original_lane_is_inserted_first_when_one_was_built(job, staged, monkeypatch):
    """It is the backing track everything else plays against, so it leads the
    mixer rather than appearing between two stems."""
    job_dir, stems_dir = staged
    monkeypatch.setattr(runner, "make_original_track", lambda j, d, s: stems_dir / "original.wav")

    _run(job, job_dir)

    assert [s["name"] for s in job.stems][0] == "original"
    assert job.stems[0]["url"].endswith("/stems/original.wav")


def test_no_original_lane_when_the_mix_step_produced_none(job, staged):
    """The user kept every stem, so there is no complement to play against."""
    job_dir, _ = staged

    _run(job, job_dir)

    assert "original" not in [s["name"] for s in job.stems]


# --------------------------------------------------------------------------
# the mix URL
# --------------------------------------------------------------------------


def test_the_download_mix_button_points_at_what_was_actually_written(job, staged, monkeypatch):
    job_dir, stems_dir = staged
    monkeypatch.setattr(runner, "make_selected_mix", lambda j, s, f: stems_dir / "mix.wav")

    _run(job, job_dir)

    assert job.mix_url == f"/api/jobs/{job.id}/stems/mix.wav"


def test_a_single_stem_selection_points_the_button_at_that_stem(job, staged, monkeypatch):
    """make_selected_mix returns the existing stem rather than copying 30 MB,
    so the URL has to follow whatever name came back."""
    job_dir, stems_dir = staged
    monkeypatch.setattr(runner, "make_selected_mix", lambda j, s, f: stems_dir / "drums.wav")

    _run(job, job_dir)

    assert job.mix_url == f"/api/jobs/{job.id}/stems/drums.wav"


def test_no_mix_url_when_nothing_was_mixed(job, staged):
    job_dir, _ = staged

    _run(job, job_dir)

    assert job.mix_url is None


# --------------------------------------------------------------------------
# peaks and presence
# --------------------------------------------------------------------------


def test_the_mix_gets_peaks_but_is_kept_out_of_presence(job, staged, monkeypatch):
    """Peaks drive the waveform, which the mix lane needs; presence is a
    comparison between the separated stems, which the mix is not one of."""
    job_dir, stems_dir = staged
    seen = {}

    monkeypatch.setattr(runner, "make_selected_mix", lambda j, s, f: stems_dir / "mix.wav")
    monkeypatch.setattr(runner, "make_original_track", lambda j, d, s: stems_dir / "original.wav")

    def _peaks(d, names):
        seen["names"] = list(names)
        return {n: 0.5 for n in names}

    monkeypatch.setattr(runner, "compute_stem_peaks", _peaks)

    _run(job, job_dir)

    assert "mix" in seen["names"]
    assert "original" in seen["names"]
    assert set(job.stem_presence or {}) == {"vocals", "drums"}


def test_a_mix_that_is_an_existing_stem_is_not_listed_twice(job, staged, monkeypatch):
    job_dir, stems_dir = staged
    seen = {}
    monkeypatch.setattr(runner, "make_selected_mix", lambda j, s, f: stems_dir / "drums.wav")

    def _peaks(d, names):
        seen["names"] = list(names)
        return {}

    monkeypatch.setattr(runner, "compute_stem_peaks", _peaks)

    _run(job, job_dir)

    assert seen["names"].count("drums") == 1


# --------------------------------------------------------------------------
# the beat-grid stage swallows its own failures
# --------------------------------------------------------------------------


def test_a_failing_beat_grid_does_not_fail_the_job(job, staged, monkeypatch, caplog):
    """By this point the job is fully usable and a missing grid only costs the
    metronome. compute_beat_grid never raises today; the guard stays so a future
    change there cannot take the whole pipeline down with it."""
    job_dir, _ = staged

    def _boom(_stems_dir):
        raise RuntimeError("librosa exploded")

    monkeypatch.setattr(runner, "compute_beat_grid", _boom)

    _run(job, job_dir)  # must not raise

    assert "beat grid stage failed" in caplog.text
    assert job.stems, "the job lost its stems over a beat-grid failure"


def test_the_stage_timings_record_each_phase(job, staged):
    """Shown in the failure report and used to explain a slow import."""
    job_dir, _ = staged

    _run(job, job_dir)

    assert set(job.stage_timings or {}) >= {"post", "beatgrid"}


# --------------------------------------------------------------------------
# metadata.json
# --------------------------------------------------------------------------


def test_the_metadata_sidecar_records_what_recovery_needs(job, tmp_path):
    """restore() reads this to rebuild a job the registry lost."""
    job_dir = tmp_path / job.id
    job_dir.mkdir()
    job.title = "Get Lucky"
    job.bpm = 116
    job.compute_device = "cuda"

    runner._write_metadata(job, job_dir)

    meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Get Lucky"
    assert meta["bpm"] == 116
    assert meta["compute_device"] == "cuda"


def test_metadata_that_cannot_be_written_does_not_fail_the_job(job, tmp_path, monkeypatch, caplog):
    """The stems are on disk and the job succeeded; losing the sidecar costs a
    title after a registry loss, not the track."""
    job_dir = tmp_path / job.id
    job_dir.mkdir()

    def _boom(self, *a, **kw):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", _boom)

    runner._write_metadata(job, job_dir)  # must not raise

    assert "could not write metadata.json" in caplog.text


# --------------------------------------------------------------------------
# _rmtree
# --------------------------------------------------------------------------


def test_removing_something_already_gone_is_silent(tmp_path, caplog):
    runner._rmtree(tmp_path / "never-existed")

    assert "failed to remove" not in caplog.text


def test_a_removal_that_fails_is_logged_rather_than_raised(tmp_path, monkeypatch, caplog):
    """This runs on the failure path, where raising would replace the real
    error with a cleanup one."""
    import shutil

    target = tmp_path / "d"
    target.mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **kw: (_ for _ in ()).throw(OSError("busy")))

    runner._rmtree(target)  # must not raise

    assert "failed to remove" in caplog.text
