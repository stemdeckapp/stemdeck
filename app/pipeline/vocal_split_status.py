"""Whether this install can make Lead + Backing, and why a split failed (#832).

Two jobs, both small:

- ``failure_code`` turns an exception, or the text a worker left behind, into
  one of the codes in app.core.vocal_split_outcome. The worker calls it where
  it knows which step failed; the parent calls it on whatever text it has
  when the worker died without saying.

- The capability: a failed setup warmup (``WARMUP_FAILED vocal_split ...``)
  used to be written to setup.log and nowhere else, so the first a person
  learnt of it was choosing Lead + Backing and getting one Vocals stem. The
  warmup and every real split now record their outcome in a small file in
  the data folder, and /api/health reports it, the way it reports an OGG
  encoder that is missing (#824).

Only a missing dependency makes the split unavailable. It cannot fix itself
on this install: the next attempt fails the same way until StemDeck is
updated or repaired. A model that did not download can still download on the
next attempt, and memory, a crash or a stall belong to one run, not to the
install, so none of those switch anything off.

A recorded failure is tied to the versions of the packages the split loads.
When those change (an update, or the startup repair that swaps a broken
torchvision for the right one, #778) the record no longer describes this
install and is ignored, so a fixed install is never left switched off.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from importlib import metadata

from app.core.config import DATA_DIR
from app.core.vocal_split_outcome import (
    DEPENDENCY_MISSING,
    MODEL_UNAVAILABLE,
    OUT_OF_MEMORY,
    WORKER_FAILED,
)
from app.pipeline.errors import classify_failure

logger = logging.getLogger("stemdeck.pipeline")

# The packages the split imports, by distribution name. A change in any of
# them is a different install as far as a recorded failure is concerned.
_PACKAGES = ("torch", "torchvision", "onnxruntime", "onnx", "onnx2torch", "audio-separator")

# Lowercased substrings that name a missing or broken dependency: a module
# that is not there, a shared library that would not load, or one of the
# packages the model is run through failing on load (#778's torchvision).
_DEPENDENCY_PATTERNS = (
    "no module named",
    "cannot open shared object",
    "dll load failed",
    "library not loaded",
    "undefined symbol",
    "symbol not found",
    "torchvision",
    "onnx2torch",
    "onnxruntime",
)

# The model could not be fetched, or what was fetched is not a model.
_MODEL_PATTERNS = (
    "failed to download",
    "response code",
    "max retries exceeded",
    "connection",
    "timed out",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "certificate",
    "ssl",
    "unsupported model file",
    "not found in supported model files",
)

_status_path = DATA_DIR / "vocal-split-status.json"
_lock = threading.Lock()
_state: dict | None = None
_fingerprint_cache: dict[str, str | None] | None = None


def failure_code(phase: str, exc: BaseException | None = None, text: str = "") -> str:
    """The code for a failure in ``phase`` ("import", "load", "separate", or
    "unknown" when the caller cannot tell), from the exception or its text."""
    words = f"{type(exc).__name__}: {exc}" if exc is not None else text
    low = words.lower()
    if isinstance(exc, MemoryError) or classify_failure(low) == "out-of-memory":
        return OUT_OF_MEMORY
    if isinstance(exc, ImportError) or "importerror" in low or "modulenotfounderror" in low:
        return DEPENDENCY_MISSING
    if any(p in low for p in _DEPENDENCY_PATTERNS):
        return DEPENDENCY_MISSING
    if phase == "import":
        return DEPENDENCY_MISSING
    if phase == "load" or any(p in low for p in _MODEL_PATTERNS):
        return MODEL_UNAVAILABLE
    return WORKER_FAILED


def _fingerprint() -> dict[str, str | None]:
    """{package: installed version or None}, read from package metadata only.
    Nothing is imported, so this stays cheap enough for a health poll, and it
    is read once per process: the packages do not change under a running
    backend."""
    global _fingerprint_cache
    if _fingerprint_cache is None:
        found: dict[str, str | None] = {}
        for name in _PACKAGES:
            try:
                found[name] = metadata.version(name)
            except metadata.PackageNotFoundError:
                found[name] = None
            except Exception:
                logger.warning("could not read the version of %s", name, exc_info=True)
                found[name] = None
        _fingerprint_cache = found
    return _fingerprint_cache


def _load() -> dict:
    global _state
    if _state is None:
        try:
            data = json.loads(_status_path.read_text(encoding="utf-8"))
            _state = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            _state = {}
        except (OSError, ValueError):
            logger.warning("vocal split status unreadable; ignoring it", exc_info=True)
            _state = {}
    return _state


def record(ok: bool, code: str | None = None, *, source: str) -> None:
    """Remember how the split went on this install.

    A success clears any failure. A failure is kept only when it says
    something about the install (a dependency or the model); one that belongs
    to a single run leaves the record as it was. Best-effort: a data folder
    that cannot be written is logged, never raised."""
    global _state
    if not ok and code not in (DEPENDENCY_MISSING, MODEL_UNAVAILABLE):
        return
    data = {
        "ok": bool(ok),
        "code": None if ok else code,
        "source": source,
        "at": time.time(),
        "packages": _fingerprint(),
    }
    with _lock:
        _state = data
        try:
            _status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = _status_path.with_name(f"{_status_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, _status_path)
            finally:
                tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not record the vocal split status", exc_info=True)


def capability() -> dict[str, object]:
    """{"available": bool, "reason": code or None} for /api/health.

    Unavailable only when the last outcome on this exact set of packages was
    a missing dependency. Anything else, a record from other package versions
    included, reads as available: a split is then tried as it always was."""
    with _lock:
        data = dict(_load())
    if (
        data.get("ok") is False
        and data.get("code") == DEPENDENCY_MISSING
        and data.get("packages") == _fingerprint()
    ):
        return {"available": False, "reason": DEPENDENCY_MISSING}
    return {"available": True, "reason": None}


def reset_state() -> None:
    """Forget the in-memory copy (tests)."""
    global _state, _fingerprint_cache
    with _lock:
        _state = None
        _fingerprint_cache = None
