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
