"""One answer to "is this process gone", shared by the tests that kill a tree.

A killed child whose parent is gone is reparented to pid 1, and where pid 1
never reaps (the CI job container runs `tail -f /dev/null`) it stays a zombie:
kill(pid, 0) still succeeds for it, but it is not running. So a zombie counts
as gone. app.core.process.process_exists alone would call it alive, which is
what a watchdog wants and what a "did the cancel kill it" test does not.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.process import process_exists


def is_gone(pid: int) -> bool:
    """Whether the process is dead, a zombie counting as dead."""
    if not process_exists(pid):
        return True
    if os.name == "nt":
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[0] in ("Z", "X")
