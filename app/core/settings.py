"""Runtime, user-toggleable settings (persisted to disk).

These are read live (unlike the env-var constants in config.py, which are fixed
at startup), so the Settings UI can change them without a restart:

- `allow_network`     — whether StemDeck answers requests from other devices.
- `max_duration_sec`  — longest track accepted for processing.
- `jobs_dir`          — where extracted stems are written (needs a restart).
- `playlist_max_items` — how many tracks one playlist import may queue.
- `video_max_height`  — max video resolution for MP4 export / YouTube pulls.
- `export_sample_rate` — sample rate for exported mixes/regions (WAV/FLAC/MP3).
- `demucs_device`     — compute device for separation: auto | cuda | mps | cpu.
- `separation_quality` — demucs shift-averaging: standard | best (2x slower).

Defaults fall back to the config.py constants (which honor their env vars), so
nothing changes until the user overrides a value.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from app.core.config import (
    DATA_DIR,
    MAX_DURATION_SEC,
    PLAYLIST_MAX_ITEMS,
    VIDEO_MAX_HEIGHT,
    available_torch_devices,
    detect_torch_device,
)

_log = logging.getLogger("stemdeck.settings")

_SETTINGS_PATH = DATA_DIR / "settings.json"
_LOCK = threading.RLock()
_state: dict | None = None  # whole settings dict, loaded lazily

# Clamp bounds. Max track length is capped at 60 min (the product ceiling).
_DURATION_MIN, _DURATION_MAX = 60, 3600  # 1 min .. 60 min
_HEIGHT_MIN, _HEIGHT_MAX = 144, 2160
_PLAYLIST_MIN, _PLAYLIST_MAX = 1, 200
_PORT_MIN, _PORT_MAX = 1024, 65535
DEFAULT_PORT = 8000

# Sample rates offered for mix/region export. 44.1 kHz (the Demucs stem rate, so
# the default is a pass-through) covers most samplers and DAWs; the others cover
# hardware that demands a specific rate (e.g. an Akai MPC rejecting 48 kHz).
EXPORT_SAMPLE_RATES = (22050, 32000, 44100, 48000)
DEFAULT_EXPORT_SAMPLE_RATE = 44100


def _default_allow_network() -> bool:
    # STEMDECK_ALLOW_NETWORK takes precedence when set explicitly.
    # Otherwise: desktop keeps network off (user opts in via UI toggle);
    # server/Docker deployments open it by default since network access is
    # the entire point of a headless deployment.
    env = os.environ.get("STEMDECK_ALLOW_NETWORK")
    if env is not None:
        return env.strip() == "1"
    return os.environ.get("STEMDECK_DESKTOP") != "1"


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
        # Seed the per-user copy from settings that already exist. Mirroring
        # only on _save() would protect nobody who configured StemDeck before
        # this shipped and never opens Settings again -- their next install
        # would still start empty. Safe against recursion (_state is assigned
        # first) and against clobbering: an empty dict means a genuine first
        # run, and overwriting a good copy with it is exactly the data loss
        # this whole mechanism exists to prevent.
        if _state:
            _mirror_settings()
    return _state


def _save() -> bool:
    """Write settings.json. Best-effort for most settings (read-only FS,
    permissions: the in-memory value still applies for this session, so a
    caller here does not fail its request over it) -- but the write outcome
    is still reported back, because one caller (set_jobs_dir) is coupled to
    something irreversible enough that silently swallowing a failure there
    would be actively misleading rather than merely inconvenient (#403)."""
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(_ensure()), encoding="utf-8")
    except Exception:
        _log.warning("could not persist settings to %s", _SETTINGS_PATH, exc_info=True)
        return False
    _mirror_settings()
    return True


