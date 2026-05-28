from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.api.stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    from app.main import app

    return TestClient(app)


def _make_stem_file(tmp_path, job_id: str, name: str, contents: bytes = b"RIFF"):
    stems_dir = tmp_path / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    path = stems_dir / f"{name}.wav"
    path.write_bytes(contents)
    return path


def test_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde", "abcd-efabcdef"):
        r = client.get(f"/api/jobs/{bad_id}/stems/vocals.wav")
        assert r.status_code == 404, f"id {bad_id!r} should 404"


def test_rejects_unknown_stem_name(client):
    job = Job(id="abcdefabcdef")
    job.status = "done"
    _jobs[job.id] = job
    r = client.get(f"/api/jobs/{job.id}/stems/banjo.wav")
    assert r.status_code == 404


def test_requires_done_status(client, tmp_path):
    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals")
    r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
    assert r.status_code == 404


def test_serves_done_job_stem(client, tmp_path):
    job = Job(id="abcdefabcdee")
    job.status = "done"
    _jobs[job.id] = job
    _make_stem_file(tmp_path, job.id, "vocals", b"RIFF1234")
    r = client.get(f"/api/jobs/{job.id}/stems/vocals.wav")
    assert r.status_code == 200
    assert r.content == b"RIFF1234"
    assert r.headers["content-type"] == "audio/wav"


# --- peaks endpoint ---


def _make_peaks_file(tmp_path, job_id: str, data: dict) -> None:
    stems_dir = tmp_path / job_id / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    (stems_dir / "peaks.json").write_text(json.dumps(data), encoding="utf-8")


def test_peaks_returns_json_for_done_job(client, tmp_path):
    job = Job(id="abcdefabcdea")
    job.status = "done"
    _jobs[job.id] = job
    payload = {"vocals": [[-0.1, 0.2], [-0.3, 0.4]], "drums": [[-0.5, 0.6]]}
    _make_peaks_file(tmp_path, job.id, payload)

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert "immutable" in r.headers.get("cache-control", "")
    assert r.json() == payload


def test_peaks_404_when_file_missing(client, tmp_path):
    job = Job(id="abcdefabcdeb")
    job.status = "done"
    _jobs[job.id] = job
    # stems dir exists but no peaks.json
    (tmp_path / job.id / "stems").mkdir(parents=True, exist_ok=True)

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 404


def test_peaks_404_for_non_done_job(client):
    job = Job(id="abcdefabcdec")
    job.status = "separating"
    _jobs[job.id] = job

    r = client.get(f"/api/jobs/{job.id}/stems/peaks.json")
    assert r.status_code == 404


def test_peaks_rejects_malformed_job_id(client):
    for bad_id in ("../etc", "ABC", "abcdefabcdef0", "abcdefabcde"):
        r = client.get(f"/api/jobs/{bad_id}/stems/peaks.json")
        assert r.status_code == 404, f"id {bad_id!r} should 404"
