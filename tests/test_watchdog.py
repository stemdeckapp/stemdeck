"""Tests for the desktop parent watchdog's shutdown path (#282)."""

from __future__ import annotations

import signal

import pytest

from app import main as main_mod


@pytest.mark.asyncio
async def test_watchdog_raises_sigterm_in_process(monkeypatch):
    """When the parent dies, the watchdog must raise SIGTERM in-process
    (uvicorn's handler runs) -- NOT os.kill, which on Windows is
    TerminateProcess and bypasses the shutdown sequence."""
    raised: list[int] = []
    monkeypatch.setattr(main_mod, "_process_exists", lambda _pid: False)
    monkeypatch.setattr(main_mod.signal, "raise_signal", raised.append)

    killed: list = []
    monkeypatch.setattr(main_mod.os, "kill", lambda *a: killed.append(a))

    await main_mod._desktop_parent_watchdog(12345)

    assert raised == [signal.SIGTERM]
    assert killed == []  # the hard-kill path must be gone


@pytest.mark.asyncio
async def test_watchdog_keeps_waiting_while_parent_alive(monkeypatch):
    """While the parent lives, the watchdog sleeps and loops -- no signal."""
    checks: list[int] = []

    def alive(pid: int) -> bool:
        checks.append(pid)
        return len(checks) < 3  # alive twice, then gone

    async def instant_sleep(_delay):
        return None

    raised: list[int] = []
    monkeypatch.setattr(main_mod, "_process_exists", alive)
    monkeypatch.setattr(main_mod.asyncio, "sleep", instant_sleep)
    monkeypatch.setattr(main_mod.signal, "raise_signal", raised.append)

    await main_mod._desktop_parent_watchdog(999)

    assert len(checks) == 3
    assert raised == [signal.SIGTERM]
