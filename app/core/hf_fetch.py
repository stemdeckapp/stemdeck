"""Fetching a user model from Hugging Face (My models, "Add by link").

The link the user pastes is parsed, never fetched as it is. Only an https
link to huggingface.co (no user name, no port but 443, ASCII) naming a model
repository is accepted, optionally with /tree/<rev>, /blob/<rev>/<file> or
/resolve/<rev>/<file>; its query and fragment are dropped. Every request is
then built here from the parts that passed:

  https://huggingface.co/api/models/<owner>/<repo>/revision/<rev>?blobs=true
      the repository's commit and file list (JSON, read up to HF_API_MAX_BYTES)
  https://huggingface.co/<owner>/<repo>/resolve/<commit>/<file>
      each file, pinned to that commit, so a branch that moves while a model
      downloads cannot swap the file under it

Redirects: at most MAX_REDIRECTS, each to https on port 443 and a host in
ALLOWED_HOSTS or under ALLOWED_SUFFIXES (a dot-boundary match, so
"evilhuggingface.co" is not one). Hugging Face answers a file's address with
a redirect to its file CDN: cdn-lfs.huggingface.co and cdn-lfs.hf.co for
LFS files, cas-bridge.xethub.hf.co for Xet storage (huggingface_hub's own
constants name hf.co as the Hub's short domain; tests/test_model_download.py
follows the same cdn-lfs.hf.co redirect for Kim's checkpoint). Any other
redirect fails as "network".

No credentials are ever sent: no Authorization header, no HF_TOKEN. A gated
or private repository answers 401 or 403, which reads as "gated" (the user can
download the files and add them from disk). Every file is capped while it
streams, whatever its Content-Length says or does not say. Certificate
checks are urllib's defaults. Nothing here blocks addresses by IP: the host
allowlist plus TLS is the control, and a split-horizon DNS would otherwise
break the one host this supports.

Failures are Refused(code) from app.core.user_model_format; Cancelled when the
caller's event is set. No message carries the link or a path.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.core.config import (
    HF_API_MAX_BYTES,
    MODEL_DOWNLOAD_ATTEMPTS,
    MODEL_DOWNLOAD_BACKOFF,
    TIMEOUT_MODEL_DOWNLOAD_READ,
)
from app.core.user_model_format import Refused, display_text

logger = logging.getLogger("stemdeck.hf_fetch")

# The only host a pasted link may name.
LINK_HOST = "huggingface.co"
# Where requests go. Tests point these at a local server; nothing else does.
_BASE = "https://huggingface.co"
_SCHEMES: tuple[str, ...] = ("https",)
_PORTS: tuple[int | None, ...] = (None, 443)
# Hosts a request may be redirected to.
ALLOWED_HOSTS = frozenset({"huggingface.co", "hf.co"})
ALLOWED_SUFFIXES: tuple[str, ...] = (".huggingface.co", ".hf.co")
MAX_REDIRECTS = 3
MAX_LINK_CHARS = 300
_CHUNK = 1024 * 1024

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_REV_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_COMMIT_RE = re.compile(r"^[a-f0-9]{40}\Z")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}\Z")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}\Z")
MAX_FILE_SEGMENTS = 3
# First path parts that are Hugging Face pages, not an owner of a model.
_RESERVED_OWNERS = frozenset(
    {"api", "datasets", "spaces", "docs", "models", "settings", "join", "login", "blog"}
)

WEIGHTS_PREFERRED = (".safetensors",)
WEIGHTS_TORCH = (".ckpt", ".pth", ".pt")
CONFIG_EXTS = (".yaml", ".yml")


class Cancelled(Exception):
    """The caller's cancel event was set."""


