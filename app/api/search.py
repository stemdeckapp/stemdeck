"""Search endpoint for the topbar.

Fired while the user types, so the two things that matter are not spending a
round trip when an identical one just happened, and not letting a burst of
typing spawn unbounded work.

  * A short TTL cache absorbs the repeats. Word-boundary triggering means
    "daft", "daft punk", "daft punk around" are three separate queries, and
    backspacing walks straight back through them. A 60 s window turns the
    revisits into cache hits.
  * A semaphore caps how many yt-dlp searches can be in flight. The client
    aborts superseded requests, but an abort does not stop a thread that has
    already started, so the cap is what actually bounds the work.

Neither is a substitute for the client debouncing. Both exist because the
client cannot be trusted to be the only caller.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.settings import get_max_duration_sec
from app.pipeline.download import InvalidYouTubeURL
from app.pipeline.preview import PreviewUnavailable
from app.pipeline.preview import resolve as resolve_preview
from app.pipeline.search import (
    DEFAULT_LIMIT,
    KINDS,
    MAX_LIMIT,
    MIN_QUERY_LEN,
    SOURCES,
    UnsupportedSearch,
    search,
    supported,
)

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["search"])

# yt-dlp searches are ~1-2 s of blocking network I/O each. Three at once keeps
# a fast typist responsive without turning the thread pool into a queue of
# results nobody is waiting for any more.
_MAX_CONCURRENT = 3
_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

_CACHE_TTL_SEC = 60.0
_CACHE_MAX = 128
# key -> (expires_at, payload). Plain dict rather than functools.lru_cache: the
# entries expire on time, not on eviction pressure alone, and search results go
# stale in a way that a pure LRU would happily serve forever.
_cache: dict[tuple, tuple[float, dict]] = {}


class SearchRequest(BaseModel):
    query: str = Field(min_length=MIN_QUERY_LEN, max_length=200)
    source: str = "youtube"
    kind: str = "track"
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def _cache_get(key: tuple) -> dict | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    expires_at, payload = hit
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    return payload


def _cache_put(key: tuple, payload: dict) -> None:
    if len(_cache) >= _CACHE_MAX:
        # Drop whatever expires soonest. Cheap at this size, and it sheds the
        # entries closest to being useless rather than an arbitrary one.
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic() + _CACHE_TTL_SEC, payload)


def _clear_cache() -> None:
    """Test hook. Results are per-process and disposable, so nothing else needs
    to reach in here."""
    _cache.clear()


@router.post("")
async def search_sources(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}") from e
    try:
        payload = SearchRequest(**body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if payload.source not in SOURCES or payload.kind not in KINDS:
        raise HTTPException(status_code=422, detail="unknown source or kind")
    if not supported(payload.source, payload.kind):
        raise HTTPException(
            status_code=422,
            detail=f"{payload.source} has no {payload.kind} search",
        )

    query = payload.query.strip()
    # The duration limit is part of the key, not just part of the payload. It
    # decides each result's too_long verdict, and it is a live setting: raising
    # it in Settings has to un-grey the rows now, not once a 60 s entry expires.
    key = (
        payload.source,
        payload.kind,
        query.casefold(),
        payload.limit,
        get_max_duration_sec(),
    )
    cached = _cache_get(key)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        async with _semaphore:
            # Re-check: while queued behind the semaphore, an identical search
            # may have finished and filled the cache. Common when a burst of
            # keystrokes produces the same query twice.
            cached = _cache_get(key)
            if cached is not None:
                return {**cached, "cached": True}
            result = await asyncio.to_thread(
                search, query, payload.source, payload.kind, payload.limit
            )
    except UnsupportedSearch as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("search failed")
        raise HTTPException(status_code=502, detail="Could not reach that service") from e

    _cache_put(key, result)
    return {**result, "cached": False}


@router.get("/sources")
def search_sources_available() -> dict:
    """Which source/kind pairs actually return anything, so the UI does not
    offer a SoundCloud playlist tab that can only ever be empty."""
    return {
        "sources": [{"source": s, "kinds": [k for k in KINDS if supported(s, k)]} for s in SOURCES]
    }


# Preview audio is proxied, never redirected: the CSP allows `media-src 'self'`
# only, and widening it so an <audio> tag could reach googlevideo.com would let
# any injected string in the webview pull media from anywhere.
#
# Two previews at once is already generous for an audition button, and each one
# holds a socket open for as long as it plays.
_PREVIEW_CONCURRENCY = 2
_preview_semaphore = asyncio.Semaphore(_PREVIEW_CONCURRENCY)

# Big enough that a normal listen is a handful of reads, small enough that
# abandoning a preview stops promptly.
_CHUNK = 64 * 1024

_PREVIEW_TIMEOUT_SEC = 20


def _proxy(stream: dict, range_header: str | None):
    """Open the upstream stream and yield it in chunks.

    Range is forwarded rather than swallowed, so seeking the time bar costs one
    short request instead of pulling the track from the beginning again.
    """
    req = urllib.request.Request(stream["url"], headers=dict(stream["headers"]))
    if range_header:
        req.add_header("Range", range_header)
    upstream = urllib.request.urlopen(req, timeout=_PREVIEW_TIMEOUT_SEC)

    def body():
        try:
            while True:
                chunk = upstream.read(_CHUNK)
                if not chunk:
                    return
                yield chunk
        except (BrokenPipeError, ConnectionResetError, GeneratorExit):
            # The listener closed the tab or hit pause and seeked away. Normal.
            return
        finally:
            upstream.close()

    return upstream, body


@router.get("/preview")
async def preview(url: str, request: Request):
    """Stream a search result's audio so it can be auditioned before import."""
    try:
        stream = await asyncio.to_thread(resolve_preview, url)
    except InvalidYouTubeURL as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except PreviewUnavailable as e:
        raise HTTPException(status_code=404, detail="No preview available for this track") from e
    except Exception as e:
        logger.exception("preview resolve failed")
        raise HTTPException(status_code=502, detail="Could not load a preview") from e

    range_header = request.headers.get("range")
    try:
        async with _preview_semaphore:
            upstream, body = await asyncio.to_thread(_proxy, stream, range_header)
    except urllib.error.HTTPError as e:
        # A signed URL that expired between resolve and play, most often.
        raise HTTPException(status_code=502, detail="Could not load a preview") from e
    except Exception as e:
        logger.exception("preview proxy failed")
        raise HTTPException(status_code=502, detail="Could not load a preview") from e

    status = upstream.status if upstream.status in (200, 206) else 200
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
    for name in ("Content-Range", "Content-Length"):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value
    return StreamingResponse(body(), status_code=status, media_type=stream["mime"], headers=headers)
