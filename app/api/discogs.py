"""Discogs artists by name, for a band typed in the artist box.

GET /api/discogs/artist?q=<name> lists the artists Discogs finds for a name
Wikipedia did not know, and GET /api/discogs/artist/{id} is the one the user
picked, in the shape GET /api/jobs/{id}/artist-extra answers in, so the box
draws it the same way (static/js/artistDiscogs.js).

Nothing is taken on a name alone here: the user picks. Both answer 404 at once
when no Discogs token is saved, and ask Discogs nothing. The token is the
server's (settings.get_discogs_token): it goes in the Authorization header of
the requests app/pipeline/discogs.py makes and never into an answer. Each is
bounded by DISCOGS_SEARCH_BUDGET_SEC, and shares the process-wide limiter and
the on-disk cache with every other Discogs request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from app.core.config import DISCOGS_SEARCH_BUDGET_SEC
from app.core.settings import get_discogs_token
from app.pipeline import discogs

logger = logging.getLogger("stemdeck.artist")

router = APIRouter()

_ARTIST_ID_RE = re.compile(r"^[1-9]\d{0,11}$")


async def _bounded(func: Any, *args: Any) -> Any:
    """``func(*args, cancelled=...)`` in a thread, given up on after
    DISCOGS_SEARCH_BUDGET_SEC; the thread stops at its next request."""
    deadline = time.monotonic() + DISCOGS_SEARCH_BUDGET_SEC

    def cancelled() -> bool:
        return time.monotonic() > deadline

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, cancelled=cancelled), DISCOGS_SEARCH_BUDGET_SEC + 1
        )
    except asyncio.TimeoutError:
        logger.info("Discogs artist request timed out")
        return None


@router.get("/artist")
async def search_discogs_artists(q: str = "") -> dict[str, list[dict[str, Any]]]:
    """{"candidates": [{"id", "name", "profile"}]} for the name ``q``, empty
    when Discogs has none. 422 for a name that is empty, over
    discogs.QUERY_MAX_CHARS or holds control characters; 404 when no token
    is saved; 503 when Discogs could not be asked."""
    query = discogs.valid_query(q)
    if not query:
        raise HTTPException(status_code=422, detail="invalid name")
    token = get_discogs_token()
    if not token:
        raise HTTPException(status_code=404, detail="no Discogs token")
    try:
        found = await _bounded(discogs.search_artists, query, token)
    except Exception:
        logger.exception("Discogs artist search failed")
        found = None
    if found is None:
        raise HTTPException(status_code=503, detail="Discogs could not be reached")
    return {"candidates": found}


@router.get("/artist/{artist_id}")
async def get_discogs_artist(artist_id: str) -> dict[str, Any]:
    """The Discogs artist ``artist_id``, as GET /api/jobs/{id}/artist-extra
    answers, or 404 when the id is not one, no token is saved, or Discogs has
    no such artist or could not be asked."""
    if not _ARTIST_ID_RE.match(artist_id):
        raise HTTPException(status_code=404, detail="no artist details")
    token = get_discogs_token()
    if not token:
        raise HTTPException(status_code=404, detail="no artist details")
    try:
        answer = await _bounded(discogs.artist_by_id, int(artist_id), token)
    except Exception:
        logger.exception("Discogs artist failed")
        answer = None
    if answer is None:
        raise HTTPException(status_code=404, detail="no artist details")
    return answer
