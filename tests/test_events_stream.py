from __future__ import annotations

import asyncio
import json

import pytest

from app.api.events import job_events
from app.core.models import Job, _set


@pytest.fixture(autouse=True)
def _isolate_registry():
    from app.core.registry import _jobs

    _jobs.clear()
    yield
    _jobs.clear()


def _register(job: Job) -> Job:
    from app.core.registry import _jobs

    _jobs[job.id] = job
    return job


def _parse(chunk: str) -> dict:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: ") : -2])


class _Stream:
    """Thin wrapper so each test can pull frames with a bounded wait instead
    of guessing how many internal 0.2s poll ticks a change takes to surface,
    and reliably closes the generator (decrementing _sse_active) on exit."""

    def __init__(self, it):
        self._it = it

    async def next_data(self, timeout: float = 1.5) -> dict:
        while True:
            chunk = await asyncio.wait_for(self._it.__anext__(), timeout=timeout)
            if chunk.startswith("data: "):
                return _parse(chunk)
            # keepalive comment -- keep waiting for the next real frame

    async def expect_no_frame(self, timeout: float = 0.6) -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(self._it.__anext__(), timeout=timeout)

    async def expect_closed(self, timeout: float = 1.5) -> None:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(self._it.__anext__(), timeout=timeout)

    async def aclose(self) -> None:
        await self._it.aclose()


async def _open(job: Job) -> _Stream:
    response = await job_events(job.id)
    return _Stream(response.body_iterator)


@pytest.mark.asyncio
async def test_initial_snapshot_always_sent():
    job = _register(Job(id="abcdefabcdef", status="done"))
    stream = await _open(job)
    try:
        frame = await stream.next_data()
        assert frame["job_id"] == "abcdefabcdef"
        assert frame["status"] == "done"
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_no_duplicate_frames_when_state_is_static():
    job = _register(Job(id="abcdefabcdee", status="downloading"))
    stream = await _open(job)
    try:
        await stream.next_data()  # initial snapshot
        await stream.expect_no_frame()  # nothing changed -- no re-serialization
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_frame_emitted_on_set():
    job = _register(Job(id="abcdefabced0", status="downloading"))
    stream = await _open(job)
    try:
        first = await stream.next_data()
        assert first["progress"] == 0.0

        _set(job, progress=0.5, stage="Downloading 50%")
        second = await stream.next_data()
        assert second["progress"] == 0.5
        assert second["stage"] == "Downloading 50%"

        _set(job, status="done", progress=1.0, stage="Done")
        third = await stream.next_data()
        assert third["status"] == "done"

        # Terminal state was reached -- the generator ends on its own.
        await stream.expect_closed()
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_torn_read_is_discarded_and_reserialized():
    """#285: a snapshot taken while _set() is mid-flight must never reach the
    client. Simulate the torn read by monkeypatching to_state() to bump the
    job's version partway through the first real call, forcing the stream
    loop to detect the mismatch and retry instead of yielding it."""
    job = _register(Job(id="abcdefabced1", status="downloading"))
    original_to_state = Job.to_state
    state = {"bumped": False}

    def torn_to_state(self):
        if not state["bumped"]:
            state["bumped"] = True
            self.version += 1  # simulate a concurrent _set() mid-serialize
        return original_to_state(self)

    Job.to_state = torn_to_state
    stream = await _open(job)
    try:
        frame = await stream.next_data()
    finally:
        Job.to_state = original_to_state
        await stream.aclose()

    # The torn snapshot was discarded; what actually arrived is internally
    # consistent (version matched what was read just before serializing).
    assert frame["status"] == "downloading"
    assert state["bumped"] is True


@pytest.mark.asyncio
async def test_already_terminal_closes_promptly():
    job = _register(Job(id="abcdefabced2", status="error", error="boom"))
    stream = await _open(job)
    try:
        frame = await stream.next_data()
        assert frame["status"] == "error"
        # No idling on int-compares for an already-terminal job -- the
        # generator returns right after the initial snapshot.
        await stream.expect_closed()
    finally:
        await stream.aclose()
