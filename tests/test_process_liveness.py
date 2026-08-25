"""Process liveness, the shared half of both parent-death watchdogs.

This decides whether the Demucs worker shoots itself when its parent is gone.
Getting it wrong in one direction orphans a process holding the GPU; getting
it wrong in the other kills a worker mid-separation. Neither announces itself.

The POSIX branch is unreachable on Windows and the Windows branch is
unreachable on POSIX, so each is driven with os.name patched. That is the only
way one machine can cover both, and both ship.
"""

from __future__ import annotations

import os

import pytest

from app.core.process import process_exists


def test_this_process_is_alive():
    assert process_exists(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1, -12345])
def test_a_nonsense_pid_is_not_alive(pid):
    """Guarded before either platform branch: on POSIX, kill(0, 0) signals the
    whole process group and kill(-n, 0) a different group entirely."""
    assert process_exists(pid) is False


def test_a_dead_pid_is_not_alive():
    """A pid high enough to be unused. Both branches must agree it is gone,
    or the watchdog never fires and a worker outlives its parent."""
    assert process_exists(4_000_000) is False


# ── the POSIX branch ─────────────────────────────────────────────────


def test_posix_treats_a_permission_error_as_alive(monkeypatch):
    """The rule this module exists for: a process owned by someone else is
    still a process. A watchdog must not shoot on ambiguity."""
    monkeypatch.setattr(os, "name", "posix")

    def denied(_pid, _sig):
        raise PermissionError("not yours")

    monkeypatch.setattr(os, "kill", denied)
    assert process_exists(1234) is True


def test_posix_treats_a_lookup_error_as_dead(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def gone(_pid, _sig):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(os, "kill", gone)
    assert process_exists(1234) is False


def test_posix_signal_zero_is_a_probe_not_a_kill(monkeypatch):
    """Signal 0 asks "does this exist" without delivering anything. Sending a
    real signal here would kill the process the watchdog is checking on."""
    monkeypatch.setattr(os, "name", "posix")
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert process_exists(4321) is True
    assert sent == [(4321, 0)]


# ── the Windows branch ───────────────────────────────────────────────


class _Kernel32:
    """Stands in for kernel32, so the Windows path runs anywhere."""

    def __init__(self, handle: int, last_error: int = 0):
        self._handle = handle
        self.last_error = last_error
        self.closed: list[int] = []

    def OpenProcess(self, _access, _inherit, _pid):  # noqa: N802 - Win32 name
        return self._handle

    def CloseHandle(self, handle):  # noqa: N802 - Win32 name
        self.closed.append(handle)
        return True


def _run_windows_branch(monkeypatch, handle: int, last_error: int = 0):
    import ctypes

    monkeypatch.setattr(os, "name", "nt")
    fake = _Kernel32(handle, last_error)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: fake, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: fake.last_error, raising=False)
    return fake, process_exists(1234)


def test_windows_an_open_handle_means_alive(monkeypatch):
    fake, alive = _run_windows_branch(monkeypatch, handle=42)
    assert alive is True
    assert fake.closed == [42], "the handle leaked; this runs on a watchdog timer"


def test_windows_invalid_parameter_means_dead(monkeypatch):
    """ERROR_INVALID_PARAMETER (87) is what OpenProcess returns for a pid that
    does not exist. It is the only failure that proves absence."""
    _, alive = _run_windows_branch(monkeypatch, handle=0, last_error=87)
    assert alive is False


def test_windows_access_denied_means_alive(monkeypatch):
    """ERROR_ACCESS_DENIED (5): the process is there and owned by someone
    else. Same ambiguity rule as the POSIX branch."""
    _, alive = _run_windows_branch(monkeypatch, handle=0, last_error=5)
    assert alive is True
