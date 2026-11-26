"""Settings > Models: the Download button of a built-in model.

POST /api/models/{id}/download (app/api/config.py) starts a download, which
runs in a process of its own (app/pipeline/model_get_worker.py) so the
server can stop it at any moment, a read waiting on a quiet network
included. One download at a time, across the whole server.

Each model is fetched the way its own family's code fetches it, from the
same address into the same file, so the first job that needs it finds it
and downloads nothing:

    htdemucs_6s, htdemucs_ft  each pinned torch.hub checkpoint, through
                              model_download.fetch_pinned_hub_file: the
                              download bounded_downloads gives torch.hub (the
                              model mirror first for htdemucs_6s)
    kim_mel_roformer          mel_roformer.ensure_checkpoint
    piano_hr                  model_download.fetch_verified_file with the
                              pins a MIDI run hands model_fetch.py
    ymt3                      its checkpoint the same way, then
                              ymt3_convert.convert, then the checkpoint
                              removed, as a MIDI run's prepare does
    uvr_karaoke_2, beat_this, model_download.fetch_verified_file against the
    whisper_turbo,            pins in config.py: the address the family's
    whisper_small             own library fetches (audio-separator,
                              beat_this's torch.hub call, openai-whisper)

Never a file that looks complete and is not: every file is written under a
temporary name in a folder of the run's own (".stemdeck-get-<token>", beside
its destination, so the last move never crosses a disk), flushed, synced and
checked against its pinned size and SHA-256, and only then renamed into
place. A download stopped or cut off at any point leaves nothing under the
final name; the server removes the run's folders when it stops one, and a
later run removes the ones a crash left once they are old enough
(_STAGING_MAX_AGE_SEC).

Never a file a running separation or transcription is reading:

    - a pipeline model (separation, the Karaoke split, Whisper, beat_this)
      downloads only while nothing runs or waits in the pipeline, and holds
      the pipeline lock for its whole run, so a song added meanwhile waits
      for it (the speed test does the same);
    - a MIDI engine's weights (piano_hr, ymt3) download only while no MIDI
      run is in progress, and a MIDI run asking for that engine meanwhile is
      refused (app/api/midi.py, "model_downloading").

A file that is already its pinned file is never touched.

What the page reads (snapshot()):

    {"state": "idle" | "running" | "done" | "failed" | "cancelled",
     "model": <id> | None, "phase": "download" | "prepare" | None,
     "bytes": <int>, "total": <int> | None, "error_code": <code> | None}

error_code is one of ERROR_CODES, each said by the page in its own words:
the server never sends a path or an exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core import config

logger = logging.getLogger("stemdeck.model_get")

# How a failed download is said (static/js/i18n.js settings.models.downloadError.*).
ERROR_CODES = ("network", "disk", "checksum", "io", "prepare", "failed")

# The folder a run writes in, beside each destination: this prefix and the
# run's token (32 hex).
STAGING_PREFIX = ".stemdeck-get-"
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
# A run's folder this old is no one's: a run writes to it at least every read
# timeout while bytes come, and is stopped after TIMEOUT_MODEL_GET_STALL.
_STAGING_MAX_AGE_SEC = 3600

# The model ids, as GET /api/models names them: lowercase, digits, "_".
MODEL_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")

Family = Literal["pipeline", "midi"]
Via = Literal["hub", "kim", "verified"]


@dataclass(frozen=True)
class PlanFile:
    """One file of a model: where it comes from, what it must be, where it
    goes, and which family's download fetches it."""

    url: str
    sha256: str
    size: int
    dest: Path
    via: Via


@dataclass(frozen=True)
class Plan:
    model_id: str
    family: Family
    files: tuple[PlanFile, ...]
    # YourMT3+: the weights file made from files[0], which is then removed.
    convert_to: Path | None = None

    @property
    def total(self) -> int:
        """Bytes a full download fetches."""
        return sum(f.size for f in self.files)

    def folders(self) -> list[Path]:
        """Every folder a file of this model lands in."""
        out: list[Path] = []
        for path in [f.dest for f in self.files] + [self.convert_to]:
            if path is not None and path.parent not in out:
                out.append(path.parent)
        return out


def plan_for(model_id: str) -> Plan | None:
    """What a download of ``model_id`` fetches, or None when it has no
    Download button: built in (Basic Pitch), a model a deployment swapped in
    (the code has no pin for it), or a folder that cannot be worked out
    (torch does not load). Blocking: torch.hub's folder imports torch."""
    if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
        return None
    try:
        return _plan(model_id)
    except Exception:  # noqa: BLE001 -- no plan is "no button"; the job path still works
        logger.info("models: no download plan for %s", model_id, exc_info=True)
        return None


