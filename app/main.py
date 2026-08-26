from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import os
import re
import signal
import socket
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.core.config import (
    DEMUCS_MODEL,
    FFMPEG_BIN,
    JOBS_DIR,
    LOGS_DIR,
    STATIC_DIR,
    available_torch_devices,
    configure_portable_environment,
    ensure_runtime_dirs,
)
from app.core.logging_setup import configure_logging
from app.core.process import process_exists as _process_exists
from app.core.redact import redact
from app.core.registry import all_jobs as registry_all_jobs
from app.core.registry import registry_path, take_pending_resume
from app.core.registry import reset_all as reset_registry
from app.core.registry import restore as restore_registry
from app.core.settings import (
    DURATION_MAX_SEC,
    DURATION_MIN_SEC,
    get_allow_network,
    get_cookies_file,
    get_demucs_device,
    get_demucs_device_choice,
    get_export_sample_rate,
    get_jobs_dir,
    get_max_duration_sec,
    get_playlist_max_items,
    get_port,
    get_separation_quality,
    get_video_max_height,
    set_allow_network,
    set_cookies_file,
    set_demucs_device,
    set_export_sample_rate,
    set_jobs_dir,
    set_max_duration_sec,
    set_playlist_max_items,
    set_port,
    set_separation_quality,
    set_video_max_height,
)
from app.core.stems_location import (
    StemsLocationError,
    abandon_relocation,
    begin_relocation,
    directory_size,
    move_library,
    validate_target,
)
from app.pipeline.collect import sweep_failed_jobs, sweep_old_jobs

# Set the stemdeck logger level (Python's default root level of WARNING would
# silently drop every logger.info(...) call) and attach the rotating file log
# at LOGS_DIR/stemdeck.log. The analyze diagnostics ("chroma:", "key
# candidates:") are DEBUG-level -- set STEMDECK_DEBUG=1 (or
# STEMDECK_LOG_LEVEL=DEBUG) to see them.
configure_logging()
logging.getLogger("stemdeck").info(
    "demucs config: model=%s device=%s", DEMUCS_MODEL, get_demucs_device()
)

configure_portable_environment()

# Pre-import librosa so the first job submission doesn't pay the 1-2 s
# cost of numpy/scipy/numba lazy initialization. Adds ~1 s to server
# boot in exchange for snappier first-job UX. Best-effort: if librosa
# isn't installed, analyze() degrades gracefully on its own.
try:
    import librosa  # noqa: F401  -- intentional warm-up import
except ImportError:
    pass

_log = logging.getLogger("stemdeck")


def app_version() -> str:
    # Version is git-tag-derived via hatch-vcs (#169).
    #
    # A packaged desktop install carries its version in the app layer
    # (static/version.json, written by the make-portable scripts), and that is
    # checked FIRST. The in-app updater (#421) replaces backend/ but
    # deliberately never replaces python/, where the installed dist metadata
    # lives -- so after a self-update that metadata is a version behind, and
    # trusting it would make the app keep offering an update it already applied.
    # The file is gitignored, so Docker images and source checkouts do not have
    # it and correctly fall through to the metadata below.
    try:
        # utf-8-sig: some writers emit a BOM, which json.loads rejects.
        raw = (STATIC_DIR / "version.json").read_text(encoding="utf-8-sig")
        packaged = json.loads(raw).get("version")
        if isinstance(packaged, str) and packaged.strip():
            return packaged.strip()
    except (OSError, ValueError, AttributeError):
        # Absent or unreadable (OSError), not valid JSON or not decodable
        # (ValueError), or valid JSON that is not an object so has no .get
        # (AttributeError). All of those simply mean "no app-layer marker
        # here", so fall through to the metadata below. Narrow rather than a
        # bare except so a genuine bug in this function still surfaces instead
        # of silently degrading the reported version (bandit B110).
        pass
    # Installed package metadata (set at install/build from the tag); then the
    # generated app/_version.py for non-installed runs, then a dev placeholder.
    try:
        return package_version("stemdeck")
    except PackageNotFoundError:
        pass
    try:
        from app._version import __version__

        return str(__version__)
    except Exception:
        return "0.0.0-dev"


