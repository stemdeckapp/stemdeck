"""Reset and the sweeps remove only what StemDeck put in the jobs folder.

The jobs folder can be a folder the user shares with other things (an Unraid or
Docker volume mapped onto a share, a desktop user who picked an existing
folder). Reset app data used to empty it entry by entry, and the sweeps judged
some folders by name or walked through links, so another program's files, or a
folder a link points at, could go with StemDeck's.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core.models import Job
from app.pipeline.collect import sweep_failed_jobs, sweep_old_jobs
from app.pipeline.sections import sweep_orphaned_workspaces
from app.pipeline.take_render import sweep_render_temps


@pytest.fixture(autouse=True)
def _isolate_registry():
    registry._jobs.clear()
    registry._procs.clear()
    yield
    registry._jobs.clear()
    registry._procs.clear()


@pytest.fixture
def jobs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "jobs"
    d.mkdir()
    return d


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A folder that is not the jobs folder, which a link inside it points at."""
    d = tmp_path / "outside"
    (d / "stems").mkdir(parents=True)
    (d / "photo.jpg").write_bytes(b"JPEG")
    return d


def _link(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no privilege needed), a
    symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _backdate(path: Path) -> None:
    old = time.time() - 999_999
    os.utime(path, (old, old))


def _registered_job(jobs_dir: Path, job_id: str) -> Path:
    job = Job(id=job_id, status="done", title="Song")
    job.created_at = time.time() - 999_999
    registry._jobs[job_id] = job
    d = jobs_dir / job_id
    (d / "stems").mkdir(parents=True)
    (d / "stems" / "vocals.wav").write_bytes(b"RIFF")
    return d


# ─── Reset app data ─────────────────────────────────────────────────────────


