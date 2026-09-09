"""The companion listener's lifecycle, without a certificate or a socket.

test_tls_listener.py drives the real thing end to end, but every one of those
tests skips unless the desktop Rust crate has been built -- which CI does not
do -- so the parts that decide whether a failure is survivable were running
untested on every platform.

Those parts are all reachable with a stand-in uvicorn: the module imports it
inside start(). What is asserted here is the containment, not the serving --
that a failed bind is reported rather than raised, that SystemExit out of
uvicorn cannot reach the event loop and take the primary listener with it, that
the companion never installs signal handlers over the primary's, and that
stop() is safe from every state it can be called in.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.core import tls_listener


@pytest.fixture(autouse=True)
def _reset_module_state():
    """The listener keeps its server in module globals, so a test that left one
    behind would make the next one return early from start()."""
    tls_listener._active_port = None
    tls_listener._server = None
    tls_listener._task = None
    yield
    task = tls_listener._task
    if task is not None and not task.done():
        task.cancel()
    tls_listener._active_port = None
    tls_listener._server = None
    tls_listener._task = None


@pytest.fixture
def certs(tmp_path):
    """Files that exist. Their contents never reach a TLS stack here -- start()
    only checks that both are present before handing the paths to uvicorn."""
    cert = tmp_path / "lan.crt"
    key = tmp_path / "lan.key"
    cert.write_text("not-a-real-certificate")
    key.write_text("not-a-real-key")
    return cert, key


class _FakeServer:
    """Enough of uvicorn.Server for start()/stop() to drive it."""

    def __init__(self, config):
        self.config = config
        self.started = False
        self.should_exit = False
        self.install_signal_handlers = self._real_install_signal_handlers
        self.installed_handlers = False

    def _real_install_signal_handlers(self):
        self.installed_handlers = True

    async def serve(self):
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0.01)


class _NeverStartsServer(_FakeServer):
    """uvicorn's answer to an unusable port: it never reports started, and the
    coroutine simply returns."""

    async def serve(self):
        return None


class _ExitingServer(_FakeServer):
    """uvicorn calls sys.exit(1) on a failed bind."""

    async def serve(self):
        raise SystemExit(1)


class _HangingServer(_FakeServer):
    """Comes up, then ignores should_exit -- a server stuck on an open
    connection past its graceful-shutdown window."""

    async def serve(self):
        self.started = True
        while True:
            await asyncio.sleep(0.05)


class _FailsOnExitServer(_FakeServer):
    async def serve(self):
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0.01)
        raise RuntimeError("event loop is closed")


def _install_fake_uvicorn(monkeypatch, server_cls=_FakeServer):
    """start() does `import uvicorn` in its body, so this replaces it wholesale."""
    made: list = []

    class _Config:
        def __init__(self, app, **kwargs):
            self.app = app
            self.__dict__.update(kwargs)
            self.kwargs = kwargs

    def _server(config):
        srv = server_cls(config)
        made.append(srv)
        return srv

    module = types.ModuleType("uvicorn")
    module.Config = _Config
    module.Server = _server
    monkeypatch.setitem(sys.modules, "uvicorn", module)
    return made


# --------------------------------------------------------------------------
# _serve -- the containment around uvicorn's exit-on-failed-bind
# --------------------------------------------------------------------------


async def test_a_uvicorn_systemexit_never_reaches_the_event_loop():
    """SystemExit is a BaseException: asyncio re-raises it out of the task and
    into the loop, which would take the primary listener down with the
    companion. Containing it here is the only thing preventing that."""

    class _Exits:
        async def serve(self):
            raise SystemExit(1)

    await tls_listener._serve(_Exits())  # returns rather than propagating


async def test_a_genuine_error_is_not_swallowed_with_it():
    """Only the documented failed-bind signal is contained. Hiding everything
    would turn a broken listener into a silent one."""

    class _Breaks:
        async def serve(self):
            raise RuntimeError("ssl context is unusable")

    with pytest.raises(RuntimeError, match="ssl context"):
        await tls_listener._serve(_Breaks())


# --------------------------------------------------------------------------
# start()
# --------------------------------------------------------------------------


async def test_a_missing_private_key_is_refused_like_a_missing_certificate(tmp_path, caplog):
    cert = tmp_path / "lan.crt"
    cert.write_text("x")

    started = await tls_listener.start(
        object(), port=8443, certfile=cert, keyfile=tmp_path / "absent.key"
    )

    assert started is False
    assert tls_listener.active_port() is None
    assert "private key" in caplog.text


async def test_starting_twice_keeps_the_listener_that_is_already_up(monkeypatch, certs):
    """The lifespan can run start() again on a reload; binding a second socket
    to the same port would fail and clear a working listener."""
    cert, key = certs
    made = _install_fake_uvicorn(monkeypatch)

    assert await tls_listener.start(object(), port=8443, certfile=cert, keyfile=key) is True
    assert await tls_listener.start(object(), port=8443, certfile=cert, keyfile=key) is True

    assert len(made) == 1, "a second server was constructed"
    assert tls_listener.active_port() == 8443


async def test_a_listener_that_comes_up_is_recorded_as_the_live_port(monkeypatch, certs):
    cert, key = certs
    _install_fake_uvicorn(monkeypatch)

    assert await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key) is True

    assert tls_listener.active_port() == 9443
    assert tls_listener._server is not None
    assert tls_listener._task is not None


async def test_a_bind_that_never_comes_up_is_reported_not_raised(monkeypatch, certs, caplog):
    """uvicorn signals a refused port by returning without ever setting started.
    A finished task at that point means the port was refused."""
    cert, key = certs
    _install_fake_uvicorn(monkeypatch, _NeverStartsServer)

    assert await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key) is False

    # Nothing is advertised, so /api/settings hands out no https address.
    assert tls_listener.active_port() is None
    assert "could not bind port 9443" in caplog.text


async def test_a_uvicorn_that_exits_during_startup_is_also_just_a_false(monkeypatch, certs):
    cert, key = certs
    _install_fake_uvicorn(monkeypatch, _ExitingServer)

    assert await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key) is False
    assert tls_listener.active_port() is None


async def test_the_companion_never_installs_signal_handlers(monkeypatch, certs):
    """Server.serve() installs SIGINT/SIGTERM handlers when it runs on the main
    thread, which is where the lifespan runs. Left alone the companion would
    replace the primary server's, and Ctrl+C would stop only this one."""
    cert, key = certs
    made = _install_fake_uvicorn(monkeypatch)

    await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key)

    made[0].install_signal_handlers()
    assert made[0].installed_handlers is False


