"""The AcoustID key check when AcoustID does not answer (POST /api/settings,
identify.acoustid_key_works).

A key is tried once before it is saved, so that one AcoustID refuses is not
kept to fail every import without a word. When AcoustID cannot be asked
(offline, down, slow, too busy to wait for), the key is saved on trust, and
the Settings request is not held past a bound: the Save button waits on it.

The other AcoustID key tests (tests/test_identify_api.py) stub the whole
request. These keep the real one and stand in only for the socket: urlopen
itself, or a server on loopback that accepts and never answers. Nothing
reaches api.acoustid.org.
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.pipeline.identify as ident
from app.core import settings as _settings

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_ACOUSTID_REQUEST = ident._acoustid_request

KEY = "Zx9Yw8Vu7T"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ident, "_acoustid_request", _REAL_ACOUSTID_REQUEST)
    ident.ratelimit.ACOUSTID.reset()
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c
    ident.ratelimit.ACOUSTID.reset()


def _save(client):
    started = time.monotonic()
    r = client.post("/api/settings", json={"acoustid_api_key": KEY})
    return r, time.monotonic() - started


def test_acoustid_timing_out_saves_the_key_on_trust(client, monkeypatch):
    asked = []

    def urlopen(request, timeout, context):
        asked.append(timeout)
        raise TimeoutError("timed out")

    monkeypatch.setattr(ident.urllib.request, "urlopen", urlopen)
    r, _ = _save(client)
    assert r.status_code == 200
    assert _settings.get_acoustid_api_key() == KEY
    assert asked == [ident.TIMEOUT_IDENTIFY_REQUEST], "asked once, with the request timeout"


def test_acoustid_unreachable_saves_the_key_on_trust(client, monkeypatch):
    def urlopen(request, timeout, context):
        raise urllib.error.URLError(OSError("connection refused"))

    monkeypatch.setattr(ident.urllib.request, "urlopen", urlopen)
    r, _ = _save(client)
    assert r.status_code == 200 and _settings.get_acoustid_api_key() == KEY


def test_acoustid_down_saves_the_key_on_trust(client, monkeypatch):
    """A 503 with an HTML page is AcoustID down, not the key refused."""

    def urlopen(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url, 503, "Service Unavailable", {}, __import__("io").BytesIO(b"<html>")
        )

    monkeypatch.setattr(ident.urllib.request, "urlopen", urlopen)
    assert ident.acoustid_key_works(KEY) is None, "unknown, not a verdict on the key"
    r, _ = _save(client)
    assert r.status_code == 200 and _settings.get_acoustid_api_key() == KEY


def test_a_rate_limit_queue_too_long_saves_the_key_without_asking(client, monkeypatch):
    """Imports running flat out hold the AcoustID turns: the check does not
    wait behind them past the limiter's bound, it saves on trust."""
    monkeypatch.setattr(ident.urllib.request, "urlopen", lambda *a, **k: pytest.fail("asked"))

    def busy(*args, **kwargs):
        raise ident.ratelimit.RateLimited("next turn in 60.0s")

    monkeypatch.setattr(ident.ratelimit.ACOUSTID, "wait", busy)
    r, _ = _save(client)
    assert r.status_code == 200 and _settings.get_acoustid_api_key() == KEY


# ── a real socket that never answers ──


class _SilentServer:
    """Accepts connections on loopback and never writes a byte, or with
    ``trickle`` writes one byte of a status line every ``trickle`` seconds,
    until closed."""

    def __init__(self, trickle: float | None = None):
        self.trickle = trickle
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.conns: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.conns.append(conn)
            if self.trickle:
                threading.Thread(target=self._drip, args=(conn,), daemon=True).start()

    def _drip(self, conn):
        line = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"x" * 10_000
        for byte in line:
            if self.stop.wait(self.trickle):
                return
            try:
                conn.sendall(bytes([byte]))
            except OSError:
                return

    def close(self):
        self.stop.set()
        for conn in self.conns:
            conn.close()
        self.sock.close()
        self.thread.join(timeout=2)


@pytest.fixture
def silent(monkeypatch):
    servers = []

    def start(trickle=None, timeout=0.5):
        server = _SilentServer(trickle)
        servers.append(server)
        monkeypatch.setattr(
            ident, "ACOUSTID_LOOKUP_URL", f"http://127.0.0.1:{server.port}/v2/lookup"
        )
        monkeypatch.setattr(ident, "TIMEOUT_IDENTIFY_REQUEST", timeout)
        return server

    yield start
    for server in servers:
        server.close()


# The request timeout, its turn under the AcoustID rate limit, and room for a
# slow test machine.
_SLACK_SEC = 3.0


def test_a_server_that_never_answers_is_given_up_on_within_the_timeout(client, silent):
    silent(timeout=0.5)
    r, took = _save(client)
    assert r.status_code == 200 and _settings.get_acoustid_api_key() == KEY
    assert took < 0.5 + _SLACK_SEC, f"the Save waited {took:.1f}s"


def test_a_server_that_drips_its_answer_is_given_up_on_within_a_bound(client, silent):
    # A byte every 0.2 s never trips a 0.5 s timeout. 10 kB of header would
    # take half an hour; a bound of a few seconds is what the Save can wait.
    silent(trickle=0.2, timeout=0.5)
    done = {}

    def save():
        done["result"] = _save(client)

    thread = threading.Thread(target=save, daemon=True)
    thread.start()
    thread.join(timeout=0.5 * 2 + _SLACK_SEC)
    assert not thread.is_alive(), "the Save was still waiting on the dripping server"
