"""Settings must survive a write that does not finish (#509).

`_save()` used to call `write_text`, which truncates first and writes second.
A process that died in between left a file that existed and did not parse, and
`_load()` returned `{}` for it -- indistinguishable from a first run. The next
`set_*()` then persisted a single key over both settings.json and the mirror
that exists to protect it, so a real user lost `port` and `allow_network` from
both copies with only a warning in the log.
"""

from __future__ import annotations

import json
import os
import pathlib

from app.core import settings as _settings


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_an_absent_file_is_a_first_run(tmp_path):
    assert not _settings._SETTINGS_PATH.exists()
    assert _settings._load() == {}


def test_an_unreadable_file_is_not_mistaken_for_a_first_run(tmp_path, monkeypatch):
    # The distinction is the whole bug: defaults are right for a first run and
    # catastrophic for a settings file we merely failed to read.
    mirror = tmp_path / "shared" / "settings.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"port": 8081, "allow_network": True}), encoding="utf-8")
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))

    _settings._SETTINGS_PATH.write_text('{"port": 80', encoding="utf-8")  # torn write

    assert _settings._load() == {"port": 8081, "allow_network": True}


def test_an_unreadable_file_is_kept_for_diagnosis(tmp_path):
    _settings._SETTINGS_PATH.write_text('{"port": 80', encoding="utf-8")

    _settings._load()

    corrupt = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(corrupt) == 1, "the unreadable bytes must not be silently destroyed"
    assert corrupt[0].read_text(encoding="utf-8") == '{"port": 80'


def test_recovered_settings_are_written_back_immediately(tmp_path, monkeypatch):
    # Recovering only into memory would last until the next start, which would
    # read a now-absent primary and fall back to defaults again.
    mirror = tmp_path / "shared" / "settings.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"port": 8081}), encoding="utf-8")
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))
    _settings._SETTINGS_PATH.write_text("", encoding="utf-8")  # truncated to nothing

    _settings._load()

    assert _read(_settings._SETTINGS_PATH) == {"port": 8081}


def test_no_mirror_and_a_corrupt_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("STEMDECK_SETTINGS_MIRROR", raising=False)
    _settings._SETTINGS_PATH.write_text("not json at all", encoding="utf-8")

    assert _settings._load() == {}


def test_a_non_object_settings_file_is_treated_as_unusable(tmp_path, monkeypatch):
    # Valid JSON, wrong shape. Returning it would make every later .get() raise.
    monkeypatch.delenv("STEMDECK_SETTINGS_MIRROR", raising=False)
    _settings._SETTINGS_PATH.write_text("[1, 2, 3]", encoding="utf-8")

    assert _settings._load() == {}
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_a_failed_write_leaves_the_previous_settings_intact(tmp_path, monkeypatch):
    # The heart of it: temp + replace means an interrupted write cannot truncate
    # what was already there.
    #
    # Only the temp write is made to fail, and monkeypatch.undo() is deliberately
    # not used: the same monkeypatch instance carries conftest's _SETTINGS_PATH
    # isolation, so undoing here would point the assertion at the developer's
    # real settings file.
    path = _settings._SETTINGS_PATH
    path.write_text(json.dumps({"port": 8081, "allow_network": True}), encoding="utf-8")

    def _boom(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", _boom)

    assert _settings._atomic_write_json(path, {"port": 9000}) is False
    assert _read(path) == {"port": 8081, "allow_network": True}
    assert not list(tmp_path.glob("*.tmp")), "a failed write must not leave a temp file"


def test_settings_reach_the_disk_before_they_replace_the_old_file(monkeypatch):
    # A library move writes the new jobs_dir here and then deletes the originals.
    # Replaced before its bytes were on the disk, a power cut could leave a torn
    # settings.json, and the next start would fall back to the empty default.
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def replace(src, dst):
        calls.append(("replace", pathlib.Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    assert _settings._atomic_write_json(_settings._SETTINGS_PATH, {"port": 8081}) is True

    assert calls[:2] == ["fsync", ("replace", "settings.json")]
    if os.name != "nt":
        assert calls[2:] == ["fsync"], "the rename itself must reach the disk too"


def test_atomic_write_leaves_no_temp_files_behind(tmp_path):
    assert _settings._atomic_write_json(_settings._SETTINGS_PATH, {"port": 8081}) is True

    assert _read(_settings._SETTINGS_PATH) == {"port": 8081}
    assert not list(tmp_path.glob("*.tmp")), "temp files must not accumulate next to settings"
