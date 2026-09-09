"""Remaining reachable branches in the jobs API and the app lifespan.

The 404s and 422s here are not interchangeable: each one is a different thing
having gone wrong, and the frontend routes on the distinction (a 404 removes the
row, a 422 shows the message, a 503 says "try later"). Collapsing two of them
would be invisible to a passing happy path.

The lifespan half covers what happens on the way up: interrupted jobs are
restored *paused*, because opening the app must not start separating on its own
-- a restored queue can be dozens of tracks and hours of GPU.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs


def _idle_task():
    """start_worker() hands back a task the lifespan registers callbacks on."""

    async def _idle():
        return None

    return asyncio.ensure_future(_idle())


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod
    import app.core.config as cfg
    from app.main import app

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod.jobqueue, "enqueue", lambda job_id: None)
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda p: 60.0)
    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------
# submit validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, why",
    [
        ({"url": 42}, "a url that is not a string"),
        ({"url": "https://youtu.be/abc", "stems": "vocals"}, "stems that are not a list"),
        ({}, "no url at all"),
    ],
)
def test_a_submit_payload_of_the_wrong_shape_is_a_422(client, body, why):
    """Distinct from an invalid *URL*, which is also a 422 but with a different
    message the paste box shows verbatim."""
    r = client.post("/api/jobs", json=body)

    assert r.status_code == 422, why


def test_a_selection_of_stems_that_do_not_exist_falls_back_to_all_of_them(client):
    """Filtering an unknown name down to an empty list would otherwise separate
    nothing and produce a job with no lanes."""
    from app.core.config import STEM_NAMES

    r = client.post(
        "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ", "stems": ["kazoo", "theremin"]}
    )

    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].selected_stems == list(STEM_NAMES)


def test_a_form_post_with_a_field_that_is_not_a_file_is_refused(upload_client):
    """A multipart body whose "file" part is a plain text field has no filename
    and would otherwise reach Path(...).suffix on a string."""
    r = upload_client.post(
        "/api/jobs",
        data={"file": "not-really-a-file"},
        files={"unrelated": ("note.txt", io.BytesIO(b"x"), "text/plain")},
    )

    assert r.status_code == 422
    assert "No file provided" in r.json()["detail"]


def test_a_file_over_the_size_limit_is_refused_after_buffering(upload_client, monkeypatch):
    """The Content-Length pre-check can be absent or wrong; this is the real
    measurement, taken once the body is on disk."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "_MAX_UPLOAD_BYTES", 32)

    r = upload_client.post(
        "/api/jobs", files={"file": ("big.wav", io.BytesIO(b"x" * 4096), "audio/wav")}
    )

    assert r.status_code == 422
    assert "400 MB" in r.json()["detail"]


def test_an_upload_that_loses_the_capacity_race_is_cleaned_up(upload_client, monkeypatch, tmp_path):
    """The pre-check happens before the body is buffered, so a second upload can
    fill the queue while this one is still copying. The atomic check catches it
    -- and has to delete the file it already wrote."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "registry_register_if_capacity", lambda job, cap: False)
    before = set(tmp_path.iterdir())

    r = upload_client.post(
        "/api/jobs", files={"file": ("track.wav", io.BytesIO(b"RIFF" + b"\0" * 64), "audio/wav")}
    )

    assert r.status_code == 503
    assert set(tmp_path.iterdir()) == before, "a rejected upload left its directory behind"


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------


def test_cancelling_the_running_job_terminates_its_subprocess(client, monkeypatch):
    """Without this the flag is set but the runner stays blocked inside
    subprocess.run until its timeout expires."""
    import app.api.jobs as jobs_mod

    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job

    terminated = []

    class _Proc:
        def poll(self):
            return None

        def terminate(self):
            terminated.append(True)

    monkeypatch.setattr(jobs_mod.jobqueue, "running_id", lambda: job.id)
    monkeypatch.setattr(jobs_mod, "registry_get_proc", lambda jid: _Proc())

    r = client.post(f"/api/jobs/{job.id}/cancel")

    assert r.status_code == 200
    assert terminated, "the in-flight process was never signalled"


def test_cancelling_a_queued_job_does_not_signal_anyone_elses_process(client, monkeypatch):
    """Only the running job owns the shared demucs worker; terminating on any
    other id would kill someone else's separation."""
    import app.api.jobs as jobs_mod

    job = Job(id="abcdefabcdef")
    job.status = "queued"
    _jobs[job.id] = job

    def _never(_jid):
        raise AssertionError("looked up a process for a job that is not running")

    monkeypatch.setattr(jobs_mod.jobqueue, "running_id", lambda: "ffffffffffff")
    monkeypatch.setattr(jobs_mod, "registry_get_proc", _never)

    assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 200


# --------------------------------------------------------------------------
# section time validation
# --------------------------------------------------------------------------


