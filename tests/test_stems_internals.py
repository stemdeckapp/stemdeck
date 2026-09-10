"""The export-path helpers inside app/api/stems.py.

Measured under Python 3.12 (sys.monitoring), where async endpoint bodies are
traced correctly, these were the real remaining gaps in the largest API module:
the on-disk mp3 cache that mobile playback depends on, the rubberband probe
that decides whether transpose is offered at all, the mixdown cache's eviction,
and the beat-grid read an exported click track is built from.

All four are failure-shaped: each one has a "something went wrong, degrade
rather than crash" branch, and none of them was exercised.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import app.api.stems as stems_mod


def _wav(path: Path, seconds: float = 0.3, rate: int = 8000) -> Path:
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), rate)
    return path


# --------------------------------------------------------------------------
# _ensure_cached_mp3 -- what mobile playback fetches
# --------------------------------------------------------------------------


async def test_a_stem_is_transcoded_to_a_sibling_mp3(tmp_path):
    src = _wav(tmp_path / "vocals.wav")

    dest = await stems_mod._ensure_cached_mp3(src)

    assert dest == tmp_path / "vocals.mp3"
    assert dest.is_file() and dest.stat().st_size > 0
    # Written atomically, so a concurrent fetch can never see a partial file.
    assert not list(tmp_path.glob(".*.tmp"))


async def test_a_cached_mp3_is_reused_rather_than_re_encoded(tmp_path, monkeypatch):
    """Re-encoding a full song per request is the slow part of loading a track
    on mobile -- about 3 s per stem, six in parallel."""
    src = _wav(tmp_path / "vocals.wav")
    await stems_mod._ensure_cached_mp3(src)

    def _never(*a, **kw):
        raise AssertionError("ffmpeg was run for an already-cached stem")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _never)

    assert await stems_mod._ensure_cached_mp3(src) == tmp_path / "vocals.mp3"


async def test_a_stale_cached_mp3_is_re_encoded(tmp_path):
    """The vocal split rewrites stems in place. An mp3 older than its source is
    the previous take, and serving it would play the wrong audio."""
    import os

    src = _wav(tmp_path / "vocals.wav")
    dest = await stems_mod._ensure_cached_mp3(src)
    dest.write_bytes(b"stale")
    os.utime(dest, (1, 1))  # older than the source

    await stems_mod._ensure_cached_mp3(src)

    assert dest.read_bytes() != b"stale"


async def test_a_transcode_that_fails_is_a_500_and_leaves_no_temp_file(tmp_path):
    from fastapi import HTTPException

    src = tmp_path / "not-audio.wav"
    src.write_bytes(b"this is not a wav")

    with pytest.raises(HTTPException) as exc:
        await stems_mod._ensure_cached_mp3(src)

    assert exc.value.status_code == 500
    assert not (tmp_path / "not-audio.mp3").exists()
    assert not list(tmp_path.glob(".*.tmp")), "a failed transcode left a temp file behind"


async def test_a_transcode_that_overruns_its_timeout_is_a_504(tmp_path, monkeypatch):
    """The timeout is the only bound on a hung encoder, and a request left
    waiting on it holds a connection open for the full TIMEOUT_FFMPEG."""
    from fastapi import HTTPException

    src = _wav(tmp_path / "vocals.wav")

    async def _slow(awaitable, timeout=None):
        # Close what we are refusing to await; leaving the communicate()
        # coroutine unstarted is a RuntimeWarning at the next collection.
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", _slow)

    with pytest.raises(HTTPException) as exc:
        await stems_mod._ensure_cached_mp3(src)

    assert exc.value.status_code == 504
    assert not list(tmp_path.glob(".*.tmp")), "a timed-out transcode left a temp file behind"


# --------------------------------------------------------------------------
# _rubberband_available -- whether transpose is offered at all
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_rubberband_probe():
    """The result is cached in a module global, so one test's answer would
    otherwise decide every later test's."""
    stems_mod._rubberband_cached = None
    yield
    stems_mod._rubberband_cached = None


def test_a_build_carrying_the_filter_can_transpose(monkeypatch):
    class _Probe:
        stdout = "Filters:\n ... rubberband       A->A       Apply pitch shifting.\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Probe())

    assert stems_mod._rubberband_available() is True


