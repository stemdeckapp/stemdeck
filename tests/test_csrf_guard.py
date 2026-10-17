"""A web page the user visits must not be able to drive StemDeck.

Without the guard (app/core/csrf.py), `fetch(url, {mode: "no-cors", method:
"POST", body})` from any site reached every write endpoint: POST /api/reset
wiped the library, POST /api/settings turned network access on. DNS rebinding
gave the same page read access. These tests pin the rules and, as much, what
must keep working: the app's own page, the phone over the LAN, a reverse proxy,
the desktop shell, curl and the Rust side.
"""

from __future__ import annotations

import httpx
import pytest

import app.main as main
from app.core import csrf
from app.core import settings as _settings
from app.core.models import Job
from app.core.registry import _jobs

LOOPBACK = ("127.0.0.1", 51234)
LAN_PEER = ("192.168.1.50", 51234)
LAN_HOST = "192.168.1.10:8000"  # the server's own LAN address, as a phone types it
EVIL = "https://evil.example"


def _client(peer=LOOPBACK, base="http://127.0.0.1:8000") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main.app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url=base)


@pytest.fixture(autouse=True)
def _empty_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def network(monkeypatch):
    """Set network access for the request, the way the Settings toggle does."""

    def set_to(on: bool) -> None:
        monkeypatch.setattr("app.main.get_allow_network", lambda: on)

    set_to(False)
    return set_to


@pytest.fixture
def lan_ip(monkeypatch):
    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.10"}))


# ── 1. cross-site writes ──


async def test_cross_site_reset_is_refused_and_the_library_survives(network):
    """The reproduction: an idle server, a page elsewhere, a no-cors POST."""
    _jobs["a1a1a1a1a1a1"] = Job(id="a1a1a1a1a1a1", status="done", title="Kept")
    async with _client() as c:
        r = await c.post(
            "/api/reset",
            content=b"",
            headers={"Origin": EVIL, "Content-Type": "text/plain"},
        )
    assert r.status_code == 403
    assert r.json() == {"detail": "cross-site request refused"}
    assert "a1a1a1a1a1a1" in _jobs


async def test_cross_site_settings_cannot_open_the_network(network):
    async with _client() as c:
        r = await c.post(
            "/api/settings",
            content=b'{"allow_network": true}',
            headers={"Origin": EVIL, "Content-Type": "text/plain;charset=UTF-8"},
        )
    assert r.status_code == 403
    assert _settings._ensure().get("allow_network") is None, "nothing was written"


