"""MIDI from a finished song's piano or bass stem, on request.

POST /api/jobs/{id}/midi (app/api/midi.py) starts a run; this module does
it. The notes come from Spotify's Basic Pitch (Apache-2.0), vendored with its
ONNX model in app/_vendor/transcribe and run on the CPU in a fresh worker
process (midi_worker.py), so the server process never imports it. One run at
a time across the whole server: a run is seconds of CPU, and two at once
would only halve each.

Files, per song:

- ``midi/<stem>.mid``, the file. A run writes it under a temp name in the
  same folder, synced to disk, and renames it into place only once it is
  complete: a run that fails, is cancelled or is cut off by a crash leaves
  the file the song already had, byte for byte.
- ``midi.json``, what made each file: engine, version, the vendored build,
  the detail and parameters, notes, tempo, timings.
- ``Job.midi``, the state the page reads (clean_midi), kept in registry.json
  and metadata.json like every other saved field.

The tempo comes from the song's beat grid when it is trusted
(midi_file.tempo_plan), else 120 BPM. Notes are never moved onto it.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import (
    MIDI_DETAIL_PRESETS,
    MIDI_DETAILS,
    MIDI_STEM_RANGES,
    MIDI_STEMS,
    TIMEOUT_MIDI,
    TIMEOUT_MIDI_STALL,
    TRANSCRIBE_MAX_SECONDS,
)
from app.core.models import Job, _set, clean_midi
from app.core.registry import set_proc
from app.core.settings import fsync_dir
from app.core.speed_test import stop_process
from app.pipeline.midi_file import (
    ENGINE_ID,
    ENGINE_LABEL,
    ENGINE_LICENCE,
    MANIFEST_PATH,
    MODEL_PATH,
    VENDOR_DIR,
    engine_version,
    tempo_plan,
    vendor_id,
)
from app.pipeline.stem_reextract import _replace as replace_held
from app.pipeline.stem_reextract import _unlink as unlink_held

logger = logging.getLogger("stemdeck.midi")

MIDI_DIRNAME = "midi"
INFO_FILENAME = "midi.json"
_INFO_VERSION = 1

# What the vendored engine imports from the installed runtime. onnxruntime
# and resampy come with audio-separator, which uv.lock leaves out on Intel
# macOS; the rest are librosa's and the vendored packages' own dependencies.
# Looked up, never imported: the server process must not load them.
_RUNTIME_MODULES = (
    "onnxruntime",
    "numpy",
    "scipy",
    "librosa",
    "soundfile",
    "resampy",
    "decorator",
    "six",
    "packaging",
)

# Error codes a run can end with (Job.midi[stem]["error_code"]).
ERROR_CODES = (
    "too_long",
    "no_audio",
    "engine",
    "disk",
    "io",
    "timeout",
    "stalled",
    "interrupted",
)

# Set when the worker could not import the engine at all: every later run
# would fail the same way, so the status says so until a restart.
_engine_broken = False


def _intel_mac() -> bool:
    return sys.platform == "darwin" and platform.machine() == "x86_64"


@lru_cache(maxsize=1)
def _installed() -> tuple[bool, str | None]:
    if _intel_mac():
        return False, "unsupported_platform"
    for name in _RUNTIME_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                return False, "no_runtime"
        except (ImportError, ValueError):
            return False, "no_runtime"
    if (
        not MODEL_PATH.is_file()
        or not MANIFEST_PATH.is_file()
        or not (VENDOR_DIR / "basic_pitch" / "inference.py").is_file()
        or engine_version() is None
    ):
        return False, "engine_error"
    return True, None


def availability() -> tuple[bool, str | None]:
    """(available, reason): reason None, "unsupported_platform" (Intel macOS,
    which has no onnxruntime in uv.lock), "no_runtime" (a module the engine
    needs is not installed) or "engine_error" (the vendored engine is
    incomplete, or failed to import in a worker since the server started).
    Cheap: file and module lookups once, never an import of the engine."""
    available, reason = _installed()
    if available and _engine_broken:
        return False, "engine_error"
    return available, reason


def status() -> dict[str, Any]:
    """GET /api/transcribe/status."""
    available, reason = availability()
    return {
        "available": available,
        "reason": reason,
        "engines": [
            {
                "id": ENGINE_ID,
                "label": ENGINE_LABEL,
                "version": engine_version(),
                "licence": ENGINE_LICENCE,
            }
        ],
        "stems": list(MIDI_STEMS),
        "max_seconds": TRANSCRIBE_MAX_SECONDS,
        "details": list(MIDI_DETAILS),
    }


# -- files


def midi_dir(job_dir: Path) -> Path:
    return job_dir / MIDI_DIRNAME


def midi_path(job_dir: Path, stem: str) -> Path:
    """``stem`` must already be one of MIDI_STEMS."""
    return midi_dir(job_dir) / f"{stem}.mid"


def info_path(job_dir: Path) -> Path:
    return job_dir / INFO_FILENAME


def _temp_name(stem: str) -> str:
    return f".{stem}.{uuid.uuid4().hex}.mid.tmp"


def _sweep_temps(job_dir: Path, stem: str) -> None:
    """Remove what a run of ``stem`` cut off by a crash left behind."""
    folder = midi_dir(job_dir)
    if folder.is_dir():
        for leftover in folder.glob(f".{stem}.*.mid.tmp"):
            leftover.unlink(missing_ok=True)


def read_grid(stems_dir: Path) -> tuple[dict[str, Any] | None, bool]:
    """(beats.json with the user's edits applied, whether there were edits).
    The grid an export clicks to: the same rule as stems._read_beat_grid."""
    computed = stems_dir / "beats.json"
    try:
        grid = json.loads(computed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    if not isinstance(grid, dict):
        return None, False
    user = stems_dir / "beats.user.json"
    if user.is_file():
        try:
            edits = json.loads(user.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("ignoring unreadable beat edits in %s", stems_dir)
            return grid, False
        if isinstance(edits, dict) and isinstance(edits.get("beats"), list) and edits["beats"]:
            return {**grid, "beats": edits["beats"], "bars": edits.get("bars") or []}, True
    return grid, False


def song_tempo(job_dir: Path) -> dict[str, Any]:
    grid, edited = read_grid(job_dir / "stems")
    return tempo_plan(grid, edited=edited)


def run_params(stem: str, detail: str) -> dict[str, Any]:
    """Basic Pitch's parameters for ``stem`` at ``detail``."""
    low, high = MIDI_STEM_RANGES[stem]
    return {
        **MIDI_DETAIL_PRESETS[stem][detail],
        "minimum_frequency": low,
        "maximum_frequency": high,
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_code(exc: OSError) -> str:
    return "disk" if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else "io"


# midi.json is read, changed and replaced by a run finishing and by a
# delete; this orders them.
_info_lock = threading.Lock()


def read_info(job_dir: Path) -> dict[str, Any]:
    """midi.json's per-stem records, {} when there is none or it is damaged."""
    try:
        data = json.loads(info_path(job_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    stems = data.get("stems") if isinstance(data, dict) else None
    if not isinstance(stems, dict):
        return {}
    return {s: v for s, v in stems.items() if s in MIDI_STEMS and isinstance(v, dict)}


def _write_info(job_dir: Path, stems: dict[str, Any]) -> None:
    """Replace midi.json, flushed and synced before the rename, or remove it
    when no stem has a record. Raises OSError."""
    path = info_path(job_dir)
    if not stems:
        path.unlink(missing_ok=True)
        return
    temp = job_dir / f".{INFO_FILENAME}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"v": _INFO_VERSION, "stems": stems}, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    fsync_dir(job_dir)


# -- runs


@dataclass
class Run:
    """The one MIDI run in progress: which song and stem, and how to stop it.
    ``reason`` is what a stop is recorded as: "cancelled" when the user asked,
    "interrupted" when the server is stopping."""

    job_id: str
    stem: str
    detail: str
    cancel: threading.Event = field(default_factory=threading.Event)
    reason: str = "cancelled"
    task: Any = None


_runs_lock = threading.Lock()
_current: Run | None = None


def current() -> Run | None:
    with _runs_lock:
        return _current


def claim(job: Job, stem: str, detail: str, has_file: bool) -> Run | None:
    """Take the server's one run slot for ``job``'s ``stem`` and mark it
    running, or None when a run holds it. The caller checks everything else
    first, with no await between that check and this."""
    global _current
    with _runs_lock:
        if _current is not None:
            return None
        run = Run(job.id, stem, detail)
        _current = run
    runs = dict(clean_midi(job.midi) or {})
    previous = runs.get(stem) or {}
    runs[stem] = {
        "state": "running",
        "pct": 0,
        # The file a failed or cancelled run leaves: still the old one's.
        "notes": previous.get("notes") if has_file else None,
        "engine": ENGINE_ID,
        "detail": detail,
        "tempo_source": previous.get("tempo_source") if has_file else None,
        "at": _now_iso(),
        "error_code": None,
        "has_file": has_file,
    }
    _set(job, midi=runs)
    return run


def _release(run: Run) -> None:
    global _current
    with _runs_lock:
        if _current is run:
            _current = None


def cancel(job_id: str, stem: str, reason: str = "cancelled") -> Run | None:
    """Ask the run of ``job_id``'s ``stem`` to stop. Returns it, or None when
    no such run is in progress. The run's own thread stops the worker and
    every process it started within a tenth of a second, and records it."""
    run = current()
    if run is None or run.job_id != job_id or run.stem != stem:
        return None
    run.reason = reason
    run.cancel.set()
    return run


def cancel_all() -> None:
    """The server is stopping: the run in progress ends as interrupted."""
    run = current()
    if run is not None:
        run.reason = "interrupted"
        run.cancel.set()


def _update(job: Job, stem: str, **fields: Any) -> None:
    runs = dict(clean_midi(job.midi) or {})
    entry = dict(runs.get(stem) or {})
    entry.update(fields)
    runs[stem] = entry
    _set(job, midi=clean_midi(runs))


def _spawn_worker_cmd(
    audio: Path, out: Path, stem: str, params: dict[str, Any], plan: dict[str, Any]
) -> list[str]:
    """The worker invocation. Module-level seam so tests can swap in a stub,
    the way transcribe.py exposes its own."""
    return [
        sys.executable,
        "-m",
        "app.pipeline.midi_worker",
        "--audio",
        str(audio),
        "--out",
        str(out),
        "--stem",
        stem,
        "--params",
        json.dumps(params, separators=(",", ":")),
        "--tempo",
        json.dumps(plan, separators=(",", ":")),
        "--max-seconds",
        str(TRANSCRIBE_MAX_SECONDS),
        "--vendor",
        str(VENDOR_DIR),
        "--model",
        str(MODEL_PATH),
    ]


@dataclass
class _Outcome:
    """How the worker ended: its last event, or why it was stopped."""

    event: dict[str, Any] | None
    stopped: str | None  # "cancelled", "timeout", "stalled" or None
    returncode: int | None
    tail: list[str]


def parse_event(line: str) -> dict[str, Any] | None:
    """One protocol line as {"event", ...}, or None when it is not one."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("event") not in ("progress", "done", "error"):
        return None
    return value


def _run_worker(job: Job, run: Run, cmd: list[str], on_pct: Callable[[int], None]) -> _Outcome:
    """Run the worker to its end, a cancel, the total timeout or a stall,
    whichever comes first. Always reaped, with every process it started."""
    env = os.environ.copy()
    # The worker hard-exits when this process disappears (app/core/process.py).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    proc = subprocess.Popen(  # noqa: S603 -- fixed args: our interpreter and module
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        # Its own process group on POSIX, so a stop reaches what it started.
        start_new_session=os.name != "nt",
    )
    lock = threading.Lock()
    last_output = [time.monotonic()]
    events: list[dict[str, Any]] = []
    tail: deque[str] = deque(maxlen=40)

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            event = parse_event(line)
            with lock:
                last_output[0] = time.monotonic()
                if event is not None:
                    events.append(event)
                elif line.strip():
                    # The protocol is JSON lines only. Anything else is kept
                    # as a diagnostic, never read as an answer.
                    tail.append("stdout: " + line.strip()[:500])

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            with lock:
                last_output[0] = time.monotonic()
                if line.strip():
                    tail.append(line.strip()[:500])

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()
    set_proc(job.id, proc)
    started = time.monotonic()
    stopped: str | None = None
    shown = -1
    seen = 0
    try:
        while proc.poll() is None:
            if run.cancel.is_set():
                stopped = "cancelled"
                break
            now = time.monotonic()
            with lock:
                silent_for = now - last_output[0]
                fresh = events[seen:]
                seen = len(events)
            for event in fresh:
                pct = event.get("pct")
                if event["event"] == "progress" and isinstance(pct, int) and pct != shown:
                    shown = pct
                    on_pct(max(0, min(99, pct)))
            if now - started > TIMEOUT_MIDI:
                stopped = "timeout"
                break
            if silent_for > TIMEOUT_MIDI_STALL:
                stopped = "stalled"
                break
            time.sleep(0.1)
    finally:
        stop_process(proc)
        set_proc(job.id, None)
        for reader in readers:
            reader.join(timeout=2)
    if stopped is None and run.cancel.is_set():
        stopped = "cancelled"
    with lock:
        final = next((e for e in reversed(events) if e["event"] != "progress"), None)
        return _Outcome(final, stopped, proc.returncode, list(tail))


def _record(
    run: Run, params: dict[str, Any], plan: dict[str, Any], event: dict[str, Any], took: float
) -> dict[str, Any]:
    seconds_audio = event.get("seconds_audio")
    return {
        "engine": ENGINE_ID,
        "engine_version": engine_version(),
        "vendor_id": vendor_id(),
        "stem": run.stem,
        "detail": run.detail,
        "params": params,
        "notes": event.get("notes"),
        "tempo_source": plan.get("source"),
        "bpm": plan.get("bpm"),
        "beats_per_bar": plan.get("beats_per_bar"),
        "bar_start": plan.get("bar_start"),
        "created_at": _now_iso(),
        "seconds_audio": seconds_audio if isinstance(seconds_audio, (int, float)) else None,
        "seconds_run": round(took, 2),
    }


def run_blocking(job: Job, job_dir: Path, run: Run, audio: Path) -> None:
    """The run ``claim`` started: worker, file, midi.json, state. Blocking
    (asyncio.to_thread). Never raises; every ending is in Job.midi."""
    global _engine_broken
    started = time.monotonic()
    stem = run.stem
    final = midi_path(job_dir, stem)
    temp = midi_dir(job_dir) / _temp_name(stem)
    had_file = final.is_file()
    try:
        params = run_params(stem, run.detail)
        plan = song_tempo(job_dir)
        try:
            midi_dir(job_dir).mkdir(exist_ok=True)
            _sweep_temps(job_dir, stem)
        except OSError as exc:
            logger.warning("[%s] MIDI %s: cannot write the midi folder: %s", job.id, stem, exc)
            _end(job, run, "error", _write_code(exc), had_file)
            return

        def on_pct(pct: int) -> None:
            _update(job, stem, pct=pct)

        outcome = _run_worker(job, run, _spawn_worker_cmd(audio, temp, stem, params, plan), on_pct)
        if outcome.stopped == "cancelled":
            if run.reason == "interrupted":
                _end(job, run, "error", "interrupted", had_file)
            else:
                _end(job, run, "cancelled", None, had_file)
            logger.info("[%s] MIDI %s %s", job.id, stem, run.reason)
            return
        event = outcome.event or {}
        if outcome.stopped is not None or event.get("event") != "done" or outcome.returncode:
            code = outcome.stopped or event.get("code")
            if code not in ERROR_CODES:
                code = "engine"
            if event.get("phase") == "import":
                _engine_broken = True
            logger.warning(
                "[%s] MIDI %s failed (%s, exit %s): %s",
                job.id,
                stem,
                code,
                outcome.returncode,
                " | ".join(outcome.tail[-5:]) or "no diagnostic output",
            )
            _end(job, run, "error", code, had_file)
            return
        if not temp.is_file():
            logger.warning(
                "[%s] MIDI %s: the worker reported a file it did not write", job.id, stem
            )
            _end(job, run, "error", "engine", had_file)
            return
        took = time.monotonic() - started
        record = _record(run, params, plan, event, took)
        try:
            with _info_lock:
                # The rename is what publishes the new file: complete and
                # synced (midi_file.write_midi), or not at all. Retried while
                # a download of the old file holds it open on Windows.
                replace_held(temp, final)
                fsync_dir(final.parent)
                info = read_info(job_dir)
                info[stem] = record
                try:
                    _write_info(job_dir, info)
                except OSError:
                    # The file is in place and is what the user asked for;
                    # only its provenance record is missing.
                    logger.warning(
                        "[%s] MIDI %s written, but midi.json could not be",
                        job.id,
                        stem,
                        exc_info=True,
                    )
        except OSError as exc:
            logger.warning("[%s] MIDI %s could not be put in place: %s", job.id, stem, exc)
            _end(job, run, "error", _write_code(exc), had_file)
            return
        notes = event.get("notes")
        _update(
            job,
            stem,
            state="done",
            pct=100,
            notes=notes if isinstance(notes, int) else None,
            tempo_source=plan.get("source"),
            at=_now_iso(),
            error_code=None,
            has_file=True,
        )
        logger.info(
            "[%s] MIDI %s: %s notes in %.1fs (%s s of audio, detail %s, tempo %s %s)",
            job.id,
            stem,
            notes,
            took,
            event.get("seconds_audio"),
            run.detail,
            plan.get("source"),
            plan.get("bpm"),
        )
    except Exception:
        logger.exception("[%s] MIDI %s failed", job.id, stem)
        _end(job, run, "error", "engine", final.is_file())
    finally:
        temp.unlink(missing_ok=True)
        _release(run)


def _end(job: Job, run: Run, state: str, code: str | None, has_file: bool) -> None:
    """A run that made no new file: the old one, if any, is still the file,
    with the notes and tempo it was written with."""
    _update(
        job,
        run.stem,
        state=state,
        pct=0,
        at=_now_iso(),
        error_code=code,
        has_file=has_file,
        **({} if has_file else {"notes": None, "tempo_source": None}),
    )


def delete_output(job: Job, job_dir: Path, stem: str) -> bool:
    """Remove ``stem``'s file, its midi.json record and its entry. False when
    there was none of the three. Raises OSError when the file stays."""
    path = midi_path(job_dir, stem)
    runs = dict(clean_midi(job.midi) or {})
    with _info_lock:
        info = read_info(job_dir)
        had = path.is_file() or stem in info or stem in runs
        unlink_held(path)
        _sweep_temps(job_dir, stem)
        if stem in info:
            del info[stem]
            _write_info(job_dir, info)
        folder = midi_dir(job_dir)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    runs.pop(stem, None)
    _set(job, midi=runs or None)
    return had
