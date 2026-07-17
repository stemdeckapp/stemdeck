from __future__ import annotations

import asyncio
import ctypes
import functools
import logging
import os
import re
import signal
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.core.config import (
    DEMUCS_MODEL,
    FFMPEG_BIN,
    JOBS_DIR,
    STATIC_DIR,
    available_torch_devices,
    configure_portable_environment,
    ensure_runtime_dirs,
)
from app.core.logging_setup import configure_logging
from app.core.registry import registry_path
from app.core.registry import restore as restore_registry
from app.core.settings import (
    get_allow_network,
    get_demucs_device,
    get_demucs_device_choice,
    get_export_sample_rate,
    get_max_duration_sec,
    get_port,
    get_separation_quality,
    get_video_max_height,
    set_allow_network,
    set_demucs_device,
    set_export_sample_rate,
    set_max_duration_sec,
    set_port,
    set_separation_quality,
    set_video_max_height,
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
        "video_max_height": get_video_max_height(),
        "export_sample_rate": get_export_sample_rate(),
        "separation_quality": get_separation_quality(),
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


@app.get("/api/registry", tags=["settings"])
def get_registry_raw() -> PlainTextResponse:
    """Read-only view of the persisted job registry (Settings -> Registry)."""
    path = registry_path(JOBS_DIR)
    if not path.is_file():
        return PlainTextResponse(
            '{\n  "version": 1,\n  "jobs": []\n}\n', media_type="application/json"
        )
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/json")


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
