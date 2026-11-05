"""A song folder is rebuilt as "done" only when its separation finished.

restore() rebuilds a song from its folder when the registry says it was still
running, or has no entry for it. It used to take any folder holding one stem
for a finished song, so a process killed part way through the pipeline came
back as a "done" song with stems, original.wav or mix.wav missing. The last
thing a separation writes is now a marker in stems/, after every stem file is
on the disk, and only a folder that has it, or a song made before the marker
existed (it has metadata.json, written once a song was done), is rebuilt.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import registry as _registry
from app.core.config import STEM_NAMES
from app.core.models import Job
from app.pipeline import runner, stem_storage
from app.pipeline.vocal_split import _move_in

SONG = "c0ffee5e1ec7"


class _Killed(BaseException):
    """The process dying: no `except Exception` on the way out sees it."""


@pytest.fixture(autouse=True)
def _empty_registry():
    _registry._jobs.clear()
    _registry._pending_resume.clear()
    yield
    _registry._jobs.clear()
    _registry._pending_resume.clear()


def _fake_separate(job: Job, source: Path, job_dir: Path) -> Path:
    out = job_dir / "htdemucs_6s" / "source"
    out.mkdir(parents=True)
    for name in STEM_NAMES:
        (out / f"{name}.wav").write_bytes(b"RIFF")
    return out


def _fake_ffmpeg(job: Job, cmd: list[str]) -> bool:
    Path(cmd[-1]).write_bytes(b"RIFF")
    return True


def _kill(*args, **kwargs):
    raise _Killed


def _dies_on_second_call(target: str, real):
    calls: list[tuple] = []

    def call(*args, **kwargs):
        if calls:
            raise _Killed
        calls.append(args)
        return real(*args, **kwargs)

    return patch(target, call)


def _mp3_storage_dies_on_second_call(target: str, real) -> contextlib.ExitStack:
    """The real MP3 storage step, with `target` dying on its second call
    inside it. Encoding and its check are stubbed: what is under test is the
    order the files move in."""

    def encode(job, wav, tmp, kbps):
        tmp.write_bytes(b"ID3")

    def store(job, job_dir):
        with _dies_on_second_call(target, real):
            stem_storage.store_stems(job, job_dir)

    stack = contextlib.ExitStack()
    for p in (
        patch.object(runner, "store_stems", store),
        patch.object(stem_storage, "get_stem_format", return_value="mp3"),
        patch.object(stem_storage, "get_stem_mp3_kbps", return_value=128),
        patch.object(stem_storage, "_encode", encode),
        patch.object(stem_storage, "verify_mp3"),
    ):
        stack.enter_context(p)
    return stack


def _kill_after(target: str):
    real = getattr(runner, target)

    def run_then_die(*args, **kwargs):
        real(*args, **kwargs)
        raise _Killed

    return patch.object(runner, target, run_then_die)


# Each step of the pipeline that writes into stems/, and the gap after the last.
KILLS = {
    "collect, one stem moved": lambda: _dies_on_second_call(
        "app.pipeline.collect.shutil.move", shutil.move
    ),
    "before original.wav": lambda: patch.object(runner, "make_original_track", _kill),
    "before mix.wav": lambda: patch.object(runner, "make_selected_mix", _kill),
    "after mix.wav": lambda: patch.object(runner, "compute_stem_peaks", _kill),
    "before stem storage": lambda: patch.object(runner, "store_stems", _kill),
    "stem storage, one MP3 moved in": lambda: _mp3_storage_dies_on_second_call(
        "app.pipeline.stem_storage.os.replace", os.replace
    ),
    "stem storage, one WAV deleted": lambda: _mp3_storage_dies_on_second_call(
        "pathlib.Path.unlink", Path.unlink
    ),
    "after stem storage": lambda: _kill_after("_store_stems"),
}


def _run(jobs_dir: Path, kill=None) -> Job:
    """An upload run through the real pipeline steps that write stems/, with
    the registry saved as the app saves it when the job starts."""
    job = Job(
        id=SONG,
        status="separating",
        title="Song",
        source_url="local:Song",
        selected_stems=["vocals", "drums"],
        auto_lyrics=False,
    )
    _registry._jobs[job.id] = job
    _registry.persist(jobs_dir)
    job_dir = jobs_dir / SONG
    job_dir.mkdir()
    source = job_dir / "source.wav"
    source.write_bytes(b"RIFF")
    with (
        patch.object(runner, "_prepare_local_source", return_value=source),
        patch.object(runner.IdentifyLookup, "start", return_value=None),
        patch.object(runner, "analyze"),
        patch.object(runner, "separate", _fake_separate),
        patch.object(runner, "refine_key_from_stems"),
        patch("app.pipeline.collect._run_ffmpeg", _fake_ffmpeg),
        patch.object(runner, "compute_stem_peaks", return_value={}),
        patch.object(runner, "compute_beat_grid"),
        patch.object(runner, "store_stems"),
    ):
        if kill is None:
            runner._run_local_blocking(job, source, job_dir)
        else:
            with kill(), pytest.raises(_Killed):
                runner._run_local_blocking(job, source, job_dir)
    return job


def _restart(jobs_dir: Path, *, registry_lost: bool) -> Job | None:
    _registry._jobs.clear()
    _registry._pending_resume.clear()
    if registry_lost:
        _registry.registry_path(jobs_dir).unlink()
    _registry.restore(jobs_dir)
    return _registry.get(SONG)


@pytest.mark.parametrize("registry_lost", [False, True], ids=["registry kept", "registry lost"])
@pytest.mark.parametrize("step", list(KILLS))
def test_a_song_killed_part_way_is_never_rebuilt_as_done(tmp_path: Path, step, registry_lost):
    _run(tmp_path, KILLS[step])
    assert (tmp_path / SONG / "stems").is_dir(), "the kill left stems behind"

    job = _restart(tmp_path, registry_lost=registry_lost)

    assert job is not None and job.status != "done"
    if registry_lost:
        # Kept as the upload it is, so Extract can run it again.
        assert job.status == "stopped"
    else:
        assert job.status == "queued"
        assert _registry.take_pending_resume() == [SONG]
    assert (tmp_path / SONG / "source.wav").is_file()


@pytest.mark.parametrize("registry_lost", [False, True], ids=["registry kept", "registry lost"])
def test_a_song_whose_separation_finished_is_rebuilt_as_done(tmp_path: Path, registry_lost):
    """The process died after the last write, before "done" was saved."""
    _run(tmp_path)

    job = _restart(tmp_path, registry_lost=registry_lost)

    assert job is not None and job.status == "done"
    assert {s["name"] for s in job.stems} == {"original", *STEM_NAMES}
    assert job.mix_url == f"/api/jobs/{SONG}/stems/mix.wav"
    assert _registry.take_pending_resume() == []


def test_a_marker_that_cannot_be_written_does_not_fail_the_song(tmp_path: Path, caplog):
    """Failing the job would delete stems that are whole. The song is then
    rebuilt by its metadata.json, as a song made before the marker is."""
    with patch.object(runner, "mark_stems_complete", side_effect=OSError("disk full")):
        job = _run(tmp_path)
    assert "could not mark the stems complete" in caplog.text
    assert not (tmp_path / SONG / "stems" / _registry.STEMS_COMPLETE).exists()
    runner._write_metadata(job, tmp_path / SONG)  # what the runner does once done

    rebuilt = _restart(tmp_path, registry_lost=True)

    assert rebuilt is not None and rebuilt.status == "done"
    assert rebuilt.title == "Song"


def test_a_kill_between_the_two_split_renames_leaves_the_song_unsplit(tmp_path: Path):
    """The lead/backing pair moves in with two renames. A song rebuilt with
    only one of them would list a lead without its backing."""
    _run(tmp_path)
    stems_dir = tmp_path / SONG / "stems"
    (tmp_path / SONG / "metadata.json").write_text(json.dumps({"title": "Song"}), "utf-8")
    work = stems_dir / ".vocal_split.0123.tmp"
    work.mkdir()
    for name in ("lead_vocals", "backing_vocals"):
        (work / f"{name}.wav").write_bytes(b"RIFF")
    real = os.replace
    renames: list[str] = []

    def replace(src, dst):
        if renames:
            raise _Killed
        renames.append(str(dst))
        return real(src, dst)

    with patch("app.pipeline.vocal_split.os.replace", replace), pytest.raises(_Killed):
        _move_in(work, stems_dir, ["lead_vocals", "backing_vocals"])
    assert (stems_dir / "lead_vocals.wav").is_file()

    job = _restart(tmp_path, registry_lost=True)

    assert job is not None and job.status == "done"
    assert job.vocal_split == "none"
    assert not {"lead_vocals", "backing_vocals"} & {s["name"] for s in job.stems}


def test_a_song_made_before_the_marker_gets_it_while_the_registry_holds_it(tmp_path: Path):
    """Songs from before 0.2 have no metadata.json: the registry is the only
    record that they finished, so the marker comes from it."""
    stems_dir = tmp_path / SONG / "stems"
    stems_dir.mkdir(parents=True)
    for name in STEM_NAMES:
        (stems_dir / f"{name}.wav").write_bytes(b"RIFF")
    _registry._jobs[SONG] = Job(id=SONG, status="done", title="Old Song")
    _registry.persist(tmp_path)

    _restart(tmp_path, registry_lost=False)
    assert (stems_dir / _registry.STEMS_COMPLETE).is_file()
    job = _restart(tmp_path, registry_lost=True)

    assert job is not None and job.status == "done"
    assert {s["name"] for s in job.stems} == set(STEM_NAMES)
