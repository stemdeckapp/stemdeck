"""Favourites live on the server, so the phone can set them and filter by them (#734).

They used to live only in the desktop page's catalog store. The phone builds its
library from GET /api/jobs and had no way to reach that store, so its Favorites
chip listed every track and there was no heart to press.

`favorite` starts as None, meaning no client has said either way. The desktop
uses that to hand up favourites it set before the field existed, without
undoing one taken back out on the phone.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs, register
from app.main import app


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _done(job_id: str) -> Job:
    job = Job(id=job_id, status="done", title="Song")
    register(job)
    return job


def test_a_new_job_has_said_nothing_about_favourites(client: TestClient) -> None:
    _done("aaaaaaaaaaaa")
    assert client.get("/api/jobs/aaaaaaaaaaaa").json()["favorite"] is None


def test_favourite_and_back_out(client: TestClient) -> None:
    _done("aaaaaaaaaaaa")
    resp = client.put("/api/jobs/aaaaaaaaaaaa/favorite", json={"favorite": True})
    assert resp.status_code == 200
    assert resp.json() == {"job_id": "aaaaaaaaaaaa", "favorite": True}
    assert client.get("/api/jobs").json()[0]["favorite"] is True

    resp = client.put("/api/jobs/aaaaaaaaaaaa/favorite", json={"favorite": False})
    assert resp.json()["favorite"] is False
    # False, not None: taken back out is an answer the desktop must respect.
    assert client.get("/api/jobs/aaaaaaaaaaaa").json()["favorite"] is False


def test_the_flag_survives_a_restart() -> None:
    job = _done("aaaaaaaaaaaa")
    job.favorite = True
    assert Job.from_record(job.to_record()).favorite is True


@pytest.mark.parametrize("body", [{}, {"favorite": "false"}, {"favorite": 1}, {"favorite": None}])
def test_anything_but_a_bool_is_refused(client: TestClient, body: dict) -> None:
    _done("aaaaaaaaaaaa")
    resp = client.put("/api/jobs/aaaaaaaaaaaa/favorite", json=body)
    assert resp.status_code == 422
    assert client.get("/api/jobs/aaaaaaaaaaaa").json()["favorite"] is None


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.put("/api/jobs/999999999999/favorite", json={"favorite": True}).status_code == 404


@pytest.mark.parametrize(
    "job_id", ["../../etc/passwd", "..%2F..%2Fetc", "not-a-job-id", "AAAAAAAAAAAA"]
)
def test_crafted_ids_are_refused_not_500(client: TestClient, job_id: str) -> None:
    """Some are collapsed before routing and nothing answers PUT there (405);
    the rest arrive as a job id and are refused (404). Never a 500."""
    resp = client.put(f"/api/jobs/{job_id}/favorite", json={"favorite": True})
    assert resp.status_code in (404, 405)
