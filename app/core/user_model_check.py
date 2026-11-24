"""The server's side of a user model's test run (My models).

run_check() starts app/pipeline/user_model_check_worker.py for one model
folder and reads its stdout, the way the speed test reads its device
workers (app/core/speed_test.run_device): the same marks, the same tree
stop (speed_test.signal_tree, stop_process) and the same watchdog. It is
ended on cancel, after config.TIMEOUT_USER_MODEL_CHECK seconds in all and
after config.TIMEOUT_USER_MODEL_CHECK_STALL seconds with no line: asked to
stop, then killed with every process it started, then always reaped.

The worker's answer is checked again here (clean_result): only the fields
and the codes the protocol has survive. Its other output, exceptions
included, is kept as a 40-line tail for the log and never reaches the API.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core import config
from app.core.speed_test import KILL_WAIT_SEC, signal_tree, stop_process
from app.core.user_model_format import REASONS

logger = logging.getLogger("stemdeck.user_model_check")

# The worker's marks (app/pipeline/user_model_check_worker.py imports them).
STEP_MARK = "@@STEP@@"
PCT_MARK = "@@PCT@@"
RESULT_MARK = "@@RESULT@@"
STEPS = ("read", "build", "run")
MAX_LINE_CHARS = 64 * 1024
_TAIL_LINES = 40
# How long a killed worker gets to be reaped.
_REAP_WAIT_SEC = 10


@dataclass
class CheckRun:
    """What the test run answered: its result when it passed, or its code."""

    result: dict | None
    code: str | None
    tail: list[str]


def parse_line(line: str) -> tuple[str, object] | None:
    """One stdout line as ("step", name), ("pct", 0..100) or ("result",
    dict); None for anything else. Never raises."""
    if len(line) > MAX_LINE_CHARS:
        return None
    line = line.strip()
    if line.startswith(STEP_MARK):
        step = line[len(STEP_MARK) :]
        return ("step", step) if step in STEPS else None
    if line.startswith(PCT_MARK):
        raw = line[len(PCT_MARK) :]
        if not (raw.isascii() and raw.isdecimal()) or len(raw) > 3:
            return None
        return ("pct", max(0, min(100, int(raw))))
    if line.startswith(RESULT_MARK):
        try:
            value = json.loads(line[len(RESULT_MARK) :])
        except (ValueError, RecursionError):
            return None
        return ("result", value) if isinstance(value, dict) else None
    return None


def _short(value: object, limit: int = 64) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value if value.isprintable() else None


def clean_result(raw: object) -> tuple[dict | None, str]:
    """(result, "") for a passing run, (None, code) otherwise. A passing
    result is {"params", "cpu_rtf", "torch", "separator", "keys"}."""
    if not isinstance(raw, Mapping):
        return None, "load_failed"
    if raw.get("ok") is not True:
        code = raw.get("code")
        return None, code if isinstance(code, str) and code in REASONS else "load_failed"
    params = raw.get("params")
    keys = raw.get("keys")
    rtf = raw.get("cpu_rtf")
    if (
        not isinstance(params, int)
        or isinstance(params, bool)
        or not 0 < params <= config.MAX_USER_MODEL_PARAMS
    ):
        return None, "load_failed"
    if not isinstance(keys, int) or isinstance(keys, bool) or not 0 < keys <= 1_000_000:
        return None, "load_failed"
    if rtf is not None and (
        isinstance(rtf, bool)
        or not isinstance(rtf, (int, float))
        or not math.isfinite(rtf)
        or not 0 < rtf < 10_000
    ):
        rtf = None
    return {
        "params": params,
        "cpu_rtf": float(rtf) if rtf is not None else None,
        "torch": _short(raw.get("torch")),
        "separator": _short(raw.get("separator")),
        "keys": keys,
    }, ""


def worker_cmd(folder: Path, stems: list[str]) -> list[str]:
    """The worker invocation. A seam, so tests run a stub."""
    return [
        sys.executable,
        "-m",
        "app.pipeline.user_model_check_worker",
        str(folder),
        ",".join(stems),
    ]


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    # The worker exits when this process is gone (app/core/process.py).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    # The test run is on the CPU: a GPU driver fault must not end it, and
    # the separation worker may hold the GPU. "-1" is the value that hides
    # every device. An empty value is not: torch can then still report a
    # GPU and fail with "Invalid device id" while a model is built.
    env["CUDA_VISIBLE_DEVICES"] = "-1"
    return env


def run_check(
    folder: Path,
    stems: list[str],
    *,
    on_step: Callable[[str], None],
    on_pct: Callable[[int], None],
    cancel: threading.Event,
    register: Callable[[subprocess.Popen | None], None],
    timeout: float | None = None,
    stall: float | None = None,
    grace: float | None = None,
) -> CheckRun:
    """Run the test of the model in ``folder``, blocking. Code "interrupted"
    when cancelled, "timeout" when it ran too long or went silent."""
    timeout = float(config.TIMEOUT_USER_MODEL_CHECK) if timeout is None else timeout
    stall = float(config.TIMEOUT_USER_MODEL_CHECK_STALL) if stall is None else stall
    grace = KILL_WAIT_SEC if grace is None else grace
    proc = subprocess.Popen(  # noqa: S603 -- fixed args: our interpreter, module and folder
        worker_cmd(folder, stems),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_worker_env(),
        start_new_session=os.name != "nt",
    )
    register(proc)
    started = time.monotonic()
    last_line = [started]
    ended_by: list[str] = []
    done = threading.Event()

    def watchdog() -> None:
        while not done.wait(timeout=0.25):
            now = time.monotonic()
            if cancel.is_set():
                why = "cancelled"
            elif now - started > timeout:
                why = "timeout"
            elif now - last_line[0] > stall:
                why = "stalled"
            else:
                continue
            ended_by.append(why)
            if why != "cancelled":
                logger.warning("my models: the test run %s; stopping it", why)
            signal_tree(proc, hard=False)
            if not done.wait(timeout=grace):
                signal_tree(proc, hard=True)
                if not done.wait(timeout=_REAP_WAIT_SEC):
                    logger.error("my models: the test run's output never ended")
            return

    guard = threading.Thread(target=watchdog, name="user-model-check", daemon=True)
    guard.start()
    result: object = None
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            last_line[0] = time.monotonic()
            parsed = parse_line(line)
            if parsed is None:
                text = line.strip()
                if text:
                    tail.append(text[:500])
                    del tail[:-_TAIL_LINES]
                continue
            kind, value = parsed
            if kind == "step":
                on_step(str(value))
            elif kind == "pct":
                on_pct(int(value))  # type: ignore[call-overload]
            else:
                result = value
    finally:
        done.set()
        stop_process(proc)
        guard.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        register(None)

    if ended_by and ended_by[0] == "cancelled":
        return CheckRun(None, "interrupted", tail)
    if ended_by:
        return CheckRun(None, "timeout", tail)
    if result is None:
        return CheckRun(None, "load_failed", tail)
    cleaned, code = clean_result(result)
    return CheckRun(cleaned, code or None, tail)