def test_a_build_without_the_filter_cannot(monkeypatch):
    """The bundled macOS build is exactly this case, which is why it is probed
    rather than assumed."""

    class _Probe:
        stdout = "Filters:\n ... atempo       A->A       Adjust audio tempo.\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Probe())

    assert stems_mod._rubberband_available() is False


def test_a_similarly_named_filter_is_not_mistaken_for_it(monkeypatch):
    """The probe matches on a word boundary; a substring match would claim
    support this build does not have and export the wrong algorithm."""

    class _Probe:
        stdout = "Filters:\n ... rubberbanding_thing   A->A\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Probe())

    assert stems_mod._rubberband_available() is False


@pytest.mark.parametrize(
    "boom",
    [OSError("no ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 10), subprocess.SubprocessError()],
)
def test_a_probe_that_cannot_run_reads_as_no_support(monkeypatch, boom):
    """An export that silently used a different algorithm would be worse than
    one that says it cannot transpose."""

    def _raise(*a, **kw):
        raise boom

    monkeypatch.setattr(subprocess, "run", _raise)

    assert stems_mod._rubberband_available() is False


def test_the_probe_runs_once_and_is_remembered(monkeypatch):
    """It shells out to ffmpeg; doing that per export request would cost every
    transpose an extra process spawn."""
    calls = {"n": 0}

    class _Probe:
        stdout = "rubberband"

    def _run(*a, **kw):
        calls["n"] += 1
        return _Probe()

    monkeypatch.setattr(subprocess, "run", _run)

    assert stems_mod._rubberband_available() is True
    assert stems_mod._rubberband_available() is True
    assert calls["n"] == 1


def test_a_negative_answer_is_remembered_too(monkeypatch):
    """A None-vs-False mix-up here would re-probe on every request for exactly
    the builds that cannot transpose."""
    calls = {"n": 0}

    def _run(*a, **kw):
        calls["n"] += 1
        raise OSError("no ffmpeg")

    monkeypatch.setattr(subprocess, "run", _run)

    assert stems_mod._rubberband_available() is False
    assert stems_mod._rubberband_available() is False
    assert calls["n"] == 1


# --------------------------------------------------------------------------
# _prune_mixdown_cache
# --------------------------------------------------------------------------


def _entry(cache: Path, name: str, size: int, mtime: float) -> Path:
    import os

    p = cache / name
    p.write_bytes(b"\0" * size)
    os.utime(p, (mtime, mtime))
    return p


def test_the_cache_evicts_the_oldest_entries_first(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(25):
        _entry(cache, f"mix{i:02d}.wav", 10, 1000 + i)

    stems_mod._prune_mixdown_cache(cache)

    left = sorted(p.name for p in cache.iterdir())
    assert len(left) <= stems_mod._MIXDOWN_CACHE_MAX_FILES
    # The survivors are the newest ones.
    assert "mix24.wav" in left
    assert "mix00.wav" not in left


def test_the_file_being_served_is_never_evicted(tmp_path, monkeypatch):
    """The render the caller is about to hand to FileResponse outranks the
    budget -- deleting it produced a path that no longer existed (#482)."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 1)
    cache = tmp_path / "cache"
    cache.mkdir()
    keep = _entry(cache, "fresh.wav", 10, 2000)
    _entry(cache, "old.wav", 10, 1000)

    stems_mod._prune_mixdown_cache(cache, keep=keep)

    assert keep.is_file()


def test_an_oversized_render_evicts_everything_else_and_stops(tmp_path, monkeypatch):
    """A single render larger than the whole budget used to delete itself. It
    now empties the cache around it and leaves the directory one file over
    budget, which is the intended trade."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_BYTES", 100)
    cache = tmp_path / "cache"
    cache.mkdir()
    keep = _entry(cache, "huge.wav", 500, 2000)
    _entry(cache, "small.wav", 10, 1000)

    stems_mod._prune_mixdown_cache(cache, keep=keep)

    assert keep.is_file()
    assert not (cache / "small.wav").exists()


def test_pruning_a_directory_it_cannot_read_gives_up_quietly(tmp_path):
    """Pruning runs on the way to serving a render. A cache directory that
    vanished must not fail the request that was about to use it."""
    stems_mod._prune_mixdown_cache(tmp_path / "not-there")  # must not raise


def test_an_entry_that_cannot_be_removed_does_not_stop_the_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 1)
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(4):
        _entry(cache, f"mix{i}.wav", 10, 1000 + i)

    real_unlink = Path.unlink
    calls = {"n": 0}

    def _flaky(self, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("in use")
        return real_unlink(self, **kw)

    monkeypatch.setattr(Path, "unlink", _flaky)

    stems_mod._prune_mixdown_cache(cache)  # must not raise

    monkeypatch.undo()
    # It carried on past the one it could not remove.
    assert calls["n"] > 1


def test_dotfiles_are_left_alone(tmp_path, monkeypatch):
    """In-flight temp files are dotted; evicting one would corrupt the render
    that is still writing it."""
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 1)
    cache = tmp_path / "cache"
    cache.mkdir()
    partial = _entry(cache, ".mix.tmp", 10, 500)
    for i in range(3):
        _entry(cache, f"mix{i}.wav", 10, 1000 + i)

    stems_mod._prune_mixdown_cache(cache)

    assert partial.is_file()


# --------------------------------------------------------------------------
# _read_beat_grid -- what an exported click track is built from
# --------------------------------------------------------------------------


@pytest.fixture
def grid_job(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    return stems


def test_the_detected_grid_is_read_when_there_are_no_edits(grid_job):
    (grid_job / "beats.json").write_text(
        json.dumps({"beats": [0.0, 0.5], "bars": [{"beat": 0, "beats_per_bar": 4}]}),
        encoding="utf-8",
    )

    grid = stems_mod._read_beat_grid("abcdefabcdef")

    assert grid["beats"] == [0.0, 0.5]


def test_user_edits_win_over_the_detected_grid(grid_job):
    """Export has to click to what the user sees, or the editor and the
    exported track disagree."""
    (grid_job / "beats.json").write_text(
        json.dumps({"beats": [0.0, 0.5], "bars": [], "onsets": [0.1]}), encoding="utf-8"
    )
    (grid_job / "beats.user.json").write_text(
        json.dumps({"beats": [1.0, 2.0], "bars": [{"beat": 0, "beats_per_bar": 3}]}),
        encoding="utf-8",
    )

    grid = stems_mod._read_beat_grid("abcdefabcdef")

    assert grid["beats"] == [1.0, 2.0]
    assert grid["bars"] == [{"beat": 0, "beats_per_bar": 3}]
    # The computed onsets survive: editing never changes them.
    assert grid["onsets"] == [0.1]


def test_an_empty_edit_list_does_not_replace_the_detected_grid(grid_job):
    """An edit file with no beats is a cleared editor, not an instruction to
    export a click track with no clicks."""
    (grid_job / "beats.json").write_text(json.dumps({"beats": [0.0, 0.5]}), encoding="utf-8")
    (grid_job / "beats.user.json").write_text(json.dumps({"beats": []}), encoding="utf-8")

    assert stems_mod._read_beat_grid("abcdefabcdef")["beats"] == [0.0, 0.5]


def test_unreadable_edits_fall_back_and_say_so(grid_job, caplog):
    """Silently ignoring this is how a user ends up asking why their grid edits
    disappeared."""
    (grid_job / "beats.json").write_text(json.dumps({"beats": [0.0, 0.5]}), encoding="utf-8")
    (grid_job / "beats.user.json").write_text("{truncated", encoding="utf-8")

    grid = stems_mod._read_beat_grid("abcdefabcdef")

    assert grid["beats"] == [0.0, 0.5]
    assert "ignoring unreadable beat edits" in caplog.text


def test_a_corrupt_detected_grid_reads_as_no_grid(grid_job):
    (grid_job / "beats.json").write_text("{truncated", encoding="utf-8")

    assert stems_mod._read_beat_grid("abcdefabcdef") is None


def test_a_job_with_no_grid_at_all_reads_as_none(grid_job):
    assert stems_mod._read_beat_grid("abcdefabcdef") is None


def test_a_job_id_that_escapes_the_library_reads_as_none(tmp_path, monkeypatch):
    """Same containment check as everywhere else: a grid outside JOBS_DIR is
    never read, whatever is in it."""
    library = tmp_path / "jobs"
    library.mkdir()
    outside = tmp_path / "outside"
    (outside / "stems").mkdir(parents=True)
    (outside / "stems" / "beats.json").write_text(json.dumps({"beats": [1.0]}), encoding="utf-8")
    (library / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(stems_mod, "JOBS_DIR", library)

    assert stems_mod._read_beat_grid("escape") is None
