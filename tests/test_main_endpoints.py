"""The app-level endpoints and helpers that no other test file owns.

app/main.py was at 76%. The settings writer's validation, the registry viewer,
the log tail's parsing, the host-address detection behind the network gate, and
the version fallbacks all had branches nothing reached -- most of them the
failure branches, which is where a bug survives longest because the happy path
keeps passing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reopen_the_import_door():
    """A successful relocation deliberately never clears the flag -- the restart
    is what clears it, and the restart is the point. In-process that leaves it
    latched, and every later job creation anywhere in the suite gets a 409.
    """
    from app.core import stems_location

    stems_location.abandon_relocation()
    yield
    stems_location.abandon_relocation()


# --------------------------------------------------------------------------
# POST /api/settings -- validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["auto_delete_days", "max_duration_sec", "playlist_max_items", "video_max_height", "port"],
)
def test_an_integer_setting_rejects_something_that_is_not_one(client, key):
    """These all reach int(). Without the guard a string lands as a 500, and the
    settings panel shows a crash where it should show the field it dislikes."""
    r = client.post("/api/settings", json={key: "not-a-number"})

    assert r.status_code == 422
    assert key in r.json()["detail"]


@pytest.mark.parametrize(
    "key",
    ["auto_delete_days", "max_duration_sec", "playlist_max_items", "video_max_height", "port"],
)
def test_an_integer_setting_rejects_a_value_of_the_wrong_type(client, key):
    r = client.post("/api/settings", json={key: {"nested": 1}})

    assert r.status_code == 422


def test_a_body_that_is_not_json_is_treated_as_an_empty_update(client):
    """The panel sends JSON; anything else means "change nothing" rather than a
    500 that would look like the setting failed to save."""
    r = client.post(
        "/api/settings", content=b"not json at all", headers={"content-type": "application/json"}
    )

    assert r.status_code == 200


def test_an_unknown_export_sample_rate_names_the_valid_ones(client):
    r = client.post("/api/settings", json={"export_sample_rate": 12345})

    assert r.status_code == 422
    assert r.json()["detail"]


def test_an_unknown_demucs_device_is_refused_with_a_reason(client):
    r = client.post("/api/settings", json={"demucs_device": "quantum"})

    assert r.status_code == 422
    assert r.json()["detail"]


def test_an_unknown_separation_quality_is_refused_with_a_reason(client):
    r = client.post("/api/settings", json={"separation_quality": "perfect"})

    assert r.status_code == 422
    assert r.json()["detail"]


def test_a_cookies_file_that_is_not_a_path_is_refused(client):
    """set_cookies_file raises TypeError/OSError on a non-path; both have to
    land as a 422 rather than a traceback."""
    r = client.post("/api/settings", json={"cookies_file": {"not": "a path"}})

    assert r.status_code == 422
    assert r.json()["detail"]


def test_the_boolean_settings_round_trip(client):
    client.post(
        "/api/settings",
        json={"auto_delete_jobs": True, "auto_sections": True, "allow_network": True},
    )
    body = client.get("/api/settings").json()

    assert body["auto_delete_jobs"] is True
    assert body["auto_sections"] is True
    assert body["allow_network"] is True


# --------------------------------------------------------------------------
# GET /api/registry
# --------------------------------------------------------------------------


def test_the_registry_view_is_valid_json_even_before_anything_is_saved(client):
    """Settings -> Registry renders whatever this returns. An empty file would
    make it show a parse error on a fresh install."""
    import json

    r = client.get("/api/registry")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.text) == {"version": 1, "jobs": []}


def test_the_registry_view_serves_the_file_that_is_actually_on_disk(client, monkeypatch):
    import app.main as main
    from app.core.registry import registry_path

    path = registry_path(main.JOBS_DIR)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 1, "jobs": ["written-by-the-test"]}', encoding="utf-8")

    r = client.get("/api/registry")

    assert "written-by-the-test" in r.text


# --------------------------------------------------------------------------
# log tail helpers
# --------------------------------------------------------------------------


def test_a_python_log_timestamp_is_read_as_epoch_seconds():
    """The shape configure_logging() actually writes: "%Y-%m-%d %H:%M:%S", one
    letter of level, logger name, message -- no milliseconds."""
    import app.main as main

    line = "2026-03-04 05:06:07 I stemdeck something happened"

    assert main._line_time(line) == time.mktime(
        time.strptime("2026-03-04 05:06:07", "%Y-%m-%d %H:%M:%S")
    )


def test_the_setup_logs_bracketed_epoch_is_read_too():
    """The desktop shell's setup log stamps a raw epoch instead."""
    import app.main as main

    assert main._line_time("[1772600767] installing python runtime") == 1772600767.0


