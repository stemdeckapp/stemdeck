"""Server mode refuses to serve a browser a plaintext non-local origin.

Transpose is an AudioWorklet, which browsers grant only to a secure context.
On http://<lan-ip> the API is simply absent, so the feature disappears with no
error anywhere -- and there is no fallback worth shipping: driving the same DSP
from a ScriptProcessorNode measured ~5% of the audio missing, because that node
type is lossy on its own at every buffer size. So server mode asks for TLS
instead of serving an app that is quietly half-broken.

The whole difficulty is in *not* over-applying that. Most self-hosted installs
sit behind a reverse proxy that terminates TLS and forwards plain HTTP: the
browser already has its secure context and only the last hop is plaintext.
Judging by our own socket would reject exactly those deployments, so the tests
below pin the distinction rather than the mechanism.
"""

from __future__ import annotations

import httpx
import pytest

import app.main as main

# Captured at import, before conftest's autouse fixture relaxes it for the rest
# of the suite. Restoring the real function rather than restating its rule is
# what keeps these tests about the shipped behaviour.
_REQUIRED = main._secure_origin_required

LAN = ("192.168.1.50", 51234)
LOOPBACK = ("127.0.0.1", 51234)


def _client(peer: tuple[str, int], base: str = "http://testserver") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main.app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url=base)


@pytest.fixture(autouse=True)
def _server_mode(monkeypatch):
    """Run as a server deployment, not as the desktop app's backend."""
    monkeypatch.setattr(main, "_secure_origin_required", _REQUIRED)
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    # The network gate is a separate switch; keep it out of the way so these
    # tests fail for their own reason and not that one.
    monkeypatch.setattr("app.main.get_allow_network", lambda: True)


async def test_a_lan_client_on_plain_http_is_refused() -> None:
    async with _client(LAN) as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 403
    assert "https://" in resp.text


async def test_the_refusal_explains_itself_rather_than_erroring() -> None:
    """Someone hitting this did nothing wrong, and a bare 403 reads as a bug."""
    async with _client(LAN) as c:
        resp = await c.get("/api/health")
    body = resp.text.lower()
    assert "secure context" in body
    # It has to name a way out, or it is just a wall.
    assert "reverse proxy" in body
    assert "tailscale" in body
    assert "stemdeck_ssl_cert" in body


async def test_the_host_machine_is_always_served() -> None:
    """Turning this on must never lock the host out of its own server."""
    async with _client(LOOPBACK) as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 200


async def test_a_lan_client_over_https_is_served() -> None:
    async with _client(LAN, base="https://testserver") as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 200


async def test_a_proxy_that_terminated_tls_is_served() -> None:
    """The common self-hosted shape: SWAG/NPM/Traefik in front, http upstream.

    The browser has a secure context; only this hop is plaintext. Rejecting it
    would break the installs that already did the right thing.
    """
    async with _client(LAN) as c:
        resp = await c.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 200


async def test_a_chain_of_proxies_is_read_from_the_client_end() -> None:
    """Each hop appends, so the browser-facing scheme is the first entry."""
    async with _client(LAN) as c:
        resp = await c.get("/api/health", headers={"X-Forwarded-Proto": "https, http"})
    assert resp.status_code == 200


async def test_the_rfc7239_header_works_too() -> None:
    """Caddy and others prefer `Forwarded` to the X- header."""
    async with _client(LAN) as c:
        resp = await c.get("/api/health", headers={"Forwarded": "for=203.0.113.9;proto=https"})
    assert resp.status_code == 200


async def test_a_proxy_forwarding_plain_http_is_still_refused() -> None:
    """A proxy is not a free pass: if the browser used http, this still applies."""
    async with _client(LAN) as c:
        resp = await c.get("/api/health", headers={"X-Forwarded-Proto": "http"})
    assert resp.status_code == 403


async def test_the_desktop_app_is_not_subject_to_this(monkeypatch) -> None:
    """The desktop has its own network toggle and its own explanation in the UI.

    Its LAN sharing is a different feature from server mode, and turning it into
    a hard failure would take away playback from a phone to fix transpose on it.
    """
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    async with _client(LAN) as c:
        resp = await c.get("/api/health")
    assert resp.status_code == 200


async def test_static_pages_are_gated_too_not_just_the_api() -> None:
    """The point is the browser tab, so the page itself has to be refused.

    Serving index.html and failing only its fetches is the confusing outcome
    this exists to prevent.
    """
    async with _client(LAN) as c:
        resp = await c.get("/")
    assert resp.status_code == 403
