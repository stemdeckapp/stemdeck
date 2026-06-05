from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import MAX_PENDING_JOBS
from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test gets a fresh in-memory registry."""
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    async def _noop_pipeline(job, url, jobs_dir):
        return None

    with patch("app.api.jobs.run_pipeline", _noop_pipeline):
        from app.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)

    async def _noop_local(job, source_path, jobs_dir):
        return None

    async def _noop_youtube(job, url, jobs_dir):
        return None

    with (
        patch("app.api.jobs.run_local_pipeline", _noop_local),
        patch("app.api.jobs.run_pipeline", _noop_youtube),
        patch("app.api.jobs._probe_duration", return_value=60.0),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


def test_post_rejects_invalid_url(client):
    r = client.post("/api/jobs", json={"url": "https://example.com/foo"})
    assert r.status_code == 422
    assert "unsupported host" in r.json()["detail"]


def test_post_rejects_empty_url(client):
    r = client.post("/api/jobs", json={"url": ""})
    assert r.status_code == 422


def test_post_accepts_youtube_url(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200
    assert "job_id" in r.json()
    assert len(r.json()["job_id"]) == 12


def test_get_unknown_job_returns_404(client):
    r = client.get("/api/jobs/000000000000")
    assert r.status_code == 404


def test_cancel_unknown_job_returns_404(client):
    r = client.post("/api/jobs/000000000000/cancel")
    assert r.status_code == 404


def test_delete_running_job_rejected(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 409


def test_cancel_sets_flag_and_returns_state(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert _jobs[job_id].cancel_requested is True


def test_cancel_after_done_is_idempotent(client):
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    job_id = r.json()["job_id"]
    _jobs[job_id].status = "done"
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert _jobs[job_id].cancel_requested is False


# ─── Capacity (503) ───────────────────────────────────────────────────────────


def test_youtube_503_when_queue_full(client):
    for _ in range(MAX_PENDING_JOBS):
        r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert r.status_code == 200
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 503


def test_upload_503_when_queue_full(upload_client):
    for _ in range(MAX_PENDING_JOBS):
        r = upload_client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert r.status_code == 200
    data = io.BytesIO(b"ID3" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("track.mp3", data, "audio/mpeg")},
    )
    assert r.status_code == 503


# ─── File upload ─────────────────────────────────────────────────────────────


def test_upload_rejects_unsupported_extension(upload_client):
    data = io.BytesIO(b"OGG data")
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("track.ogg", data, "audio/ogg")},
    )
    assert r.status_code == 422
    assert "Unsupported file type" in r.json()["detail"]


def test_upload_rejects_empty_file(upload_client):
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("track.wav", io.BytesIO(b""), "audio/wav")},
    )
    assert r.status_code == 422
    assert "empty" in r.json()["detail"].lower()


def test_upload_mp3_returns_job_id(upload_client):
    data = io.BytesIO(b"ID3" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("my_track.mp3", data, "audio/mpeg")},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()
    assert len(r.json()["job_id"]) == 12


def test_upload_wav_returns_job_id(upload_client):
    data = io.BytesIO(b"RIFF" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("my_track.wav", data, "audio/wav")},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_upload_flac_returns_job_id(upload_client):
    data = io.BytesIO(b"fLaC" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("my_track.flac", data, "audio/flac")},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


# ─── Sections endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def done_job(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job


def test_sections_happy_path(client, done_job, tmp_path):
    payload = {
        "sections": [{"id": "sec1", "name": "Verse", "start": 0.0, "end": 30.0, "color": "#ff0000"}]
    }
    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == done_job.id
    assert len(body["sections"]) == 1
    assert body["sections"][0]["name"] == "Verse"
    # Verify written to disk
    meta_path = tmp_path / done_job.id / "metadata.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["sections"][0]["id"] == "sec1"


def test_sections_unknown_job_returns_404(client):
    payload = {"sections": []}
    r = client.patch("/api/jobs/000000000000/sections", json=payload)
    assert r.status_code == 404


def test_sections_malformed_job_id_returns_404(client):
    # Job IDs must be 12 lowercase hex chars; anything else is rejected.
    r = client.patch("/api/jobs/BADID/sections", json={"sections": []})
    assert r.status_code == 404


def test_sections_invalid_color_returns_422(client, done_job):
    payload = {
        "sections": [
            {"id": "sec1", "name": "Intro", "start": 0.0, "end": 10.0, "color": "not-a-color"}
        ]
    }
    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)
    assert r.status_code == 422


def test_sections_invalid_id_returns_422(client, done_job):
    payload = {
        "sections": [{"id": "has space", "name": "x", "start": 0.0, "end": 5.0, "color": "#fff"}]
    }
    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)
    assert r.status_code == 422


# ─── SSE job_id validation ────────────────────────────────────────────────────


def test_sse_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0"):
        r = client.get(f"/api/jobs/{bad_id}/events")
        assert r.status_code == 404, f"SSE should 404 for id {bad_id!r}"


def test_sse_503_when_connection_cap_reached(client):
    """#86/#88: SSE endpoint rejects with 503 when _MAX_SSE_CONNECTIONS is reached."""
    import app.api.events as events_mod

    original = events_mod._sse_active
    try:
        events_mod._sse_active = events_mod._MAX_SSE_CONNECTIONS
        job = Job(id="abcdefabcdef")
        job.status = "done"
        _jobs[job.id] = job
        r = client.get(f"/api/jobs/{job.id}/events")
        assert r.status_code == 503
    finally:
        events_mod._sse_active = original
