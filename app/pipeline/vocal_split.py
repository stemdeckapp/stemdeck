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

from app.core.config import TIMEOUT_VOCAL_SPLIT
from app.core.models import Job
from app.core.registry import set_proc
from app.core.settings import get_demucs_device
from app.pipeline.errors import SeparationError

logger = logging.getLogger("stemdeck.pipeline")


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
    success. Raises SeparationError (carrying the stderr tail) on failure.

    Best-effort by design: the caller (the vocal-split API endpoint) is
    responsible for catching failures and leaving the job's base stems
    untouched -- this function never mutates anything but stems_dir's
    contents, and never touches vocals.wav itself."""
    vocals_path = stems_dir / "vocals.wav"
    if not vocals_path.is_file():
        raise SeparationError("vocals.wav not found -- job has no vocals stem to split")

    device = get_demucs_device()
    env = os.environ.copy()
    try:
        import certifi

        env.setdefault("SSL_CERT_FILE", certifi.where())
        env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ModuleNotFoundError:
        pass

    proc = subprocess.Popen(
        _spawn_cmd(device, vocals_path, stems_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    set_proc(job.id, proc)

    tail: list[str] = []
    last_output = [time.monotonic()]
    done_evt = threading.Event()

    def _watchdog() -> None:
        while not done_evt.wait(timeout=30):
            if proc.poll() is not None:
                return
            if time.monotonic() - last_output[0] > TIMEOUT_VOCAL_SPLIT:
                logger.warning(
                    "vocal split stalled for %ss with no output, terminating job %s",
                    TIMEOUT_VOCAL_SPLIT,
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
        raise SeparationError(f"vocal split failed: {last}", tail=tail[-40:], device=device)

    for name in ("lead_vocals", "backing_vocals"):
        if not (stems_dir / f"{name}.wav").is_file():
            raise SeparationError(f"vocal split did not produce {name}.wav", device=device)
    return ["lead_vocals", "backing_vocals"]
