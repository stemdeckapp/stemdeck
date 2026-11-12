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

A file named in MIRRORED_FILES is fetched from the model mirror first
(config.MODEL_MIRROR, the same path under another host), with the same
timeout and tries. It is written beside its destination under a temporary
name and moved into place only once its size and full SHA-256 match the
table, so a wrong file never lands in the checkpoints folder. On any failure
there, one line on stderr says so and the original address is used exactly as
before. Meta's host served an expired certificate on 2026-11-12, which is what
made every first Split fail. A file not in the table is never mirrored.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.core.config import MODEL_DOWNLOAD_BACKOFF, MODEL_MIRROR

# The original host the mirror stands in for. Only its addresses are mirrored.
_ORIGIN = "https://dl.fbaipublicfiles.com/"
# A mirrored path: lowercase folders, then a torch.hub checkpoint name whose
# part after the last dash is the start of its SHA-256 (torch.hub's own rule).
_PATH_RE = re.compile(r"^(?:[a-z0-9_]+/)+(?P<name>[a-z0-9]+-(?P<prefix>[a-f0-9]{8,})\.th)$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CHUNK = 128 * 1024


@dataclass(frozen=True)
class MirroredFile:
    """One file the mirror holds: its path under both hosts, and what it must be."""

    path: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        m = _PATH_RE.match(self.path)
        if m is None or ".." in self.path:
            raise ValueError(f"not a mirrorable path: {self.path!r}")
        if not _SHA256_RE.match(self.sha256) or not self.sha256.startswith(m["prefix"]):
            raise ValueError(f"SHA-256 does not fit {self.path!r}")
        if self.size <= 0:
            raise ValueError(f"size must be positive for {self.path!r}")

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[1]


def _table(*files: MirroredFile) -> dict[str, MirroredFile]:
    return {_ORIGIN + f.path: f for f in files}


# Keyed by the original address, as torch.hub is handed it. Measured on
# 2026-11-12 against both hosts' copies of the file.
MIRRORED_FILES: dict[str, MirroredFile] = _table(
    # htdemucs_6s, the default separation model (config.DEMUCS_MODEL).
    MirroredFile(
        path="demucs/hybrid_transformer/5c90dfd2-34c22ccb.th",
        sha256="34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd",
        size=54_996_327,
    ),
)


class MirrorFileMismatch(Exception):
    """The mirror answered with a whole file that is not the one in the table.
    Asking again would get the same file, so it is never tried twice."""


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


class _RefuseRedirect(HTTPRedirectHandler):
    """The mirror answers with the file itself. A redirect would hand the
    download to a host the setting never named, so it fails as its HTTP status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _mirror_url(url: str, dst: str, hash_prefix: str | None) -> tuple[str, MirroredFile] | None:
    """The mirror's address for `url` and what the file must be, or None when
    the mirror is off or `url` is not in the table. Built from the table
    alone; `dst` must be the table's own file name, as torch.hub names it."""
    entry = MIRRORED_FILES.get(url)
    if MODEL_MIRROR is None or entry is None:
        return None
    if os.path.basename(dst) != entry.name:
        return None
    if hash_prefix is not None and not entry.sha256.startswith(hash_prefix):
        return None
    return f"{MODEL_MIRROR}/{entry.path}", entry


def _fetch_verified(url: str, dst: str, entry: MirroredFile, progress: bool) -> None:
    """Download `url` to `dst` only if it is exactly `entry`. A short body is a
    cut connection (tried again); a wrong length or hash is MirrorFileMismatch.
    Nothing is left behind but `dst` itself, and that only when it matched."""
    from torch.hub import tqdm  # torch.hub's own bar, or its stand-in without tqdm

    request = Request(url, headers={"User-Agent": "StemDeck"})
    with build_opener(_RefuseRedirect).open(request) as response:
        if response.status != 200:
            raise HTTPError(url, response.status, "not the whole file", response.headers, None)
        length = response.headers.get("Content-Length")
        if length is not None and length.strip() != str(entry.size):
            raise MirrorFileMismatch(f"length {length.strip()}, expected {entry.size}")
        folder = os.path.dirname(os.path.abspath(dst))
        tmp = os.path.join(folder, f"{entry.name}.{uuid.uuid4().hex}.partial")
        try:
            sha256 = hashlib.sha256()
            received = 0
            with (
                open(tmp, "xb") as out,
                tqdm(
                    total=entry.size,
                    disable=not progress,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                ) as bar,
            ):
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > entry.size:
                        raise MirrorFileMismatch(f"more than {entry.size} bytes")
                    out.write(chunk)
                    sha256.update(chunk)
                    bar.update(len(chunk))
            if received < entry.size:
                raise http.client.IncompleteRead(b"", entry.size - received)
            if sha256.hexdigest() != entry.sha256:
                raise MirrorFileMismatch("SHA-256 does not match")
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


@contextmanager
def bounded_downloads(read_timeout: int, attempts: int) -> Iterator[None]:
    """Inside the block, every torch.hub file download gives up after
    `read_timeout` seconds without a byte and is tried `attempts` times. A
    file in MIRRORED_FILES is tried from the mirror first, the same way."""
    import torch.hub

    download = torch.hub.download_url_to_file

    def bounded(url: str, dst: str, hash_prefix: str | None = None, progress: bool = True) -> None:
        folder = os.path.dirname(os.path.abspath(dst))
        mirrored = _mirror_url(url, dst, hash_prefix)
        if mirrored is not None:
            mirror_url, entry = mirrored
            try:
                fetch_with_retry(
                    lambda: _fetch_verified(mirror_url, dst, entry, progress),
                    url=mirror_url,
                    folder=folder,
                    attempts=attempts,
                    read_timeout=read_timeout,
                )
                return
            except (ModelDownloadError, MirrorFileMismatch) as exc:
                if isinstance(exc, ModelDownloadError) and exc.__cause__ is not None:
                    reason = _reason(exc.__cause__, read_timeout)
                else:
                    reason = str(exc)
                # On stderr, the worker's failure evidence. No "%" in it: the
                # parent reads a percentage there as download progress. The
                # Downloading line is torch.hub's own, said again so the parent
                # follows the second download as it did the first.
                sys.stderr.write(
                    f"model mirror failed ({reason.replace('%', '')}), "
                    "downloading from the original host\n"
                    f'Downloading: "{url}" to {dst}\n'
                )
                sys.stderr.flush()
        fetch_with_retry(
            lambda: download(url, dst, hash_prefix, progress),
            url=url,
            folder=folder,
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
