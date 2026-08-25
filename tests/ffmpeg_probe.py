"""One answer to "can this machine run ffmpeg", shared by every test that asks.

Each caller used to ask `shutil.which("ffmpeg")`, which is not the question.
The app does not require ffmpeg on PATH: `ffmpeg_executable()` prefers the
bundled binary and only falls back to PATH. So on a developer machine with a
bundled ffmpeg and nothing on PATH, twenty-odd tests skipped while the code
they cover worked perfectly.

That is worse than a failing test. It reported app/api/stems.py at 51% when it
was really at 85%, and the gap pointed a coverage effort at a module that did
not need one.
"""

from __future__ import annotations

import subprocess

import pytest


def ffmpeg_available() -> bool:
    """Whether ffmpeg can actually be run, the way the app resolves it."""
    from app.core.config import ffmpeg_executable

    try:
        subprocess.run(
            [ffmpeg_executable(), "-version"],
            capture_output=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def skip_without_ffmpeg() -> None:
    if not ffmpeg_available():
        pytest.skip("ffmpeg not available (not bundled and not on PATH)")
