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


def vorbis_available() -> bool:
    """Whether the ffmpeg the app runs has libvorbis, asked the way the app
    asks it (app/pipeline/audio_encoders.py)."""
    from app.pipeline.audio_encoders import ffmpeg_encoders

    names = ffmpeg_encoders()
    return names is not None and "libvorbis" in names


def skip_without_vorbis() -> None:
    """For a test that renders a real OGG. libvorbis is optional (#824):
    Homebrew's plain ffmpeg has none, and there OGG export is refused by
    design. tests/test_audio_encoders.py covers that path on every machine."""
    skip_without_ffmpeg()
    if not vorbis_available():
        pytest.skip("ffmpeg has no libvorbis encoder, so OGG export is unavailable here (#824)")