def test_reset_removes_stemdecks_entries_and_leaves_foreign_ones(jobs_dir: Path):
    job_dir = _registered_job(jobs_dir, "abcdefabcdef")
    (jobs_dir / "registry.json").write_text("{}", encoding="utf-8")
    (jobs_dir / "registry.json.corrupt-1700000000").write_text("{", encoding="utf-8")
    (jobs_dir / "user-data.json").write_text("{}", encoding="utf-8")
    (jobs_dir / "user-data.json.bak").write_text("{}", encoding="utf-8")
    (jobs_dir / "user-data.json.tmp").write_text("{", encoding="utf-8")
    (jobs_dir / "failed" / "abcdefabcde1").mkdir(parents=True)
    (jobs_dir / "failed" / "abcdefabcde1" / "error.txt").write_text("x", encoding="utf-8")
    (jobs_dir / "My Photos").mkdir()
    (jobs_dir / "My Photos" / "beach.jpg").write_bytes(b"JPEG")
    (jobs_dir / "notes.txt").write_text("mine", encoding="utf-8")
    (jobs_dir / "registry.json.bak").write_text("mine", encoding="utf-8")

    result = registry.reset_all(jobs_dir)

    assert not job_dir.exists()
    assert not (jobs_dir / "registry.json").exists()
    assert not (jobs_dir / "registry.json.corrupt-1700000000").exists()
    assert not (jobs_dir / "user-data.json").exists()
    # #1023: the shell restores a damaged store from these, so a reset that
    # left them would bring the old library back.
    assert not (jobs_dir / "user-data.json.bak").exists()
    assert not (jobs_dir / "user-data.json.tmp").exists()
    assert not (jobs_dir / "failed").exists()
    assert (jobs_dir / "My Photos" / "beach.jpg").read_bytes() == b"JPEG"
    assert (jobs_dir / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert (jobs_dir / "registry.json.bak").is_file(), "only the names StemDeck writes go"
    failed, skipped = result
    assert failed == []
    assert sorted(skipped) == ["My Photos", "notes.txt", "registry.json.bak"]


def test_reset_keeps_a_folder_named_like_a_job_that_is_not_stemdecks(jobs_dir: Path):
    """Twelve hex characters is also a Docker container's short id. Without a
    record, a tombstone or a file StemDeck writes, the folder is not a song."""
    foreign = jobs_dir / "0123456789ab"
    foreign.mkdir()
    (foreign / "config.v2.json").write_text("{}", encoding="utf-8")
    leftover = jobs_dir / "0123456789ac"
    leftover.mkdir()
    (leftover / "source.mp3").write_bytes(b"ID3")

    result = registry.reset_all(jobs_dir)

    assert (foreign / "config.v2.json").is_file()
    assert not leftover.exists(), "a StemDeck folder without a record is still StemDeck's"
    failed, skipped = result
    assert failed == []
    assert skipped == ["0123456789ab"]


def test_reset_never_follows_a_link_out_of_the_jobs_folder(jobs_dir: Path, outside: Path):
    (outside / "stems" / "vocals.wav").write_bytes(b"RIFF")
    _link(jobs_dir / "abcdefabcdef", outside)
    _link(jobs_dir / "Shared", outside)
    registry._jobs["abcdefabcdef"] = Job(id="abcdefabcdef", status="done")

    result = registry.reset_all(jobs_dir)

    assert (outside / "photo.jpg").read_bytes() == b"JPEG"
    assert (outside / "stems" / "vocals.wav").is_file()
    failed, skipped = result
    assert failed == [], "a link is left alone, not a removal that failed"
    assert sorted(skipped) == ["Shared", "abcdefabcdef"]
    # The song behind the link must not come back on the next start (#521).
    registry.restore(jobs_dir)
    assert "abcdefabcdef" not in registry._jobs


def test_reset_keeps_foreign_content_of_a_failed_folder(jobs_dir: Path):
    (jobs_dir / "failed" / "abcdefabcde1").mkdir(parents=True)
    (jobs_dir / "failed" / "abcdefabcde1" / "error.txt").write_text("x", encoding="utf-8")
    (jobs_dir / "failed" / "torrent.iso").write_bytes(b"ISO")
    # A job-id name without the error.txt the runner writes is not a quarantine.
    (jobs_dir / "failed" / "0123456789ab").mkdir()
    (jobs_dir / "failed" / "0123456789ab" / "config.v2.json").write_text("{}", encoding="utf-8")

    result = registry.reset_all(jobs_dir)

    assert not (jobs_dir / "failed" / "abcdefabcde1").exists()
    assert (jobs_dir / "failed" / "torrent.iso").is_file()
    assert (jobs_dir / "failed" / "0123456789ab" / "config.v2.json").is_file()
    failed, skipped = result
    assert failed == []
    assert skipped == ["failed"]


def test_reset_endpoint_reports_what_it_left_alone(jobs_dir: Path, monkeypatch):
    from app.main import app

    monkeypatch.setattr("app.main.JOBS_DIR", jobs_dir)
    _registered_job(jobs_dir, "abcdefabcdef")
    (jobs_dir / "notes.txt").write_text("mine", encoding="utf-8")

    r = TestClient(app).post("/api/reset")

    assert r.status_code == 200
    assert r.json() == {"ok": True, "undeleted": 0, "skipped": 1}
    assert (jobs_dir / "notes.txt").is_file()
    assert not (jobs_dir / "abcdefabcdef").exists()


# ─── Auto-delete (the hourly TTL sweep and the failed-job sweep) ───────────


def test_ttl_sweep_keeps_an_old_job_named_folder_that_is_not_stemdecks(jobs_dir: Path):
    foreign = jobs_dir / "0123456789ab"
    foreign.mkdir()
    (foreign / "config.v2.json").write_text("{}", encoding="utf-8")
    _backdate(foreign)

    sweep_old_jobs(jobs_dir, ttl_seconds=60)

    assert (foreign / "config.v2.json").is_file()


@pytest.mark.parametrize("name", ["My Stuff", "0123456789ab"])
def test_failed_sweep_keeps_foreign_content_of_the_failed_folder(jobs_dir: Path, name: str):
    mine = jobs_dir / "failed" / name
    mine.mkdir(parents=True)
    (mine / "keep.txt").write_text("x", encoding="utf-8")
    _backdate(mine)

    sweep_failed_jobs(jobs_dir)

    assert (mine / "keep.txt").is_file()


def test_failed_sweep_never_follows_a_linked_failed_folder(jobs_dir: Path, outside: Path):
    old = outside / "abcdefabcde1"
    old.mkdir()
    _backdate(old)
    _link(jobs_dir / "failed", outside)

    sweep_failed_jobs(jobs_dir)

    assert old.is_dir()


# ─── Startup sweeps (section workspaces, render temp files) ────────────────


def test_workspace_sweep_never_follows_a_link(jobs_dir: Path, outside: Path):
    workspace = outside / "stems" / ".sections-work-x"
    workspace.mkdir()
    _link(jobs_dir / "Elsewhere", outside)

    sweep_orphaned_workspaces(jobs_dir)

    assert workspace.is_dir()


def test_render_temp_sweep_never_follows_a_link(jobs_dir: Path, outside: Path):
    take = outside / "takes" / "t0000aaaa"
    take.mkdir(parents=True)
    temp = take / ".render.mp4.0123456789abcdef.tmp.mp4"
    temp.write_bytes(b"x")
    _link(jobs_dir / "Elsewhere", outside)

    sweep_render_temps(jobs_dir)

    assert temp.is_file()
