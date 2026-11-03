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
    assert settings_mod.set_max_duration_sec(99999) == 3600  # ceiling = 60 min
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


def test_export_sample_rate_round_trip_and_default(_isolated_settings):
    assert settings_mod.get_export_sample_rate() == 44100  # default = stem rate
    with TestClient(app) as c:
        r = c.post("/api/settings", json={"export_sample_rate": 48000})
        assert r.status_code == 200
        assert r.json()["export_sample_rate"] == 48000
        assert c.get("/api/settings").json()["export_sample_rate"] == 48000


def test_export_sample_rate_rejects_off_allowlist(_isolated_settings):
    # An arbitrary rate is rejected (422), not clamped to the nearest allowed one.
    with TestClient(app) as c:
        assert c.post("/api/settings", json={"export_sample_rate": 96000}).status_code == 422
        assert c.post("/api/settings", json={"export_sample_rate": "abc"}).status_code == 422
    with pytest.raises(ValueError):
        settings_mod.set_export_sample_rate(96000)


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


# ── separation_quality ──


def test_separation_quality_defaults_to_standard(_isolated_settings):
    assert settings_mod.get_separation_quality() == "standard"


def test_separation_quality_env_seeds_default(monkeypatch, _isolated_settings):
    monkeypatch.setenv("STEMDECK_SEPARATION_QUALITY", "best")
    assert settings_mod.get_separation_quality() == "best"


def test_separation_quality_rejects_unknown_choice(_isolated_settings):
    with pytest.raises(ValueError):
        settings_mod.set_separation_quality("ultra")
    assert settings_mod.get_separation_quality() == "standard"  # nothing persisted


def test_separation_quality_api_round_trip_and_422(_isolated_settings):
    with TestClient(app) as c:
        assert c.get("/api/settings").json()["separation_quality"] == "standard"
        r = c.post("/api/settings", json={"separation_quality": "best"})
        assert r.status_code == 200
        assert r.json()["separation_quality"] == "best"
        assert c.get("/api/settings").json()["separation_quality"] == "best"
        r = c.post("/api/settings", json={"separation_quality": "ultra"})
        assert r.status_code == 422
        assert c.get("/api/settings").json()["separation_quality"] == "best"  # unchanged


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


# ── auto_sections (experimental song-structure extraction) ──


def test_auto_sections_defaults_off(_isolated_settings):
    """Experimental, and it costs an inference pass. Nobody pays by default."""
    assert settings_mod.get_auto_sections() is False


def test_auto_sections_env_can_turn_it_on(monkeypatch, _isolated_settings):
    """A deployment that wants it from first boot opts in explicitly."""
    monkeypatch.setenv("STEMDECK_AUTO_SECTIONS", "1")
    assert settings_mod.get_auto_sections() is True
    # Anything else still means off, so a malformed value cannot silently
    # enable a cost the user never asked for.
    monkeypatch.setenv("STEMDECK_AUTO_SECTIONS", "yes please")
    assert settings_mod.get_auto_sections() is False


def test_auto_sections_api_round_trip(_isolated_settings):
    with TestClient(app) as c:
        assert c.get("/api/settings").json()["auto_sections"] is False
        r = c.post("/api/settings", json={"auto_sections": True})
        assert r.status_code == 200
        assert r.json()["auto_sections"] is True
        assert c.get("/api/settings").json()["auto_sections"] is True
        assert settings_mod.get_auto_sections() is True


def test_auto_sections_saved_choice_beats_the_env_default(monkeypatch, _isolated_settings):
    """An explicit choice must survive an env var that says otherwise."""
    settings_mod.set_auto_sections(True)
    monkeypatch.setenv("STEMDECK_AUTO_SECTIONS", "")
    assert settings_mod.get_auto_sections() is True


# ── the gate against real peer addresses (#859) ──
#
# The tests above fake the peer by patching _is_loopback, or rely on
# TestClient's "testclient" peer. These give the app a real address instead,
# the way uvicorn would: a device elsewhere on the network (203.0.113.5, a
# documentation address), loopback, and the machine's own LAN address, which
# is what the report in #859 used for its curl.

OUTSIDE = "203.0.113.5"
OWN_LAN_IP = "192.168.1.77"
REFUSED = "StemDeck is not available on the network"


