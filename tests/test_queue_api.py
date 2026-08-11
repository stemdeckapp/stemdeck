from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import MAX_PENDING_UPLOAD_JOBS, MAX_PENDING_URL_JOBS
from app.core.models import Job, _set
from app.core.registry import _jobs
from app.pipeline import jobqueue


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client(monkeypatch):
    # No worker: these tests assert what the endpoint reports for a given queue
    # state, and a live worker would drain it mid-assertion.
    monkeypatch.setattr(jobqueue, "start_worker", lambda: _NoopTask())
    from app.main import app

    with TestClient(app) as c:
        yield c


class _NoopTask:
    """Stand-in for the worker task; the lifespan only stores and discards it."""

    def add_done_callback(self, _cb):
        pass

    def cancel(self):
        pass


def _queued(job_id: str, title: str | None = None) -> Job:
    job = Job(id=job_id, title=title, source_url="https://www.youtube.com/watch?v=x")
    _jobs[job_id] = job
    jobqueue._queue.append(job_id)
    return job


# ── GET /api/queue ───────────────────────────────────────────────────────────


def test_empty_queue(client):
    body = client.get("/api/queue").json()
    assert body["running"] is None
    assert body["queued"] == []
    assert body["max_pending_urls"] == MAX_PENDING_URL_JOBS
    assert body["max_pending_uploads"] == MAX_PENDING_UPLOAD_JOBS
    assert body["capacity_left_urls"] == MAX_PENDING_URL_JOBS
    assert body["capacity_left_uploads"] == MAX_PENDING_UPLOAD_JOBS


def test_reports_waiting_jobs_in_order_with_positions(client):
    for i, jid in enumerate(("aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3")):
        _queued(jid, title=f"Track {i}")
    queued = client.get("/api/queue").json()["queued"]
    assert [j["job_id"] for j in queued] == ["aaaaaaaaaaa1", "aaaaaaaaaaa2", "aaaaaaaaaaa3"]
    assert [j["position"] for j in queued] == [0, 1, 2]


def test_reports_the_running_job_separately(client):
    job = _queued("aaaaaaaaaaa1")
    jobqueue._queue.remove(job.id)
    jobqueue._set_running(job.id)
    _set(job, status="processing", stage="Starting...")

    body = client.get("/api/queue").json()
    assert body["running"]["job_id"] == job.id
    assert body["running"]["status"] == "processing"
    assert body["queued"] == []


def test_capacity_reflects_waiting_jobs_only(client):
    _queued("aaaaaaaaaaa1")
    running = Job(id="aaaaaaaaaaa9")
    running.status = "processing"
    _jobs[running.id] = running

    body = client.get("/api/queue").json()
    assert body["capacity_left_urls"] == MAX_PENDING_URL_JOBS - 1, (
        "the running job must not take a slot"
    )


def test_skips_a_queued_id_with_no_registry_entry(client):
    """A reset can empty the registry while ids are still in the deque."""
    jobqueue._queue.append("aaaaaaaaaaa1")
    assert client.get("/api/queue").json()["queued"] == []


def test_payload_is_compact(client):
    """The queue frame ships for every waiting job several times a second, so it
    must not carry stems/sections/analysis."""
    job = _queued("aaaaaaaaaaa1", title="T")
    job.stems = [{"name": "vocals", "url": "/x"}]
    job.sections = [{"id": "a"}]
    rec = client.get("/api/queue").json()["queued"][0]
    assert set(rec) == {
        "job_id",
        "status",
        "progress",
        "stage",
        "title",
        "thumbnail",
        "source_url",
        "error",
        "position",
    }


# ── GET /api/queue/events ────────────────────────────────────────────────────


# Driven directly rather than through TestClient: this stream never self-closes,
# so a TestClient.stream() context would block on exit waiting for it to end.
# Same approach as tests/test_events_stream.py.


class _Stream:
    def __init__(self, it):
        self._it = it

    async def next_data(self, timeout: float = 2.0) -> dict:
        while True:
            chunk = await asyncio.wait_for(self._it.__anext__(), timeout=timeout)
            if chunk.startswith("data: "):
                return json.loads(chunk[len("data: ") : -2])

    async def expect_no_frame(self, timeout: float = 0.7) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self._it.__anext__(), timeout=timeout)

    async def aclose(self) -> None:
        await self._it.aclose()


async def _open() -> _Stream:
    from app.api.queue import queue_events

    response = await queue_events()
    return _Stream(response.body_iterator)


async def test_events_emits_an_initial_frame():
    _queued("aaaaaaaaaaa1", title="T")
    stream = await _open()
    try:
        frame = await stream.next_data()
        assert [j["job_id"] for j in frame["queued"]] == ["aaaaaaaaaaa1"]
    finally:
        await stream.aclose()


async def test_events_emits_again_when_a_job_changes():
    job = _queued("aaaaaaaaaaa1", title="T")
    stream = await _open()
    try:
        await stream.next_data()
        _set(job, progress=0.5, stage="Separating")
        frame = await stream.next_data()
        assert frame["queued"][0]["progress"] == 0.5
    finally:
        await stream.aclose()


async def test_events_does_not_repeat_an_unchanged_queue():
    _queued("aaaaaaaaaaa1", title="T")
    stream = await _open()
    try:
        await stream.next_data()
        await stream.expect_no_frame()
    finally:
        await stream.aclose()


async def test_events_stays_open_when_the_queue_empties():
    """Unlike the per-job stream, this one outlives any single job: it is the
    session-long view, so an empty queue must not end it."""
    job = _queued("aaaaaaaaaaa1", title="T")
    stream = await _open()
    try:
        await stream.next_data()
        jobqueue._queue.remove(job.id)
        frame = await stream.next_data()
        assert frame["queued"] == []
        await stream.expect_no_frame()  # still open, just idle
    finally:
        await stream.aclose()


async def test_events_503_at_the_shared_connection_cap(monkeypatch):
    """The budget is shared with the per-job stream rather than each having its
    own, so twenty queue streams cannot starve job streams."""
    from fastapi import HTTPException

    import app.api.events as events_mod
    from app.api.queue import queue_events

    monkeypatch.setattr(events_mod, "_sse_active", events_mod._MAX_SSE_CONNECTIONS)
    with pytest.raises(HTTPException) as exc:
        await queue_events()
    assert exc.value.status_code == 503


async def test_events_releases_its_slot_on_close():
    """The slot is released in the generator's finally, so the generator has to
    have started -- pull a frame first, as Starlette's response iteration does."""
    import app.api.events as events_mod

    before = events_mod._sse_active
    stream = await _open()
    assert events_mod._sse_active == before + 1
    await stream.next_data()
    await stream.aclose()
    assert events_mod._sse_active == before
