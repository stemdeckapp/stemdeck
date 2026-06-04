from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def _csp_directive(name: str) -> str:
    """Return the named directive from the served Content-Security-Policy header."""
    with TestClient(app) as c:
        resp = c.get("/")
    csp = resp.headers["content-security-policy"]
    return next(d.strip() for d in csp.split(";") if d.strip().startswith(name))


def test_connect_src_permits_data_and_blob():
    # Regression for #186: multitrack.js fetches a data: URI while initializing
    # each track's audio. Without data:/blob: in connect-src the browser blocks
    # it, Multitrack.create throws, and no audio/waveform/playback loads.
    connect = _csp_directive("connect-src")
    assert "data:" in connect
    assert "blob:" in connect


def test_script_src_stays_locked():
    # Lock #171's intent: loosening connect-src must not weaken the XSS defense.
    script = _csp_directive("script-src")
    assert "'self'" in script
    assert "unsafe-inline" not in script
    assert "unsafe-eval" not in script
