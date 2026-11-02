"""A song a stem fill (Extract missing stems) or a vocal split is working on
is busy: deleting it, moving the library or resetting the app is refused with
409 and the code "job_busy", because each would remove or move the folder the
worker writes into. Cancel stops a running fill, and only that fill.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import httpx
import pytest

import app.api.jobs as jobs_mod
from app.core import stems_location
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs, _procs, set_proc
from app.pipeline import stem_fill as fill_mod
from app.pipeline.lyrics_retime import claim_run, forget_run

JOB = "b1b2c3d4e5f6"
OTHER = "c1b2c3d4e5f6"
BUSY = ({"stem_fill": "running"}, {"vocal_split": "running"})


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    _procs.clear()
    stems_location.abandon_relocation()
    yield
    _jobs.clear()
    _procs.clear()
    stems_location.abandon_relocation()


@pytest.fixture
def jobs_dir():
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _song(jobs_dir: Path, job_id: str = JOB, **fields) -> Job:
    job = Job(id=job_id, status="done", title="Song", source_url="local:Song", **fields)
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    (stems / "vocals.wav").write_bytes(b"RIFF-vocals-original")
    job.stems.append({"name": "vocals", "url": f"/api/jobs/{job_id}/stems/vocals.wav"})
    _jobs[job.id] = job
    return job


def _assert_busy(r: httpx.Response) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "job_busy"


# -- delete, reset and the library move refuse a busy song


@pytest.mark.parametrize("running", BUSY)
async def test_delete_is_refused_while_the_song_is_busy(jobs_dir, client, running):
    _song(jobs_dir, **running)
    async with client as c:
        r = await c.delete(f"/api/jobs/{JOB}")
    _assert_busy(r)
    assert (jobs_dir / JOB / "stems" / "vocals.wav").is_file()
    assert JOB in _jobs


async def test_delete_goes_ahead_when_nothing_runs(jobs_dir, client):
    _song(jobs_dir, stem_fill="done", vocal_split="error")
    async with client as c:
        r = await c.delete(f"/api/jobs/{JOB}")
    assert r.status_code == 200, r.text
    assert not (jobs_dir / JOB).exists()


@pytest.mark.parametrize("running", BUSY)
async def test_reset_is_refused_while_a_song_is_busy(jobs_dir, client, running):
    _song(jobs_dir)
    _song(jobs_dir, OTHER, **running)
    async with client as c:
        r = await c.post("/api/reset")
    _assert_busy(r)
    assert (jobs_dir / JOB).is_dir() and (jobs_dir / OTHER).is_dir()
    assert set(_jobs) == {JOB, OTHER}


async def test_reset_goes_ahead_when_nothing_runs(jobs_dir, client):
    _song(jobs_dir, stem_fill="error")
    async with client as c:
        r = await c.post("/api/reset")
    assert r.status_code == 200, r.text
    assert not (jobs_dir / JOB).exists()


@pytest.mark.parametrize("running", BUSY)
async def test_the_library_move_is_refused_while_a_song_is_busy(
    jobs_dir, client, monkeypatch, tmp_path, running
):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    _song(jobs_dir, **running)
    target = tmp_path / "elsewhere"
    async with client as c:
        r = await c.post("/api/settings/stems-location", json={"path": str(target)})
    _assert_busy(r)
    assert (jobs_dir / JOB / "stems" / "vocals.wav").is_file()
    assert not (target / JOB).exists()
    # The door the move closed first is open again: nothing moved.
    assert not stems_location.is_relocating()


async def test_the_library_move_goes_ahead_when_nothing_runs(
    jobs_dir, client, monkeypatch, tmp_path
):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    _song(jobs_dir, stem_fill="done")
    target = tmp_path / "elsewhere"
    async with client as c:
        r = await c.post("/api/settings/stems-location", json={"path": str(target)})
    assert r.status_code == 200, r.text
    assert (target / JOB / "stems" / "vocals.wav").is_file()


# -- cancel stops a running fill


class _Worker:
    """The demucs worker as cancel sees it: a process registered under the
    song's id, which POST .../cancel terminates."""

    def __init__(self) -> None:
        self.terminated = threading.Event()

    def poll(self):
        return 0 if self.terminated.is_set() else None

    def terminate(self) -> None:
        self.terminated.set()


