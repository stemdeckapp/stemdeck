"""A second uvicorn listener, serving the same app over TLS on the LAN.

The desktop app has two audiences with incompatible requirements, and one
listener cannot satisfy both.

Its own webview needs plain http on loopback. `http://127.0.0.1` is already a
secure context by browser rule, so nothing is lost there, and it is the only
scheme the webview can use: a self-signed certificate would raise an interstitial
that a Tauri window has no UI to click through, and the app would simply fail to
load.

A phone on the LAN needs https. Transpose is an AudioWorklet, which browsers
withhold from `http://192.168.x.x`, so over plain http the key control is dead
with nothing on screen to explain it. Only TLS makes that origin secure.

So both run, against the same FastAPI app object in the same process -- one
registry, one queue, one demucs worker. Only the primary listener runs the
lifespan; see `lifespan="off"` below.

None of this applies to server mode, where there is one listener and whoever
launched uvicorn decides its scheme. This module is started only when
STEMDECK_HTTPS_PORT is set, which is the desktop shell's doing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

# The port the companion is actually accepting connections on, or None. Read by
# /api/settings to build the address it hands out, so it must reflect a socket
# that exists rather than the port we were asked for -- a failed bind has to
# show up as "no https address", not as a QR code that cannot connect.
_active_port: int | None = None
_server: object | None = None
_task: asyncio.Task | None = None


def active_port() -> int | None:
    """The live TLS port, or None if no companion listener is running."""
    return _active_port


async def _serve(server: object) -> None:
    """Run a uvicorn server, containing the way it reports a failed bind.

    uvicorn answers an unusable port with `sys.exit(1)`, which is fine for the
    process it normally owns and wrong here: SystemExit is a BaseException, so
    asyncio re-raises it out of the task and into the event loop, taking the
    primary listener down with it. Ours is a companion; it is allowed to fail.
    """
    try:
        await server.serve()  # type: ignore[attr-defined]
    except SystemExit:
        _log.debug("https listener exited during startup", exc_info=True)


async def start(app: object, *, port: int, certfile: Path, keyfile: Path) -> bool:
    """Serve `app` over TLS on 0.0.0.0:port. Returns whether it came up.

    Failure is deliberately not fatal. This runs inside the app's lifespan, and
    an unreadable certificate or an occupied port must cost the user LAN
    transpose, never the ability to open StemDeck at all.
    """
    global _active_port, _server, _task

    if _active_port is not None:
        return True
    for label, path in (("certificate", certfile), ("private key", keyfile)):
        if not path.is_file():
            _log.warning("https listener not started: %s missing at %s", label, path)
            return False

    import uvicorn

    config = uvicorn.Config(
        app,
        # The LAN is the entire point of this listener: it exists so that a
        # phone can reach StemDeck over a secure origin. The loopback half of
        # the pair is the primary server, bound separately. Whether another
        # device is actually served is decided per request by the network gate
        # in app/main.py, which defaults to off.
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=port,
        ssl_certfile=str(certfile),
        ssl_keyfile=str(keyfile),
        # The primary listener owns startup and shutdown. Running the lifespan
        # twice would start a second queue worker against the same registry,
        # and the first server to stop would reap the shared demucs worker out
        # from under the other.
        lifespan="off",
        # Inherit the logging the app already configured rather than reapplying
        # uvicorn's dictConfig, which would tear down our handlers.
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    # Server.serve() installs SIGINT/SIGTERM handlers when it runs on the main
    # thread, which is where the lifespan runs. Left alone, the companion would
    # replace the primary server's handlers and Ctrl+C would stop only this one.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    task = asyncio.create_task(_serve(server), name="stemdeck-https")
    # Wait for the bind to resolve either way. uvicorn signals a failed bind by
    # setting should_exit and returning, so a finished task here means the port
    # was refused, not that the server is running.
    while not server.started and not task.done():
        await asyncio.sleep(0.02)
    if task.done():
        exc = task.exception() if not task.cancelled() else None
        _log.warning("https listener could not bind port %d: %s", port, exc or "port in use")
        return False

    _server, _task, _active_port = server, task, port
    _log.info("https listener on 0.0.0.0:%d (%s)", port, certfile.name)
    return True


async def stop() -> None:
    """Shut the companion down with the app. Safe to call when never started."""
    global _active_port, _server, _task

    server, task = _server, _task
    _server = _task = None
    _active_port = None
    if server is None or task is None:
        return
    server.should_exit = True  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(task, timeout=3)
    except (TimeoutError, asyncio.TimeoutError):
        # Past the graceful window. The process is going away regardless, and
        # blocking shutdown on a stuck connection is worse than a stray socket.
        task.cancel()
    except Exception:
        _log.exception("https listener did not stop cleanly")
