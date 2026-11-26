"""MIDI from a finished song's piano or bass stem, on request.

POST /api/jobs/{id}/midi (app/api/midi.py) starts a run; this module does
it, in a fresh worker process (midi_worker.py), so the server process never
imports an engine. One run at a time across the whole server: two at once
would only halve each.

Two engines (config.MIDI_PIANO_ENGINES):

- basic_pitch, Spotify's Basic Pitch (Apache-2.0), vendored with its ONNX
  model in app/_vendor/transcribe, on the CPU. Every bass run, and a piano
  run unless the setting says otherwise.
- piano_hr, ByteDance's high-resolution piano transcription (app/pipeline/
  piano_hr.py), for the piano when settings.transcribe_piano_engine picks
  it: on the GPU when the separation device setting resolves to CUDA, else
  on the CPU (MPS is not used: never measured on a Mac). Its checkpoint is
  fetched on the first run that needs it, by app/core/model_fetch.py in a
  process of its own (the worker has no network), with the run's progress
  and cancel; a run on the GPU that fails is run again, whole, on the CPU.

Files, per song:

- ``midi/<stem>.mid``, the file. A run writes it under a temp name in the
  same folder, synced to disk, and renames it into place only once it is
  complete: a run that fails, is cancelled or is cut off by a crash leaves
  the file the song already had, byte for byte.
- ``midi.json``, what made each file: engine, version, the vendored build,
  the device, the detail and parameters, notes, tempo, timings.
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
    MIDI_PIANO_ENGINES,
    MIDI_PIANO_HR_PRESETS,
    MIDI_STEM_RANGES,
    MIDI_STEMS,
    PIANO_HR_BATCH_CPU,
    PIANO_HR_BATCH_GPU,
    PIANO_HR_CHECKPOINT_SHA256,
    PIANO_HR_CHECKPOINT_SIZE,
    PIANO_HR_CHECKPOINT_URL,
    PIANO_HR_GPU_MIN_FREE_MB,
    TIMEOUT_MIDI,
    TIMEOUT_MIDI_MODEL_DOWNLOAD,
    TIMEOUT_MIDI_PIANO_HR,
    TIMEOUT_MIDI_STALL,
    TRANSCRIBE_MAX_SECONDS,
)
from app.core.models import Job, _set, clean_midi
from app.core.registry import set_proc
from app.core.settings import fsync_dir, get_demucs_device, get_transcribe_piano_engine
from app.core.speed_test import stop_process
from app.pipeline import piano_hr
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

# What piano_hr imports from the installed runtime besides _RUNTIME_MODULES'
# numpy, scipy, librosa and soundfile. torch is the one StemDeck already ships.
_PIANO_HR_MODULES = ("torch", "numpy", "scipy", "librosa", "soundfile")

# Error codes a run can end with (Job.midi[stem]["error_code"]). "download":
# the piano_hr checkpoint could not be fetched, or what arrived was not it.
ERROR_CODES = (
    "too_long",
    "no_audio",
    "engine",
    "disk",
    "io",
    "timeout",
    "stalled",
    "interrupted",
    "download",
)

# The engines whose worker could not import them at all: every later run of
# one would fail the same way, so the status says so until a restart.
_engine_broken: set[str] = set()


def _intel_mac() -> bool:
    return sys.platform == "darwin" and platform.machine() == "x86_64"


def _missing(modules: tuple[str, ...]) -> bool:
    for name in modules:
        try:
            if importlib.util.find_spec(name) is None:
                return True
        except (ImportError, ValueError):
            return True
    return False


@lru_cache(maxsize=1)
def _installed() -> tuple[bool, str | None]:
    if _intel_mac():
        return False, "unsupported_platform"
    if _missing(_RUNTIME_MODULES):
        return False, "no_runtime"
    if (
        not MODEL_PATH.is_file()
        or not MANIFEST_PATH.is_file()
        or not (VENDOR_DIR / "basic_pitch" / "inference.py").is_file()
        or engine_version() is None
    ):
        return False, "engine_error"
    return True, None


@lru_cache(maxsize=1)
def _piano_hr_installed() -> tuple[bool, str | None]:
    # Intel macOS has no Basic Pitch runtime, so no MIDI at all, and torch
    # there is 2.2 with numpy 1: never run, so never offered.
    if _intel_mac():
        return False, "unsupported_platform"
    if _missing(_PIANO_HR_MODULES):
        return False, "no_runtime"
    if not piano_hr.vendored():
        return False, "engine_error"
    return True, None


def availability() -> tuple[bool, str | None]:
    """(available, reason) of MIDI at all, which is Basic Pitch's: it makes
    every bass file. reason None, "unsupported_platform" (Intel macOS, which
    has no onnxruntime in uv.lock), "no_runtime" (a module the engine needs
    is not installed) or "engine_error" (the vendored engine is incomplete,
    or failed to import in a worker since the server started). Cheap: file
    and module lookups once, never an import of the engine."""
    available, reason = _installed()
    if available and ENGINE_ID in _engine_broken:
        return False, "engine_error"
    return available, reason


def engine_availability(engine: str) -> tuple[bool, str | None]:
    """availability() for one engine. piano_hr also needs Basic Pitch's
    runtime (MIDI as a whole) and torch; its checkpoint is not needed, the
    first run fetches it."""
    available, reason = availability()
    if engine == ENGINE_ID or not available:
        return available, reason
    if engine != piano_hr.ENGINE_ID:
        return False, "engine_error"
    available, reason = _piano_hr_installed()
    if available and piano_hr.ENGINE_ID in _engine_broken:
        return False, "engine_error"
    return available, reason


def engine_for(stem: str, piano_engine: str) -> str:
    """The engine a run of ``stem`` uses: the piano setting's for the piano,
    Basic Pitch for the bass, which piano_hr cannot hear."""
    if stem == "piano" and piano_engine in MIDI_PIANO_ENGINES:
        return piano_engine
    return ENGINE_ID


def status() -> dict[str, Any]:
    """GET /api/transcribe/status."""
    available, reason = availability()
    hr_available, hr_reason = engine_availability(piano_hr.ENGINE_ID)
    return {
        "available": available,
        "reason": reason,
        "engines": [
            {
                "id": ENGINE_ID,
                "label": ENGINE_LABEL,
                "version": engine_version(),
                "licence": ENGINE_LICENCE,
                "model_licence": ENGINE_LICENCE,
                "stems": list(MIDI_STEMS),
                "available": available,
                "reason": reason,
                "model": "built_in",
                "download_bytes": None,
            },
            {
                "id": piano_hr.ENGINE_ID,
                "label": piano_hr.ENGINE_LABEL,
                "version": piano_hr.engine_version(),
                "licence": piano_hr.ENGINE_LICENCE,
                "model_licence": piano_hr.MODEL_LICENCE,
                "stems": ["piano"],
                "available": hr_available,
                "reason": hr_reason,
                # "downloaded", "partial" or "not_downloaded": the size on
                # disk only; a run checks the SHA-256 before it uses it.
                "model": piano_hr.checkpoint_state(),
                "download_bytes": PIANO_HR_CHECKPOINT_SIZE,
            },
        ],
        "piano_engine": get_transcribe_piano_engine(),
        "piano_engines": list(MIDI_PIANO_ENGINES),
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


def run_params(stem: str, detail: str, engine: str = ENGINE_ID) -> dict[str, Any]:
    """The engine's parameters for ``stem`` at ``detail``."""
    if engine == piano_hr.ENGINE_ID:
        return dict(MIDI_PIANO_HR_PRESETS[detail])
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
    engine: str = ENGINE_ID
    cancel: threading.Event = field(default_factory=threading.Event)
    reason: str = "cancelled"
    task: Any = None


