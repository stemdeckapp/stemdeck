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
