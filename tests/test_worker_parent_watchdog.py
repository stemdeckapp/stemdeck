"""The separation worker must not outlive whoever spawned it.

Closing StemDeck has to leave nothing behind. The worker holds the GPU and is
the one child that can run for minutes, so it cannot depend on the parent
getting a chance to clean up: Force Quit, Task Manager and a crash all skip
that entirely.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from app.core.process import process_exists


def test_process_exists_reports_a_live_process():
    assert process_exists(os.getpid()) is True


def test_process_exists_reports_a_dead_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    # A pid can be recycled, but not within the moment after a wait().
    assert process_exists(proc.pid) is False


def test_process_exists_rejects_nonsense_pids():
    assert process_exists(0) is False
    assert process_exists(-1) is False


def test_worker_exits_when_its_parent_disappears(tmp_path):
    """The watchdog in isolation: same code path, without torch in the way.

    Spawning the real worker would load demucs (seconds, and a model download on
    a clean machine), so this drives _arm_parent_watchdog directly with a stand
    -in parent it can kill.
    """
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])

    script = textwrap.dedent(
        """
        import sys, time
        from app.pipeline.demucs_worker import _arm_parent_watchdog
        _arm_parent_watchdog()
        # Busy the way a separation is busy: never reading stdin, so only the
        # watchdog can end this process.
        while True:
            time.sleep(0.1)
        """
    )
    env = {**os.environ, "STEMDECK_PARENT_PID": str(parent.pid)}
    worker = subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        cwd=os.getcwd(),
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        assert worker.poll() is None, "worker should still be running while the parent lives"

        parent.kill()
        parent.wait()

        deadline = time.time() + 20
        while time.time() < deadline:
            if worker.poll() is not None:
                break
            time.sleep(0.25)
        assert worker.poll() is not None, "worker outlived its parent"
    finally:
        if worker.poll() is None:
            worker.kill()
        if parent.poll() is None:
            parent.kill()


def test_worker_ignores_an_unset_or_bogus_parent_pid(monkeypatch):
    """A worker run by hand (no STEMDECK_PARENT_PID) must not arm the watchdog
    and shoot itself."""
    from app.pipeline import demucs_worker

    started: list[object] = []
    monkeypatch.setattr(
        demucs_worker.threading,
        "Thread",
        lambda *a, **k: started.append((a, k)) or _NoopThread(),
    )

    for value in ("", "   ", "not-a-number", "0", "-5", str(os.getpid())):
        monkeypatch.setenv("STEMDECK_PARENT_PID", value)
        demucs_worker._arm_parent_watchdog()
    assert started == [], "watchdog armed on a pid it should have ignored"


class _NoopThread:
    def start(self):
        pass


def test_the_worker_is_spawned_with_the_parent_pid(monkeypatch):
    """The watchdog is only armed if separate.py actually passes the pid."""
    import app.pipeline.separate as separate

    captured: dict = {}

    class _FakeProc:
        stdin = None
        stderr = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(separate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(separate, "_kill_worker", lambda: None)
    separate._worker.clear()
    try:
        separate._get_worker("cpu")
    finally:
        separate._worker.clear()

    assert captured["env"]["STEMDECK_PARENT_PID"] == str(os.getpid())
