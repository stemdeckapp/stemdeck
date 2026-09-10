"""The last branches in the two streaming API surfaces.

Both are about a connection rather than a result: an SSE stream that has
nothing further to say, and a preview proxy handing bytes through. Neither
shows up in a status code, which is why they were the last two left.
"""

from __future__ import annotations

import asyncio
import contextlib
import urllib.request
from unittest import mock

import pytest

from app.api import events as events_api
from app.api import search as search_api
from app.core.models import Job
from app.core.registry import _jobs

JOB = "abcdefabcdef"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


# --------------------------------------------------------------------------
# the job event stream
# --------------------------------------------------------------------------


async def test_a_stream_closes_when_the_job_ends_without_a_version_bump():
    """A status written straight onto the job -- a restart reconciling state it
    found on disk, say -- terminates it without going through _set(), so the
    version the stream watches never changes. Without this check the connection
    would sit comparing two equal ints until the four-hour cap, holding one of
    the small number of SSE slots against a job that finished."""
    job = Job(id=JOB)
    job.status = "separating"
    _jobs[JOB] = job

    response = await events_api.job_events(JOB)
    stream = response.body_iterator

    first = await stream.__anext__()
    assert first.startswith("data: "), "the current state is sent before anything else"

    # Terminal, but by assignment rather than _set(): same version, new status.
    version_before = job.version
    job.status = "done"
    assert job.version == version_before

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


async def test_a_stream_stays_open_while_the_job_is_still_working():
    """So the close above is a decision about terminal jobs and not the stream
    ending on its own."""
    job = Job(id=JOB)
    job.status = "separating"
    _jobs[JOB] = job

    response = await events_api.job_events(JOB)
    stream = response.body_iterator

    await stream.__anext__()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.5)

    await stream.aclose()


# --------------------------------------------------------------------------
# the search cache re-check behind the semaphore
# --------------------------------------------------------------------------


class _Request:
    """The two things the search endpoint asks of a Request."""

    def __init__(self, body):
        self._body = body
        self.client = None

    async def json(self):
        return self._body


async def test_a_query_that_was_answered_while_it_queued_is_served_from_the_cache(monkeypatch):
    """Typing into the search box fires one request per keystroke and the last
    two are routinely identical. The first one through fills the cache while
    the second is still waiting for a slot; running the second search anyway
    would spend a second round trip to YouTube on an answer already in hand."""
    searched: list[str] = []

    def _never(*a, **kw):
        searched.append("ran")
        raise AssertionError("the queued request searched anyway")

    class _FillsTheCacheWhileYouWait:
        """The semaphore, standing in for the request that got there first."""

        async def __aenter__(self):
            search_api._cache_put(_key(), {"results": [{"id": "first"}]})

        async def __aexit__(self, *_exc):
            return False

    def _key():
        return (
            "youtube",
            "track",
            "daft punk",
            10,
            search_api.get_max_duration_sec(),
        )

    monkeypatch.setattr(search_api, "_semaphore", _FillsTheCacheWhileYouWait())
    monkeypatch.setattr(search_api, "search", _never)
    monkeypatch.setattr(search_api, "supported", lambda source, kind: True)
    search_api._clear_cache()

    body = {"query": "Daft Punk", "source": "youtube", "kind": "track", "limit": 10}
    result = await search_api.search_sources(_Request(body))

    assert result["cached"] is True
    assert result["results"] == [{"id": "first"}]
    assert not searched


# --------------------------------------------------------------------------
# the preview proxy
# --------------------------------------------------------------------------


@contextlib.contextmanager
def _urlopen_returning(upstream):
    with mock.patch.object(urllib.request, "urlopen", lambda req, timeout=None: upstream):
        yield


def test_the_preview_proxy_hands_the_upstream_body_through_in_chunks():
    """The audition button plays while the file is still arriving, so the body
    is a generator over the socket rather than a buffered read. A change that
    made it collect first would still pass every status-code test."""

    class _Upstream:
        def __init__(self):
            self.chunks = [b"aaaa", b"bbbb", b""]
            self.closed = False

        def read(self, _n):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    upstream_obj = _Upstream()
    with _urlopen_returning(upstream_obj):
        _upstream, body = search_api._proxy(
            {"url": "https://example.invalid/audio.m4a", "headers": {}},
            range_header="bytes=0-",
        )

    assert list(body()) == [b"aaaa", b"bbbb"]
    assert upstream_obj.closed, "the socket was left open after the last chunk"
