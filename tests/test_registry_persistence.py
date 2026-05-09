from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.core.registry import persist as persist_registry
from app.core.registry import restore as restore_registry


def setup_function():
    _jobs.clear()


def teardown_function():
    _jobs.clear()


def test_persist_and_restore_terminal_job(tmp_path: Path):
    job = Job(
        id="abcdefabcdef",
        status="done",
        progress=1.0,
        stage_message="Done",
        title="Saved song",
        stems=[{"name": "vocals", "url": "/api/jobs/abcdefabcdef/stems/vocals.wav"}],
        selected_stems=["vocals"],
    )
    _jobs[job.id] = job

    persist_registry(tmp_path)
    _jobs.clear()
    restore_registry(tmp_path)

    restored = _jobs[job.id]
    assert restored.status == "done"
    assert restored.title == "Saved song"
    assert restored.stems == job.stems
    assert restored.cancel_requested is False


def test_restore_recovers_orphan_done_job_from_stems(tmp_path: Path):
    stems_dir = tmp_path / "abcdefabcdee" / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF")
    (stems_dir / "drums.wav").write_bytes(b"RIFF")

    restore_registry(tmp_path)

    restored = _jobs["abcdefabcdee"]
    assert restored.status == "done"
    assert restored.progress == 1.0
    assert {stem["name"] for stem in restored.stems} == {"vocals", "drums"}


def test_restored_job_serves_stems(tmp_path: Path, monkeypatch):
    stems_dir = tmp_path / "abcdefabcded" / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF1234")
    data = {
        "version": 1,
        "jobs": [
            Job(
                id="abcdefabcded",
                status="done",
                stems=[{"name": "vocals", "url": "/api/jobs/abcdefabcded/stems/vocals.wav"}],
                selected_stems=["vocals"],
            ).to_record()
        ],
    }
    (tmp_path / "registry.json").write_text(json.dumps(data), encoding="utf-8")

    monkeypatch.setattr("app.api.stems.JOBS_DIR", tmp_path)
    restore_registry(tmp_path)

    from app.main import app

    with TestClient(app) as client:
        state = client.get("/api/jobs/abcdefabcded")
        assert state.status_code == 200
        assert state.json()["status"] == "done"
        stem = client.get("/api/jobs/abcdefabcded/stems/vocals.wav")
        assert stem.status_code == 200
        assert stem.content == b"RIFF1234"


def test_delete_updates_persisted_registry(tmp_path: Path, monkeypatch):
    job = Job(id="abcdefabcdec", status="done")
    _jobs[job.id] = job
    job_dir = tmp_path / job.id
    job_dir.mkdir(parents=True)
    persist_registry(tmp_path)

    monkeypatch.setattr("app.api.jobs.JOBS_DIR", tmp_path)

    from app.main import app

    with TestClient(app) as client:
        response = client.delete(f"/api/jobs/{job.id}")

    assert response.status_code == 200
    assert not job_dir.exists()
    data = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert data["jobs"] == []
