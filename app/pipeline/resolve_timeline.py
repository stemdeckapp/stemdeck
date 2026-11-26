"""The OpenTimelineIO timeline (content.otio) of a Performance take's
DaVinci Resolve package.

DaVinci Resolve 18.5 and newer import an OpenTimelineIO bundle (.otioz) with
File > Import > Timeline: it unpacks the media next to the file and links it.
The bundle is a zip of content.otio, version.txt and a flat media/ folder
(app/pipeline/resolve_package.write_otioz), and every clip names its file by
a path relative to the bundle, "media/<file>".

Kept to what the import needs: one video track, then one audio track per
audio file, each holding one clip from frame 0 to frame N. Every file starts
at the same song time and is exactly N frames long, so no offset has to
survive the import: the clips line up because they all start at the
timeline's start. The timeline starts at 01:00:00:00, Resolve's own default.

Pure and free of I/O, like reaper_project.py: written with json.dumps, so no
new dependency.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, NamedTuple

# Integer rates only: no drop-frame timecode to get wrong.
OTIO_FPS = (24, 30, 60)
# What a clip may point at: one flat media folder, a file name of plain
# characters (it is also a ZIP member name, so no separators and no traversal).
TARGET_RE = re.compile(r"^media/[A-Za-z0-9_]+\.(mp4|wav)\Z")
# 01:00:00:00, in seconds.
TIMELINE_START_SEC = 3600
# The bundle format version the OTIO file bundle specification names.
BUNDLE_VERSION = "1.0.0"


class TimelineClip(NamedTuple):
    """One track of the timeline: "video" or "audio", the track's (and the
    clip's) name, and the file it plays, relative to the bundle."""

    kind: str
    name: str
    target: str


def _time(value: float, rate: int) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": float(value)}


def _range(frames: int, rate: int) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "duration": _time(frames, rate),
        "start_time": _time(0, rate),
    }


def _clip(clip: TimelineClip, frames: int, fps: int) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Clip.1",
        "metadata": {},
        "name": clip.name,
        "source_range": _range(frames, fps),
        "effects": [],
        "markers": [],
        "enabled": True,
        "media_reference": {
            "OTIO_SCHEMA": "ExternalReference.1",
            "metadata": {},
            "name": clip.target.rsplit("/", 1)[-1],
            "available_range": _range(frames, fps),
            "target_url": clip.target,
        },
    }


def _track(clip: TimelineClip, frames: int, fps: int) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Track.1",
        "metadata": {},
        "name": clip.name,
        "source_range": None,
        "effects": [],
        "markers": [],
        "enabled": True,
        "children": [_clip(clip, frames, fps)],
        "kind": "Video" if clip.kind == "video" else "Audio",
    }


def check_clips(clips: list[TimelineClip]) -> None:
    """Refuse what the bundle cannot hold: a target outside media/ or with
    other characters, two files whose names differ only in case (one would
    overwrite the other on a case-insensitive disk), a kind other than video
    or audio, and a timeline without exactly one video track first."""
    if not clips or clips[0].kind != "video" or any(c.kind == "video" for c in clips[1:]):
        raise ValueError("a timeline needs one video track, first")
    seen: set[str] = set()
    for clip in clips:
        if clip.kind not in ("video", "audio"):
            raise ValueError(f"unknown track kind {clip.kind!r}")
        if not TARGET_RE.match(clip.target):
            raise ValueError(f"media target not allowed: {clip.target!r}")
        if (clip.kind == "video") != clip.target.endswith(".mp4"):
            raise ValueError(f"wrong file type for a {clip.kind} track: {clip.target!r}")
        base = clip.target.lower()
        if base in seen:
            raise ValueError(f"two media files share a name: {clip.target!r}")
        seen.add(base)


def build_otio(name: str, fps: int, frames: int, clips: list[TimelineClip]) -> str:
    """The content.otio text: a Timeline.1 named `name` at `fps`, starting at
    01:00:00:00, with one track per clip, every clip from frame 0 to frame
    `frames`. Raises ValueError for anything the bundle cannot hold."""
    if fps not in OTIO_FPS:
        raise ValueError(f"unsupported frame rate {fps}")
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
        raise ValueError("a timeline needs at least one frame")
    if not math.isfinite(frames / fps):
        raise ValueError("the timeline's length is not finite")
    check_clips(clips)
    timeline = {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {},
        "name": name,
        "global_start_time": _time(TIMELINE_START_SEC * fps, fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "metadata": {},
            "name": "tracks",
            "source_range": None,
            "effects": [],
            "markers": [],
            "enabled": True,
            "children": [_track(c, frames, fps) for c in clips],
        },
    }
    return json.dumps(timeline, indent=4, ensure_ascii=False, allow_nan=False) + "\n"
