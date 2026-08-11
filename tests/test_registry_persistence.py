from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry as _registry
from app.core.models import Job
from app.core.registry import _jobs
from app.core.registry import persist as persist_registry
from app.core.registry import restore as restore_registry


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    _registry._pending_resume.clear()
    yield
    _jobs.clear()
    _registry._pending_resume.clear()


# ── resuming a queue across a restart ────────────────────────────────────────


def _stems_dir(tmp_path: Path, job_id: str, names=("vocals", "drums")) -> None:
    d = tmp_path / job_id / "stems"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.wav").write_bytes(b"RIFF")


def test_a_queued_job_survives_a_restart(tmp_path: Path):
    """Only done jobs used to be persisted, so closing the app silently threw
    away everything the user had queued."""
    job = Job(id="abcdef000001", status="queued", title="Waiting", source_url="local:Waiting")
    _jobs[job.id] = job

    persist_registry(tmp_path)
    _jobs.clear()
    restore_registry(tmp_path)

    assert _jobs[job.id].status == "queued"
    assert _registry.take_pending_resume() == [job.id]


def test_take_pending_resume_only_fires_once(tmp_path: Path):
    _jobs["abcdef000001"] = Job(id="abcdef000001", status="queued", title="Waiting")
    persist_registry(tmp_path)
    _jobs.clear()
    restore_registry(tmp_path)

    assert _registry.take_pending_resume() == ["abcdef000001"]
    assert _registry.take_pending_resume() == []


def test_an_interrupted_job_whose_stems_landed_is_done_not_rerun(tmp_path: Path):
    """The crash window between the last stem being written and the done-persist.
    Re-running would duplicate the library entry and redo the whole separation."""
    job = Job(id="abcdef000002", status="separating", title="Nearly done")
    _jobs[job.id] = job
    persist_registry(tmp_path)
    _stems_dir(tmp_path, job.id)
    _jobs.clear()

    restore_registry(tmp_path)

    assert _jobs[job.id].status == "done"
    assert _registry.take_pending_resume() == []


def test_an_interrupted_job_without_stems_is_requeued(tmp_path: Path):
    job = Job(id="abcdef000003", status="separating", title="Half done", progress=0.6)
    _jobs[job.id] = job
    persist_registry(tmp_path)
    _jobs.clear()

    restore_registry(tmp_path)

    restored = _jobs[job.id]
    assert restored.status == "queued"
    assert restored.progress == 0.0
    assert restored.resume_attempts == 1
    assert _registry.take_pending_resume() == [job.id]


def test_partial_demucs_output_is_cleared_before_a_resume(tmp_path: Path):
    """collect() would otherwise mistake a half-written model dir for results."""
    from app.core.config import DEMUCS_MODEL

    job = Job(id="abcdef000004", status="separating", title="Half done")
    _jobs[job.id] = job
    persist_registry(tmp_path)
    partial = tmp_path / job.id / DEMUCS_MODEL / "track"
    partial.mkdir(parents=True)
    (partial / "vocals.wav").write_bytes(b"partial")
    _jobs.clear()

    restore_registry(tmp_path)

    assert not (tmp_path / job.id / DEMUCS_MODEL).exists()


def test_a_job_interrupted_twice_fails_instead_of_looping(tmp_path: Path):
    """A job that reliably takes the process down would otherwise be re-queued
    on every start, wedging the queue forever."""
    job = Job(id="abcdef000005", status="separating", title="Poison", resume_attempts=1)
    _jobs[job.id] = job
    persist_registry(tmp_path)
    _jobs.clear()

    restore_registry(tmp_path)

    assert _jobs[job.id].status == "error"
    assert "again" in (_jobs[job.id].error or "")
    assert _registry.take_pending_resume() == []


def test_resumed_jobs_keep_their_original_order(tmp_path: Path):
    for i, jid in enumerate(("abcdef00000a", "abcdef00000b", "abcdef00000c")):
        _jobs[jid] = Job(id=jid, status="queued", title=f"T{i}", created_at=100.0 + i)
    persist_registry(tmp_path)
    _jobs.clear()

    restore_registry(tmp_path)

    assert _registry.take_pending_resume() == ["abcdef00000a", "abcdef00000b", "abcdef00000c"]


def test_cancelled_and_errored_jobs_are_still_not_persisted(tmp_path: Path):
    """Widening persistence to cover the queue must not resurrect dead jobs."""
    _jobs["abcdef000006"] = Job(id="abcdef000006", status="cancelled", title="Nope")
    _jobs["abcdef000007"] = Job(id="abcdef000007", status="error", title="Nope")
    persist_registry(tmp_path)
    _jobs.clear()

    restore_registry(tmp_path)

    assert _jobs == {}


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