@dataclass(frozen=True)
class HfLink:
    owner: str
    repo: str
    rev: str
    file: str | None = None

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}"

    def canonical(self) -> str:
        """The link rebuilt from its checked parts (what the registry keeps)."""
        base = f"https://{LINK_HOST}/{self.owner}/{self.repo}"
        if self.file is not None:
            return f"{base}/blob/{quote(self.rev, safe='')}/{quote(self.file, safe='/')}"
        if self.rev != "main":
            return f"{base}/tree/{quote(self.rev, safe='')}"
        return base


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int | None
    sha256: str | None


@dataclass
class RepoInfo:
    commit: str
    files: dict[str, RemoteFile] = field(default_factory=dict)
    license: str | None = None


# ── the link ──


def _rev_ok(rev: str) -> bool:
    return bool(_REV_RE.match(rev)) and rev not in (".", "..") and ".." not in rev


def _file_ok(path: str) -> bool:
    parts = path.split("/")
    return 1 <= len(parts) <= MAX_FILE_SEGMENTS and all(_SEGMENT_RE.match(p) for p in parts)


def parse_link(text: object) -> HfLink:
    """The checked parts of a pasted Hugging Face link. Raises
    Refused("bad_link")."""
    if not isinstance(text, str):
        raise Refused("bad_link")
    raw = text.strip()
    if not raw or len(raw) > MAX_LINK_CHARS or not raw.isascii():
        raise Refused("bad_link")
    if any(ch.isspace() for ch in raw) or "\\" in raw:
        raise Refused("bad_link")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise Refused("bad_link") from exc
    host = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    if parts.scheme.lower() != "https" or host != LINK_HOST:
        raise Refused("bad_link")
    if "@" in netloc or port not in (None, 443) or netloc not in (LINK_HOST, f"{LINK_HOST}:443"):
        raise Refused("bad_link")
    segments = parts.path.split("/")
    if not segments or segments[0] != "":
        raise Refused("bad_link")
    segments = segments[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    segments = [unquote(s) for s in segments]
    if len(segments) < 2:
        raise Refused("bad_link")
    owner, repo, rest = segments[0], segments[1], segments[2:]
    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo) or owner.lower() in _RESERVED_OWNERS:
        raise Refused("bad_link")
    if ".." in owner or ".." in repo:
        raise Refused("bad_link")
    if not rest:
        return HfLink(owner, repo, "main")
    kind = rest[0]
    if kind == "tree" and len(rest) == 2 and _rev_ok(rest[1]):
        return HfLink(owner, repo, rest[1])
    if kind in ("blob", "resolve") and len(rest) >= 3 and _rev_ok(rest[1]):
        path = "/".join(rest[2:])
        if _file_ok(path):
            return HfLink(owner, repo, rest[1], path)
    raise Refused("bad_link")


def api_url(link: HfLink) -> str:
    return (
        f"{_BASE}/api/models/{link.owner}/{link.repo}/revision/"
        f"{quote(link.rev, safe='')}?blobs=true"
    )


def file_url(link: HfLink, commit: str, path: str) -> str:
    if not _COMMIT_RE.match(commit) or not _file_ok(path):
        raise Refused("bad_link")
    return f"{_BASE}/{link.owner}/{link.repo}/resolve/{commit}/{quote(path, safe='/')}"


# ── requests ──


def target_ok(url: str) -> bool:
    """Whether a request may go to ``url``: an allowed scheme and port, no
    user name, and an allowed host (exactly, or under an allowed suffix)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in _SCHEMES or port not in _PORTS:
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    host = parts.hostname or ""
    if not host or not host.isascii() or host.endswith("."):
        return False
    host = host.lower()
    return host in ALLOWED_HOSTS or any(
        host.endswith(sfx) and len(host) > len(sfx) for sfx in ALLOWED_SUFFIXES
    )


class _AllowedRedirects(HTTPRedirectHandler):
    """Follows at most MAX_REDIRECTS redirects, each to an allowed target.
    Any other one fails as its HTTP status, which reads as "network"."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not target_ok(newurl):
            logger.warning("my models: a download was redirected to a host that is not allowed")
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    """One GET, built here, with no credentials. A seam for tests."""
    if not target_ok(url):
        raise Refused("network")
    request = Request(url, headers={"User-Agent": "StemDeck", "Accept-Encoding": "identity"})
    return build_opener(_AllowedRedirects).open(request, timeout=TIMEOUT_MODEL_DOWNLOAD_READ)


