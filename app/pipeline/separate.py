from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import DEMUCS_DEVICE, DEMUCS_MODEL, TIMEOUT_DEMUCS_STALL
from app.core.models import Job, JobCancelled
from app.core.registry import set_proc

logger = logging.getLogger("stemdeck.pipeline")

_PCT_RE = re.compile(r"(\d{1,3})%")
# Terminate demucs if stderr produces no output for this many seconds.
# GPU processing can be silent for minutes; 30 min covers legitimate pauses
# while still catching genuine hangs (GPU deadlock, OOM stall, etc.).


def separate(job: Job, source: Path, job_dir: Path) -> Path:
    from app.pipeline.download import _set

    _set(job, status="separating", progress=0.0, stage="Separating stems...")

    cmd = [
        sys.executable,
        "-m",
        "demucs",
        "-n",
        DEMUCS_MODEL,
        "-d",
        DEMUCS_DEVICE,
        "-o",
        str(job_dir),
        str(source),
    ]
    env = os.environ.copy()
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
        env=env,
    )
    if proc.stderr is None:
        raise RuntimeError("demucs subprocess has no stderr pipe")
    set_proc(job.id, proc)

    # tqdm uses \r to redraw -- read char-by-char and split on \r or \n.
    # Keep the last few non-progress lines so we can surface them if demucs
    # exits non-zero (otherwise the only signal would be a bare exit code).
    buf = ""
    tail: list[str] = []
    last_output: list[float] = [time.monotonic()]

    def _watchdog() -> None:
        while proc.poll() is None:
            time.sleep(30)
            if time.monotonic() - last_output[0] > TIMEOUT_DEMUCS_STALL:
                logger.warning(
                    "demucs stalled for %ss with no output, terminating job %s",
                    TIMEOUT_DEMUCS_STALL,
                    job.id,
                )
                proc.terminate()
                break

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
        set_proc(job.id, None)
        wt.join(timeout=5)

    # POST /cancel calls proc.terminate() directly, which causes the read loop
    # above to hit EOF and proc.wait() to return a nonzero status. Translate
    # that into JobCancelled before the generic "demucs failed" path.
    if job.cancel_requested:
        raise JobCancelled()
    if proc.returncode != 0:
        detail = "\n".join(tail[-15:]) if tail else "(no stderr captured)"
        logger.error("[%s] demucs exited %s; tail:\n%s", job.id, proc.returncode, detail)
        last = tail[-1] if tail else f"exit status {proc.returncode}"
        raise RuntimeError(f"demucs failed: {last}")

    stems_root = job_dir / DEMUCS_MODEL / source.stem
    if not stems_root.is_dir():
        raise RuntimeError(f"demucs output not found at {stems_root}")
    return stems_root
