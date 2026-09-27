"""fpcalc, Chromaprint's own fingerprinter, for an FFmpeg without chromaprint.

Song identification fingerprints with FFmpeg's chromaprint muxer (identify.py).
The Windows (BtbN) and Docker (Debian) FFmpeg builds have it; the macOS
(shaka-project, evermeet) and Linux desktop (johnvansickle) builds do not. There
the desktop shell downloads fpcalc beside FFmpeg, and a source install can have
it from a distro package (libchromaprint-tools, chromaprint).

fpcalc's default output is the same fingerprint the muxer prints: algorithm 2
on its command line is the API's 1 (identify.CHROMAPRINT_ALGORITHM), compressed
and base64-encoded, so AcoustID takes either unchanged. The two were compared on
the same audio and came out byte for byte identical.

Only the helpers live here; identify.py runs the process, so a fingerprint made
either way is stopped by the same cancel, timeout and release_source.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from app.core.config import FPCALC_BIN, ffmpeg_executable

FPCALC_NAME = "fpcalc.exe" if sys.platform.startswith("win") else "fpcalc"


def fpcalc_executable() -> str | None:
    """The fpcalc to run, or None when there is none.

    FPCALC_BIN first (STEMDECK_FPCALC, else the data directory's ffmpeg folder,
    where the desktop shell puts it), then beside the FFmpeg in use (setup may
    have settled on one elsewhere, or data/ffmpeg/bin), then PATH. Evaluated
    per call, so one downloaded while the backend runs is picked up."""
    candidates = [FPCALC_BIN]
    ffmpeg = Path(ffmpeg_executable())
    if ffmpeg.is_file():
        candidates.append(ffmpeg.resolve().parent / FPCALC_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("fpcalc")


def fpcalc_command(fpcalc: str, source: str, length: int) -> list[str]:
    """fpcalc printing, as JSON, the fingerprint of the first ``length``
    seconds of ``source``: a file's absolute path, or "-" for audio on stdin."""
    return [fpcalc, "-json", "-length", str(length), source]


def parse_fpcalc_output(stdout: bytes | str | None) -> str | None:
    """The fingerprint in fpcalc's JSON (``{"duration": .., "fingerprint":
    ..}``), unchecked, or None when there is none. identify.parse_fingerprint
    decides whether it is one."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    try:
        answer = json.loads(stdout or "")
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    fingerprint = answer.get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None
