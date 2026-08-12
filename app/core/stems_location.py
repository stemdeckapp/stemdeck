"""Moving the stems library to a different folder (#354).

Documents is a sensible default -- the library is the user's work, it belongs
somewhere visible that survives a reinstall -- but on macOS and Windows it is
also the folder most people have syncing to iCloud or OneDrive, and a stem
library is tens of gigabytes nobody agreed to spend on cloud storage.

Changing the location is not just a preference write. The registry lives inside
the stems folder, so leaving the existing library behind would make the app come
back empty with the files stranded. The move is the feature; the setting is
bookkeeping.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("stemdeck.stems_location")


class StemsLocationError(ValueError):
    """Rejected before anything on disk was touched."""


# JOBS_DIR is bound at import time across the app, so a move leaves this process
# still writing to the folder the files just left. An import accepted in that
# window lands in the old folder and disappears from the library on the next
# start -- the stems are on disk, in a directory nothing looks at any more.
#
# Set before the move begins (which also closes the race against an import
# arriving mid-move) and never cleared on success: the restart is what clears
# it, and the restart is the point.
_relocating = False


def is_relocating() -> bool:
    return _relocating


def begin_relocation() -> None:
    global _relocating
    _relocating = True


def abandon_relocation() -> None:
    """The move failed, so the library is still where this process thinks it is
    and the app stays usable."""
    global _relocating
    _relocating = False


@dataclass(frozen=True)
class MoveResult:
    source: str
    target: str
    moved_entries: int
    bytes_moved: int
    same_filesystem: bool


def directory_size(path: Path) -> int:
    """Best-effort total size. Used to tell the user what they are about to move,
    so a failure to stat one file must not sink the whole answer."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def validate_target(target: Path, current: Path) -> Path:
    """Check a candidate location before anything is moved into it.

    Deliberately strict. This path becomes the root of a directory the app
    creates and deletes inside, and a careless answer here -- the user's home
    directory, or a folder already full of their own files -- turns a settings
    change into data loss.
    """
    if not str(target).strip():
        raise StemsLocationError("Pick a folder to store stems in.")

    target = Path(target).expanduser()
    if not target.is_absolute():
        raise StemsLocationError("The stems folder must be an absolute path.")
    target = target.resolve()
    current = current.expanduser().resolve()

    if target == current:
        raise StemsLocationError("Stems are already stored there.")
    if current in target.parents:
        # Moving a directory inside itself would recurse forever.
        raise StemsLocationError("Pick a folder outside the current stems folder.")

    if target.exists():
        if not target.is_dir():
            raise StemsLocationError("That path is a file, not a folder.")
        # An existing folder is fine only if it is empty or already ours: merging
        # a stem library into someone's Desktop is not a recoverable mistake.
        entries = list(target.iterdir())
        ours = {"registry.json", "failed"}
        if entries and not all(e.name in ours or _looks_like_job_dir(e) for e in entries):
            raise StemsLocationError("Pick an empty folder, or one StemDeck already uses.")
    else:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StemsLocationError(f"Cannot create that folder: {e.strerror or e}") from e

    if not os.access(target, os.W_OK):
        raise StemsLocationError("That folder is not writable.")
    return target


def _looks_like_job_dir(entry: Path) -> bool:
    from app.core.config import JOB_ID_RE

    return entry.is_dir() and bool(JOB_ID_RE.match(entry.name))


def move_library(current: Path, target: Path) -> MoveResult:
    """Move every stem directory from `current` into `target`.

    Entry by entry rather than moving the folder itself: the folder may be one
    the user picked in a native dialog and expects to keep, and on desktop the
    parent (~/Documents/StemDeck) also holds the library index, which stays put.

    On a failure partway, whatever has already moved stays moved and the error
    names the entry that stopped it. Rolling back a half-finished multi-gigabyte
    copy has its own failure modes, and the app can see a library in either
    place -- it is the setting, written only on success, that decides which one
    it reads.
    """
    current = current.expanduser().resolve()
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)

    same_fs = _same_filesystem(current, target)
    moved = 0
    moved_bytes = 0

    if not current.exists():
        return MoveResult(str(current), str(target), 0, 0, same_fs)

    for entry in sorted(current.iterdir()):
        destination = target / entry.name
        if destination.exists():
            # A previous, interrupted attempt already brought this one over.
            logger.info("stems move: %s already at the target, skipping", entry.name)
            continue
        size = directory_size(entry) if entry.is_dir() else entry.stat().st_size
        try:
            shutil.move(str(entry), str(destination))
        except OSError as e:
            raise StemsLocationError(
                f"Could not move {entry.name}: {e.strerror or e}. "
                f"Anything already moved is in the new folder."
            ) from e
        moved += 1
        moved_bytes += size

    return MoveResult(str(current), str(target), moved, moved_bytes, same_fs)


def _same_filesystem(a: Path, b: Path) -> bool:
    """Whether the move is a rename or a copy. Only used to set expectations in
    the UI, so an unanswerable question is answered with "assume the slow one"."""
    try:
        probe = a if a.exists() else a.parent
        other = b if b.exists() else b.parent
        return probe.stat().st_dev == other.stat().st_dev
    except OSError:
        return False