_runs_lock = threading.Lock()
_current: Run | None = None


def current() -> Run | None:
    with _runs_lock:
        return _current


def claim(job: Job, stem: str, detail: str, has_file: bool, engine: str = ENGINE_ID) -> Run | None:
    """Take the server's one run slot for ``job``'s ``stem`` and mark it
    running, or None when a run holds it. The caller checks everything else
    first, with no await between that check and this."""
    global _current
    with _runs_lock:
        if _current is not None:
            return None
        run = Run(job.id, stem, detail, engine)
        _current = run
    runs = dict(clean_midi(job.midi) or {})
    previous = runs.get(stem) or {}
    runs[stem] = {
        "state": "running",
        "pct": 0,
        "phase": "transcribe",
        # The file a failed or cancelled run leaves: still the old one's.
        "notes": previous.get("notes") if has_file else None,
        "engine": engine,
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
    audio: Path,
    out: Path,
    stem: str,
    params: dict[str, Any],
    plan: dict[str, Any],
    engine: str = ENGINE_ID,
    device: str = "cpu",
) -> list[str]:
    """The worker invocation. Module-level seam so tests can swap in a stub,
    the way transcribe.py exposes its own."""
    extra: list[str] = []
    if engine == piano_hr.ENGINE_ID:
        extra = [
            "--engine",
            engine,
            "--piano-vendor",
            str(piano_hr.VENDOR_DIR),
            "--checkpoint",
            str(piano_hr.checkpoint_path()),
            "--device",
            device,
            "--batch",
            str(PIANO_HR_BATCH_GPU if device == "cuda" else PIANO_HR_BATCH_CPU),
            "--gpu-min-free-mb",
            str(PIANO_HR_GPU_MIN_FREE_MB),
        ]
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
        *extra,
    ]