def test_a_line_with_no_timestamp_has_no_time():
    import app.main as main

    assert main._line_time("a bare continuation line") is None


def test_an_impossible_python_timestamp_is_not_a_time():
    """strptime raises on a date that matches the shape but not the calendar; a
    log line must never take the endpoint down."""
    import app.main as main

    assert main._line_time("2026-13-45 99:99:99,000 INFO nope") is None


def test_the_tail_reads_only_the_end_of_a_large_log(tmp_path):
    """Log files reach tens of megabytes. Reading all of it to show the last
    hour would stall the settings panel."""
    import app.main as main

    log = tmp_path / "big.log"
    log.write_text("".join(f"line {i}\n" for i in range(200_000)), encoding="utf-8")

    lines = main._tail_lines(log)

    assert lines, "nothing was read back"
    assert lines[-1] == "line 199999"
    # Bounded by the tail window rather than the file.
    assert len("\n".join(lines)) <= main._LOG_TAIL_BYTES


def test_the_tail_discards_the_partial_line_it_seeks_into(tmp_path):
    import app.main as main

    log = tmp_path / "big.log"
    log.write_text("".join(f"{i:09d} padding line\n" for i in range(200_000)), encoding="utf-8")

    lines = main._tail_lines(log)

    # Every line kept is whole -- the truncated first one is dropped.
    assert all(len(ln) == len("000000000 padding line") for ln in lines)


def test_a_log_that_is_not_there_tails_to_nothing(tmp_path):
    import app.main as main

    assert main._tail_lines(tmp_path / "absent.log") == []


def test_an_undecodable_log_is_still_readable(tmp_path):
    """A log can pick up bytes from a subprocess in another encoding. Showing
    replacement characters beats refusing to show the log."""
    import app.main as main

    log = tmp_path / "mixed.log"
    log.write_bytes(b"good line\n\xff\xfe invalid utf-8\ntail\n")

    assert main._tail_lines(log) == ["good line", "�� invalid utf-8", "tail"]


# --------------------------------------------------------------------------
# host detection -- what the network gate lets through
# --------------------------------------------------------------------------


def test_the_host_is_recognised_by_its_own_lan_address(monkeypatch):
    """Turning network access off must never cut the host off from its own
    server, including when it reached it via 192.168.x.x."""
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))

    assert main._is_host_request("192.168.1.50") is True


def test_an_ipv4_mapped_ipv6_address_is_unwrapped(monkeypatch):
    """A dual-stack socket reports the host's own address as ::ffff:192.168.1.50,
    which would not match the interface list as written."""
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))

    assert main._is_host_request("::ffff:192.168.1.50") is True


def test_another_device_is_not_the_host(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))

    assert main._is_host_request("192.168.1.99") is False


