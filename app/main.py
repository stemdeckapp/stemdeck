from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.core.config import (
    DEMUCS_DEVICE,
    DEMUCS_MODEL,
    FFMPEG_BIN,
    JOBS_DIR,
    STATIC_DIR,
    configure_portable_environment,
    ensure_runtime_dirs,
)
from app.core.registry import restore as restore_registry
from app.pipeline.collect import sweep_old_jobs

# Show our INFO-level logs through uvicorn's root handler. Without this,
# Python's default root level (WARNING) silently drops every
# logger.info(...) call across the app, including the analyze
# diagnostics ("chroma:", "key candidates:").
logging.getLogger("stemdeck").setLevel(logging.INFO)
logging.getLogger("stemdeck").info("demucs config: model=%s device=%s", DEMUCS_MODEL, DEMUCS_DEVICE)

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


def _process_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() != ERROR_INVALID_PARAMETER


def app_version() -> str:
    # Version is git-tag-derived via hatch-vcs (#169). Prefer installed package
    # metadata (set at install/build from the tag); fall back to the generated
    # app/_version.py for non-installed runs, then a dev placeholder.
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
    library entries that ask to "re-upload to restore". So skip the sweep under
    the desktop shell (STEMDECK_DESKTOP=1); the user manages disk via Trash."""
    return os.environ.get("STEMDECK_DESKTOP") == "1"


async def _sweep_loop() -> None:
    if _sweep_disabled():
        _log.info("desktop mode: job TTL sweep disabled (library is user-managed)")
        return
    while True:
        try:
            await asyncio.to_thread(sweep_old_jobs, JOBS_DIR)
        except Exception:
            _log.warning("sweep failed", exc_info=True)
        await asyncio.sleep(3600)


async def _desktop_parent_watchdog(parent_pid: int) -> None:
    while True:
        if not _process_exists(parent_pid):
            _log.info("desktop parent process exited; stopping backend")
            # Send SIGTERM to ourselves so uvicorn runs its shutdown sequence
            # instead of bypassing cleanup with os._exit().
            os.kill(os.getpid(), signal.SIGTERM)
            return
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _background_tasks = set()
    t = asyncio.create_task(_sweep_loop())
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
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


app = FastAPI(
    title="StemDeck",
    description="Paste a YouTube URL or upload an audio file, get audio stems split into a DAW-style player.",
    version=app_version(),
    lifespan=lifespan,
)


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
        "demucs_device": DEMUCS_DEVICE,
    }


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


app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

ensure_runtime_dirs()
restore_registry(JOBS_DIR)
