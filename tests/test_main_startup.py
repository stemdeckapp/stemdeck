"""What app/main.py does when the backend starts and stops, and the two
read-only or settings endpoints whose odd inputs had no test.

The lifespan is driven directly, with the queue worker, the sweeps and the
listeners replaced by recorders: what is tested is the order and the guards
(a failing cleanup must not stop the boot, interrupted jobs come back paused,
the desktop watchdog is armed only for a real parent), not the things it starts.
"""

from __future__ import annotations

import json
import logging
import os
import signal

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.core.registry import registry_path
from app.pipeline import jobqueue


class _Task:
    def add_done_callback(self, _cb):
        pass

    def cancel(self):
        pass


@pytest.fixture
def calls(monkeypatch):
    """Replace everything the lifespan starts or stops with a recorder."""
    seen: list[tuple] = []

    async def no_sweep():
        seen.append(("sweep_loop",))

    monkeypatch.setattr(main_mod, "_sweep_loop", no_sweep)
    monkeypatch.setattr(jobqueue, "start_worker", lambda: seen.append(("worker",)) or _Task())
    monkeypatch.setattr(main_mod, "sweep_orphaned_section_workspaces", lambda d: None)
    from app.pipeline import take_render

    monkeypatch.setattr(take_render, "sweep_render_temps", lambda d: None)
    monkeypatch.setattr(main_mod, "take_pending_resume", lambda: [])
    monkeypatch.setattr(jobqueue, "pause", lambda: seen.append(("pause",)))
    monkeypatch.setattr(
        jobqueue, "enqueue", lambda job_id, **kw: seen.append(("enqueue", job_id, kw))
    )
    monkeypatch.setattr(jobqueue, "request_stop", lambda: seen.append(("request_stop",)))
    from app.api import takes

    monkeypatch.setattr(takes, "cancel_all_renders", lambda: seen.append(("cancel_renders",)))
    from app.pipeline import separate

    monkeypatch.setattr(separate, "_kill_worker", lambda: seen.append(("kill_demucs",)))
    from app.core import tls_listener

    async def tls_start(app, *, port, certfile, keyfile):
        seen.append(("tls_start", port, certfile, keyfile))
        return True

    async def tls_stop():
        seen.append(("tls_stop",))

    monkeypatch.setattr(tls_listener, "start", tls_start)
    monkeypatch.setattr(tls_listener, "stop", tls_stop)

    async def watchdog(pid):
        seen.append(("watchdog", pid))

    monkeypatch.setattr(main_mod, "_desktop_parent_watchdog", watchdog)
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.delenv("STEMDECK_PARENT_PID", raising=False)
    monkeypatch.setattr(main_mod, "HTTPS_PORT", None)
    return seen


async def _run_lifespan() -> None:
    import asyncio

    async with main_mod.lifespan(main_mod.app):
        await asyncio.sleep(0)  # let the tasks it created take their first step


async def test_startup_and_shutdown_in_order(calls):
    await _run_lifespan()

    names = [c[0] for c in calls]
    assert "worker" in names and "sweep_loop" in names
    # Shutdown: stop taking work, stop renders, then the demucs worker.
    tail = names[names.index("tls_stop") :]
    assert tail == ["tls_stop", "request_stop", "cancel_renders", "kill_demucs"]
    assert "pause" not in names, "nothing was interrupted, so nothing is paused"


async def test_interrupted_jobs_come_back_paused_and_in_order(calls, monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "take_pending_resume", lambda: ["aaaaaaaaaaa1", "aaaaaaaaaaa2"])

    with caplog.at_level(logging.INFO, logger="stemdeck"):
        await _run_lifespan()

    pause = calls.index(("pause",))
    enq = [c for c in calls if c[0] == "enqueue"]
    assert [c[1] for c in enq] == ["aaaaaaaaaaa1", "aaaaaaaaaaa2"]
    assert all(c[2] == {"autostart": False} for c in enq)
    assert pause < calls.index(enq[0]), "paused before anything is queued"
    assert "restored 2 interrupted job(s)" in caplog.text


async def test_a_failing_cleanup_does_not_stop_the_boot(calls, monkeypatch, caplog):
    def boom(_d):
        raise OSError("disk went away")

    monkeypatch.setattr(main_mod, "sweep_orphaned_section_workspaces", boom)
    from app.pipeline import take_render

    monkeypatch.setattr(take_render, "sweep_render_temps", boom)

    with caplog.at_level(logging.ERROR, logger="stemdeck"):
        await _run_lifespan()

    assert "could not sweep orphaned section workspaces" in caplog.text
    assert "could not sweep stale render files" in caplog.text
    assert ("kill_demucs",) in calls, "the app started and shut down normally"


