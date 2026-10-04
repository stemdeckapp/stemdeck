"""Cancellation has to reach the processes it is meant to stop (#519).

python-fastapi.md: "Always register subprocess with set_proc(job_id, proc)
immediately after Popen(); deregister in finally." Several stages used
subprocess.run(), which cannot be interrupted -- the flag is set but nothing
looks at it until the call returns.
"""

from __future__ import annotations

import os

import pytest

from app.core import process as _process
from app.core.models import Job
from app.core.registry import register as registry_register
from app.pipeline import cancel as _cancel


def _job(**kw):
    return Job(id="a1b2c3d4e5f6", **kw)


def test_ffmpeg_is_registered_so_cancel_can_reach_it(tmp_path, monkeypatch):
    seen = {}

    def _capture(job_id, proc):
        # Registered while it runs, cleared after: both halves matter.
        seen.setdefault("during", proc if proc is not None else seen.get("during"))
        seen["last"] = proc

    monkeypatch.setattr(_cancel, "set_proc", _capture)

    job = _job()
    rc = _cancel.run_registered(job, ["sh", "-c", "exit 0"], 30).returncode

    assert rc == 0
    assert seen["during"] is not None, "cancel could not have reached this process"
    assert seen["last"] is None, "the registration must be cleared in a finally"


def test_a_failing_command_still_deregisters(tmp_path, monkeypatch):
    cleared = []
    monkeypatch.setattr(_cancel, "set_proc", lambda job_id, proc: cleared.append(proc))

    job = _job()
    result = _cancel.run_registered(job, ["sh", "-c", "echo boom >&2; exit 3"], 30)
    rc, stderr = result.returncode, result.stderr

    assert rc == 3
    assert b"boom" in stderr, "stderr must still be captured for the error message"
    assert cleared[-1] is None


# ─── parent-death watchdog ───


def test_the_watchdog_arms_when_the_parent_asks(monkeypatch):
    started = []
    monkeypatch.setenv("STEMDECK_PARENT_PID", "999999")
    monkeypatch.setattr(
        "threading.Thread",
        lambda *a, **kw: type("T", (), {"start": lambda self: started.append(True)})(),
    )

    _process.arm_parent_watchdog()

    assert started


@pytest.mark.parametrize("value", ["", "not-a-number", "0", "-1"])
def test_the_watchdog_stays_off_without_a_usable_parent_pid(monkeypatch, value):
    started = []
    monkeypatch.setenv("STEMDECK_PARENT_PID", value)
    monkeypatch.setattr(
        "threading.Thread",
        lambda *a, **kw: type("T", (), {"start": lambda self: started.append(True)})(),
    )

    _process.arm_parent_watchdog()

    assert not started


def test_the_watchdog_never_targets_our_own_pid(monkeypatch):
    # Would hard-exit the worker the moment it started.
    started = []
    monkeypatch.setenv("STEMDECK_PARENT_PID", str(os.getpid()))
    monkeypatch.setattr(
        "threading.Thread",
        lambda *a, **kw: type("T", (), {"start": lambda self: started.append(True)})(),
    )

    _process.arm_parent_watchdog()

    assert not started


def test_every_worker_spawn_exports_the_parent_pid():
    # demucs_worker had this; the other two did not, so a Force-Quit orphaned
    # an onnxruntime process holding the GPU.
    import pathlib

    for path in (
        "app/pipeline/separate.py",
        "app/pipeline/vocal_split.py",
        "app/pipeline/sections.py",
    ):
        src = pathlib.Path(path).read_text()
        assert "STEMDECK_PARENT_PID" in src, f"{path} spawns a worker without the watchdog"


def test_every_worker_arms_the_watchdog():
    import pathlib

    for path in (
        "app/pipeline/demucs_worker.py",
        "app/pipeline/vocal_split_worker.py",
        "app/pipeline/section_worker.py",
    ):
        src = pathlib.Path(path).read_text()
        assert "arm_parent_watchdog()" in src, f"{path} never arms the watchdog"


# ─── a running vocal split must be cancellable ───


class _LiveProc:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


def test_cancelling_a_running_vocal_split_terminates_it(monkeypatch):
    # cancel_job returns early for a done job -- and a vocal split only ever
    # runs on a done job, so it was uncancellable by construction while holding
    # _pipeline_lock and stalling the import queue.
    from app.api import jobs as _jobs_api

    job = _job(status="done", title="Song")
    job.vocal_split = "running"
    registry_register(job)

    proc = _LiveProc()
    monkeypatch.setattr(_jobs_api, "registry_get_proc", lambda job_id: proc)

    _jobs_api.cancel_job(job.id)

    assert job.cancel_requested is True
    assert proc.terminated, "the split ran to completion with the cancel button doing nothing"


def test_cancelling_a_plain_done_job_still_does_nothing(monkeypatch):
    from app.api import jobs as _jobs_api

    job = _job(status="done", title="Song")
    registry_register(job)

    proc = _LiveProc()
    monkeypatch.setattr(_jobs_api, "registry_get_proc", lambda job_id: proc)

    _jobs_api.cancel_job(job.id)

    assert not proc.terminated
    assert job.cancel_requested is False


# ─── the cancelled job's folder (#749) ───


async def test_a_locked_file_does_not_leave_a_cancelled_jobs_folder_behind(tmp_path, monkeypatch):
    """Right after a cancel terminates ffmpeg or the demucs worker, Windows can
    still hold one of its files. One attempt left the folder on disk for good,
    the orphan #749 reported; the removal is retried like delete_job's."""
    import shutil

    from app.core.models import JobCancelled
    from app.pipeline import collect as _collect
    from app.pipeline import runner as _runner

    job = _job(cancel_requested=True)
    registry_register(job)
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")

    real_rmtree = shutil.rmtree
    attempts = []

    def locked_once(path, *a, **k):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(32, "The process cannot access the file", str(path))
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(_collect.shutil, "rmtree", locked_once)

    def cancelled(_job, _job_dir):
        raise JobCancelled()

    await _runner._run_async(job, job_dir, tmp_path, cancelled)

    assert job.status == "cancelled"
    assert len(attempts) == 2, "the lock must be retried, not given up on"
    assert not job_dir.exists(), "the cancelled job's folder stayed on disk"


# ─── a job stopped from the Trash (#748) ───


async def test_a_run_stopped_from_the_trash_keeps_its_files(tmp_path):
    """Only emptying the Trash deletes files, so Restore and Extract have the
    source to work from."""
    from app.core.models import JobCancelled
    from app.pipeline import runner as _runner

    job = _job(cancel_requested=True, stop_requested=True)
    registry_register(job)
    job_dir = tmp_path / job.id
    job_dir.mkdir()
    (job_dir / "source.wav").write_bytes(b"RIFF")

    def cancelled(_job, _job_dir):
        raise JobCancelled()

    await _runner._run_async(job, job_dir, tmp_path, cancelled)

    assert job.status == "stopped"
    assert (job_dir / "source.wav").is_file()


async def test_a_run_that_finishes_despite_a_late_trash_clears_the_flags(tmp_path):
    """The trash arrived after the last cancel check, so the job finished. Left
    set, the flags made every later vocal split or section run on it cancel
    itself at once."""
    from app.pipeline import runner as _runner

    job = _job(cancel_requested=True, stop_requested=True)
    registry_register(job)
    job_dir = tmp_path / job.id
    job_dir.mkdir()

    await _runner._run_async(job, job_dir, tmp_path, lambda _job, _job_dir: None)

    assert job.status == "done"
    assert job.cancel_requested is False
    assert job.stop_requested is False
