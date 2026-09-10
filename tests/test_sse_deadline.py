"""The four-hour cap on both event streams.

Every other exit from these loops depends on the client or the job: the browser
closing the tab, the job reaching a terminal status. The deadline is the only
one that does not, and it is what bounds a stream nobody is reading -- a laptop
that slept with the page open, a phone whose connection died without a FIN.
Each open stream holds one of a small number of SSE slots, so a stream that
could not expire would eventually refuse new listeners with a 503.

Four hours of real polling is not a test, so both streams get a module-local
`asyncio` whose clock only moves when the stream sleeps.
"""

from __future__ import annotations

import pytest

from app.api import events as events_api
from app.api import queue as queue_api
from app.core.models import Job
from app.core.registry import _jobs

JOB = "abcdefabcdef"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


class _FastForward:
    """The `asyncio` module with a clock that only advances on sleep, so a
    stream reaches its deadline in as many iterations as it has ticks."""

    def __init__(self):
        self._t = 0.0

    class _Loop:
        def __init__(self, outer):
            self._outer = outer

        def time(self):
            return self._outer._t

    def get_running_loop(self):
        return self._Loop(self)

    async def sleep(self, seconds):
        self._t += seconds


async def _drain(response, limit=200_000):
    """Consume a stream to its natural end, refusing to loop forever."""
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        if len(chunks) > limit:
            raise AssertionError("the stream never reached its deadline")
    return chunks


async def test_a_job_stream_nobody_is_reading_expires_on_its_own(monkeypatch):
    """The job never finishes and the client never disconnects -- the shape of
    a sleeping laptop. The stream has to end anyway, or the slot it holds is
    gone until the server restarts."""
    job = Job(id=JOB)
    job.status = "separating"
    _jobs[JOB] = job

    monkeypatch.setattr(events_api, "asyncio", _FastForward())

    response = await events_api.job_events(JOB)
    chunks = await _drain(response)

    assert chunks[0].startswith("data: "), "the current state is still sent first"
    assert chunks[-1].startswith(": keepalive"), "it stopped somewhere other than the cap"


async def test_the_queue_stream_expires_the_same_way(monkeypatch):
    """The queue stream has no terminal status to close on at all: an idle
    queue is a perfectly normal steady state, so the deadline is its only
    exit."""
    monkeypatch.setattr(queue_api, "asyncio", _FastForward())

    response = await queue_api.queue_events()
    chunks = await _drain(response)

    assert chunks[0].startswith("data: ")
    assert chunks[-1].startswith(": keepalive")