def _plan(model_id: str) -> Plan | None:
    from app.core import stem_models

    model = stem_models.MODELS.get(model_id)
    if model is not None:
        files = tuple(
            PlanFile(
                f.url,
                f.sha256,
                f.size,
                stem_models.model_path(f),
                "hub" if f.root == "torch_hub" else "kim",
            )
            for f in model.files
        )
        return Plan(model_id, "pipeline", files)
    if model_id == "uvr_karaoke_2":
        if config.VOCAL_SPLIT_MODEL != "UVR_MDXNET_KARA_2.onnx":
            return None
        from app.core.model_cache import vocal_split_artifacts

        dest = vocal_split_artifacts(config.MODELS_DIR, config.VOCAL_SPLIT_MODEL)[0]
        return Plan(
            model_id,
            "pipeline",
            (
                PlanFile(
                    config.KARAOKE_2_URL,
                    config.KARAOKE_2_SHA256,
                    config.KARAOKE_2_SIZE,
                    dest,
                    "verified",
                ),
            ),
        )
    if model_id == "beat_this":
        if config.BEAT_MODEL_CHECKPOINT != "final0":
            return None
        from app.core.model_cache import beat_this_artifacts

        dest = beat_this_artifacts("final0")[0]
        return Plan(
            model_id,
            "pipeline",
            (
                PlanFile(
                    config.BEAT_THIS_FINAL0_URL,
                    config.BEAT_THIS_FINAL0_SHA256,
                    config.BEAT_THIS_FINAL0_SIZE,
                    dest,
                    "verified",
                ),
            ),
        )
    if model_id.startswith("whisper_"):
        name = model_id[len("whisper_") :]
        pinned = config.WHISPER_FILES.get(name)
        if pinned is None or name not in (config.TRANSCRIBE_MODEL_GPU, config.TRANSCRIBE_MODEL_CPU):
            return None
        from app.core.model_inventory import whisper_file
        from app.pipeline.transcribe import whisper_models_dir

        url, sha256, size = pinned
        dest = whisper_models_dir() / whisper_file(name)
        return Plan(model_id, "pipeline", (PlanFile(url, sha256, size, dest, "verified"),))
    if model_id == "piano_hr":
        from app.pipeline import piano_hr

        return Plan(
            model_id,
            "midi",
            (
                PlanFile(
                    config.PIANO_HR_CHECKPOINT_URL,
                    config.PIANO_HR_CHECKPOINT_SHA256,
                    config.PIANO_HR_CHECKPOINT_SIZE,
                    piano_hr.checkpoint_path(),
                    "verified",
                ),
            ),
        )
    if model_id == "ymt3":
        from app.pipeline import ymt3

        return Plan(
            model_id,
            "midi",
            (
                PlanFile(
                    config.YMT3_CHECKPOINT_URL,
                    config.YMT3_CHECKPOINT_SHA256,
                    config.YMT3_CHECKPOINT_SIZE,
                    ymt3.checkpoint_path(),
                    "verified",
                ),
            ),
            convert_to=ymt3.weights_path(),
        )
    return None


def staging_dir(folder: Path, token: str) -> Path:
    """The run's own folder beside ``folder``'s files."""
    if not _TOKEN_RE.match(token):
        raise ValueError("a run token is 32 hex characters")
    return folder / f"{STAGING_PREFIX}{token}"


def remove_staging(plan: Plan, token: str) -> None:
    """Remove the run's folders: whatever a stopped run left. Never raises."""
    for folder in plan.folders():
        try:
            shutil.rmtree(staging_dir(folder, token), ignore_errors=True)
        except (OSError, ValueError):
            continue


def sweep_stale_staging(plan: Plan, max_age: float = _STAGING_MAX_AGE_SEC) -> list[str]:
    """Remove the run folders a crash left beside ``plan``'s files: exactly
    STAGING_PREFIX and 32 hex, a folder and not a link, untouched for
    ``max_age`` seconds. Returns their names. Never raises."""
    removed: list[str] = []
    now = time.time()
    for folder in plan.folders():
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not name.startswith(STAGING_PREFIX) or not _TOKEN_RE.match(
                name[len(STAGING_PREFIX) :]
            ):
                continue
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < max_age:
                    continue
                shutil.rmtree(entry.path, ignore_errors=True)
                removed.append(name)
            except OSError:
                continue
    return removed


# ── The one run ──

_lock = threading.Lock()
_IDLE: dict[str, Any] = {
    "state": "idle",
    "model": None,
    "phase": None,
    "bytes": 0,
    "total": None,
    "error_code": None,
}
_state: dict[str, Any] = dict(_IDLE)
_cancel = threading.Event()
_proc: list[subprocess.Popen] = []


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def running_model() -> str | None:
    """The model a download is running for, or None."""
    with _lock:
        return _state["model"] if _state["state"] == "running" else None


