from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import settings as settings_mod
from app.main import _is_host_request, _is_loopback, app

MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("::ffff:127.0.0.1", True),
        ("127.0.1.1", True),
        ("192.168.1.14", False),
        ("10.0.0.5", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback(host, expected):
    assert _is_loopback(host) is expected


def test_host_request_recognizes_own_lan_ip(monkeypatch):
    # The host reaching itself via its LAN address must count as local, so
    # turning network access off never cuts the host off from its own server.
    monkeypatch.setattr("app.main._local_ips", lambda: frozenset({"192.168.1.14"}))
    assert _is_host_request("192.168.1.14") is True  # the host's own IP
    assert _is_host_request("127.0.0.1") is True  # loopback
    assert _is_host_request("192.168.1.99") is False  # a different device


def test_default_is_off(monkeypatch):
    # Off by default everywhere — the user must opt in.
    monkeypatch.delenv("STEMDECK_ALLOW_NETWORK", raising=False)
    assert settings_mod._default_allow_network() is False


def test_env_var_pre_enables(monkeypatch):
    monkeypatch.setenv("STEMDECK_ALLOW_NETWORK", "1")
    assert settings_mod._default_allow_network() is True


def test_runtime_settings_round_trip_and_clamp():
    with TestClient(app) as c:
        r = c.post("/api/settings", json={"max_duration_sec": 600, "video_max_height": 1080})
        assert r.status_code == 200
        body = r.json()
        assert body["max_duration_sec"] == 600
        assert body["video_max_height"] == 1080
        # GET reflects the new values.
        assert c.get("/api/settings").json()["max_duration_sec"] == 600

    # Out-of-range values are clamped, not rejected.
    assert settings_mod.set_max_duration_sec(5) == 60  # floor
    assert settings_mod.set_max_duration_sec(99999) == 1200  # ceiling = 20 min
    assert settings_mod.set_video_max_height(99999) == 2160  # ceil


def test_settings_reject_non_integer():
    with TestClient(app) as c:
        assert c.post("/api/settings", json={"max_duration_sec": "abc"}).status_code == 422


def test_gate_blocks_non_loopback_when_off():
    settings_mod.set_allow_network(False)
    # TestClient's client host ("testclient") is treated as non-loopback.
    with TestClient(app) as c:
        r = c.get("/", headers={"user-agent": MOBILE_UA})
    assert r.status_code == 403


def test_gate_allows_everyone_when_on():
    settings_mod.set_allow_network(True)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_loopback_always_allowed_even_when_off(monkeypatch):
    settings_mod.set_allow_network(False)
    monkeypatch.setattr("app.main._is_loopback", lambda _host: True)
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_post_toggles_off_then_blocks():
    settings_mod.set_allow_network(True)  # so the non-loopback client can reach POST
    with TestClient(app) as c:
        r = c.post("/api/settings", json={"allow_network": False})
        assert r.status_code == 200
        assert r.json()["allow_network"] is False
    # Now off → a non-loopback client is blocked from everything.
    with TestClient(app) as c:
        assert c.get("/api/settings").status_code == 403
