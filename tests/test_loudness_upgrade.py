"""Songs analysed before the loudness fix keep a reading about 4 LU too low
(mono, 22 kHz, first 180 s). Opening one measures it again, once, in the
background: the open answers with the stored numbers (F11)."""

from __future__ import annotations

import json
import threading
import time
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
    _settle()
    _jobs.clear()
    api_jobs._LOUDNESS_TRIED.clear()
    api_jobs._LOUDNESS_PENDING.clear()


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _settle() -> None:
    """Wait for the background measurements to finish."""
    from app.api import jobs as api_jobs

    worker = api_jobs._LOUDNESS_WORKER
    if worker is not None:
        worker.join(10)
        assert not worker.is_alive(), "the background measurement did not finish"


def _old_song(version: int | None, job_id: str = JOB_ID) -> Job:
    """A finished song whose stems sum to a -20 dBFS stereo sine, stored as -30."""
    from app.core import config

    stems = config.JOBS_DIR / job_id / "stems"
    stems.mkdir(parents=True)
    _sine_wav(stems / "vocals.wav", [(5.0, -26.0)])
    _sine_wav(stems / "drums.wav", [(5.0, -26.0)])
    (config.JOBS_DIR / job_id / "metadata.json").write_text(
        json.dumps({"title": "Old", "lufs": -30.0, "peak_db": -25.0}), encoding="utf-8"
    )
    job = Job(id=job_id, status="done", title="Old", lufs=-30.0, peak_db=-25.0)
    job.dynamic_range = 5.0
    job.loudness_version = version
    _jobs[job_id] = job
    return job


def test_an_old_song_is_measured_again_when_opened(client):
    skip_without_ffmpeg()
    job = _old_song(None)

    client.get(f"/api/jobs/{JOB_ID}")
    _settle()
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


def test_opening_an_old_song_does_not_wait_for_the_measurement(client):
    """F11: the first open answered only after a full-song ffmpeg run."""
    job = _old_song(None)
    release = threading.Event()

    def slow_measure(_sources):
        release.wait(5)
        return -12.0, -1.0

    with patch("app.api.jobs.measure_loudness", side_effect=slow_measure):
        started = time.monotonic()
        state = client.get(f"/api/jobs/{JOB_ID}").json()
        waited = time.monotonic() - started
        release.set()
        _settle()

    assert waited < 2, f"GET waited {waited:.1f} s for the measurement"
    assert state["lufs"] == -30.0, "the stored numbers answer at once"
    assert job.lufs == -12.0
    assert job.loudness_version == LOUDNESS_VERSION


def test_a_song_is_measured_once(client):
    _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(-12.0, -1.0)) as measure:
        client.get(f"/api/jobs/{JOB_ID}")
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    assert measure.call_count == 1


def test_a_song_already_at_the_current_version_is_left_alone(client):
    _old_song(LOUDNESS_VERSION)
    with patch("app.api.jobs.measure_loudness") as measure:
        state = client.get(f"/api/jobs/{JOB_ID}").json()
        _settle()
    measure.assert_not_called()
    assert state["lufs"] == -30.0


def test_a_failed_measurement_keeps_the_old_numbers_and_is_not_retried(client):
    job = _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(None, None)) as measure:
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    assert measure.call_count == 1
    assert job.lufs == -30.0
    assert job.loudness_version is None


def test_a_song_that_is_not_done_is_not_measured(client):
    job = _old_song(None)
    job.status = "analyzing"
    with patch("app.api.jobs.measure_loudness") as measure:
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    measure.assert_not_called()


def test_the_kept_upload_is_preferred_over_the_stems(client):
    """A song that kept its source is measured from it, not from the stem sum."""
    skip_without_ffmpeg()
    from app.core import config

    job = _old_song(None)
    _sine_wav(config.JOBS_DIR / JOB_ID / "source.wav", [(5.0, -14.0)])
    client.get(f"/api/jobs/{JOB_ID}")
    _settle()
    assert job.lufs == pytest.approx(-14.0, abs=0.1)


def test_no_measurement_runs_beside_a_separation(client):
    """The separation keeps the machine; the song is measured on a later open."""
    job = _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(-12.0, -1.0)) as measure:
        with patch("app.api.jobs.jobqueue.running_id", return_value="fedcba987654"):
            client.get(f"/api/jobs/{JOB_ID}")
            _settle()
        measure.assert_not_called()
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    measure.assert_called_once()
    assert job.loudness_version == LOUDNESS_VERSION


def test_a_separation_that_starts_after_the_open_is_not_shared(client):
    job = _old_song(None)
    running = iter([None, "fedcba987654"])
    with (
        patch("app.api.jobs.measure_loudness", return_value=(-12.0, -1.0)) as measure,
        patch("app.api.jobs.jobqueue.running_id", side_effect=lambda: next(running, None)),
    ):
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
        measure.assert_not_called()
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    measure.assert_called_once()
    assert job.loudness_version == LOUDNESS_VERSION


def test_queued_songs_share_one_registry_save(client):
    ids = [f"abcdef01234{n}" for n in range(3)]
    for job_id in ids:
        _old_song(None, job_id)
    release = threading.Event()

    def slow_measure(_sources):
        release.wait(5)
        return -12.0, -1.0

    with (
        patch("app.api.jobs.measure_loudness", side_effect=slow_measure),
        patch("app.api.jobs.registry_persist") as persist,
    ):
        for job_id in ids:
            client.get(f"/api/jobs/{job_id}")
        release.set()
        _settle()

    assert all(_jobs[job_id].loudness_version == LOUDNESS_VERSION for job_id in ids)
    persist.assert_called_once()