def is_running() -> bool:
    return running_model() is not None


def claim(model_id: str, total: int) -> threading.Event | None:
    """Take the one download slot for ``model_id``, or None when a download
    holds it. No await between the caller's checks and this."""
    global _cancel
    with _lock:
        if _state["state"] == "running":
            return None
        _cancel = threading.Event()
        _state.update(
            state="running",
            model=model_id,
            phase="download",
            bytes=0,
            total=total,
            error_code=None,
        )
        return _cancel


def _update(**fields: Any) -> None:
    with _lock:
        _state.update(fields)


def cancel_running() -> bool:
    """Ask the running download to stop. Its thread stops the process within
    a tenth of a second and removes what it left. Also called at shutdown."""
    with _lock:
        if _state["state"] != "running":
            return False
        _cancel.set()
        return True


def reset_state() -> None:
    """Back to idle, for tests."""
    global _cancel
    with _lock:
        _state.clear()
        _state.update(_IDLE)
        _proc.clear()
        _cancel = threading.Event()


def _spawn_cmd(model_id: str, token: str) -> list[str]:
    """The worker invocation. A seam for tests."""
    return [
        sys.executable,
        "-m",
        "app.pipeline.model_get_worker",
        "--model",
        model_id,
        "--token",
        token,
    ]


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


def run_blocking(plan: Plan, token: str, cancel: threading.Event) -> None:
    """The download claim() started, to its end. Blocking (asyncio.to_thread).
    Never raises; every ending is in the state. Removes the run's folders
    whatever the ending: on success the worker has moved every file out."""
    from app.core.speed_test import stop_process

    stall = config.TIMEOUT_MODEL_GET_STALL
    env = os.environ.copy()
    # The worker hard-exits when this process disappears (app/core/process.py).
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    outcome: str | None = None
    code: str | None = None
    tail: deque[str] = deque(maxlen=40)
    try:
        proc = subprocess.Popen(  # noqa: S603 -- fixed args: our interpreter and module
            _spawn_cmd(plan.model_id, token),
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
    except OSError:
        logger.exception("models: the download of %s could not start", plan.model_id)
        _update(state="failed", phase=None, error_code="failed")
        remove_staging(plan, token)
        return
    with _lock:
        _proc[:] = [proc]
    lock = threading.Lock()
    last_output = [time.monotonic()]
    events: list[dict[str, Any]] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            event = parse_event(line)
            with lock:
                last_output[0] = time.monotonic()
                if event is not None:
                    events.append(event)
                elif line.strip():
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
    seen = 0
    try:
        while True:
            with lock:
                fresh = events[seen:]
                seen = len(events)
                silent_for = time.monotonic() - last_output[0]
            for event in fresh:
                _apply(event, plan)
            if proc.poll() is not None:
                break
            if cancel.is_set():
                outcome = "cancelled"
                break
            if silent_for > stall:
                outcome = "stalled"
                break
            time.sleep(0.1)
    finally:
        stop_process(proc)
        for reader in readers:
            reader.join(timeout=2)
        with _lock:
            _proc.clear()
    with lock:
        fresh = events[seen:]
        final = next((e for e in reversed(events) if e["event"] != "progress"), None)
    for event in fresh:
        _apply(event, plan)
    if outcome is None and cancel.is_set():
        outcome = "cancelled"
    if outcome is None and final is not None and final["event"] == "done" and proc.returncode == 0:
        _update(state="done", phase=None, bytes=plan.total, error_code=None)
        logger.info("models: %s downloaded", plan.model_id)
    elif outcome == "cancelled":
        _update(state="cancelled", phase=None, error_code=None)
        logger.info("models: the download of %s was cancelled", plan.model_id)
    else:
        if outcome == "stalled":
            code = "network"
        else:
            raw = (final or {}).get("code")
            code = raw if raw in ERROR_CODES else "failed"
        _update(state="failed", phase=None, error_code=code)
        logger.warning(
            "models: the download of %s failed (%s): %s",
            plan.model_id,
            outcome or code,
            " | ".join(list(tail)[-3:]) or "no diagnostic output",
        )
    remove_staging(plan, token)


def _apply(event: dict[str, Any], plan: Plan) -> None:
    if event.get("event") != "progress":
        return
    fields: dict[str, Any] = {}
    got = event.get("bytes")
    if isinstance(got, int) and not isinstance(got, bool) and got >= 0:
        fields["bytes"] = min(got, plan.total)
    phase = event.get("phase")
    if phase in ("download", "prepare"):
        fields["phase"] = phase
    if fields:
        _update(**fields)


def new_token() -> str:
    """A run's token: names its folders, so a stop removes only its own."""
    return os.urandom(16).hex()
