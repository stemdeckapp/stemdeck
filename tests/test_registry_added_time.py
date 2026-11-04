"""A song rebuilt from its folder keeps the time it was added.

The library lists songs oldest first by created_at (#772). When registry.json
is lost, restore() rebuilds each song from its folder, and a song stopped before
it was separated took the folder's own time: that changes with every file
written into the folder, so the song moved in the list. The add time is now
written into the folder's metadata.json when the song is added, and the
rebuild reads it back. A folder without it still takes the folder's time.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.config import DEMUCS_MODEL, STEM_NAMES
from app.core.models import Job, JobCancelled
from app.core.registry import registry_path
from app.pipeline.runner import _write_metadata, run_local_pipeline, run_pipeline

SONG = "c1c2c3d4e5f6"
OLD = "0a20c0ffee01"
FIXTURE = Path(__file__).parent / "fixtures" / "upgrade-0.20.0" / "library" / OLD
ADDED = 1790800000.0


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry._jobs.clear()
    registry._deleted.clear()
    yield
    registry._jobs.clear()
    registry._deleted.clear()


@pytest.fixture
def jobs_dir() -> Path:
    """The jobs folder conftest points every JOBS_DIR at, so the API sees it."""
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client():
    with (
        patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None),
        patch("app.api.jobs._probe_duration", return_value=60.0),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _folder_changes(job_dir: Path) -> None:
    """What a run does to a song's folder: its separation is written into it
    and cleared again, and the folder's time moves on."""
    (job_dir / DEMUCS_MODEL).mkdir()
    shutil.rmtree(job_dir / DEMUCS_MODEL)
    later = time.time() + 3600
    os.utime(job_dir, (later, later))


def _rebuilt(jobs_dir: Path, job_id: str) -> Job:
    registry_path(jobs_dir).unlink()
    registry._jobs.clear()
    registry.restore(jobs_dir)
    job = registry.get(job_id)
    assert job is not None
    return job


def _finished(jobs_dir: Path, **fields) -> Job:
    """A finished song as the pipeline leaves it: complete stems,
    metadata.json and a registry entry."""
    stems = jobs_dir / SONG / "stems"
    stems.mkdir(parents=True)
    for name in STEM_NAMES:
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    registry.mark_stems_complete(jobs_dir / SONG)
    job = Job(id=SONG, status="done", progress=1.0, title="Song", **fields)
    _write_metadata(job, jobs_dir / SONG)
    registry.register(job)
    registry.persist(jobs_dir)
    return job


def _stopped_upload(client: TestClient, jobs_dir: Path) -> Job:
    """An upload stopped before it was separated, its source kept."""
    r = client.post("/api/jobs", files={"file": ("song.mp3", io.BytesIO(b"ID3"), "audio/mpeg")})
    assert r.status_code == 200, r.text
    job = registry.get(r.json()["job_id"])
    job.status = "stopped"
    registry.persist(jobs_dir)
    return job


# ── a finished song ─────────────────────────────────────────────────────────


def test_a_finished_song_keeps_its_add_time_when_its_folder_changes(jobs_dir):
    _finished(jobs_dir, created_at=ADDED)
    _folder_changes(jobs_dir / SONG)

    assert _rebuilt(jobs_dir, SONG).created_at == ADDED


def test_a_finished_song_from_0_20_0_still_takes_its_folder_time(jobs_dir):
    """0.20.0's metadata.json holds no add time: nothing better is known."""
    (jobs_dir / OLD / "stems").mkdir(parents=True)
    (jobs_dir / OLD / "stems" / "vocals.wav").write_bytes(b"RIFF")
    shutil.copy(FIXTURE / "metadata.json", jobs_dir / OLD / "metadata.json")
    registry.persist(jobs_dir)

    job = _rebuilt(jobs_dir, OLD)

    assert job.created_at == (jobs_dir / OLD).stat().st_mtime


# ── a song stopped before it was separated ──────────────────────────────────


