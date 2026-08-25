"""Resolve a playable audio stream for a search result, so it can be auditioned
before committing minutes of separation to it.

The stream cannot be handed to the page directly. The CSP allows `media-src
'self' blob: data:` only, so a googlevideo.com URL in an <audio> tag is blocked,
and widening that would let any injected string in the webview pull media from
anywhere. The bytes are proxied instead, which keeps the CSP where it is and
means the browser never talks to YouTube at all.

Two things make it affordable:

  * Resolving is the expensive half (a real extraction, one to two seconds) and
    the result is stable for hours, so resolved URLs are cached.
  * The proxy forwards Range, so the browser pulls only what it plays. Opening
    a preview costs a few hundred KB, not the whole track.

Only progressive HTTP formats are accepted. HLS would need a manifest rewrite
and a segment proxy, which is a lot of machinery for an audition button.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from yt_dlp import YoutubeDL

from app.pipeline.download import _ALLOWED_EXTRACTORS, _base_ydl_opts, validate_youtube_url

logger = logging.getLogger("stemdeck.preview")


class PreviewUnavailable(RuntimeError):
    """No progressive audio stream could be resolved for this URL."""


# Resolved media URLs are signed and time-limited (YouTube's run about six
# hours). Twenty minutes is comfortably inside that while still not pinning a
# stale URL for a whole session.
_TTL_SEC = 20 * 60
_MAX_ENTRIES = 64

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}

# Anything not on http(s) needs a player, not a byte proxy.
_PROGRESSIVE = ("https", "http")


def _cache_get(url: str) -> dict | None:
    with _lock:
        hit = _cache.get(url)
        if hit is None:
            return None
        expires, payload = hit
        if expires < time.monotonic():
            _cache.pop(url, None)
            return None
        return payload


def _cache_put(url: str, payload: dict) -> None:
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[url] = (time.monotonic() + _TTL_SEC, payload)


def _clear_cache() -> None:
    with _lock:
        _cache.clear()


def _pick_format(info: dict) -> dict | None:
    """Smallest progressive audio-only format that will actually play.

    Smallest, not best: this is an audition. The lowest bitrate audio stream is
    a fraction of the bytes and indistinguishable for deciding whether it is
    the right song.
    """
    candidates = []
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        if f.get("protocol") not in _PROGRESSIVE:
            continue
        if f.get("acodec") in (None, "none"):
            continue
        # Audio-only. A progressive video format would drag the whole picture
        # down the wire to play a sound.
        if f.get("vcodec") not in (None, "none"):
            continue
        candidates.append(f)
    if not candidates:
        return None
    candidates.sort(key=lambda f: f.get("abr") or f.get("tbr") or 9999)
    return candidates[0]


def resolve(url: str) -> dict:
    """Resolve `url` to a proxyable audio stream.

    Returns {"url", "mime", "size", "headers", "title", "duration"}. Blocking:
    callers must keep this off the event loop.
    """
    url = validate_youtube_url(url)
    cached = _cache_get(url)
    if cached is not None:
        return cached

    opts = {
        **_base_ydl_opts(_ALLOWED_EXTRACTORS),
        "noplaylist": True,
        "skip_download": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    fmt = _pick_format(info)
    if not fmt:
        raise PreviewUnavailable("no progressive audio stream")

    # The proxy opens this URL, so the scheme is checked here rather than
    # trusted. _pick_format reads yt-dlp's `protocol` field, which is metadata
    # about the format and not a guarantee about the string: a file:// or a
    # custom scheme reaching urlopen would be read off the host's disk and
    # streamed straight to the client. Cheap to assert, and the assertion is
    # what makes the nosec on the urlopen honest.
    if urllib.parse.urlparse(fmt["url"]).scheme not in _PROGRESSIVE:
        raise PreviewUnavailable("resolved stream is not http(s)")

    payload = {
        "url": fmt["url"],
        # yt-dlp knows the container; the browser needs a usable type to decode.
        "mime": _mime_for(fmt),
        "size": fmt.get("filesize") or fmt.get("filesize_approx"),
        # These are not decoration. A googlevideo URL fetched without the
        # headers yt-dlp negotiated is frequently refused.
        "headers": {k: v for k, v in (fmt.get("http_headers") or {}).items() if v},
        "title": info.get("title") or "",
        "duration": info.get("duration"),
    }
    _cache_put(url, payload)
    return payload


_MIME_BY_EXT = {
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
}


def _mime_for(fmt: dict) -> str:
    ext = (fmt.get("ext") or "").lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    acodec = (fmt.get("acodec") or "").lower()
    if "opus" in acodec:
        return "audio/webm"
    if "mp4a" in acodec or "aac" in acodec:
        return "audio/mp4"
    return "audio/mpeg"
