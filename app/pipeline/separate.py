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

from app.core.config import DEMUCS_MODEL, TIMEOUT_DEMUCS_STALL, TIMEOUT_MODEL_STALL
from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc
from app.core.settings import get_demucs_device, get_separation_quality
from app.pipeline import idle_unload, jobqueue
from app.pipeline.errors import SeparationError, classify_failure
from app.pipeline.idle_unload import IdleResource

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")
# The line torch.hub writes before it fetches a checkpoint.
_DOWNLOADING_RE = re.compile(r'^Downloading: "(?P<url>[^"]+)" to (?P<dst>.+)$')
# Terminate demucs if stderr produces no output for this many seconds.
# GPU processing can be silent for minutes; 30 min covers legitimate pauses
# while still catching genuine hangs (GPU deadlock, OOM stall, etc.).

# Persistent worker (#309): only one job ever runs at a time (_pipeline_lock
# in runner.py), so there is exactly one worker to track, not a pool. Reused
# across consecutive successful jobs on the same device; torn down on cancel,
# a device change, or any job failure -- see demucs_worker.py's docstring for
# why a failure always kills the worker rather than trying to keep serving --
# and stopped by the idle timer when no job has used it for a while (#975).
_worker: dict[str, object] = {}


def _worker_alive() -> bool:
    proc = _worker.get("proc")
    return proc is not None and proc.poll() is None


def _spawn_worker_cmd(device: str) -> list[str]:
    """Build the persistent-worker invocation. Module-level seam so tests can
    swap in a stub executable without touching the process-management
    machinery (mirrors the old _demucs_cmd seam)."""
    return [sys.executable, "-m", "app.pipeline.demucs_worker", device]


# How long a killed worker gets to exit before it is given up on. The idle
# lock is held across a stop (idle_unload.py), up to 15 s in all: 5 s for
# terminate() and this for kill(). The next job and shutdown wait on that
# lock, so it must end even for a worker wedged in a CUDA call.
_KILL_REAP_TIMEOUT_SEC = 10

# Workers given up on after kill(), with their device. Still counted, so the
# next job does not start a second GPU worker beside one that may still hold
# the card's memory, and shutdown tries them once more.
_left_behind: list[tuple[subprocess.Popen, str]] = []


def _reap_left_behind() -> set[str]:
    """Forget the left-behind workers that have exited, kill the rest again.
    Returns the devices that still have one alive."""
    alive: set[str] = set()
    for entry in list(_left_behind):
        proc, device = entry
        if proc.poll() is not None:
            logger.info("left-behind demucs worker %s (%s) has exited", proc.pid, device)
            _left_behind.remove(entry)
            _close_pipes(proc)
            continue
        logger.warning(
            "left-behind demucs worker %s (%s) is still running; killing again", proc.pid, device
        )
        try:
            proc.kill()
        except OSError:
            pass
        alive.add(device)
    return alive