def _mirror_settings() -> None:
    """Keep a per-user copy of settings.json outside the install directory.

    A Windows portable package keeps its data in `<app>/data` (#399), so
    settings.json lives *inside the install*. Upgrading by extracting the new
    zip to a fresh folder therefore started that install with no settings at
    all: the stems location, port, compute device, quality and language were
    all silently back to defaults, and a relocated library looked empty.

    The desktop shell already restores from this copy -- `ensure_workspace`
    seeds a fresh portable install from it before the backend ever reads
    settings.json. Only the write half was missing, because #399 moved the
    data directory and nothing took over writing the old location.

    Best-effort by definition: this is a redundant copy, and failing to write
    it must never fail the setting the user just changed. The path comes from
    the shell (STEMDECK_SETTINGS_MIRROR) so the platform logic stays in one
    place and both halves cannot drift apart.
    """
    target = os.environ.get("STEMDECK_SETTINGS_MIRROR", "").strip()
    if not target:
        return
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Same-directory temp + replace: a torn write here would be restored
        # verbatim into the user's next install.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_ensure()), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        _log.warning("could not mirror settings to %s", target, exc_info=True)


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


# ── jobs_dir ──
# Where extracted stems are written. Read by config.py at import time (straight
# from settings.json, to avoid importing this module from there), so a change
# only takes effect on the next start -- which is also when the move that goes
# with it has finished. Absent means "wherever the default puts it".
def get_jobs_dir() -> str | None:
    with _LOCK:
        value = _ensure().get("jobs_dir")
        return value if isinstance(value, str) and value.strip() else None


def set_jobs_dir(value: str | None) -> tuple[str | None, bool]:
    """Persist the jobs folder choice. Returns (resolved_value, persisted).

    Unlike every other setting in this module, a failed persist here is not a
    minor inconvenience: this is called only after move_library() has already
    physically relocated the user's library (POST /api/settings/stems-location
    in app/main.py), so quietly keeping the in-memory value "for this session"
    and reporting success would mean the app comes back to the OLD (now-empty)
    folder on the very next restart, with the real data sitting at a location
    nothing points at any more (#403). The caller must check `persisted` and
    tell the user the truth rather than assume a 200 means the choice stuck."""
    with _LOCK:
        if value is None or not str(value).strip():
            _ensure().pop("jobs_dir", None)
            return None, _save()
        resolved = str(Path(str(value)).expanduser().resolve())
        _ensure()["jobs_dir"] = resolved
        return resolved, _save()


# ── playlist_max_items ──
# How many tracks one playlist import may queue. A waiting link costs a registry
# record, so the ceiling is generous; the real reason to keep this adjustable is
# that "import 200 tracks" is a decision about the user's evening, not about
# resources.
def get_playlist_max_items() -> int:
    with _LOCK:
        v = _num(_ensure().get("playlist_max_items"))
        return max(_PLAYLIST_MIN, min(_PLAYLIST_MAX, v)) if v is not None else PLAYLIST_MAX_ITEMS


def set_playlist_max_items(value: int) -> int:
    with _LOCK:
        clamped = max(_PLAYLIST_MIN, min(_PLAYLIST_MAX, int(value)))
        _ensure()["playlist_max_items"] = clamped
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


# ── port ──
# The preferred port the server binds on launch. The desktop launcher reads this
# (default 8000) before spawning the backend; a self-hosted server's --port wins.
# Changing it needs a restart — the socket is bound at startup.
def get_port() -> int:
    with _LOCK:
        v = _num(_ensure().get("port"))
        return max(_PORT_MIN, min(_PORT_MAX, v)) if v is not None else DEFAULT_PORT


def set_port(value: int) -> int:
    with _LOCK:
        clamped = max(_PORT_MIN, min(_PORT_MAX, int(value)))
        _ensure()["port"] = clamped
        _save()
        return clamped


# ── export_sample_rate ──
# Sample rate (Hz) the mix/region export encodes at. Read live per request by the
# mixdown endpoint (app/api/stems.py), so a change applies to the next export
# without a restart. Restricted to a small allowlist rather than clamped: an
# arbitrary rate is more likely a mistake than an intent, and hardware samplers
# only accept specific rates.
def get_export_sample_rate() -> int:
    with _LOCK:
        v = _num(_ensure().get("export_sample_rate"))
        return v if v in EXPORT_SAMPLE_RATES else DEFAULT_EXPORT_SAMPLE_RATE


