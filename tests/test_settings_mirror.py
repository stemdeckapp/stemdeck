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


def test_mirror_holds_the_relocated_stems_folder(tmp_path, monkeypatch):
    # The reported symptom: a new install went back to the default jobs folder
    # because jobs_dir only existed inside the old install directory.
    mirror = tmp_path / "shared" / "settings.json"
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))
    chosen = tmp_path / "MyStems"
    chosen.mkdir()

    _settings.set_jobs_dir(str(chosen))

    assert json.loads(mirror.read_text(encoding="utf-8"))["jobs_dir"] == str(chosen)