def _spawn_fetch_cmd(dst: Path) -> list[str]:
    """The piano_hr checkpoint's download (app/core/model_fetch.py): the
    pinned address, size and SHA-256, into MODELS_DIR. A seam for tests."""
    return [
        sys.executable,
        "-m",
        "app.core.model_fetch",
        "--url",
        PIANO_HR_CHECKPOINT_URL,
        "--dst",
        str(dst),
        "--sha256",
        PIANO_HR_CHECKPOINT_SHA256,
        "--size",
        str(PIANO_HR_CHECKPOINT_SIZE),
    ]


@dataclass
class _Outcome:
    """How the worker ended: its last event, or why it was stopped."""

    event: dict[str, Any] | None
    stopped: str | None  # "cancelled", "timeout", "stalled" or None
    returncode: int | None
    tail: list[str]
    # The device the worker said it runs on, in any event, or None.
    device: str | None = None


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


def _run_worker(
    job: Job,
    run: Run,
    cmd: list[str],
    on_pct: Callable[[int], None],
    timeout: float | None = None,
    stall: float | None = None,
) -> _Outcome:
    """Run the worker (or the checkpoint's download) to its end, a cancel,
    ``timeout`` (TIMEOUT_MIDI) or ``stall`` seconds with no output
    (TIMEOUT_MIDI_STALL), whichever comes first. Always reaped, with every
    process it started."""
    timeout = TIMEOUT_MIDI if timeout is None else timeout
    stall = TIMEOUT_MIDI_STALL if stall is None else stall
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
            if now - started > timeout:
                stopped = "timeout"
                break
            if silent_for > stall:
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
        device = next((e["device"] for e in reversed(events) if e.get("device")), None)
        return _Outcome(final, stopped, proc.returncode, list(tail), device)


