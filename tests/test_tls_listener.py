"""The desktop app's second listener: https on the LAN, http on loopback.

One listener cannot serve both audiences. The Tauri webview has to be talked to
over plain http on 127.0.0.1, because a self-signed certificate raises an
interstitial that window has no chrome to click through -- and it loses nothing,
since loopback is already a secure context by browser rule. A phone on the LAN
has to be talked to over https, because `http://192.168.x.x` is not a secure
context and browsers withhold the AudioWorklet that transpose is built on.

So both run against the same app object in the same process. The tests here pin
the two things that decide whether that is safe and whether it is any use: that
only one of them runs the lifespan, and that Settings hands out the address of
the listener that is actually up.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.core import tls_listener


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def certificate(tmp_path_factory) -> tuple[Path, Path]:
    """A throwaway certificate, made the way the desktop shell makes the real one.

    Emitted by the Rust crate rather than restated here in Python: that is the
    code that ships, and a hand-rolled substitute would let its output drift
    (wrong SANs, a lifetime Safari rejects) without a single test noticing.

    Uses an already-built binary and skips when there is none, rather than
    building one. `cargo build` on this crate is minutes of Tauri, which does
    not belong in a Python test run, and CI does not build the desktop app at
    all -- the Rust half is covered by `cargo test certs::`.
    """
    root = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "target"
    exe = "stemdeck.exe" if sys.platform == "win32" else "stemdeck"
    # Newest wins. A checkout can hold both profiles, and the stale one predates
    # this flag -- which it answers by opening the app window and never exiting.
    built = max(
        (p for p in (root / "release" / exe, root / "debug" / exe) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        default=None,
    )
    if built is None:
        pytest.skip("no desktop binary built; run `cargo build` in desktop/src-tauri")
    out = tmp_path_factory.mktemp("certs")
    try:
        probe = subprocess.run(  # noqa: S603
            [str(built), "--emit-lan-cert", str(out)],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(f"{built.name} predates --emit-lan-cert; rebuild the desktop crate")
    cert, key = out / "certs" / "lan.crt", out / "certs" / "lan.key"
    if not (cert.is_file() and key.is_file()):
        pytest.skip(f"could not emit a certificate: {probe.stderr[-400:]!r}")
    return cert, key


async def test_it_stays_off_unless_it_is_asked_for() -> None:
    """Server mode has one listener whose scheme is not ours to choose."""
    assert tls_listener.active_port() is None


async def test_a_missing_certificate_does_not_take_the_app_down(tmp_path) -> None:
    """Startup runs inside the lifespan. A bad certificate must cost LAN
    transpose, never the ability to open StemDeck at all."""
    from app.main import app

    started = await tls_listener.start(
        app,
        port=_free_port(),
        certfile=tmp_path / "absent.crt",
        keyfile=tmp_path / "absent.key",
    )
    assert started is False
    assert tls_listener.active_port() is None


async def test_stopping_one_that_never_started_is_harmless() -> None:
    await tls_listener.stop()
    assert tls_listener.active_port() is None


@pytest.mark.skipif(sys.platform not in ("win32", "linux", "darwin"), reason="needs sockets")
async def test_it_serves_the_same_app_over_tls(certificate) -> None:
    """The point of sharing the app object: one registry, one queue, one worker.

    A separate process per scheme would give a phone its own library and its own
    idea of what is separating.
    """
    from app.main import app

    cert, key = certificate
    port = _free_port()
    assert await tls_listener.start(app, port=port, certfile=cert, keyfile=key)
    assert tls_listener.active_port() == port
    try:
        # Our own certificate signed it, so verification is the thing under
        # test only in the sense that the phone will be asked to skip it too.
        ctx = ssl.create_default_context(cafile=str(cert))
        ctx.check_hostname = False
        async with httpx.AsyncClient(verify=ctx) as c:
            resp = await c.get(f"https://127.0.0.1:{port}/api/health", timeout=10)
        assert resp.status_code == 200
    finally:
        await tls_listener.stop()
    assert tls_listener.active_port() is None


@pytest.mark.skipif(sys.platform not in ("win32", "linux", "darwin"), reason="needs sockets")
async def test_the_companion_does_not_run_the_lifespan(certificate) -> None:
    """Running it twice would start a second queue worker against the same
    registry, and the first server to stop would reap the demucs worker out
    from under the other. `lifespan="off"` is the whole safeguard."""
    from app.main import app

    cert, key = certificate
    port = _free_port()
    assert await tls_listener.start(app, port=port, certfile=cert, keyfile=key)
    try:
        assert tls_listener._server.config.lifespan == "off"
    finally:
        await tls_listener.stop()


@pytest.mark.skipif(sys.platform not in ("win32", "linux", "darwin"), reason="needs sockets")
async def test_a_taken_port_is_reported_rather_than_raised(certificate) -> None:
    from app.main import app

    cert, key = certificate
    with socket.socket() as held:
        held.bind(("0.0.0.0", 0))  # noqa: S104
        held.listen(1)
        port = held.getsockname()[1]
        assert await tls_listener.start(app, port=port, certfile=cert, keyfile=key) is False
    assert tls_listener.active_port() is None


async def test_settings_advertises_the_listener_that_is_actually_up(monkeypatch) -> None:
    """A QR code is only worth anything if it points at a live socket.

    Reading the configured port instead of the live one would turn a failed
    bind into an address that looks right and cannot connect, which is the one
    outcome worse than saying nothing.
    """
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))
    monkeypatch.setattr(tls_listener, "_active_port", 8443)
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        body = (await c.get("/api/settings")).json()
    assert body["lan_addresses"] == ["https://192.168.1.50:8443"]


async def test_without_it_the_address_is_the_scheme_this_server_speaks(monkeypatch) -> None:
    """Server mode is unchanged: one listener, and http unless it terminates TLS."""
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))
    monkeypatch.setattr(tls_listener, "_active_port", None)
    monkeypatch.setattr(main, "SSL_CERTFILE", None)
    monkeypatch.setattr(main, "SSL_KEYFILE", None)
    transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 5000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        body = (await c.get("/api/settings")).json()
    assert body["lan_addresses"] == ["http://192.168.1.50:8000"]