def _code_of(exc: BaseException) -> str | None:
    """The reason code of a failed request; None for one worth trying again."""
    if isinstance(exc, HTTPError):
        if exc.code in (401, 403):
            return "gated"
        if exc.code in (404, 410):
            return "not_found"
        if 300 <= exc.code < 400:
            return "network"  # a redirect that was not followed
        if exc.code in (408, 429) or exc.code >= 500:
            return None
        return "network"
    if isinstance(
        exc,
        (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException, OSError),
    ):
        return None
    return "network"


def _with_retry(fn: Callable[[], object], cancel: threading.Event) -> object:
    """Call ``fn``; a network failure is tried again, MODEL_DOWNLOAD_ATTEMPTS
    times in all, with the model downloads' pauses. The last one, and any
    answer that asking again cannot change (401, 403, 404), is Refused."""
    for attempt in range(1, MODEL_DOWNLOAD_ATTEMPTS + 1):
        if cancel.is_set():
            raise Cancelled()
        try:
            return fn()
        except (Refused, Cancelled):
            raise
        except Exception as exc:
            code = _code_of(exc)
            if code is not None:
                raise Refused(code) from exc
            if attempt == MODEL_DOWNLOAD_ATTEMPTS:
                logger.warning("my models: download failed: %s", type(exc).__name__)
                raise Refused("network") from exc
            delay = MODEL_DOWNLOAD_BACKOFF[min(attempt, len(MODEL_DOWNLOAD_BACKOFF)) - 1]
            if cancel.wait(delay):
                raise Cancelled() from exc
    raise Refused("network")


def _read_capped(response, cap: int) -> bytes:
    data = response.read(cap + 1)
    if len(data) > cap:
        raise Refused("network")
    return data


# ── the repository ──


def _repo_info_once(link: HfLink) -> RepoInfo:
    with _open(api_url(link)) as response:
        data = json.loads(_read_capped(response, HF_API_MAX_BYTES).decode("utf-8"))
    if not isinstance(data, dict):
        raise Refused("network")
    if data.get("gated") not in (None, False) or data.get("private") is True:
        raise Refused("gated")
    commit = data.get("sha")
    siblings = data.get("siblings")
    if not isinstance(commit, str) or not _COMMIT_RE.match(commit):
        raise Refused("network")
    if not isinstance(siblings, list):
        raise Refused("network")
    info = RepoInfo(commit=commit)
    for entry in siblings[:10_000]:
        if not isinstance(entry, dict):
            continue
        path = entry.get("rfilename")
        if not isinstance(path, str) or not _file_ok(path):
            continue  # never fetched, never shown
        size = entry.get("size")
        lfs = entry.get("lfs")
        sha = lfs.get("sha256") if isinstance(lfs, dict) else None
        if isinstance(lfs, dict) and isinstance(lfs.get("size"), int):
            size = lfs["size"]
        info.files[path] = RemoteFile(
            path=path,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            sha256=sha if isinstance(sha, str) and _SHA256_RE.match(sha) else None,
        )
    card = data.get("cardData")
    raw_license = card.get("license") if isinstance(card, dict) else None
    if isinstance(raw_license, list):
        raw_license = ", ".join(str(x) for x in raw_license[:4] if isinstance(x, str))
    info.license = display_text(raw_license, 80)
    return info


def repo_info(link: HfLink, cancel: threading.Event) -> RepoInfo:
    """The commit ``link``'s revision points at, and its files."""
    try:
        return _with_retry(lambda: _repo_info_once(link), cancel)  # type: ignore[return-value]
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise Refused("network") from exc


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1].lower()
    return name[name.rfind(".") :] if "." in name else ""


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[: name.rfind(".")] if "." in name else name


