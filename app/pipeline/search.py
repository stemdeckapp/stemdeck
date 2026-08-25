"""Search YouTube and SoundCloud for something to import.

The topbar already takes a pasted link. This lets it take a query instead, so
finding a track does not mean leaving StemDeck, finding it in a browser, and
coming back with a URL.

Two things make this cheap enough to run while the user types:

  * Flat extraction. One request returns titles, durations, uploaders and
    thumbnails for the whole page of results, with no per-result round trip.
    Measured at roughly 1.1 s for YouTube and 2.0 s for SoundCloud.
  * Every result is a URL we already know how to validate, so nothing new has
    to be trusted. Results go back through validate_youtube_url /
    validate_playlist_url exactly as playlist entries do (#173), and anything
    that fails is dropped rather than shown.

The SSRF boundary is unchanged. Each search gets the narrowest extractor
allowlist that can serve it, and "generic" stays out of all of them.
"""

from __future__ import annotations

import logging
import urllib.parse

from yt_dlp import YoutubeDL

from app.core.settings import get_max_duration_sec
from app.pipeline.download import (
    InvalidPlaylistURL,
    InvalidYouTubeURL,
    _base_ydl_opts,
    validate_playlist_url,
    validate_youtube_url,
)

logger = logging.getLogger("stemdeck.search")

SOURCES = ("youtube", "soundcloud")
KINDS = ("track", "playlist")

# Enough to fill a dropdown without scrolling forever. Each result costs
# nothing extra (one request returns all of them), but the JSON and the DOM do.
DEFAULT_LIMIT = 8
MAX_LIMIT = 15

# Below this a query is too vague to be worth a round trip to YouTube.
MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200

# YouTube's own "Playlist" search filter, from the sp= parameter on the results
# page. There is no ytsearch: prefix for playlists, so the search URL extractor
# is the only route to them.
_YT_PLAYLIST_FILTER = "EgIQAw%3D%3D"

# One allowlist per (source, kind). Deliberately separate rather than one union:
# playlist search needs the tab extractor and track search must not have it.
_EXTRACTORS = {
    ("youtube", "track"): ["youtube:search", "youtube"],
    ("youtube", "playlist"): ["youtube:search_url", "youtube:tab", "youtube:playlist", "youtube"],
    ("soundcloud", "track"): ["soundcloud:search", "soundcloud"],
}


class UnsupportedSearch(ValueError):
    """The requested source/kind pair has no search behind it."""


def supported(source: str, kind: str) -> bool:
    """SoundCloud has no playlist search: yt-dlp exposes exactly one search key
    for it (scsearch, tracks only). The UI uses this to not offer a tab that
    cannot return anything."""
    return (source, kind) in _EXTRACTORS


def _target(source: str, kind: str, query: str, limit: int) -> str:
    if source == "youtube" and kind == "playlist":
        q = urllib.parse.quote(query)
        return f"https://www.youtube.com/results?search_query={q}&sp={_YT_PLAYLIST_FILTER}"
    prefix = "ytsearch" if source == "youtube" else "scsearch"
    return f"{prefix}{limit}:{query}"


def _entry_url(entry: dict, kind: str) -> str | None:
    """The importable URL for a result, or None if there is not one.

    Order matters and differs from expand_playlist's. SoundCloud search returns
    `url` as an api.soundcloud.com endpoint, which is not on the allowlisted
    host set and is rejected on sight; the real permalink is in `webpage_url`.
    Reading `url` first, as playlist expansion does, silently drops every
    SoundCloud result.
    """
    for key in ("webpage_url", "url"):
        raw = entry.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            return validate_playlist_url(raw) if kind == "playlist" else validate_youtube_url(raw)
        except (InvalidYouTubeURL, InvalidPlaylistURL):
            continue
    return None


def _thumbnail(entry: dict) -> str | None:
    """Smallest usable thumbnail. The dropdown renders these at ~64px wide, so
    pulling the 720p variant would cost bandwidth and decode time for nothing."""
    if isinstance(entry.get("thumbnail"), str):
        return entry["thumbnail"]
    thumbs = [t for t in (entry.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
    if not thumbs:
        return None
    thumbs.sort(key=lambda t: t.get("width") or 0)
    for t in thumbs:
        if (t.get("width") or 0) >= 120:
            return t["url"]
    return thumbs[-1]["url"]


def search(query: str, source: str, kind: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Run one search and return normalised results.

    Blocking: yt-dlp does network I/O. Callers must keep this off the event
    loop (see app/api/search.py).
    """
    query = (query or "").strip()
    if len(query) < MIN_QUERY_LEN:
        raise UnsupportedSearch("query is too short")
    query = query[:MAX_QUERY_LEN]
    if source not in SOURCES or kind not in KINDS:
        raise UnsupportedSearch("unknown source or kind")
    if not supported(source, kind):
        raise UnsupportedSearch(f"{source} has no {kind} search")

    limit = max(1, min(MAX_LIMIT, int(limit)))
    opts = {
        **_base_ydl_opts(_EXTRACTORS[(source, kind)]),
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noprogress": True,
        # Only meaningful for the search-URL route, which has no count in the
        # target string the way ytsearchN:/scsearchN: do.
        "playlistend": limit,
    }

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_target(source, kind, query, limit), download=False) or {}

    max_duration = get_max_duration_sec()
    items: list[dict] = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        url = _entry_url(entry, kind)
        if not url:
            continue
        duration = entry.get("duration")
        duration = float(duration) if isinstance(duration, int | float) else None
        items.append(
            {
                "url": url,
                "title": (entry.get("title") or "").strip() or url,
                "duration": duration,
                "uploader": (entry.get("uploader") or entry.get("channel") or "").strip() or None,
                "thumbnail": _thumbnail(entry),
                # Computed here rather than in the client so the limit is read
                # from one place. Over it, the pipeline refuses the job outright
                # (download.py), so the UI has to say "cannot import", not
                # "this may be slow".
                "too_long": duration is not None and duration > max_duration,
            }
        )
        if len(items) >= limit:
            break

    return {"items": items, "max_duration_sec": max_duration}