def test_a_stopped_upload_keeps_its_add_time_when_its_folder_changes(client, jobs_dir):
    job = _stopped_upload(client, jobs_dir)
    _folder_changes(jobs_dir / job.id)

    rebuilt = _rebuilt(jobs_dir, job.id)

    assert rebuilt.status == "stopped"
    assert rebuilt.created_at == job.created_at


async def test_a_stopped_link_keeps_its_add_time_when_its_folder_changes(jobs_dir):
    """A link's folder is made when it runs, not when it is added."""
    job = Job(id=SONG, source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", created_at=ADDED)
    registry.register(job)
    registry.persist(jobs_dir)

    def stopped_after_the_download(job, url, job_dir):
        (job_dir / "source.m4a").write_bytes(b"\x00")
        job.stop_requested = True
        raise JobCancelled()

    with patch("app.pipeline.runner._run_blocking", side_effect=stopped_after_the_download):
        await run_pipeline(job, job.source_url, jobs_dir)
    assert job.status == "stopped"
    _folder_changes(jobs_dir / SONG)

    assert _rebuilt(jobs_dir, SONG).created_at == ADDED


async def test_a_song_added_before_the_add_time_was_kept_gets_it_when_it_runs(jobs_dir):
    """Extract of an old stopped upload: the registry still knows when it was
    added, and the run puts that in the folder before writing into it."""
    (jobs_dir / SONG).mkdir()
    source = jobs_dir / SONG / "source.wav"
    source.write_bytes(b"RIFF")
    job = Job(id=SONG, title="Old", source_url="local:Old", created_at=ADDED)
    registry.register(job)

    def stopped(job, source_path, job_dir):
        job.stop_requested = True
        raise JobCancelled()

    with patch("app.pipeline.runner._run_local_blocking", side_effect=stopped):
        await run_local_pipeline(job, source, jobs_dir)

    meta = json.loads((jobs_dir / SONG / "metadata.json").read_text(encoding="utf-8"))
    assert meta == {"created_at": ADDED}


def test_a_folder_that_cannot_be_checked_does_not_fail_the_caller(jobs_dir, monkeypatch, caplog):
    (jobs_dir / SONG).mkdir()

    def denied(self):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(Path, "exists", denied)
    with caplog.at_level(logging.WARNING, logger="stemdeck.registry"):
        registry.keep_added_time(Job(id=SONG), jobs_dir / SONG)

    assert f"could not write the add time of {SONG}" in caplog.text


def test_a_resplit_folder_holds_its_own_add_time(jobs_dir):
    from app.main import app

    _finished(jobs_dir, source_url="local:Song")
    (jobs_dir / SONG / "source.mp3").write_bytes(b"ID3")
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        r = TestClient(app).post(f"/api/jobs/{SONG}/resplit", json={"stems": ["vocals"]})
    assert r.status_code == 200, r.text
    new_id = r.json()["job_id"]

    meta = json.loads((jobs_dir / new_id / "metadata.json").read_text(encoding="utf-8"))

    assert meta == {"created_at": registry.get(new_id).created_at}


def test_a_stopped_folder_from_before_still_takes_its_folder_time(jobs_dir):
    (jobs_dir / SONG).mkdir()
    (jobs_dir / SONG / "source.wav").write_bytes(b"RIFF")
    registry.persist(jobs_dir)

    job = _rebuilt(jobs_dir, SONG)

    assert job.status == "stopped"
    assert job.created_at == (jobs_dir / SONG).stat().st_mtime


def test_the_add_time_is_never_overwritten(client, jobs_dir):
    job = _stopped_upload(client, jobs_dir)
    meta_path = jobs_dir / job.id / "metadata.json"
    meta_path.write_text(json.dumps({"created_at": ADDED}), encoding="utf-8")

    registry.keep_added_time(Job(id=job.id), jobs_dir / job.id)

    assert json.loads(meta_path.read_text(encoding="utf-8")) == {"created_at": ADDED}


# ── what is read back from disk ─────────────────────────────────────────────


# Python's json writes and reads NaN and infinity; no JSON response can carry them.
@pytest.mark.parametrize(
    "bad", [-1.0, float("nan"), float("inf"), 10**400, "1790800000", True, None]
)
@pytest.mark.parametrize("kind", ["finished", "stopped"])
def test_an_add_time_that_is_no_time_is_not_loaded(jobs_dir, kind, bad):
    if kind == "finished":
        _finished(jobs_dir)
    else:
        (jobs_dir / SONG).mkdir()
        (jobs_dir / SONG / "source.wav").write_bytes(b"RIFF")
    meta_path = jobs_dir / SONG / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta_path.write_text(json.dumps({**meta, "created_at": bad}), encoding="utf-8")
    registry.persist(jobs_dir)

    job = _rebuilt(jobs_dir, SONG)

    assert job.created_at == (jobs_dir / SONG).stat().st_mtime


def test_a_registry_record_with_a_negative_add_time_is_repaired(jobs_dir):
    """registry.json is read from disk too. Left negative, the auto-delete
    sweep would take the song as long expired."""
    record = Job(id=SONG, status="done", title="Song", created_at=-5.0).to_record()
    registry_path(jobs_dir).write_text(
        json.dumps({"version": 2, "jobs": [record]}), encoding="utf-8"
    )

    registry.restore(jobs_dir)

    assert registry.get(SONG).created_at > 0
    assert list(jobs_dir.glob("registry.json.bad-*")), "the record as it was is kept"


# ── metadata.json from the start does not make a song done ──────────────────


@pytest.mark.parametrize("meta", [{}, {"sections": [], "sections_source": "manual"}])
def test_an_older_metadata_file_without_a_title_still_makes_the_song_done(jobs_dir, meta):
    """Only the file started with the add time is from before the song was
    done. Any other one, as written before (the sections editor makes one
    without a title), keeps meaning a finished run, as it always did."""
    (jobs_dir / SONG / "stems").mkdir(parents=True)
    (jobs_dir / SONG / "stems" / "vocals.wav").write_bytes(b"RIFF")
    (jobs_dir / SONG / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    registry.persist(jobs_dir)

    assert _rebuilt(jobs_dir, SONG).status == "done"


def _cut_off(jobs_dir: Path) -> Path:
    """A separation cut off part way: one stem written, no marker, and the
    metadata.json written when the song was added."""
    job_dir = jobs_dir / SONG
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
    (job_dir / "source.wav").write_bytes(b"RIFF")
    (job_dir / "metadata.json").write_text(json.dumps({"created_at": ADDED}), encoding="utf-8")
    return job_dir


def test_a_separation_cut_off_part_way_is_not_rebuilt_as_done(jobs_dir):
    _cut_off(jobs_dir)
    registry.persist(jobs_dir)

    job = _rebuilt(jobs_dir, SONG)

    assert job.status == "stopped", "brought back from its source, not as a done song"
    assert job.created_at == ADDED


def test_a_separation_cut_off_by_a_crash_runs_again(jobs_dir):
    _cut_off(jobs_dir)
    running = Job(id=SONG, status="separating", title="Song", created_at=ADDED)
    registry_path(jobs_dir).write_text(
        json.dumps({"version": 2, "jobs": [running.to_record()]}), encoding="utf-8"
    )

    registry.restore(jobs_dir)

    assert registry.get(SONG).status == "queued"
    assert registry.take_pending_resume() == [SONG]


def test_a_song_done_as_the_process_died_keeps_its_add_time(jobs_dir, caplog):
    """Crash window (#284): the stems are marked complete and the process died
    before the done metadata.json. The song gets a placeholder title, written
    beside the add time, and the log still says it came back without its
    favourite, Trash and versions."""
    job_dir = _cut_off(jobs_dir)
    registry.mark_stems_complete(job_dir)
    registry.persist(jobs_dir)

    with caplog.at_level(logging.WARNING, logger="stemdeck.registry"):
        job = _rebuilt(jobs_dir, SONG)

    [line] = [r.getMessage() for r in caplog.records if "rebuilt" in r.getMessage()]
    assert "1 without" in line and SONG in line
    assert job.status == "done"
    assert job.title == f"Recovered track {SONG[:6]}"
    assert job.created_at == ADDED
    meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta == {"created_at": ADDED, "title": job.title}