def _blocking_separate(started: threading.Event, workers: list[_Worker], calls: list):
    """A separation that runs until its worker is terminated, then ends the
    way the real one does: JobCancelled when the stand-in was cancelled."""

    def run(stand_in, source, job_dir, quality=None):
        calls.append(stand_in)
        worker = _Worker()
        workers.append(worker)
        set_proc(stand_in.id, worker)
        started.set()
        try:
            worker.terminated.wait(3)
        finally:
            set_proc(stand_in.id, None)
        if stand_in.cancel_requested:
            raise JobCancelled()
        raise RuntimeError("the worker was never stopped")

    return run


async def test_cancel_stops_a_running_fill_and_keeps_the_songs_stems(jobs_dir, client, monkeypatch):
    job = _song(jobs_dir)
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    started, workers, calls = threading.Event(), [], []
    monkeypatch.setattr(fill_mod, "separate", _blocking_separate(started, workers, calls))
    async with client as c:
        fill = asyncio.create_task(c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]}))
        assert await asyncio.to_thread(started.wait, 3)
        r = await c.post(f"/api/jobs/{JOB}/cancel")
        assert r.status_code == 200
        done = await asyncio.wait_for(fill, 5)
        assert workers[0].terminated.is_set()
        assert done.status_code == 409 and "cancelled" in done.json()["detail"]
        assert job.status == "done" and job.stem_fill == "error"
        stems = jobs_dir / JOB / "stems"
        assert (stems / "vocals.wav").read_bytes() == b"RIFF-vocals-original"
        assert not (stems / "drums.wav").exists()
        assert not list((jobs_dir / JOB).glob(".fill-*"))
        # Once it has stopped, the song is no longer busy.
        assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 200


async def test_a_fill_cancelled_while_it_waits_never_starts(jobs_dir, client, monkeypatch):
    job = _song(jobs_dir)
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _blocking_separate(threading.Event(), [], calls))
    # A fresh lock, held here as an import in progress would hold it.
    lock = asyncio.Semaphore(1)
    monkeypatch.setattr(jobs_mod, "_pipeline_lock", lock)
    await lock.acquire()
    # Whatever holds the lock may hold this song's process slot too (a lyrics
    # timing does): it is not the fill's, so cancel leaves it running.
    holder = _Worker()
    set_proc(JOB, holder)
    async with client as c:
        fill = asyncio.create_task(c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]}))
        for _ in range(100):
            if job.stem_fill == "running":
                break
            await asyncio.sleep(0.01)
        assert job.stem_fill == "running"
        await c.post(f"/api/jobs/{JOB}/cancel")
        assert not holder.terminated.is_set()
        set_proc(JOB, None)
        lock.release()
        done = await asyncio.wait_for(fill, 5)
    assert calls == []
    assert done.status_code == 409
    assert job.stem_fill == "error"


async def test_cancel_reaches_the_fill_before_a_lyrics_timing_waiting_behind_it(
    jobs_dir, client, monkeypatch
):
    job = _song(jobs_dir)
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    started, workers = threading.Event(), []
    monkeypatch.setattr(fill_mod, "separate", _blocking_separate(started, workers, []))
    async with client as c:
        fill = asyncio.create_task(c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]}))
        assert await asyncio.to_thread(started.wait, 3)
        # A timing asked for meanwhile, still waiting for the pipeline.
        assert claim_run(JOB) is not None
        try:
            await c.post(f"/api/jobs/{JOB}/cancel")
            done = await asyncio.wait_for(fill, 5)
        finally:
            forget_run(JOB)
    assert workers[0].terminated.is_set()
    # Stopped as a cancel, not ended as a failure by a worker killed under it.
    assert done.status_code == 409 and "cancelled" in done.json()["detail"]
    assert job.stem_fill == "error"