@pytest.mark.parametrize(
    ("pid", "armed"),
    [
        ("not-a-pid", False),
        ("0", False),
        ("-5", False),
        (str(os.getpid()), False),  # its own pid: watching itself would never fire
        ("424242", True),
    ],
)
async def test_the_desktop_watchdog_is_armed_only_for_a_real_parent(calls, monkeypatch, pid, armed):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setenv("STEMDECK_PARENT_PID", pid)

    await _run_lifespan()

    watched = [c for c in calls if c[0] == "watchdog"]
    assert watched == ([("watchdog", int(pid))] if armed else [])


async def test_the_watchdog_needs_the_desktop_shell(calls, monkeypatch):
    monkeypatch.setenv("STEMDECK_PARENT_PID", "424242")  # no STEMDECK_DESKTOP

    await _run_lifespan()

    assert not any(c[0] == "watchdog" for c in calls)


async def test_an_invalid_parent_pid_is_logged(calls, monkeypatch, caplog):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setenv("STEMDECK_PARENT_PID", "12ab")

    with caplog.at_level(logging.WARNING, logger="stemdeck"):
        await _run_lifespan()

    assert "invalid STEMDECK_PARENT_PID='12ab'" in caplog.text


async def test_the_lan_listener_starts_only_with_a_port_and_a_certificate(
    calls, monkeypatch, tmp_path
):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    monkeypatch.setattr(main_mod, "HTTPS_PORT", 8443)
    monkeypatch.setattr(main_mod, "SSL_CERTFILE", cert)
    monkeypatch.setattr(main_mod, "SSL_KEYFILE", None)
    await _run_lifespan()
    assert not any(c[0] == "tls_start" for c in calls)

    calls.clear()
    monkeypatch.setattr(main_mod, "SSL_KEYFILE", key)
    await _run_lifespan()
    assert ("tls_start", 8443, cert, key) in calls


async def test_the_parent_watchdog_raises_sigterm_when_the_parent_is_gone(monkeypatch):
    raised: list[int] = []
    alive = iter([True, False])
    monkeypatch.setattr(main_mod, "_process_exists", lambda pid: next(alive))
    monkeypatch.setattr(main_mod.signal, "raise_signal", raised.append)

    async def no_wait(_s):
        return None

    monkeypatch.setattr(main_mod.asyncio, "sleep", no_wait)

    await main_mod._desktop_parent_watchdog(424242)

    assert raised == [signal.SIGTERM]


async def test_a_failing_sweep_is_logged_and_the_loop_goes_on(monkeypatch, caplog):
    def broken():
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(main_mod, "get_auto_delete_jobs", broken)

    class Stop(Exception):
        pass

    async def stop(_delay):
        raise Stop

    monkeypatch.setattr(main_mod.asyncio, "sleep", stop)

    with caplog.at_level(logging.WARNING, logger="stemdeck"), pytest.raises(Stop):
        await main_mod._sweep_loop()

    assert "sweep failed" in caplog.text


# --- GET /api/registry ----------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(main_mod.app) as c:
        yield c


def test_registry_view_without_a_file_is_an_empty_registry(client):
    path = registry_path(main_mod.JOBS_DIR)
    path.unlink(missing_ok=True)

    r = client.get("/api/registry")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.text) == {"version": 1, "jobs": []}


def test_registry_view_is_the_file_byte_for_byte(client):
    path = registry_path(main_mod.JOBS_DIR)
    raw = '{\n  "version": 1,\n  "jobs": [{"id": "aaaaaaaaaaaa", "title": "Ação"}]\n}\n'
    path.write_text(raw, encoding="utf-8")

    r = client.get("/api/registry")

    assert r.status_code == 200
    assert r.text == raw


# --- POST /api/settings with a body that is JSON but not an object ------------------


@pytest.mark.parametrize("body", [b'"allow_network"', b"[]", b"7", b"not json"])
def test_settings_with_a_body_that_is_not_an_object_change_nothing(client, body):
    before = client.get("/api/settings").json()

    r = client.post("/api/settings", content=body, headers={"content-type": "application/json"})

    assert r.status_code == 200, r.text
    after = client.get("/api/settings").json()
    assert after == before
