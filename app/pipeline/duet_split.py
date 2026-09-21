"""On-demand duet split: separate two singers out of the vocals stem.

A post-hoc action on an already-"done" job, the same shape as the lead/backing
vocal split in `vocal_split.py`, sharing its subprocess supervision via
`split_runner.run_split`.

What this can and cannot do
---------------------------
The model separates voices by **vocal weight/register**, not by singer
identity. It splits a duet cleanly when the two singers sit in different
registers -- measured on "Shallow" (Cooper/Gaga), the quiet stem is suppressed
by 30-40 dB through the other singer's verse, and the stems alternate with the
song (per-frame energy share std 0.41 across the excerpt).

It does **not** split two singers of the same register. On "What Is This
Feeling" (Wicked, two sopranos) it routes 93% of the energy to one stem and
alternates not at all. Same-register duets are an unsolved problem, not a
tuning gap: a speech separator (SepFormer) and the purpose-built singing
separator (MedleyVox iSRNet) were both measured on the same material and
neither produced any split at all. See docs/models.md.

Stems are therefore named for what the model actually keys on, so the feature
does not promise per-singer separation it cannot deliver.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from app.core.config import TIMEOUT_DUET_SPLIT
from app.core.models import Job
from app.core.settings import get_demucs_device
from app.pipeline.errors import SeparationError
from app.pipeline.split_runner import run_split

logger = logging.getLogger("stemdeck.pipeline")

DUET_STEMS = ["voice_1", "voice_2"]


def _spawn_cmd(device: str, vocals_path: Path, out_dir: Path) -> list[str]:
    """The worker argv. Kept as its own function so tests can swap in a stub
    worker and still exercise the real subprocess machinery."""
    return [
        sys.executable,
        "-m",
        "app.pipeline.duet_split_worker",
        device,
        str(vocals_path),
        str(out_dir),
    ]


def split_duet(job: Job, stems_dir: Path) -> list[str]:
    """Split stems/vocals.wav into voice_1.wav + voice_2.wav in stems_dir.

    Returns the two new stem names on success. Raises SeparationError (carrying
    the stderr tail) on failure.

    Best-effort by design: the caller is responsible for catching failures and
    leaving the job's base stems untouched -- this never mutates anything but
    stems_dir's contents, and never touches vocals.wav itself.
    """
    vocals_path = stems_dir / "vocals.wav"
    if not vocals_path.is_file():
        raise SeparationError("vocals.wav not found -- job has no vocals stem to split")

    device = get_demucs_device()
    return run_split(
        job,
        argv=_spawn_cmd(device, vocals_path, stems_dir),
        device=device,
        timeout=TIMEOUT_DUET_SPLIT,
        label="duet split",
        produces=DUET_STEMS,
        out_dir=stems_dir,
    )
