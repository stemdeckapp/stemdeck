"""Two ways the registry used to strand state (#520).

A cancel that lands in the window between the queue worker popping a job and
claiming it left the job at "queued" forever, invisible but still counted
against the capacity limit. And a registry.json that was valid JSON but not an
object stopped the backend booting at all.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from app.core import registry as _registry
from app.core.config import JOB_ID_RE
from app.core.models import Job
from app.core.registry import registry_path


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
