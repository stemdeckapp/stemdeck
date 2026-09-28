"""Cancel reaches an import while yt-dlp is still resolving the link.

The metadata probe has no progress hook, and resolving a YouTube link takes
up to twenty seconds; Cancel used to wait all of it out (the acceptance run's
E5 measured 19 to 22 s)."""

from __future__ import annotations

import threading
import time

import pytest

import app.pipeline.download as dl
from app.core.models import Job, JobCancelled


class _SlowYoutubeDL:
    """A YoutubeDL whose probe takes ``seconds`` and records that it ran."""

    started = threading.Event()

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        _SlowYoutubeDL.started.set()
        time.sleep(5)
        return {"duration": 200, "title": "x"}


def _cancel_soon(job: Job, after: float) -> None:
    def later():
        _SlowYoutubeDL.started.wait(2)
        time.sleep(after)
        job.cancel_requested = True

    threading.Thread(target=later, daemon=True).start()


def test_cancel_stops_the_wait_for_the_metadata_probe(tmp_path, monkeypatch):
    _SlowYoutubeDL.started.clear()
    monkeypatch.setattr(dl, "YoutubeDL", _SlowYoutubeDL)
    job = Job(id="abcdefcafe01", title="x")
    _cancel_soon(job, 0.2)
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        dl.download(job, "https://www.youtube.com/watch?v=Zi_XLOBDo_Y", tmp_path)
    assert time.monotonic() - started < 1.5, "not the five seconds the probe takes"


def test_a_probe_that_finishes_answers_as_before():
    job = Job(id="abcdefcafe02", title="x")
    assert dl._cancellable(job, lambda: {"duration": 1}) == {"duration": 1}


def test_a_probe_that_fails_raises_its_own_error():
    job = Job(id="abcdefcafe03", title="x")

    def boom():
        raise ValueError("unavailable")

    with pytest.raises(ValueError, match="unavailable"):
        dl._cancellable(job, boom)


def test_the_wait_between_retries_stops_on_cancel(monkeypatch):
    job = Job(id="abcdefcafe04", title="x")
    job.cancel_requested = True
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        dl._sleep_unless_cancelled(job, 10)
    assert time.monotonic() - started < 0.5
