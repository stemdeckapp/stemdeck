"""Removing a song from the Queue stops its extraction and nothing else (#772).

The Queue's remove button used to send POST /cancel, which settles a job as
"cancelled" and deletes its folder. The song then left the Library too, and it
never reached the Trash, so the upload was gone for good. Removing from the
Queue now stops the job the way a trash does (#748), without the Trash: it
settles as "stopped" with its files kept, stays in the Library, and Extract
runs it again.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.config import DEMUCS_MODEL
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs, register
from app.core.registry import get_proc as registry_get_proc
from app.main import app
from app.pipeline import jobqueue


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _queued_upload(job_id: str):
    job = Job(id=job_id, status="queued", title="Waiting", source_url="local:Waiting")
    register(job)
    jobqueue.enqueue(job.id, autostart=False)
    job_dir = jobqueue.JOBS_DIR / job.id
    job_dir.mkdir(parents=True)
    (job_dir / "source.wav").write_bytes(b"RIFF")
    return job, job_dir


def test_removing_a_waiting_upload_keeps_it_and_its_file(client: TestClient) -> None:
    job, job_dir = _queued_upload("7a7a7a7a7a01")

    res = client.post(f"/api/jobs/{job.id}/stop")

    assert res.status_code == 200
    assert res.json()["status"] == "stopped"
    assert job.status == "stopped", "a cancelled job is dropped from the Library"
    assert job.trashed_at is None, "removing from the Queue is not a delete"
    assert jobqueue.snapshot() == (None, []), "it would still run"
    assert (job_dir / "source.wav").is_file(), "the upload was deleted with no Trash"


def test_a_removed_upload_can_be_extracted_again(client: TestClient) -> None:
    job, job_dir = _queued_upload("7a7a7a7a7a02")
    client.post(f"/api/jobs/{job.id}/stop")

    res = client.post(f"/api/jobs/{job.id}/extract")

    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    assert jobqueue.snapshot() == (None, [job.id])
    assert (job_dir / "source.wav").is_file()


def test_a_waiting_link_with_no_audio_yet_is_kept_and_extract_fetches_it(
    client: TestClient,
) -> None:
    """A link has no audio until its turn. Its link is the song: kept as
    stopped, Extract queues it and the pipeline downloads it again."""
    job = Job(
        id="7a7a7a7a7a03",
        status="queued",
        source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )
    register(job)
    jobqueue.enqueue(job.id, autostart=False)

    assert client.post(f"/api/jobs/{job.id}/stop").status_code == 200
    assert job.status == "stopped"
    assert job.source_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    assert client.post(f"/api/jobs/{job.id}/extract").json()["status"] == "queued"
    assert jobqueue.snapshot() == (None, [job.id])


def test_stopping_a_finished_job_changes_nothing(client: TestClient) -> None:
    """The row can outlive the job by a frame. A late click must not touch a
    song that just finished."""
    job = Job(id="7a7a7a7a7a04", status="done", title="Finished")
    register(job)

    res = client.post(f"/api/jobs/{job.id}/stop")

    assert res.status_code == 200
    assert job.status == "done"
    assert job.cancel_requested is False and job.stop_requested is False


def test_stop_rejects_a_malformed_or_unknown_id(client: TestClient) -> None:
    for bad in ("../../etc/passwd", "not a job id", "%2e%2e%2f", "ABCDEF123456"):
        resp = client.post(f"/api/jobs/{bad}/stop")
        assert 400 <= resp.status_code < 500, (bad, resp.status_code)
    res = client.post("/api/jobs/aaaaaaaaaaaa/stop")
    assert res.status_code == 404
    assert res.json() == {"detail": "job not found"}


# ─── a running job ───


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


async def test_removing_the_running_job_kills_its_process_and_keeps_the_song(
    worker, monkeypatch, client: TestClient
) -> None:
    """A real child process stands in for Demucs or ffmpeg: it must die, the
    job must settle, and the partial separation must not stay behind."""
    from app.core.registry import set_proc
    from app.pipeline import runner

    started = {}

    def fake_blocking(job, source_path, job_dir):
        (job_dir / DEMUCS_MODEL).mkdir()
        (job_dir / DEMUCS_MODEL / "partial.wav").write_bytes(b"RIFF")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        started["proc"] = proc
        set_proc(job.id, proc)
        try:
            proc.wait(timeout=30)
        finally:
            set_proc(job.id, None)
        if job.cancel_requested:
            raise JobCancelled()

    monkeypatch.setattr(runner, "_run_local_blocking", fake_blocking)
    job, job_dir = _queued_upload("7a7a7a7a7a05")
    jobqueue.resume()

    await _until(lambda: "proc" in started and registry_get_proc(job.id) is not None)

    res = await asyncio.to_thread(client.post, f"/api/jobs/{job.id}/stop")
    assert res.status_code == 200

    await _until(lambda: job.status == "stopped")
    assert started["proc"].poll() is not None, "the child process outlived the stop"
    assert registry_get_proc(job.id) is None
    assert jobqueue.snapshot() == (None, [])
    assert job.trashed_at is None
    assert (job_dir / "source.wav").is_file()
    assert not (job_dir / DEMUCS_MODEL).exists(), "partial separation output was left"
