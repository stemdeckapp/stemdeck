"""Trash lives on the server, so both UIs agree on what the library contains.

It used to live only in the browser's catalog store. That store is per-device,
so a track the user deleted on their desktop was still returned by
GET /api/jobs, and the phone UI -- which builds its entire library from that
endpoint -- listed everything they thought they had thrown away. Two clients,
two answers to "what is in my library", and the phone's answer was the wrong
one in the direction that matters.

Trashing is deliberately not deleting: stems stay on disk and the job stays in
the registry, so restore costs nothing and a mistaken tap never destroys audio.
Only emptying the Trash calls DELETE.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs, register
from app.main import app


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test gets a fresh in-memory registry, as everywhere else here.

    The registry is module-global and is restored from the real jobs directory
    at import, so without this a developer's own library leaks into the
    assertions.
    """
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _done(job_id: str, title: str) -> Job:
    job = Job(id=job_id, status="done", title=title)
    register(job)
    return job


def _ids(resp) -> list[str]:
    return [j["job_id"] for j in resp.json()]


def test_a_trashed_job_leaves_the_library(client: TestClient) -> None:
    _done("aaaaaaaaaaaa", "Keep")
    _done("bbbbbbbbbbbb", "Bin")
    assert client.post("/api/jobs/bbbbbbbbbbbb/trash").status_code == 200
    assert _ids(client.get("/api/jobs")) == ["aaaaaaaaaaaa"]


def test_the_phone_and_the_desktop_now_see_the_same_list(client: TestClient) -> None:
    """The actual bug: the mobile UI reads this endpoint and nothing else."""
    _done("cccccccccccc", "Kept")
    _done("dddddddddddd", "Deleted on the desktop")
    client.post("/api/jobs/dddddddddddd/trash")
    # Whatever any browser has in local storage, this is the one answer.
    assert _ids(client.get("/api/jobs")) == ["cccccccccccc"]


def test_the_trash_itself_can_be_listed(client: TestClient) -> None:
    _done("eeeeeeeeeeee", "Kept")
    _done("ffffffffffff", "Binned")
    client.post("/api/jobs/ffffffffffff/trash")
    assert _ids(client.get("/api/jobs?trashed=only")) == ["ffffffffffff"]
    assert set(_ids(client.get("/api/jobs?trashed=include"))) == {
        "eeeeeeeeeeee",
        "ffffffffffff",
    }


def test_restore_puts_it_back(client: TestClient) -> None:
    _done("111111111111", "Oops")
    client.post("/api/jobs/111111111111/trash")
    assert _ids(client.get("/api/jobs")) == []
    assert client.post("/api/jobs/111111111111/restore").status_code == 200
    assert _ids(client.get("/api/jobs")) == ["111111111111"]


def test_trashing_records_when(client: TestClient) -> None:
    """A timestamp, not a flag, so the Trash can say how old something is."""
    _done("222222222222", "Timed")
    body = client.post("/api/jobs/222222222222/trash").json()
    assert isinstance(body["trashed_at"], float)
    assert body["trashed_at"] > 0
    assert client.post("/api/jobs/222222222222/restore").json()["trashed_at"] is None


def test_trashing_does_not_delete_the_job(client: TestClient) -> None:
    """The reversibility this whole design depends on."""
    _done("333333333333", "Still here")
    client.post("/api/jobs/333333333333/trash")
    # Gone from the library, still fully addressable.
    assert client.get("/api/jobs/333333333333").status_code == 200
    assert client.get("/api/jobs/333333333333").json()["trashed_at"] is not None


def test_trashing_is_idempotent(client: TestClient) -> None:
    """Two devices can both decide to bin the same track."""
    _done("444444444444", "Twice")
    first = client.post("/api/jobs/444444444444/trash").json()["trashed_at"]
    second = client.post("/api/jobs/444444444444/trash").json()["trashed_at"]
    assert first is not None and second is not None
    assert _ids(client.get("/api/jobs")) == []


def test_an_unknown_job_is_a_404_not_a_500(client: TestClient) -> None:
    assert client.post("/api/jobs/999999999999/trash").status_code == 404
    assert client.post("/api/jobs/999999999999/restore").status_code == 404


def test_a_malformed_id_never_reaches_the_registry(client: TestClient) -> None:
    """Rejected, and never a 500 or a silent success.

    Not pinned to 404: a traversal attempt normalises away before routing and
    comes back 405, which is the router refusing to dispatch it at all. What
    matters is that nothing is trashed and nothing blows up.
    """
    _done("777777777777", "Untouched")
    for bad in ("../../etc/passwd", "not a job id", "%2e%2e%2f"):
        resp = client.post(f"/api/jobs/{bad}/trash")
        assert 400 <= resp.status_code < 500, (bad, resp.status_code)
    assert _ids(client.get("/api/jobs")) == ["777777777777"]


def test_unfinished_jobs_are_not_listed_either_way(client: TestClient) -> None:
    """The library is finished tracks; the filter must not change that."""
    register(Job(id="555555555555", status="queued", title="Waiting"))
    assert _ids(client.get("/api/jobs")) == []
    assert _ids(client.get("/api/jobs?trashed=include")) == []


def test_an_unknown_filter_value_is_rejected(client: TestClient) -> None:
    assert client.get("/api/jobs?trashed=maybe").status_code == 422


