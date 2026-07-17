from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs, _procs
from app.core.registry import reset_all as reset_registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    _procs.clear()
    yield
    _jobs.clear()
    _procs.clear()


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


# ─── registry.reset_all() ──────────────────────────────────────────────────


def test_reset_all_clears_registry_and_deletes_job_dirs(tmp_path):
    job = Job(id="abcdefabcdef", status="done", title="Old song")
    _jobs[job.id] = job
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
    (tmp_path / "registry.json").write_text(json.dumps({"version": 1, "jobs": [job.to_record()]}))
    (tmp_path / "failed").mkdir()
    (tmp_path / "failed" / "somejob").mkdir()

    reset_registry(tmp_path)

    assert _jobs == {}
    assert list(tmp_path.iterdir()) == []  # every entry under jobs_dir is gone


def test_reset_all_clears_active_procs(tmp_path):
    from unittest.mock import MagicMock

    _procs["abcdefabcdef"] = MagicMock()
    reset_registry(tmp_path)
    assert _procs == {}


def test_reset_all_on_missing_dir_is_a_noop(tmp_path):
    missing = tmp_path / "does-not-exist"
    reset_registry(missing)  # must not raise


# ─── POST /api/reset ────────────────────────────────────────────────────────


def test_reset_endpoint_requires_desktop_mode(client, monkeypatch, tmp_path):
    monkeypatch.delenv("STEMDECK_DESKTOP", raising=False)
    monkeypatch.setattr("app.main.JOBS_DIR", tmp_path)

    r = client.post("/api/reset")

    assert r.status_code == 403


def test_reset_endpoint_rejects_active_job(client, monkeypatch, tmp_path):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setattr("app.main.JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdee", status="separating")
    _jobs[job.id] = job

    r = client.post("/api/reset")

    assert r.status_code == 409
    assert job.id in _jobs  # nothing was touched


def test_reset_endpoint_succeeds_in_desktop_mode(client, monkeypatch, tmp_path):
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setattr("app.main.JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdea", status="done")
    _jobs[job.id] = job
    (tmp_path / job.id).mkdir()

    r = client.post("/api/reset")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert _jobs == {}
    assert not (tmp_path / job.id).exists()


def test_reset_endpoint_allows_only_terminal_jobs(client, monkeypatch, tmp_path):
    """done/error/cancelled jobs never block a reset -- only genuinely active
    ones (queued through processing) do."""
    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setattr("app.main.JOBS_DIR", tmp_path)
    for i, status in enumerate(("done", "error", "cancelled")):
        _jobs[f"abcdefabcd{i:02x}"] = Job(id=f"abcdefabcd{i:02x}", status=status)

    r = client.post("/api/reset")

    assert r.status_code == 200
