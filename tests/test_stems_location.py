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
    from app.core import stems_location

    _jobs.clear()
    stems_location.abandon_relocation()
    yield
    _jobs.clear()
    stems_location.abandon_relocation()


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


def test_accepts_a_folder_with_the_desktop_shells_user_data_store(tmp_path):
    """user-data.json (#403) now lives inside the jobs folder like
    registry.json -- re-selecting a folder from a previous move must not be
    rejected just because that file is already there."""
    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "new"
    _job_dir(target, "abcdefabcdef")
    (target / "user-data.json").write_text("{}", encoding="utf-8")
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
    from app.pipeline import jobqueue

    # Accepting a submit is the whole assertion here; letting the worker pick it
    # up would send the suite to YouTube.
    monkeypatch.setattr(jobqueue, "enqueue", lambda job_id, **kw: None)

    monkeypatch.setattr(main_mod, "JOBS_DIR", jobs)
    monkeypatch.setattr(settings_mod, "_SETTINGS_PATH", tmp_path / "settings.json")
    settings_mod._state = None

    with TestClient(main_mod.app) as c:
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
    assert body["editable"] is True


def test_moving_writes_the_setting_and_asks_for_a_restart(client, tmp_path):
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    target = tmp_path / "elsewhere"

    body = client.post("/api/settings/stems-location", json={"path": str(target)}).json()

    assert body["path"] == str(target.resolve())
    assert body["moved_entries"] == 1
    assert body["restart_required"] is True
    assert body["persisted"] is True
    assert (target / "aaaaaaaaaaaa").is_dir()

    import app.core.settings as settings_mod

    assert settings_mod.get_jobs_dir() == str(target.resolve())


def test_persist_failure_is_reported_not_swallowed(client, tmp_path, monkeypatch):
    """#403: a settings.json write failure used to be silently swallowed, so
    the endpoint reported success even though a restart would revert to the
    old (now-empty) folder. The physical move still succeeds either way --
    only the persisted flag (and the message the UI shows) should change."""
    import app.core.settings as settings_mod

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    target = tmp_path / "elsewhere"
    monkeypatch.setattr(settings_mod, "_save", lambda: False)

    body = client.post("/api/settings/stems-location", json={"path": str(target)}).json()

    assert body["moved_entries"] == 1
    assert (target / "aaaaaaaaaaaa").is_dir(), "the move itself must still happen"
    assert body["persisted"] is False

    # A restart re-reads settings.json from disk; the mocked failure means
    # the write never actually landed, so the stored choice is gone too.
    settings_mod._state = None
    assert settings_mod.get_jobs_dir() is None


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
    import app.core.settings as settings_mod

    occupied = tmp_path / "someone-elses"
    occupied.mkdir()
    (occupied / "holiday.jpg").write_bytes(b"\xff\xd8")

    r = client.post("/api/settings/stems-location", json={"path": str(occupied)})

    assert r.status_code == 422
    assert settings_mod.get_jobs_dir() is None


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

    with TestClient(main_mod.app) as c:
        c.jobs_dir = jobs
        yield c


def test_not_offered_outside_the_desktop_app(server_client):
    """Docker and Unraid mount their storage; the location is the operator's
    decision, and it is still the mount on the next start.

    The read still answers -- that flag is how the UI knows to hide the control.
    Refusing it would log a failed request every time Settings is opened."""
    body = server_client.get("/api/settings/stems-location").json()
    assert body["editable"] is False


def test_cannot_be_moved_outside_the_desktop_app(server_client, tmp_path):
    _job_dir(server_client.jobs_dir, "aaaaaaaaaaaa")
    r = server_client.post("/api/settings/stems-location", json={"path": str(tmp_path / "new")})
    assert r.status_code == 403
    assert (server_client.jobs_dir / "aaaaaaaaaaaa").is_dir(), "nothing may move"


# ── where JOBS_DIR comes from ────────────────────────────────────────────────
#
# Resolved at import time across the app, so these run in a subprocess. Worth
# the awkwardness: get this wrong and every existing desktop user opens the app
# to an empty library after an update, with their stems still on disk.


def _resolve_jobs_dir(tmp_path: Path, env_extra: dict, settings: dict | None) -> str:
    import json
    import os
    import subprocess
    import sys

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    settings_file = data / "settings.json"
    if settings is None:
        settings_file.unlink(missing_ok=True)
    else:
        settings_file.write_text(json.dumps(settings), encoding="utf-8")

    env = {**os.environ, "STEMDECK_DATA_DIR": str(data)}
    env.pop("STEMDECK_JOBS_DIR", None)
    env.pop("STEMDECK_DEFAULT_JOBS_DIR", None)
    env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", "from app.core.config import JOBS_DIR; print(JOBS_DIR)"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        check=True,
    )
    return out.stdout.strip()


