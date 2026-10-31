"""Why a Lead + Backing split did not make its two stems (#832).

The split is a best-effort second pass over a finished song, so its failure
never fails the song: the Vocals stem stays as it was. That used to be the
whole story, and the person who asked for Lead + Backing got one Vocals lane
and no word about it. The job now carries one of these codes, and the client
says what happened from it in the person's own language.

The messages below are the API's plain-English fallback for any client that
has no table of its own. They are fixed strings on purpose: nothing from the
worker's stderr, no path and no traceback ever reaches a client through them.
"""

from __future__ import annotations

DEPENDENCY_MISSING = "dependency_missing"
MODEL_UNAVAILABLE = "model_unavailable"
OUT_OF_MEMORY = "out_of_memory"
WORKER_FAILED = "worker_failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
# The app stopped while the split ran, so it never finished.
INTERRUPTED = "interrupted"

MESSAGES: dict[str, str] = {
    DEPENDENCY_MISSING: (
        "A part StemDeck needs for Lead + Backing did not load on this install. "
        "Update StemDeck, then try again."
    ),
    MODEL_UNAVAILABLE: (
        "The Lead + Backing model could not be downloaded or loaded. "
        "Check the internet connection, then try again."
    ),
    OUT_OF_MEMORY: (
        "The computer ran out of memory during the split. Close other apps, then try again."
    ),
    WORKER_FAILED: "The split stopped with an error. Try again, or check the logs.",
    TIMEOUT: "The split stopped responding and was stopped. Try again.",
    CANCELLED: "The split was cancelled.",
    INTERRUPTED: "StemDeck closed before the split finished. Try again.",
}

CODES = frozenset(MESSAGES)


def message_for(code: str | None) -> str | None:
    """The safe sentence for ``code``, or None when there is no failure."""
    if code is None:
        return None
    return MESSAGES.get(code, MESSAGES[WORKER_FAILED])
