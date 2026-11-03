"""Bounded model downloads (#125).

torch.hub fetches a checkpoint with urlopen() and no timeout, so a network that
opens the connection and then sends nothing blocks the caller forever, at 0%
CPU, with nothing on screen. bounded_downloads() gives every torch.hub download
inside it a read timeout and a few tries, and turns the last failure into a
ModelDownloadError that names the file's address and the folder a person can
put it in by hand. torch.hub only downloads a file that is not in that folder
yet, so a file put there is used as it is.

The timeout is the socket default, which every socket created inside the block
inherits, so it is set for the block only and put back after it: the worker's
pipes and anything the server does are never touched by it.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import sleep
from urllib.error import HTTPError, URLError

from app.core.config import MODEL_DOWNLOAD_BACKOFF

# torch.hub's own words for a file whose SHA-256 does not start with the prefix
# in its name (download_url_to_file). A proxy or captive portal that answers
# with a page of its own ends here, so it is a failed download like the others.
_HASH_MISMATCH = "invalid hash value"


class ModelDownloadError(RuntimeError):
    """A model file could not be downloaded after every try."""

    def __init__(self, url: str, folder: str, reason: str) -> None:
        # "model download failed:" is what errors.classify_failure matches. The
        # reason goes last: the job's error_detail keeps 200 characters, and a
        # long one (a proxy's TLS error) would otherwise cut off the folder.
        super().__init__(f"model download failed: {url}; put the file in {folder} ({reason})")


def _reason(exc: BaseException, read_timeout: int) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, TimeoutError):
        return f"no data for {read_timeout} s"
    if isinstance(exc, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (ConnectionError, http.client.IncompleteRead)):
        return "connection closed early"
    if isinstance(exc, RuntimeError):
        return "file did not match its checksum"
    return str(exc) or type(exc).__name__


def _is_download_failure(exc: Exception) -> bool:
    # A full disk or a folder that cannot be written is an OSError too, and is
    # not a network problem a retry or another network could solve. A TLS
    # connection cut mid-file raises a bare ssl.SSLError: URLError only wraps
    # what goes wrong while connecting.
    if isinstance(exc, RuntimeError):
        return str(exc).startswith(_HASH_MISMATCH)
    return isinstance(
        exc, (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException)
    )


def fetch_with_retry(
    fn: Callable[[], None], *, url: str, folder: str, attempts: int, read_timeout: int
) -> None:
    """Call `fn` until it returns, at most `attempts` times, pausing between
    tries. A download failure on the last try raises ModelDownloadError; any
    other error propagates as it is, at once."""
    for attempt in range(1, attempts + 1):
        try:
            fn()
            return
        except Exception as exc:
            if not _is_download_failure(exc):
                raise
            reason = _reason(exc, read_timeout)
            if attempt == attempts:
                raise ModelDownloadError(url, folder, reason) from exc
            delay = MODEL_DOWNLOAD_BACKOFF[min(attempt, len(MODEL_DOWNLOAD_BACKOFF)) - 1]
            # On stderr, which the separation worker's parent reads: the line
            # shows in the failure's evidence, and it resets the parent's stall
            # timer, so the stall limit has to outlast one try, not all of them.
            sys.stderr.write(
                f"model download try {attempt} of {attempts} failed ({reason}), "
                f"trying again in {delay} s\n"
            )
            sys.stderr.flush()
            sleep(delay)


@contextmanager
def bounded_downloads(read_timeout: int, attempts: int) -> Iterator[None]:
    """Inside the block, every torch.hub file download gives up after
    `read_timeout` seconds without a byte and is tried `attempts` times."""
    import torch.hub

    download = torch.hub.download_url_to_file

    def bounded(url: str, dst: str, hash_prefix: str | None = None, progress: bool = True) -> None:
        fetch_with_retry(
            lambda: download(url, dst, hash_prefix, progress),
            url=url,
            folder=os.path.dirname(os.path.abspath(dst)),
            attempts=attempts,
            read_timeout=read_timeout,
        )

    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(read_timeout)
    # load_state_dict_from_url, which demucs calls, looks this name up in
    # torch.hub when it runs, so the replacement is what it calls.
    torch.hub.download_url_to_file = bounded
    try:
        yield
    finally:
        torch.hub.download_url_to_file = download
        socket.setdefaulttimeout(previous)
