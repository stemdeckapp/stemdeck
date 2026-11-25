"""The hardware speed test's routes (Settings, Separation, "Run benchmark").

  POST /api/speed-test         start it: 202 {"state": "running"}, or 409
                               {"detail", "code": "busy"} while anything runs
  POST /api/speed-test/cancel  stop it: 200 with the state below
  GET  /api/speed-test         {"state", "step", "pct", "error", "error_code",
                               "result"}

One test at a time, and only while StemDeck is idle: no separation running,
none waiting in a queue that is not paused, nothing holding the pipeline lock
(a stem fill, a vocal split), no test already running, the server not
stopping and no library move in progress. The run then holds the pipeline lock itself, so a song
imported meanwhile waits for the test rather than both running at half speed.

A run: the disk write test in the jobs folder, then each device the app can
use in its own subprocess (app/core/speed_test.run_device). A device that fails
is skipped and reported; the run fails only when no device could be measured.
A finished run's result replaces the saved one (settings.speed_test). Cancel
ends the subprocess and the disk test, whose temporary file is removed in its
own ``finally``. Errors are fixed sentences: never a path or an exception.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import config, settings, stem_models
from app.core import speed_test as st
from app.core.stems_location import is_relocating
from app.pipeline import jobqueue

logger = logging.getLogger("stemdeck.speed_test")

router = APIRouter(tags=["settings"])

BUSY_DETAIL = (
    "The benchmark runs only while StemDeck is idle: no song separating or waiting, "
    "and no other benchmark running. Try again when it has finished."
)

# How long POST /cancel waits for the run to settle, so its answer already
# says "cancelled". The subprocess is reaped within about 15 s at worst.
_CANCEL_SETTLE_SEC = 20.0

# The share of the progress bar the disk test takes; the devices share the rest.
_DISK_PCT = 10

_lock = threading.Lock()
_IDLE: dict[str, object] = {
    "state": "idle",
    "step": None,
    "pct": 0,
    "error": None,
    "error_code": None,
}
_state: dict[str, object] = dict(_IDLE)
_cancel = threading.Event()
_proc: list[subprocess.Popen] = []
_task: asyncio.Task | None = None


def _update(**fields: object) -> None:
    with _lock:
        _state.update(fields)


def _register(proc: subprocess.Popen | None) -> None:
    with _lock:
        _proc.clear()
        if proc is not None:
            _proc.append(proc)
    # A cancel that landed while the process was starting found nothing to
    # stop; the watchdog in run_device sees the flag within half a second.


def snapshot() -> dict[str, object]:
    """The state GET /api/speed-test answers, with the saved result."""
    with _lock:
        state = dict(_state)
    try:
        state["result"] = settings.get_speed_test_public()
    except Exception:  # noqa: BLE001 -- the state must still answer
        logger.warning("speed test: could not read the saved result", exc_info=True)
        state["result"] = None
    return state


def _busy() -> bool:
    """A separation running, one waiting in a queue that is not paused, the
    pipeline lock held, a test running, the server stopping, or a library
    move in progress (the disk test writes into the folder being moved).

    A paused queue is idle, as for the idle unload (idle_unload._pipeline_busy):
    jobs restored at startup wait for Start, possibly for days. A job started
    during the test waits for the pipeline lock the test holds."""
    from app.pipeline.runner import _pipeline_lock

    running, waiting = jobqueue.snapshot()
    queued = bool(waiting) and not jobqueue.is_paused()
    with _lock:
        testing = _state["state"] == "running"
    return (
        testing
        or running is not None
        or queued
        or _pipeline_lock.locked()
        or jobqueue.is_stopping()
        or is_relocating()
    )


def _kim_devices(devices: list[str]) -> set[str]:
    """The devices Kim may run on here: the static rule's answer."""
    return {
        d
        for d in devices
        if stem_models.recommended("vocals", d, settings.stem_model_vram_mb(d)) == st.KIM
    }


def _fail(code: str, text: str) -> None:
    _update(state="failed", step=None, error=text, error_code=code)


