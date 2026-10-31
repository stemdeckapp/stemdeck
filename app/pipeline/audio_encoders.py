"""Which audio export formats the FFmpeg this process runs can encode.

One answer for the whole backend and, through /api/health, for the UI. The
desktop shell requires pcm_s16le, flac, libmp3lame and aac in the FFmpeg it
installs, and treats libvorbis as optional (#824): the per-architecture macOS
download and Homebrew's plain `ffmpeg` have none. Without libvorbis everything
works except OGG export, which the UI then shows as unavailable and the export
routes refuse with a clear 422 instead of a failed render.

`ffmpeg -encoders` runs once per binary. The result is cached against
`ffmpeg_identity()`, the same key app/pipeline/video_encoder.py caches its
probes under, so a replaced binary is probed again and nothing else is.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading

from app.core.config import TIMEOUT_AUDIO_ENCODER_LIST, ffmpeg_executable
from app.pipeline.video_encoder import ffmpeg_identity

logger = logging.getLogger("stemdeck.encoders")

# The encoder each audio export format is rendered with (app/api/stems.py's
# _ENCODE_ARGS; MP3 names no encoder there, and libmp3lame is the only MP3
# encoder FFmpeg has). OGG needs libvorbis specifically: the native "vorbis"
# encoder most builds also list is experimental and not what the export uses.
EXPORT_FORMAT_ENCODERS = {
    "wav": "pcm_s16le",
    "mp3": "libmp3lame",
    "flac": "flac",
    "ogg": "libvorbis",
}

# Safe to show anyone: names no path, says what to do.
OGG_UNAVAILABLE_DETAIL = (
    "OGG export needs an FFmpeg with Vorbis (libvorbis) support, and the FFmpeg "
    "StemDeck is using has none. Export as WAV, MP3 or FLAC instead, or install "
    "an FFmpeg that includes libvorbis (on a Mac: brew install ffmpeg-full)."
)

# One line of `ffmpeg -encoders`: six flag characters (type V/A/S, then five
# capability flags or dots), the encoder name, its description. The legend
# above the "------" line has the same shape with "=" for a name.
_ENCODER_LINE = re.compile(r"^\s*[VAS][A-Z.]{5}\s+(\S+)")

_cache: dict[str, frozenset[str] | None] = {}
_lock = threading.Lock()


def parse_encoder_names(listing: str) -> frozenset[str]:
    """The encoder names in an `ffmpeg -encoders` listing."""
    names = set()
    for line in listing.splitlines():
        m = _ENCODER_LINE.match(line)
        if m and m.group(1) != "=":
            names.add(m.group(1))
    return frozenset(names)


def _list_encoders() -> frozenset[str] | None:
    try:
        out = subprocess.run(  # noqa: S603 -- fixed args, trusted ffmpeg
            [ffmpeg_executable(), "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_AUDIO_ENCODER_LIST,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("ffmpeg -encoders failed; export formats unknown", exc_info=True)
        return None
    if out.returncode != 0:
        logger.warning("ffmpeg -encoders exited %s; export formats unknown", out.returncode)
        return None
    names = parse_encoder_names(out.stdout)
    return names or None


def ffmpeg_encoders() -> frozenset[str] | None:
    """The encoders the active ffmpeg has, or None when that is unknown (no
    ffmpeg to run, or the listing failed). Unknown is not "none": callers
    treat it as they did before this probe existed."""
    ident, _version = ffmpeg_identity()
    if not ident:
        return None
    with _lock:
        if ident in _cache:
            return _cache[ident]
    names = _list_encoders()
    with _lock:
        _cache[ident] = names
    return names


def export_formats() -> dict[str, bool] | None:
    """{format: whether the active ffmpeg can encode it} for the audio export
    formats, or None when unknown."""
    names = ffmpeg_encoders()
    if names is None:
        return None
    return {fmt: encoder in names for fmt, encoder in EXPORT_FORMAT_ENCODERS.items()}


def unavailable_detail(fmt: str) -> str | None:
    """Why `fmt` cannot be exported, as a safe message for a 422, or None.

    Only when the active ffmpeg is known to lack its encoder: unknown reads as
    available, so an export is tried as it always was."""
    formats = export_formats()
    if formats is None or formats.get(fmt) is not False:
        return None
    if fmt == "ogg":
        return OGG_UNAVAILABLE_DETAIL
    return (
        f"{fmt.upper()} export needs the {EXPORT_FORMAT_ENCODERS[fmt]} encoder, and the "
        "FFmpeg StemDeck is using does not have it."
    )


def reset_cache() -> None:
    """Forget every probe (tests)."""
    with _lock:
        _cache.clear()