def _sweep_disabled() -> bool:
    """The desktop app is a personal, user-curated library (folders + Trash),
    with its track list persisted permanently in ~/Documents/StemDeck. The 24h
    job TTL sweep -- a sensible disk-hygiene default for the shared server/Docker
    deployment -- would wrongly purge stems the user kept, leaving orphaned
    library entries that ask to "re-upload to restore".

    So skip the sweep under the desktop shell (STEMDECK_DESKTOP=1), or when a
    self-hosted deployment opts into a persistent library
    (STEMDECK_PERSIST_LIBRARY=1 -- set by default in run.sh). The user manages
    disk via Trash. Shared/Docker deployments that set neither keep the sweep."""
    return (
        os.environ.get("STEMDECK_DESKTOP") == "1"
        or os.environ.get("STEMDECK_PERSIST_LIBRARY") == "1"
    )


async def _sweep_loop() -> None:
    # The job TTL sweep is disabled for persistent libraries, but the
    # failed-job quarantine (jobs/failed/) expires unconditionally -- failure
    # evidence is diagnostics, not library content, on every deployment.
    persistent = _sweep_disabled()
    if persistent:
        _log.info("job TTL sweep disabled (persistent library; user-managed)")
    while True:
        try:
            if not persistent:
                await asyncio.to_thread(sweep_old_jobs, JOBS_DIR)
            await asyncio.to_thread(sweep_failed_jobs, JOBS_DIR)
        except Exception:
            _log.warning("sweep failed", exc_info=True)
        await asyncio.sleep(3600)


async def _desktop_parent_watchdog(parent_pid: int) -> None:
    while True:
        if not _process_exists(parent_pid):
            _log.info("desktop parent process exited; stopping backend")
            # Raise SIGTERM in-process so uvicorn's handler runs its shutdown
            # sequence. os.kill(pid, SIGTERM) would be wrong here: on Windows
            # it is TerminateProcess -- a hard kill that bypasses cleanup
            # (#282). raise_signal triggers the Python-level handler on both
            # platforms.
            signal.raise_signal(signal.SIGTERM)
            return
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _background_tasks = set()
    t = asyncio.create_task(_sweep_loop())
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    # The single queue consumer. Started here rather than at import time because
    # restore_registry() runs at module scope, where there is no running loop.
    from app.pipeline import jobqueue

    qt = jobqueue.start_worker()
    _background_tasks.add(qt)
    qt.add_done_callback(_background_tasks.discard)
    # Jobs that were queued or in flight when the process last died. restore()
    # already put them back to "queued"; this puts them back in the queue.
    #
    # Deliberately paused: opening the app must not start separating on its own.
    # A restored queue can be dozens of tracks and hours of GPU, and the user
    # may well have opened StemDeck to do something else entirely. They press
    # Start (or simply import something new, which lifts the pause).
    resumed = take_pending_resume()
    if resumed:
        jobqueue.pause()
        for job_id in resumed:
            jobqueue.enqueue(job_id, autostart=False)
        _log.info(
            "restored %d interrupted job(s) from the previous session; queue is paused",
            len(resumed),
        )
    if os.environ.get("STEMDECK_DESKTOP") == "1":
        parent_pid = os.environ.get("STEMDECK_PARENT_PID")
        if parent_pid:
            try:
                parent_pid_int = int(parent_pid)
            except ValueError:
                _log.warning("invalid STEMDECK_PARENT_PID=%r", parent_pid)
            else:
                if parent_pid_int > 0 and parent_pid_int != os.getpid():
                    wt = asyncio.create_task(_desktop_parent_watchdog(parent_pid_int))
                    _background_tasks.add(wt)
                    wt.add_done_callback(_background_tasks.discard)
    yield
    # Stop taking new work. Deliberately not cancelling the in-flight job: it is
    # blocked in asyncio.to_thread, which cannot be cancelled, so it dies with
    # the process exactly as it did before the queue existed.
    jobqueue.request_stop()
    # Tear down the persistent demucs worker (#309) so a clean shutdown never
    # leaves it as an orphaned process -- it has no parent-death watchdog of
    # its own, unlike the desktop backend itself.
    from app.pipeline.separate import _kill_worker

    _kill_worker()


