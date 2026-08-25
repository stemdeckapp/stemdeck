"""The per-user settings copy that survives extracting a new package (#421).

A Windows portable package keeps its data in `<app>/data`, so settings.json
lives inside the install directory. Upgrading by extracting the new zip to a
fresh folder started that install with no settings at all -- stems location,
port, compute device, quality and language silently back to defaults, and a
relocated library looking empty.

The desktop shell already restored from a per-user copy in `ensure_workspace`;
nothing had written it since the data directory moved. These cover the write
half.
"""

from __future__ import annotations

import json

from app.core import settings as _settings


def test_settings_are_mirrored_when_the_shell_asks_for_it(tmp_path, monkeypatch):
    mirror = tmp_path / "shared" / "settings.json"
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))

    _settings.set_max_duration_sec(11 * 60)

    assert mirror.is_file(), "a fresh install has nothing to restore from without this"
    assert json.loads(mirror.read_text(encoding="utf-8"))["max_duration_sec"] == 11 * 60


def test_the_mirror_tracks_later_changes(tmp_path, monkeypatch):
    mirror = tmp_path / "shared" / "settings.json"
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))

    _settings.set_max_duration_sec(5 * 60)
    _settings.set_max_duration_sec(9 * 60)

    # Restoring a stale copy would quietly hand back a setting the user had
    # already changed, which is the same class of bug as losing it.
    assert json.loads(mirror.read_text(encoding="utf-8"))["max_duration_sec"] == 9 * 60


def test_no_mirror_is_written_when_the_shell_does_not_ask(tmp_path, monkeypatch):
    # Docker and a source checkout keep their data outside the install already,
    # so there is nothing to preserve and nothing should be created.
    monkeypatch.delenv("STEMDECK_SETTINGS_MIRROR", raising=False)
    stray = tmp_path / "shared"

    _settings.set_max_duration_sec(7 * 60)

    assert not stray.exists()


def test_a_failing_mirror_never_fails_the_setting(tmp_path, monkeypatch):
    # The mirror is a redundant copy. If it cannot be written -- read-only disk,
    # a path the user cannot reach -- the setting the user just changed must
    # still be saved and reported as saved.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(blocker / "nested" / "settings.json"))

    assert _settings.set_max_duration_sec(6 * 60) is not None
    assert _settings.get_max_duration_sec() == 6 * 60
    assert (
        json.loads(_settings._SETTINGS_PATH.read_text(encoding="utf-8"))["max_duration_sec"]
        == 6 * 60
    )


def test_existing_settings_are_mirrored_without_waiting_for_a_change(tmp_path, monkeypatch):
    # Someone who relocated their stems folder in an earlier release and never
    # opens Settings again would never trigger a save, so mirroring on save
    # alone would leave them exposed on their next install.
    _settings._SETTINGS_PATH.write_text(
        json.dumps({"jobs_dir": str(tmp_path / "MyStems")}), encoding="utf-8"
    )
    _settings._state = None  # a fresh process, reading what is already on disk
    mirror = tmp_path / "shared" / "settings.json"
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))

    _settings.get_jobs_dir()

    assert mirror.is_file()
    assert json.loads(mirror.read_text(encoding="utf-8"))["jobs_dir"] == str(tmp_path / "MyStems")


def test_a_first_run_never_clobbers_an_existing_mirror(tmp_path, monkeypatch):
    # The restore is what seeds a new install, and it happens before the backend
    # starts. If a run with no settings of its own overwrote the copy with its
    # defaults, it would destroy the very thing being preserved.
    mirror = tmp_path / "shared" / "settings.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"jobs_dir": str(tmp_path / "MyStems")}), encoding="utf-8")
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))
    _settings._state = None
    assert not _settings._SETTINGS_PATH.exists()

    _settings.get_jobs_dir()

    assert json.loads(mirror.read_text(encoding="utf-8"))["jobs_dir"] == str(tmp_path / "MyStems")


def test_mirror_holds_the_relocated_stems_folder(tmp_path, monkeypatch):
    # The reported symptom: a new install went back to the default jobs folder
    # because jobs_dir only existed inside the old install directory.
    mirror = tmp_path / "shared" / "settings.json"
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))
    chosen = tmp_path / "MyStems"
    chosen.mkdir()

    _settings.set_jobs_dir(str(chosen))

    assert json.loads(mirror.read_text(encoding="utf-8"))["jobs_dir"] == str(chosen)


def test_the_duration_ceiling_is_published_not_duplicated():
    """The UI used to keep its own copy of the max-track-length ceiling. It
    went stale at 20 minutes against a real ceiling of 60, so typing 60 was
    silently clamped to 20 and the field snapped back with no explanation.

    The cure was to stop having a second copy, so the bound has to stay in the
    payload for the client to read.
    """
    from fastapi.testclient import TestClient

    from app.core.settings import DURATION_MAX_SEC, DURATION_MIN_SEC
    from app.main import app

    with TestClient(app) as c:
        body = c.get("/api/settings").json()
    assert body["max_duration_max_sec"] == DURATION_MAX_SEC
    assert body["max_duration_min_sec"] == DURATION_MIN_SEC


def test_a_value_at_the_ceiling_is_kept(tmp_path, monkeypatch):
    """60 minutes is a legal setting. It was reachable through the API all
    along; only the client refused to send it."""
    mirror = tmp_path / "settings.json"
    monkeypatch.setattr(_settings, "_SETTINGS_PATH", mirror)
    monkeypatch.setattr(_settings, "_state", None)
    assert _settings.set_max_duration_sec(3600) == 3600
    assert _settings.get_max_duration_sec() == 3600


def test_beyond_the_ceiling_still_clamps(tmp_path, monkeypatch):
    mirror = tmp_path / "settings.json"
    monkeypatch.setattr(_settings, "_SETTINGS_PATH", mirror)
    monkeypatch.setattr(_settings, "_state", None)
    assert _settings.set_max_duration_sec(99 * 60) == 3600
    assert _settings.set_max_duration_sec(1) == 60
