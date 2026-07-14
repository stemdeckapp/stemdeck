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


def test_default_is_off_in_desktop_mode(monkeypatch):
    # Desktop keeps network off; the user opts in via the UI toggle.
    monkeypatch.delenv("STEMDECK_ALLOW_NETWORK", raising=False)
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    assert settings_mod._default_allow_network() is False


def test_default_is_on_in_server_mode(monkeypatch):
    # Server/Docker deployments open the gate by default.
    monkeypatch.delenv("STEMDECK_ALLOW_NETWORK", raising=False)
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    assert settings_mod._default_allow_network() is True


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


def test_port_default_and_clamp():
    assert settings_mod.get_port() == 8000  # default
    with TestClient(app) as c:
        assert c.post("/api/settings", json={"port": 9000}).json()["port"] == 9000
    assert settings_mod.set_port(80) == 1024  # floor (privileged ports rejected)
    assert settings_mod.set_port(70000) == 65535  # ceil


def test_settings_reject_non_integer():
    with TestClient(app) as c:
        assert c.post("/api/settings", json={"max_duration_sec": "abc"}).status_code == 422


# ── demucs_device (compute device) ──


@pytest.fixture()
def _isolated_settings(monkeypatch, tmp_path):
    """Point the settings store at a temp file with a fresh in-memory state, so
    device tests neither read nor pollute the developer's real settings.json."""
    monkeypatch.setattr(settings_mod, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "_state", None)
    monkeypatch.delenv("STEMDECK_DEMUCS_DEVICE", raising=False)


def test_demucs_device_defaults_to_auto_and_resolves(monkeypatch, _isolated_settings):
    monkeypatch.setattr(settings_mod, "detect_torch_device", lambda: "cpu")
    assert settings_mod.get_demucs_device_choice() == "auto"
    assert settings_mod.get_demucs_device() == "cpu"  # auto -> hardware probe
    # A different probe result flows through without any persisted change.
    monkeypatch.setattr(settings_mod, "detect_torch_device", lambda: "cuda")
    assert settings_mod.get_demucs_device() == "cuda"


def test_demucs_device_env_seeds_default(monkeypatch, _isolated_settings):
    # Existing env-based deployments keep their forced device as the default.
    monkeypatch.setenv("STEMDECK_DEMUCS_DEVICE", "cuda")
    assert settings_mod.get_demucs_device_choice() == "cuda"
    assert settings_mod.get_demucs_device() == "cuda"  # forced, no probe


def test_demucs_device_force_verified_before_persist(monkeypatch, _isolated_settings):
    # Forcing a device that isn't available must be rejected loudly, not
    # persisted to silently fail later (#247 lesson applied to the server).
    monkeypatch.setattr(settings_mod, "available_torch_devices", lambda: ["cpu"])
    with pytest.raises(ValueError):
        settings_mod.set_demucs_device("cuda")
    assert settings_mod.get_demucs_device_choice() == "auto"  # nothing persisted
    # Forcing CPU is always allowed; forcing an available GPU is allowed.
    assert settings_mod.set_demucs_device("cpu") == "cpu"
    monkeypatch.setattr(settings_mod, "available_torch_devices", lambda: ["cuda", "cpu"])
    assert settings_mod.set_demucs_device("cuda") == "cuda"
    assert settings_mod.get_demucs_device() == "cuda"  # forced, no probe


def test_demucs_device_rejects_unknown_choice(_isolated_settings):
    with pytest.raises(ValueError):
        settings_mod.set_demucs_device("bogus")


def test_demucs_device_api_round_trip_and_422(monkeypatch, _isolated_settings):
    monkeypatch.setattr(settings_mod, "detect_torch_device", lambda: "cpu")
    monkeypatch.setattr(settings_mod, "available_torch_devices", lambda: ["cpu"])
    monkeypatch.setattr("app.main.available_torch_devices", lambda: ["cpu"])
    with TestClient(app) as c:
        body = c.get("/api/settings").json()
        assert body["demucs_device"] == "auto"
        assert body["demucs_device_resolved"] == "cpu"
        # UI availability: cuda/mps grayed out when only cpu is present.
        assert body["demucs_devices_available"] == ["cpu"]
        # Valid change round-trips.
        r = c.post("/api/settings", json={"demucs_device": "cpu"})
        assert r.status_code == 200
        assert r.json()["demucs_device"] == "cpu"
        # Unavailable device -> 422 with a user-actionable detail, not persisted.
        r = c.post("/api/settings", json={"demucs_device": "cuda"})
        assert r.status_code == 422
        assert "not available" in r.json()["detail"]
        assert c.get("/api/settings").json()["demucs_device"] == "cpu"
        # Unknown value -> 422.
        assert c.post("/api/settings", json={"demucs_device": "bogus"}).status_code == 422


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
