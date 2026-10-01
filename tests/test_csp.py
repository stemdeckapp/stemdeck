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


def test_connect_src_allows_the_artist_hosts_and_nothing_wider():
    # The artist box (#699) reads Wikidata and Wikipedia straight from the
    # page. Those hosts by name, never a bare https: that would let an
    # injected script send anything anywhere. LRCLIB is not among them: the
    # Lyrics tab asks the server to look lyrics up (#719).
    sources = _csp_directive("connect-src").split()[1:]
    for host in (
        "https://www.wikidata.org",
        "https://query.wikidata.org",
        "https://*.wikipedia.org",
    ):
        assert host in sources
    assert "https://lrclib.net" not in sources
    assert "https:" not in sources
    assert "*" not in sources


def test_script_src_stays_locked():
    # Lock #171's intent: loosening connect-src must not weaken the XSS defense.
    script = _csp_directive("script-src").split()[1:]
    assert "'self'" in script
    assert "'unsafe-inline'" not in script
    assert "'unsafe-eval'" not in script
    # 'wasm-unsafe-eval' compiles WebAssembly and nothing else: JS eval,
    # new Function and string timers stay blocked. The Signalsmith tempo stage
    # needs it (#729). Anything beyond it here is a loosening to justify.
    assert set(script) == {"'self'", "'wasm-unsafe-eval'"}
