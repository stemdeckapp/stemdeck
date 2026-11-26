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

fetch_verified_file() is the same verified download for a checkpoint StemDeck
fetches itself rather than through torch.hub (the Mel-Band RoFormer vocal
model, app/core/stem_models.py): its address is pinned to a revision, and a
redirect is followed only to another https address, since Hugging Face answers
with one to its CDN. The file's integrity comes from its SHA-256, not from the
host that served it.

Demucs' checkpoints are pickles that torch.hub loads in full, and torch.hub
checks only the first 8 hex characters of their SHA-256 (the part of the name
after the dash). Every one of them in the registry (stem_models.hub_files())
is pinned by its full SHA-256 and size: inside bounded_downloads its download,
from the original host as from the mirror, is kept only when it matches, and
drop_unverified() removes a cached copy that does not match before a model is
loaded, so torch.hub fetches it again through that check. A file someone put
in the folder by hand is used when it matches.

A download cut short by a hard kill leaves its ".partial" beside the file (up
to 913 MB); the next verified download into that folder removes the ones that
are old enough to be no one's (sweep_partials).
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.core import stem_models
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


class _HttpsRedirectOnly(HTTPRedirectHandler):
    """Follows a redirect only to another https address. Hugging Face answers
    a pinned file's address with a redirect to its CDN; one to plain http, or
    to any other scheme, fails as its HTTP status."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_verified(
    url: str,
    dst: str,
    sha256: str,
    size: int,
    progress: bool,
    redirects: type[HTTPRedirectHandler],
    timeout: float | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """Download `url` to `dst` only if it is exactly `size` bytes with SHA-256
    `sha256`. A short body is a cut connection (tried again); a wrong length or
    hash is MirrorFileMismatch. Nothing is left behind but `dst` itself, and
    that only when it matched, flushed to the disk before it is moved into
    place: a crash right after the move must not leave a truncated file under
    the final name, which every later load would take as complete.
    ``on_bytes`` is told the bytes received so far after every chunk."""
    from torch.hub import tqdm  # torch.hub's own bar, or its stand-in without tqdm

    from app.core.settings import fsync_dir

    request = Request(url, headers={"User-Agent": "StemDeck"})
    opener = build_opener(redirects)
    response = opener.open(request) if timeout is None else opener.open(request, timeout=timeout)
    with response:
        if response.status != 200:
            raise HTTPError(url, response.status, "not the whole file", response.headers, None)
        length = response.headers.get("Content-Length")
        if length is not None and length.strip() != str(size):
            raise MirrorFileMismatch(f"length {length.strip()}, expected {size}")
        folder = os.path.dirname(os.path.abspath(dst))
        tmp = os.path.join(folder, f"{os.path.basename(dst)}.{uuid.uuid4().hex}.partial")
        try:
            digest = hashlib.sha256()
            received = 0
            with (
                open(tmp, "xb") as out,
                tqdm(
                    total=size,
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
                    if received > size:
                        raise MirrorFileMismatch(f"more than {size} bytes")
                    out.write(chunk)
                    digest.update(chunk)
                    bar.update(len(chunk))
                    if on_bytes is not None:
                        on_bytes(received)
                out.flush()
                os.fsync(out.fileno())
            if received < size:
                raise http.client.IncompleteRead(b"", size - received)
            if digest.hexdigest() != sha256:
                raise MirrorFileMismatch("SHA-256 does not match")
            os.replace(tmp, dst)
            fsync_dir(Path(folder))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _fetch_verified(url: str, dst: str, entry: MirroredFile, progress: bool) -> None:
    """Download `url` from the mirror to `dst` only if it is exactly `entry`.
    The mirror answers with the file itself, so a redirect is refused."""
    _download_verified(url, dst, entry.sha256, entry.size, progress, _RefuseRedirect)


# A ".partial" this module, or torch.hub, writes beside a download: the file's
# name, a uuid4 in hex, ".partial".
_PARTIAL_RE = re.compile(r"^(?P<name>.+)\.[0-9a-f]{32}\.partial\Z")
# A partial this old is no one's: a download that is running writes to it at
# least every read timeout, and a stalled one is given up long before.
PARTIAL_MAX_AGE_SEC = 600


def sweep_partials(folder: str | os.PathLike[str], name: str) -> list[str]:
    """Remove the ".partial" leftovers of ``name`` in ``folder`` that a killed
    download left: exactly "<name>.<32 hex>.partial", a regular file and not
    a link, last written over PARTIAL_MAX_AGE_SEC ago. Returns the names
    removed. Never raises: a leftover that cannot go is only disk space."""
    removed: list[str] = []
    now = time.time()
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return removed
    for entry in entries:
        m = _PARTIAL_RE.match(entry.name)
        if m is None or m["name"] != name:
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime < PARTIAL_MAX_AGE_SEC:
                continue
            os.remove(entry.path)
            removed.append(entry.name)
        except OSError:
            continue
    return removed


def file_matches(path: str | os.PathLike[str], sha256: str, size: int) -> bool:
    """Whether the file at ``path`` is exactly ``size`` bytes with SHA-256
    ``sha256``. A missing or unreadable file does not match."""
    try:
        if os.stat(path).st_size != size:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == sha256


def drop_unverified(model_id: str) -> list[str]:
    """Remove each cached torch.hub checkpoint of ``model_id`` that is not
    exactly its pinned file (a copy fetched before pins existed, or one cut
    short), so torch.hub downloads it again inside bounded_downloads, where
    the download is verified. Returns the paths removed; a file that cannot
    be removed is left, and torch.hub loads it as before."""
    model = stem_models.MODELS.get(model_id)
    if model is None:
        return []
    removed: list[str] = []
    for f in model.files:
        if f.root != "torch_hub":
            continue
        path = stem_models.model_path(f)
        if not path.exists() or file_matches(path, f.sha256, f.size):
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    return removed


def _pinned(url: str) -> tuple[str, int] | None:
    """(SHA-256, size) a download from ``url`` must have, when it is pinned."""
    entry = MIRRORED_FILES.get(url)
    if entry is not None:
        return entry.sha256, entry.size
    hub = stem_models.hub_files().get(url)
    return (hub.sha256, hub.size) if hub is not None else None


def fetch_verified_file(
    url: str,
    dst: str,
    sha256: str,
    size: int,
    *,
    read_timeout: int,
    attempts: int,
    progress: bool = True,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """Download `url` to `dst`, kept only when it is exactly `size` bytes with
    SHA-256 `sha256`. Redirects are followed to https addresses only. A cut
    connection is tried again, up to `attempts` times, each read given up
    after `read_timeout` seconds without a byte. ``on_bytes`` is told the
    bytes the current try has received, after every chunk.

    Raises ModelDownloadError, naming the address and the folder to put the
    file in by hand, when every try failed, or at once when the file that
    arrived is not the one asked for: asking again would get the same file.
    Nothing but `dst` is ever left behind."""
    if not _SHA256_RE.match(sha256) or size <= 0:
        raise ValueError("a verified download needs a SHA-256 and a size")
    folder = os.path.dirname(os.path.abspath(dst))
    os.makedirs(folder, exist_ok=True)
    sweep_partials(folder, os.path.basename(dst))
    try:
        fetch_with_retry(
            lambda: _download_verified(
                url,
                dst,
                sha256,
                size,
                progress,
                _HttpsRedirectOnly,
                timeout=read_timeout,
                on_bytes=on_bytes,
            ),
            url=url,
            folder=folder,
            attempts=attempts,
            read_timeout=read_timeout,
        )
    except MirrorFileMismatch as exc:
        raise ModelDownloadError(url, folder, "file did not match its checksum") from exc


@contextmanager
def bounded_downloads(read_timeout: int, attempts: int) -> Iterator[None]:
    """Inside the block, every torch.hub file download gives up after
    `read_timeout` seconds without a byte and is tried `attempts` times. A
    file in MIRRORED_FILES is tried from the mirror first, the same way. A
    pinned file (MIRRORED_FILES, stem_models.hub_files()) is kept only when
    its full SHA-256 and size match, from either host."""
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
        pinned = _pinned(url)
        if pinned is not None:
            sha256, size = pinned
            sweep_partials(folder, os.path.basename(dst))
            try:
                fetch_with_retry(
                    lambda: _download_verified(
                        url, dst, sha256, size, progress, _HttpsRedirectOnly
                    ),
                    url=url,
                    folder=folder,
                    attempts=attempts,
                    read_timeout=read_timeout,
                )
            except MirrorFileMismatch as exc:
                raise ModelDownloadError(url, folder, "file did not match its checksum") from exc
            return
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