async def test_the_companion_is_configured_for_the_lan_and_not_for_the_lifespan(monkeypatch, certs):
    """lifespan="off" is the safeguard: running it twice would start a second
    queue worker against the same registry, and the first server to stop would
    reap the shared demucs worker out from under the other."""
    cert, key = certs
    made = _install_fake_uvicorn(monkeypatch)
    app = object()

    await tls_listener.start(app, port=9443, certfile=cert, keyfile=key)

    config = made[0].config
    assert config.app is app, "the companion must share the one app object"
    assert config.lifespan == "off"
    assert config.host == "0.0.0.0"  # noqa: S104  -- the LAN is the point
    assert config.port == 9443
    assert config.ssl_certfile == str(cert)
    assert config.ssl_keyfile == str(key)
    # Reapplying uvicorn's dictConfig would tear down the handlers the app
    # already installed.
    assert config.log_config is None
    assert config.access_log is False


# --------------------------------------------------------------------------
# stop()
# --------------------------------------------------------------------------


async def test_stopping_asks_the_server_to_exit_and_clears_the_port(monkeypatch, certs):
    cert, key = certs
    made = _install_fake_uvicorn(monkeypatch)
    await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key)

    await tls_listener.stop()

    assert made[0].should_exit is True
    assert tls_listener.active_port() is None
    assert tls_listener._server is None
    assert tls_listener._task is None


async def test_stopping_twice_is_harmless(monkeypatch, certs):
    cert, key = certs
    _install_fake_uvicorn(monkeypatch)
    await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key)

    await tls_listener.stop()
    await tls_listener.stop()  # must not raise on the now-empty state


async def test_a_listener_that_will_not_stop_is_cancelled_rather_than_waited_on(monkeypatch, certs):
    """Past the graceful window the process is going away regardless, and
    blocking shutdown on a stuck connection is worse than a stray socket.

    Waits out the real 3 s window rather than stubbing asyncio.wait_for --
    patching that module-wide reaches pytest-asyncio's own machinery.
    """
    cert, key = certs
    made = _install_fake_uvicorn(monkeypatch, _HangingServer)
    await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key)
    task = tls_listener._task

    await tls_listener.stop()

    assert made[0].should_exit is True, "the server was never asked to exit"
    assert task.cancelled() or task.cancelling() > 0
    assert tls_listener.active_port() is None


async def test_a_failure_while_stopping_is_logged_not_propagated(monkeypatch, certs, caplog):
    """stop() runs in the lifespan's shutdown. Raising here would turn a messy
    shutdown into a traceback on every quit."""
    cert, key = certs
    _install_fake_uvicorn(monkeypatch, _FailsOnExitServer)
    await tls_listener.start(object(), port=9443, certfile=cert, keyfile=key)

    await tls_listener.stop()  # must not raise

    assert "did not stop cleanly" in caplog.text
    assert tls_listener.active_port() is None