def test_the_desktop_default_is_used_until_the_user_chooses(tmp_path):
    """The upgrade path. The launcher passes its Documents folder as a DEFAULT,
    and with no stored choice that is exactly where the library must stay."""
    default = tmp_path / "documents-jobs"
    got = _resolve_jobs_dir(tmp_path, {"STEMDECK_DEFAULT_JOBS_DIR": str(default)}, None)
    assert got == str(default)


def test_a_stored_choice_beats_the_desktop_default(tmp_path):
    chosen = tmp_path / "chosen"
    chosen.mkdir()  # it exists in reality: the move creates it before the setting is written
    got = _resolve_jobs_dir(
        tmp_path,
        {"STEMDECK_DEFAULT_JOBS_DIR": str(tmp_path / "documents-jobs")},
        {"jobs_dir": str(chosen)},
    )
    assert got == str(chosen)


def test_an_explicit_pin_beats_everything(tmp_path):
    """Docker and Unraid mount their storage. A stray setting in the image must
    not send the library somewhere the container no longer looks."""
    mount = tmp_path / "mount"
    got = _resolve_jobs_dir(
        tmp_path,
        {
            "STEMDECK_JOBS_DIR": str(mount),
            "STEMDECK_DEFAULT_JOBS_DIR": str(tmp_path / "documents-jobs"),
        },
        {"jobs_dir": str(tmp_path / "chosen")},
    )
    assert got == str(mount)


def test_a_corrupt_setting_falls_back_rather_than_moving_the_library(tmp_path):
    """A library that quietly relocates because a JSON file got mangled is far
    worse than one that ignores the preference."""
    default = tmp_path / "documents-jobs"
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "settings.json").write_text("{not json", encoding="utf-8")

    import os
    import subprocess
    import sys

    env = {**os.environ, "STEMDECK_DATA_DIR": str(tmp_path / "data")}
    env.pop("STEMDECK_JOBS_DIR", None)
    env["STEMDECK_DEFAULT_JOBS_DIR"] = str(default)
    out = subprocess.run(
        [sys.executable, "-c", "from app.core.config import JOBS_DIR; print(JOBS_DIR)"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        check=True,
    )
    assert out.stdout.strip() == str(default)


def test_a_configured_folder_that_is_gone_falls_back(tmp_path):
    """An external disk that is not mounted. Creating the folder instead would
    put a phantom directory at the mount point on the boot disk, and the user's
    library would look empty with no hint why."""
    default = tmp_path / "documents-jobs"
    got = _resolve_jobs_dir(
        tmp_path,
        {"STEMDECK_DEFAULT_JOBS_DIR": str(default)},
        {"jobs_dir": str(tmp_path / "unplugged-drive" / "StemDeck")},
    )
    assert got == str(default)
    assert not (tmp_path / "unplugged-drive").exists(), "must not create the missing path"


# ── the window between the move and the restart ──────────────────────────────


def test_importing_is_refused_until_the_restart(client, tmp_path):
    """JOBS_DIR is bound at import time, so this process still writes to the old
    folder after a move. A track accepted here would be orphaned by the restart:
    on disk, in a directory nothing looks at any more."""
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})

    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})

    assert r.status_code == 409
    assert "Restart" in r.json()["detail"]
    assert not list(client.jobs_dir.glob("*/")), "nothing new may appear in the old folder"


def test_a_playlist_import_is_refused_too(client, tmp_path):
    client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})
    r = client.post("/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"})
    assert r.status_code == 409


def test_a_failed_move_leaves_the_app_usable(client, tmp_path):
    """Nothing moved, so this process is still right about where the library is
    and there is no reason to make the user restart."""
    from app.core import stems_location

    occupied = tmp_path / "someone-elses"
    occupied.mkdir()
    (occupied / "holiday.jpg").write_bytes(b"\xff\xd8")

    client.post("/api/settings/stems-location", json={"path": str(occupied)})

    assert stems_location.is_relocating() is False
    assert client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).status_code == 200


