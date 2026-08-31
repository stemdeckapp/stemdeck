"""A deleted song must not come back (#521).

restore() adopts any job-shaped directory it finds, which is the right
behaviour for a library whose registry was lost and the wrong behaviour for a
job the user deleted whose files outlived the delete. Reported on macOS via
Settings -> "Reset app data", where the frontend also wipes its own tombstone
on the strength of an unconditional {"ok": true}.
"""

from __future__ import annotations

import json

import pytest

from app.core import registry as _registry
from app.core.registry import registry_path


@pytest.fixture(autouse=True)
def _clean_registry():
    _registry._jobs.clear()
    _registry._deleted.clear()
    yield
    _registry._jobs.clear()
    _registry._deleted.clear()


def _simulate_restart():
    """Drop in-memory state the way a process restart does; restore() then
    reads everything back from disk."""
    _registry._jobs.clear()
    _registry._deleted.clear()


@pytest.fixture
def jobs_dir(tmp_path):
    """A jobs root of our own. conftest puts _jobs_root next to tmp_path, and
    reset_all iterates whatever it is handed."""
    d = tmp_path / "library"
    d.mkdir()
    return d


def _done_job_dir(jobs_dir, job_id="a1b2c3d4e5f6"):
    """A directory shaped like a finished job, which is what restore() adopts."""
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")
    return jobs_dir / job_id


def test_an_orphan_directory_is_adopted_when_it_was_not_deleted(jobs_dir):
    # The behaviour we must not break: a library whose registry was lost is
    # rebuilt from the directories on disk.
    _done_job_dir(jobs_dir)

    _registry.restore(jobs_dir)

    assert "a1b2c3d4e5f6" in _registry.all_jobs()


def test_a_deleted_job_is_not_re_adopted(jobs_dir):
    # The bug: files outlive the delete, and the next start brings the song back.
    _done_job_dir(jobs_dir)
    _registry.mark_deleted("a1b2c3d4e5f6")
    _registry.persist(jobs_dir)

    _simulate_restart()
    _registry.restore(jobs_dir)

    assert _registry.all_jobs() == {}, "a deleted song came back from disk"


def test_the_deletion_record_survives_a_restart(jobs_dir):
    _done_job_dir(jobs_dir)
    _registry.mark_deleted("a1b2c3d4e5f6")
    _registry.persist(jobs_dir)

    assert "a1b2c3d4e5f6" in json.loads(registry_path(jobs_dir).read_text())["deleted"]


def test_the_record_is_forgotten_once_the_directory_is_gone(jobs_dir):
    # Otherwise the set grows for the life of the install. A record only has to
    # outlive the directory it refers to.
    job_dir = _done_job_dir(jobs_dir)
    _registry.mark_deleted("a1b2c3d4e5f6")
    _registry.persist(jobs_dir)

    import shutil

    shutil.rmtree(job_dir)
    _registry.persist(jobs_dir)

    assert json.loads(registry_path(jobs_dir).read_text())["deleted"] == []


def test_reset_reports_what_it_could_not_remove(jobs_dir, monkeypatch):
    _done_job_dir(jobs_dir)

    def _boom(path, *a, **kw):
        raise OSError("Directory not empty")

    monkeypatch.setattr("shutil.rmtree", _boom)

    undeleted = _registry.reset_all(jobs_dir)

    assert undeleted == ["a1b2c3d4e5f6"], "reset must not claim success it did not have"


def test_reset_records_survivors_so_they_cannot_come_back(jobs_dir, monkeypatch):
    # This is the reported path: reset "succeeds", the frontend wipes its
    # tombstone, and the surviving directory is adopted on the next start.
    _done_job_dir(jobs_dir)

    def _boom(path, *a, **kw):
        raise OSError("Directory not empty")

    monkeypatch.setattr("shutil.rmtree", _boom)
    _registry.reset_all(jobs_dir)
    monkeypatch.undo()

    _simulate_restart()
    _registry.restore(jobs_dir)

    assert _registry.all_jobs() == {}, "a reset that half-worked refilled the library"


def test_a_clean_reset_reports_nothing_undeleted(jobs_dir):
    _done_job_dir(jobs_dir)

    assert _registry.reset_all(jobs_dir) == []
