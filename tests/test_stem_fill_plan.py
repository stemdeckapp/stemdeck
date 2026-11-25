"""Extract on a song in the library (fill-stems) runs the plan the song was
separated with, never the current Settings: a stem made with another model
would not add up with the ones the song has. A plan naming a model this
version no longer knows is refused as "model", one naming a model this install
cannot run as "model_unavailable", both a 409 with a fixed message."""

from __future__ import annotations

import httpx
import pytest

from app.core import settings
from app.core.config import DEMUCS_MODEL, STEM_NAMES
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import stem_fill as fill_mod

JOB = "f1f2f3f4f5f6"
SIX = "htdemucs_6s"
ALL_6S = {s: SIX for s in ("vocals", "drums", "bass", "guitar", "piano")}
KIM_PLAN = {**ALL_6S, "vocals": "kim_mel_roformer"}


@pytest.fixture
def jobs_dir():
    from app.core import config

    yield config.JOBS_DIR
    _jobs.pop(JOB, None)


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture(autouse=True)
def _no_peaks(monkeypatch):
    monkeypatch.setattr(fill_mod, "merge_stem_peaks", lambda stems_dir, names: {})


def _make_job(jobs_dir, **fields) -> Job:
    job = Job(
        id=JOB,
        status="done",
        title="Song",
        source_url="local:Song",
        separation_model=DEMUCS_MODEL,
        separation_quality="standard",
        **fields,
    )
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    (stems / "vocals.wav").write_bytes(b"RIFF-vocals-original")
    job.stems.append({"name": "vocals", "url": f"/api/jobs/{JOB}/stems/vocals.wav"})
    (jobs_dir / JOB / "source.wav").write_bytes(b"RIFF-source")
    _jobs[job.id] = job
    return job


def _fake_separate(calls: list):
    def run(job, source, job_dir, quality=None, plan=None):
        calls.append(plan)
        root = job_dir / DEMUCS_MODEL / source.stem
        root.mkdir(parents=True, exist_ok=True)
        for name in STEM_NAMES:
            (root / f"{name}.wav").write_bytes(f"RIFF-{name}-new".encode())
        return root

    return run


async def test_a_fill_runs_the_recorded_plan_not_the_current_settings(
    jobs_dir, client, monkeypatch
):
    _make_job(jobs_dir, stem_models=dict(KIM_PLAN))
    monkeypatch.setattr(fill_mod, "model_available", lambda model_id: True)
    # Settings now say otherwise; the song was separated with Kim's vocals.
    settings.set_stem_model_choices({"drums": "htdemucs_ft"})
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})

    assert r.status_code == 200, r.text
    assert r.json()["filled"] == ["drums"]
    assert calls == [KIM_PLAN]


async def test_a_song_from_before_plans_fills_with_6s_alone(jobs_dir, client, monkeypatch):
    job = _make_job(jobs_dir)
    assert job.stem_models is None
    settings.set_stem_model_choices({"bass": "htdemucs_ft"})
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["bass"]})

    assert r.status_code == 200, r.text
    assert calls == [{s: DEMUCS_MODEL for s in ALL_6S}]


async def test_a_plan_with_a_model_this_version_does_not_know_is_refused(
    jobs_dir, client, monkeypatch
):
    _make_job(jobs_dir, stem_models={**ALL_6S, "vocals": "a_model_since_removed"})
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})

    assert r.status_code == 409
    assert r.json()["detail"] == "this song was separated with another model"
    assert calls == []
    assert not (jobs_dir / JOB / "stems" / "drums.wav").exists()
    assert _jobs[JOB].stem_fill == "error" and _jobs[JOB].status == "done"


async def test_a_plan_with_a_model_this_install_cannot_run_is_refused(
    jobs_dir, client, monkeypatch
):
    _make_job(jobs_dir, stem_models=dict(KIM_PLAN))
    monkeypatch.setattr(
        fill_mod, "model_available", lambda model_id: model_id != "kim_mel_roformer"
    )
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})

    assert r.status_code == 409
    assert r.json()["detail"] == (
        "the model this song was separated with is not available on this install"
    )
    assert calls == []
    assert not (jobs_dir / JOB / "stems" / "drums.wav").exists()


