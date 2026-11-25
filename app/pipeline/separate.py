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

from app.core import stem_models
from app.core.config import DEMUCS_MODEL, TIMEOUT_DEMUCS_STALL, TIMEOUT_MODEL_STALL
from app.core.models import Job, JobCancelled, _set
from app.core.registry import set_proc
from app.core.settings import (
    get_demucs_device,
    get_separation_quality,
    get_stem_model_choices,
    resolve_stem_models,
)
from app.pipeline import idle_unload, jobqueue, stem_plan
from app.pipeline.errors import SeparationError, classify_failure
from app.pipeline.idle_unload import IdleResource

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")
# The line torch.hub writes before it fetches a checkpoint.
_DOWNLOADING_RE = re.compile(r'^Downloading: "(?P<url>[^"]+)" to (?P<dst>.+)$')
# Terminate demucs if stderr produces no output for this many seconds.
# GPU processing can be silent for minutes; 30 min covers legitimate pauses
# while still catching genuine hangs (GPU deadlock, OOM stall, etc.).

# The worker's last line when a model the person added could not be loaded or
# run (demucs_worker.UserModelFailed): matched whole, on the LAST tail line
# only, so the same words anywhere else in a tail never pick this branch.
_USER_FAILED_RE = re.compile(r"user model failed: (user_[a-f0-9]{12}): ([a-z_]{1,32})")
# What stands in for a model the person added that could not run: it makes
# every selectable stem, and every install that separates at all has it.
STAND_IN_MODEL = "htdemucs_6s"
# At most this many models the person added are stood in for in one job; a
# plan names at most five, and a job failing over and over must end.
MAX_STAND_INS = 3

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


class _LeftBehind:
    """A worker given up on after kill(), with its device."""

    __slots__ = ("proc", "device", "since", "kills", "released")

    def __init__(self, proc: subprocess.Popen, device: str) -> None:
        self.proc = proc
        self.device = device
        self.since = time.monotonic()
        self.kills = 0
        self.released = False


# Workers given up on after kill(). Still counted, so the next job does not
# start a second GPU worker beside one that may still hold the card's memory,
# and shutdown tries them once more. The lock guards the list only and is
# never held across a wait.
_left_behind: list[_LeftBehind] = []
_left_behind_lock = threading.Lock()
# After this many more kills, or this long, a leftover stops blocking the GPU:
# one that never exits must not turn GPU separation off for the life of the
# process. The next GPU worker is started beside it, and if memory runs out
# the ordinary GPU-to-CPU retry in separate() takes over.
_LEFT_BEHIND_MAX_KILLS = 3
_LEFT_BEHIND_MAX_SEC = 600


