from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import DEMUCS_MODEL, TIMEOUT_DEMUCS_STALL
from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc
from app.core.settings import get_demucs_device, get_separation_quality
from app.pipeline.errors import SeparationError, classify_failure

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")
# Terminate demucs if stderr produces no output for this many seconds.
# GPU processing can be silent for minutes; 30 min covers legitimate pauses
# while still catching genuine hangs (GPU deadlock, OOM stall, etc.).


def _demucs_cmd(device: str, source: Path, job_dir: Path) -> list[str]:
    """Build the demucs CLI invocation. Module-level seam so tests can swap
    in a stub executable without touching the process-management machinery."""
    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        DEMUCS_MODEL,
        "-d",
        device,
    ]
    # "best" quality (Settings -> General): demucs re-runs separation on a
    # randomly time-shifted copy of the input and averages the two passes --
    # measurably cleaner stems, ~2x the separation time. Applies on any
    # device; read fresh per job like get_demucs_device() below.
    if get_separation_quality() == "best":
        cmd += ["--shifts", "2"]
    cmd += ["-o", str(job_dir), str(source)]
    return cmd


def _run_demucs(job: Job, source: Path, job_dir: Path, device: str) -> tuple[int, list[str]]:
    """One demucs attempt on `device`: spawn, stream progress, watchdog stalls.

    Returns (returncode, stderr_tail). Raises JobCancelled when the exit was
    caused by POST /cancel. The retry policy lives in separate()."""
    env = os.environ.copy()
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    spawn_at = time.monotonic()
    proc = subprocess.Popen(
        _demucs_cmd(device, source, job_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
        env=env,
    )
    if proc.stderr is None:
        raise RuntimeError("demucs subprocess has no stderr pipe")
    set_proc(job.id, proc)
    # Time from spawn to demucs's first progress line -- process/model-load
    # startup cost, as opposed to actual separation work (#288). Measurement
    # only: subprocess isolation (kill-on-cancel, crash containment) is a
    # design feature we keep regardless of what this turns out to be.
    startup_recorded = False

    # tqdm uses \r to redraw -- read char-by-char and split on \r or \n.
    # Keep the last few non-progress lines so we can surface them if demucs
    # exits non-zero (otherwise the only signal would be a bare exit code).
    buf = ""
    tail: list[str] = []
    last_output: list[float] = [time.monotonic()]
    # Event set by the reader loop when the process exits normally so the
    # watchdog can wake up immediately instead of waiting out its 30 s sleep.
    _done_evt = threading.Event()

    def _watchdog() -> None:
        while not _done_evt.wait(timeout=30):
            if proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > TIMEOUT_DEMUCS_STALL:
                logger.warning(
                    "demucs stalled for %ss with no output, terminating job %s",
                    TIMEOUT_DEMUCS_STALL,
                    job.id,
                )
                proc.terminate()
                return

    wt = threading.Thread(target=_watchdog, daemon=True)
    wt.start()
    try:
        while True:
            ch = proc.stderr.read(1)
            if not ch:
                break
            last_output[0] = time.monotonic()
            if ch in ("\r", "\n"):
                line = buf.strip()
                buf = ""
                if not line:
                    continue
                m = _PCT_RE.search(line)
                if m:
                    if not startup_recorded:
                        startup_recorded = True
                        if job.stage_timings is None:
                            job.stage_timings = {}
                        job.stage_timings["separate_startup"] = round(
                            time.monotonic() - spawn_at, 1
                        )
                    pct = max(0, min(100, int(m.group(1))))
                    _set(job, progress=pct / 100.0, stage=f"Separating {pct}%")
                else:
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
            else:
                buf += ch

        proc.wait()
    finally:
        _done_evt.set()
        set_proc(job.id, None)
        wt.join(timeout=2)

    # POST /cancel calls proc.terminate() directly, which causes the read loop
    # above to hit EOF and proc.wait() to return a nonzero status. Translate
    # that into JobCancelled before the generic "demucs failed" path.
    if job.cancel_requested:
        raise JobCancelled()
    return proc.returncode, tail


def separate(job: Job, source: Path, job_dir: Path) -> Path:
    """Run demucs on the configured device, falling back to CPU once when a
    GPU attempt fails (#276).

    The fallback is deliberately loud, never silent (the #247 lesson): the
    stage line says so while it runs, the WARNING log carries the full stderr
    tail, and gpu_fallback/compute_device persist to job state and metadata.
    It applies even when the user forced cuda/mps in Settings -- a dead job
    with no diagnostics is strictly worse for them than a slow one that
    explains itself."""
    _set(job, status="separating", progress=0.0, stage="Separating stems...")

    # Read the device fresh per job (not a frozen import) so a Settings change
    # applies to the next separation without a restart. Recorded on the job for
    # the completion summary / metadata / failure quarantine.
    device = get_demucs_device()
    job.compute_device = device
    logger.info("[%s] separating on device=%s", job.id, device)

    rc, tail = _run_demucs(job, source, job_dir, device)

    if rc != 0 and device != "cpu":
        cause = classify_failure("\n".join(tail))
        logger.warning(
            "[%s] demucs failed on %s (exit %s, cause=%s); retrying on CPU. tail:\n%s",
            job.id,
            device,
            rc,
            cause,
            "\n".join(tail[-15:]) or "(no stderr captured)",
        )
        # Partial output from the failed attempt must not be mistaken for
        # results by collect(); CPU restarts from scratch, so does progress.
        shutil.rmtree(job_dir / DEMUCS_MODEL, ignore_errors=True)
        _set(job, progress=0.0, stage="GPU failed — retrying on CPU (slower)...")
        job.gpu_fallback = True
        job.compute_device = f"cpu (fallback from {device})"
        first_tail = tail
        rc, tail = _run_demucs(job, source, job_dir, "cpu")
        if rc != 0:
            combined = [
                f"--- attempt on {device} ---",
                *first_tail[-20:],
                "--- cpu fallback attempt ---",
                *tail[-20:],
            ]
            last = tail[-1] if tail else f"exit status {rc}"
            logger.error("[%s] cpu fallback also failed (exit %s)", job.id, rc)
            raise SeparationError(
                f"demucs failed: {last}", tail=combined, device=f"{device}, then cpu"
            )
    elif rc != 0:
        detail = "\n".join(tail[-15:]) if tail else "(no stderr captured)"
        logger.error("[%s] demucs exited %s; tail:\n%s", job.id, rc, detail)
        last = tail[-1] if tail else f"exit status {rc}"
        # SeparationError carries the stderr tail + device so the runner's
        # failure quarantine can preserve the evidence (#277).
        raise SeparationError(f"demucs failed: {last}", tail=tail[-40:], device=device)

    stems_root = job_dir / DEMUCS_MODEL / source.stem
    if not stems_root.is_dir():
        raise SeparationError(f"demucs output not found at {stems_root}", device=job.compute_device)
    return stems_root
