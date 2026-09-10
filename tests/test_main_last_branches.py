"""The three branches in app/main.py only a desktop or portable build reaches.

A source checkout runs one plain HTTP listener, imports librosa successfully,
and never has a cookies file to reject -- so all three of these are invisible
in development and each one breaks a shipped build if it regresses.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from app.core.registry import _jobs


def _idle_task():
    """start_worker() hands back a task the lifespan registers callbacks on."""
    import asyncio

    async def _idle():
        return None

    return asyncio.ensure_future(_idle())


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


# --------------------------------------------------------------------------
# the warm-up import
# --------------------------------------------------------------------------


def test_the_server_still_boots_without_librosa():
    """The import is a warm-up, not a dependency: it buys a snappier first job
    by paying numpy/scipy/numba's lazy initialization at boot. A minimal Docker
    image without it must still start -- analyze() degrades on its own -- and
    an ImportError here would take the whole backend down instead."""
    import app.main as main

    real_import = builtins.__import__

    def _no_librosa(name, *args, **kwargs):
        if name == "librosa":
            raise ImportError("no librosa in this image")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("librosa", None)
    builtins.__import__ = _no_librosa
    try:
        reloaded = importlib.reload(main)
        assert reloaded.app is not None, "the application was never built"
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["librosa"] = saved
        # Re-import with librosa available so the rest of the suite gets the
        # module in the state it found it.
        importlib.reload(main)


# --------------------------------------------------------------------------
# the second listener
# --------------------------------------------------------------------------


async def test_the_https_listener_is_started_when_the_shell_supplied_a_certificate(
    monkeypatch, tmp_path
):
    """The desktop shell runs two listeners: loopback HTTP for its own webview
    and HTTPS on the LAN for phones, which will not grant microphone or
    clipboard access to an insecure origin. Server mode sets none of these and
    keeps its single listener."""
    import app.main as main
    from app.pipeline import jobqueue as jq

    started: list[dict] = []

    async def _start(app, *, port, certfile, keyfile):
        started.append({"port": port, "certfile": certfile, "keyfile": keyfile})

    async def _stop():
        return None

    monkeypatch.setattr(main, "HTTPS_PORT", 8443)
    monkeypatch.setattr(main, "SSL_CERTFILE", str(tmp_path / "cert.pem"))
    monkeypatch.setattr(main, "SSL_KEYFILE", str(tmp_path / "key.pem"))
    monkeypatch.setattr(main.tls_listener, "start", _start)
    monkeypatch.setattr(main.tls_listener, "stop", _stop)
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "take_pending_resume", lambda: [])
    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", lambda d: None)
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    async with main.lifespan(main.app):
        pass

    assert started == [
        {
            "port": 8443,
            "certfile": str(tmp_path / "cert.pem"),
            "keyfile": str(tmp_path / "key.pem"),
        }
    ]


# --------------------------------------------------------------------------
# the cookies setting
# --------------------------------------------------------------------------


def test_a_cookies_path_the_filesystem_rejects_is_a_422_not_a_500(monkeypatch):
    """set_cookies_file raises ValueError for the cases it can name ("not
    found", "not readable"). Everything else -- a path that is not a string, a
    directory the OS refuses to stat, a dead network mount -- reaches here, and
    the settings dialog needs a message it can show rather than a 500 that
    leaves the field looking accepted."""
    import app.main as main

    def _unreadable(_value):
        raise OSError("host is down")

    monkeypatch.setattr(main, "set_cookies_file", _unreadable)

    with TestClient(main.app) as client:
        r = client.post("/api/settings", json={"cookies_file": "//nas/share/cookies.txt"})

    assert r.status_code == 422
    assert r.json()["detail"] == "invalid cookies file"