def _reap_left_behind() -> set[str]:
    """Forget the left-behind workers that have exited, kill the rest again.
    Returns the devices whose leftover still blocks a new worker."""
    with _left_behind_lock:
        entries = list(_left_behind)
    blocking: set[str] = set()
    for entry in entries:
        proc = entry.proc
        if proc.poll() is not None:
            logger.info("left-behind demucs worker %s (%s) has exited", proc.pid, entry.device)
            with _left_behind_lock:
                if entry in _left_behind:
                    _left_behind.remove(entry)
            _close_pipes(proc)
            continue
        if entry.released:
            continue
        if (
            entry.kills >= _LEFT_BEHIND_MAX_KILLS
            or time.monotonic() - entry.since >= _LEFT_BEHIND_MAX_SEC
        ):
            entry.released = True
            logger.error(
                "left-behind demucs worker %s (%s) survived %d kills; no longer holding %s"
                " jobs back, the next one starts beside it",
                proc.pid,
                entry.device,
                entry.kills,
                entry.device,
            )
            continue
        logger.warning(
            "left-behind demucs worker %s (%s) is still running; killing again",
            proc.pid,
            entry.device,
        )
        entry.kills += 1
        try:
            proc.kill()
        except OSError:
            pass
        blocking.add(entry.device)
    return blocking


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
                with _left_behind_lock:
                    _left_behind.append(_LeftBehind(proc, str(device or "")))
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
    # A snapshot: a job thread's _kill_worker can still add one after this
    # loop. That one, and any of these the kill does not end, has the
    # worker's parent-PID watchdog as its backstop (STEMDECK_PARENT_PID): it
    # exits once this process is gone, unless it is wedged in the kernel,
    # where nothing from here could reach it either.
    with _left_behind_lock:
        entries = list(_left_behind)
    for entry in entries:
        if entry.proc.poll() is None:
            try:
                entry.proc.kill()
            except OSError:
                pass
            logger.error(
                "demucs worker %s (%s) still running at shutdown", entry.proc.pid, entry.device
            )


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
    # The plan separate() recorded for this job: the CPU retry runs the same
    # one. None (a caller that recorded none) is the base model alone.
    plan = job.stem_models
    # What the worker needs to run each model the person added that the plan
    # names, and nothing for any other: it never reads their store. Built
    # before a worker is started: one removed since the plan was made fails
    # the attempt as that model's failure, so separate() stands 6s in.
    user: dict[str, dict] = {}
    for model_id in sorted({m for m in (plan or {}).values() if stem_models.is_user_model(m)}):
        try:
            user[model_id] = stem_models.user_worker_spec(model_id)
        except Exception:  # noqa: BLE001 -- removed or no longer usable since resolved
            logger.warning(
                "[%s] model %s cannot be handed to the worker", job.id, model_id, exc_info=True
            )
            return 1, [f"user model failed: {model_id}: unavailable"]
    # GPU only: one check per GPU attempt, so the kill count below counts jobs.
    if device != "cpu" and device in _reap_left_behind():
        # A worker given up on after kill() may still hold the GPU's memory,
        # and a second one beside it fails on memory. A failure, so
        # separate() retries on the CPU; the reason is in the tail and the
        # WARNING log, while the stage line shows the ordinary GPU retry.
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
    request: dict[str, object] = {"source": str(source), "job_dir": str(job_dir), "shifts": shifts}
    if plan is not None:
        request["plan"] = plan
    if user:
        request["user_models"] = user
    req = json.dumps(request) + "\n"
    phases = stem_plan.phases(
        plan or stem_plan.base_plan(), device, quality, stem_models.user_model_rtf
    )
    try:
        proc.stdin.write(req)
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        # The worker died between _get_worker() and here -- e.g. a cancel on
        # the previous job raced this dispatch. Treat as an ordinary failure;
        # separate()'s retry policy handles it exactly like a nonzero exit.
        set_proc(job.id, None)
        _kill_worker()
        # Unless it died because this job was cancelled: set_proc() stops a
        # worker registered after its job's cancel (#748), and on Linux that
        # stop lands before this write, which then meets a closed pipe. That
        # is a cancel, not a failed separation (Windows still buffers the
        # write, so there the read below saw EOF and said so).
        if job.cancel_requested:
            raise JobCancelled() from None
        return 1, ["demucs worker is not accepting input (died before dispatch)"]

    # Time from dispatch to the first progress line -- near-zero for a reused
    # warm worker, the full process/model-load cost for a freshly spawned one
    # (#288/#309). Subprocess isolation (kill-on-cancel, crash containment)
    # is a design feature we keep regardless of which case this run hits.
    startup_recorded = False
    # The pass the worker is on (its @@PHASE@@ line), mapped onto one bar,
    # and the sweep inside it: Demucs runs one 0-100% sweep per model of its
    # bag and per shift (@@SWEEPS@@), and a drop in the percentage is the next.
    phase = phases[0]
    phase_at: float | None = None
    sweeps, sweep, last_pct = 1, 0, -1

    def _end_phase(now: float) -> None:
        if phase_at is not None:
            logger.info(
                "[%s] pass %s took %.1f s on %s", job.id, phase.model_id, now - phase_at, device
            )

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
    # tqdm prints a finished bar's 100% twice (the last refresh, then the close).
    # The second one is the download's, not a separation sweep's.
    after_fetch_100 = False
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
            except ValueError as exc:
                # The pipe was closed under the read: a shutdown or another
                # stop ran _kill_worker. The same as the worker exiting.
                logger.warning("[%s] demucs worker stderr closed during read: %s", job.id, exc)
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
                    _end_phase(time.monotonic())
                    job_ok = True
                    break
                if line.startswith("@@PHASE@@"):
                    # A new pass: any download before it is over.
                    fetching = None
                    now = time.monotonic()
                    _end_phase(now)
                    model_id = line[len("@@PHASE@@") :]
                    phase = next((p for p in phases if p.model_id == model_id), phase)
                    phase_at = now
                    sweeps, sweep, last_pct = 1, 0, -1
                    continue
                if line.startswith("@@SWEEPS@@"):
                    count = line[len("@@SWEEPS@@") :]
                    sweeps = max(1, min(64, int(count))) if count.isdigit() else 1
                    sweep, last_pct = 0, -1
                    continue
                if line.startswith("@@SCALE@@"):
                    logger.info(
                        "[%s] stems scaled down by %s to fit, the same for every stem",
                        job.id,
                        line[len("@@SCALE@@") :],
                    )
                    continue
                if line.startswith("@@ERROR@@"):
                    msg = line[len("@@ERROR@@") :]
                    try:
                        msg = json.loads(msg)
                    except json.JSONDecodeError:
                        pass
                    tail.append(str(msg))
                    job_ok = False
                    break
                # Before the percentage: a destination path can hold a "%".
                downloading = _DOWNLOADING_RE.match(line)
                if downloading is not None:
                    fetching = downloading
                    tail.append(line)
                    if len(tail) > 40:
                        tail.pop(0)
                    continue
                m = _PCT_RE.search(line)
                if m and fetching is not None:
                    # The model download's progress bar, not separation.
                    pct = max(0, min(100, int(m.group(1))))
                    _set(job, stage=f"Downloading the separation model {pct}%")
                    if pct == 100:
                        fetching = None
                        after_fetch_100 = True
                elif m and after_fetch_100 and int(m.group(1)) >= 100:
                    after_fetch_100 = False
                elif m:
                    after_fetch_100 = False
                    if not startup_recorded:
                        startup_recorded = True
                        # A new dict, as runner._lap writes one.
                        job.stage_timings = {
                            **(job.stage_timings or {}),
                            "separate_startup": round(time.monotonic() - spawn_at, 1),
                        }
                    pct = max(0, min(100, int(m.group(1))))
                    if pct < last_pct:
                        sweep = min(sweep + 1, sweeps - 1)
                    last_pct = pct
                    done = (sweep + pct / 100.0) / sweeps
                    _set(
                        job,
                        progress=phase.start + phase.span * done,
                        stage=f"{phase.label} {round(done * 100)}%",
                    )
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


