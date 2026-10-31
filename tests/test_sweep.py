from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.collect import sweep_old_jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


def _mkdir(jobs_dir: Path, name: str) -> Path:
    d = jobs_dir / name
    d.mkdir(parents=True)
    (d / "marker").write_bytes(b"x")
    return d


def test_skip_active_job_even_if_old(tmp_path: Path):
    """An active (non-terminal) job's directory must never be swept,
    even if its created_at predates the TTL cutoff."""
    d = _mkdir(tmp_path, "abcdefabcdef")
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    job.created_at = time.time() - 999_999  # ancient
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert d.is_dir()
    assert job.id in _jobs


def test_sweeps_terminal_old_job(tmp_path: Path):
    d = _mkdir(tmp_path, "abcdefabcdee")
    job = Job(id="abcdefabcdee")
    job.status = "done"
    job.created_at = time.time() - 999_999
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert not d.exists()
    assert job.id not in _jobs


def test_deletion_is_off_unless_asked_for(monkeypatch):
    """The default has to be "keep". Deleting a finished separation destroys
    work that cannot be recovered, so an install nobody configured must not do
    it. It used to be the other way round, which is how a directly-run backend
    silently emptied a user's library within a day (#459)."""
    from app.core.settings import get_auto_delete_jobs

    for env in ("STEMDECK_DESKTOP", "STEMDECK_PERSIST_LIBRARY"):
        monkeypatch.delenv(env, raising=False)
    assert get_auto_delete_jobs() is False

    # The two variables that used to be the only thing standing between a user
    # and an empty library are now redundant rather than load-bearing.
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    assert get_auto_delete_jobs() is False
    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "1")
    assert get_auto_delete_jobs() is False


def test_persist_library_zero_still_opts_into_deletion(monkeypatch):
    """The one env-based way in, kept for deployments that set it deliberately
    (the Unraid template exposes it, run.sh defaults it to 1)."""
    from app.core.settings import get_auto_delete_jobs

    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "0")
    assert get_auto_delete_jobs() is True


def test_stored_setting_beats_the_environment(monkeypatch):
    """Retention is the user's call, not the deployment's. Unlike jobs_dir,
    where an env pin wins because a mounted volume is not the user's to move,
    how long their own work is kept is theirs to decide."""
    from app.core.settings import get_auto_delete_jobs, set_auto_delete_jobs

    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "0")
    set_auto_delete_jobs(False)
    assert get_auto_delete_jobs() is False

    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "1")
    set_auto_delete_jobs(True)
    assert get_auto_delete_jobs() is True


def _sweep_loop_calls(monkeypatch):
    """Run one pass of the sweep loop, returning what each sweep was called
    with. sweep_old_jobs now takes a TTL, so record both arguments."""
    from app import main as main_mod

    ttl_calls: list = []
    failed_calls: list = []
    monkeypatch.setattr(main_mod, "sweep_old_jobs", lambda d, ttl: ttl_calls.append((d, ttl)))
    monkeypatch.setattr(main_mod, "sweep_failed_jobs", failed_calls.append)

    async def stop_loop(_delay):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(main_mod.asyncio, "sleep", stop_loop)
    return main_mod, ttl_calls, failed_calls


@pytest.mark.asyncio
async def test_sweep_loop_keeps_jobs_but_still_expires_failures(monkeypatch):
    """With deletion off the library is untouched, but the failed-job
    quarantine still expires (#277) -- failure evidence isn't library
    content, and that half was never opt-in."""
    from app.core.settings import set_auto_delete_jobs

    set_auto_delete_jobs(False)
    main_mod, ttl_calls, failed_calls = _sweep_loop_calls(monkeypatch)

    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()

    assert ttl_calls == []
    assert failed_calls == [main_mod.JOBS_DIR]


@pytest.mark.asyncio
async def test_sweep_loop_uses_the_configured_number_of_days(monkeypatch):
    from app.core.settings import set_auto_delete_days, set_auto_delete_jobs

    set_auto_delete_jobs(True)
    set_auto_delete_days(7)
    main_mod, ttl_calls, failed_calls = _sweep_loop_calls(monkeypatch)

    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()

    assert ttl_calls == [(main_mod.JOBS_DIR, 7 * 86400)]
    assert failed_calls == [main_mod.JOBS_DIR]