async def test_sec_fetch_site_cross_site_is_refused_without_an_origin(network):
    async with _client() as c:
        r = await c.post("/api/settings", json={}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
async def test_every_unsafe_method_is_guarded(network, method):
    async with _client() as c:
        r = await c.request(method.upper(), "/api/jobs/a1a1a1a1a1a1", headers={"Origin": EVIL})
    assert r.status_code == 403


async def test_an_opaque_origin_is_refused(network):
    """Origin: null comes from sandboxed frames and file:// pages."""
    async with _client() as c:
        r = await c.post("/api/settings", json={}, headers={"Origin": "null"})
    assert r.status_code == 403


async def test_another_local_app_on_another_port_is_refused(network):
    """localhost:3000 is same-site with StemDeck, not same-origin."""
    async with _client(base="http://localhost:8000") as c:
        r = await c.post(
            "/api/settings",
            json={},
            headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"},
        )
    assert r.status_code == 403


async def test_same_origin_post_is_allowed(network):
    async with _client() as c:
        r = await c.post(
            "/api/settings",
            json={"auto_sections": False},
            headers={"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"},
        )
    assert r.status_code == 200


async def test_same_origin_by_origin_alone_is_allowed(network):
    """Older Safari sends Origin but no Sec-Fetch headers."""
    async with _client(base="http://localhost:8000") as c:
        r = await c.post("/api/settings", json={}, headers={"Origin": "http://localhost:8000"})
    assert r.status_code == 200


async def test_sec_fetch_same_origin_wins_over_a_proxy_rewritten_host(network):
    """A proxy that rewrites Host and sends no X-Forwarded-Host: the browser's
    own verdict is trusted, so the install is not locked out."""
    network(True)
    async with _client(base="http://stemdeck-upstream:8000") as c:
        r = await c.post(
            "/api/settings",
            json={},
            headers={"Origin": "https://music.example.org", "Sec-Fetch-Site": "same-origin"},
        )
    assert r.status_code == 200


async def test_no_origin_and_no_fetch_headers_is_allowed(network):
    """curl, the Rust shell, Python clients: not a browser, nothing to forge."""
    async with _client() as c:
        r = await c.post("/api/settings", json={})
    assert r.status_code == 200


async def test_the_phone_over_the_lan_https_listener_is_same_origin(network, lan_ip):
    network(True)
    async with _client(peer=LAN_PEER, base="https://192.168.1.10:8444") as c:
        r = await c.post(
            "/api/settings",
            json={},
            headers={"Origin": "https://192.168.1.10:8444", "Sec-Fetch-Site": "same-origin"},
        )
    assert r.status_code == 200


async def test_a_lan_host_same_origin_without_fetch_headers(network, lan_ip):
    network(True)
    async with _client(peer=LAN_PEER, base=f"http://{LAN_HOST}") as c:
        r = await c.post(
            "/api/settings",
            json={},
            headers={"Origin": f"http://{LAN_HOST}", "X-Forwarded-Proto": "https"},
        )
    assert r.status_code == 200


async def test_a_reverse_proxy_forwarded_host_counts_as_same_origin(network):
    network(True)
    async with _client(base="http://stemdeck:8000") as c:
        r = await c.post(
            "/api/settings",
            json={},
            headers={
                "Origin": "https://music.example.org",
                "X-Forwarded-Host": "music.example.org",
            },
        )
    assert r.status_code == 200


async def test_the_tauri_setup_origin_is_accepted_on_the_desktop(network, monkeypatch):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    async with _client() as c:
        for origin in ("tauri://localhost", "http://tauri.localhost"):
            r = await c.post("/api/settings", json={}, headers={"Origin": origin})
            assert r.status_code == 200, origin


async def test_the_tauri_origin_means_nothing_outside_the_desktop(network, monkeypatch):
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    async with _client() as c:
        r = await c.post("/api/settings", json={}, headers={"Origin": "tauri://localhost"})
    assert r.status_code == 403


# ── safe methods ──


async def test_get_sse_head_and_options_are_not_write_guarded(network):
    hostile = {"Origin": EVIL, "Sec-Fetch-Site": "cross-site"}
    async with _client() as c:
        assert (await c.get("/api/health", headers=hostile)).status_code == 200
        assert (await c.head("/api/health", headers=hostile)).status_code != 403
        assert (await c.options("/api/settings", headers=hostile)).status_code != 403
    # A progress stream never ends, and ASGITransport buffers the whole body,
    # so the SSE route is checked at the guard itself: a GET passes whatever
    # its fetch headers say.
    sse = httpx.Headers({"host": "127.0.0.1:8000", **hostile})
    for path in ("/api/queue/events", "/api/jobs/a1a1a1a1a1a1/events"):
        assert (
            csrf.check_request(
                "GET", path, sse, allow_network=False, desktop=False, extra_hosts=(), local_ips=()
            )
            is None
        )


# ── 2. Host (DNS rebinding) ──


async def test_a_rebound_host_is_refused_while_the_network_is_off(network):
    """evil.example resolved to 127.0.0.1: same origin to the browser, so only
    the Host header gives it away. Refused for reads too."""
    async with _client(base="http://evil.example:8000") as c:
        r = await c.get("/api/registry")
    assert r.status_code == 400
    assert "STEMDECK_ALLOWED_HOSTS" in r.text


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "app.localhost", "127.0.0.2", "LOCALHOST"],
)
async def test_loopback_names_are_always_accepted(network, host):
    async with _client(base=f"http://{host}") as c:
        assert (await c.get("/api/health")).status_code == 200


async def test_the_machines_own_ip_and_name_are_accepted(network, lan_ip, monkeypatch):
    monkeypatch.setattr(csrf, "own_hostnames", lambda: frozenset({"studio-pc", "studio-pc.local"}))
    for host in ("192.168.1.10:8000", "studio-pc:8000", "studio-pc.local"):
        async with _client(base=f"http://{host}") as c:
            assert (await c.get("/api/health")).status_code == 200, host


async def test_any_host_is_accepted_with_the_network_on(network):
    """Docker and Unraid: the container cannot know the address the user typed.
    Locking those users out is worse than the rebinding risk on a server that
    every LAN device can already read."""
    network(True)
    async with _client(base="http://tower:8000") as c:
        assert (await c.get("/api/health")).status_code == 200