def separate(
    job: Job,
    source: Path,
    job_dir: Path,
    quality: str | None = None,
    plan: dict[str, str] | None = None,
    *,
    stand_in: bool = False,
) -> Path:
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

    `plan` is the model per selectable stem (app/core/stem_models.py); None
    resolves Settings, Separation for the device this job runs on, when it
    starts. Recorded on the job as stem_models before the worker sees it, so
    a Settings change while it runs applies to the next separation, the CPU
    retry runs the same plan, and a later fill-stems runs it again.

    A model the person added that cannot be loaded or run is stood in for:
    the job runs again from scratch on the same device with htdemucs_6s for
    that model's stems, stem_models says what really ran, and
    stem_model_fallbacks records which model each stood-in stem was meant
    for and why. Loud like the CPU retry: the stage line, a WARNING with the
    tail, and the record in job state and metadata. A given plan (a fill) is
    never stood in for: a 6s stem would not add up with the stems the song
    has, so the fill fails instead. ``stand_in`` lets a given plan be stood in
    for too: a re-extract (stem_reextract.py) takes one stem of the run and
    rebuilds "other" around it, so any model's stem adds up.

    Holds the worker in use for the whole call, the CPU retry included, so the
    idle timer never stops it under a job (#975)."""
    with WORKER.in_use():
        return _separate(job, source, job_dir, quality, plan, stand_in=stand_in)


def _separate(
    job: Job,
    source: Path,
    job_dir: Path,
    quality: str | None = None,
    plan: dict[str, str] | None = None,
    *,
    stand_in: bool = False,
) -> Path:
    _set(job, status="separating", progress=0.0, stage="Separating stems...")
    quality = quality or get_separation_quality()
    job.separation_model = DEMUCS_MODEL
    job.separation_quality = quality

    # Read the device fresh per job (not a frozen import) so a Settings change
    # applies to the next separation without a restart. Recorded on the job for
    # the completion summary / metadata / failure quarantine.
    device = get_demucs_device()
    job.compute_device = device
    # The plan, like the device, is read once per job, here: a new dict, so
    # nothing that changes later reaches the one this job runs. Read with it:
    # what the stems left on "auto" resolve to on the CPU, for the CPU retry.
    # A given plan (a fill's recorded one) is never changed.
    cpu_plan: dict[str, str] | None = None
    job.stem_model_fallbacks = None
    if plan is not None:
        job.stem_models = dict(plan)
    else:
        auto = {stem for stem, choice in get_stem_model_choices().items() if choice == "auto"}
        job.stem_models = resolve_stem_models(device)
        if device != "cpu" and auto:
            on_cpu = resolve_stem_models("cpu")
            cpu_plan = {
                stem: on_cpu[stem] if stem in auto else model_id
                for stem, model_id in job.stem_models.items()
            }
    logger.info(
        "[%s] separating on device=%s plan: %s",
        job.id,
        device,
        stem_plan.describe(job.stem_models),
    )

    stand_ins = _StandIns(job, source, job_dir, may_stand_in=plan is None or stand_in)
    rc, tail = stand_ins.run(device)
    if stand_ins.replaced and cpu_plan is not None:
        cpu_plan = stand_ins.substitute(cpu_plan)
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
        # The stems on "auto" get what "auto" picks on the CPU (htdemucs_6s's
        # vocals, not the vocal model's 3.4 x real time); a stem chosen in
        # Settings keeps its model.
        if cpu_plan is not None and cpu_plan != job.stem_models:
            job.stem_models = cpu_plan
            logger.info("[%s] CPU retry plan: %s", job.id, stem_plan.describe(cpu_plan))
        first_tail = tail
        rc, tail = stand_ins.run("cpu")
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


def user_failure(tail: list[str]) -> tuple[str, str] | None:
    """(model id, reason code) when the LAST line of ``tail`` is the worker's
    "user model failed" line, else None."""
    if not tail:
        return None
    m = _USER_FAILED_RE.fullmatch(tail[-1].strip())
    return (m.group(1), m.group(2)) if m else None


class _StandIns:
    """Runs one attempt of a job on a device, and again with htdemucs_6s in
    place of each model the person added that could not run, at most
    MAX_STAND_INS times in all for the job, on that same device."""

    def __init__(self, job: Job, source: Path, job_dir: Path, *, may_stand_in: bool) -> None:
        self.job = job
        self.source = source
        self.job_dir = job_dir
        self.may_stand_in = may_stand_in
        self.count = 0
        # Every model stood in for so far, so the CPU retry's plan gets the
        # same stand-ins.
        self.replaced: set[str] = set()

    def substitute(self, plan: dict[str, str]) -> dict[str, str]:
        return {
            stem: STAND_IN_MODEL if model_id in self.replaced else model_id
            for stem, model_id in plan.items()
        }

    def run(self, device: str) -> tuple[int, list[str]]:
        job = self.job
        rc, tail = _run_demucs(job, self.source, self.job_dir, device)
        while rc != 0 and self.may_stand_in and self.count < MAX_STAND_INS:
            failed = user_failure(tail)
            plan = job.stem_models or {}
            if failed is None or failed[0] not in plan.values():
                break
            model_id, code = failed
            self.count += 1
            logger.warning(
                "[%s] the model %s could not run on %s (%s); separating its stems with %s."
                " tail:\n%s",
                job.id,
                model_id,
                device,
                code,
                STAND_IN_MODEL,
                "\n".join(tail[-15:]),
            )
            # The failed attempt's partial output must not be mistaken for
            # results; the next attempt starts from scratch, so does progress.
            shutil.rmtree(self.job_dir / DEMUCS_MODEL, ignore_errors=True)
            if job.cancel_requested:
                raise JobCancelled()
            name = stem_models.user_model_name(model_id)
            record = {
                "model": model_id,
                "name": name[:200] if isinstance(name, str) else None,
                "code": code,
            }
            # New dicts, so nothing holding the old ones sees them change.
            job.stem_model_fallbacks = {
                **(job.stem_model_fallbacks or {}),
                **{stem: dict(record) for stem, m in plan.items() if m == model_id},
            }
            self.replaced.add(model_id)
            job.stem_models = self.substitute(plan)
            logger.info(
                "[%s] plan with HT Demucs 6 stems standing in: %s",
                job.id,
                stem_plan.describe(job.stem_models),
            )
            _set(job, progress=0.0, stage="A model could not run; using HT Demucs 6 stems...")
            rc, tail = _run_demucs(job, self.source, self.job_dir, device)
        return rc, tail