def test_a_registry_written_before_this_existed_still_loads() -> None:
    """Upgrades must not trip over a record with no trashed_at key."""
    record = Job(id="666666666666", status="done", title="Old").to_record()
    del record["trashed_at"]
    assert Job.from_record(record).trashed_at is None


# ── trashing a job that has not finished (#748) ─────────────────────────────
#
# Trashing used to leave an unfinished import running, hidden: the queue kept
# working on a song the user had thrown away. The server now stops it, so the
# phone and the desktop both get it. Its files stay: only emptying the Trash
# deletes anything.


class _FakeProc:
    """Stands in for the registered subprocess: records the terminate."""

    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True


def _queued_upload(job_id: str) -> tuple[Job, Path]:
    from app.pipeline import jobqueue

    job = Job(id=job_id, status="queued", title="Waiting", source_url="local:Waiting")
    register(job)
    jobqueue.enqueue(job.id, autostart=False)
    job_dir = jobqueue.JOBS_DIR / job.id
    job_dir.mkdir(parents=True)
    (job_dir / "source.wav").write_bytes(b"RIFF")
    return job, job_dir


def test_trashing_a_queued_job_stops_it_and_keeps_its_files(client: TestClient) -> None:
    from app.pipeline import jobqueue

    job, job_dir = _queued_upload("888888888888")

    assert client.post(f"/api/jobs/{job.id}/trash").status_code == 200

    assert job.status == "stopped"
    assert jobqueue.snapshot() == (None, []), "the queue would still run it"
    assert (job_dir / "source.wav").is_file(), "only emptying the Trash deletes files"


def test_a_plain_cancel_still_removes_a_queued_jobs_files(client: TestClient) -> None:
    job, job_dir = _queued_upload("888888888887")

    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 200

    assert job.status == "cancelled"
    assert not job_dir.exists(), "a waiting upload's source must not stay on disk"


def test_emptying_the_trash_deletes_a_stopped_job(client: TestClient) -> None:
    job, job_dir = _queued_upload("888888888886")
    client.post(f"/api/jobs/{job.id}/trash")

    assert client.delete(f"/api/jobs/{job.id}").status_code == 200

    assert not job_dir.exists()


def test_extract_queues_a_restored_stopped_job_again(client: TestClient) -> None:
    from app.core.config import DEMUCS_MODEL
    from app.pipeline import jobqueue

    job, job_dir = _queued_upload("888888888885")
    client.post(f"/api/jobs/{job.id}/trash")
    (job_dir / DEMUCS_MODEL).mkdir()  # partial output from the stopped run
    client.post(f"/api/jobs/{job.id}/restore")

    res = client.post(f"/api/jobs/{job.id}/extract")

    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    assert job.cancel_requested is False and job.stop_requested is False
    assert jobqueue.snapshot() == (None, [job.id])
    assert (job_dir / "source.wav").is_file()
    assert not (job_dir / DEMUCS_MODEL).exists(), "collect() would read stale stems"


def test_restore_alone_does_not_queue_a_stopped_job(client: TestClient) -> None:
    from app.pipeline import jobqueue

    job, _ = _queued_upload("888888888884")
    client.post(f"/api/jobs/{job.id}/trash")

    assert client.post(f"/api/jobs/{job.id}/restore").status_code == 200

    assert job.status == "stopped"
    assert jobqueue.snapshot() == (None, [])


def test_extract_refuses_a_job_still_in_the_trash(client: TestClient) -> None:
    job, _ = _queued_upload("888888888883")
    client.post(f"/api/jobs/{job.id}/trash")

    assert client.post(f"/api/jobs/{job.id}/extract").status_code == 409
    assert job.status == "stopped"


def test_extract_refuses_a_job_that_is_not_stopped(client: TestClient) -> None:
    job = _done("888888888882", "Finished")
    assert client.post(f"/api/jobs/{job.id}/extract").status_code == 409
    assert job.status == "done"


def test_extract_rejects_a_malformed_or_unknown_id(client: TestClient) -> None:
    # A traversal normalises away before routing (405), as for trash above.
    for bad in ("../../etc/passwd", "not a job id", "%2e%2e%2f"):
        resp = client.post(f"/api/jobs/{bad}/extract")
        assert 400 <= resp.status_code < 500, (bad, resp.status_code)
    assert client.post("/api/jobs/aaaaaaaaaaaa/extract").status_code == 404


def test_trashing_the_running_job_stops_its_process(client: TestClient) -> None:
    from app.core.registry import set_proc
    from app.pipeline import jobqueue

    job = Job(id="999999999990", status="analyzing", title="Running")
    register(job)
    jobqueue._set_running(job.id)
    proc = _FakeProc()
    set_proc(job.id, proc)
    try:
        assert client.post(f"/api/jobs/{job.id}/trash").status_code == 200
    finally:
        set_proc(job.id, None)
        jobqueue._set_running(None)

    assert job.cancel_requested is True
    assert job.stop_requested is True, "it would settle as cancelled and lose its files"
    assert proc.terminated, "the pipeline would have kept going in the Trash"
    # Still in the Trash: the runner, not the endpoint, finalises a running job.
    assert job.trashed_at is not None


def test_trashing_a_finished_job_cancels_nothing(client: TestClient) -> None:
    """A done job's files are what Restore brings back."""
    job = _done("999999999991", "Finished")
    client.post(f"/api/jobs/{job.id}/trash")
    assert job.status == "done"
    assert job.cancel_requested is False
