"""Whether Discogs accepts the user's personal access token.

Asked once, when the token is saved in Settings (POST /api/settings), so a
token Discogs refuses is caught there rather than leaving every band lookup
to fail without a word. Asked of /oauth/identity, which answers any valid
token with its user and refuses any other with 401.

The token is sent only in the Authorization header: never in a URL, never
logged, and never in an exception message.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request

from app.core.config import DISCOGS_IDENTITY_URL, MUSICBRAINZ_USER_AGENT, TIMEOUT_DISCOGS_CHECK
from app.pipeline.artist_lookup import _ssl_context

logger = logging.getLogger(__name__)

# An identity answer is a few hundred bytes; nothing past this is read.
_MAX_BYTES = 64 * 1024


def _ask_identity(token: str) -> int:
    """The HTTP status Discogs answers /oauth/identity with for ``token``.
    Raises on anything that is not an answer (offline, timeout)."""
    request = urllib.request.Request(
        DISCOGS_IDENTITY_URL,
        headers={
            "Authorization": f"Discogs token={token}",
            "Accept": "application/json",
            # Discogs refuses clients that do not name themselves.
            "User-Agent": MUSICBRAINZ_USER_AGENT,
        },
    )
    try:
        # A fixed https URL, never one from a request or a tag (B310).
        with urllib.request.urlopen(  # nosec B310
            request, timeout=TIMEOUT_DISCOGS_CHECK, context=_ssl_context()
        ) as response:
            response.read(_MAX_BYTES)
            return int(response.status)
    except urllib.error.HTTPError as err:
        return int(err.code)


def _verdict(token: str) -> bool | None:
    try:
        status = _ask_identity(token)
    except Exception as err:
        # The type only: an exception from urllib never holds the header, but
        # nothing about this request is worth the risk of logging more.
        logger.info("Discogs could not be asked whether the token works (%s)", type(err).__name__)
        return None
    if status == 401:
        return False
    if 200 <= status < 300:
        return True
    # A 429 or a 5xx says nothing about the token.
    return None


def discogs_token_works(token: str) -> bool | None:
    """Whether Discogs accepts ``token``: True or False, or None when it
    cannot be asked (offline, Discogs down or too slow), and the token is then
    kept on trust.

    The Save waits for this, so it waits at most twice the request timeout:
    the request's own timeout bounds each read, not the whole answer. The
    request is left to finish on its own thread."""
    result: list[bool | None] = [None]

    def ask() -> None:
        result[0] = _verdict(token)

    worker = threading.Thread(target=ask, name="discogs-token-check", daemon=True)
    worker.start()
    worker.join(TIMEOUT_DISCOGS_CHECK * 2)
    if worker.is_alive():
        logger.info("Discogs took too long to say whether the token works")
        return None
    return result[0]
