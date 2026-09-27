"""What a source says about itself: artist, title, album, and lyrics (#699).

Read once at import and kept on the job as ``audio_tags``, for the Lyrics tab
and the artist box. The library otherwise records only a title, and a title
names the song as often as the band, so a file's own tags are the best answer
there is to "who is this and what are the words".

Two sources, one shape:

* an upload's container tags, read with ffprobe before the upload is deleted
  (the stems carry no tags, and the source goes once the pipeline is done);
* a link's metadata from yt-dlp, which fills artist, track and album for
  releases YouTube knows as music, and otherwise the channel, when the video
  itself corroborates that the channel is the artist's.

Everything here is optional. A probe that fails, times out or finds nothing
gives ``None`` and the import goes ahead exactly as before.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import (
    AUDIO_TAG_LYRICS_MAX_CHARS,
    AUDIO_TAG_MAX_CHARS,
    TIMEOUT_PROBE_TAGS,
    ffprobe_executable,
)

logger = logging.getLogger(__name__)

# ffprobe reports an MP3's lyrics frame as "lyrics-<language>" ("lyrics-eng"),
# a FLAC's as "LYRICS" or "UNSYNCEDLYRICS", an M4A's as "lyrics". Keys are
# compared lower-cased.
_LYRICS_KEYS = ("lyrics", "unsyncedlyrics", "syncedlyrics")


def _clean(value: Any, limit: int) -> str:
    """A tag value as plain text: control characters other than line breaks
    dropped, trimmed, and cut at ``limit``. Tags are written by anyone, and
    this text ends up in the page."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch in "\n\t" or ch >= " ")
    return text.replace("\r\n", "\n").strip()[:limit]


def _first(tags: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = _clean(tags.get(key), AUDIO_TAG_MAX_CHARS)
        if value:
            return value
    return ""


def tags_from_probe(raw: dict[str, Any] | None) -> dict[str, str] | None:
    """Artist, title, album and lyrics from ffprobe's ``format.tags``, or None
    when the file names none of them."""
    if not isinstance(raw, dict):
        return None
    tags = {str(k).lower(): v for k, v in raw.items() if isinstance(v, str)}
    lyrics = ""
    for key, value in tags.items():
        if key in _LYRICS_KEYS or key.startswith("lyrics-") or key.startswith("lyrics "):
            lyrics = _clean(value, AUDIO_TAG_LYRICS_MAX_CHARS)
            if lyrics:
                break
    found = {
        "artist": _first(tags, "artist", "album_artist", "albumartist", "performer"),
        "title": _first(tags, "title"),
        "album": _first(tags, "album"),
        "lyrics": lyrics,
    }
    found = {k: v for k, v in found.items() if v}
    return found or None


# YouTube Music's auto-generated channels are named "<Artist> - Topic", and
# only ever carry that artist's releases.
_TOPIC_SUFFIX = " - Topic"
# "Artist - Title", with the separators people actually type: a hyphen, an en
# or em dash or a pipe with space either side, or a colon followed by one.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[-\N{EN DASH}\N{EM DASH}|]\s+|\s*:\s+")
_NON_WORD_RE = re.compile(r"[\W_]+")
# What an artist's own channel tends to append to their name: "NirvanaVEVO",
# "Nirvana Official". Compared on the squashed key, so spacing is irrelevant.
_CHANNEL_SUFFIXES = ("vevo", "official")


def _name_key(text: str) -> str:
    """A name reduced for comparison: case-folded, punctuation and spacing gone."""
    return _NON_WORD_RE.sub("", text.casefold())


def _channel_key(channel: str) -> str:
    key = _name_key(channel)
    stripped = True
    while stripped:
        stripped = False
        for suffix in _CHANNEL_SUFFIXES:
            if key.endswith(suffix):
                key = key.removesuffix(suffix)
                stripped = True
    return key


def _artist_from_channel(info: dict[str, Any]) -> tuple[str, str] | None:
    """(artist, title) from the channel, only when something corroborates it.

    A bare channel name is not evidence: labels, compilers and fans upload
    other people's songs all the time. It counts when the channel is YouTube's
    auto-generated "Artist - Topic", or when the video's own title starts with
    the channel's name ("Nirvana - Lithium (Official Music Video)" on the
    NirvanaVEVO channel). The title keeps whatever noise follows; the page
    cleans that up.
    """
    video_title = _clean(info.get("title"), AUDIO_TAG_MAX_CHARS)
    channels: list[str] = []
    for field in ("channel", "uploader"):
        name = _clean(info.get(field), AUDIO_TAG_MAX_CHARS)
        if name and name not in channels:
            channels.append(name)
    for channel in channels:
        if channel.endswith(_TOPIC_SUFFIX):
            artist = channel.removesuffix(_TOPIC_SUFFIX).strip()
            if artist:
                return artist, video_title
            continue
        key = _channel_key(channel)
        if not key:
            continue
        for match in _TITLE_SEPARATOR_RE.finditer(video_title):
            prefix = video_title[: match.start()].strip()
            if _name_key(prefix) == key:
                rest = video_title[match.end() :].strip()
                if rest:
                    return prefix, rest
                break
    return None


def tags_from_ytdlp(info: dict[str, Any] | None) -> dict[str, str] | None:
    """Artist, track and album from yt-dlp's info, or None.

    The music fields first, and they always win. Only when they name no
    artist does the channel count, and then only when corroborated (see
    _artist_from_channel): the channel alone is as often a label ("Roadrunner
    Records") or a fan's upload, and a wrong artist is worse than none because
    it gets searched for.
    """
    if not isinstance(info, dict):
        return None
    artists = info.get("artists")
    artist = _clean(info.get("artist"), AUDIO_TAG_MAX_CHARS) or (
        _clean(artists[0], AUDIO_TAG_MAX_CHARS) if isinstance(artists, list) and artists else ""
    )
    title = _clean(info.get("track"), AUDIO_TAG_MAX_CHARS)
    if not artist and (corroborated := _artist_from_channel(info)) is not None:
        artist = corroborated[0]
        title = title or corroborated[1]
    found = {
        "artist": artist,
        "title": title,
        "album": _clean(info.get("album"), AUDIO_TAG_MAX_CHARS),
    }
    found = {k: v for k, v in found.items() if v}
    # An album alone says nothing useful about who or what this is.
    if not found.get("artist") and not found.get("title"):
        return None
    return found


def probe_tags(path: Path) -> dict[str, str] | None:
    """Read an upload's tags. Never raises: tags are not worth failing an
    upload over, so every failure is logged and gives None."""
    try:
        result = subprocess.run(
            [
                ffprobe_executable(),
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags",
                str(path),
            ],
            capture_output=True,
            # The same reason as _probe_duration in api/jobs.py: tags are
            # arbitrary text, and the Windows locale codec would choke on it.
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_PROBE_TAGS,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not read tags from %s", path.name, exc_info=True)
        return None
    if result.returncode != 0:
        logger.info("ffprobe found no readable tags in %s", path.name)
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        logger.info("ffprobe tag output for %s was not JSON", path.name)
        return None
    fmt = data.get("format") if isinstance(data, dict) else None
    return tags_from_probe(fmt.get("tags") if isinstance(fmt, dict) else None)
