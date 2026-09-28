"""A MusicBrainz request that is being refused stops when its job is
cancelled, and never runs past MUSICBRAINZ_REQUEST_BUDGET_SEC in all.

Every lookup hands its job's cancel check down to the request: a request
that could not see it retried for up to a minute after the job was gone,
holding the queue every other job's lookups wait in."""

from __future__ import annotations

import urllib.error

import pytest

import app.pipeline.musicbrainz as mb
import app.pipeline.name_aliases as na
from app.pipeline import ratelimit

# Taken at import, before conftest stands in for the network: these tests
# drive the real request with urlopen replaced instead.
_real_fetch_json = mb._fetch_json

MBID = "b9545342-1e6d-4dae-84ac-013374ad8d7c"


class _Refusing:
    """urlopen answering 503, with ``retry_after`` when given."""

    def __init__(self, retry_after=None):
        self.retry_after = retry_after
        self.calls = 0

    def __call__(self, request, timeout=None, context=None):
        self.calls += 1
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after else {}
        raise urllib.error.HTTPError(request.full_url, 503, "busy", headers, None)


@pytest.fixture
def refusing(monkeypatch):
    opener = _Refusing()
    monkeypatch.setattr(mb.urllib.request, "urlopen", opener)
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda **kw: None)
    return opener


def test_a_cancelled_job_stops_waiting_to_ask_again(refusing, monkeypatch):
    slept = []
    monkeypatch.setattr(mb, "_sleep", slept.append)
    with pytest.raises(ratelimit.RateLimited):
        _real_fetch_json("recording", {"query": "x"}, cancelled=lambda: True)
    assert refusing.calls == 1
    assert slept == [], "not a single step of the wait"


def test_refusals_stop_at_the_budget(refusing, monkeypatch):
    refusing.retry_after = 8
    monkeypatch.setattr(mb, "MUSICBRAINZ_REQUEST_BUDGET_SEC", 5.0)
    monkeypatch.setattr(mb, "_sleep", lambda s: pytest.fail("waited past the budget"))
    with pytest.raises(urllib.error.HTTPError):
        _real_fetch_json("recording", {"query": "x"})
    assert refusing.calls == 1


def test_the_turn_in_the_queue_is_waited_for_with_the_cancel_check(monkeypatch):
    seen = {}

    def wait(**kwargs):
        seen.update(kwargs)
        raise ratelimit.RateLimited("cancelled while waiting")

    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", wait)
    check = lambda: True  # noqa: E731
    with pytest.raises(ratelimit.RateLimited):
        _real_fetch_json("recording", {"query": "x"}, cancelled=check)
    assert seen["cancelled"] is check


@pytest.fixture
def recorder(monkeypatch):
    seen = []

    def fetch(path, params, *, cancelled=lambda: False):
        seen.append(cancelled)
        return {}

    monkeypatch.setattr(mb, "_fetch_json", fetch)
    return seen


@pytest.mark.parametrize(
    "lookup",
    [
        lambda check: mb.lookup_recording(MBID, cancelled=check),
        lambda check: mb.artist_wikidata_id(MBID, cancelled=check),
        lambda check: mb.lookup_release_group(MBID, cancelled=check),
        lambda check: mb.work_titles(MBID, cancelled=check),
        lambda check: mb.search_recording("IU", "Good Day", 233.0, cancelled=check),
        lambda check: na._musicbrainz_artist(MBID, cancelled=check),
    ],
    ids=["recording", "artist", "release-group", "work-titles", "search", "aliases"],
)
def test_every_lookup_hands_its_cancel_check_to_the_request(recorder, lookup):
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        return False

    lookup(check)
    assert recorder, "asked MusicBrainz"
    assert all(c is check for c in recorder)
