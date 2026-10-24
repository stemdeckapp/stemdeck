"""Songs analysed before the loudness fix keep a reading about 4 LU too low
(mono, 22 kHz, first 180 s). Opening one measures it again, once."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.models import LOUDNESS_VERSION, Job
from app.core.registry import _jobs
from tests.ffmpeg_probe import skip_without_ffmpeg
from tests.test_pipeline_analyze import _sine_wav

JOB_ID = "abcdef012345"


@pytest.fixture(autouse=True)
def _fresh_state():
    from app.api import jobs as api_jobs

    _jobs.clear()
    api_jobs._LOUDNESS_TRIED.clear()
    yield
    _jobs.clear()
    api_jobs._LOUDNESS_TRIED.clear()


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _old_song(version: int | None) -> Job:
    """A finished song whose stems sum to a -20 dBFS stereo sine, stored as -30."""
    from app.core import config

    stems = config.JOBS_DIR / JOB_ID / "stems"
    stems.mkdir(parents=True)
    _sine_wav(stems / "vocals.wav", [(5.0, -26.0)])
    _sine_wav(stems / "drums.wav", [(5.0, -26.0)])
    (config.JOBS_DIR / JOB_ID / "metadata.json").write_text(
        json.dumps({"title": "Old", "lufs": -30.0, "peak_db": -25.0}), encoding="utf-8"
    )
    job = Job(id=JOB_ID, status="done", title="Old", lufs=-30.0, peak_db=-25.0)
    job.dynamic_range = 5.0
    job.loudness_version = version
    _jobs[JOB_ID] = job
    return job


def test_an_old_song_is_measured_again_when_opened(client):
    skip_without_ffmpeg()
    job = _old_song(None)

    state = client.get(f"/api/jobs/{JOB_ID}").json()

    assert state["lufs"] == pytest.approx(-20.0, abs=0.15)
    assert state["peak_db"] == pytest.approx(-20.0, abs=0.3)
    assert state["dynamic_range"] == pytest.approx(state["peak_db"] - state["lufs"], abs=0.11)
    assert state["loudness_version"] == LOUDNESS_VERSION
    assert job.loudness_version == LOUDNESS_VERSION
    from app.core import config

    meta = json.loads((config.JOBS_DIR / JOB_ID / "metadata.json").read_text(encoding="utf-8"))
    assert meta["loudness_version"] == LOUDNESS_VERSION
    assert meta["lufs"] == pytest.approx(-20.0, abs=0.15)
    assert meta["title"] == "Old", "the rest of the metadata is left alone"


def test_a_song_is_measured_once(client):
    skip_without_ffmpeg()
    _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(-12.0, -1.0)) as measure:
        client.get(f"/api/jobs/{JOB_ID}")
        client.get(f"/api/jobs/{JOB_ID}")
    assert measure.call_count == 1


def test_a_song_already_at_the_current_version_is_left_alone(client):
    _old_song(LOUDNESS_VERSION)
    with patch("app.api.jobs.measure_loudness") as measure:
        state = client.get(f"/api/jobs/{JOB_ID}").json()
    measure.assert_not_called()
    assert state["lufs"] == -30.0


def test_a_failed_measurement_keeps_the_old_numbers_and_is_not_retried(client):
    _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(None, None)) as measure:
        first = client.get(f"/api/jobs/{JOB_ID}").json()
        client.get(f"/api/jobs/{JOB_ID}")
    assert measure.call_count == 1
    assert first["lufs"] == -30.0
    assert first["loudness_version"] is None


def test_a_song_that_is_not_done_is_not_measured(client):
    job = _old_song(None)
    job.status = "analyzing"
    with patch("app.api.jobs.measure_loudness") as measure:
        client.get(f"/api/jobs/{JOB_ID}")
    measure.assert_not_called()


def test_the_kept_upload_is_preferred_over_the_stems(client):
    """A song that kept its source is measured from it, not from the stem sum."""
    skip_without_ffmpeg()
    from app.core import config

    _old_song(None)
    _sine_wav(config.JOBS_DIR / JOB_ID / "source.wav", [(5.0, -14.0)])
    state = client.get(f"/api/jobs/{JOB_ID}").json()
    assert state["lufs"] == pytest.approx(-14.0, abs=0.1)


def test_the_version_survives_a_registry_round_trip():
    job = Job(id=JOB_ID, status="done", loudness_version=LOUDNESS_VERSION)
    assert Job.from_record(job.to_record()).loudness_version == LOUDNESS_VERSION
