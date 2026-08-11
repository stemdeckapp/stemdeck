"""Moving the stem library to a different folder (#354).

The setting is bookkeeping; the move is the feature, and it is the part that can
destroy someone's library. Most of what follows is about refusing to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.core.stems_location import (
    StemsLocationError,
    directory_size,
    move_library,
    validate_target,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


def _job_dir(root: Path, job_id: str, *, size: int = 32) -> Path:
    d = root / job_id / "stems"
    d.mkdir(parents=True, exist_ok=True)
    (d / "vocals.wav").write_bytes(b"R" * size)
    return root / job_id


# ── validation ───────────────────────────────────────────────────────────────


def test_accepts_an_empty_folder(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "new"
    target.mkdir()
    assert validate_target(target, current) == target.resolve()


def test_creates_the_folder_when_it_does_not_exist(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "brand" / "new"
    assert validate_target(target, current) == target.resolve()
    assert target.is_dir()


def test_rejects_the_current_location(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    with pytest.raises(StemsLocationError, match="already stored"):
        validate_target(current, current)


def test_rejects_a_folder_inside_the_current_one(tmp_path):
    """Moving a directory into itself would recurse forever."""
    current = tmp_path / "old"
    (current / "inner").mkdir(parents=True)
    with pytest.raises(StemsLocationError, match="outside"):
        validate_target(current / "inner", current)


def test_rejects_a_folder_full_of_the_users_own_files(tmp_path):
    """Merging a stem library into someone's Desktop is not recoverable."""
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "desktop"
    target.mkdir()
    (target / "tax return.pdf").write_bytes(b"%PDF")
    with pytest.raises(StemsLocationError, match="empty folder"):
        validate_target(target, current)


def test_accepts_a_folder_stemdeck_already_uses(tmp_path):
    """Retrying an interrupted move must not be blocked by its own progress."""
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "new"
    _job_dir(target, "abcdefabcdef")
    (target / "registry.json").write_text("{}", encoding="utf-8")
    assert validate_target(target, current) == target.resolve()


