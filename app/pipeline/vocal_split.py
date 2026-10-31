"""On-demand lead/backing vocal split (#275).

A post-hoc action on an already-"done" job (POST /api/jobs/{id}/vocal-split
in app/api/jobs.py), not part of the main separate/collect pipeline. Runs
UVR-MDX-NET Karaoke 2 (or STEMDECK_KARAOKE_MODEL's override) on the job's
existing stems/vocals.wav via a fresh subprocess per invocation --
deliberately not a persistent worker like demucs_worker.py, since this is an
occasional user-triggered action, not the hot path every job takes (the
persistent-worker pattern in ml-pipeline.md exists specifically to amortize a
cost every job pays; that reasoning doesn't apply here, #309).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import TIMEOUT_VOCAL_SPLIT, VOCAL_SPLIT_WATCHDOG_POLL_SEC
from app.core.models import Job
from app.core.registry import set_proc
from app.core.settings import get_demucs_device
from app.core.stem_files import decoded_wav, stem_path
from app.core.vocal_split_outcome import CANCELLED, CODES, TIMEOUT, WORKER_FAILED
from app.pipeline.errors import SeparationError
from app.pipeline.vocal_split_status import failure_code

logger = logging.getLogger("stemdeck.pipeline")


class VocalSplitError(SeparationError):
    """A split that did not make its two stems, with the reason as one of
    app.core.vocal_split_outcome.CODES (#832). The message and tail are for
    the log and vocal_split_error.txt; only the code reaches a client."""

    def __init__(self, message: str, *, code: str, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.code = code if code in CODES else WORKER_FAILED


def _spawn_cmd(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.pipeline.vocal_split_worker",
        device,
        str(vocals_path),
        str(out_dir),
    ]


def split_vocals(job: Job, stems_dir: Path) -> list[str]:
    """Run the karaoke model on stems/vocals.wav, producing lead_vocals.wav +
    backing_vocals.wav in stems_dir. Returns the two new stem names on
    success. Raises VocalSplitError (carrying the stderr tail and the reason
    code) on failure.

    Best-effort by design: the caller (the vocal-split API endpoint) is
    responsible for catching failures and leaving the job's base stems
    untouched -- this function never mutates anything but stems_dir's
    contents, and never touches vocals.wav itself."""
    vocals_path = stem_path(stems_dir, "vocals")
    if not vocals_path.is_file():
        raise VocalSplitError(
            "vocals.wav not found -- job has no vocals stem to split", code=WORKER_FAILED
        )
    # audio-separator gets a WAV whatever the song keeps (#783): an MP3 vocals
    # stem is decoded by ffmpeg first, so the model reads exactly the samples
    # every other reader does.
    try:
        with decoded_wav(vocals_path) as wav_in:
            return _split(job, wav_in, stems_dir)
    except SeparationError:
        raise
    except RuntimeError as exc:
        raise VocalSplitError("could not read the vocals stem", code=WORKER_FAILED) from exc


def _split(job: Job, vocals_path: Path, stems_dir: Path) -> list[str]:
    """split_vocals on a WAV vocals file."""

    device = get_demucs_device()
    env = os.environ.copy()
    # Pin the child's stdio encoding to match what the parent now decodes with.
    # Without it a Windows child writes cp1252 while the parent reads utf-8, so
    # the mismatch simply moves rather than being fixed. Demucs and audio-
    # separator both emit progress bars and can echo track metadata, neither of
    # which is guaranteed to be cp1252-safe.
    # The worker arms a watchdog on this and hard-exits when we disappear, so a
    # kill that runs no cleanup (SIGKILL, Force Quit, Task Manager, a crash)
    # cannot leave it running with nobody to collect the result (#519).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    try:
        proc = subprocess.Popen(
            _spawn_cmd(device, vocals_path, stems_dir),
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
    except OSError as exc:
        raise VocalSplitError(
            f"vocal split could not start: {exc}", code=WORKER_FAILED, device=device
        ) from exc
    set_proc(job.id, proc)

    tail: list[str] = []
    last_output = [time.monotonic()]
    done_evt = threading.Event()
    stalled = threading.Event()
    # The code the worker gave with its error line, when it gave one.
    worker_code: list[str | None] = [None]

    def _watchdog() -> None:
        while not done_evt.wait(timeout=VOCAL_SPLIT_WATCHDOG_POLL_SEC):
            if proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > TIMEOUT_VOCAL_SPLIT:
                logger.warning(
                    "vocal split stalled for %ss with no output, terminating job %s",
                    TIMEOUT_VOCAL_SPLIT,
                    job.id,
                )
                stalled.set()
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
                # {"message", "code"} from this worker; a plain string from
                # one that predates the code.
                if isinstance(msg, dict):
                    code = msg.get("code")
                    worker_code[0] = code if isinstance(code, str) else None
                    msg = msg.get("message", "")
                tail.append(str(msg))
                break
            tail.append(line)
            if len(tail) > 40:
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
        logger.warning("[%s] vocal split failed: %s", job.id, detail)
        last = tail[-1] if tail else "vocal split failed"
        # A cancel and the watchdog both end the worker from outside, so what
        # it last wrote says nothing about why it stopped.
        if job.cancel_requested:
            code = CANCELLED
        elif stalled.is_set():
            code = TIMEOUT
        elif worker_code[0] is not None:
            code = worker_code[0]
        else:
            # A worker that died without its error line (a crash, the OOM
            # killer), or one too old to send a code: judged by its words.
            code = failure_code("unknown", text="\n".join(tail))
        raise VocalSplitError(
            f"vocal split failed: {last}", code=code, tail=tail[-40:], device=device
        )

    for name in ("lead_vocals", "backing_vocals"):
        if not (stems_dir / f"{name}.wav").is_file():
            raise VocalSplitError(
                f"vocal split did not produce {name}.wav", code=WORKER_FAILED, device=device
            )
    return ["lead_vocals", "backing_vocals"]
