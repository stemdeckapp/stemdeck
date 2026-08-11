from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import jobqueue


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
async def worker(monkeypatch, tmp_path):
    """A running queue worker, torn down after the test."""
    monkeypatch.setattr(jobqueue, "JOBS_DIR", tmp_path)
    task = jobqueue.start_worker()
    yield task
    jobqueue.request_stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _job(job_id: str, url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ") -> Job:
    job = Job(id=job_id, source_url=url)
    _jobs[job_id] = job
    return job


async def _drain(predicate, timeout: float = 3.0) -> None:
    """Wait for the worker to reach a state instead of sleeping a fixed amount."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the queue")


async def test_runs_a_queued_job(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa")

    await _drain(lambda: ran == ["aaaaaaaaaaaa"])


async def test_runs_in_fifo_order(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    ids = ["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]
    for jid in ids:
        _job(jid)
        jobqueue.enqueue(jid)

    await _drain(lambda: len(ran) == 3)
    assert ran == ids


async def test_never_runs_two_jobs_at_once(worker, monkeypatch):
    """The demucs worker in separate.py is a single shared process, so overlap
    would interleave two jobs on one stdin. This is the guarantee that stops it."""
    active = 0
    max_active = 0

    async def _fake(job, url, jobs_dir):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)  # a real pipeline yields; so must this
        active -= 1

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    for jid in ("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"):
        _job(jid)
        jobqueue.enqueue(jid)

    await _drain(lambda: active == 0 and max_active == 1 and jobqueue.depth() == 0)
    assert max_active == 1


async def test_a_raising_job_does_not_kill_the_worker(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)
        if job.id.endswith("1"):
            raise RuntimeError("boom")

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    for jid in ("aaaaaaaaaaa1", "aaaaaaaaaaa2"):
        _job(jid)
        jobqueue.enqueue(jid)

    await _drain(lambda: ran == ["aaaaaaaaaaa1", "aaaaaaaaaaa2"])


async def test_marks_the_job_processing_when_picked_up(worker, monkeypatch):
    seen: list[str] = []

    async def _fake(job, url, jobs_dir):
        seen.append(job.status)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa")

    await _drain(lambda: seen == ["processing"])


async def test_skips_a_job_cancelled_while_waiting(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    job = _job("aaaaaaaaaaaa")
    job.cancel_requested = True
    jobqueue.enqueue("aaaaaaaaaaaa")

    await _drain(lambda: jobqueue.depth() == 0)
    assert ran == []


async def test_skips_a_job_that_vanished_from_the_registry(worker):
    jobqueue.enqueue("aaaaaaaaaaaa")  # never registered
    await _drain(lambda: jobqueue.depth() == 0)


async def test_routes_local_jobs_to_the_local_pipeline(worker, monkeypatch, tmp_path):
    ran = []

    async def _fake_local(job, source, jobs_dir):
        ran.append(("local", job.id, source.name))

    async def _fake_yt(job, url, jobs_dir):
        ran.append(("youtube", job.id, url))

    monkeypatch.setattr("app.pipeline.runner.run_local_pipeline", _fake_local)
    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake_yt)

    (tmp_path / "aaaaaaaaaaaa").mkdir()
    (tmp_path / "aaaaaaaaaaaa" / "source.mp3").write_bytes(b"ID3")
    _job("aaaaaaaaaaaa", url="local:My Song")
    jobqueue.enqueue("aaaaaaaaaaaa")

    await _drain(lambda: ran == [("local", "aaaaaaaaaaaa", "source.mp3")])


async def test_a_local_job_with_no_source_errors_rather_than_crashing(
    worker, monkeypatch, tmp_path
):
    """The upload can be gone after a restart; that must not take the queue down."""
    called = []

    async def _fake_local(job, source, jobs_dir):
        called.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_local_pipeline", _fake_local)
    job = _job("aaaaaaaaaaaa", url="local:Missing")
    jobqueue.enqueue("aaaaaaaaaaaa")

    await _drain(lambda: job.status == "error")
    assert called == []
    assert "no longer available" in (job.error or "")


async def test_request_stop_drains_nothing_further(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    jobqueue.request_stop()
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa")

    await asyncio.sleep(0.1)
    assert ran == []


# ── queue bookkeeping (no worker needed) ──


def test_discard_removes_a_waiting_job():
    jobqueue.enqueue("aaaaaaaaaaa1")
    # Not inside the assert: discard() mutates the queue, and an assert can
    # be stripped (python -O), which would silently skip the thing under test.
    removed = jobqueue.discard("aaaaaaaaaaa1")
    assert removed is True
    assert jobqueue.depth() == 0


def test_discard_reports_false_for_an_unknown_job():
    removed = jobqueue.discard("aaaaaaaaaaa9")
    assert removed is False


def test_enqueue_is_idempotent():
    jobqueue.enqueue("aaaaaaaaaaa1")
    jobqueue.enqueue("aaaaaaaaaaa1")
    assert jobqueue.depth() == 1


def test_snapshot_reports_running_and_waiting():
    jobqueue.enqueue("aaaaaaaaaaa1")
    jobqueue.enqueue("aaaaaaaaaaa2")
    running, waiting = jobqueue.snapshot()
    assert running is None
    assert waiting == ["aaaaaaaaaaa1", "aaaaaaaaaaa2"]


def test_clear_empties_the_queue():
    jobqueue.enqueue("aaaaaaaaaaa1")
    jobqueue.clear()
    assert jobqueue.depth() == 0


# ── pausing ──────────────────────────────────────────────────────────────────


async def test_a_paused_queue_does_not_start_anything(worker, monkeypatch):
    """Opening the app must not put the machine to work on its own."""
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    jobqueue.pause()
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa", autostart=False)

    await asyncio.sleep(0.3)
    assert ran == []
    assert jobqueue.depth() == 1, "the job is still queued, just not started"
    assert jobqueue.is_paused() is True


async def test_resume_starts_the_queue(worker, monkeypatch):
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    jobqueue.pause()
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa", autostart=False)
    await asyncio.sleep(0.2)
    assert ran == []

    jobqueue.resume()
    await _drain(lambda: ran == ["aaaaaaaaaaaa"])
    assert jobqueue.is_paused() is False


async def test_a_new_import_lifts_the_pause(worker, monkeypatch):
    """Pressing Process and having nothing happen would be its own bug, so an
    explicit submit starts the queue -- and drains the restored jobs first."""
    ran = []

    async def _fake(job, url, jobs_dir):
        ran.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    jobqueue.pause()
    _job("aaaaaaaaaaaa")  # restored from the last session
    jobqueue.enqueue("aaaaaaaaaaaa", autostart=False)
    await asyncio.sleep(0.2)
    assert ran == []

    _job("bbbbbbbbbbbb")  # the user imports something new
    jobqueue.enqueue("bbbbbbbbbbbb")

    await _drain(lambda: ran == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    assert jobqueue.is_paused() is False


async def test_pausing_does_not_touch_a_running_job(worker, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = []

    async def _fake(job, url, jobs_dir):
        started.set()
        await release.wait()
        finished.append(job.id)

    monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake)
    _job("aaaaaaaaaaaa")
    jobqueue.enqueue("aaaaaaaaaaaa")
    await asyncio.wait_for(started.wait(), timeout=3)

    jobqueue.pause()
    release.set()
    await _drain(lambda: finished == ["aaaaaaaaaaaa"])


async def test_start_worker_begins_unpaused(worker):
    assert jobqueue.is_paused() is False
