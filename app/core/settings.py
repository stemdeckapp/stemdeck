"""Runtime, user-toggleable settings (persisted to disk).

These are read live (unlike the env-var constants in config.py, which are fixed
at startup), so the Settings UI can change them without a restart:

- `allow_network`     — whether StemDeck answers requests from other devices.
- `max_duration_sec`  — longest track accepted for processing.
- `video_max_height`  — max video resolution for MP4 export / YouTube pulls.

Defaults fall back to the config.py constants (which honor their env vars), so
nothing changes until the user overrides a value.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from app.core.config import DATA_DIR, MAX_DURATION_SEC, VIDEO_MAX_HEIGHT

_log = logging.getLogger("stemdeck.settings")

_SETTINGS_PATH = DATA_DIR / "settings.json"
_LOCK = threading.RLock()
_state: dict | None = None  # whole settings dict, loaded lazily

# Clamp bounds. Max track length is capped at 20 min (the product ceiling).
_DURATION_MIN, _DURATION_MAX = 60, 1200  # 1 min .. 20 min
_HEIGHT_MIN, _HEIGHT_MAX = 144, 2160


def _default_allow_network() -> bool:
    # Off by default everywhere — the user explicitly opts other devices in.
    # STEMDECK_ALLOW_NETWORK=1 can pre-enable it (e.g. headless/Docker deploys).
    env = os.environ.get("STEMDECK_ALLOW_NETWORK")
    if env is not None:
        return env.strip() == "1"
    return False


def _load() -> dict:
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass  # no settings file yet — first run; use defaults
    except Exception:
        # Corrupt/unreadable file: fall back to defaults rather than crash.
        _log.warning("could not read settings from %s", _SETTINGS_PATH, exc_info=True)
    return {}


def _ensure() -> dict:
    global _state
    if _state is None:
        _state = _load()
    return _state


def _save() -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(_ensure()), encoding="utf-8")
    except Exception:
        # Persistence is best-effort (read-only FS, permissions): the in-memory
        # value still applies for this session, so don't fail the request.
        _log.warning("could not persist settings to %s", _SETTINGS_PATH, exc_info=True)


def _num(v: object) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ── allow_network ──
def get_allow_network() -> bool:
    with _LOCK:
        v = _ensure().get("allow_network")
        return v if isinstance(v, bool) else _default_allow_network()


def set_allow_network(value: bool) -> bool:
    with _LOCK:
        _ensure()["allow_network"] = bool(value)
        _save()
        return bool(value)


# ── max_duration_sec ──
def get_max_duration_sec() -> int:
    with _LOCK:
        v = _num(_ensure().get("max_duration_sec"))
        return max(_DURATION_MIN, min(_DURATION_MAX, v)) if v is not None else MAX_DURATION_SEC


def set_max_duration_sec(value: int) -> int:
    with _LOCK:
        clamped = max(_DURATION_MIN, min(_DURATION_MAX, int(value)))
        _ensure()["max_duration_sec"] = clamped
        _save()
        return clamped


# ── video_max_height ──
def get_video_max_height() -> int:
    with _LOCK:
        v = _num(_ensure().get("video_max_height"))
        return max(_HEIGHT_MIN, min(_HEIGHT_MAX, v)) if v is not None else VIDEO_MAX_HEIGHT


def set_video_max_height(value: int) -> int:
    with _LOCK:
        clamped = max(_HEIGHT_MIN, min(_HEIGHT_MAX, int(value)))
        _ensure()["video_max_height"] = clamped
        _save()
        return clamped
