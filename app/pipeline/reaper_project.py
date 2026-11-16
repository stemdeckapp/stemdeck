"""A REAPER project (.rpp) for a set of exported stems.

The project sits next to the stems in the export zip and names each one by
its file name alone. REAPER resolves a relative FILE against the folder the
.rpp is in, so once the zip is unpacked the project opens with every stem on
its own track, all starting at 0 and lined up exactly as in StemDeck.

Kept to what REAPER needs to load the project as intended: the sample rate
(so nothing is resampled on open), the song's tempo (so the grid and a click
in REAPER follow the song), one track per stem, and one item per track with
its length. Everything else is REAPER's own default.

The tempo is one value, the beat grid's median BPM. Bar 1 is not moved onto
the song's first downbeat: that would need a tempo map from the beat grid.
"""

from __future__ import annotations

import time

# REAPER's source block type for each export format.
_SOURCE_TYPES = {"wav": "WAVE", "flac": "FLAC", "mp3": "MP3", "ogg": "VORBIS"}

DEFAULT_BPM = 120.0
_MIN_BPM = 1.0
_MAX_BPM = 960.0  # REAPER's own limit.


def _quote(text: str) -> str:
    """One RPP string token. REAPER takes "...", '...' or `...`, whichever
    quote the text does not contain; a text with all three loses its
    backticks."""
    for q in ('"', "'", "`"):
        if q not in text:
            return f"{q}{text}{q}"
    return "`" + text.replace("`", "'") + "`"


def track_label(name: str) -> str:
    """A track name from a stem name: "lead_vocals" -> "Lead Vocals"."""
    words = [w for w in name.replace("-", "_").split("_") if w]
    return " ".join(w[:1].upper() + w[1:] for w in words) or name


def build_rpp(
    files: list[tuple[str, str]],
    fmt: str,
    sample_rate: int,
    length_s: float,
    bpm: float | None,
) -> str:
    """The project text. `files` is (track name, file name) per track, in
    order; each file name is relative to the .rpp."""
    source_type = _SOURCE_TYPES[fmt]
    tempo = bpm if bpm and _MIN_BPM <= bpm <= _MAX_BPM else DEFAULT_BPM
    lines = [
        f'<REAPER_PROJECT 0.1 "7.0" {int(time.time())}',
        "  RIPPLE 0",
        f"  SAMPLERATE {int(sample_rate)} 0 0",
        f"  TEMPO {tempo:.6g} 4 4",
    ]
    for track, file in files:
        lines += [
            "  <TRACK",
            f"    NAME {_quote(track_label(track))}",
            "    <ITEM",
            "      POSITION 0",
            f"      LENGTH {length_s:.6f}",
            # REAPER loops an item's source by default. A transcode can end a
            # few ms short of the length measured on the WAV, and a loop would
            # repeat its head there.
            "      LOOP 0",
            f"      NAME {_quote(file)}",
            f"      <SOURCE {source_type}",
            f"        FILE {_quote(file)}",
            "      >",
            "    >",
            "  >",
        ]
    lines.append(">")
    return "\n".join(lines) + "\n"
