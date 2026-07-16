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


def test_sweep_disabled_under_desktop(monkeypatch):
    """The desktop shell (STEMDECK_DESKTOP=1) opts out of the TTL sweep so a
    user's curated library isn't purged; the server/Docker default keeps it."""
    from app.main import _sweep_disabled

    monkeypatch.delenv("STEMDECK_PERSIST_LIBRARY", raising=False)
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    assert _sweep_disabled() is True
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    assert _sweep_disabled() is False


def test_sweep_disabled_under_persistent_library(monkeypatch):
    """A self-hosted server (run.sh) opts into a persistent library
    (STEMDECK_PERSIST_LIBRARY=1) so its processed tracks aren't purged."""
    from app.main import _sweep_disabled

    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "1")
    assert _sweep_disabled() is True
    monkeypatch.delenv("STEMDECK_PERSIST_LIBRARY", raising=False)
    assert _sweep_disabled() is False


@pytest.mark.asyncio
async def test_sweep_loop_desktop_skips_ttl_but_sweeps_failed(monkeypatch):
    """Desktop mode skips the library TTL sweep but the failed-job quarantine
    still expires (#277) -- failure evidence isn't library content."""
    from app import main as main_mod

    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    ttl_calls: list = []
    failed_calls: list = []
    monkeypatch.setattr(main_mod, "sweep_old_jobs", ttl_calls.append)
    monkeypatch.setattr(main_mod, "sweep_failed_jobs", failed_calls.append)

    async def stop_loop(_delay):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(main_mod.asyncio, "sleep", stop_loop)
    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()

    assert ttl_calls == []
    assert failed_calls == [main_mod.JOBS_DIR]


@pytest.mark.asyncio
async def test_sweep_loop_server_runs_both_sweeps(monkeypatch):
    from app import main as main_mod

    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.delenv("STEMDECK_PERSIST_LIBRARY", raising=False)
    ttl_calls: list = []
    failed_calls: list = []
    monkeypatch.setattr(main_mod, "sweep_old_jobs", ttl_calls.append)
    monkeypatch.setattr(main_mod, "sweep_failed_jobs", failed_calls.append)

    async def stop_loop(_delay):
        raise RuntimeError("stop-loop")

    monkeypatch.setattr(main_mod.asyncio, "sleep", stop_loop)
    with pytest.raises(RuntimeError, match="stop-loop"):
        await main_mod._sweep_loop()

    assert ttl_calls == [main_mod.JOBS_DIR]
    assert failed_calls == [main_mod.JOBS_DIR]


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
