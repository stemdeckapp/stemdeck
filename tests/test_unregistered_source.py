"""A job folder that holds only a source file is never auto-deleted.

A song stopped before it was separated keeps its source and nothing else, and
for an upload that file is the only copy StemDeck has. When registry.json is
lost or set aside as corrupt, restore() rebuilt only folders with stems, and the
auto-delete sweep then removed the old unregistered folder with the file in it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.config import STEM_NAMES
from app.core.models import Job
from app.pipeline.collect import sweep_old_jobs

ORPHAN = "abcdef00aa01"


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry._jobs.clear()
    registry._deleted.clear()
    registry._pending_resume.clear()
    yield
    registry._jobs.clear()
    registry._deleted.clear()
    registry._pending_resume.clear()


@pytest.fixture
def jobs_dir() -> Path:
    """The jobs folder conftest points every JOBS_DIR at, so the API sees it."""
    from app.core import config

    return config.JOBS_DIR


def _backdate(path: Path) -> None:
    old = time.time() - 999_999
    os.utime(path, (old, old))


def _source_only(jobs_dir: Path, job_id: str = ORPHAN, name: str = "source.wav") -> Path:
    d = jobs_dir / job_id
    d.mkdir(parents=True)
    source = d / name
    source.write_bytes(b"RIFF")
    _backdate(d)
    return source


def _link(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no privilege needed), a
    symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


# ── the auto-delete sweep ───────────────────────────────────────────────────


def test_sweep_keeps_an_old_unregistered_folder_holding_only_a_source(jobs_dir: Path, caplog):
    source = _source_only(jobs_dir)

    with caplog.at_level(logging.DEBUG, logger="stemdeck.collect"):
        sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert source.read_bytes() == b"RIFF"
    assert any(ORPHAN in r.getMessage() and "source file" in r.getMessage() for r in caplog.records)


def test_sweep_keeps_a_source_restore_does_not_adopt(jobs_dir: Path):
    """A download cut off part way is not a song to adopt, but the folder may
    still hold the only copy of something: kept until the user resets."""
    partial = _source_only(jobs_dir, name="source.webm.part")

    registry.restore(jobs_dir)
    assert ORPHAN not in registry._jobs
    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert partial.is_file()


def test_a_deleted_songs_leftover_source_still_goes(jobs_dir: Path):
    """The user asked for it gone; its files only outlived the delete."""
    source = _source_only(jobs_dir)
    registry.mark_deleted(ORPHAN)

    registry.restore(jobs_dir)
    assert ORPHAN not in registry._jobs
    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert not source.parent.exists()


# ── a restart with registry.json missing ────────────────────────────────────


def test_a_restart_brings_it_back_as_a_stopped_song(jobs_dir: Path, caplog):
    source = _source_only(jobs_dir)

    with caplog.at_level(logging.WARNING, logger="stemdeck.registry"):
        registry.restore(jobs_dir)

    job = registry._jobs[ORPHAN]
    assert job.status == "stopped"
    assert job.title
    assert job.source_url == f"local:{job.title}"
    assert job.source_format == "wav"
    assert job.selected_stems == list(STEM_NAMES)
    assert job.trashed_at is None
    assert registry.take_pending_resume() == [], "a stopped song is never started on its own"
    assert any(ORPHAN in r.getMessage() for r in caplog.records)
    # A normal stopped record, which every version that knows "stopped" loads.
    records = json.loads(registry.registry_path(jobs_dir).read_text(encoding="utf-8"))["jobs"]
    assert [r["status"] for r in records if r["id"] == ORPHAN] == ["stopped"]

    sweep_old_jobs(jobs_dir, ttl_seconds=60)
    assert source.is_file()

    registry._jobs.clear()
    registry.restore(jobs_dir)
    assert registry._jobs[ORPHAN].status == "stopped"
    assert registry._jobs[ORPHAN].title == job.title
    assert source.is_file()


def test_the_song_it_brings_back_can_be_extracted(jobs_dir: Path):
    _source_only(jobs_dir, name="source.mp3")
    registry.restore(jobs_dir)

    from app.main import app

    r = TestClient(app).post(f"/api/jobs/{ORPHAN}/extract")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"


def test_reset_still_removes_it(jobs_dir: Path):
    source = _source_only(jobs_dir)
    registry.restore(jobs_dir)

    registry.reset_all(jobs_dir)

    assert not source.parent.exists()


def test_a_registered_stopped_song_keeps_its_own_record(jobs_dir: Path):
    job = Job(id=ORPHAN, status="stopped", title="My Song", source_url="local:My Song")
    registry._jobs[job.id] = job
    registry.persist(jobs_dir)
    source = _source_only(jobs_dir)
    registry._jobs.clear()

    registry.restore(jobs_dir)
    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert registry._jobs[ORPHAN].title == "My Song"
    assert registry._jobs[ORPHAN].source_url == "local:My Song"
    assert source.is_file()


def test_a_folder_with_someone_elses_file_is_left_alone(jobs_dir: Path):
    foreign = jobs_dir / "0123456789ab"
    foreign.mkdir()
    (foreign / "config.v2.json").write_text("{}", encoding="utf-8")
    _backdate(foreign)

    registry.restore(jobs_dir)
    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert "0123456789ab" not in registry._jobs
    assert (foreign / "config.v2.json").is_file()


def test_a_linked_folder_is_never_followed(jobs_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source.wav").write_bytes(b"RIFF")
    _backdate(outside)
    _link(jobs_dir / ORPHAN, outside)

    registry.restore(jobs_dir)
    sweep_old_jobs(jobs_dir, ttl_seconds=60)
    registry.reset_all(jobs_dir)

    assert ORPHAN not in registry._jobs
    assert (outside / "source.wav").read_bytes() == b"RIFF"


def test_a_folder_holding_a_link_is_not_adopted_but_kept(jobs_dir: Path, tmp_path: Path):
    """Extract writes the stems into stems/: through a link they would land
    on whatever it points at."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "photo.jpg").write_bytes(b"JPEG")
    source = _source_only(jobs_dir)
    _link(source.parent / "stems", outside)
    _backdate(source.parent)

    registry.restore(jobs_dir)
    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert ORPHAN not in registry._jobs
    assert source.is_file()
    assert (outside / "photo.jpg").read_bytes() == b"JPEG"


def test_it_is_adopted_by_the_file_extract_would_run(jobs_dir: Path):
    """Extract runs the first source.* there is. When that one is not an
    upload's type, the folder is kept but not offered as a song."""
    _source_only(jobs_dir, name="source.m4a.part")
    (jobs_dir / ORPHAN / "source.mp3").write_bytes(b"ID3")

    registry.restore(jobs_dir)

    assert ORPHAN not in registry._jobs