@pytest.mark.parametrize(
    "stem_models",
    [
        {**ALL_6S, "guitar": "htdemucs_ft"},  # a model that cannot make that stem
        {"vocals": SIX},  # stems missing
        {**ALL_6S, "kazoo": SIX},  # a stem nobody chooses
        "htdemucs_6s",  # not a plan at all
    ],
)
def test_a_plan_that_cannot_run_is_refused_as_another_model(stem_models):
    job = Job(id=JOB, separation_model=DEMUCS_MODEL)
    job.stem_models = stem_models
    with pytest.raises(fill_mod.FillRefused) as exc_info:
        fill_mod.recorded_plan(job)
    assert exc_info.value.code == "model"


def test_the_recorded_plan_is_returned_as_recorded(monkeypatch):
    monkeypatch.setattr(fill_mod, "model_available", lambda model_id: True)
    job = Job(id=JOB, separation_model=DEMUCS_MODEL, stem_models=dict(KIM_PLAN))
    assert fill_mod.recorded_plan(job) == KIM_PLAN


# ── models the person added (My models) ──

USER_A = "user_0123456789ab"
USER_PLAN = {**ALL_6S, "drums": USER_A, "bass": USER_A}


@pytest.fixture
def store(monkeypatch):
    from app.core import stem_models
    from tests.test_stem_models import fake_store

    models = {USER_A: {"name": "My drums", "stems": ["drums", "bass"]}}
    fake_store(monkeypatch, models)
    monkeypatch.setattr(stem_models, "roformer_arch_available", lambda arch: True)
    monkeypatch.setattr(fill_mod, "model_available", lambda model_id: True)
    return models


def test_a_usable_user_model_fills_with_the_recorded_plan(store):
    job = Job(id=JOB, separation_model=DEMUCS_MODEL, stem_models=dict(USER_PLAN))
    assert fill_mod.recorded_plan(job) == USER_PLAN


def test_a_removed_user_model_is_refused_as_removed(store):
    store.clear()
    job = Job(id=JOB, separation_model=DEMUCS_MODEL, stem_models=dict(USER_PLAN))
    with pytest.raises(fill_mod.FillRefused) as exc_info:
        fill_mod.recorded_plan(job)
    assert exc_info.value.code == "model_removed"


def test_a_user_model_that_cannot_run_now_is_refused_as_unavailable(store):
    store[USER_A]["usable"] = False
    job = Job(id=JOB, separation_model=DEMUCS_MODEL, stem_models=dict(USER_PLAN))
    with pytest.raises(fill_mod.FillRefused) as exc_info:
        fill_mod.recorded_plan(job)
    assert exc_info.value.code == "model_unavailable"


def test_a_user_model_named_for_a_stem_it_does_not_declare_is_another_model(store):
    job = Job(id=JOB, separation_model=DEMUCS_MODEL, stem_models={**ALL_6S, "piano": USER_A})
    with pytest.raises(fill_mod.FillRefused) as exc_info:
        fill_mod.recorded_plan(job)
    assert exc_info.value.code == "model"


async def test_a_fill_with_a_removed_model_answers_409_and_fills_nothing(
    jobs_dir, client, store, monkeypatch
):
    _make_job(jobs_dir, stem_models=dict(USER_PLAN))
    store.clear()
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})

    assert r.status_code == 409
    assert r.json()["detail"] == "a model this song was separated with was removed"
    assert USER_A not in r.text
    assert calls == []
    assert not (jobs_dir / JOB / "stems" / "drums.wav").exists()


async def test_a_fill_runs_a_usable_user_model_as_recorded(jobs_dir, client, store, monkeypatch):
    _make_job(jobs_dir, stem_models=dict(USER_PLAN))
    calls: list = []
    monkeypatch.setattr(fill_mod, "separate", _fake_separate(calls))

    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]})

    assert r.status_code == 200, r.text
    assert calls == [USER_PLAN]