def set_export_sample_rate(value: int) -> int:
    """Persist an export sample rate. Rejects anything outside the allowlist with
    ValueError (surfaced as a 422) rather than clamping to the nearest rate."""
    try:
        rate = int(value)
    except (TypeError, ValueError):
        raise ValueError("export_sample_rate must be an integer") from None
    if rate not in EXPORT_SAMPLE_RATES:
        raise ValueError(
            "export_sample_rate must be one of: " + ", ".join(map(str, EXPORT_SAMPLE_RATES))
        )
    with _LOCK:
        _ensure()["export_sample_rate"] = rate
        _save()
        return rate


# ── demucs_device ──
# Compute device for stem separation. "auto" (default) resolves to the best
# available device via a hardware probe at job time; "cuda"/"mps"/"cpu" force
# it. Read live per job (app/pipeline/separate.py), so changes apply to the
# NEXT separation without a restart. STEMDECK_DEMUCS_DEVICE seeds the default
# so existing env-based deployments keep their forced device.
_DEVICE_CHOICES = ("auto", "cuda", "mps", "cpu")


def _default_demucs_device() -> str:
    env = os.environ.get("STEMDECK_DEMUCS_DEVICE", "").strip().lower()
    return env if env in ("cuda", "mps", "cpu") else "auto"


def get_demucs_device_choice() -> str:
    """The persisted user choice ("auto" | "cuda" | "mps" | "cpu") -- what the
    Settings UI displays, as opposed to what jobs run on (see below)."""
    with _LOCK:
        v = _ensure().get("demucs_device")
        return v if isinstance(v, str) and v in _DEVICE_CHOICES else _default_demucs_device()


def get_demucs_device() -> str:
    """The device the next separation job will actually use: the forced choice,
    or a fresh hardware probe when the choice is "auto"."""
    choice = get_demucs_device_choice()
    return detect_torch_device() if choice == "auto" else choice


def set_demucs_device(value: str) -> str:
    """Persist a device choice. Forcing "cuda"/"mps" verifies the device is
    actually available first and raises ValueError if not -- rejecting the
    write loudly beats persisting a device that would silently fall back or
    crash the next job (the #247 lesson, applied to the server path)."""
    choice = (value or "").strip().lower()
    if choice not in _DEVICE_CHOICES:
        raise ValueError("demucs_device must be one of: " + ", ".join(_DEVICE_CHOICES))
    if choice in ("cuda", "mps") and choice not in available_torch_devices():
        raise ValueError(f"{choice} is not available on this machine")
    with _LOCK:
        _ensure()["demucs_device"] = choice
        _save()
        return choice


# ── separation_quality ──
# "standard" (default) runs demucs once. "best" adds --shifts 2: demucs
# re-runs separation on a randomly time-shifted copy of the input and
# averages the two -- measurably cleaner stems, at ~2x the separation time.
# Applies on any device; a CPU user who picks "best" is accepting the wait
# knowingly. STEMDECK_SEPARATION_QUALITY seeds the default so existing
# env-based deployments can force it.
_QUALITY_CHOICES = ("standard", "best")


def _default_separation_quality() -> str:
    env = os.environ.get("STEMDECK_SEPARATION_QUALITY", "").strip().lower()
    return env if env in _QUALITY_CHOICES else "standard"


def get_separation_quality() -> str:
    with _LOCK:
        v = _ensure().get("separation_quality")
        return v if isinstance(v, str) and v in _QUALITY_CHOICES else _default_separation_quality()


def set_separation_quality(value: str) -> str:
    choice = (value or "").strip().lower()
    if choice not in _QUALITY_CHOICES:
        raise ValueError("separation_quality must be one of: " + ", ".join(_QUALITY_CHOICES))
    with _LOCK:
        _ensure()["separation_quality"] = choice
        _save()
        return choice
