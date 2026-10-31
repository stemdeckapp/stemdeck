"""Versions of a song (#804): named choices of stems over one shared pool.

A song keeps one set of stem files on disk. A version is only settings: a
name, the stems it shows, and when it was made. Its saved mix (levels, EQ,
master, speed and key, per mode) lives where every saved mix already lives,
in the page's store, under a key the page derives from the song and the
version id (static/js/versionScope.js). So a version costs a few bytes here
and nothing on disk, and deleting one never touches a stem file.

Every song has at least one version. A song made before versions existed has
none recorded: it reads as one implicit default version (DEFAULT_VERSION_ID)
built from its import selection, and nothing is written until a version is
created, renamed, deleted or chosen. The default's mix is the song's mix as it
was before versions, under the same store keys, so nothing moves.

Everything read back from the registry goes through clean_versions: a damaged
or hand-edited record loses only the entries that are not a version, never the
song.
"""

from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
import uuid
from typing import Any

from app.core.config import STEM_NAMES

logger = logging.getLogger("stemdeck.versions")

# The id of the version every song has before any other is made. Its saved
# mix is the one under the song's own store keys (no version suffix).
DEFAULT_VERSION_ID = "main"
# A version id: short, lowercase, safe in a URL path and a store key.
VERSION_ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
# A song with more versions than this is a script, not a person.
MAX_VERSIONS = 32
VERSION_NAME_MAX_CHARS = 60


def clean_name(value: Any) -> str | None:
    """A version name as kept: a string, trimmed, control characters removed,
    at most VERSION_NAME_MAX_CHARS long. None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch)[0] == "C" else ch for ch in value)
    text = " ".join(text.split())[:VERSION_NAME_MAX_CHARS].strip()
    return text or None


def clean_stems(value: Any) -> list[str]:
    """The base stems of a choice, in STEM_NAMES order, each once. Unknown
    names are dropped."""
    if not isinstance(value, list):
        return []
    wanted = {s for s in value if isinstance(s, str)}
    return [s for s in STEM_NAMES if s in wanted]


def _finite_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _clean_one(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    vid = raw.get("id")
    if not isinstance(vid, str) or not VERSION_ID_RE.match(vid):
        return None
    return {
        "id": vid,
        "name": clean_name(raw.get("name")),
        "stems": clean_stems(raw.get("stems")) or list(STEM_NAMES),
        "created_at": _finite_time(raw.get("created_at")),
    }


def clean_versions(value: Any) -> list[dict[str, Any]] | None:
    """The versions recorded on a song, made safe, or None when it has none
    recorded (the implicit default then applies). Never raises."""
    if value is None:
        return None
    if not isinstance(value, list):
        logger.warning("dropping a versions value that is not a list")
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value[:MAX_VERSIONS]:
        version = _clean_one(raw)
        if version is None or version["id"] in seen:
            logger.warning("dropping an unreadable version entry")
            continue
        seen.add(version["id"])
        out.append(version)
    return out or None


def default_version(selected_stems: Any, created_at: Any) -> dict[str, Any]:
    """The version a song has before any is made: its import selection."""
    return {
        "id": DEFAULT_VERSION_ID,
        "name": None,
        "stems": clean_stems(selected_stems) or list(STEM_NAMES),
        "created_at": _finite_time(created_at),
    }


def versions_of(job: Any) -> list[dict[str, Any]]:
    """Every version of `job`, never empty: the recorded ones, or the
    implicit default when none is recorded. Copies, safe to change."""
    recorded = clean_versions(getattr(job, "versions", None))
    if recorded:
        return [dict(v, stems=list(v["stems"])) for v in recorded]
    return [default_version(getattr(job, "selected_stems", None), getattr(job, "created_at", 0))]


def active_of(job: Any, versions: list[dict[str, Any]] | None = None) -> str:
    """The id of the version that opens with the song: the one chosen last,
    or the first when that one is gone."""
    versions = versions if versions is not None else versions_of(job)
    active = getattr(job, "active_version", None)
    ids = [v["id"] for v in versions]
    return active if isinstance(active, str) and active in ids else ids[0]


def new_version_id(existing: set[str]) -> str:
    while True:
        vid = uuid.uuid4().hex[:8]
        if vid not in existing and vid != DEFAULT_VERSION_ID:
            return vid


def make_version(existing: list[dict[str, Any]], name: str | None, stems: list[str]) -> dict:
    return {
        "id": new_version_id({v["id"] for v in existing}),
        "name": clean_name(name),
        "stems": clean_stems(stems) or list(STEM_NAMES),
        "created_at": time.time(),
    }