def test_a_request_with_no_peer_address_is_not_the_host(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_local_ips", lambda: frozenset({"192.168.1.50"}))

    assert main._is_host_request(None) is False


def test_loopback_is_always_the_host():
    import app.main as main

    assert main._is_host_request("127.0.0.1") is True


def test_interface_enumeration_survives_a_machine_that_cannot_answer(monkeypatch):
    """An odd hostname or no default route makes both probes raise. The gate
    still has to return a set rather than blow up mid-request."""
    import socket as socket_mod

    import app.main as main

    def _boom(*a, **kw):
        raise OSError("no such host")

    monkeypatch.setattr(socket_mod, "gethostname", _boom)
    monkeypatch.setattr(socket_mod, "socket", _boom)
    main._local_ips.cache_clear()
    try:
        assert main._local_ips() == frozenset()
    finally:
        main._local_ips.cache_clear()


# --------------------------------------------------------------------------
# app_version fallbacks
# --------------------------------------------------------------------------


def test_the_version_falls_back_to_the_generated_module(monkeypatch, tmp_path):
    """A source checkout is not pip-installed, so there is no dist metadata --
    only the file hatch-vcs writes at build time."""
    import app.main as main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    def _absent(_name):
        raise main.PackageNotFoundError("stemdeck")

    monkeypatch.setattr(main, "package_version", _absent)
    monkeypatch.setattr("app._version.__version__", "4.5.6", raising=False)

    assert main.app_version() == "4.5.6"


def test_the_version_degrades_to_a_dev_placeholder_when_nothing_knows(monkeypatch, tmp_path):
    """No marker, no metadata, no generated file: a git clone run in place. It
    reports a placeholder rather than failing the health check."""
    import sys

    import app.main as main

    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    def _absent(_name):
        raise main.PackageNotFoundError("stemdeck")

    monkeypatch.setattr(main, "package_version", _absent)
    monkeypatch.setitem(sys.modules, "app._version", None)

    assert main.app_version() == "0.0.0-dev"


def test_a_version_marker_that_is_json_but_not_an_object_is_ignored(monkeypatch, tmp_path):
    """A truncated or hand-edited version.json can be a bare list or string;
    .get would raise AttributeError on it."""
    import app.main as main

    (tmp_path / "version.json").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    assert main.app_version() == main.package_version("stemdeck")


def test_a_version_marker_holding_a_blank_string_is_ignored(monkeypatch, tmp_path):
    import app.main as main

    (tmp_path / "version.json").write_text('{"version": "   "}', encoding="utf-8")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)

    assert main.app_version() == main.package_version("stemdeck")


# --------------------------------------------------------------------------
# the hourly sweep loop
# --------------------------------------------------------------------------


async def test_a_failing_sweep_does_not_end_the_hourly_loop(monkeypatch):
    """The loop runs for the life of the process. One unreadable directory must
    not stop every later job from ever being cleaned up."""
    import app.main as main

    calls = {"n": 0}

    def _boom(*a, **kw):
        calls["n"] += 1
        raise OSError("disk went away")

    monkeypatch.setattr(main, "get_auto_delete_jobs", lambda: True)
    monkeypatch.setattr(main, "get_auto_delete_days", lambda: 30)
    monkeypatch.setattr(main, "sweep_old_jobs", _boom)

    slept = asyncio.Event()

    async def _sleep(_seconds):
        slept.set()
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._sweep_loop()

    # It reached the sleep, which means the exception was caught rather than
    # propagating out of the loop body.
    assert calls["n"] == 1
    assert slept.is_set()


async def test_the_failure_quarantine_is_swept_even_with_deletion_off(monkeypatch):
    """Failure evidence is diagnostics, not library content: it expires on every
    deployment, including the ones that keep their jobs forever."""
    import app.main as main

    swept: list = []
    monkeypatch.setattr(main, "get_auto_delete_jobs", lambda: False)
    monkeypatch.setattr(main, "sweep_old_jobs", lambda *a: swept.append("jobs"))
    monkeypatch.setattr(main, "sweep_failed_jobs", lambda *a: swept.append("failed"))

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._sweep_loop()

    assert swept == ["failed"]


async def test_the_configured_retention_is_converted_to_seconds(monkeypatch):
    import app.main as main

    seen: list = []
    monkeypatch.setattr(main, "get_auto_delete_jobs", lambda: True)
    monkeypatch.setattr(main, "get_auto_delete_days", lambda: 7)
    monkeypatch.setattr(main, "sweep_old_jobs", lambda _dir, ttl: seen.append(ttl))
    monkeypatch.setattr(main, "sweep_failed_jobs", lambda *a: None)

    async def _sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._sweep_loop()

    assert seen == [7 * 86400]