@pytest.mark.asyncio
async def test_sweep_loop_rereads_the_setting_every_pass(monkeypatch):
    """Turning deletion on must not need a restart. The loop used to decide
    once, before its first iteration, which would have made this setting the
    only one in the panel that did nothing until relaunch."""
    from app.core.settings import set_auto_delete_jobs

    set_auto_delete_jobs(False)
    main_mod, ttl_calls, _ = _sweep_loop_calls(monkeypatch)

    passes = {"n": 0}

    async def sleep_then_enable(_delay):
        passes["n"] += 1
        if passes["n"] >= 2:
            raise RuntimeError("stop-loop")
        set_auto_delete_jobs(True)

    monkeypatch.setattr(main_mod.asyncio, "sleep", sleep_then_enable)
    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()

    # First pass swept nothing, second one did.
    assert len(ttl_calls) == 1


def test_keeps_recent_terminal_job(tmp_path: Path):
    d = _mkdir(tmp_path, "abcdefabcded")
    job = Job(id="abcdefabcded")
    job.status = "done"
    job.created_at = time.time()  # fresh
    _jobs[job.id] = job

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert d.is_dir()
    assert job.id in _jobs


def test_orphan_dir_falls_back_to_mtime(tmp_path: Path):
    """Directories with no registry entry (e.g. left over from a prior
    server run) still get swept by mtime."""
    d = _mkdir(tmp_path, "abcdefabcdec")
    # Backdate the directory.
    old = time.time() - 999_999
    import os

    os.utime(d, (old, old))

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert not d.exists()


# ── failed-job quarantine sweep (#277) ──


def test_ttl_sweep_never_touches_failed_root(tmp_path: Path):
    """sweep_old_jobs must skip jobs/failed/ even when it looks ancient --
    the quarantine has its own, longer TTL."""
    failed = _mkdir(tmp_path / "failed", "abcdefabcdef")
    old = time.time() - 999_999
    import os

    os.utime(tmp_path / "failed", (old, old))
    os.utime(failed, (old, old))

    with patch("app.pipeline.collect.JOB_TTL_SECONDS", 60):
        sweep_old_jobs(tmp_path)

    assert failed.is_dir()


def test_sweep_failed_jobs_expires_old_keeps_fresh(tmp_path: Path):
    from app.pipeline.collect import sweep_failed_jobs

    old_dir = _mkdir(tmp_path / "failed", "abcdefabcde1")
    fresh_dir = _mkdir(tmp_path / "failed", "abcdefabcde2")
    old = time.time() - 999_999
    import os

    os.utime(old_dir, (old, old))

    with patch("app.pipeline.collect.FAILED_TTL_SECONDS", 3600):
        sweep_failed_jobs(tmp_path)

    assert not old_dir.exists()
    assert fresh_dir.is_dir()


def test_sweep_failed_jobs_noop_without_quarantine(tmp_path: Path):
    from app.pipeline.collect import sweep_failed_jobs

    sweep_failed_jobs(tmp_path)  # must not raise


def test_restore_ignores_failed_quarantine(tmp_path: Path):
    """registry.restore must not resurrect a quarantined failure as a job."""
    from app.core.registry import restore

    quarantined = tmp_path / "failed" / "abcdefabcde3"
    (quarantined / "stems").mkdir(parents=True)
    (quarantined / "error.txt").write_text("evidence", encoding="utf-8")

    restore(tmp_path)

    assert "abcdefabcde3" not in _jobs
    assert quarantined.is_dir()  # restore must not delete it either


# ── what auto-delete must never take (favourites, takes, non-job folders) ──


def _old_done_job(jobs_dir: Path, job_id: str, **fields) -> tuple[Job, Path]:
    d = _mkdir(jobs_dir, job_id)
    job = Job(id=job_id)
    job.status = "done"
    job.created_at = time.time() - 999_999
    for key, value in fields.items():
        setattr(job, key, value)
    _jobs[job.id] = job
    return job, d


