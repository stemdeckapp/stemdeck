"""What a pipeline stage needs so a cancel can actually stop it.

Lives apart from runner.py so analyze.py can use it too: runner imports
analyze, so analyze reaching back into runner would be a circular import. The
analyze stage had no cancel path at all, and on a long track it runs for over
a minute (85 s in one report), so a song trashed or cancelled there kept
"Analyzing" until it finished (#748).
"""

from __future__ import annotations

import subprocess

from app.core.models import Job, JobCancelled
from app.core.registry import set_proc


def check_cancel(job: Job) -> None:
    if job.cancel_requested:
        raise JobCancelled()


def run_registered(
    job: Job, cmd: list[str], timeout: int, *, capture_stdout: bool = False
) -> subprocess.CompletedProcess[bytes]:
    """Run a subprocess with it registered, so cancel can reach it.

    subprocess.run() cannot be interrupted: POST /cancel sets the flag, but
    nothing looks at it until the call returns, so a cancel during a large
    upload's transcode was a no-op for up to TIMEOUT_FFMPEG per call -- twice
    over on the .mp4 path, which runs both this and the video extract (#519).
    The analyze decode had the same hole (#748).

    Mirrors collect._run_ffmpeg, which registers for exactly this reason. A
    terminated process comes back as a non-zero returncode, never an
    exception; a timeout kills it and raises subprocess.TimeoutExpired.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    set_proc(job.id, proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout or b"", stderr or b"")
    finally:
        set_proc(job.id, None)
