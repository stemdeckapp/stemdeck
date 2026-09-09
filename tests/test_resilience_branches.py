"""The "something on disk went wrong" branches across the core modules.

Each of these is a few lines that only run when a write fails, a file is
unreadable, a folder is not writable, or a cache entry has expired. They are
scattered across five modules, so they are collected here rather than bolted
onto five existing files.

They matter more than their size suggests: every one of them is the difference
between the app degrading and the app raising in a place the caller does not
expect. A failure to persist the registry must not fail the request that
triggered it; a settings file someone hand-edited must not stop the app
starting.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_a_registry_that_cannot_be_written_does_not_fail_the_request(tmp_path, monkeypatch, caplog):
    """persist() is called at the end of nearly every mutating endpoint. Raising
    here would turn a full disk into a 500 on an operation that already
    succeeded in memory."""
    from app.core import registry

    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "replace", _boom)

    registry.persist(tmp_path)  # must not raise

    assert "could not persist registry" in caplog.text


def test_the_live_process_for_a_job_is_readable(tmp_path):
    """get_proc is what POST /cancel uses to reach an in-flight ffmpeg."""
    from app.core import registry

    class _Proc:
        pass

    proc = _Proc()
    registry.set_proc("abcdefabcdef", proc)
    try:
        assert registry.get_proc("abcdefabcdef") is proc
        assert registry.get_proc("nonexistent000") is None
    finally:
        registry.set_proc("abcdefabcdef", None)


def test_recovering_a_job_notices_an_existing_mix(tmp_path):
    """A job whose mix.wav survived is offered for download again rather than
    losing the button on a restart."""
    from app.core import registry

    job_dir = tmp_path / "abcdefabcdef"
    stems = job_dir / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "mix"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")

    registry.restore(tmp_path)

    job = registry.all_jobs().get("abcdefabcdef")
    assert job is not None
    assert job.mix_url and job.mix_url.endswith("/stems/mix.wav")


def test_a_job_with_unreadable_metadata_is_still_recovered(tmp_path):
    """The stems are the valuable part. A corrupt metadata.json costs the title
    and BPM, not the track."""
    from app.core import registry

    job_dir = tmp_path / "abcdefabcdef"
    stems = job_dir / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")
    (job_dir / "metadata.json").write_text("{truncated", encoding="utf-8")

    registry.restore(tmp_path)

    assert "abcdefabcdef" in registry.all_jobs()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_a_settings_file_that_is_not_json_does_not_stop_the_app(tmp_path, monkeypatch, caplog):
    from app.core import settings

    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    assert settings._read_json_dict(path) is None
    assert "could not read settings" in caplog.text


def test_a_settings_file_that_is_absent_is_not_an_error(tmp_path, caplog):
    """Absent and unusable are different answers, and the caller depends on
    telling them apart."""
    from app.core import settings

    assert settings._read_json_dict(tmp_path / "nope.json") is None
    assert "could not read settings" not in caplog.text


def test_clearing_the_stems_folder_removes_the_stored_choice(tmp_path, monkeypatch):
    """Reverting to the default must delete the key, not store an empty string
    that later reads as a path to nowhere."""
    from app.core import settings

    monkeypatch.setattr(settings, "_SETTINGS_PATH", tmp_path / "settings.json")

    settings.set_jobs_dir(str(tmp_path / "somewhere"))
    resolved, _persisted = settings.set_jobs_dir("")

    assert resolved is None
    assert settings.get_jobs_dir() is None


def test_a_cookies_file_that_cannot_be_read_is_refused(tmp_path, monkeypatch):
    """The message reaches the settings panel, so "not readable" has to be
    distinguishable from "not found"."""
    from app.core import settings

    monkeypatch.setattr(settings, "_SETTINGS_PATH", tmp_path / "settings.json")
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# cookies", encoding="utf-8")

    def _boom(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "open", _boom)

    with pytest.raises(ValueError, match="not readable"):
        settings.set_cookies_file(str(cookies))


# --------------------------------------------------------------------------
# preview
# --------------------------------------------------------------------------


def test_an_expired_preview_is_not_served_from_cache(monkeypatch):
    """A resolved stream URL is signed and short-lived; serving an expired one
    gives the listener a 403 instead of audio."""
    import time

    from app.pipeline import preview

    preview._clear_cache()
    preview._cache_put("https://x/1", {"url": "signed"})
    assert preview._cache_get("https://x/1") is not None

    # Captured first: a lambda that called time.monotonic() after the patch
    # would call itself.
    later = time.monotonic() + preview._TTL_SEC + 60
    monkeypatch.setattr(time, "monotonic", lambda: later)

    assert preview._cache_get("https://x/1") is None


def test_the_preview_cache_is_bounded(monkeypatch):
    """Auditioning through a long search result would otherwise keep every
    resolved format for the life of the process."""
    from app.pipeline import preview

    preview._clear_cache()
    for i in range(500):
        preview._cache_put(f"https://x/{i}", {"url": str(i)})

    assert len(preview._cache) < 500


@pytest.mark.parametrize(
    "fmt, expected, why",
    [
        ({"ext": "webm"}, "audio/webm", "a known extension wins outright"),
        ({"ext": "m4a"}, "audio/mp4", "a known extension wins outright"),
        ({"ext": "", "acodec": "opus"}, "audio/webm", "opus is served in webm"),
        ({"ext": "", "acodec": "mp4a.40.2"}, "audio/mp4", "aac is served in mp4"),
        ({"ext": "", "acodec": "aac"}, "audio/mp4", "aac by its other name"),
        ({"ext": "", "acodec": "unknown"}, "audio/mpeg", "an unknown codec falls back"),
        ({}, "audio/mpeg", "nothing at all falls back"),
    ],
)
def test_the_preview_content_type_is_derived_from_the_format(fmt, expected, why):
    """The browser picks its decoder from this header; a wrong one is silence
    with no error in the console."""
    from app.pipeline import preview

    assert preview._mime_for(fmt) == expected, why


# --------------------------------------------------------------------------
# stems_location
# --------------------------------------------------------------------------


def test_sizing_a_folder_skips_what_it_cannot_stat(tmp_path, monkeypatch):
    """This number is shown before a move to say how much will be copied. One
    unreadable file must not sink the whole answer."""
    from app.core.stems_location import directory_size

    (tmp_path / "a.bin").write_bytes(b"0" * 100)
    (tmp_path / "b.bin").write_bytes(b"0" * 100)

    real_stat = Path.stat
    calls = {"n": 0}

    def _flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("gone")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", _flaky)

    assert directory_size(tmp_path) == 100


def test_an_empty_target_is_refused_before_anything_is_touched(tmp_path):
    from app.core.stems_location import StemsLocationError, validate_target

    with pytest.raises(StemsLocationError, match="Pick a folder"):
        validate_target(Path("   "), tmp_path)


def test_a_target_that_cannot_be_created_says_so(tmp_path, monkeypatch):
    from app.core.stems_location import StemsLocationError, validate_target

    def _boom(self, *a, **kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", _boom)

    with pytest.raises(StemsLocationError, match="Cannot create that folder"):
        validate_target(tmp_path / "new", tmp_path / "current")


def test_a_target_that_is_not_writable_is_refused(tmp_path, monkeypatch):
    """Discovered before the move rather than partway through it."""
    import os

    from app.core.stems_location import StemsLocationError, validate_target

    target = tmp_path / "new"
    target.mkdir()
    monkeypatch.setattr(os, "access", lambda p, mode: False)

    with pytest.raises(StemsLocationError, match="not writable"):
        validate_target(target, tmp_path / "current")


def test_a_move_that_fails_names_the_entry_that_stopped_it(tmp_path, monkeypatch):
    """Whatever already moved stays moved, so the message has to say where the
    library now is."""
    import shutil

    from app.core.stems_location import StemsLocationError, move_library

    current = tmp_path / "old"
    current.mkdir()
    (current / "abcdefabcdef").mkdir()
    target = tmp_path / "new"

    def _boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "move", _boom)

    with pytest.raises(StemsLocationError) as exc:
        move_library(current, target)

    assert "abcdefabcdef" in str(exc.value)
    assert "already moved is in the new folder" in str(exc.value)


def test_a_filesystem_comparison_that_fails_assumes_the_slow_path(tmp_path, monkeypatch):
    """Only used to set expectations in the UI, so an unanswerable question is
    answered with "assume the copy"."""
    from app.core.stems_location import _same_filesystem

    def _boom(self, **kw):
        raise OSError("gone")

    monkeypatch.setattr(Path, "stat", _boom)

    assert _same_filesystem(tmp_path, tmp_path / "other") is False


def test_moving_from_a_library_that_does_not_exist_yet_is_a_no_op(tmp_path):
    """A first run has nothing to move; the setting change still has to work."""
    from app.core.stems_location import move_library

    result = move_library(tmp_path / "never-existed", tmp_path / "new")

    assert result.moved_entries == 0
    assert result.bytes_moved == 0


def test_an_entry_already_at_the_target_is_skipped(tmp_path):
    """A previous, interrupted attempt already brought it over; moving it again
    would fail on the existing destination."""
    from app.core.stems_location import move_library

    current = tmp_path / "old"
    (current / "abcdefabcdef").mkdir(parents=True)
    (current / "abcdefabcdef" / "x.wav").write_bytes(b"new")
    target = tmp_path / "new"
    (target / "abcdefabcdef").mkdir(parents=True)
    (target / "abcdefabcdef" / "x.wav").write_bytes(b"already here")

    result = move_library(current, target)

    assert result.moved_entries == 0
    assert (target / "abcdefabcdef" / "x.wav").read_bytes() == b"already here"


# --------------------------------------------------------------------------
# playlist endpoints
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", ["/api/playlist/preview", "/api/playlist"])
def test_a_playlist_body_that_is_not_json_is_refused(client, path):
    r = client.post(path, content=b"<xml/>", headers={"content-type": "application/json"})

    assert r.status_code == 422
    assert "Invalid JSON" in r.json()["detail"]


@pytest.mark.parametrize("path", ["/api/playlist/preview", "/api/playlist"])
def test_a_playlist_body_missing_its_url_is_refused(client, path):
    r = client.post(path, json={"stems": ["vocals"]})

    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/playlist/preview", "/api/playlist"])
def test_a_playlist_that_cannot_be_read_is_a_502(client, monkeypatch, path):
    """yt-dlp failing on a private or deleted playlist is an upstream problem,
    not a bad request -- and the reason must not be echoed to the client."""
    import app.api.playlist as pl

    def _boom(url, limit):
        raise RuntimeError("HTTP Error 404: /internal/path/leak")

    monkeypatch.setattr(pl, "expand_playlist", _boom)

    r = client.post(path, json={"url": "https://www.youtube.com/playlist?list=PL1234567890"})

    assert r.status_code == 502
    assert r.json()["detail"] == "Could not read that playlist"
    assert "internal/path/leak" not in r.text
