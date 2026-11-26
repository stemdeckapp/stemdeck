"""POST /api/jobs/{id}/reextract, .../reextract/undo and .../reextract/keep.

The routes check the job id first, then the song, the stem and the model,
answer every refusal as {"detail", "code"}, start a run only while nothing
holds the pipeline, and hold it until the run is over. The separation is the
stand-in of test_reextract_pipeline.py, so the files are checked too.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import numpy as np
import pytest

from app.core.models import Job
from app.core.registry import _jobs, _procs
from app.pipeline import stem_reextract as rx
from app.pipeline.runner import _pipeline_lock
from tests.test_reextract_pipeline import JOB, SIX, FakeRun, _noise, _read, _song


@pytest.fixture(autouse=True)
def _no_real_separation(monkeypatch):
    """Every test that reaches a separation sets its own stand-in; one that
    reaches the real worker by mistake fails here instead of running it."""

    def refuse(*_a, **_kw):
        raise AssertionError("a test reached the real separation")

    monkeypatch.setattr(rx, "separate", refuse)
    monkeypatch.setattr(rx, "download", refuse)


@pytest.fixture
def jobs_dir():
    from app.core import config

    _procs.clear()
    yield config.JOBS_DIR
    _jobs.pop(JOB, None)
    _procs.clear()


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _settled(c: httpx.AsyncClient) -> dict:
    for _ in range(400):
        state = (await c.get(f"/api/jobs/{JOB}")).json()
        if state["stem_reextract"]["state"] != "running":
            return state
        await asyncio.sleep(0.05)
    raise AssertionError("the re-extract never ended")


def _refused(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["code"] == code, r.text
    assert isinstance(r.json()["detail"], str)


async def test_a_re_extract_runs_and_its_state_is_served(jobs_dir, client, monkeypatch):
    _job, values = _song(jobs_dir)
    new = _noise(99)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", new))
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
        assert r.status_code == 202, r.text
        assert r.json()["state"]["stem_reextract"]["state"] == "running"
        state = await _settled(c)
    run = state["stem_reextract"]
    assert run["state"] == "done" and run["stem"] == "guitar" and run["progress"] == 1.0
    assert set(state["stem_undo"]) == {"guitar"}
    assert state["stem_models"]["guitar"] == SIX
    assert np.array_equal(_read(jobs_dir / JOB / "stems" / "guitar.wav"), new)
    assert not _pipeline_lock.locked()


async def test_a_run_reads_as_ended_only_once_the_lock_is_free(jobs_dir, client, monkeypatch):
    """The run's outcome is shown after the pipeline lock is released: a page
    that sees the run end can start the next one, never a "busy". Shown from
    the worker thread, it raced the release (seen on CI)."""
    _job, values = _song(jobs_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    seen: list[tuple[str, bool]] = []
    publish = rx.publish_final

    def watch(job, final):
        seen.append((final["state"], _pipeline_lock.locked()))
        publish(job, final)

    monkeypatch.setattr(rx, "publish_final", watch)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
        assert r.status_code == 202, r.text
        state = await _settled(c)
    assert state["stem_reextract"]["state"] == "done"
    assert seen == [("done", False)]


async def test_undo_and_keep(jobs_dir, client, monkeypatch):
    _job, values = _song(jobs_dir)
    stems = jobs_dir / JOB / "stems"
    old = (stems / "guitar.wav").read_bytes()
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
        await _settled(c)
        r = await c.post(f"/api/jobs/{JOB}/reextract/undo", json={"stem": "guitar"})
        assert r.status_code == 200, r.text
        assert r.json()["state"]["stem_undo"] == {}
        assert (stems / "guitar.wav").read_bytes() == old
        _refused(
            await c.post(f"/api/jobs/{JOB}/reextract/undo", json={"stem": "guitar"}),
            409,
            "no_undo",
        )
        _refused(
            await c.post(f"/api/jobs/{JOB}/reextract/keep", json={"stem": "guitar"}),
            409,
            "no_undo",
        )
        await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
        await _settled(c)
        r = await c.post(f"/api/jobs/{JOB}/reextract/keep", json={"stem": "guitar"})
        assert r.status_code == 200 and r.json()["state"]["stem_undo"] == {}
    assert not (jobs_dir / JOB / rx.UNDO_DIRNAME).exists()


async def test_bad_bodies_are_422(jobs_dir, client):
    _song(jobs_dir)
    async with client as c:
        for body in ({}, {"stem": "guitar"}, {"model": SIX}, {"stem": 1, "model": SIX},
                     {"stem": "g" * 33, "model": SIX}, {"stem": "guitar", "model": "m" * 65}):  # fmt: skip
            r = await c.post(f"/api/jobs/{JOB}/reextract", json=body)
            assert r.status_code == 422, body
        for path in ("undo", "keep"):
            r = await c.post(f"/api/jobs/{JOB}/reextract/{path}", json={})
            assert r.status_code == 422
        for stem in ("other", "lead_vocals", "original", "mix", "../other"):
            for path in ("", "/undo", "/keep"):
                r = await c.post(
                    f"/api/jobs/{JOB}/reextract{path}", json={"stem": stem, "model": SIX}
                )
                _refused(r, 422, "stem_not_selectable")


async def test_unknown_jobs_and_traversal_are_404(jobs_dir, client):
    _song(jobs_dir)
    async with client as c:
        for job_id in ("ffffffffffff", "not-a-job", "..%2F..%2Fetc", "..%2F" + JOB):
            for path in ("", "/undo", "/keep"):
                r = await c.post(
                    f"/api/jobs/{job_id}/reextract{path}", json={"stem": "guitar", "model": SIX}
                )
                assert r.status_code in (404, 405), (job_id, path, r.status_code)
    assert (jobs_dir / JOB / "stems" / "guitar.wav").is_file()


async def test_refusals_have_codes(jobs_dir, client, monkeypatch):
    job, _values = _song(jobs_dir)
    url = f"/api/jobs/{JOB}/reextract"
    async with client as c:
        _refused(await c.post(url, json={"stem": "guitar", "model": "kim_mel_roformer"}), 409,
                 "model_not_allowed")  # fmt: skip
        _refused(await c.post(url, json={"stem": "guitar", "model": "user_0123456789ab"}), 409,
                 "model_removed")  # fmt: skip
        # A context of its own: undo() on the test's monkeypatch would also
        # undo conftest's isolation (JOBS_DIR) for the rest of the test.
        with monkeypatch.context() as m:
            m.setattr(rx.stem_models, "model_available", lambda m: False)
            _refused(await c.post(url, json={"stem": "vocals", "model": "kim_mel_roformer"}),
                     409, "model_unavailable")  # fmt: skip
        (jobs_dir / JOB / "source.wav").unlink()
        _refused(await c.post(url, json={"stem": "guitar", "model": SIX}), 409, "no_source")
        (jobs_dir / JOB / "stems" / "other.wav").unlink()
        _refused(await c.post(url, json={"stem": "guitar", "model": SIX}), 409, "stems_missing")
        job.status = "separating"
        _refused(await c.post(url, json={"stem": "guitar", "model": SIX}), 409, "not_ready")
        _refused(await c.post(f"{url}/undo", json={"stem": "guitar"}), 409, "not_ready")
    assert job.stem_reextract is None


async def test_a_busy_pipeline_is_refused(jobs_dir, client, monkeypatch):
    _song(jobs_dir)
    other = Job(id="aaaaaaaaaaaa", status="done", vocal_split="running")
    async with client as c:
        await _pipeline_lock.acquire()
        try:
            r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
            _refused(r, 409, "busy")
        finally:
            _pipeline_lock.release()
        _jobs[other.id] = other
        try:
            r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
            _refused(r, 409, "busy")
        finally:
            _jobs.pop(other.id, None)


async def test_one_run_at_a_time_and_cancel_stops_it(jobs_dir, client, monkeypatch):
    _job, values = _song(jobs_dir)
    before = (jobs_dir / JOB / "stems" / "guitar.wav").read_bytes()
    fake = FakeRun(values, "guitar", _noise(99))
    fake.gate = threading.Event()
    monkeypatch.setattr(rx, "separate", fake)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "guitar", "model": SIX})
        assert r.status_code == 202
        assert await asyncio.to_thread(fake.started.wait, 5)
        r = await c.post(f"/api/jobs/{JOB}/reextract", json={"stem": "bass", "model": SIX})
        _refused(r, 409, "busy")
        _refused(await c.post(f"/api/jobs/{JOB}/reextract/keep", json={"stem": "bass"}), 409,
                 "busy")  # fmt: skip
        # The song is busy for the routes that would remove its folder.
        r = await c.delete(f"/api/jobs/{JOB}")
        assert r.status_code == 409 and r.json()["code"] == "job_busy"
        r = await c.post(f"/api/jobs/{JOB}/vocal-split")
        assert r.status_code == 409 and r.json()["code"] == "job_busy"
        assert (await c.post(f"/api/jobs/{JOB}/cancel")).status_code == 200
        fake.gate.set()
        state = await _settled(c)
    assert state["stem_reextract"]["state"] == "cancelled"
    assert state["status"] == "done"
    assert (jobs_dir / JOB / "stems" / "guitar.wav").read_bytes() == before
    assert not _pipeline_lock.locked()


def test_a_run_saved_while_running_reads_as_interrupted():
    job = Job.from_record(
        {"id": JOB, "status": "done", "stem_reextract": rx.start_state("guitar", SIX, "reextract")}
    )
    assert job.stem_reextract["state"] == "error"
    assert job.stem_reextract["code"] == "interrupted"
    assert not job.post_hoc_running()


def test_the_export_caches_miss_once_a_stem_was_replaced(jobs_dir):
    from app.api import stems as stems_api

    job = Job(id=JOB, status="done")
    _jobs[JOB] = job
    assert stems_api._take_versions(JOB, ["guitar"]) == []
    job.stem_revisions = {"guitar": 2, "other": 2}
    assert stems_api._take_versions(JOB, ["drums"]) == ["r:guitar=2,other=2"]


def test_a_rebuilt_registry_keeps_the_revised_urls(jobs_dir, monkeypatch):
    import json

    from app.core import registry

    job, values = _song(jobs_dir)
    monkeypatch.setattr(rx, "separate", FakeRun(values, "guitar", _noise(99)))
    rx.reextract_stem(job, jobs_dir / JOB, "guitar", SIX, jobs_dir / JOB / "source.wav")
    meta = {"title": "Song", **registry.saved_fields(job)}
    (jobs_dir / JOB / "metadata.json").write_text(json.dumps(meta))
    (jobs_dir / JOB / "stems" / registry.STEMS_COMPLETE).write_text("")
    rebuilt, _ = registry._rebuild_from_folder(jobs_dir / JOB)
    urls = {s["name"]: s["url"] for s in rebuilt.stems}
    assert urls["guitar"].endswith("guitar.wav?v=1")
    assert urls["drums"].endswith("drums.wav")
    assert rx.undo_entry(rebuilt, jobs_dir / JOB, "guitar") is not None
