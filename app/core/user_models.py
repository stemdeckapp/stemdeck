"""The user's own separation models ("My models", Settings, Separation).

A user model is a Mel-Band RoFormer or BS-RoFormer the user added from a
Hugging Face link or from two files on disk (app/api/my_models.py). Each lives
in its own folder under ``root()``:

  <root>/models.json                     this registry, one JSON object
  <root>/<id>/model.safetensors          or model.ckpt: the name comes from the
                                         format read from the bytes, never from
                                         the name the file arrived with
  <root>/<id>/config.yaml                the config as received
  <root>/<id>/<name>.<32 hex>.partial    a write in progress

An id is "user_" and 12 hex (config.USER_MODEL_ID_RE), random. File names in
a folder are fixed words; a name a user or a remote repository chose is kept
only as display text.

The registry is read lazily and written whole, temp file then flush, fsync
and rename, under one lock. A model is usable for separation only when its
status is "ready" (the test run passed) and its file is still the size and
modification time recorded; ``usable()`` asks the disk on each call, so a
file removed by hand reads as missing at once.

Lock order. settings._LOCK may be held while this module's lock is taken
(settings asks ``choices_for`` while it validates a choice); the other way
round never happens: nothing here calls into settings while holding
``_lock``. A caller that removes a model and then scrubs the setting releases
this lock first (``remove`` returns before the scrub).

Nothing here imports torch, and nothing here deletes a folder that holds a
finished model file unless its entry is removed on purpose: a registry that
was lost or quarantined leaves the user's copies where they are.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from app.core import config
from app.core.config import MAX_USER_MODELS, USER_MODEL_ID_RE

logger = logging.getLogger("stemdeck.user_models")

VERSION = 1
REGISTRY_FILE = "models.json"
CONFIG_FILE = "config.yaml"
# The model file's name per format (user_model_format.sniff_format).
MODEL_FILES: dict[str, str] = {"safetensors": "model.safetensors", "torch": "model.ckpt"}
# The name a model file is written under before its format is known.
MODEL_STAGING_NAME = "model"
ARCHES = ("mel_band_roformer", "bs_roformer")
SOURCE_KINDS = ("hf", "upload")

# Stored statuses. "missing" is never stored: it is what a "ready" model
# whose file is gone or no longer its recorded size reads as.
STATUSES = ("staging", "downloading", "checking", "ready", "refused")
WORKING = ("staging", "downloading", "checking")
MISSING = "missing"
# A folder under root() with no entry and no finished model file is removed
# by reconcile() once it is this old; one that holds a model file is kept.
ORPHAN_MIN_AGE_SEC = 3600

_lock = threading.RLock()
_state: dict | None = None
_loaded_from: Path | None = None
_reset = False
# Progress of the work on an entry: {"pct": int, "step": str | None}. Kept in
# memory only; a restart ends the work (reconcile).
_progress: dict[str, dict] = {}


class LimitReached(Exception):
    """The list already holds MAX_USER_MODELS models."""


class UnknownModel(KeyError):
    """No entry has this id."""


# ── paths ──


def root() -> Path:
    """Where user models live: MODELS_DIR/user, read at call time so a test
    or a changed setting moves every use of it at once."""
    return config.MODELS_DIR / "user"


def registry_path() -> Path:
    return root() / REGISTRY_FILE


def is_user_id(value: object) -> bool:
    return isinstance(value, str) and USER_MODEL_ID_RE.match(value) is not None


def model_dir(model_id: str) -> Path:
    """The folder of ``model_id``, checked to be directly under root().
    Raises ValueError for an id that is not one, or a folder that resolves
    elsewhere (a link put there by hand)."""
    if not is_user_id(model_id):
        raise ValueError("not a user model id")
    base = root().resolve()
    folder = (root() / model_id).resolve()
    if not folder.is_relative_to(base) or folder.parent != base:
        raise ValueError("model folder outside the models folder")
    return root() / model_id


def model_file_path(model_id: str, rec: dict | None = None) -> Path | None:
    """The model file of ``model_id`` per its record, or None if it has none."""
    rec = rec if rec is not None else record(model_id)
    if rec is None:
        return None
    name = (rec.get("file") or {}).get("name")
    if name not in MODEL_FILES.values():
        return None
    return model_dir(model_id) / name


def config_file_path(model_id: str) -> Path:
    return model_dir(model_id) / CONFIG_FILE


# ── the registry file ──


def _empty() -> dict:
    return {"version": VERSION, "models": {}}


def _entry_ok(model_id: object, entry: object) -> bool:
    return (
        is_user_id(model_id)
        and isinstance(entry, dict)
        and entry.get("status") in STATUSES
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("stems"), list)
        and all(isinstance(s, str) for s in entry["stems"])
        and isinstance(entry.get("added_at"), (int, float))
    )


def _read(path: Path) -> dict:
    """The registry at ``path``. A file that is not a registry is moved aside
    (settings.quarantine_corrupt) and the list starts empty; an entry that is
    not one is dropped with a warning. The model folders stay either way."""
    global _reset
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- unreadable or not JSON: the same outcome
        logger.warning("my models: could not read %s", path, exc_info=True)
        data = None
    if (
        not isinstance(data, dict)
        or data.get("version") != VERSION
        or not isinstance(data.get("models"), dict)
    ):
        from app.core.settings import quarantine_corrupt

        quarantine_corrupt(path, "my models")
        _reset = True
        return _empty()
    models: dict[str, dict] = {}
    for model_id, entry in data["models"].items():
        if _entry_ok(model_id, entry):
            models[model_id] = entry
        else:
            logger.warning("my models: dropped an unreadable entry from %s", path)
    return {"version": VERSION, "models": models}


def _ensure() -> dict:
    """The registry in memory, read on first use and again whenever root()
    has moved. Callers hold _lock."""
    global _state, _loaded_from
    path = registry_path()
    if _state is None or _loaded_from != path:
        _state = _read(path)
        _loaded_from = path
        _progress.clear()
    return _state


def _save() -> bool:
    """Write the registry: temp file in the same folder, flush, fsync, rename,
    then the folder synced. A failure is logged and the memory copy is served
    until the next start. Callers hold _lock."""
    from app.core.settings import fsync_dir

    state = _ensure()
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(state))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        fsync_dir(path.parent)
    except Exception:  # noqa: BLE001 -- disk full, read-only folder: the list still answers
        logger.warning("my models: could not write %s", path, exc_info=True)
        return False
    return True


def registry_reset() -> bool:
    """Whether the registry file was unreadable and moved aside this run."""
    with _lock:
        _ensure()
        return _reset


def reset_state() -> None:
    """Forget the memory copy, for tests (tests/conftest.py)."""
    global _state, _loaded_from, _reset
    with _lock:
        _state = None
        _loaded_from = None
        _reset = False
        _progress.clear()


# ── reading ──


def record(model_id: str) -> dict | None:
    """A copy of the stored entry of ``model_id``, or None."""
    if not is_user_id(model_id):
        return None
    with _lock:
        entry = _ensure()["models"].get(model_id)
        return copy.deepcopy(entry) if entry is not None else None


def count() -> int:
    with _lock:
        return len(_ensure()["models"])


def ids() -> list[str]:
    """Every id, oldest first."""
    with _lock:
        models = _ensure()["models"]
        return sorted(models, key=lambda i: (models[i].get("added_at", 0), i))


def file_state(model_id: str, rec: dict | None = None) -> str:
    """ "ok", "missing" (no file, or not its recorded size) or "changed" (its
    modification time moved: the same size, maybe other bytes) for a record
    that has a file; "none" for one that has not got one yet."""
    rec = rec if rec is not None else record(model_id)
    info = (rec or {}).get("file")
    if not isinstance(info, dict):
        return "none"
    try:
        path = model_file_path(model_id, rec)
    except ValueError:
        return "missing"
    if path is None:
        return "missing"
    try:
        st = path.stat()
    except OSError:
        return "missing"
    if st.st_size != info.get("size"):
        return "missing"
    if st.st_mtime_ns != info.get("mtime_ns"):
        return "changed"
    return "ok"


def requirements_met() -> bool:
    """Whether this install can run a user model: torch and audio-separator's
    RoFormer classes are there (stem_models' own rule, never importing them)."""
    from app.core import stem_models

    try:
        return stem_models._requirement_met("audio_separator")
    except OSError:
        return False


def _spec_ok(rec: dict) -> bool:
    """Whether a ready record carries everything the worker is handed, each
    value passing the same whitelist it passed when it was added: a registry
    edited by hand or torn is not handed to the model's constructor."""
    from app.core import user_model_format as fmt

    try:
        fmt.check_stored(
            rec.get("arch"), rec.get("kwargs"), rec.get("audio"), rec.get("outputs"), rec["stems"]
        )
    except (fmt.Refused, KeyError, TypeError):
        return False
    info = rec.get("file")
    return (
        isinstance(info, dict)
        and info.get("name") in MODEL_FILES.values()
        and isinstance(info.get("size"), int)
        and isinstance(info.get("mtime_ns"), int)
        and isinstance(info.get("sha256"), str)
        and len(info["sha256"]) == 64
    )


def usable(model_id: str) -> bool:
    """Ready, its file the recorded size and modification time, its record
    whole, and this install able to run it."""
    rec = record(model_id)
    if rec is None or rec.get("status") != "ready":
        return False
    if file_state(model_id, rec) != "ok" or not _spec_ok(rec):
        return False
    return requirements_met()


def choices_for(stem: str) -> tuple[str, ...]:
    """The usable models that declare ``stem``, oldest first."""
    found: list[str] = []
    for model_id in ids():
        rec = record(model_id)
        if rec is not None and stem in rec.get("stems", ()) and usable(model_id):
            found.append(model_id)
    return tuple(found)


def name_of(model_id: str) -> str | None:
    rec = record(model_id)
    return rec.get("name") if rec is not None else None


def find_by_sha(sha256: str, *, exclude: str | None = None) -> str | None:
    """The id whose model file has this SHA-256, other than ``exclude``."""
    with _lock:
        for model_id, entry in _ensure()["models"].items():
            if model_id == exclude:
                continue
            if (entry.get("file") or {}).get("sha256") == sha256:
                return model_id
    return None


def worker_spec(model_id: str) -> dict:
    """What the separation worker is handed for ``model_id`` (it never reads
    this registry): {"id", "path", "arch", "kwargs", "audio", "outputs",
    "num_stems", "sha256", "size", "mtime_ns", "stems", "format"}.
    Raises UnknownModel when the model is not usable."""
    rec = record(model_id)
    if rec is None or not usable(model_id):
        raise UnknownModel(model_id)
    info = rec["file"]
    path = model_dir(model_id) / info["name"]
    return {
        "id": model_id,
        "path": str(path),
        "arch": rec["arch"],
        "kwargs": copy.deepcopy(rec["kwargs"]),
        "audio": dict(rec["audio"]),
        "outputs": dict(rec["outputs"]),
        "num_stems": int(rec["kwargs"]["num_stems"]),
        "sha256": info["sha256"],
        "size": info["size"],
        "mtime_ns": info["mtime_ns"],
        "stems": list(rec["stems"]),
        "format": rec.get("format"),
    }


# The only links a record can hold: what hf_fetch.HfLink.canonical() builds.
_LINK_PREFIX = "https://huggingface.co/"
_MAX_LINK_CHARS = 400


def _public_link(source: dict) -> str | None:
    """The Hugging Face link a model was added from, or None (a model from
    files, or a record from before links were kept). Read back from the
    registry, so checked again: https on huggingface.co, no whitespace or
    quote, bounded."""
    link = source.get("link") if source.get("kind") == "hf" else None
    if not isinstance(link, str) or not link.startswith(_LINK_PREFIX):
        return None
    if len(link) > _MAX_LINK_CHARS or any(c.isspace() or c in "\"'<>\\" for c in link):
        return None
    return link


def public(model_id: str, rec: dict | None = None) -> dict | None:
    """The entry as GET /api/my-models serves it. No path, no link query."""
    rec = rec if rec is not None else record(model_id)
    if rec is None:
        return None
    state = file_state(model_id, rec)
    status = rec.get("status")
    missing = status == "ready" and state == "missing"
    code = rec.get("code")
    if status == "ready" and state == "changed":
        code = "changed"
    with _lock:
        progress = dict(_progress.get(model_id) or {})
    source = rec.get("source") or {}
    info = rec.get("file") or {}
    check = rec.get("check") or {}
    return {
        "id": model_id,
        "name": rec.get("name"),
        "status": MISSING if missing else status,
        "pct": progress.get("pct", 100 if status in ("ready", "refused") else 0),
        "step": progress.get("step"),
        "code": code,
        "size": info.get("size"),
        "sha256": info.get("sha256"),
        "stems": list(rec.get("stems") or ()),
        "arch": rec.get("arch"),
        "params": rec.get("params"),
        "source": {
            "kind": source.get("kind"),
            "label": source.get("label"),
            "license_declared": source.get("license_declared"),
            # The link the person added it from (hf_fetch.HfLink.canonical,
            # rebuilt from checked parts), for Settings' "Source" line.
            "link": _public_link(source),
        },
        "added_at": rec.get("added_at"),
        "checked_at": rec.get("checked_at"),
        "cpu_rtf": check.get("cpu_rtf"),
        "usable": status == "ready" and state == "ok" and usable(model_id),
        "missing": missing,
    }


def get(model_id: str) -> dict | None:
    """The public entry of ``model_id`` (see public()), or None."""
    return public(model_id)


def list_models() -> list[dict]:
    """Every public entry, oldest first."""
    out: list[dict] = []
    for model_id in ids():
        entry = public(model_id)
        if entry is not None:
            out.append(entry)
    return out


# ── writing ──


def new_entry(
    *, name: str, stems: list[str], source: dict, licence_confirmed_at: float, status: str
) -> str:
    """Save a new entry, then make its folder; returns its id. The entry is
    on disk before the folder exists, so a crash between the two leaves an
    entry reconcile() ends, never a folder no entry names.
    Raises LimitReached when the list is full."""
    if status not in WORKING:
        raise ValueError("a new entry starts as work in progress")
    with _lock:
        models = _ensure()["models"]
        if len(models) >= MAX_USER_MODELS:
            raise LimitReached()
        model_id = f"user_{uuid.uuid4().hex[:12]}"
        while model_id in models or (root() / model_id).exists():
            model_id = f"user_{uuid.uuid4().hex[:12]}"
        now = time.time()
        models[model_id] = {
            "name": name,
            "arch": None,
            "stems": list(stems),
            "outputs": None,
            "format": None,
            "file": None,
            "config": None,
            "kwargs": None,
            "audio": None,
            "params": None,
            "source": dict(source),
            "licence_confirmed_at": licence_confirmed_at,
            "added_at": now,
            "checked_at": None,
            "status": status,
            "code": None,
            "check": None,
        }
        _progress[model_id] = {"pct": 0, "step": None}
        _save()
    model_dir(model_id).mkdir(parents=True, exist_ok=False)
    return model_id


# Fields set_status may change. The id, name, stems and the dates of adding
# are fixed when the entry is made; the source gains its commit and declared
# licence once the repository has answered.
_UPDATABLE = frozenset(
    {"arch", "outputs", "format", "file", "config", "kwargs", "audio", "params", "check", "source"}
)


def set_status(model_id: str, status: str, *, code: str | None = None, **fields: object) -> bool:
    """Set the status (and its reason code) and any of the _UPDATABLE fields,
    saved before this returns. "ready" and "refused" also set checked_at when
    a check produced them (``checked=True``). Returns whether it was saved."""
    if status not in STATUSES:
        raise ValueError("not a status")
    checked = bool(fields.pop("checked", False))
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError("not an updatable field")
    with _lock:
        entry = _ensure()["models"].get(model_id)
        if entry is None:
            raise UnknownModel(model_id)
        entry.update(copy.deepcopy(fields))
        entry["status"] = status
        entry["code"] = code
        if checked:
            entry["checked_at"] = time.time()
        if status in ("ready", "refused"):
            _progress.pop(model_id, None)
        return _save()


def set_progress(model_id: str, *, pct: int | None = None, step: str | None = None) -> None:
    """Progress of the work on an entry, in memory only."""
    with _lock:
        if model_id not in _ensure()["models"]:
            return
        current = _progress.setdefault(model_id, {"pct": 0, "step": None})
        if pct is not None:
            current["pct"] = max(0, min(100, int(pct)))
        if step is not None:
            current["step"] = step


def _remove_tree(folder: Path) -> bool:
    """Remove ``folder``; True when it is gone. A link is unlinked, never
    followed. Tried twice: on Windows a process that just ended (the check's
    subprocess) can hold a file for a moment."""
    for attempt in (1, 2):
        try:
            if folder.is_symlink():
                folder.unlink()
                return True
            if not folder.exists():
                return True
            shutil.rmtree(folder)
            return True
        except OSError:
            logger.warning(
                "my models: could not remove %s (attempt %d)", folder, attempt, exc_info=True
            )
            time.sleep(0.2)
    return not folder.exists() and not folder.is_symlink()


def remove(model_id: str) -> bool:
    """Remove the folder of ``model_id``, then its entry. True when both are
    gone. When bytes remain the entry is kept with code "busy" and this
    returns False, so the list never claims a model removed while its files
    are still on disk. Raises UnknownModel for an id with no entry.

    Does not touch settings: the caller scrubs settings.stem_models after this
    returns, with this module's lock released (the lock order above)."""
    with _lock:
        if model_id not in _ensure()["models"]:
            raise UnknownModel(model_id)
    folder = model_dir(model_id)
    gone = _remove_tree(folder)
    with _lock:
        models = _ensure()["models"]
        if model_id not in models:
            return gone
        if gone:
            del models[model_id]
            _progress.pop(model_id, None)
        else:
            models[model_id]["code"] = "busy"
        _save()
    return gone


def clear_files(model_id: str) -> bool:
    """Empty the folder of ``model_id`` (a download that was refused leaves
    nothing worth keeping) and forget its file and config. The entry stays,
    with whatever status the caller sets. True when the folder is empty."""
    with _lock:
        if model_id not in _ensure()["models"]:
            raise UnknownModel(model_id)
    folder = model_dir(model_id)
    gone = _remove_tree(folder)
    if gone:
        folder.mkdir(parents=True, exist_ok=True)
    with _lock:
        entry = _ensure()["models"].get(model_id)
        if entry is not None and gone:
            entry["file"] = None
            entry["config"] = None
            entry["format"] = None
            _save()
    return gone


def drop_entry(model_id: str) -> None:
    """Remove an entry whose folder holds nothing worth keeping (a cancelled
    or failed download), folder first. Never raises for an unknown id."""
    try:
        remove(model_id)
    except UnknownModel:
        pass


# ── startup ──


def _holds_model_file(folder: Path) -> bool:
    return any((folder / name).is_file() for name in MODEL_FILES.values())


def _sweep_partials(folder: Path) -> None:
    from app.core.model_download import sweep_partials

    for name in (MODEL_STAGING_NAME, CONFIG_FILE, *MODEL_FILES.values()):
        sweep_partials(folder, name)


def reconcile() -> None:
    """Settle what a stop left behind, at startup. Never raises.

    - staging or downloading: the work is lost and nothing is usable; the
      folder and the entry go.
    - checking: refused with "interrupted"; the files are whole, and Check
      again is one click.
    - ready: nothing is read here; usable() asks the disk when it matters.
    - a folder with an id's name and no entry: removed when it holds no
      finished model file and is older than ORPHAN_MIN_AGE_SEC; kept, and
      said in the log, when it holds one: a lost registry must not take the
      user's copies with it.
    - a partial write older than model_download.PARTIAL_MAX_AGE_SEC: removed.
    """
    try:
        _reconcile()
    except Exception:  # noqa: BLE001 -- startup must go on
        logger.warning("my models: could not settle the models folder", exc_info=True)


def _reconcile() -> None:
    base = root()
    with _lock:
        models = _ensure()["models"]
        lost = [i for i, e in models.items() if e.get("status") in ("staging", "downloading")]
        interrupted = [i for i, e in models.items() if e.get("status") == "checking"]
        for model_id in interrupted:
            models[model_id]["status"] = "refused"
            models[model_id]["code"] = "interrupted"
        if interrupted:
            _save()
        known = set(models)
    for model_id in lost:
        logger.warning("my models: %s was still downloading at the last stop; removed", model_id)
        drop_entry(model_id)
        known.discard(model_id)
    if not base.is_dir():
        return
    now = time.time()
    for child in base.iterdir():
        if not is_user_id(child.name) or child.is_symlink() or not child.is_dir():
            continue
        _sweep_partials(child)
        if child.name in known:
            continue
        if _holds_model_file(child):
            logger.warning(
                "my models: %s holds a model file but no entry names it; kept", child.name
            )
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age >= ORPHAN_MIN_AGE_SEC:
            _remove_tree(child)