def _kill_worker() -> None:
    proc = _worker.pop("proc", None)
    device = _worker.pop("device", None)
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            # kill() only sends the signal; the wait reaps it, so it does not
            # stay a zombie. Bounded, unlike the communicate() this replaced:
            # a worker wedged in an uninterruptible call must not hold the
            # idle lock, and with it the next job and shutdown, forever.
            try:
                proc.wait(timeout=_KILL_REAP_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                logger.error(
                    "demucs worker %s did not exit %s s after kill; left behind",
                    proc.pid,
                    _KILL_REAP_TIMEOUT_SEC,
                )
                # Its pipes stay open: a job thread may still be blocked
                # reading stderr, and closing under it would block here.
                _left_behind.append((proc, str(device or "")))
                return
    _close_pipes(proc)


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close an exited worker's pipes now, rather than whenever the Popen is
    collected (a ResourceWarning under -X dev, and a handle held until then)."""
    for pipe in (proc.stdin, proc.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            # stdin's close flushes, and the reader end is gone.
            pass


# Stopped by the idle timer after the Settings' idle minutes without a
# separation (#975); the next separate() starts it again through _get_worker().
# A dead worker counts as unloaded: the next job replaces it either way.
# Lambdas, so a test that replaces _kill_worker replaces it here too.
WORKER = idle_unload.register(
    IdleResource("separation worker", lambda: _worker_alive(), lambda: _kill_worker())
)


def shutdown_worker() -> None:
    """Stop the worker at server shutdown, even under a running job: the
    runner reads that as an interruption (jobqueue.is_stopping), not a
    failure. Through the idle lock, so an idle unload in progress finishes
    first and the two never stop the same process at once. A worker given up
    on earlier gets one more kill, and is logged if it still runs."""
    WORKER.unload_now()
    for proc, device in _left_behind:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            logger.error("demucs worker %s (%s) still running at shutdown", proc.pid, device)


def _get_worker(device: str) -> subprocess.Popen:
    """Return a live worker bound to `device`, reusing the current one if it
    already matches and is still alive, spawning fresh otherwise (first call,
    a device change, or the previous worker died/was torn down)."""
    proc = _worker.get("proc")
    if proc is not None and _worker.get("device") == device and proc.poll() is None:
        return proc
    _kill_worker()

    env = os.environ.copy()
    # Our pid, not whatever the backend inherited: the worker watches this and
    # exits when we are gone, so it cannot be left holding a GPU after a kill
    # that ran no cleanup (SIGKILL, Force Quit, Task Manager, a crash).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    # Pin the child's stdio encoding to match what the parent now decodes with.
    # Without it a Windows child writes cp1252 while the parent reads utf-8, so
    # the mismatch simply moves rather than being fixed. Demucs and audio-
    # separator both emit progress bars and can echo track metadata, neither of
    # which is guaranteed to be cp1252-safe.
    env["PYTHONIOENCODING"] = "utf-8:replace"
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
        # utf-8/replace explicitly. text=True alone decodes with the locale
        # encoding, which on Windows is cp1252, and a single byte outside it
        # in a child's output kills the whole job with a UnicodeDecodeError
        # ("'charmap' codec can't decode byte 0x8f"). This is diagnostic text:
        # a byte we cannot read is never a reason to fail a separation.
        text=True,
        encoding="utf-8",
        errors="replace",
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
    if device in _reap_left_behind() and device != "cpu":
        # A worker given up on after kill() may still hold the GPU's memory,
        # and a second one beside it fails on memory. Said in the tail, so
        # separate() retries on the CPU and the stage line says why.
        return 1, [f"a previous separation worker on {device} has not exited yet"]
    spawn_at = time.monotonic()
    proc = _get_worker(device)
    if proc.stdin is None or proc.stderr is None:
        # Raised before the try below, so nothing else tears this down, and a
        # worker without pipes would otherwise stay cached and be handed to
        # every subsequent job.
        _kill_worker()
        raise RuntimeError("demucs worker has no stdin/stderr pipe")
    set_proc(job.id, proc)

    # The quality separate() recorded for this job (#804), else the setting.
    quality = job.separation_quality or get_separation_quality()
    shifts = 2 if quality == "best" else 1
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
    # The model file the worker is downloading (#125): torch.hub's
    # "Downloading:" line, until that file's 100% or @@READY@@. Only this
    # phase has the short stall limit; importing torch and loading a cached
    # model say nothing either, and are not held to it. A reused worker loaded
    # its model on an earlier job, so it never downloads.
    fetching: re.Match[str] | None = None
    # Set by the watchdog when it stops a worker that stalled downloading.
    stalled_fetching = threading.Event()
    # Event set by the reader loop when the job finishes so the watchdog can
    # wake up immediately instead of waiting out its 30 s sleep.
    _done_evt = threading.Event()

    def _watchdog() -> None:
        while True:
            limit = TIMEOUT_DEMUCS_STALL if fetching is None else TIMEOUT_MODEL_STALL
            # Woken often enough for the short limit, which a download line
            # can switch on between two checks.
            if _done_evt.wait(timeout=min(30, TIMEOUT_MODEL_STALL)) or proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > limit:
                logger.warning(
                    "demucs worker stalled for %ss with no output (%s), terminating job %s",
                    limit,
                    "separating" if fetching is None else "downloading the model",
                    job.id,
                )
                if fetching is not None:
                    stalled_fetching.set()
                proc.terminate()
                return

    wt = threading.Thread(target=_watchdog, daemon=True)
    wt.start()
    try:
        while True:
            try:
                ch = proc.stderr.read(1)
            except ValueError:
                # The pipe was closed under the read: a shutdown or another
                # stop ran _kill_worker. The same as the worker exiting.
                ch = ""
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
                if line == "@@READY@@":
                    fetching = None
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
                if m and fetching is not None:
                    # The model download's progress bar, not separation.
                    pct = max(0, min(100, int(m.group(1))))
                    _set(job, stage=f"Downloading the separation model {pct}%")
                    if pct == 100:
                        fetching = None
                elif m:
                    if not startup_recorded:
                        startup_recorded = True
                        # A new dict, as runner._lap writes one.
                        job.stage_timings = {
                            **(job.stage_timings or {}),
                            "separate_startup": round(time.monotonic() - spawn_at, 1),
                        }
                    pct = max(0, min(100, int(m.group(1))))
                    _set(job, progress=pct / 100.0, stage=f"Separating {pct}%")
                else:
                    fetching = _DOWNLOADING_RE.match(line) or fetching
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
        #
        # Inside the finally, not after it: an exception out of the read loop
        # -- proc.stderr.read(1) raising OSError when the API thread's
        # terminate() races the read, or _set() raising -- skipped this
        # entirely and left a worker whose CUDA state followed an exception
        # warm for the next job (#514).
        if job_ok is not True:
            _kill_worker()

    # POST /cancel calls proc.terminate() directly, which causes the read
    # loop above to hit EOF. Translate that into JobCancelled before the
    # generic "demucs failed" path.
    if job.cancel_requested:
        raise JobCancelled()
    if stalled_fetching.is_set() and fetching is not None:
        # Said in words, or the job fails as a bare "exit status 1".
        tail.append(
            f"model download failed: {fetching['url']};"
            f" put the file in {os.path.dirname(fetching['dst'])}"
            f" (no data for {TIMEOUT_MODEL_STALL} s)"
        )
    return (0, tail) if job_ok else (1, tail)


def separate(job: Job, source: Path, job_dir: Path, quality: str | None = None) -> Path:
    """Run demucs on the configured device, falling back to CPU once when a
    GPU attempt fails (#276).

    The fallback is deliberately loud, never silent (the #247 lesson): the
    stage line says so while it runs, the WARNING log carries the full stderr
    tail, and gpu_fallback/compute_device persist to job state and metadata.
    It applies even when the user forced cuda/mps in Settings -- a dead job
    with no diagnostics is strictly worse for them than a slow one that
    explains itself.

    `quality` is the Separation quality to use, "standard" or "best"; None
    reads the setting, per job like the device. Recorded on the job with the
    model (#804), so stems separated later for the same song (fill-stems) use
    the same settings and add up with the ones it has.

    Holds the worker in use for the whole call, the CPU retry included, so the
    idle timer never stops it under a job (#975)."""
    with WORKER.in_use():
        return _separate(job, source, job_dir, quality)


def _separate(job: Job, source: Path, job_dir: Path, quality: str | None) -> Path:
    _set(job, status="separating", progress=0.0, stage="Separating stems...")
    quality = quality or get_separation_quality()
    job.separation_model = DEMUCS_MODEL
    job.separation_quality = quality

    # Read the device fresh per job (not a frozen import) so a Settings change
    # applies to the next separation without a restart. Recorded on the job for
    # the completion summary / metadata / failure quarantine.
    device = get_demucs_device()
    job.compute_device = device
    logger.info("[%s] separating on device=%s", job.id, device)

    rc, tail = _run_demucs(job, source, job_dir, device)
    cause = classify_failure("\n".join(tail)) if rc != 0 else ""

    # A worker the shutdown killed is not a GPU failure: a CPU pass started
    # now would keep the backend from exiting. Nor is a model that could not be
    # downloaded: the CPU worker fetches the same file and fails the same way.
    if rc != 0 and device != "cpu" and not jobqueue.is_stopping() and cause != "model-download":
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
        # The rmtree above can take seconds on a multi-GB partial result, and
        # nothing is registered for cancel during it. Re-check before paying
        # for a full CPU pass the user has already asked to stop (#514).
        if job.cancel_requested:
            raise JobCancelled()
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
