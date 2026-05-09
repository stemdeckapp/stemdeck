from __future__ import annotations

import asyncio
import json
import logging
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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    asyncio.create_task(_sweep_loop())
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
