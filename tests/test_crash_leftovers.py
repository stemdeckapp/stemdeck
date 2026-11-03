"""What a killed process leaves behind is removed at the next start, and only that.

A stem fill, a decode for the vocal split, a stem written beside its final name,
an export render and an all-stems zip each write to a temp name first. A kill or
a power loss in the middle leaves that temp behind, and each can be hundreds of
MB. Every test here runs in the test's own throwaway folders.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import types
import uuid
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.models import Job
from tests.test_jobs_folder_scope import _link

HEX = uuid.uuid4().hex
DONE = "abcdefabcd01"
STOPPED = "abcdefabcd02"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry._jobs.clear()
    yield
    registry._jobs.clear()


@pytest.fixture
def caches(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    from app.api import stems as stems_mod

    mixdown = tmp_path / "cache" / "mixdown"
    click = tmp_path / "cache" / "click"
    mixdown.mkdir(parents=True)
    click.mkdir(parents=True)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", mixdown)
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", click)
    return mixdown, click


def _song(jobs: Path, job_id: str, status: str) -> list[Path]:
    """A song with everything a user keeps, registered with `status`. Returns
    every file it holds."""
    job = Job(id=job_id)
    job.status = status
    registry._jobs[job_id] = job
    stems = jobs / job_id / "stems"
    stems.mkdir(parents=True)
    kept = [
        stems / "vocals.wav",
        stems / "vocals.mp3",
        stems / "drums.mp3",
        stems / "peaks.json",
        stems / "beats.json",
        stems / ".DS_Store",
        jobs / job_id / "source.mp3",
        jobs / job_id / "metadata.json",
        jobs / job_id / "takes" / "t0000aaaa" / "take.webm",
    ]
    for path in kept:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"keep")
    return kept


def _leftovers(jobs: Path, job_id: str) -> list[Path]:
    """What each writer leaves in a song's folder when the process dies mid-write,
    named exactly as the writer names it."""
    job_dir = jobs / job_id
    stems = job_dir / "stems"
    fill = job_dir / f".fill-{HEX[:8]}"  # stem_fill.fill_missing_stems
    decode = stems / ".decode-ab_12cd3"  # stem_files.decoded_wav
    work = stems / f".vocal_split.{HEX}.tmp"  # a stem writer's work folder
    for folder in (fill, decode, work):
        folder.mkdir()
        (folder / "vocals.wav").write_bytes(b"x")
    files = [
        stems / f".vocals.{HEX}.tmp.mp3",  # stem_storage, WAV to MP3
        stems / f".vocals.mp3.{HEX}.tmp",  # stems._ensure_cached_mp3
        stems / f".vocals.wav.{HEX}.tmp",  # a stem written atomically
    ]
    for path in files:
        path.write_bytes(b"x")
    return [fill, decode, work, *files]


def test_startup_removes_crash_leftovers_and_keeps_every_song_file(
    tmp_path: Path, caches: tuple[Path, Path]
):
    from app.core.config import JOBS_DIR
    from app.main import app

    mixdown, click = caches
    kept = _song(JOBS_DIR, DONE, "done") + _song(JOBS_DIR, STOPPED, "cancelled")
    gone = _leftovers(JOBS_DIR, DONE) + _leftovers(JOBS_DIR, STOPPED)
    cache_kept = [
        mixdown / "0123abcd.wav",
        click / "4567cdef.wav",
        mixdown / ".DS_Store",
        # Written a moment ago: another server sharing the cache may be writing it.
        mixdown / f".render.{uuid.uuid4().hex}.tmp.wav",
    ]
    cache_gone = [
        mixdown / f".render.{HEX}.tmp.wav",  # stems._render_to_file, no cache entry
        mixdown / f".0123abcd.wav.{HEX}.tmp.wav",  # stems._render_to_file into the cache
        mixdown / f".0123abcd.mp3.{HEX}.tmp",  # stems._stream_ffmpeg into the cache
        mixdown / ".stemdeck_zip_k3j2h1g0.zip",  # get_all_stems_zip
        mixdown / ".zipwork-ab12cd34" / "vocals.mp3",  # _build_stems_zip
        click / f".4567cdef.wav.{HEX}.tmp",  # click_render._render_events
    ]
    long_ago = time.time() - 3600
    for path in [*cache_kept, *cache_gone]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    for path in [*cache_kept[:3], *cache_gone]:
        os.utime(path, (long_ago, long_ago))
    os.utime(mixdown / ".zipwork-ab12cd34", (long_ago, long_ago))
    # Not StemDeck's, in a shared jobs folder: a folder that is not a job, and
    # one named like a job that holds nothing StemDeck writes.
    foreign = [JOBS_DIR / name / f".fill-{HEX[:8]}" for name in ("photos", "0123456789ab")]
    for folder in foreign:
        folder.mkdir(parents=True)

    with TestClient(app):
        pass

    assert [p for p in kept + cache_kept + foreign if not p.exists()] == []
    assert [p for p in gone + cache_gone if p.exists()] == []
    assert not (mixdown / ".zipwork-ab12cd34").exists()


def test_leftover_sweep_never_follows_a_link(tmp_path: Path, caches: tuple[Path, Path]):
    from app.core.config import JOBS_DIR
    from app.pipeline.collect import sweep_crash_leftovers

    outside = tmp_path / "outside"
    (outside / "stems").mkdir(parents=True)
    temp = outside / "stems" / f".vocals.{HEX}.tmp.mp3"
    temp.write_bytes(b"x")
    _link(JOBS_DIR / DONE, outside)
    linked_cache = tmp_path / "elsewhere"
    (linked_cache / "stems").mkdir(parents=True)
    linked_temp = linked_cache / "stems" / f".drums.{HEX}.tmp.mp3"
    linked_temp.write_bytes(b"x")
    (JOBS_DIR / STOPPED).mkdir()
    _link(JOBS_DIR / STOPPED / "stems", linked_cache / "stems")

    sweep_crash_leftovers(JOBS_DIR, caches)

    assert temp.is_file()
    assert linked_temp.is_file()


# ─── The all-stems zip (F47) ──────────────────────────────────────────────


def test_zip_temp_is_removed_when_the_build_is_cancelled(
    tmp_path: Path, caches: tuple[Path, Path], monkeypatch
):
    """A request cancelled while its zip builds (the client or the server going
    away) raises CancelledError, which is not an Exception, and the build
    thread goes on writing: the temp zip was left behind. It now goes when the
    build ends, and it is built in the export cache, where the startup sweep
    finds it if the process dies instead."""
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    job = Job(id=DONE)
    job.status = "done"
    registry._jobs[DONE] = job
    (tmp_path / DONE / "stems").mkdir(parents=True)
    (tmp_path / DONE / "stems" / "vocals.wav").write_bytes(b"RIFF")
    built: list[Path] = []
    started = threading.Event()
    release = threading.Event()

    def slow_build(sources, fmt, dest, prefix, click):
        built.append(dest)
        with open(dest, "wb") as f:
            f.write(b"PK partial")
            started.set()
            assert release.wait(timeout=10)
            f.write(b" and the rest")

    monkeypatch.setattr(stems_mod, "_build_stems_zip_with_click", slow_build)

    async def cancel_mid_build() -> None:
        request = asyncio.create_task(
            stems_mod.get_all_stems_zip(
                DONE,
                fmt="wav",
                stems=None,
                click=False,
                click_mult=1.0,
                click_accent=-1,
                click_gain=0.6,
                click_groups="",
                click_sound=stems_mod.DEFAULT_CLICK_SOUND,
            )
        )
        assert await asyncio.to_thread(started.wait, 10)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        # The build is still writing: the file must outlive the request.
        assert built[0].exists()
        release.set()

    try:
        # asyncio.run returns once the build thread has ended.
        asyncio.run(cancel_mid_build())
        assert not built[0].exists()
        assert built[0].parent == caches[0]
    finally:
        for path in built:
            path.unlink(missing_ok=True)


# ─── Click renders of one cache key at once (F39) ─────────────────────────


def test_two_renders_of_one_click_both_finish(tmp_path: Path, monkeypatch):
    """Two exports rendering the same click at once wrote one shared temp name:
    the first rename took the other's file away, and that export lost its
    click. The first render here has written its file and waits to rename it
    while the second runs from start to end."""
    from app.pipeline import click_render

    first_written = threading.Event()
    second_done = threading.Event()

    class _Written:
        def __init__(self, path: str, mode: str) -> None:
            self._w = wave.open(path, mode)  # noqa: SIM115 -- closed in __exit__

        def __enter__(self):
            return self._w

        def __exit__(self, *exc) -> None:
            self._w.close()
            if not first_written.is_set():
                first_written.set()
                second_done.wait(timeout=10)

    monkeypatch.setattr(click_render, "wave", types.SimpleNamespace(open=_Written))
    dest = tmp_path / "click" / "samekey.wav"
    errors: list[BaseException] = []

    def render() -> None:
        try:
            click_render.render_click_wav(dest, [0.5, 1.0, 1.5, 2.0], [], 3.0)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=render)
    first.start()
    assert first_written.wait(timeout=10)
    render()
    second_done.set()
    first.join(timeout=10)

    assert errors == []
    with wave.open(str(dest), "rb") as w:
        assert w.getnframes() == 3 * 44100
    assert sorted(p.name for p in dest.parent.iterdir()) == ["samekey.wav"]
