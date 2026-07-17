"""Tests for the shared download retry policy (#279)."""

from __future__ import annotations

import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import download as dl_mod


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Backoff sleeps are pointless in tests."""
    monkeypatch.setattr(dl_mod.time, "sleep", lambda _s: None)


@pytest.fixture()
def job():
    return Job(id="abcdefabc279")


def test_retries_transient_error_then_succeeds(job):
    calls: list[int] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OSError("Connection reset by peer")
        return {"title": "ok"}

    result = dl_mod._with_retries(job, flaky, what="metadata probe")

    assert result == {"title": "ok"}
    assert len(calls) == 3
    # The retry stage message reached the job (user-visible feedback).
    assert "retrying" in job.stage_message


def test_non_retriable_raises_immediately(job):
    calls: list[int] = []

    def private_video():
        calls.append(1)
        raise RuntimeError("ERROR: Private video. Sign in if you have access")

    with pytest.raises(RuntimeError, match="Private video"):
        dl_mod._with_retries(job, private_video, what="metadata probe")

    assert len(calls) == 1  # never retried


def test_exhausted_retries_reraise_last_error(job):
    calls: list[int] = []

    def always_down():
        calls.append(1)
        raise OSError("Read timed out")

    with pytest.raises(OSError, match="timed out"):
        dl_mod._with_retries(job, always_down, what="download")

    assert len(calls) == dl_mod._MAX_RETRIES + 1


def test_cancel_mid_attempt_becomes_jobcancelled(job):
    def fails():
        job.cancel_requested = True  # POST /cancel raced the attempt
        raise OSError("connection reset")

    with pytest.raises(JobCancelled):
        dl_mod._with_retries(job, fails, what="download")


def test_unrecognized_error_is_not_retried(job):
    """Errors matching neither list are treated as permanent -- retrying an
    unknown failure mode would just triple the wait for the same outcome."""
    calls: list[int] = []

    def weird():
        calls.append(1)
        raise ValueError("some novel explosion")

    with pytest.raises(ValueError):
        dl_mod._with_retries(job, weird, what="download")

    assert len(calls) == 1
