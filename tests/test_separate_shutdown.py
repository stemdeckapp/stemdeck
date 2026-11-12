"""A clean shutdown during a separation interrupts the job; it does not fail it.

The lifespan's teardown kills the demucs worker, and the worker's EOF reads
exactly like a crash. Taken as a failure, the job was quarantined with its
upload deleted, and "error" is not persisted, so after a docker stop the song
was gone. Here the real lifespan runs a real job through the queue on a stub
worker, so the teardown, the worker kill, the EOF and the runner's handling are
the production ones; only demucs itself is a stand-in.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import app.main as main_mod
from app.core import registry
from app.core.models import Job
from app.pipeline import jobqueue, runner
from app.pipeline import separate as sep_mod

# Reports some progress, then separates until it is killed.
_SLOW_WORKER = """
import sys, time
sys.stderr.write("@@READY@@\\n")
sys.stdin.readline()
sys.stderr.write("5%\\n")
sys.stderr.flush()
time.sleep(60)
"""


async def _no_sweep():
    pass


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(main_mod, "_sweep_loop", _no_sweep)
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.setattr(main_mod, "HTTPS_PORT", None)
    monkeypatch.setattr(registry, "_jobs", {})
    sep_mod._worker.clear()
    yield
    sep_mod._kill_worker()


async def _until(condition, what: str) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.parametrize("device", ["cpu", "cuda"])
async def test_shutdown_during_separation_keeps_the_upload_and_resumes(monkeypatch, device):
    jobs_dir = jobqueue.JOBS_DIR
    spawned: list[str] = []

    def spawn(dev: str) -> list[str]:
        spawned.append(dev)
        return [sys.executable, "-c", _SLOW_WORKER]

    monkeypatch.setattr(sep_mod, "_spawn_worker_cmd", spawn)
    monkeypatch.setattr(sep_mod, "get_demucs_device", lambda: device)
    # Separation only: the stages around it are not what a shutdown interrupts.
    monkeypatch.setattr(runner, "_run_local_blocking", sep_mod.separate)

    job = registry.register(Job(id="f4f4f4f4f4f4", source_url="local:Hollow Veins"))
    job.title = "Hollow Veins"
    # Resumed once already, by an earlier stop: a clean stop must not use up
    # the one resume that exists for a job that crashes the backend.
    job.resume_attempts = 1
    source = jobs_dir / job.id / "source.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"RIFF" + b"upload" * 32)
    original = source.read_bytes()
    registry.persist(jobs_dir)  # as POST /api/jobs/upload does

    async with main_mod.lifespan(main_mod.app):
        jobqueue.enqueue(job.id)
        await _until(lambda: job.progress > 0, "the separation to start")
    # The teardown killed the worker; let the runner handle what that did.
    await asyncio.wait_for(jobqueue._worker_task, 10)

    assert spawned == [device], "a shutdown never starts a CPU fallback"
    assert source.read_bytes() == original
    assert not (jobs_dir / "failed" / job.id).exists()
    assert job.status in registry._RESUMABLE
    on_disk = json.loads(registry.registry_path(jobs_dir).read_text(encoding="utf-8"))
    assert [
        (r["id"], r["status"] in registry._RESUMABLE, r["resume_attempts"]) for r in on_disk["jobs"]
    ] == [(job.id, True, 0)]

    # The next start: restore() reads the registry back and the job waits,
    # paused, for Start; Start runs it from the kept upload.
    monkeypatch.setattr(registry, "_jobs", {})
    registry.restore(jobs_dir)
    restored = registry.get(job.id)
    assert restored is not None and restored.status == "queued"

    ran_from: list = []
    monkeypatch.setattr(runner, "_run_local_blocking", lambda j, src, d: ran_from.append(src))
    async with main_mod.lifespan(main_mod.app):
        assert jobqueue.is_paused() and jobqueue.snapshot() == (None, [job.id])
        jobqueue.resume()
        await _until(lambda: restored.status == "done", "the resumed job to finish")
    assert ran_from == [source]