def test_the_panel_shows_the_new_folder_before_the_restart(client, tmp_path):
    """After a move the stored choice and this process disagree until the
    restart. Reporting JOBS_DIR would show the folder the user just moved away
    from, which reads as the move having failed."""
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa", size=500)
    target = tmp_path / "elsewhere"
    client.post("/api/settings/stems-location", json={"path": str(target)})

    body = client.get("/api/settings/stems-location").json()

    assert body["path"] == str(target.resolve())
    assert body["restart_required"] is True
    assert body["bytes"] == 500, "size should come from where the files now are"
    assert body["is_default"] is False


def test_no_restart_is_advertised_when_nothing_moved(client):
    body = client.get("/api/settings/stems-location").json()
    assert body["restart_required"] is False


def test_a_refused_move_reopens_the_door(client, tmp_path):
    """A busy queue stops the move, and the app has to stay usable afterwards --
    the guard is closed before the busy check, so it must be reopened."""
    from app.core import stems_location

    job = Job(id="aaaaaaaaaaaa")
    job.status = "separating"
    _jobs[job.id] = job

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})

    assert r.status_code == 409
    assert stems_location.is_relocating() is False, "imports would be blocked forever"


# ── a move that fails partway, and the targets that must be refused ──────────


def _fail_on(monkeypatch, entry_name: str) -> dict[str, bool]:
    """shutil.move as the stems move calls it, failing for one entry the way a
    full disk or a file held open by another program does, while the returned
    switch's "fail" is on."""
    import shutil

    from app.core import stems_location

    real = shutil.move
    switch = {"fail": True}

    def flaky(src, dst):
        if switch["fail"] and Path(src).name == entry_name:
            raise OSError(28, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(stems_location.shutil, "move", flaky)
    return switch


def test_a_half_failed_move_names_the_entry_and_keeps_the_setting(client, tmp_path, monkeypatch):
    """The second of two songs cannot be moved. The first is already in the new
    folder and stays there (no rollback of a multi-gigabyte copy); the setting
    is not written, so this process keeps reading the old folder, and the app
    stays usable rather than waiting for a restart that would change nothing."""
    import app.core.settings as settings_mod
    from app.core import stems_location

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    _job_dir(client.jobs_dir, "bbbbbbbbbbbb")
    target = tmp_path / "elsewhere"
    _fail_on(monkeypatch, "bbbbbbbbbbbb")

    r = client.post("/api/settings/stems-location", json={"path": str(target)})

    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "bbbbbbbbbbbb" in detail and "already moved is in the new folder" in detail
    assert str(tmp_path) not in detail, "no paths in errors"
    assert (target / "aaaaaaaaaaaa").is_dir()
    assert (client.jobs_dir / "bbbbbbbbbbbb").is_dir(), "the failed entry is not lost"
    assert settings_mod.get_jobs_dir() is None
    assert stems_location.is_relocating() is False


def test_a_half_failed_move_can_be_finished_by_trying_again(client, tmp_path, monkeypatch):
    """The target now holds one of our songs, which validation accepts as a
    folder StemDeck already uses; the retry moves the rest and writes the
    setting."""
    import app.core.settings as settings_mod

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    _job_dir(client.jobs_dir, "bbbbbbbbbbbb")
    target = tmp_path / "elsewhere"
    disk = _fail_on(monkeypatch, "bbbbbbbbbbbb")
    assert (
        client.post("/api/settings/stems-location", json={"path": str(target)}).status_code == 500
    )
    disk["fail"] = False  # the disk has room again

    r = client.post("/api/settings/stems-location", json={"path": str(target)})

    assert r.status_code == 200, r.text
    assert r.json()["moved_entries"] == 1
    assert (target / "aaaaaaaaaaaa").is_dir() and (target / "bbbbbbbbbbbb").is_dir()
    assert settings_mod.get_jobs_dir() == str(target.resolve())


def test_an_unexpected_error_while_moving_is_a_500_that_reopens_the_door(
    client, tmp_path, monkeypatch
):
    import app.core.settings as settings_mod
    import app.main as main_mod
    from app.core import stems_location

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")

    def boom(_current, _target):
        raise RuntimeError("C:\\secret\\path exploded")

    monkeypatch.setattr(main_mod, "move_library", boom)

    r = client.post("/api/settings/stems-location", json={"path": str(tmp_path / "elsewhere")})

    assert r.status_code == 500
    assert r.json()["detail"] == "Could not move the stems folder"
    assert stems_location.is_relocating() is False
    assert settings_mod.get_jobs_dir() is None


def _write_registry(folder: Path, job: Job) -> str:
    import json

    text = json.dumps({"version": 1, "jobs": [job.to_record()]})
    (folder / "registry.json").write_text(text, encoding="utf-8")
    return text


def test_moving_into_another_stemdeck_library_keeps_both_registries_records(tmp_path):
    """move_library skips an entry already at the target, so moving into a
    folder with its own registry.json would leave ours behind and drop our
    favourites, Trash and tags (#798). The move is refused before anything
    moves, and both libraries stay exactly as they were."""
    from app.core import registry as _registry
    from app.core.registry import _jobs as jobs

    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    mine = _write_registry(
        current, Job(id="aaaaaaaaaaaa", status="done", title="Mine", favorite=True)
    )
    target = tmp_path / "other-library"
    _job_dir(target, "cccccccccccc")
    theirs = _write_registry(target, Job(id="cccccccccccc", status="done", title="Theirs"))

    with pytest.raises(StemsLocationError, match="already holds another StemDeck library"):
        validate_target(target, current)

    assert (current / "registry.json").read_text(encoding="utf-8") == mine
    assert (target / "registry.json").read_text(encoding="utf-8") == theirs
    assert (current / "aaaaaaaaaaaa" / "stems" / "vocals.wav").is_file()
    assert (target / "cccccccccccc" / "stems" / "vocals.wav").is_file()
    assert not (target / "aaaaaaaaaaaa").exists()
    jobs.clear()
    _registry.restore(current)
    restored = _registry.get("aaaaaaaaaaaa")
    assert restored is not None
    assert restored.favorite is True, "our favourite must still be where it was"


def test_another_librarys_user_data_store_is_refused_too(tmp_path):
    """user-data.json (#403) holds the desktop shell's records, and the move
    would skip ours the same way."""
    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    (current / "user-data.json").write_text('{"mine": 1}', encoding="utf-8")
    target = tmp_path / "other-library"
    _job_dir(target, "cccccccccccc")
    (target / "user-data.json").write_text('{"theirs": 1}', encoding="utf-8")

    with pytest.raises(StemsLocationError, match="already holds another StemDeck library"):
        validate_target(target, current)


def test_a_linked_registry_in_the_target_is_refused(tmp_path):
    """A link could point at our own registry and look identical; it is never
    taken as our own progress."""
    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    (current / "registry.json").write_text('{"jobs": []}', encoding="utf-8")
    target = tmp_path / "new"
    target.mkdir()
    try:
        (target / "registry.json").symlink_to(current / "registry.json")
    except OSError:
        pytest.skip("symlinks are not available here")

    with pytest.raises(StemsLocationError, match="already holds another StemDeck library"):
        validate_target(target, current)


def test_a_registry_copied_but_not_yet_removed_is_our_own_progress(tmp_path):
    """A cross-device move copies, then deletes the source. Interrupted between
    the two, both folders hold the same registry, and the retry must go on."""
    current = tmp_path / "old"
    _job_dir(current, "aaaaaaaaaaaa")
    text = _write_registry(current, Job(id="aaaaaaaaaaaa", status="done"))
    target = tmp_path / "new"
    _job_dir(target, "aaaaaaaaaaaa")
    (target / "registry.json").write_text(text, encoding="utf-8")

    assert validate_target(target, current) == target.resolve()


def test_a_folder_holding_another_library_is_refused_by_the_endpoint(client, tmp_path):
    import app.core.settings as settings_mod
    from app.core import stems_location

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    mine = _write_registry(client.jobs_dir, Job(id="aaaaaaaaaaaa", status="done"))
    other = tmp_path / "other-library"
    _job_dir(other, "cccccccccccc")
    theirs = _write_registry(other, Job(id="cccccccccccc", status="done"))

    r = client.post("/api/settings/stems-location", json={"path": str(other)})

    assert r.status_code == 422
    assert r.json() == {
        "detail": "That folder already holds another StemDeck library. "
        "Pick an empty folder instead."
    }
    assert (client.jobs_dir / "registry.json").read_text(encoding="utf-8") == mine
    assert (other / "registry.json").read_text(encoding="utf-8") == theirs
    assert (client.jobs_dir / "aaaaaaaaaaaa").is_dir()
    assert not (other / "aaaaaaaaaaaa").exists()
    assert settings_mod.get_jobs_dir() is None
    assert stems_location.is_relocating() is False


def test_a_move_that_failed_after_the_registry_can_still_be_finished(client, tmp_path, monkeypatch):
    """The registry moves before user-data.json, so a move that stops on the
    last entry leaves our registry.json in the target. That is this library's
    own progress, not another library, and the retry must finish the move."""
    import app.core.settings as settings_mod

    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    mine = _write_registry(client.jobs_dir, Job(id="aaaaaaaaaaaa", status="done"))
    (client.jobs_dir / "user-data.json").write_text('{"mine": 1}', encoding="utf-8")
    target = tmp_path / "elsewhere"
    disk = _fail_on(monkeypatch, "user-data.json")
    assert (
        client.post("/api/settings/stems-location", json={"path": str(target)}).status_code == 500
    )
    assert (target / "registry.json").is_file()
    assert not (client.jobs_dir / "registry.json").exists()
    disk["fail"] = False

    r = client.post("/api/settings/stems-location", json={"path": str(target)})

    assert r.status_code == 200, r.text
    assert r.json()["moved_entries"] == 1
    assert (target / "registry.json").read_text(encoding="utf-8") == mine
    assert (target / "user-data.json").read_text(encoding="utf-8") == '{"mine": 1}'
    assert list(client.jobs_dir.iterdir()) == []
    assert settings_mod.get_jobs_dir() == str(target.resolve())


def test_a_destination_inside_the_library_is_refused_and_nothing_moves(client):
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    inside = client.jobs_dir / "aaaaaaaaaaaa" / "deeper"

    r = client.post("/api/settings/stems-location", json={"path": str(inside)})

    assert r.status_code == 422
    assert "outside the current stems folder" in r.json()["detail"]
    assert (client.jobs_dir / "aaaaaaaaaaaa" / "stems" / "vocals.wav").is_file()
    assert not inside.exists()


def test_a_folder_with_our_registry_and_someone_elses_file_is_refused(client, tmp_path):
    """One stranger among our own entries is enough: the folder is not ours."""
    _job_dir(client.jobs_dir, "aaaaaaaaaaaa")
    mixed = tmp_path / "mixed"
    _job_dir(mixed, "cccccccccccc")
    (mixed / "registry.json").write_text('{"jobs": []}', encoding="utf-8")
    (mixed / "tax-return.pdf").write_bytes(b"%PDF")

    r = client.post("/api/settings/stems-location", json={"path": str(mixed)})

    assert r.status_code == 422
    assert (client.jobs_dir / "aaaaaaaaaaaa").is_dir()
    assert not (mixed / "aaaaaaaaaaaa").exists()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"path": 5}'])