def _backdate(path: Path) -> None:
    import os

    old = time.time() - 999_999
    os.utime(path, (old, old))


def test_favourite_older_than_ttl_survives(tmp_path: Path):
    job, d = _old_done_job(tmp_path, "aaaaaaaaaaa1", favorite=True)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()
    assert job.id in _jobs


def test_job_with_takes_in_its_record_survives(tmp_path: Path):
    job, d = _old_done_job(tmp_path, "aaaaaaaaaaa2", takes=[{"id": "t1"}])
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()
    assert job.id in _jobs


def test_job_with_tracks_in_its_record_survives(tmp_path: Path):
    _job, d = _old_done_job(tmp_path, "aaaaaaaaaaa3", tracks=[{"id": "r1"}])
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


def test_job_with_a_take_on_disk_only_survives(tmp_path: Path):
    """A take being recorded is a folder before it is in job.takes."""
    _job, d = _old_done_job(tmp_path, "aaaaaaaaaaa4")
    (d / "takes" / "sometake").mkdir(parents=True)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


def test_plain_old_job_is_removed_and_logged(tmp_path: Path, caplog):
    import logging

    job, d = _old_done_job(tmp_path, "aaaaaaaaaaa5", favorite=False)
    with caplog.at_level(logging.INFO, logger="stemdeck.collect"):
        sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert not d.exists()
    assert job.id not in _jobs
    assert any("aaaaaaaaaaa5" in r.getMessage() for r in caplog.records)


def test_trashed_old_job_follows_the_ordinary_rule(tmp_path: Path):
    """The Trash has no purge of its own; the sweep treats a trashed finished
    job like any other finished job, as it always did."""
    _job, d = _old_done_job(tmp_path, "aaaaaaaaaaa6", trashed_at=time.time() - 999_999)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert not d.exists()


def test_trashed_favourite_is_still_kept(tmp_path: Path):
    _job, d = _old_done_job(
        tmp_path, "aaaaaaaaaaa7", trashed_at=time.time() - 999_999, favorite=True
    )
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


@pytest.mark.parametrize("name", ["My Samples", "backup", "ABCDEFABCDEF", "abcdefabcdef0"])
def test_old_folder_that_is_not_a_job_survives(tmp_path: Path, name: str):
    d = _mkdir(tmp_path, name)
    _backdate(d)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


def test_unregistered_job_folder_with_stems_survives(tmp_path: Path):
    """restore() adopts every folder with stems, so one still unregistered may
    be a load that went wrong. Its audio is kept."""
    d = _mkdir(tmp_path, "bbbbbbbbbbb1")
    (d / "stems").mkdir()
    (d / "stems" / "vocals.wav").write_bytes(b"RIFF")
    _backdate(d)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


def test_unregistered_job_folder_with_takes_survives(tmp_path: Path):
    d = _mkdir(tmp_path, "bbbbbbbbbbb2")
    (d / "takes" / "t").mkdir(parents=True)
    _backdate(d)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert d.is_dir()


def test_tombstoned_folder_with_stems_is_removed(tmp_path: Path):
    """The user deleted this job and its files outlived the delete."""
    from app.core.registry import mark_deleted

    d = _mkdir(tmp_path, "bbbbbbbbbbb3")
    (d / "stems").mkdir()
    (d / "stems" / "vocals.wav").write_bytes(b"RIFF")
    _backdate(d)
    mark_deleted(d.name)
    sweep_old_jobs(tmp_path, ttl_seconds=60)
    assert not d.exists()


@pytest.mark.asyncio
async def test_ttl_disabled_deletes_nothing(tmp_path: Path, monkeypatch):
    """Auto-delete off (the default): the loop never calls the TTL sweep, so a
    plain old job, the one thing the sweep would take, stays."""
    from app import main as main_mod
    from app.core.settings import set_auto_delete_jobs

    _job, d = _old_done_job(tmp_path, "aaaaaaaaaaa8")
    set_auto_delete_jobs(False)
    monkeypatch.setattr(main_mod, "JOBS_DIR", tmp_path)

    async def stop_loop(_delay):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(main_mod.asyncio, "sleep", stop_loop)
    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()
    assert d.is_dir()