# --------------------------------------------------------------------------
# moving the stems library -- the branches that reopen the door
# --------------------------------------------------------------------------


@pytest.fixture
def desktop(monkeypatch):
    """The relocation endpoint is desktop-only; server mode has no folder picker."""
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")


def test_a_move_is_refused_while_a_job_is_running(client, desktop, tmp_path):
    """Moving files out from under a running separation would corrupt it."""
    from app.core.models import Job
    from app.core.registry import _jobs

    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    try:
        r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})
    finally:
        _jobs.clear()

    assert r.status_code == 409
    assert "queue" in r.json()["detail"]


def test_a_refused_move_leaves_the_door_open_for_new_imports(client, desktop, tmp_path):
    """begin_relocation() closes the door before validation touches the disk.
    Every early exit has to reopen it, or the app silently stops accepting
    imports until it is restarted."""
    from app.core import stems_location

    r = client.post("/api/settings/stems-location", json={"path": "not-an-absolute-path"})

    assert r.status_code == 422
    assert stems_location.is_relocating() is False


def test_a_missing_path_is_refused_before_the_door_is_touched(client, desktop):
    from app.core import stems_location

    r = client.post("/api/settings/stems-location", json={})

    assert r.status_code == 422
    assert "path is required" in r.json()["detail"]
    assert stems_location.is_relocating() is False


def test_a_move_that_fails_partway_reopens_the_door_and_explains(
    client, desktop, tmp_path, monkeypatch
):
    """The library is still where this process thinks it is, so the app stays
    usable rather than being wedged shut by a failed move."""
    from app.core import stems_location

    def _boom(current, target):
        raise stems_location.StemsLocationError("Could not move drums.wav: disk full")

    monkeypatch.setattr("app.main.move_library", _boom)

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})

    assert r.status_code == 500
    assert "disk full" in r.json()["detail"]
    assert stems_location.is_relocating() is False


def test_an_unexpected_move_failure_does_not_leak_its_internals(
    client, desktop, tmp_path, monkeypatch
):
    """A path or an OS error in the message would end up on screen."""
    from app.core import stems_location

    def _boom(current, target):
        raise RuntimeError(f"internal state at {tmp_path}")

    monkeypatch.setattr("app.main.move_library", _boom)

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})

    assert r.status_code == 500
    assert r.json()["detail"] == "Could not move the stems folder"
    assert str(tmp_path) not in r.text
    assert stems_location.is_relocating() is False


def test_an_unexpected_validation_failure_also_reopens_the_door(
    client, desktop, tmp_path, monkeypatch
):
    from app.core import stems_location

    def _boom(target, current):
        raise RuntimeError("stat exploded")

    monkeypatch.setattr("app.main.validate_target", _boom)

    with pytest.raises(RuntimeError, match="stat exploded"):
        client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})

    assert stems_location.is_relocating() is False


def test_a_non_json_body_on_the_move_endpoint_is_a_422(client, desktop):
    from app.core import stems_location

    r = client.post(
        "/api/settings/stems-location",
        content=b"<xml/>",
        headers={"content-type": "application/json"},
    )

    assert r.status_code == 422
    assert stems_location.is_relocating() is False


def test_the_move_endpoint_is_not_reachable_outside_the_desktop_shell(
    client, monkeypatch, tmp_path
):
    """Server mode has no folder picker and no business relocating a library on
    a request from the network."""
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})

    assert r.status_code in (403, 404)


def test_a_successful_move_reports_what_it_moved(client, desktop, tmp_path, monkeypatch):
    import app.main as main
    from app.core.stems_location import MoveResult

    target = tmp_path / "new-library"

    def _move(current, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        return MoveResult(str(current), str(dest), 3, 4096, True)

    monkeypatch.setattr(main, "move_library", _move)

    r = client.post("/api/settings/stems-location", json={"path": str(target)})

    assert r.status_code == 200
    body = r.json()
    assert body["moved_entries"] == 3
    assert body["bytes_moved"] == 4096
