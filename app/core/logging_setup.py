"""File logging for the stemdeck logger tree (#291).

Until now the app logged to stdout only (via uvicorn's root handler): server
and Docker deployments kept no log file at all, and LOGS_DIR existed but was
never written to. This module attaches a rotating file handler to the
"stemdeck" logger so every deployment keeps a bounded on-disk trail:

    LOGS_DIR/stemdeck.log   (5 MB x 3 backups, UTF-8, timestamped)

Level control:
  - STEMDECK_LOG_LEVEL=DEBUG|INFO|WARNING  (default INFO)
  - STEMDECK_DEBUG=1                        (shorthand for DEBUG; enables the
    per-job analyze diagnostics: "chroma:", "key candidates:")

Everything here is best-effort: a read-only filesystem (locked-down Docker)
must never prevent startup, so failures degrade to stdout-only logging.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from app.core.config import LOGS_DIR

# Module-level so tests can shrink them to exercise rotation.
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3

# Marker attribute so repeat calls (uvicorn --reload re-imports app.main)
# don't stack duplicate handlers.
_HANDLER_MARK = "_stemdeck_file_handler"

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING}


def _resolve_level() -> int:
    if os.environ.get("STEMDECK_DEBUG", "").strip() == "1":
        return logging.DEBUG
    name = os.environ.get("STEMDECK_LOG_LEVEL", "").strip().upper()
    return _LEVELS.get(name, logging.INFO)


def configure_logging() -> None:
    """Set the stemdeck logger level and attach the rotating file handler.

    Propagation stays on, so records continue to flow to uvicorn's stdout
    handler exactly as before -- the file is additive.
    """
    root = logging.getLogger("stemdeck")
    root.setLevel(_resolve_level())

    if any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        return  # already configured (reload / repeated import)

    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # delay=True: don't open the file until the first record, so a
        # read-only FS fails at emit time (swallowed by logging's internal
        # error handling) instead of at startup.
        handler = RotatingFileHandler(
            LOGS_DIR / "stemdeck.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        print(
            f"stemdeck: file logging disabled (cannot use logs dir {LOGS_DIR})",
            file=sys.stderr,
        )
        return

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname).1s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