def run_test(cancel: threading.Event) -> None:
    """One whole run, blocking: called in a thread. Sets the final state."""
    try:
        _update(step="disk", pct=0)
        # A file a killed run left: never one a run still writes (age rule).
        st.sweep_stale_temp_files(config.JOBS_DIR)
        disk = st.measure_disk_bounded(config.JOBS_DIR, cancel)
        if cancel.is_set():
            _update(state="cancelled", step=None)
            return
        _update(pct=_DISK_PCT)

        devices = st.measure_devices(config.available_torch_devices())
        measured: dict[str, dict] = {}
        failed: dict[str, str] = {}
        torch_version: str | None = None
        span = (100 - _DISK_PCT) / len(devices)
        for index, device in enumerate(devices):
            base = _DISK_PCT + span * index
            _update(step=device, pct=int(base))
            outcome = st.run_device(
                device,
                on_step=lambda step: _update(step=step),
                on_pct=lambda pct, base=base: _update(pct=int(base + span * pct / 100)),
                cancel=cancel,
                register=_register,
            )
            if cancel.is_set():
                _update(state="cancelled", step=None)
                return
            torch_version = torch_version or outcome.torch
            if outcome.measured is not None:
                measured[device] = outcome.measured
                if outcome.partial:
                    logger.warning(
                        "speed test: %s ended early; its other models are estimated", device
                    )
            else:
                failed[device] = outcome.error or "failed"
                logger.warning(
                    "speed test: %s could not be measured (%s); tail:\n%s",
                    device,
                    failed[device],
                    "\n".join(outcome.tail[-15:]) or "(no output)",
                )

        if not measured:
            _fail(*st.run_error(list(failed.values())))
            return
        result = st.build_result(
            hw=st.current_hw(torch_version),
            disk=disk,
            measured=measured,
            failed=failed,
            kim_devices=_kim_devices(list(measured)),
        )
        settings.set_speed_test(result)
        logger.info(
            "speed test: %s",
            ", ".join(f"{d} 6s rtf {measured[d][st.MEASURED_MODEL]['rtf']:.3f}" for d in measured),
        )
        _update(state="done", step=None, pct=100)
    except Exception:
        logger.exception("speed test failed")
        _fail("failed", st.ERROR_FAILED)


async def _run(cancel: threading.Event) -> None:
    from app.pipeline.runner import _pipeline_lock

    try:
        await asyncio.to_thread(run_test, cancel)
    finally:
        _pipeline_lock.release()
        with _lock:
            if _state["state"] == "running":
                # run_test always settles the state; this is the backstop.
                _state.update(state="failed", step=None, error=st.ERROR_FAILED, error_code="failed")


def _task_done(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("speed test task raised", exc_info=task.exception())


@router.post("/speed-test", status_code=202)
async def start_speed_test() -> JSONResponse:
    global _cancel, _task
    from app.pipeline.runner import _pipeline_lock

    if _busy():
        return JSONResponse({"detail": BUSY_DETAIL, "code": "busy"}, status_code=409)
    # No await between the check above and these two lines: on the event loop
    # nothing else can take the lock or start a test in between, and an
    # unlocked semaphore is acquired without suspending.
    cancel = threading.Event()
    with _lock:
        _cancel = cancel
        _state.update(state="running", step=None, pct=0, error=None, error_code=None)
    await _pipeline_lock.acquire()
    _task = asyncio.create_task(_run(cancel))
    _task.add_done_callback(_task_done)
    return JSONResponse({"state": "running"}, status_code=202)


@router.post("/speed-test/cancel")
async def cancel_speed_test() -> dict[str, object]:
    cancel_running()
    deadline = time.monotonic() + _CANCEL_SETTLE_SEC
    while time.monotonic() < deadline:
        with _lock:
            if _state["state"] != "running":
                break
        await asyncio.sleep(0.05)
    # Off the loop: the saved result's staleness check runs the device probe.
    return await asyncio.to_thread(snapshot)


@router.get("/speed-test")
def get_speed_test() -> dict[str, object]:
    return snapshot()


def is_running() -> bool:
    """Whether a test is running now (a library move waits for it)."""
    with _lock:
        return _state["state"] == "running"


def cancel_running() -> None:
    """Stop a running test: the flag ends the disk test and the device loop,
    and the subprocess is terminated now. Also called at server shutdown."""
    with _lock:
        if _state["state"] != "running":
            return
        _cancel.set()
        procs = list(_proc)
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass


def reset_state() -> None:
    """Back to idle, for tests."""
    global _cancel, _task
    with _lock:
        _state.update(_IDLE)
        _proc.clear()
        _cancel = threading.Event()
        _task = None