@pytest.fixture()
def own_ips(monkeypatch):
    """The machine's own interface IPs, fixed so no test depends on the host."""
    monkeypatch.setattr("app.main._local_ips", lambda: frozenset({OWN_LAN_IP}))


@pytest.fixture()
def network_off(own_ips):
    settings_mod.set_allow_network(False)


def _peer(host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


def _assert_refused(r) -> None:
    assert r.status_code == 403
    assert REFUSED in r.text


def test_off_refuses_every_method_from_another_device(network_off):
    from app.core import config
    from app.core.models import Job
    from app.core.registry import _jobs

    with _peer(OUTSIDE) as c:
        job = Job(id="abcdefabcd59", status="done")
        _jobs[job.id] = job
        (config.JOBS_DIR / job.id).mkdir()
        before = set(_jobs)
        try:
            _assert_refused(c.get("/"))
            _assert_refused(c.get("/api/jobs"))
            _assert_refused(c.get("/api/settings"))
            _assert_refused(c.post("/api/jobs", json={"url": "https://example.com/a"}))
            _assert_refused(c.post("/api/settings", json={"allow_network": True}))
            _assert_refused(c.delete(f"/api/jobs/{job.id}"))

            # Nothing was created, deleted or switched on.
            assert set(_jobs) == before
            assert (config.JOBS_DIR / job.id).is_dir()
            assert sorted(p.name for p in config.JOBS_DIR.iterdir() if p.is_dir()) == [job.id]
            assert settings_mod.get_allow_network() is False
        finally:
            _jobs.pop(job.id, None)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_off_serves_loopback(network_off, host):
    with _peer(host) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/settings").status_code == 200
        assert c.get("/api/jobs").status_code == 200


def test_off_serves_the_machines_own_lan_address(network_off):
    # The case in #859: a curl from the Mac to its own LAN address is the
    # host reaching itself, which the gate always serves by design.
    with _peer(OWN_LAN_IP) as c:
        r = c.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["allow_network"] is False


@pytest.mark.parametrize(
    "host,served",
    [("::ffff:203.0.113.5", False), ("::ffff:127.0.0.1", True)],
)
def test_off_reads_ipv4_mapped_peers_as_ipv4(network_off, host, served):
    with _peer(host) as c:
        r = c.get("/api/settings")
    if served:
        assert r.status_code == 200
    else:
        _assert_refused(r)


def test_on_serves_another_device(own_ips):
    settings_mod.set_allow_network(True)
    with _peer(OUTSIDE) as c:
        assert c.get("/api/settings").status_code == 200


@pytest.mark.parametrize("path", ["/no-such-page", "/api/no-such-route"])
def test_off_refuses_before_routing(network_off, path):
    # A 404 here would mean a route answered first.
    with _peer(OUTSIDE) as c:
        _assert_refused(c.get(path))


def test_toggling_applies_without_a_restart(own_ips):
    settings_mod.set_allow_network(False)
    with _peer("127.0.0.1") as host:
        outside = _peer(OUTSIDE)
        _assert_refused(outside.get("/api/settings"))

        assert host.post("/api/settings", json={"allow_network": True}).status_code == 200
        assert outside.get("/api/settings").status_code == 200

        assert host.post("/api/settings", json={"allow_network": False}).status_code == 200
        _assert_refused(outside.get("/api/settings"))


# ── what the desktop backend actually listens on (#859) ──
#
# The shell binds loopback while network access is off and every interface
# while it is on, and says which through STEMDECK_BIND_HOST. Settings reports
# it, so the switch shows what is listening, not only what was saved.


@pytest.mark.parametrize(
    "bind_host,listening",
    [("127.0.0.1", False), ("0.0.0.0", True), (None, None)],
)
def test_settings_report_what_the_backend_listens_on(monkeypatch, bind_host, listening):
    monkeypatch.setattr("app.main.BIND_HOST", bind_host)
    with _peer("127.0.0.1") as c:
        assert c.get("/api/settings").json()["network_listening"] is listening


def test_a_saved_choice_does_not_change_what_is_listening(monkeypatch):
    # The new bind needs the restart; until then the report stays the old one.
    monkeypatch.setattr("app.main.BIND_HOST", "127.0.0.1")
    with _peer("127.0.0.1") as c:
        body = c.post("/api/settings", json={"allow_network": True}).json()
        assert body["allow_network"] is True
        assert c.get("/api/settings").json()["network_listening"] is False