def test_rejects_a_file(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "notafolder.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(StemsLocationError, match="not a folder"):
        validate_target(target, current)


def test_rejects_a_relative_path(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    with pytest.raises(StemsLocationError, match="absolute"):
        validate_target(Path("stems"), current)


def test_rejects_an_empty_path(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    with pytest.raises(StemsLocationError):
        validate_target(Path(""), current)


# ── the move ─────────────────────────────────────────────────────────────────


def test_moves_every_job_and_the_registry(tmp_path):
    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    _job_dir(current, "bbbbbbbbbbbb")
    (current / "registry.json").write_text('{"jobs": []}', encoding="utf-8")
    target = tmp_path / "new"

    result = move_library(current, target)

    assert result.moved_entries == 3
    assert (target / "aaaaaaaaaaaa" / "stems" / "vocals.wav").is_file()
    assert (target / "bbbbbbbbbbbb" / "stems" / "vocals.wav").is_file()
    assert (target / "registry.json").is_file()
    assert list(current.iterdir()) == [], "nothing should be left behind"


def test_the_registry_moves_with_the_stems(tmp_path):
    """It lives inside the stems folder, so leaving it behind would bring the
    app back empty with the files stranded."""
    current = tmp_path / "old"
    current.mkdir()
    (current / "registry.json").write_text('{"jobs": [{"id": "x"}]}', encoding="utf-8")
    target = tmp_path / "new"

    move_library(current, target)

    assert not (current / "registry.json").exists()
    assert '"id": "x"' in (target / "registry.json").read_text(encoding="utf-8")


def test_an_interrupted_move_can_be_resumed(tmp_path):
    """Entries already at the target are skipped rather than colliding."""
    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    _job_dir(current, "bbbbbbbbbbbb")
    target = tmp_path / "new"
    _job_dir(target, "aaaaaaaaaaaa")  # a previous attempt got this far

    result = move_library(current, target)

    assert result.moved_entries == 1
    assert (target / "bbbbbbbbbbbb").is_dir()


def test_moving_an_empty_library_is_not_an_error(tmp_path):
    current = tmp_path / "old"
    current.mkdir()
    result = move_library(current, tmp_path / "new")
    assert result.moved_entries == 0


def test_moving_from_a_folder_that_never_existed(tmp_path):
    result = move_library(tmp_path / "never", tmp_path / "new")
    assert result.moved_entries == 0


def test_directory_size_adds_up(tmp_path):
    _job_dir(tmp_path, "aaaaaaaaaaaa", size=100)
    _job_dir(tmp_path, "bbbbbbbbbbbb", size=50)
    assert directory_size(tmp_path) == 150


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    jobs = tmp_path / "current"
    jobs.mkdir()
    import app.core.settings as settings_mod
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "JOBS_DIR", jobs)
    monkeypatch.setattr(settings_mod, "_SETTINGS_PATH", tmp_path / "settings.json")
    settings_mod._state = None
    from app.main import app

    with TestClient(app) as c:
        c.jobs_dir = jobs
        yield c
    settings_mod._state = None


def test_reports_the_current_location_and_size(client, tmp_path):
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa", size=200)
    body = client.get("/api/settings/stems-location").json()
    assert body["path"] == str(client.jobs_dir)
    assert body["bytes"] == 200
    assert body["is_default"] is True
    assert body["busy"] is False


def test_moving_writes_the_setting_and_asks_for_a_restart(client, tmp_path):
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    target = tmp_path / "elsewhere"

    body = client.post("/api/settings/stems-location", json={"path": str(target)}).json()

    assert body["path"] == str(target.resolve())
    assert body["moved_entries"] == 1
    assert body["restart_required"] is True
    assert (target / "aaaaaaaaaaaa").is_dir()

    from app.core.settings import get_jobs_dir

    assert get_jobs_dir() == str(target.resolve())


def test_refuses_while_a_job_is_running(client, tmp_path):
    """Moving files out from under a separation would corrupt it."""
    job = Job(id="aaaaaaaaaaaa")
    job.status = "separating"
    _jobs[job.id] = job
    _job_dir(client.jobs_dir, "bbbbbbbbbbbb")

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})

    assert r.status_code == 409
    assert (client.jobs_dir / "bbbbbbbbbbbb").is_dir(), "nothing may move while busy"


def test_refuses_while_a_job_is_merely_queued(client, tmp_path):
    job = Job(id="aaaaaaaaaaaa")
    job.status = "queued"
    _jobs[job.id] = job
    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})
    assert r.status_code == 409


def test_a_rejected_target_leaves_the_setting_alone(client, tmp_path):
    """The preference is only written after the move succeeds, so a failure
    leaves the app reading the folder the files are actually in."""
    from app.core.settings import get_jobs_dir

    occupied = tmp_path / "someone-elses"
    occupied.mkdir()
    (occupied / "holiday.jpg").write_bytes(b"\xff\xd8")

    r = client.post("/api/settings/stems-location", json={"path": str(occupied)})

    assert r.status_code == 422
    assert get_jobs_dir() is None


def test_missing_path_is_a_422(client):
    assert client.post("/api/settings/stems-location", json={}).status_code == 422


# ── desktop only ─────────────────────────────────────────────────────────────


@pytest.fixture
def server_client(tmp_path, monkeypatch):
    """The same app without the desktop shell: a self-hosted server, Docker or
    Unraid deployment."""
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    jobs = tmp_path / "current"
    jobs.mkdir()
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "JOBS_DIR", jobs)
    from app.main import app

    with TestClient(app) as c:
        c.jobs_dir = jobs
        yield c


def test_not_offered_outside_the_desktop_app(server_client):
    """Docker and Unraid mount their storage; the location is the operator's
    decision, and it is still the mount on the next start."""
    assert server_client.get("/api/settings/stems-location").status_code == 403


def test_cannot_be_moved_outside_the_desktop_app(server_client, tmp_path):
    _job_dir(server_client.jobs_dir, "aaaaaaaaaaaa")
    r = server_client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})
    assert r.status_code == 403
    assert (server_client.jobs_dir / "aaaaaaaaaaaa").is_dir(), "nothing may move"