# Phones hitting the self-hosted server URL get the mobile UI; everything
# else (desktop browsers, and the Tauri webviews, which all report desktop
# user-agents) gets the DAW. Tablets are intentionally treated as desktop —
# the DAW layout is usable there. "Mobi" is the cross-browser marker for a
# phone form factor (Chrome/Firefox/Safari all include it); the rest cover
# vendors that don't.
_MOBILE_UA_RE = re.compile(
    r"Mobi|Android|iPhone|iPod|IEMobile|BlackBerry|Opera Mini", re.IGNORECASE
)


def _is_mobile_ua(user_agent: str) -> bool:
    return bool(user_agent) and _MOBILE_UA_RE.search(user_agent) is not None


app = FastAPI(
    title="StemDeck",
    description="Paste a YouTube URL or upload an audio file, get audio stems split into a DAW-style player.",
    version=app_version(),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def index(request: Request) -> FileResponse:
    """Serve the mobile shell to phones, the DAW to everyone else. `?ui=mobile`
    / `?ui=desktop` forces either one (handy for testing from a desktop). This
    route is registered before the StaticFiles mount at "/", so it wins for the
    bare path while the mount still serves every other asset."""
    ui = request.query_params.get("ui")
    if ui == "mobile":
        mobile = True
    elif ui == "desktop":
        mobile = False
    else:
        mobile = _is_mobile_ua(request.headers.get("user-agent", ""))
    page = "mobile/index.html" if mobile else "index.html"
    return FileResponse(STATIC_DIR / page)


@app.get("/health", include_in_schema=False)
def health_root() -> dict[str, object]:
    return health()


@app.get("/api/health", tags=["health"])
def health() -> dict[str, object]:
    return {
        "name": "StemDeck",
        "status": "ok",
        "version": app_version(),
        "ffmpeg_configured": FFMPEG_BIN.is_file(),
        "demucs_model": DEMUCS_MODEL,
        "demucs_device": get_demucs_device(),
        # Who is answering. The desktop shell spawns this backend and then polls
        # this endpoint to know it came up -- but a 200 alone only proves
        # *something* is listening on that port, not that it is the backend the
        # shell just started. When a second StemDeck was launched, the new
        # window adopted the already-running instance's backend, and with it
        # that instance's data directory and library (#424).
        #
        # The token is the answer to that, and the PID is now only diagnostic
        # (it is what the shell names in its port-conflict message). #424 used
        # the PID for identity, which assumed the process that binds the port is
        # the one the shell spawned. On the Windows portable build it is not:
        # python/Scripts/python.exe is a venv launcher and Windows has no exec,
        # so it starts python/base/python.exe as a child and *that* is the
        # process here. The comparison could never succeed and every Windows
        # portable launch timed out (#457). The environment survives any number
        # of re-execs, so identity travels there instead.
        "pid": os.getpid(),
        "instance": os.environ.get("STEMDECK_INSTANCE_TOKEN", ""),
    }


def _is_lan_ipv4(ip: str) -> bool:
    """A reachable IPv4 LAN address to show another device: not IPv6 (link-local
    needs a zone index and won't work in a browser), not loopback, not the
    169.254.x auto-config range."""
    if ":" in ip:  # IPv6
        return False
    if _is_loopback(ip) or ip.startswith("169.254."):
        return False
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def _settings_payload() -> dict[str, object]:
    return {
        "allow_network": get_allow_network(),
        "max_duration_sec": get_max_duration_sec(),
        # The clamp bounds, so the UI does not need its own copy of them. It
        # had one, it was stale (20 min against a 60 min ceiling), and the
        # symptom was a user typing 60 and silently getting 20 back.
        "max_duration_min_sec": DURATION_MIN_SEC,
        "max_duration_max_sec": DURATION_MAX_SEC,
        "playlist_max_items": get_playlist_max_items(),
        "video_max_height": get_video_max_height(),
        "export_sample_rate": get_export_sample_rate(),
        "separation_quality": get_separation_quality(),
        # Absent unless the user set one. Only the path is exposed, never the
        # file's contents -- those are the user's YouTube session.
        "cookies_file": get_cookies_file(),
        "port": get_port(),
        # The user's choice ("auto" | "cuda" | "mps" | "cpu") drives the UI
        # select; the resolved value shows what jobs will actually run on;
        # available lets the UI gray out devices this machine can't use.
        "demucs_device": get_demucs_device_choice(),
        "demucs_device_resolved": get_demucs_device(),
        "demucs_devices_available": available_torch_devices(),
    }


@app.get("/api/settings", tags=["settings"])
def get_settings(request: Request) -> dict[str, object]:
    # LAN addresses other devices can use — loopback excluded (only works on the
    # host). The port is whatever this request came in on.
    port = request.url.port or 8000
    addresses = sorted(f"http://{ip}:{port}" for ip in _local_ips() if _is_lan_ipv4(ip))
    # Show the port the server is actually running on rather than the stored
    # preference (which only takes effect on the next restart) -- so the field
    # reflects reality. Editing it still saves the preference via POST.
    return {**_settings_payload(), "port": port, "lan_addresses": addresses}


@app.post("/api/settings", tags=["settings"])
async def update_settings(request: Request) -> dict[str, object]:
    """Update runtime settings. Reachable from the host machine always; from a
    LAN device only while network access is currently on (the gate below), so a
    phone can't change settings once the owner turned access off."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if "allow_network" in body:
        set_allow_network(bool(body["allow_network"]))
    for key, setter in (
        ("max_duration_sec", set_max_duration_sec),
        ("playlist_max_items", set_playlist_max_items),
        ("video_max_height", set_video_max_height),
        ("port", set_port),
    ):
        if key in body:
            try:
                setter(int(body[key]))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{key} must be an integer") from None
    if "export_sample_rate" in body:
        try:
            set_export_sample_rate(body["export_sample_rate"])
        except ValueError as e:
            # Allowlist violation / non-integer -- the message names the valid rates.
            raise HTTPException(status_code=422, detail=str(e)) from None
    if "cookies_file" in body:
        try:
            set_cookies_file(body["cookies_file"])
        except ValueError as e:
            # "cookies file not found" / "not readable" -- both safe to show.
            raise HTTPException(status_code=422, detail=str(e)) from None
        except (TypeError, OSError):
            raise HTTPException(status_code=422, detail="invalid cookies file") from None
    if "demucs_device" in body:
        try:
            set_demucs_device(str(body["demucs_device"]))
        except ValueError as e:
            # set_demucs_device's messages are safe, user-actionable strings
            # (invalid choice / device not available on this machine).
            raise HTTPException(status_code=422, detail=str(e)) from None
    if "separation_quality" in body:
        try:
            set_separation_quality(str(body["separation_quality"]))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
    return _settings_payload()


_ACTIVE_JOB_STATUSES = ("queued", "downloading", "analyzing", "separating", "processing")


def _stems_location_editable() -> bool:
    """Relocating the stem library is a desktop-app feature only (#354).

    A server, Docker or Unraid deployment gets its storage from a mounted volume
    or an explicit STEMDECK_JOBS_DIR, decided by whoever runs it. Moving files
    from inside the app there would fight the deployment: the mount would still
    be the mount on the next start, and the library would be somewhere the
    container no longer looks.
    """
    return os.environ.get("STEMDECK_DESKTOP") == "1"


def _require_desktop_shell() -> None:
    if not _stems_location_editable():
        raise HTTPException(
            status_code=403,
            detail=(
                "The stems folder is set by this deployment. "
                "Change the mounted volume or STEMDECK_JOBS_DIR instead."
            ),
        )


@app.get("/api/settings/stems-location", tags=["settings"])
def get_stems_location() -> dict[str, object]:
    """Where stems are stored now, and how much is there.

    The size is what makes the setting actionable -- "2.5 GB in your Documents
    folder" is the thing the user is trying to fix (#354).

    Answers everywhere, including deployments that cannot change it: `editable`
    is how the UI knows whether to offer the control at all. Probing a 403
    instead would log a failed request every time Settings is opened.

    After a move the stored choice and this process disagree until the restart:
    the files are at the new path, JOBS_DIR still points at the old one. Report
    where the stems actually are, or reopening Settings would show the folder
    the user just moved away from."""
    configured = get_jobs_dir()
    pending = bool(configured and configured != str(JOBS_DIR))
    path = Path(configured) if configured else JOBS_DIR
    return {
        "path": str(path),
        "bytes": directory_size(path) if path.exists() else 0,
        "editable": _stems_location_editable(),
        "is_default": configured is None,
        "restart_required": pending,
        "busy": bool([j for j in registry_all_jobs().values() if j.status in _ACTIVE_JOB_STATUSES]),
    }


@app.post("/api/settings/stems-location", tags=["settings"])
async def set_stems_location(request: Request) -> dict[str, object]:
    """Move the stem library somewhere else and remember the choice.

    The move is the point: the registry lives inside this folder, so changing
    the setting alone would strand the library and show an empty app. The
    preference is written only after the move succeeds, so a failure leaves the
    app still reading the folder the files are actually in.

    Takes effect fully on the next start -- JOBS_DIR is bound at import time
    across the app -- which is why the response says so.
    """
    _require_desktop_shell()
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = body.get("path")
    if not isinstance(raw, str):
        raise HTTPException(status_code=422, detail="path is required")

    # Close the door first. Validation touches the disk (it creates the target
    # and lists it), and an import accepted during that would have its directory
    # moved out from under it moments later. Every early exit below reopens it.
    begin_relocation()
    try:
        active = [j for j in registry_all_jobs().values() if j.status in _ACTIVE_JOB_STATUSES]
        if active:
            # Moving files out from under a running separation would corrupt it.
            raise HTTPException(
                status_code=409,
                detail="Finish or cancel the imports in the queue before moving the stems folder",
            )
        target = validate_target(Path(raw), JOBS_DIR)
    except StemsLocationError as e:
        abandon_relocation()
        raise HTTPException(status_code=422, detail=str(e)) from None
    except Exception:
        abandon_relocation()
        raise

    try:
        result = await asyncio.to_thread(move_library, JOBS_DIR, target)
    except StemsLocationError as e:
        abandon_relocation()
        raise HTTPException(status_code=500, detail=str(e)) from None
    except Exception:
        abandon_relocation()
        _log.exception("moving the stems library failed")
        raise HTTPException(status_code=500, detail="Could not move the stems folder") from None

    _, persisted = set_jobs_dir(str(target))
    _log.info(
        "stems library moved to %s (%d entries, %d bytes, persisted=%s)",
        target,
        result.moved_entries,
        result.bytes_moved,
        persisted,
    )
    if not persisted:
        # The move itself genuinely succeeded (result is real) -- but
        # settings.json didn't take the new path, so a restart would read
        # JOBS_DIR back from the old default while the library sits at
        # `target`. Loud, not a silent false-success (#403): the response
        # still reports what physically happened, plus why it isn't safe yet.
        _log.warning(
            "stems library moved to %s but the jobs_dir setting did not persist "
            "-- a restart right now would lose track of it",
            target,
        )
    return {
        "path": str(target),
        "moved_entries": result.moved_entries,
        "bytes_moved": result.bytes_moved,
        "restart_required": True,
        "persisted": persisted,
    }


@app.post("/api/reset", tags=["settings"])
def reset_app_data() -> dict[str, object]:
    """Factory reset (Settings -> General -> "Reset app data"): delete every
    job directory and the registry, so no old work session can resurface
    across package reinstalls -- on desktop, the runtime state that actually
    persists (~/Documents/StemDeck, not the extracted package's own bundled
    data/ folder) survives a fresh install otherwise. The browser-side
    library index is a separate store the frontend clears itself (the Tauri
    reset_user_data command on desktop, localStorage directly in server/
    mobile mode) after this call succeeds.

    Available in server mode too, same trust boundary as every other
    settings-mutating endpoint: the network_gate middleware already allows
    the host machine always and a LAN device only while network access is
    on. On a shared deployment this deletes every user's library, not just
    the caller's -- same blast radius as changing the compute device or any
    other setting from a device with network access, not a new class of
    risk this endpoint introduces on its own."""
    active = [j for j in registry_all_jobs().values() if j.status in _ACTIVE_JOB_STATUSES]
    if active:
        raise HTTPException(status_code=409, detail="cannot reset while a job is in progress")
    # Clear the queue alongside the registry, or the worker would keep popping
    # ids that no longer resolve and the queue view would report ghosts.
    from app.pipeline import jobqueue

    jobqueue.clear()
    reset_registry(JOBS_DIR)
    return {"ok": True}


@app.get("/api/registry", tags=["settings"])
def get_registry_raw() -> PlainTextResponse:
    """Read-only view of the persisted job registry (Settings -> Registry)."""
    path = registry_path(JOBS_DIR)
    if not path.is_file():
        return PlainTextResponse(
            '{\n  "version": 1,\n  "jobs": []\n}\n', media_type="application/json"
        )
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/json")


# Every log this deployment can produce, with what writes each one. The desktop
# entries only exist under the Tauri shell -- the Docker/server deployments have
# just the Python log -- so the endpoint reports which are actually present
# rather than promising files that will never appear.
_LOG_FILES: tuple[tuple[str, str], ...] = (
    (
        "stemdeck.log",
        "Application log: the pipeline, API and job activity. Rotates at 5 MB, 3 kept.",
    ),
    ("stemdeck.log.1", "Older application log."),
    ("stemdeck.log.2", "Older application log."),
    ("stemdeck.log.3", "Oldest kept application log."),
    ("backend.log", "Desktop only: raw output of the bundled Python backend process."),
    ("backend.log.1", "Desktop only: older backend output."),
    ("backend.log.2", "Desktop only: oldest kept backend output."),
    ("setup.log", "Desktop only: first-run setup and GPU runtime installation."),
)


@app.get("/api/logs", tags=["settings"])
def get_logs_info() -> dict[str, object]:
    """Where the log files live and which currently exist (Settings -> Logs).

    Read-only and metadata only: it reports paths and sizes, never contents.
    Serving log text over HTTP would expose whatever a traceback happened to
    capture, and the files are on the machine the user is already sitting at.
    """
    logs_dir = LOGS_DIR.resolve()
    files: list[dict[str, object]] = []
    for name, description in _LOG_FILES:
        path = logs_dir / name
        try:
            stat = path.stat()
            exists, size, modified = True, stat.st_size, stat.st_mtime
        except OSError:
            exists, size, modified = False, 0, None
        files.append(
            {
                "name": name,
                "description": description,
                "path": str(path),
                "exists": exists,
                "size": size,
                "modified": modified,
            }
        )
    return {
        "dir": str(logs_dir),
        "dir_exists": logs_dir.is_dir(),
        "files": files,
    }


# Views the Logs tab offers, each mapping to the newest file of a family plus
# the backup immediately before it -- a rotation inside the window would
# otherwise make a busy log look empty.
_LOG_VIEWS: dict[str, tuple[str, ...]] = {
    "application": ("stemdeck.log", "stemdeck.log.1"),
    # The backend's raw stdout/stderr. Worth a view of its own because it holds
    # what the application log cannot: anything the process printed before
    # logging was configured, and anything that killed it before a handler ran.
    # A backend that dies at startup leaves stemdeck.log empty and the answer
    # here.
    "backend": ("backend.log", "backend.log.1", "backend.log.2"),
    "setup": ("setup.log",),
}
# Bounds on what a single view returns. The application log rotates at 5 MB, so
# an unbounded "last hour" on a busy server could still be enormous.
_LOG_TAIL_BYTES = 1_500_000
_LOG_TAIL_LINES = 4000

# "2026-07-18 12:43:42 I stemdeck ..." -- the file handler's datefmt.
_PY_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")
# "[1786205373] [stemdeck] ..." -- setup.log, epoch seconds (see the Rust
# writer; the crate has no date library).
_SETUP_LOG_TS = re.compile(r"^\[(\d{9,})\] ")


def _line_time(line: str) -> float | None:
    """Epoch seconds for a log line, or None when it carries no timestamp."""
    m = _PY_LOG_TS.match(line)
    if m:
        try:
            return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    m = _SETUP_LOG_TS.match(line)
    if m:
        return float(m.group(1))
    return None


def _tail_lines(path: Path) -> list[str]:
    """Last _LOG_TAIL_BYTES of a file as lines, without reading all of it."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > _LOG_TAIL_BYTES:
                f.seek(size - _LOG_TAIL_BYTES)
                f.readline()  # discard the partial first line
            raw = f.read()
    except OSError:
        return []
    return raw.decode("utf-8", errors="replace").splitlines()


@app.get("/api/logs/{view}", tags=["settings"])
def get_log_tail(view: str, minutes: int = 60) -> PlainTextResponse:
    """Recent lines from a log, for the Settings -> Logs viewer.

    Lines older than `minutes` are dropped. Lines with no timestamp of their own
    (tracebacks, subprocess output) belong to the line above them, so they
    inherit its time rather than being shredded out of a multi-line error.
    """
    files = _LOG_VIEWS.get(view)
    if files is None:
        raise HTTPException(status_code=404, detail="unknown log")
    window = max(1, min(minutes, 24 * 60))
    cutoff = time.time() - window * 60

    logs_dir = LOGS_DIR.resolve()
    lines: list[str] = []
    # Oldest backup first so the result reads forwards in time.
    for name in reversed(files):
        path = logs_dir / name
        if path.is_file():
            lines.extend(_tail_lines(path))

    kept: list[str] = []
    including = False
    for line in lines:
        ts = _line_time(line)
        if ts is None:
            # Continuation of whatever came before it.
            if including:
                kept.append(line)
            continue
        including = ts >= cutoff
        if including:
            kept.append(line)

    if not kept:
        body = (
            f"No entries in the last {window} minutes.\n"
            if lines
            else f"No log file yet at {logs_dir / files[0]}.\n"
        )
        return PlainTextResponse(body, media_type="text/plain; charset=utf-8")

    truncated = ""
    if len(kept) > _LOG_TAIL_LINES:
        truncated = f"[... {len(kept) - _LOG_TAIL_LINES} earlier lines not shown ...]\n"
        kept = kept[-_LOG_TAIL_LINES:]
    # Redacted unconditionally, not just for the report flow's callers: a log
    # line is a log line regardless of who asks for it, and the notification
    # centre's "include recent logs" button hands this straight to a public
    # GitHub issue or Discord message without a second filtering step. Strips
    # the reporter's home directory, any YouTube/SoundCloud source URL (every
    # job's download start is logged at info level -- not just the failing
    # one), and any IPv4 address (the mobile UI talks to this backend over
    # the LAN, so uvicorn's access log can carry another device's address).
    body = redact(truncated + "\n".join(kept) + "\n")
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@app.get("/api/logs.zip", tags=["settings"])
def download_logs_zip() -> StreamingResponse:
    """Bundle the log files into a zip for support and bug reports.

    Only the known log names are read, never whatever else happens to be in the
    directory: the set is fixed above, so a stray file dropped in LOGS_DIR can
    never be swept into a download. Built in memory -- these are capped at 5 MB
    x 3 backups plus two small desktop logs, so the whole set is bounded.
    """
    logs_dir = LOGS_DIR.resolve()
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, _description in _LOG_FILES:
            path = logs_dir / name
            try:
                if not path.is_file():
                    continue
                zf.writestr(name, path.read_bytes())
                written += 1
            except OSError:
                _log.warning("could not add %s to the log bundle", name, exc_info=True)
        if written == 0:
            # An empty zip is a confusing download; say so inside it instead.
            zf.writestr(
                "README.txt",
                f"No log files were found in {logs_dir}.\n"
                "Logging starts on the first message after the app launches.\n",
            )

    buf.seek(0)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="stemdeck-logs-{stamp}.zip"'},
    )


# Content-Security-Policy. Defense-in-depth so an injected string in the webview
# can't run script (and, in the desktop app, reach the exposed Tauri IPC) — #171.
# script-src has no 'unsafe-inline'/'eval': all JS is same-origin modules and the
# inline scripts/onclick were moved out. 'unsafe-inline' is allowed for *styles*
# only (the UI sets many style attributes). Allowances:
#   connect-src  -> same-origin API/SSE, the GitHub update check, Tauri IPC
#   img-src https: -> remote YouTube/SoundCloud thumbnails
#   style/font   -> the Google Fonts <link>
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob: data:; "
    # data:/blob: are required by multitrack.js (it fetches a data: URI during
    # track init); without them Multitrack.create throws and no audio loads
    # (#186). They are inline/same-origin schemes, not network endpoints, so
    # they add no exfiltration channel — script-src below stays locked.
    "connect-src 'self' https://api.github.com ipc: http://ipc.localhost data: blob:; "
    "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


# Force browsers to revalidate static assets on every request. Without
# this the JS/CSS modules can stick in disk cache across server
# restarts -- updated HTML loads against stale modules and the form
# silently breaks. `must-revalidate` keeps 304s working (cheap) while
# guaranteeing the latest mtime is honored.
@app.middleware("http")
async def security_and_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.startswith("::ffff:"):  # IPv4-mapped IPv6
        host = host[7:]
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


@functools.lru_cache(maxsize=1)
def _local_ips() -> frozenset[str]:
    """The machine's own interface IPs. Used so the host always reaches the app
    even via its LAN address (e.g. 192.168.x.x), not just 127.0.0.1 — turning
    network access off must never cut the host off from its own server."""
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        # Best-effort: name resolution can fail on odd hostnames/configs; we
        # still try the outbound-socket probe below and fall back to loopback.
        _log.debug("hostname IP enumeration failed", exc_info=True)
    try:  # primary outbound IP, robust when the hostname doesn't resolve them all
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        # Best-effort: no default route / offline — just return what we have.
        _log.debug("outbound IP probe failed", exc_info=True)
    return frozenset(ips)


def _is_host_request(host: str | None) -> bool:
    """True when the request originates from the machine StemDeck runs on —
    whether via loopback or one of its own interface addresses."""
    if _is_loopback(host):
        return True
    if not host:
        return False
    h = host[7:] if host.startswith("::ffff:") else host
    return h in _local_ips()


# Network availability gate (Settings → "Make StemDeck available on your
# network"). Added after the headers middleware so it is the OUTERMOST layer and
# short-circuits before anything else. It NEVER stops the server — it only
# refuses requests from OTHER devices when availability is off. The host machine
# (loopback or its own LAN IP) is always served, so the app keeps working
# locally regardless of this setting.
@app.middleware("http")
async def network_gate(request: Request, call_next):
    if not get_allow_network():
        client_host = request.client.host if request.client else None
        if not _is_host_request(client_host):
            return PlainTextResponse(
                "StemDeck is not available on the network. Enable it in Settings on the host machine.",
                status_code=403,
            )
    return await call_next(request)


app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

ensure_runtime_dirs()
restore_registry(JOBS_DIR)
