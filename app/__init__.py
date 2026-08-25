"""StemDeck.

The vendored path insert below runs before anything else in the package, which
is the only place it can work: yt-dlp resolves its optional dependencies at
import time, and `app.pipeline.download` imports yt_dlp at module level.
"""

from __future__ import annotations

import sys
from pathlib import Path

# yt-dlp's YouTube challenge solver (#432, #438).
#
# Vendored rather than declared as a dependency, and the reason is the desktop
# updater, not preference. runtimeId is sha256(uv.lock), and the updater stands
# down when it changes because it can replace `backend/` but never `python/`.
# A new dependency would therefore have sent every existing desktop install to
# a full manual reinstall to get this.
#
# This package is not one StemDeck imports. It is a 53 KB pure-Python payload
# with no dependencies that yt-dlp discovers at runtime, so it belongs in the
# app layer that the updater does replace. Existing installs get the solver
# through the ordinary in-app update instead.
#
# Refresh with: python scripts/update_vendored_ejs.py
_VENDOR = Path(__file__).resolve().parent / "_vendor"
if _VENDOR.is_dir():
    _path = str(_VENDOR)
    if _path not in sys.path:
        sys.path.insert(0, _path)
