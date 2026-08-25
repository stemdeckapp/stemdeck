"""Cookies are a fallback, never the default path (#432).

Supplying cookies makes yt-dlp skip every client that does not support them,
which removes the unauthenticated fallback clients that resolve formats today
without any JS challenge solver. Applying them to every request would break
imports that currently work in order to fix imports for the smaller group whose
IP YouTube has flagged.

So the first attempt never carries cookies, and they are used only once YouTube
has actually turned us away. By then the path they displace has already failed,
which is what makes the setting incapable of making anything worse.
"""

from __future__ import annotations

import pytest

from app.core.models import Job, JobCancelled
from app.pipeline import download as dl_mod

BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you're not a bot."
NO_FORMAT = "ERROR: [youtube] abc: Requested format is not available"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(dl_mod.time, "sleep", lambda _s: None)


@pytest.fixture
def job():
    return Job(id="abcdefabc432")


def _recorder(failures):
    """fn(use_cookies) that raises `failures` in order, then succeeds."""
    seen: list[bool] = []
    remaining = list(failures)

    def fn(use_cookies: bool):
        seen.append(use_cookies)
        if remaining:
            raise RuntimeError(remaining.pop(0))
        return {"ok": True}

    return fn, seen


def test_happy_path_never_touches_cookies(job, monkeypatch):
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    fn, seen = _recorder([])
    result, used = dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert result == {"ok": True}
    assert used is False
    assert seen == [False], "a succeeding request must never be retried with cookies"


def test_bot_check_retries_with_cookies(job, monkeypatch):
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    fn, seen = _recorder([BOT_CHECK])
    result, used = dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert result == {"ok": True}
    assert used is True
    assert seen == [False, True], "cookie-less first, cookies only on the retry"


def test_no_retry_without_a_configured_cookie_file(job, monkeypatch):
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: None)
    fn, seen = _recorder([BOT_CHECK])
    with pytest.raises(RuntimeError, match="not a bot"):
        dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert seen == [False]


def test_a_non_bot_failure_is_not_retried(job, monkeypatch):
    """Cookies fix a bot check. They do nothing for a deleted video, and
    retrying would just cost the user another round trip."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    fn, seen = _recorder(["ERROR: [youtube] abc: Video unavailable"])
    with pytest.raises(RuntimeError, match="Video unavailable"):
        dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert seen == [False]


def test_cancel_beats_the_retry(job, monkeypatch):
    """A cancel mid-attempt surfaces as JobCancelled (via _with_retries) and
    must not be followed by a second, cookie-bearing request."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    job.cancel_requested = True
    fn, seen = _recorder([BOT_CHECK])
    with pytest.raises(JobCancelled):
        dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert seen == [False], "a cancelled job must not start a second attempt"


def test_missing_solver_is_explained(job, monkeypatch):
    """Cookies clear the bot check, then the job dies for want of a challenge
    solver. Without this the user sees 'Requested format is not available' and
    has no way to connect it to the setting they just turned on."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    monkeypatch.setattr(dl_mod, "js_solver_available", lambda: False)
    fn, _ = _recorder([BOT_CHECK, NO_FORMAT])
    with pytest.raises(RuntimeError) as excinfo:
        dl_mod._with_cookie_fallback(job, fn, what="probe")
    msg = str(excinfo.value)
    assert "JavaScript runtime" in msg
    assert "Clearing the cookies path" in msg, "the message must name the way out"


def test_the_real_error_survives_when_a_solver_exists(job, monkeypatch):
    """With a runtime present, a format failure is a genuine format failure and
    must not be rewritten into a misleading solver explanation."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    monkeypatch.setattr(dl_mod, "js_solver_available", lambda: True)
    fn, _ = _recorder([BOT_CHECK, NO_FORMAT])
    with pytest.raises(RuntimeError, match="Requested format is not available"):
        dl_mod._with_cookie_fallback(job, fn, what="probe")


def test_the_original_error_is_chained(job, monkeypatch):
    """The rewritten message must not throw away the yt-dlp text underneath."""
    monkeypatch.setattr(dl_mod, "get_cookies_file", lambda: "/tmp/c.txt")
    monkeypatch.setattr(dl_mod, "js_solver_available", lambda: False)
    fn, _ = _recorder([BOT_CHECK, NO_FORMAT])
    with pytest.raises(RuntimeError) as excinfo:
        dl_mod._with_cookie_fallback(job, fn, what="probe")
    assert "Requested format is not available" in str(excinfo.value.__cause__)


@pytest.mark.parametrize(
    "text",
    [
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot.",
        "HTTP Error 429: Too Many Requests",
        "ERROR: too many requests, try again later",
    ],
)
def test_bot_check_detection(text):
    assert dl_mod._is_bot_check(RuntimeError(text))


def test_bot_check_detection_is_not_greedy():
    assert not dl_mod._is_bot_check(RuntimeError("Video unavailable"))
    assert not dl_mod._is_bot_check(RuntimeError("HTTP Error 404: Not Found"))
