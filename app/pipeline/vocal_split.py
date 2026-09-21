"""On-demand lead/backing vocal split (#275).

A post-hoc action on an already-"done" job (POST /api/jobs/{id}/vocal-split
in app/api/jobs.py), not part of the main separate/collect pipeline. Runs
UVR-MDX-NET Karaoke 2 (or STEMDECK_KARAOKE_MODEL's override) on the job's
existing stems/vocals.wav via a fresh subprocess per invocation --
deliberately not a persistent worker like demucs_worker.py, since this is an
occasional user-triggered action, not the hot path every job takes (the
persistent-worker pattern in ml-pipeline.md exists specifically to amortize a
cost every job pays; that reasoning doesn't apply here, #309).

The subprocess lifecycle, stall watchdog and error hygiene live in
split_runner.py, shared with the duet split.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.core.config import TIMEOUT_VOCAL_SPLIT
from app.core.models import Job
from app.core.settings import get_demucs_device
from app.pipeline.errors import SeparationError
from app.pipeline.split_runner import run_split

logger = logging.getLogger("stemdeck.pipeline")

VOCAL_SPLIT_STEMS = ["lead_vocals", "backing_vocals"]


def _spawn_cmd(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
    """The worker argv. Kept as its own function so tests can swap in a stub
    worker and still exercise the real subprocess machinery."""
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
    return run_split(
        job,
        argv=_spawn_cmd(device, vocals_path, stems_dir),
        device=device,
        timeout=TIMEOUT_VOCAL_SPLIT,
        label="vocal split",
        produces=VOCAL_SPLIT_STEMS,
        out_dir=stems_dir,
    )
