"""POST/GET /api/jobs/{id}/lyrics/retime and PUT/DELETE .../lyrics/user-synced.

The worker is a stub script (tests/test_lyrics_retime.py's stub_worker), so
no model runs; the run itself goes through the real endpoint, background task
and pipeline lock."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import lyrics_retime as rt
from tests.test_lyrics_retime import HEARD, HOP, VOICED, entry, stub_worker

JOB_ID = "abcdef123456"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    rt._RUNS.clear()
    yield
    _jobs.clear()
    rt._RUNS.clear()


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def done_job(lyrics: dict | None = None, vocals: bool = True, **fields) -> Job:
    job = Job(id=JOB_ID, status="done", title="W biegu", duration_sec=40.0, **fields)
    job.compute_device = "cuda"
    _jobs[job.id] = job
    job_dir = jobs_mod.JOBS_DIR / job.id
    (job_dir / "stems").mkdir(parents=True)
    if vocals:
        (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
        db = [-80] * int(40 / HOP)
        for a, b in VOICED:
            for f in range(int(a / HOP), int(b / HOP)):
                db[f] = -15
        (job_dir / "stems" / "vocal_envelope.json").write_text(
            json.dumps({"hop": HOP, "db": db}), encoding="utf-8"
        )
    if lyrics is not None:
        (job_dir / "lyrics.json").write_text(json.dumps(lyrics), encoding="utf-8")
    return job


def lyrics_file():
    return jobs_mod.JOBS_DIR / JOB_ID / "lyrics.json"


def wait_for(client, url: str, until=lambda body: body["state"] != "running") -> dict:
    deadline = time.monotonic() + 20
    while True:
        body = client.get(url).json()
        if until(body) or time.monotonic() > deadline:
            return body
        time.sleep(0.05)


RETIME = f"/api/jobs/{JOB_ID}/lyrics/retime"
USER = f"/api/jobs/{JOB_ID}/lyrics/user-synced"


# ── retime ──


def test_a_run_times_the_kept_lyrics(client, monkeypatch, tmp_path):
    done_job(entry())
    stub_worker(monkeypatch, tmp_path, HEARD)
    assert client.get(RETIME).json() == {"state": "idle"}
    r = client.post(RETIME)
    assert r.status_code == 202
    assert r.json()["state"] == "running"
    body = wait_for(client, RETIME)
    assert body["state"] == "done"
    assert body["lines_matched"] == 2 and body["lines"] == 2
    lyrics = client.get(f"/api/jobs/{JOB_ID}/lyrics").json()
    assert lyrics["aligned"]["synced"].startswith("[00:20.00]")
    assert lyrics["synced"] == entry()["synced"]


def test_the_state_after_a_restart_comes_from_the_file(client, monkeypatch, tmp_path):
    done_job(entry())
    stub_worker(monkeypatch, tmp_path, HEARD)
    client.post(RETIME)
    wait_for(client, RETIME)
    rt._RUNS.clear()
    body = client.get(RETIME).json()
    assert body["state"] == "done" and body["lines_matched"] == 2


def test_lyrics_the_browser_keeps_are_answered_not_saved(client, monkeypatch, tmp_path):
    done_job(None)
    stub_worker(monkeypatch, tmp_path, HEARD)
    lrc = entry()["synced"]
    r = client.post(RETIME, json={"synced": lrc, "plain": ""})
    assert r.status_code == 202
    body = wait_for(client, RETIME)
    assert body["state"] == "done"
    assert rt.clean_aligned(body["aligned"], rt.lyric_line_texts({"synced": lrc})) is not None
    assert not lyrics_file().exists()


def test_an_unsure_result_says_so_and_changes_nothing(client, monkeypatch, tmp_path):
    done_job(entry())
    stub_worker(monkeypatch, tmp_path, [{"text": "nothing", "start": 20, "end": 21}])
    before = lyrics_file().read_bytes()
    client.post(RETIME)
    body = wait_for(client, RETIME)
    assert body["state"] == "unsure"
    assert "matched" in body
    assert lyrics_file().read_bytes() == before


def test_a_failed_transcription_is_failed(client):
    done_job(entry())
    client.post(RETIME)
    assert wait_for(client, RETIME)["state"] == "failed"


def test_no_lyrics_is_404_and_no_vocals_or_lines_is_409(client):
    done_job(None)
    assert client.post(RETIME).status_code == 404
    _jobs.clear()
    import shutil

    shutil.rmtree(jobs_mod.JOBS_DIR / JOB_ID)
    done_job(entry(), vocals=False)
    assert client.post(RETIME).status_code == 409
    lyrics_file().write_text(
        json.dumps(entry(synced="", plain="", instrumental=True)), encoding="utf-8"
    )
    assert client.post(RETIME).status_code == 409


def test_a_job_not_done_is_409(client):
    job = done_job(entry())
    job.status = "running"
    assert client.post(RETIME).status_code == 409


def test_a_body_that_is_not_lyrics_is_422(client):
    done_job(entry())
    assert client.post(RETIME, json={"synced": 5}).status_code == 422
    assert client.post(RETIME, content=b"[1]").status_code == 422
    assert client.post(RETIME, json={"synced": "x" * 100_001}).status_code == 422


@pytest.mark.parametrize("route", [RETIME, USER])
@pytest.mark.parametrize("job_id", ["nope", "ABCDEF123456", "..%2F..%2Fetc", "000000000000"])
def test_unknown_or_malformed_jobs_are_404(client, route, job_id):
    url = route.replace(JOB_ID, job_id)
    assert client.get(url).status_code in (404, 405)
    assert client.post(url).status_code in (404, 405)
    assert client.put(url, json={"synced": "[00:01.00]a"}).status_code in (404, 405)
    assert client.delete(url).status_code in (404, 405)


def test_a_traversing_id_never_reaches_the_disk(client):
    for method in ("get", "post", "put", "delete"):
        r = getattr(client, method)("/api/jobs/../../etc/passwd/lyrics/retime")
        assert r.status_code in (404, 405)


def hold_the_lock(client, monkeypatch) -> asyncio.Semaphore:
    """The pipeline lock as busy: a separation is running."""
    lock = client.portal.call(lambda: _make_semaphore())
    monkeypatch.setattr(jobs_mod, "_pipeline_lock", lock)
    return lock


async def _make_semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(0)


def test_a_run_waits_for_the_pipeline_lock(client, monkeypatch, tmp_path):
    done_job(entry())
    stub_worker(monkeypatch, tmp_path, HEARD)
    lock = hold_the_lock(client, monkeypatch)
    assert client.post(RETIME).status_code == 202
    time.sleep(0.3)
    body = client.get(RETIME).json()
    assert body == {"state": "running", "progress": 0.0}
    assert "aligned" not in json.loads(lyrics_file().read_text(encoding="utf-8"))
    # One at a time per track.
    assert client.post(RETIME).status_code == 409
    client.portal.call(_release, lock)
    assert wait_for(client, RETIME)["state"] == "done"


async def _release(lock: asyncio.Semaphore) -> None:
    lock.release()


def test_a_run_waiting_for_the_lock_is_cancelled_at_once(client, monkeypatch, tmp_path):
    done_job(entry())
    stub_worker(monkeypatch, tmp_path, HEARD)
    lock = hold_the_lock(client, monkeypatch)
    client.post(RETIME)
    client.post(f"/api/jobs/{JOB_ID}/cancel")
    assert client.get(RETIME).json()["state"] == "idle"
    # It never runs, even once the lock is free, and the job is still done.
    client.portal.call(_release, lock)
    time.sleep(0.3)
    assert client.get(RETIME).json()["state"] == "idle"
    assert "aligned" not in json.loads(lyrics_file().read_text(encoding="utf-8"))
    assert client.get(f"/api/jobs/{JOB_ID}").json()["status"] == "done"


def test_a_running_transcription_is_cancelled(client, monkeypatch):
    done_job(entry())
    monkeypatch.setattr(
        "app.pipeline.transcribe._spawn_worker_cmd",
        lambda vocals, device: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    before = lyrics_file().read_bytes()
    client.post(RETIME)
    deadline = time.monotonic() + 10
    while rt.current_run(JOB_ID).started is False and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    started = time.monotonic()
    client.post(f"/api/jobs/{JOB_ID}/cancel")
    assert wait_for(client, RETIME)["state"] == "idle"
    assert time.monotonic() - started < 10
    assert lyrics_file().read_bytes() == before


def test_deleting_the_job_stops_its_run(client, monkeypatch):
    done_job(entry())
    monkeypatch.setattr(
        "app.pipeline.transcribe._spawn_worker_cmd",
        lambda vocals, device: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    client.post(RETIME)
    time.sleep(0.5)
    assert client.delete(f"/api/jobs/{JOB_ID}").status_code == 200
    assert rt.current_run(JOB_ID) is None


def test_deleting_waits_for_the_run_to_let_go_of_the_vocals(client, monkeypatch):
    """On Windows a file the transcription still has open cannot be removed:
    the folder goes only once the run has ended."""
    done_job(entry())
    monkeypatch.setattr(
        "app.pipeline.transcribe._spawn_worker_cmd",
        lambda vocals, device: [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    client.post(RETIME)
    deadline = time.monotonic() + 10
    while not rt.current_run(JOB_ID).started and time.monotonic() < deadline:
        time.sleep(0.05)
    run = rt.current_run(JOB_ID)
    seen = {}
    real_rmtree = jobs_mod._rmtree_job

    def rmtree(job_id):
        seen["ended"] = run.ended.is_set()
        return real_rmtree(job_id)

    monkeypatch.setattr(jobs_mod, "_rmtree_job", rmtree)
    assert client.delete(f"/api/jobs/{JOB_ID}").status_code == 200
    assert seen == {"ended": True}


def test_deleting_stops_a_fingerprint_before_removing_the_files(client, monkeypatch):
    """Found in review: a fingerprint of the track's audio could still have a
    file open, and on Windows the delete then failed."""
    done_job(entry())
    order = []
    real_rmtree = jobs_mod._rmtree_job
    monkeypatch.setattr(jobs_mod, "release_source", lambda job_id: order.append("released"))

    def rmtree(job_id):
        order.append("removed")
        return real_rmtree(job_id)

    monkeypatch.setattr(jobs_mod, "_rmtree_job", rmtree)
    assert client.delete(f"/api/jobs/{JOB_ID}").status_code == 200
    assert order == ["released", "removed"]


# ── user-synced ──

GOOD = "[00:20.00]znowu to samo\n[00:24.00]\n[00:25.00]<00:25.00>chciałabym <00:25.60>bardzo"


def test_a_manual_timing_is_kept_and_served(client):
    done_job(entry())
    r = client.put(USER, json={"synced": GOOD})
    assert r.status_code == 200
    assert r.json()["user_synced"] == GOOD
    lyrics = client.get(f"/api/jobs/{JOB_ID}/lyrics").json()
    assert lyrics["user_synced"] == GOOD
    assert lyrics["synced"] == entry()["synced"]


def test_a_manual_timing_is_dropped(client):
    done_job(entry(user_synced=GOOD))
    assert client.get(f"/api/jobs/{JOB_ID}/lyrics").json()["user_synced"] == GOOD
    r = client.delete(USER)
    assert r.status_code == 200
    assert "user_synced" not in client.get(f"/api/jobs/{JOB_ID}/lyrics").json()
    assert client.delete(USER).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"synced": "[00:20.00]znowu to samo\n[00:25.00]<b>other text</b>"},
        {"synced": "[00:25.00]znowu to samo\n[00:20.00]chciałabym bardzo"},
        {"synced": "not lrc at all"},
        {"synced": ""},
        {"synced": 5},
        {},
        [GOOD],
    ],
)
def test_anything_but_a_timing_of_these_lines_is_422(client, body):
    done_job(entry())
    before = lyrics_file().read_bytes()
    r = client.put(USER, json=body)
    assert r.status_code == 422
    assert "other text" not in r.text
    assert lyrics_file().read_bytes() == before


def test_a_manual_timing_too_large_is_413(client):
    done_job(entry())
    r = client.put(USER, content=b'{"synced": "' + b"a" * 2_000_000 + b'"}')
    assert r.status_code == 413


def test_a_manual_timing_without_lyrics_is_404(client):
    done_job(None)
    assert client.put(USER, json={"synced": GOOD}).status_code == 404
    assert client.delete(USER).status_code == 404


def test_a_timing_run_keeps_the_manual_timing(client, monkeypatch, tmp_path):
    done_job(entry(user_synced=GOOD))
    stub_worker(monkeypatch, tmp_path, HEARD)
    client.post(RETIME)
    assert wait_for(client, RETIME)["state"] == "done"
    lyrics = client.get(f"/api/jobs/{JOB_ID}/lyrics").json()
    assert lyrics["user_synced"] == GOOD and "aligned" in lyrics
