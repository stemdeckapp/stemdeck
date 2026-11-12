"""A song trashed or deleted while it is still importing is stopped and, once
deleted, leaves nothing behind (#748).

Two holes the queue worker could fall into. A trash that lands before the
stage's process is registered (the Demucs worker is started after a device
probe) was only seen when the whole separation had finished, so the song stayed
in the Queue as "Separating N%". And emptying the Trash while that stop was
still settling was refused, after the page had already let the song go, so its
folder and its record stayed on disk with nothing showing them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs, register, set_proc
from app.main import app
from app.pipeline import jobqueue, runner


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
async def worker():
    task = jobqueue.start_worker()
    yield task
    jobqueue.request_stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def _queued_upload(job_id: str):
    job = Job(id=job_id, status="queued", title="Importing", source_url="local:Importing")
    register(job)
    jobqueue.enqueue(job.id, autostart=False)
    job_dir = jobqueue.JOBS_DIR / job.id
    job_dir.mkdir(parents=True)
    (job_dir / "source.wav").write_bytes(b"RIFF")
    return job, job_dir


class _Stage:
    """A pipeline stage that registers a real child process once `go` is set,
    the way separate() starts Demucs after its device probe, and ends with
    JobCancelled when the job was cancelled. `settle` holds it after the child
    has exited, so a request can land while the stop is settling."""

    def __init__(self) -> None:
        self.go = threading.Event()
        self.settle = threading.Event()
        self.settle.set()
        self.running = threading.Event()
        self.procs: list[subprocess.Popen] = []

    def __call__(self, job, source_path, job_dir):
        self.running.set()
        self.go.wait(30)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.procs.append(proc)
        set_proc(job.id, proc)
        try:
            proc.wait(timeout=30)
        finally:
            set_proc(job.id, None)
        self.settle.wait(30)
        if job.cancel_requested:
            raise JobCancelled()

    def kill(self) -> None:
        self.go.set()
        self.settle.set()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.fixture
def stage(monkeypatch):
    s = _Stage()
    monkeypatch.setattr(runner, "_run_local_blocking", s)
    yield s
    s.kill()


def _recorded_ids(jobs_dir) -> set[str]:
    data = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    return {record["id"] for record in data["jobs"]}


async def test_a_trash_before_the_process_registers_still_stops_it(
    worker, stage, client: TestClient
) -> None:
    job, job_dir = _queued_upload("7b7b7b7b7b01")
    jobqueue.resume()
    await _until(stage.running.is_set)

    # Nothing is registered yet: the stage is still in its device probe.
    assert (await asyncio.to_thread(client.post, f"/api/jobs/{job.id}/trash")).status_code == 200
    stage.go.set()

    await _until(lambda: job.status == "stopped", timeout=5)
    # The running slot frees once the registry save after the status is done.
    await _until(lambda: jobqueue.snapshot()[0] is None, timeout=5)
    assert stage.procs[0].poll() is not None, "the process ran on in the Trash"
    assert jobqueue.snapshot() == (None, []), "the Queue still shows the trashed song"
    assert (job_dir / "source.wav").is_file(), "a trash keeps the files"


async def test_emptying_the_trash_while_the_stop_settles_leaves_nothing(
    worker, stage, client: TestClient
) -> None:
    job, job_dir = _queued_upload("7b7b7b7b7b02")
    stage.go.set()
    stage.settle.clear()
    jobqueue.resume()
    await _until(lambda: len(stage.procs) == 1)

    assert (await asyncio.to_thread(client.post, f"/api/jobs/{job.id}/trash")).status_code == 200
    await _until(lambda: stage.procs[0].poll() is not None)
    # Stopped, not settled: the page has already let the song go.
    res = await asyncio.to_thread(client.delete, f"/api/jobs/{job.id}")
    assert res.status_code == 202, res.text
    stage.settle.set()

    await _until(lambda: not job_dir.exists())
    await _until(lambda: jobqueue.snapshot() == (None, []))
    assert job.id not in _recorded_ids(jobqueue.JOBS_DIR)
    assert job.id not in _jobs


async def test_a_delete_that_lost_the_race_to_the_finish_still_deletes(
    worker, monkeypatch, client: TestClient
) -> None:
    """The stage never looks at the flag again and finishes anyway."""
    release = threading.Event()

    def finishing(job, source_path, job_dir):
        release.wait(30)

    monkeypatch.setattr(runner, "_run_local_blocking", finishing)
    job, job_dir = _queued_upload("7b7b7b7b7b03")
    jobqueue.resume()
    await _until(lambda: jobqueue.running_id() == job.id)
    try:
        assert (
            await asyncio.to_thread(client.post, f"/api/jobs/{job.id}/trash")
        ).status_code == 200
        res = await asyncio.to_thread(client.delete, f"/api/jobs/{job.id}")
        assert res.status_code == 202, res.text
    finally:
        release.set()

    await _until(lambda: not job_dir.exists())
    await _until(lambda: jobqueue.running_id() is None)
    assert job.id not in _jobs
    assert job.id not in _recorded_ids(jobqueue.JOBS_DIR)


def test_a_running_job_that_is_not_in_the_trash_is_still_refused(client: TestClient) -> None:
    job = Job(id="7b7b7b7b7b04", status="separating", title="Running")
    register(job)
    res = client.delete(f"/api/jobs/{job.id}")
    assert res.status_code == 409
    assert job.id not in registry.deleted_ids()


def test_a_restart_before_the_delete_settled_does_not_bring_it_back(tmp_path) -> None:
    job = Job(id="7b7b7b7b7b05", status="separating", title="Deleted", trashed_at=1.0)
    (tmp_path / job.id).mkdir()
    (tmp_path / job.id / "source.wav").write_bytes(b"RIFF")
    (tmp_path / "registry.json").write_text(
        json.dumps({"version": 1, "jobs": [job.to_record()], "deleted": [job.id]}),
        encoding="utf-8",
    )

    registry.restore(tmp_path)

    assert job.id not in _jobs
    assert not (tmp_path / job.id).exists()
    assert job.id not in _recorded_ids(tmp_path)