def _record(
    run: Run,
    params: dict[str, Any],
    plan: dict[str, Any],
    event: dict[str, Any],
    took: float,
    gpu_fallback: bool = False,
) -> dict[str, Any]:
    seconds_audio = event.get("seconds_audio")
    device = event.get("device")
    hr = run.engine == piano_hr.ENGINE_ID
    return {
        "engine": run.engine,
        "engine_version": piano_hr.engine_version() if hr else engine_version(),
        "vendor_id": piano_hr.vendor_id() if hr else vendor_id(),
        # The weights a piano_hr file was made with (Basic Pitch's ship in
        # the vendored build vendor_id names).
        "model_sha256": PIANO_HR_CHECKPOINT_SHA256 if hr else None,
        "device": device if device in ("cpu", "cuda") else None,
        "gpu_fallback": gpu_fallback,
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


def _failed(outcome: _Outcome) -> bool:
    event = outcome.event or {}
    return outcome.stopped is not None or event.get("event") != "done" or bool(outcome.returncode)


def _failure_code(outcome: _Outcome) -> str:
    code = outcome.stopped or (outcome.event or {}).get("code")
    return code if code in ERROR_CODES else "engine"


# Endings a run on the CPU would end with too: the input or the disk, not the
# GPU. Any other failure on the GPU is run again, whole, on the CPU.
_NOT_THE_GPU = ("too_long", "no_audio", "disk", "io")


def _gpu_retry(outcome: _Outcome, asked: str) -> bool:
    if asked != "cuda" or outcome.stopped == "cancelled" or not _failed(outcome):
        return False
    if (outcome.event or {}).get("phase") == "import":
        return False
    if outcome.device == "cpu":
        # The worker had already chosen the CPU (too little GPU memory).
        return False
    return _failure_code(outcome) not in _NOT_THE_GPU


def piano_hr_device() -> str:
    """The device a piano_hr run asks for: CUDA when the separation device
    setting resolves to it (read per run, so a change applies to the next),
    else the CPU. MPS is never asked for: the model's GRUs and its STFT
    convolutions were never measured on one."""
    try:
        return "cuda" if get_demucs_device() == "cuda" else "cpu"
    except Exception:  # noqa: BLE001 -- a device probe that fails is the CPU
        logger.warning("MIDI: the device probe failed, piano_hr runs on the CPU", exc_info=True)
        return "cpu"


def _ensure_checkpoint(job: Job, run: Run) -> str | None:
    """The piano_hr checkpoint on disk and checked against its pinned SHA-256
    before it is used, downloaded first when it is missing or is not that
    file. None when it is ready, "cancelled", or an error code."""
    path = piano_hr.checkpoint_path()
    if piano_hr.checkpoint_ok(path):
        return None
    if piano_hr.remove_bad_checkpoint(path):
        logger.warning("[%s] MIDI: the piano_hr checkpoint was not the pinned file", job.id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[%s] MIDI: cannot write the models folder: %s", job.id, exc)
        return _write_code(exc)
    _update(job, run.stem, phase="download", pct=0)
    logger.info("[%s] MIDI: downloading the piano_hr checkpoint", job.id)
    outcome = _run_worker(
        job,
        run,
        _spawn_fetch_cmd(path),
        lambda pct: _update(job, run.stem, pct=pct),
        timeout=TIMEOUT_MIDI_MODEL_DOWNLOAD,
    )
    if outcome.stopped == "cancelled":
        return "cancelled"
    if _failed(outcome):
        # A full disk or a folder that cannot be written is said as such;
        # anything else, a stall or the time limit included, is a download
        # that did not arrive, which is what the person can act on.
        code = (outcome.event or {}).get("code")
        code = code if code in ("disk", "io") and outcome.stopped is None else "download"
        logger.warning(
            "[%s] MIDI: the piano_hr checkpoint could not be downloaded (%s): %s",
            job.id,
            code,
            " | ".join(outcome.tail[-3:]) or "no diagnostic output",
        )
        return code
    if not piano_hr.checkpoint_ok(path):
        logger.warning("[%s] MIDI: the downloaded piano_hr checkpoint did not match", job.id)
        piano_hr.remove_bad_checkpoint(path)
        return "download"
    _update(job, run.stem, phase="transcribe", pct=0)
    return None


def _stopped(job: Job, run: Run, had_file: bool) -> None:
    if run.reason == "interrupted":
        _end(job, run, "error", "interrupted", had_file)
    else:
        _end(job, run, "cancelled", None, had_file)
    logger.info("[%s] MIDI %s %s", job.id, run.stem, run.reason)


def run_blocking(job: Job, job_dir: Path, run: Run, audio: Path) -> None:
    """The run ``claim`` started: the checkpoint (piano_hr), the worker, the
    file, midi.json, the state. Blocking (asyncio.to_thread). Never raises;
    every ending is in Job.midi."""
    started = time.monotonic()
    stem = run.stem
    hr = run.engine == piano_hr.ENGINE_ID
    final = midi_path(job_dir, stem)
    temp = midi_dir(job_dir) / _temp_name(stem)
    had_file = final.is_file()
    try:
        params = run_params(stem, run.detail, run.engine)
        plan = song_tempo(job_dir)
        try:
            midi_dir(job_dir).mkdir(exist_ok=True)
            _sweep_temps(job_dir, stem)
        except OSError as exc:
            logger.warning("[%s] MIDI %s: cannot write the midi folder: %s", job.id, stem, exc)
            _end(job, run, "error", _write_code(exc), had_file)
            return
        if hr:
            code = _ensure_checkpoint(job, run)
            if code == "cancelled":
                _stopped(job, run, had_file)
                return
            if code is not None:
                _end(job, run, "error", code, had_file)
                return

        def on_pct(pct: int) -> None:
            _update(job, stem, pct=pct)

        def attempt(device: str) -> _Outcome:
            cmd = _spawn_worker_cmd(audio, temp, stem, params, plan, run.engine, device)
            return _run_worker(
                job, run, cmd, on_pct, timeout=TIMEOUT_MIDI_PIANO_HR if hr else TIMEOUT_MIDI
            )

        asked = piano_hr_device() if hr else "cpu"
        outcome = attempt(asked)
        gpu_fallback = False
        if _gpu_retry(outcome, asked):
            logger.warning(
                "[%s] MIDI %s failed on the GPU (%s), running it again on the CPU: %s",
                job.id,
                stem,
                _failure_code(outcome),
                " | ".join(outcome.tail[-3:]) or "no diagnostic output",
            )
            temp.unlink(missing_ok=True)
            _update(job, stem, pct=0)
            outcome = attempt("cpu")
            gpu_fallback = True
        if outcome.stopped == "cancelled":
            _stopped(job, run, had_file)
            return
        event = outcome.event or {}
        if _failed(outcome):
            code = _failure_code(outcome)
            if event.get("phase") == "import":
                _engine_broken.add(run.engine)
            logger.warning(
                "[%s] MIDI %s failed (%s, %s, exit %s): %s",
                job.id,
                stem,
                code,
                run.engine,
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
        record = _record(run, params, plan, event, took, gpu_fallback)
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
            phase=None,
            notes=notes if isinstance(notes, int) else None,
            tempo_source=plan.get("source"),
            at=_now_iso(),
            error_code=None,
            has_file=True,
        )
        logger.info(
            "[%s] MIDI %s: %s notes in %.1fs (%s s of audio, %s on %s, detail %s, tempo %s %s)",
            job.id,
            stem,
            notes,
            took,
            event.get("seconds_audio"),
            run.engine,
            record["device"],
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
        phase=None,
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
