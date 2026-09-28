"""The request rate every web service StemDeck asks is owed, per process.

MusicBrainz allows one request a second per client and blocks a client that
goes faster; AcoustID allows three. Identification, the band lookup and the
tag backfill can all be asking at the same moment from different threads, so
the limit lives here, once per service, and every caller takes its turn from
the same instance. Anything that calls MusicBrainz goes through MUSICBRAINZ.

A turn is reserved under the lock and slept for outside it, so callers are
served in the order they asked and never sleep while holding anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from app.core.config import (
    ACOUSTID_MIN_INTERVAL_SEC,
    DISCOGS_MIN_INTERVAL_SEC,
    MUSICBRAINZ_MIN_INTERVAL_SEC,
    RATE_LIMIT_MAX_WAIT_SEC,
)


class RateLimited(Exception):
    """The next turn is further off than the caller was willing to wait."""


class RateLimiter:
    """At most one request per ``interval`` seconds, across every thread."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(
        self,
        max_wait: float = RATE_LIMIT_MAX_WAIT_SEC,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> None:
        """Block until this caller's turn. Raises RateLimited, reserving
        nothing, when that turn is more than ``max_wait`` seconds away, and
        when ``cancelled`` says so while waiting (the turn is then spent)."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next)
            if slot - now > max_wait:
                raise RateLimited(f"next turn in {slot - now:.1f}s")
            self._next = slot + self.interval
        # Slept in short steps so a cancelled job stops waiting promptly.
        while True:
            remaining = slot - self._clock()
            if remaining <= 0:
                return
            if cancelled():
                raise RateLimited("cancelled while waiting")
            self._sleep(min(remaining, 0.25))

    def reset(self) -> None:
        """Forget every reserved turn. For tests."""
        with self._lock:
            self._next = 0.0


MUSICBRAINZ = RateLimiter(MUSICBRAINZ_MIN_INTERVAL_SEC)
ACOUSTID = RateLimiter(ACOUSTID_MIN_INTERVAL_SEC)
# Discogs allows 60 requests a minute with a token (app/pipeline/discogs.py).
DISCOGS = RateLimiter(DISCOGS_MIN_INTERVAL_SEC)
