from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs
from app.core.registry import persist as persist_registry
from app.core.registry import restore as restore_registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
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
    job_dir = tmp_path / "abcdefabcdee"
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF")
    (stems_dir / "drums.wav").write_bytes(b"RIFF")
    (job_dir / "metadata.json").write_text(json.dumps({"title": "Test Song"}), encoding="utf-8")

    restore_registry(tmp_path)

    restored = _jobs["abcdefabcdee"]
    assert restored.status == "done"
    assert restored.progress == 1.0
    assert restored.title == "Test Song"
    assert {stem["name"] for stem in restored.stems} == {"vocals", "drums"}


def test_restore_recovers_orphan_without_metadata(tmp_path: Path):
    """#284: a crash between status=done and the metadata write used to leave
    a complete stems dir permanently unrecoverable. Now it comes back with a
    placeholder title, and a minimal metadata.json is written so the next
    restart takes the normal recovery path (self-healing)."""
    job_dir = tmp_path / "abcdefabcde0"
    stems_dir = job_dir / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF")

    restore_registry(tmp_path)

    restored = _jobs["abcdefabcde0"]
    assert restored.status == "done"
    assert restored.title == "Recovered track abcdef"
    assert {stem["name"] for stem in restored.stems} == {"vocals"}
    # Self-healed: metadata.json now exists with the placeholder title.
    meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Recovered track abcdef"


def test_restore_still_ignores_dir_without_stems(tmp_path: Path):
    """The stems requirement stays: an empty/partial job dir is not a track."""
    (tmp_path / "abcdefabcde1" / "stems").mkdir(parents=True)  # no WAVs
    (tmp_path / "abcdefabcde2").mkdir(parents=True)  # no stems dir at all

    restore_registry(tmp_path)

    assert "abcdefabcde1" not in _jobs
    assert "abcdefabcde2" not in _jobs


def test_persist_concurrent_writers_no_corruption(tmp_path: Path):
    """#281: pipeline thread, API threads, and the sweep all call persist()
    concurrently. A shared temp path let writers collide (PermissionError on
    Windows os.replace). Hammer it from threads: no exception, valid JSON,
    no stray temp files."""
    import threading

    for i in range(5):
        job = Job(id=f"abcdefabcd{i:02x}", status="done", title=f"t{i}")
        _jobs[job.id] = job

    errors: list[Exception] = []

    def hammer():
        try:
            for _ in range(30):
                persist_registry(tmp_path)
        except Exception as e:  # pragma: no cover - the failure being tested
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    data = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    assert len(data["jobs"]) == 5
    assert not list(tmp_path.glob("*.tmp")), "no temp files may be left behind"
    assert not list(tmp_path.glob(".registry.*")), "no temp files may be left behind"


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
                title="Test Song",
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
