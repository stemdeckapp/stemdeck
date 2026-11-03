"""Two ways the registry used to strand state (#520).

A cancel that lands in the window between the queue worker popping a job and
claiming it left the job at "queued" forever, invisible but still counted
against the capacity limit. And a registry.json that was valid JSON but not an
object stopped the backend booting at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time

import httpx
import pytest

from app.core import registry as _registry
from app.core.config import JOB_ID_RE
from app.core.models import Job
from app.core.registry import registry_path
from app.pipeline import jobqueue
from app.pipeline import runner as _runner


def _job(job_id="a1b2c3d4e5f6", **kw):
    return Job(id=job_id, **kw)


# ─── a registry file we cannot use must not stop the backend ───


@pytest.mark.parametrize("body", ["[1, 2, 3]", "null", '"a string"', "42", "[]"])
def test_a_non_object_registry_does_not_raise(tmp_path, body):
    # _migrate calls data.get(); anything that is not a dict raises
    # AttributeError, which restore() ran at import time and never caught.
    registry_path(tmp_path).write_text(body, encoding="utf-8")

    _registry.restore(tmp_path)  # must not raise

    assert _registry.all_jobs() == {}


def test_a_corrupt_registry_does_not_raise(tmp_path):
    registry_path(tmp_path).write_text("{not json", encoding="utf-8")

    _registry.restore(tmp_path)

    assert _registry.all_jobs() == {}


def test_a_good_registry_still_loads(tmp_path):
    job = _job(status="done", title="Song")
    registry_path(tmp_path).write_text(json.dumps({"jobs": [job.to_record()]}), encoding="utf-8")

    _registry.restore(tmp_path)

    assert job.id in _registry.all_jobs()


def test_an_unreadable_jobs_dir_does_not_raise(tmp_path, monkeypatch):
    # Orphan recovery sat outside the guard, so an OSError from iterdir() was
    # fatal at startup too.
    def _boom(self):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.iterdir", _boom)

    _registry.restore(tmp_path)  # must not raise


# ─── a cancel in the pop-to-claim window must not strand the job ───


def test_job_id_re_matches_the_ids_we_generate():
    # The orphan-recovery filter depends on this; a drift here would silently
    # stop recovery working at all.
    assert JOB_ID_RE.match("a1b2c3d4e5f6")


async def test_cancel_between_pop_and_claim_finalises_the_job(tmp_path, monkeypatch):
    """Drive the real worker loop, so this also catches the worker simply not
    calling the finaliser."""
    import asyncio

    from app.pipeline import jobqueue

    job = _job(status="queued")
    _registry.register(job)
    jobqueue.enqueue(job.id)
    assert _registry.pending_count(uploads=False) == 1

    # The cancel lands in the pop-to-claim window: cancel_job's discard() has
    # already lost the race, so all it can do is set the flag.
    job.cancel_requested = True

    task = jobqueue.start_worker()
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if job.status == "cancelled":
                break
    finally:
        jobqueue.request_stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert job.status == "cancelled", "a stranded job holds a capacity slot forever"
    assert _registry.pending_count(uploads=False) == 0, "the capacity slot must be released"


async def test_a_dropped_job_that_was_not_cancelled_is_left_alone(tmp_path):
    # Already-terminal jobs reach the same branch; finalising them would
    # rewrite a real result.
    job = _job(status="done", title="Song")
    _registry.register(job)

    jobqueue_mod = __import__("app.pipeline.jobqueue", fromlist=["jobqueue"])
    await jobqueue_mod._finalise_dropped_job(job)

    assert job.status == "done"


# ─── a registry save holds neither the lock nor the event loop while it writes ───


def _slow_write(monkeypatch) -> threading.Event:
    """Make every registry write take 0.2 s, the way an fsync on a NAS can.
    The event is set while one is in progress."""
    real_write = _registry._write_atomic
    writing = threading.Event()

    def slow(path, payload):
        writing.set()
        try:
            time.sleep(0.2)
            real_write(path, payload)
        finally:
            writing.clear()

    monkeypatch.setattr(_registry, "_write_atomic", slow)
    return writing


def test_the_write_does_not_hold_the_registry_lock(tmp_path, monkeypatch):
    """Every registry.get, from every request and stream, waited on the disk."""
    held: list[bool] = []
    real_write = _registry._write_atomic

    def write(path, payload):
        held.append(_registry._lock.locked())
        real_write(path, payload)

    monkeypatch.setattr(_registry, "_write_atomic", write)
    job = _job(status="done", title="Song")
    monkeypatch.setitem(_registry._jobs, job.id, job)

    _registry.persist(tmp_path)

    assert held == [False]
    saved = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert [record["id"] for record in saved["jobs"]] == [job.id]


def test_a_later_save_is_never_overwritten_by_an_earlier_one(tmp_path, monkeypatch):
    job = _job(status="done", title="Before")
    monkeypatch.setitem(_registry._jobs, job.id, job)
    writing = _slow_write(monkeypatch)
    first = threading.Thread(target=_registry.persist, args=(tmp_path,))
    first.start()
    assert writing.wait(1)
    job.title = "After"
    _registry.persist(tmp_path)
    first.join()

    saved = json.loads(registry_path(tmp_path).read_text(encoding="utf-8"))
    assert saved["jobs"][0]["title"] == "After"


def test_a_save_still_writing_cannot_undo_a_reset(tmp_path, monkeypatch):
    job = _job(status="done", title="Song")
    monkeypatch.setitem(_registry._jobs, job.id, job)
    writing = _slow_write(monkeypatch)
    save = threading.Thread(target=_registry.persist, args=(tmp_path,))
    save.start()
    assert writing.wait(1)
    _registry.reset_all(tmp_path)
    save.join()

    assert not registry_path(tmp_path).exists()


def test_a_save_while_the_pipeline_records_a_timing_does_not_raise(tmp_path, monkeypatch):
    """The pipeline thread adds stage timings without the registry lock, and a
    save serialising the same dict raised 'dictionary changed size during
    iteration': a favourite or an import failed with a 500 (F42)."""
    job = _job(status="done", title="Song")
    monkeypatch.setitem(_registry._jobs, job.id, job)
    monkeypatch.setattr(_registry, "_write_atomic", lambda path, payload: None)
    stop = threading.Event()

    def pipeline():
        start = time.monotonic()
        i = 0
        while not stop.is_set():
            i += 1
            if i % 200 == 0:
                job.stage_timings = None
            start = _runner._lap(job, f"stage{i}", start)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    thread = threading.Thread(target=pipeline)
    thread.start()
    try:
        for _ in range(2000):
            _registry.persist(tmp_path)
    finally:
        stop.set()
        thread.join()
        sys.setswitchinterval(interval)


