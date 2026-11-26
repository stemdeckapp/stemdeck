"""A model file pinned by size and SHA-256, and whether the one on disk is it.

For the weights a MIDI engine fetches on first use (app/pipeline/piano_hr.py,
app/pipeline/ymt3.py): where the file lives under MODELS_DIR, what state it
is in without reading it (its size only), and whether it is exactly the
pinned file (its SHA-256, read once per change of the file). Nothing here
imports torch or reaches the network: the server process asks these
questions, and model_fetch.py does the download.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PinnedFile:
    """One file: its address (or None for a file StemDeck makes on this
    computer from another one), SHA-256, size and path under MODELS_DIR."""

    url: str | None
    sha256: str
    size: int
    relpath: str
    # (path, size, mtime_ns, inode) of the last file whose SHA-256 matched,
    # so a run reads hundreds of MB again only when the file changed since.
    _verified: dict = field(default_factory=lambda: {"ident": None}, compare=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.url is not None and (not self.url.startswith("https://") or "%" in self.url):
            # The worker prints the address on stderr, where a "%" reads as
            # progress (stem_models.ModelFile has the same rule).
            raise ValueError("a model URL must be https and hold no '%'")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        if self.size <= 0:
            raise ValueError("size must be positive")
        parts = self.relpath.split("/")
        if (
            not self.relpath
            or "\\" in self.relpath
            or "%" in self.relpath
            or any(part in ("", ".", "..") for part in parts)
            or ":" in parts[0]
        ):
            raise ValueError("relpath must be a plain relative path under MODELS_DIR")

    def path(self) -> Path:
        """The file under MODELS_DIR, looked up when called, never bound at
        import (the test fixtures point MODELS_DIR at a temporary folder)."""
        from app.core import config

        return config.MODELS_DIR.joinpath(*self.relpath.split("/"))

    def state(self) -> str:
        """ "downloaded" (the pinned size), "partial" (another size) or
        "not_downloaded". The size only: ok() reads the whole file."""
        try:
            size = self.path().stat().st_size
        except OSError:
            return "not_downloaded"
        return "downloaded" if size == self.size else "partial"

    def ok(self, path: Path | None = None) -> bool:
        """Whether the file on disk is exactly the pinned one: its size, then
        its SHA-256 (about a second per 200 MB, once per change of it)."""
        path = path or self.path()
        ident = _identity(path)
        if ident is None or ident[1] != self.size:
            return False
        with self._lock:
            if self._verified["ident"] == ident:
                return True
        if sha256_of(path) != self.sha256:
            return False
        with self._lock:
            # Kept only if the file did not change while it was read.
            self._verified["ident"] = ident if _identity(path) == ident else None
        return True

    def remove_bad(self, path: Path | None = None) -> bool:
        """Remove a file that is not the pinned one, so the next run makes or
        fetches it again. True when one was removed."""
        path = path or self.path()
        if not path.exists() or self.ok(path):
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return True


def _identity(path: Path) -> tuple[str, int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_size, st.st_mtime_ns, st.st_ino)


def sha256_of(path: Path) -> str | None:
    """The file's SHA-256 in hex, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()