async def test_stemdeck_allowed_hosts_makes_the_host_check_strict(network, monkeypatch):
    network(True)
    monkeypatch.setattr(main, "ALLOWED_HOSTS", ("music.example.org",))
    async with _client(base="http://evil.example") as c:
        assert (await c.get("/api/health")).status_code == 400
    async with _client(base="https://music.example.org") as c:
        assert (await c.get("/api/health")).status_code == 200
    async with _client(base="http://127.0.0.1:8000") as c:
        assert (await c.get("/api/health")).status_code == 200, "loopback still works"


async def test_an_allowed_host_origin_matches_by_name(network, monkeypatch):
    """A proxy that forwards neither Host nor X-Forwarded-Host."""
    network(True)
    monkeypatch.setattr(main, "ALLOWED_HOSTS", ("music.example.org", "stemdeck"))
    async with _client(base="http://stemdeck:8000") as c:
        r = await c.post("/api/settings", json={}, headers={"Origin": "https://music.example.org"})
    assert r.status_code == 200


async def test_a_wildcard_turns_the_host_check_off_but_not_the_origin_check(network, monkeypatch):
    monkeypatch.setattr(main, "ALLOWED_HOSTS", ("*",))
    async with _client(base="http://anything.example") as c:
        assert (await c.get("/api/health")).status_code == 200
        r = await c.post("/api/settings", json={}, headers={"Origin": EVIL})
    assert r.status_code == 403


# ── 3. bodies are JSON ──


@pytest.mark.parametrize(
    "ctype",
    ["text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded"],
)
@pytest.mark.parametrize(
    "path",
    ["/api/settings", "/api/search", "/api/playlist/preview", "/api/settings/stems-location"],
)
async def test_a_non_json_body_is_415(network, ctype, path):
    async with _client() as c:
        r = await c.post(path, content=b'{"query": "x"}', headers={"Content-Type": ctype})
    assert r.status_code == 415
    assert r.json() == {"detail": "Content-Type must be application/json"}


async def test_a_text_plain_link_import_is_415(network):
    async with _client() as c:
        r = await c.post(
            "/api/jobs",
            content=b'{"url": "https://youtu.be/dQw4w9WgXcQ"}',
            headers={"Content-Type": "text/plain"},
        )
    assert r.status_code == 415
    assert not _jobs


async def test_json_with_a_charset_and_plus_json_are_accepted(network):
    async with _client() as c:
        for ctype in ("application/json; charset=utf-8", "application/merge-patch+json"):
            r = await c.post("/api/settings", content=b"{}", headers={"Content-Type": ctype})
            assert r.status_code == 200, ctype


async def test_an_empty_post_needs_no_content_type(network):
    async with _client() as c:
        r = await c.post("/api/queue/start", headers={"Content-Type": "text/plain"})
    assert r.status_code != 415


async def test_multipart_upload_still_reaches_the_route(network):
    """The file import is multipart; the guard must let it through. A .txt file
    is refused by the route itself, which proves the request got there."""
    async with _client() as c:
        r = await c.post(
            "/api/jobs",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
    assert r.status_code == 422
    assert "Unsupported file type" in r.json()["detail"]


async def test_multipart_is_only_accepted_on_the_import_route(network):
    async with _client() as c:
        r = await c.post("/api/settings", files={"file": ("x.json", b"{}", "application/json")})
    assert r.status_code == 415


async def test_a_take_part_upload_keeps_its_raw_body(network):
    async with _client() as c:
        r = await c.put(
            "/api/jobs/a1a1a1a1a1a1/takes/t00000000/parts/audio?seq=0",
            content=b"\0" * 8,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert r.status_code == 404, "the route answered, not the guard"


# ── unit: the pure helpers ──


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost", ("localhost", None)),
        ("LocalHost:8000", ("localhost", 8000)),
        ("[::1]:8000", ("::1", 8000)),
        ("[::1]", ("::1", None)),
        ("192.168.1.10:8444", ("192.168.1.10", 8444)),
        ("example.com.", ("example.com", None)),
    ],
)
def test_split_host(value, expected):
    assert csrf.split_host(value) == expected


def test_check_request_passes_a_plain_get():
    headers = httpx.Headers({"host": "127.0.0.1:8000"})
    assert (
        csrf.check_request(
            "GET", "/", headers, allow_network=False, desktop=False, extra_hosts=(), local_ips=()
        )
        is None
    )