def test_a_timing_never_changes_a_dict_a_save_may_be_reading():
    job = _job(stage_timings={"download": 1.0})
    before = job.stage_timings

    _runner._lap(job, "separate", time.monotonic())

    assert before == {"download": 1.0}
    assert set(job.stage_timings) == {"download", "separate"}


async def _ticks_while(writing: threading.Event, run) -> int:
    """How often another coroutine got to run while a registry write was in
    progress."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            if writing.is_set():
                ticks += 1
            await asyncio.sleep(0.005)

    task = asyncio.create_task(ticker())
    try:
        await run
    finally:
        task.cancel()
    return ticks


async def test_a_route_saving_the_registry_leaves_the_loop_free(monkeypatch):
    from app.core.config import JOBS_DIR
    from app.core.versions import DEFAULT_VERSION_ID
    from app.main import app

    job = _job(status="done", title="Song", selected_stems=["vocals", "drums"])
    (JOBS_DIR / job.id).mkdir(parents=True)
    monkeypatch.setitem(_registry._jobs, job.id, job)
    writing = _slow_write(monkeypatch)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response_task = client.post(f"/api/jobs/{job.id}/versions/{DEFAULT_VERSION_ID}/select")
        ticks = await _ticks_while(writing, response_task)

    assert ticks > 0, "the registry write blocked the event loop"


@pytest.mark.parametrize("ending", ["done", "cancelled", "dropped", "emptied from the Trash"])
async def test_the_pipeline_saving_the_registry_leaves_the_loop_free(tmp_path, monkeypatch, ending):
    job = _job(status="processing", title="Song", cancel_requested=ending != "done")
    monkeypatch.setitem(_registry._jobs, job.id, job)
    (tmp_path / job.id).mkdir()
    writing = _slow_write(monkeypatch)

    def run(_job, _job_dir):
        if job.cancel_requested:
            raise _runner.JobCancelled()

    if ending == "dropped":
        work = jobqueue._finalise_dropped_job(job)
    elif ending == "emptied from the Trash":
        _registry.mark_deleted(job.id)
        work = jobqueue.finish_deferred_delete(job, tmp_path)
    else:
        work = _runner._run_async(job, tmp_path / job.id, tmp_path, run)
    ticks = await _ticks_while(writing, work)

    assert ticks > 0, "the registry write blocked the event loop"