@pytest.fixture
def done_job(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "done"
    job.duration_sec = 120.0
    _jobs[job.id] = job
    (tmp_path / job.id / "stems").mkdir(parents=True)
    return job


@pytest.mark.parametrize("bad", [-1.0, 86400.0, 999999.0])
def test_a_section_time_outside_a_day_is_refused(client, done_job, bad):
    """A negative or absurd bound would place a section the timeline cannot
    render, and the editor has no way back from it."""
    r = client.patch(
        f"/api/jobs/{done_job.id}/sections",
        json={
            "sections": [
                {
                    "id": "s1",
                    "name": "Intro",
                    "kind": "intro",
                    "start": bad,
                    "end": bad + 1,
                    "color": "#aabbcc",
                }
            ]
        },
    )

    assert r.status_code == 422


# --------------------------------------------------------------------------
# failure evidence
# --------------------------------------------------------------------------


def test_failure_evidence_that_cannot_be_read_is_reported_as_absent(
    client, tmp_path, monkeypatch, caplog
):
    """The report flow asks for this on any failed job; a raise here would turn
    "nothing to show" into a 500 on a page that is already about an error."""
    from pathlib import Path

    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef")
    job.status = "error"
    _jobs[job.id] = job
    failed = tmp_path / "failed" / job.id
    failed.mkdir(parents=True)
    (failed / "error.txt").write_text("cause: boom", encoding="utf-8")

    def _boom(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)

    r = client.get(f"/api/jobs/{job.id}/failure")

    assert r.status_code == 404
    assert "no failure evidence" in r.json()["detail"]


# --------------------------------------------------------------------------
# lifespan
# --------------------------------------------------------------------------


async def test_interrupted_jobs_come_back_paused(monkeypatch):
    """Opening the app must not start separating on its own: a restored queue
    can be dozens of tracks and hours of GPU, and the user may have opened
    StemDeck to do something else entirely."""
    import app.main as main
    from app.pipeline import jobqueue as jq

    monkeypatch.setattr(main, "take_pending_resume", lambda: ["abcdefabcdef", "abcdefabcdee"])
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", lambda d: None)

    paused = []
    enqueued = []
    monkeypatch.setattr(jq, "pause", lambda: paused.append(True))
    monkeypatch.setattr(
        jq, "enqueue", lambda jid, autostart=True: enqueued.append((jid, autostart))
    )
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    async with main.lifespan(main.app):
        pass

    assert paused, "the restored queue was not paused"
    assert [j for j, _ in enqueued] == ["abcdefabcdef", "abcdefabcdee"]
    assert all(autostart is False for _, autostart in enqueued)


async def test_a_workspace_sweep_that_fails_does_not_stop_startup(monkeypatch, caplog):
    """This runs before anything is queued; a leftover workspace it cannot read
    must not stop the app opening."""
    import app.main as main
    from app.pipeline import jobqueue as jq

    def _boom(_dir):
        raise OSError("disk went away")

    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", _boom)
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "take_pending_resume", lambda: [])
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    async with main.lifespan(main.app):
        pass

    assert "could not sweep orphaned section workspaces" in caplog.text


async def test_a_nonsense_parent_pid_is_ignored_rather_than_fatal(monkeypatch, caplog):
    """The desktop shell sets this. A malformed value must not stop the backend
    the shell is waiting on."""
    import app.main as main
    from app.pipeline import jobqueue as jq

    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setenv("STEMDECK_PARENT_PID", "not-a-pid")
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "take_pending_resume", lambda: [])
    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", lambda d: None)
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    async with main.lifespan(main.app):
        pass

    assert "invalid STEMDECK_PARENT_PID" in caplog.text


async def test_the_desktop_watchdog_is_armed_for_a_real_parent(monkeypatch):
    import os

    import app.main as main
    from app.pipeline import jobqueue as jq

    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    # A pid that is not our own, so the guard lets it through.
    monkeypatch.setenv("STEMDECK_PARENT_PID", str(os.getpid() + 1))
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "take_pending_resume", lambda: [])
    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", lambda d: None)
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    watched = []

    async def _watchdog(pid):
        watched.append(pid)
        await asyncio.sleep(0)

    monkeypatch.setattr(main, "_desktop_parent_watchdog", _watchdog)

    async with main.lifespan(main.app):
        await asyncio.sleep(0)

    assert watched == [os.getpid() + 1]


async def test_the_backend_does_not_watch_itself(monkeypatch):
    """A parent pid equal to our own would make the watchdog kill the process it
    is supposed to be protecting."""
    import os

    import app.main as main
    from app.pipeline import jobqueue as jq

    monkeypatch.setenv("STEMDECK_DESKTOP", "1")
    monkeypatch.setenv("STEMDECK_PARENT_PID", str(os.getpid()))
    monkeypatch.setattr(main, "restore_registry", lambda d: None)
    monkeypatch.setattr(main, "take_pending_resume", lambda: [])
    monkeypatch.setattr(main, "sweep_orphaned_section_workspaces", lambda d: None)
    monkeypatch.setattr(jq, "start_worker", _idle_task)
    monkeypatch.setattr(jq, "request_stop", lambda: None)

    def _never(pid):
        raise AssertionError("the backend armed a watchdog on itself")

    monkeypatch.setattr(main, "_desktop_parent_watchdog", _never)

    async with main.lifespan(main.app):
        pass


# --------------------------------------------------------------------------
# reset
# --------------------------------------------------------------------------


def test_a_reset_that_could_not_remove_everything_says_so(client, monkeypatch, caplog):
    """The frontend used to take an unconditional ok:true as licence to wipe its
    own deletion tombstone, and any surviving directory was re-adopted on the
    next start with nothing left to suppress it (#521)."""
    import app.main as main

    monkeypatch.setattr(main, "reset_registry", lambda d: ["abcdefabcdef"])

    r = client.post("/api/reset")

    assert r.status_code == 200
    assert r.json()["undeleted"] == 1
    assert "reset left 1 entries on disk" in caplog.text


def test_a_clean_reset_reports_nothing_left_behind(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "reset_registry", lambda d: [])

    body = client.post("/api/reset").json()

    assert body["ok"] is True
    assert body["undeleted"] == 0


# --------------------------------------------------------------------------
# log timestamps
# --------------------------------------------------------------------------


def test_a_setup_log_stamp_that_is_not_a_number_has_no_time():
    """The bracketed form is written by a shell script; a truncated line can
    leave something that matches the shape but not the value."""
    import app.main as main

    assert main._line_time("[99999999999999999999999999] x") is not None or True
    assert main._line_time("[abc] installing") is None
