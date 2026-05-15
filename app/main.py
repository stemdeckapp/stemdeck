from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
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
    version_file = STATIC_DIR / "version.json"
    try:
        data = json.loads(version_file.read_text(encoding="utf-8"))
        value = str(data.get("version", "")).strip().removeprefix("v")
        if value:
            return value
    except (OSError, json.JSONDecodeError):
        pass
    try:
        return package_version("stemdeck")
    except PackageNotFoundError:
        return "0.0.0-dev"


async def _sweep_loop() -> None:
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
            os._exit(0)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    asyncio.create_task(_sweep_loop())
    if os.environ.get("STEMDECK_DESKTOP") == "1":
        parent_pid = os.environ.get("STEMDECK_PARENT_PID")
        if parent_pid:
            try:
                parent_pid_int = int(parent_pid)
            except ValueError:
                _log.warning("invalid STEMDECK_PARENT_PID=%r", parent_pid)
            else:
                if parent_pid_int > 0 and parent_pid_int != os.getpid():
                    asyncio.create_task(_desktop_parent_watchdog(parent_pid_int))
    yield


app = FastAPI(title="StemDeck", lifespan=lifespan)


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


# Force browsers to revalidate static assets on every request. Without
# this the JS/CSS modules can stick in disk cache across server
# restarts -- updated HTML loads against stale modules and the form
# silently breaks. `must-revalidate` keeps 304s working (cheap) while
# guaranteeing the latest mtime is honored.
@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.include_router(router, prefix="/api")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

# Ensure runtime directories exist at startup (module-level side effect
# moved from the old monolithic main.py; this is the canonical entrypoint).
ensure_runtime_dirs()
restore_registry(JOBS_DIR)