def test_a_full_queue_leaves_the_song_for_a_later_open(client):
    """The queue is bounded, so opening many old songs cannot pile up work."""
    ids = [f"abcdef01234{n}" for n in range(3)]
    for job_id in ids:
        _old_song(None, job_id)
    started = threading.Event()
    release = threading.Event()

    def slow_measure(_sources):
        started.set()
        release.wait(5)
        return -12.0, -1.0

    with (
        patch("app.api.jobs.LOUDNESS_UPGRADE_QUEUE_MAX", 1),
        patch("app.api.jobs.measure_loudness", side_effect=slow_measure) as measure,
    ):
        client.get(f"/api/jobs/{ids[0]}")
        assert started.wait(5)
        client.get(f"/api/jobs/{ids[1]}")
        client.get(f"/api/jobs/{ids[2]}")
        release.set()
        _settle()
        assert measure.call_count == 2
        assert _jobs[ids[2]].loudness_version is None

        client.get(f"/api/jobs/{ids[2]}")
        _settle()
    assert _jobs[ids[2]].loudness_version == LOUDNESS_VERSION


def test_the_version_survives_a_registry_round_trip():
    job = Job(id=JOB_ID, status="done", loudness_version=LOUDNESS_VERSION)
    assert Job.from_record(job.to_record()).loudness_version == LOUDNESS_VERSION


# #923: the card's numbers move when an old song is measured again, so the job
# says so, and from what, until the song is analysed afresh. The page shows the
# note once per song and keeps "seen" in its own library store.


def _remeasure(client, job_id: str = JOB_ID) -> tuple[dict, list]:
    """Open an old song, let the background measurement run, return the state
    the open answered with and the files that were measured."""
    measured: list = []

    def fake_measure(sources):
        measured.extend(sources)
        return -10.5, -1.0

    with patch("app.api.jobs.measure_loudness", side_effect=fake_measure):
        first = client.get(f"/api/jobs/{job_id}").json()
        _settle()
    return first, measured


def test_a_song_measured_from_its_stems_says_so(client):
    _old_song(None)
    first, measured = _remeasure(client)

    assert first["loudness_remeasured"] is False, "the open answers with the old numbers"
    assert first["loudness_basis"] is None
    state = client.get(f"/api/jobs/{JOB_ID}").json()
    assert state["lufs"] == -10.5
    assert state["loudness_remeasured"] is True
    assert state["loudness_basis"] == "stems"
    assert {p.name for p in measured} == {"vocals.wav", "drums.wav"}


def test_a_song_measured_from_its_kept_upload_says_so(client):
    from app.core import config

    _old_song(None)
    _sine_wav(config.JOBS_DIR / JOB_ID / "source.wav", [(1.0, -14.0)])
    _, measured = _remeasure(client)

    state = client.get(f"/api/jobs/{JOB_ID}").json()
    assert state["loudness_remeasured"] is True
    assert state["loudness_basis"] == "source"
    assert [p.name for p in measured] == ["source.wav"]


def test_a_song_not_measured_again_says_nothing(client):
    _old_song(LOUDNESS_VERSION)
    first, measured = _remeasure(client)
    second = client.get(f"/api/jobs/{JOB_ID}").json()

    assert measured == []
    for state in (first, second):
        assert state["loudness_remeasured"] is False
        assert state["loudness_basis"] is None


def test_a_failed_measurement_says_nothing(client):
    _old_song(None)
    with patch("app.api.jobs.measure_loudness", return_value=(None, None)):
        client.get(f"/api/jobs/{JOB_ID}")
        _settle()
    state = client.get(f"/api/jobs/{JOB_ID}").json()
    assert state["loudness_remeasured"] is False
    assert state["loudness_basis"] is None


def test_the_remeasure_outlives_a_restart(client):
    """Measured in the background after an open, so the next open, the one
    that shows the new numbers, is often after the app was closed."""
    from app.core import config
    from app.core.registry import _recover_done_job

    job = _old_song(None)
    _remeasure(client)

    meta = json.loads((config.JOBS_DIR / JOB_ID / "metadata.json").read_text(encoding="utf-8"))
    assert meta["loudness_remeasured"] == "stems"
    assert Job.from_record(job.to_record()).loudness_remeasured == "stems"
    rebuilt = _recover_done_job(config.JOBS_DIR / JOB_ID)
    assert rebuilt is not None
    assert rebuilt.loudness_remeasured == "stems"


def test_the_basis_is_written_before_the_version(client):
    """A GET that reads the job while the re-measure writes it must never see
    the new version without its basis: the page would take the song as
    measured with the current method and show no note."""
    from app.api import jobs as api_jobs
    from app.core.models import _set as real_set

    order: list[str] = []

    def recording_set(job, **fields):
        order.extend(fields)
        real_set(job, **fields)

    _old_song(None)
    with patch.object(api_jobs, "_set", side_effect=recording_set):
        _remeasure(client)
    assert order.index("loudness_remeasured") < order.index("loudness_version")


def test_an_unknown_basis_on_disk_is_not_reported():
    job = Job(id=JOB_ID, status="done", loudness_version=LOUDNESS_VERSION)
    job.loudness_remeasured = "elsewhere"
    state = job.to_state()
    assert state["loudness_remeasured"] is False
    assert state["loudness_basis"] is None
