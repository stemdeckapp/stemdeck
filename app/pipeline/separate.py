from __future__ import annotations

import json
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

# Persistent worker (#309): only one job ever runs at a time (_pipeline_lock
# in runner.py), so there is exactly one worker to track, not a pool. Reused
# across consecutive successful jobs on the same device; torn down on cancel,
# a device change, or any job failure -- see demucs_worker.py's docstring for
# why a failure always kills the worker rather than trying to keep serving.
_worker: dict[str, object] = {}


def _spawn_worker_cmd(device: str) -> list[str]:
    """Build the persistent-worker invocation. Module-level seam so tests can
    swap in a stub executable without touching the process-management
    machinery (mirrors the old _demucs_cmd seam)."""
    return [sys.executable, "-m", "app.pipeline.demucs_worker", device]


def _kill_worker() -> None:
    proc = _worker.pop("proc", None)
    _worker.pop("device", None)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _get_worker(device: str) -> subprocess.Popen:
    """Return a live worker bound to `device`, reusing the current one if it
    already matches and is still alive, spawning fresh otherwise (first call,
    a device change, or the previous worker died/was torn down)."""
    proc = _worker.get("proc")
    if proc is not None and _worker.get("device") == device and proc.poll() is None:
        return proc
    _kill_worker()

    env = os.environ.copy()
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    proc = subprocess.Popen(
        _spawn_worker_cmd(device),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    _worker["proc"] = proc
    _worker["device"] = device
    return proc


def _run_demucs(job: Job, source: Path, job_dir: Path, device: str) -> tuple[int, list[str]]:
    """One demucs job dispatched to the persistent worker for `device`:
    reuse-or-spawn, stream progress, watchdog stalls.

    Returns (returncode, stderr_tail). Raises JobCancelled when the exit was
    caused by POST /cancel. The retry policy lives in separate()."""
    spawn_at = time.monotonic()
    proc = _get_worker(device)
    if proc.stdin is None or proc.stderr is None:
        raise RuntimeError("demucs worker has no stdin/stderr pipe")
    set_proc(job.id, proc)

    shifts = 2 if get_separation_quality() == "best" else 1
    req = json.dumps({"source": str(source), "job_dir": str(job_dir), "shifts": shifts}) + "\n"
    try:
        proc.stdin.write(req)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        # The worker died between _get_worker() and here -- e.g. a cancel on
        # the previous job raced this dispatch. Treat as an ordinary failure;
        # separate()'s retry policy handles it exactly like a nonzero exit.
        set_proc(job.id, None)
        _kill_worker()
        return 1, ["demucs worker is not accepting input (died before dispatch)"]

    # Time from dispatch to the first progress line -- near-zero for a reused
    # warm worker, the full process/model-load cost for a freshly spawned one
    # (#288/#309). Subprocess isolation (kill-on-cancel, crash containment)
    # is a design feature we keep regardless of which case this run hits.
    startup_recorded = False

    # tqdm uses \r to redraw -- read char-by-char and split on \r or \n.
    # Keep the last few non-progress lines so we can surface them if the job
    # fails (otherwise the only signal would be a bare exit code).
    buf = ""
    tail: list[str] = []
    last_output: list[float] = [time.monotonic()]
    job_ok: bool | None = None  # None while streaming; True/False once decided
    # Event set by the reader loop when the job finishes so the watchdog can
    # wake up immediately instead of waiting out its 30 s sleep.
    _done_evt = threading.Event()

    def _watchdog() -> None:
        while not _done_evt.wait(timeout=30):
            if proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > TIMEOUT_DEMUCS_STALL:
                logger.warning(
                    "demucs worker stalled for %ss with no output, terminating job %s",
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
                # EOF -- the worker process itself exited (crash, or it
                # already wrote @@ERROR@@ and is shutting down).
                job_ok = False
                break
            last_output[0] = time.monotonic()
            if ch in ("\r", "\n"):
                line = buf.strip()
                buf = ""
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
                    job_ok = False
                    break
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
    finally:
        _done_evt.set()
        set_proc(job.id, None)
        wt.join(timeout=2)

    # Never reuse a worker after anything but a clean success: a cancel
    # (proc.terminate() from the API thread) already killed it; a failure's
    # GPU/CUDA state afterward isn't something we can vouch for. Only the
    # happy path keeps the worker warm for the next job.
    if job_ok is not True:
        _kill_worker()

    # POST /cancel calls proc.terminate() directly, which causes the read
    # loop above to hit EOF. Translate that into JobCancelled before the
    # generic "demucs failed" path.
    if job.cancel_requested:
        raise JobCancelled()
    return (0, tail) if job_ok else (1, tail)


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