def test_a_body_without_a_path_string_is_a_422(client, body):
    r = client.post(
        "/api/settings/stems-location",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_a_blank_path_is_refused_with_its_own_message(client):
    r = client.post("/api/settings/stems-location", json={"path": "   "})
    assert r.status_code == 422
    assert r.json()["detail"] == "Pick a folder to store stems in."


def test_a_folder_that_cannot_be_created_is_refused(tmp_path, monkeypatch):
    current = tmp_path / "old"
    current.mkdir()

    def denied(self, *a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(StemsLocationError, match="Cannot create that folder: Access is denied"):
        validate_target(tmp_path / "locked" / "stems", current)


def test_a_folder_that_is_not_writable_is_refused(tmp_path, monkeypatch):
    from app.core import stems_location

    current = tmp_path / "old"
    current.mkdir()
    target = tmp_path / "read-only"
    target.mkdir()
    monkeypatch.setattr(stems_location.os, "access", lambda _p, _mode: False)
    with pytest.raises(StemsLocationError, match="not writable"):
        validate_target(target, current)


def test_directory_size_skips_a_file_it_cannot_stat(tmp_path, monkeypatch):
    _job_dir(tmp_path, "aaaaaaaaaaaa", size=100)
    _job_dir(tmp_path, "bbbbbbbbbbbb", size=50)
    real_stat = Path.stat

    def stat(self, *a, **k):
        if self.parent.parent.name == "bbbbbbbbbbbb" and self.name == "vocals.wav":
            raise PermissionError(13, "locked")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    assert directory_size(tmp_path) == 100


def test_same_filesystem_assumes_the_slow_case_when_it_cannot_tell(tmp_path, monkeypatch):
    from app.core import stems_location

    assert stems_location._same_filesystem(tmp_path, tmp_path / "new") is True

    def broken(self, *a, **k):
        raise OSError("gone")

    monkeypatch.setattr(Path, "stat", broken)
    assert stems_location._same_filesystem(tmp_path, tmp_path / "new") is False
