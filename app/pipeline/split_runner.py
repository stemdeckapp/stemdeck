"""Shared supervision for the on-demand vocal-split worker subprocesses.

Both the lead/backing split (#275) and the duet split run the same shape of
job: a fresh, non-persistent subprocess that loads one `audio-separator` model,
writes a fixed set of wav files next to the job's stems, and reports over the
stderr line protocol that `demucs_worker.py` established. Only the argv, the
timeout and the expected output names differ, so the process lifecycle,
stall watchdog and error hygiene live here once rather than per split.

Deliberately not a persistent worker: these are occasional, user-triggered
actions rather than the hot path every job takes, so there is no repeat
model-load cost worth amortising (see ml-pipeline.md / #309 for why that
reasoning applies to Demucs and not here).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from app.core.models import Job
from app.core.registry import set_proc
from app.pipeline.errors import SeparationError

logger = logging.getLogger("stemdeck.pipeline")

_WATCHDOG_POLL_SECONDS = 30
_TAIL_LIMIT = 40


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # Pin the child's stdio encoding to match what the parent decodes with.
    # Without it a Windows child writes cp1252 while the parent reads utf-8, so
    # the mismatch simply moves rather than being fixed. audio-separator emits
    # progress bars and can echo track metadata, neither of which is
    # guaranteed to be cp1252-safe.
    env["PYTHONIOENCODING"] = "utf-8:replace"
    # The worker arms a watchdog on this and hard-exits when we disappear, so a
    # kill that runs no cleanup (SIGKILL, Force Quit, Task Manager, a crash)
    # cannot leave it running with nobody to collect the result (#519).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass
    return env


def run_split(
    job: Job,
    *,
    argv: list[str],
    device: str,
    timeout: int,
    label: str,
    produces: list[str],
    out_dir: Path,
) -> list[str]:
    """Run a split worker to completion and return `produces` on success.

    Raises SeparationError (carrying the stderr tail) if the worker fails,
    stalls past `timeout` with no output, or exits without writing every file
    in `produces`. Never mutates anything outside `out_dir`.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # utf-8/replace explicitly. text=True alone decodes with the locale
        # encoding, which on Windows is cp1252, and a single byte outside it
        # in a child's output kills the whole job with a UnicodeDecodeError
        # ("'charmap' codec can't decode byte 0x8f"). This is diagnostic text:
        # a byte we cannot read is never a reason to fail a separation.
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_child_env(),
    )
    set_proc(job.id, proc)

    tail: list[str] = []
    last_output = [time.monotonic()]
    done_evt = threading.Event()

    def _watchdog() -> None:
        while not done_evt.wait(timeout=_WATCHDOG_POLL_SECONDS):
            if proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > timeout:
                logger.warning(
                    "%s stalled for %ss with no output, terminating job %s",
                    label,
                    timeout,
                    job.id,
                )
                proc.terminate()
                return

    wt = threading.Thread(target=_watchdog, daemon=True)
    wt.start()
    job_ok = False
    try:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            last_output[0] = time.monotonic()
            line = raw_line.strip()
            if not line:
                continue
            if line == "@@DONE@@":
                job_ok = True
                break
            if line.startswith("@@ERROR@@"):
                msg = line[len("@@ERROR@@") :]
                try:
                    msg = json.loads(msg)
                except json.JSONDecodeError:
                    pass
                tail.append(str(msg))
                break
            tail.append(line)
            if len(tail) > _TAIL_LIMIT:
                tail.pop(0)
    finally:
        done_evt.set()
        set_proc(job.id, None)
        wt.join(timeout=2)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

    if not job_ok:
        detail = "\n".join(tail[-15:]) if tail else "(no stderr captured)"
        logger.warning("[%s] %s failed: %s", job.id, label, detail)
        last = tail[-1] if tail else f"{label} failed"
        raise SeparationError(f"{label} failed: {last}", tail=tail[-_TAIL_LIMIT:], device=device)

    for name in produces:
        if not (out_dir / f"{name}.wav").is_file():
            raise SeparationError(f"{label} did not produce {name}.wav", device=device)
    return list(produces)