def choose_files(info: RepoInfo, link: HfLink) -> tuple[RemoteFile, RemoteFile]:
    """(weights, config) to download. The link's own file when it names one;
    otherwise the one .safetensors file, else the one torch checkpoint. The
    config is the .yaml named like the weights, else the only .yaml, else
    the only config.*; several give "ambiguous", none "config_missing"."""
    files = info.files
    if link.file is not None:
        weights = files.get(link.file)
        if weights is None:
            raise Refused("not_found")
        if _ext(weights.path) not in WEIGHTS_PREFERRED + WEIGHTS_TORCH:
            raise Refused("bad_format")
    else:
        weights = None
        for kind in (WEIGHTS_PREFERRED, WEIGHTS_TORCH):
            found = [f for f in files.values() if _ext(f.path) in kind]
            if len(found) > 1:
                raise Refused("ambiguous")
            if found:
                weights = found[0]
                break
        if weights is None:
            raise Refused("not_found")
    configs = [f for f in files.values() if _ext(f.path) in CONFIG_EXTS]
    same = [f for f in configs if _stem(f.path) == _stem(weights.path)]
    named = [f for f in configs if _stem(f.path).lower() == "config"]
    for group in (same, configs, named):
        if len(group) == 1:
            return weights, group[0]
    if not configs:
        raise Refused("config_missing")
    raise Refused("ambiguous")


# ── one file ──


def _local(fn: Callable[[], object]):
    """A write to the local disk: its OSError (a full disk, a folder that
    cannot be written) is no network failure and is not tried again."""
    try:
        return fn()
    except OSError as exc:
        logger.warning("my models: could not write a downloaded file: %s", exc)
        raise Refused("disk_full") from exc


def download(
    url: str,
    folder: Path,
    name: str,
    *,
    max_bytes: int,
    cancel: threading.Event,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> tuple[Path, int, str]:
    """Download ``url`` into ``folder`` as "<name>.<32 hex>.partial", flushed
    and synced; returns (that path, its size, its SHA-256). The caller moves
    it to its final name. Over ``max_bytes`` is "too_large" the moment the
    first byte over arrives; a size or SHA-256 other than the expected one
    is "bad_download". Nothing is left behind on any failure."""
    if expected_size is not None and expected_size > max_bytes:
        raise Refused("too_large")

    def once() -> tuple[Path, int, str]:
        tmp = folder / f"{name}.{uuid.uuid4().hex}.partial"
        try:
            with _open(url) as response:
                length = response.headers.get("Content-Length")
                if length is not None and length.strip().isdecimal():
                    declared = int(length.strip())
                    if declared > max_bytes:
                        raise Refused("too_large")
                    if expected_size is not None and declared != expected_size:
                        raise Refused("bad_download")
                digest = hashlib.sha256()
                received = 0
                out = _local(lambda: open(tmp, "xb"))  # noqa: SIM115 -- closed by the with below
                with out:
                    while True:
                        if cancel.is_set():
                            raise Cancelled()
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > max_bytes:
                            raise Refused("too_large")
                        _local(lambda chunk=chunk: out.write(chunk))
                        digest.update(chunk)
                        if on_bytes is not None:
                            on_bytes(received)
                    _local(out.flush)
                    _local(lambda: os.fsync(out.fileno()))
            if expected_size is not None and received < expected_size:
                raise http.client.IncompleteRead(b"", expected_size - received)
            if expected_size is not None and received != expected_size:
                raise Refused("bad_download")
            if received == 0:
                raise Refused("empty")
            sha = digest.hexdigest()
            if expected_sha256 is not None and sha != expected_sha256:
                raise Refused("bad_download")
            return tmp, received, sha
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    return _with_retry(once, cancel)  # type: ignore[return-value]
