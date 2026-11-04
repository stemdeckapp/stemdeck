from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import MAX_PENDING_UPLOAD_JOBS, MAX_PENDING_URL_JOBS, STEM_NAMES
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
    # Stub the enqueue rather than the pipeline: these tests cover the submit
    # API, and letting the real worker drain the queue would free capacity
    # mid-test and break the 503 cases. Execution is covered by
    # tests/test_queue_worker.py.
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)

    with (
        patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None),
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


# ─── Cancelling before a job starts ──────────────────────────────────────────


def test_cancel_while_waiting_finalises_without_running(client, tmp_path, monkeypatch):
    """A queued job used to honour cancel only when its turn arrived: it kept a
    capacity slot until then, and a queued upload held its source file the whole
    time."""
    from app.pipeline import jobqueue

    monkeypatch.setattr(jobqueue, "JOBS_DIR", tmp_path)
    job = Job(id="aaaaaaaaaaaa")
    _jobs[job.id] = job
    (tmp_path / job.id).mkdir()
    (tmp_path / job.id / "source.mp3").write_bytes(b"ID3")
    # Straight into the deque: the client fixture stubs enqueue(), so calling it
    # here would be a no-op.
    jobqueue._queue.append(job.id)

    r = client.post(f"/api/jobs/{job.id}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert jobqueue.depth() == 0
    assert not (tmp_path / job.id).exists(), "a queued upload must not keep its source"


def test_cancel_while_waiting_frees_a_capacity_slot(client):
    from app.pipeline import jobqueue

    job = Job(id="aaaaaaaaaaaa")
    _jobs[job.id] = job
    jobqueue._queue.append(job.id)  # enqueue() is stubbed by the client fixture
    client.post(f"/api/jobs/{job.id}/cancel")
    assert sum(1 for j in _jobs.values() if j.status == "queued") == 0


def test_a_running_job_does_not_consume_a_queue_slot(client):
    """Capacity counts waiting jobs only, so the running one never blocks a
    submit. This is what makes the limit mean "queue depth"."""
    running = Job(id="aaaaaaaaaaaa")
    running.status = "processing"
    _jobs[running.id] = running
    for _ in range(MAX_PENDING_URL_JOBS):
        assert (
            client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}).status_code
            == 200
        )


# ─── Capacity (503) ───────────────────────────────────────────────────────────


def test_youtube_503_when_queue_full(client):
    for _ in range(MAX_PENDING_URL_JOBS):
        r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
        assert r.status_code == 200
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 503


def test_upload_503_when_upload_queue_full(upload_client):
    for i in range(MAX_PENDING_UPLOAD_JOBS):
        data = io.BytesIO(b"ID3" + b"\x00" * 128)
        r = upload_client.post("/api/jobs", files={"file": (f"track{i}.mp3", data, "audio/mpeg")})
        assert r.status_code == 200
    data = io.BytesIO(b"ID3" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("one-too-many.mp3", data, "audio/mpeg")},
    )
    assert r.status_code == 503


def test_a_full_link_queue_does_not_block_an_upload(upload_client):
    """The two are bounded separately: a waiting upload holds its source file
    on disk, a waiting link holds nothing, so a big playlist must not lock the
    user out of importing a file."""
    for _ in range(MAX_PENDING_URL_JOBS):
        assert (
            upload_client.post(
                "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
            ).status_code
            == 200
        )
    data = io.BytesIO(b"ID3" + b"\x00" * 128)
    r = upload_client.post("/api/jobs", files={"file": ("still-fine.mp3", data, "audio/mpeg")})
    assert r.status_code == 200


def test_a_full_upload_queue_does_not_block_a_link(upload_client):
    for i in range(MAX_PENDING_UPLOAD_JOBS):
        data = io.BytesIO(b"ID3" + b"\x00" * 128)
        assert (
            upload_client.post(
                "/api/jobs", files={"file": (f"t{i}.mp3", data, "audio/mpeg")}
            ).status_code
            == 200
        )
    r = upload_client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200


# ─── File upload ─────────────────────────────────────────────────────────────


def test_upload_rejects_unsupported_extension(upload_client):
    data = io.BytesIO(b"FORM\x00\x00\x00\x00AIFF")
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("track.aiff", data, "audio/aiff")},
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


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired([r"C:\Users\someone\ffmpeg\ffprobe.exe", "source.mp3"], 30),
        FileNotFoundError(2, "No such file or directory", r"C:\Users\someone\ffmpeg\ffprobe.exe"),
        RuntimeError(r"ffprobe failed: C:\Users\someone\jobs\abc\source.mp3: Invalid data"),
    ],
)
def test_upload_duration_failure_names_no_path(upload_client, caplog, error):
    """The probe's error text carries the ffprobe path and the job's source
    path. The client gets a fixed sentence; the log keeps the cause."""
    with patch("app.api.jobs._probe_duration", side_effect=error):
        r = upload_client.post(
            "/api/jobs", files={"file": ("my_track.mp3", io.BytesIO(b"ID3" + b"\x00" * 128))}
        )
    assert r.status_code == 422
    assert r.json() == {"detail": "Could not read file duration"}
    assert any(rec.exc_info and rec.exc_info[1] is error for rec in caplog.records)


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


def test_upload_ogg_returns_job_id(upload_client):
    data = io.BytesIO(b"OggS" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("my_track.ogg", data, "audio/ogg")},
    )
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_upload_opus_returns_job_id(upload_client):
    data = io.BytesIO(b"OggS" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("my_track.opus", data, "audio/opus")},
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
        "sections": [
            {
                "id": "sec1",
                "name": "Verse",
                "kind": "verse",
                "start": 0.0,
                "end": 30.0,
                "color": "#ff0000",
            }
        ]
    }
    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == done_job.id
    assert len(body["sections"]) == 1
    assert body["sections"][0]["name"] == "Verse"
    assert body["sections_source"] == "manual"
    assert done_job.sections_source == "manual"
    # Verify written to disk
    meta_path = tmp_path / done_job.id / "metadata.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text())
    assert meta["sections"][0]["id"] == "sec1"
    assert meta["sections_source"] == "manual"


def test_sections_accepts_neutral_part_kind(client, done_job):
    payload = {
        "sections": [
            {
                "id": "auto-005",
                "name": "Part",
                "kind": "part",
                "start": 49.874,
                "end": 65.901,
                "color": "#8391a5",
            }
        ]
    }

    response = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)

    assert response.status_code == 200
    assert response.json()["sections"][0]["kind"] == "part"


def test_sections_round_trip_the_locked_flag(client, done_job, tmp_path):
    """A locked section must still be locked after a reload.

    This model takes Pydantic's default extra="ignore", so before `locked` was
    declared the editor's flag was dropped here and answered 200. Locking
    looked like it worked and came back false on the next load, with nothing in
    the response, the logs or the file saying otherwise (#573).
    """
    payload = {
        "sections": [
            {
                "id": "sec1",
                "name": "Chorus",
                "start": 0.0,
                "end": 30.0,
                "color": "#ff0000",
                "locked": True,
            }
        ]
    }

    response = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)

    assert response.status_code == 200
    assert response.json()["sections"][0]["locked"] is True
    meta = json.loads((tmp_path / done_job.id / "metadata.json").read_text())
    assert meta["sections"][0]["locked"] is True


def test_sections_default_to_unlocked(client, done_job):
    """Every section saved before this feature existed omits the field."""
    payload = {
        "sections": [{"id": "sec1", "name": "Verse", "start": 0.0, "end": 30.0, "color": "#ff0000"}]
    }

    response = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)

    assert response.status_code == 200
    assert response.json()["sections"][0]["locked"] is False


def _section(index: int) -> dict:
    return {
        "id": f"sec{index}",
        "name": "Verse",
        "kind": "verse",
        "start": 0.0,
        "end": 1.0,
        "color": "#00c8a0",
    }


def test_sections_rejects_a_list_long_enough_to_stall_the_server(client, done_job):
    """The body is parsed before the handler runs, so an unbounded list holds
    the event loop and every other request with it (#481). A 33 MB body stalled
    an idle server's health check from 31 ms to 3.8 seconds."""
    import app.api.jobs as jobs_mod

    payload = {"sections": [_section(i) for i in range(jobs_mod._MAX_SECTIONS + 1)]}

    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)

    assert r.status_code == 422
    assert done_job.sections is None  # nothing partially applied


def test_sections_cap_clears_the_longest_legitimate_track(client, done_job):
    """0.5 s is the shortest section either the editor or normalize_sections
    allows, so a 3600 s track tops out at 7200. The cap must not reject that."""
    import app.api.jobs as jobs_mod

    assert jobs_mod._MAX_SECTIONS >= 3600 / 0.5

    payload = {"sections": [_section(i) for i in range(7200)]}

    assert client.patch(f"/api/jobs/{done_job.id}/sections", json=payload).status_code == 200


def test_oversized_editor_body_is_refused_before_it_is_parsed(client, done_job):
    """A model cap bounds what is stored, not what is parsed.

    FastAPI reads and validates a request body before the handler runs, so the
    max_length above does not stop an oversized payload from holding the event
    loop. Measured against a live server: 32 MB of sections took an idle health
    check from 32 ms to 5219 ms, and 16 ms once Content-Length was checked in
    middleware first (#481).
    """
    from app.main import _JSON_BODY_LIMIT

    padded = dict(_section(0), name="V" * 64)
    count = (_JSON_BODY_LIMIT // len(json.dumps(padded, separators=(",", ":")))) + 500
    payload = {"sections": [dict(padded, id=f"sec{i}") for i in range(count)]}
    # Sent as the exact bytes measured, so the assertion cannot drift from what
    # actually goes on the wire and quietly stop testing the ceiling.
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert len(raw) > _JSON_BODY_LIMIT

    r = client.patch(
        f"/api/jobs/{done_job.id}/sections",
        content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert done_job.sections is None


def test_the_body_ceiling_clears_the_largest_legitimate_editor_payload(client, done_job):
    """The ceiling must never be reachable by a real track. 10000 sections at
    the longest permitted name is about 1.6 MB against a 4 MB ceiling."""
    import app.api.jobs as jobs_mod
    from app.main import _JSON_BODY_LIMIT

    payload = {
        "sections": [
            dict(_section(i), id=f"sec{i}", name="V" * 64) for i in range(jobs_mod._MAX_SECTIONS)
        ]
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert len(raw) < _JSON_BODY_LIMIT

    r = client.patch(
        f"/api/jobs/{done_job.id}/sections",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200


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


def test_sections_invalid_kind_returns_422(client, done_job):
    payload = {
        "sections": [
            {
                "id": "sec1",
                "name": "Pre-chorus",
                "kind": "prechorus",
                "start": 0.0,
                "end": 5.0,
                "color": "#fff",
            }
        ]
    }
    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)
    assert r.status_code == 422


def test_sections_write_failure_does_not_mutate_live_job(client, done_job, monkeypatch):
    import app.api.jobs as jobs_mod

    original = [{"id": "old", "name": "Old", "start": 0.0, "end": 5.0, "color": "#fff"}]
    done_job.sections = original
    done_job.sections_source = "automatic"

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_mod, "_write_json_atomic", fail_write)
    response = client.patch(
        f"/api/jobs/{done_job.id}/sections",
        json={
            "sections": [{"id": "new", "name": "New", "start": 0.0, "end": 5.0, "color": "#000"}]
        },
    )

    assert response.status_code == 500
    assert done_job.sections == original
    assert done_job.sections_source == "automatic"


def test_atomic_section_metadata_write_leaves_no_temporary_file(tmp_path):
    import app.api.jobs as jobs_mod

    path = tmp_path / "metadata.json"
    jobs_mod._write_json_atomic(path, {"sections_source": "manual"})

    assert json.loads(path.read_text(encoding="utf-8"))["sections_source"] == "manual"
    assert not list(tmp_path.glob(".metadata.json.*.tmp"))


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


# ─── Track unavailable when stem files are missing from disk ─────────────────
#
# A "done" job's status field in the registry stays "done" even after its
# stems folder is deleted or moved outside the app (the registry has no way to
# know) - list_jobs/get_job check disk on every request and report
# "unavailable" instead, so the client never has to guess from a 404 or from
# the job disappearing off the list, neither of which catches this case.


def test_list_jobs_flags_a_done_job_with_no_stems_dir_as_unavailable(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef", status="done", title="Gone")
    _jobs[job.id] = job

    r = client.get("/api/jobs")
    assert r.status_code == 200
    [state] = r.json()
    assert state["status"] == "unavailable"


def test_get_job_flags_a_done_job_with_an_empty_stems_dir_as_unavailable(
    client, tmp_path, monkeypatch
):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef", status="done", title="Emptied")
    _jobs[job.id] = job
    (tmp_path / job.id / "stems").mkdir(parents=True)

    r = client.get(f"/api/jobs/{job.id}")
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"


def test_list_jobs_reports_done_when_stems_are_present(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabcdef", status="done", title="Still here")
    _jobs[job.id] = job
    stems_dir = tmp_path / job.id / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF1234")

    r = client.get("/api/jobs")
    [state] = r.json()
    assert state["status"] == "done"


def test_missing_stems_are_not_flagged_while_relocating(client, tmp_path, monkeypatch):
    """A library move (#354) makes the stems folder briefly absent on purpose -
    that is not the same failure as a user deleting it, and must not flash the
    unavailable badge mid-move."""
    import app.api.jobs as jobs_mod
    import app.core.stems_location as stems_location

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_location, "_relocating", True)
    job = Job(id="abcdefabcdef", status="done", title="Mid-move")
    _jobs[job.id] = job

    r = client.get("/api/jobs")
    [state] = r.json()
    assert state["status"] == "done"


# ─── The list does not go back to the disk for what has not changed (#976) ───
#
# GET /api/jobs used to resolve, stat and list every finished song's stems
# folder on every call, which is one network round trip after another on an
# SMB or NAS library. The answer is now kept per song and trusted only while
# the folder is the same folder with the same modification time, so every case
# below must still change the answer on the very next call.

_LONG_AGO = 1_600_000_000  # a modification time well outside the racy window


def _age(*paths):
    """Set the modification time back, as for a library that has sat a while.
    A folder changed within the last moments is never trusted (see
    LIBRARY_RACY_WINDOW_NS), so a test that wants the kept answer used must age it."""
    for path in paths:
        os.utime(path, (_LONG_AGO, _LONG_AGO))


def _done_song(root, job_id, **fields):
    job = Job(id=job_id, status="done", title=job_id, **fields)
    _jobs[job.id] = job
    stems = root / job_id / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF1234")
    _age(stems, stems.parent)
    return job


@pytest.fixture
def library(tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    root = tmp_path / "lib"
    root.mkdir()
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", root)
    return root


@pytest.fixture
def listings(monkeypatch):
    """Every directory listing the process makes, by folder."""
    seen: list[str] = []
    real_listdir, real_scandir = os.listdir, os.scandir

    def listdir(path="."):
        seen.append(os.fspath(path))
        return real_listdir(path)

    def scandir(path="."):
        seen.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "scandir", scandir)
    return seen


def _statuses(client, **params):
    r = client.get("/api/jobs", params=params)
    assert r.status_code == 200
    return {s["job_id"]: s["status"] for s in r.json()}


def test_listing_twice_lists_each_stems_folder_once(client, library, listings):
    for n in range(3):
        _done_song(library, f"aaaaaaaaaa0{n}")

    assert set(_statuses(client).values()) == {"done"}
    assert set(_statuses(client).values()) == {"done"}

    stems_listings = [p for p in listings if p.endswith("stems")]
    assert len(stems_listings) == 3


def test_a_warm_list_resolves_no_path_per_song(client, library, monkeypatch):
    """The jobs folder is resolved once per request, not once per song."""
    from pathlib import Path

    for n in range(4):
        _done_song(library, f"aaaaaaaaaa0{n}")
    _statuses(client)

    calls = []
    real_resolve = Path.resolve

    def resolve(self, strict=False):
        calls.append(self)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    _statuses(client)
    assert len(calls) <= 1


def test_a_stems_folder_deleted_between_lists_is_unavailable_at_once(client, library):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    shutil.rmtree(library / job.id / "stems")
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_whole_song_folder_deleted_between_lists_is_unavailable(client, library):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    shutil.rmtree(library / job.id)
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_stems_folder_emptied_between_lists_is_unavailable(client, library):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    (library / job.id / "stems" / "vocals.wav").unlink()
    assert _statuses(client) == {job.id: "unavailable"}


def test_stems_moved_away_and_back(client, library, tmp_path):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    stems = library / job.id / "stems"
    parked = tmp_path / "parked-stems"
    stems.rename(parked)
    assert _statuses(client) == {job.id: "unavailable"}

    parked.rename(stems)
    assert _statuses(client) == {job.id: "done"}


def test_stems_filled_in_after_a_list_are_seen(client, library):
    """The reverse of a deletion: an empty folder that gets its files back
    must not stay unavailable."""
    job = _done_song(library, "aaaaaaaaaa01")
    stems = library / job.id / "stems"
    (stems / "vocals.wav").unlink()
    _age(stems)
    assert _statuses(client) == {job.id: "unavailable"}

    (stems / "vocals.wav").write_bytes(b"RIFF1234")
    assert _statuses(client) == {job.id: "done"}


def _link_dir(link, target):
    """A symlink, or a junction on Windows where a symlink needs a privilege."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_stems_moved_out_of_the_library_behind_a_link_are_unavailable(client, library, tmp_path):
    """The serving routes refuse a stems folder that resolves outside the
    library, so the list must not keep calling it done because the folder
    itself (same identity, same time) did not change."""
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    stems = library / job.id / "stems"
    outside = tmp_path / "outside-stems"
    stems.rename(outside)
    _link_dir(stems, outside)
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_song_folder_moved_out_of_the_library_behind_a_link_is_unavailable(
    client, library, tmp_path
):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    song = library / job.id
    outside = tmp_path / "outside-song"
    song.rename(outside)
    _link_dir(song, outside)
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_folder_changed_moments_ago_is_listed_again(client, library, listings):
    """Some filesystems keep a folder's time to the second or coarser, so a
    change in the same tick as a listing would leave the time unchanged. A
    folder whose time is that recent is never trusted."""
    job = _done_song(library, "aaaaaaaaaa01")
    os.utime(library / job.id / "stems")  # now

    _statuses(client)
    _statuses(client)
    assert len([p for p in listings if p.endswith("stems")]) == 2


def test_a_new_import_shows_on_the_next_list(client, library):
    first = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {first.id: "done"}

    second = _done_song(library, "aaaaaaaaaa02")
    assert _statuses(client) == {first.id: "done", second.id: "done"}


def test_a_song_that_finishes_after_a_list_shows_as_done(client, library):
    job = Job(id="aaaaaaaaaa01", status="separating", title="Running")
    _jobs[job.id] = job
    assert _statuses(client) == {}

    stems = library / job.id / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF1234")
    job.status = "done"
    assert _statuses(client) == {job.id: "done"}


def test_trash_and_restore_move_a_song_between_lists_at_once(client, library):
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    assert client.post(f"/api/jobs/{job.id}/trash").status_code == 200
    assert _statuses(client) == {}
    assert _statuses(client, trashed="only") == {job.id: "done"}

    assert client.post(f"/api/jobs/{job.id}/restore").status_code == 200
    assert _statuses(client) == {job.id: "done"}
    assert _statuses(client, trashed="only") == {}


def test_a_registry_rebuilt_from_disk_lists_what_is_on_disk(client, library):
    """restore() builds fresh Job objects from registry.json and the folders.
    A song deleted while the registry was down must not come back as done
    from an answer kept for the old object."""
    from app.core import registry

    kept = _done_song(library, "aaaaaaaaaa01")
    gone = _done_song(library, "aaaaaaaaaa02")
    assert _statuses(client) == {kept.id: "done", gone.id: "done"}

    registry.persist(library)
    _jobs.clear()
    shutil.rmtree(library / gone.id / "stems")
    registry.restore(library)

    assert _statuses(client).get(kept.id) == "done"
    assert _statuses(client).get(gone.id) in (None, "unavailable")


def test_an_old_upload_with_no_source_is_searched_once(client, library, listings):
    """Uploads finished before sources were kept have no source.* to find.
    The search used to run on every list; it now runs again only when the
    song's folder changes."""
    job = _done_song(library, "aaaaaaaaaa01", source_url="local:Old upload")
    assert job.source_format is None

    for _ in range(3):
        _statuses(client)

    song_listings = [p for p in listings if os.path.basename(p) == job.id]
    assert len(song_listings) == 1
    assert job.source_format is None


def test_a_source_that_appears_for_an_old_upload_is_found(client, library):
    job = _done_song(library, "aaaaaaaaaa01", source_url="local:Old upload")
    _statuses(client)
    assert job.source_format is None

    (library / job.id / "source.flac").write_bytes(b"fLaC")
    _statuses(client)
    assert job.source_format == "flac"


# ─── The parts of the kept key, one test each (#976) ─────────────────────────
#
# The tests above change a folder "now", which the racy window alone already
# catches. These set the changed folder's time to one well outside the window,
# so only the named part of the key can tell the kept answer is stale.

_LATER = _LONG_AGO + 1000


def test_stems_emptied_long_ago_by_its_clock_are_unavailable(client, library):
    """The stems folder's modification time is part of the key."""
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    stems = library / job.id / "stems"
    (stems / "vocals.wav").unlink()
    os.utime(stems, (_LATER, _LATER))
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_stems_folder_swapped_for_another_with_the_same_time_is_unavailable(
    client, library, tmp_path
):
    """The stems folder's identity (file ID) is part of the key: a different,
    empty folder with the old time in its place must not inherit the answer."""
    job = _done_song(library, "aaaaaaaaaa01")
    assert _statuses(client) == {job.id: "done"}

    stems = library / job.id / "stems"
    stems.rename(tmp_path / "old-stems")
    stems.mkdir()
    _age(stems, stems.parent)
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_stems_junction_into_the_library_is_checked_every_time(client, library, monkeypatch):
    """A link or junction keeps its own time while its target changes, so a
    stems folder that is one is never answered from the kept key. With the
    racy window off, only the plain-folder check stands between the kept
    answer and an emptied target."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "LIBRARY_RACY_WINDOW_NS", 0)
    job = _done_song(library, "aaaaaaaaaa01")
    stems = library / job.id / "stems"
    shared = library / "shared-stems"
    stems.rename(shared)
    _link_dir(stems, shared)
    assert _statuses(client) == {job.id: "done"}

    (shared / "vocals.wav").unlink()
    assert _statuses(client) == {job.id: "unavailable"}


def test_a_source_that_appears_long_ago_by_its_clock_is_found(client, library):
    """The song folder's modification time is part of the no-source key."""
    job = _done_song(library, "aaaaaaaaaa01", source_url="local:Old upload")
    _statuses(client)
    assert job.source_format is None

    song = library / job.id
    (song / "source.flac").write_bytes(b"fLaC")
    os.utime(song, (_LATER, _LATER))
    _statuses(client)
    assert job.source_format == "flac"


def test_a_song_folder_swapped_for_one_with_a_source_and_the_same_time_is_searched(
    client, library, tmp_path
):
    """The song folder's identity (file ID) is part of the no-source key."""
    job = _done_song(library, "aaaaaaaaaa01", source_url="local:Old upload")
    _statuses(client)
    assert job.source_format is None

    song = library / job.id
    song.rename(tmp_path / "old-song")
    (song / "stems").mkdir(parents=True)
    (song / "stems" / "vocals.wav").write_bytes(b"RIFF1234")
    (song / "source.flac").write_bytes(b"fLaC")
    _age(song / "stems", song)
    _statuses(client)
    assert job.source_format == "flac"


def test_a_song_folder_changed_moments_ago_is_searched_again(client, library):
    """A song folder inside the racy window keeps no empty answer: on a
    filesystem with coarse times, a source added in the same tick leaves the
    folder's time as it was."""
    job = _done_song(library, "aaaaaaaaaa01", source_url="local:Old upload")
    song = library / job.id
    os.utime(song)  # now
    before = os.stat(song).st_mtime_ns
    _statuses(client)
    assert job.source_format is None

    (song / "source.flac").write_bytes(b"fLaC")
    os.utime(song, ns=(before, before))
    _statuses(client)
    assert job.source_format == "flac"


def test_kept_answers_go_when_the_song_leaves_the_registry(client, library):
    import app.api.jobs as jobs_mod

    link = _done_song(library, "aaaaaaaaaa01")
    upload = _done_song(library, "aaaaaaaaaa02", source_url="local:Old upload")
    _statuses(client)
    assert {link.id, upload.id} <= set(jobs_mod._stems_answers)
    assert upload.id in jobs_mod._no_source_seen

    del _jobs[link.id]
    del _jobs[upload.id]
    _statuses(client)
    assert link.id not in jobs_mod._stems_answers
    assert upload.id not in jobs_mod._stems_answers
    assert upload.id not in jobs_mod._no_source_seen


# ─── GET /jobs/{id}/failure: stderr tail and traceback sections ──────────────


def test_get_failure_separates_tail_from_traceback(client, tmp_path, monkeypatch):
    """error.txt can carry both a `--- stderr tail ---` and a
    `--- traceback ---` section; the parser must not lump the second into the
    first just because it also comes after a "---" marker."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    failed_dir = tmp_path / "failed" / "abcdefabcdef"
    failed_dir.mkdir(parents=True)
    (failed_dir / "error.txt").write_text(
        "\n".join(
            [
                "time: 2026-08-17T16:50:02+00:00",
                "stage: Error: Processing failed",
                "device: cpu",
                "cause: unknown",
                "exception: RuntimeError('boom')",
                "",
                "--- stderr tail ---",
                "line one of stderr",
                "line two of stderr",
                "",
                "--- traceback ---",
                "Traceback (most recent call last):",
                '  File "<home>/app/pipeline/runner.py", line 320, in _run_async',
                "RuntimeError: boom",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    r = client.get("/api/jobs/abcdefabcdef/failure")
    assert r.status_code == 200
    body = r.json()
    assert body["tail"] == ["line one of stderr", "line two of stderr"]
    assert body["traceback"] == [
        "Traceback (most recent call last):",
        '  File "<home>/app/pipeline/runner.py", line 320, in _run_async',
        "RuntimeError: boom",
    ]


def test_get_failure_traceback_is_empty_list_when_absent(client, tmp_path, monkeypatch):
    """Older quarantined jobs (or ones that never reached the pipeline) may
    have no traceback section -- the field must still be present as [], not
    missing, so the client doesn't have to special-case it."""
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    failed_dir = tmp_path / "failed" / "abcdefabcdef"
    failed_dir.mkdir(parents=True)
    (failed_dir / "error.txt").write_text("stage: Error\ncause: unknown\n", encoding="utf-8")

    r = client.get("/api/jobs/abcdefabcdef/failure")
    assert r.status_code == 200
    assert r.json()["traceback"] == []


# ─── POST /jobs/{id}/extract: Try again for a failed upload ──────────────────


def _failed_upload(job_id, *, keep_source=True):
    """An upload whose job errored, quarantined by the runner itself:
    jobs/failed/<id> with error.txt and, since #868, the source it kept."""
    import app.api.jobs as jobs_mod
    from app.core.registry import register
    from app.pipeline.runner import _quarantine_failed_job

    job_dir = jobs_mod.JOBS_DIR / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "source.wav").write_bytes(b"RIFF")
    job = Job(
        id=job_id,
        status="error",
        title="My Song",
        source_url="local:My Song",
        error="Audio processing failed. Please try again.",
    )
    register(job)
    _quarantine_failed_job(job, job_dir, jobs_mod.JOBS_DIR, RuntimeError("CUDA out of memory"))
    failed_dir = jobs_mod.JOBS_DIR / "failed" / job_id
    if not keep_source:
        (failed_dir / "source.wav").unlink()
    return job, failed_dir, job_dir


def test_extract_runs_a_failed_upload_again_from_its_kept_source(client):
    from app.pipeline.jobqueue import _find_local_source

    job, failed_dir, job_dir = _failed_upload("abcdef000001")
    assert job.error_detail

    r = client.post(f"/api/jobs/{job.id}/extract")

    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert _find_local_source(job_dir) == job_dir / "source.wav"
    assert not failed_dir.exists()
    assert not (job_dir / "error.txt").exists()
    assert job.error is None
    assert job.error_detail is None


def test_extract_runs_a_failed_upload_its_quarantine_left_in_place(client):
    """A quarantine that failed (disk full writing error.txt) leaves an
    upload's folder where it was, source and all."""
    import app.api.jobs as jobs_mod
    from app.core.registry import register

    job_dir = jobs_mod.JOBS_DIR / "abcdef000005"
    job_dir.mkdir(parents=True)
    (job_dir / "source.wav").write_bytes(b"RIFF")
    (job_dir / "error.txt").write_text("cause: unknown\n", encoding="utf-8")
    job = Job(id=job_dir.name, status="error", source_url="local:My Song")
    register(job)

    r = client.post(f"/api/jobs/{job.id}/extract")

    assert r.status_code == 200
    assert (job_dir / "source.wav").is_file()
    assert not (job_dir / "error.txt").exists()


def test_extract_refuses_a_failed_upload_it_cannot_move_back(client, monkeypatch):
    """A file held open on Windows: the kept file stays where it was."""
    from pathlib import Path

    job, failed_dir, job_dir = _failed_upload("abcdef000006")

    def locked(self, target):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "rename", locked)
    r = client.post(f"/api/jobs/{job.id}/extract")

    assert r.status_code == 409
    assert job.status == "error"
    assert (failed_dir / "source.wav").is_file()


def test_extract_refuses_a_failed_upload_whose_file_is_gone(client):
    job, failed_dir, job_dir = _failed_upload("abcdef000002", keep_source=False)

    r = client.post(f"/api/jobs/{job.id}/extract")

    assert r.status_code == 409
    assert job.status == "error"
    assert (failed_dir / "error.txt").is_file()
    assert not job_dir.exists()


def test_extract_refuses_a_failed_link(client):
    """A link is tried again as a new import of the link, not from here."""
    from app.core.registry import register

    job = Job(id="abcdef000003", status="error", source_url="https://youtu.be/dQw4w9WgXcQ")
    register(job)

    assert client.post(f"/api/jobs/{job.id}/extract").status_code == 409
    assert job.status == "error"


def test_extract_puts_a_failed_upload_back_when_the_queue_is_full(client):
    from app.core.registry import register

    for i in range(MAX_PENDING_UPLOAD_JOBS):
        register(Job(id=f"aaaaaaaa{i:04x}", status="queued", source_url="local:Waiting"))
    job, failed_dir, job_dir = _failed_upload("abcdef000004")

    r = client.post(f"/api/jobs/{job.id}/extract")

    assert r.status_code == 503
    assert job.status == "error"
    assert (failed_dir / "source.wav").is_file()
    assert (failed_dir / "error.txt").is_file()
    assert not job_dir.exists()


# ─── POST /jobs/{id}/vocal-split (#275) ──────────────────────────────────────


def _make_done_job_with_vocals(tmp_path, monkeypatch, job_id="abcdefabc275"):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id=job_id, status="done", title="Test track")
    _jobs[job.id] = job
    stems_dir = tmp_path / job.id / "stems"
    stems_dir.mkdir(parents=True)
    (stems_dir / "vocals.wav").write_bytes(b"RIFF1234")
    return job, stems_dir


def test_vocal_split_404_unknown_job(client):
    r = client.post("/api/jobs/000000000000/vocal-split")
    assert r.status_code == 404


def test_vocal_split_404_job_not_done(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    job = Job(id="abcdefabc275", status="separating", title="In progress")
    _jobs[job.id] = job

    r = client.post(f"/api/jobs/{job.id}/vocal-split")
    assert r.status_code == 404


def test_vocal_split_success(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    job, stems_dir = _make_done_job_with_vocals(tmp_path, monkeypatch)

    def fake_split(job_arg, stems_dir_arg):
        (stems_dir_arg / "lead_vocals.wav").write_bytes(b"RIFF")
        (stems_dir_arg / "backing_vocals.wav").write_bytes(b"RIFF")
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr(jobs_mod, "split_vocals", fake_split)

    r = client.post(f"/api/jobs/{job.id}/vocal-split")
    assert r.status_code == 200
    body = r.json()
    assert body["vocal_split"] == "done"
    names = {s["name"] for s in body["stems"]}
    assert {"lead_vocals", "backing_vocals"} <= names
    assert job.vocal_split == "done"


def test_vocal_split_409_when_already_running(client, tmp_path, monkeypatch):
    job, _ = _make_done_job_with_vocals(tmp_path, monkeypatch)
    job.vocal_split = "running"

    r = client.post(f"/api/jobs/{job.id}/vocal-split")
    assert r.status_code == 409


def test_vocal_split_202_idempotent_when_already_done(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    job, _ = _make_done_job_with_vocals(tmp_path, monkeypatch)
    job.vocal_split = "done"
    called = False

    def fake_split(*_args, **_kwargs):
        nonlocal called
        called = True
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr(jobs_mod, "split_vocals", fake_split)

    r = client.post(f"/api/jobs/{job.id}/vocal-split")
    assert r.status_code == 202
    assert called is False  # idempotent: never re-runs the model


def test_vocal_split_failure_keeps_job_done_with_base_stems(client, tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    job, stems_dir = _make_done_job_with_vocals(tmp_path, monkeypatch)

    def fake_split(job_arg, stems_dir_arg):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(jobs_mod, "split_vocals", fake_split)

    r = client.post(f"/api/jobs/{job.id}/vocal-split")
    assert r.status_code == 500
    assert job.status == "done"  # the job itself is untouched
    assert job.vocal_split == "error"
    assert (stems_dir / "vocal_split_error.txt").is_file()


# ─── Lead + Backing asked for with the import (#848) ─────────────────────────
#
# The request lived only in the page that submitted the job, so a reload, a
# restart or a second window during the import dropped it: the song finished
# with one Vocals stem and nothing said why. The job keeps it now.


def test_a_link_import_keeps_the_lead_backing_request(client):
    from app.core import registry

    r = client.post(
        "/api/jobs",
        json={
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "stems": ["vocals", "drums"],
            "vocal_split": True,
        },
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    assert registry.get(job_id).vocal_split_wanted is True
    assert client.get(f"/api/jobs/{job_id}").json()["vocal_split_wanted"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"url": "https://youtu.be/dQw4w9WgXcQ"},
        {"url": "https://youtu.be/dQw4w9WgXcQ", "vocal_split": False},
        # No vocals chosen: there is nothing to split.
        {"url": "https://youtu.be/dQw4w9WgXcQ", "stems": ["drums"], "vocal_split": True},
    ],
    ids=["not-sent", "combined", "no-vocals"],
)
def test_a_link_import_without_lead_backing_asks_for_no_split(client, body):
    from app.core import registry

    r = client.post("/api/jobs", json=body)
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).vocal_split_wanted is False


@pytest.mark.parametrize(("sent", "wanted"), [("true", True), ("false", False), (None, False)])
def test_an_upload_keeps_the_lead_backing_request(upload_client, sent, wanted):
    from app.core import registry

    form = {"stems": json.dumps(["vocals", "bass"])}
    if sent is not None:
        form["vocal_split"] = sent
    r = upload_client.post(
        "/api/jobs",
        data=form,
        files={"file": ("song.mp3", io.BytesIO(b"ID3" + b"\x00" * 128), "audio/mpeg")},
    )
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).vocal_split_wanted is wanted


def test_the_request_is_logged_with_the_import(client, caplog):
    with caplog.at_level("INFO", logger="stemdeck.api"):
        r = client.post(
            "/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ", "vocal_split": True}
        )
    assert f"[{r.json()['job_id']}] vocal split requested with the import" in caplog.text


def test_a_split_is_saved_as_running_before_it_starts(client, tmp_path, monkeypatch):
    """A restart during the split must not leave the record at "none": a page
    that finds the job asking for Lead + Backing would then run it again."""
    import app.api.jobs as jobs_mod

    job, _ = _make_done_job_with_vocals(tmp_path, monkeypatch)
    seen = {}

    def fake_split(job_arg, stems_dir_arg):
        saved = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
        seen.update(next(r for r in saved["jobs"] if r["id"] == job.id))
        (stems_dir_arg / "lead_vocals.wav").write_bytes(b"RIFF")
        (stems_dir_arg / "backing_vocals.wav").write_bytes(b"RIFF")
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr(jobs_mod, "split_vocals", fake_split)

    assert client.post(f"/api/jobs/{job.id}/vocal-split").status_code == 200
    assert seen["vocal_split"] == "running"
    restored = Job.from_record(seen)
    assert restored.vocal_split == "error"
    assert restored.vocal_split_reason == "interrupted"


def test_a_split_logs_when_it_starts_and_how_long_it_took(client, tmp_path, monkeypatch, caplog):
    """A successful split used to write nothing, so the log could not tell a
    split that ran from one that was never asked for."""
    import app.api.jobs as jobs_mod

    job, _ = _make_done_job_with_vocals(tmp_path, monkeypatch)

    def fake_split(job_arg, stems_dir_arg):
        (stems_dir_arg / "lead_vocals.wav").write_bytes(b"RIFF")
        (stems_dir_arg / "backing_vocals.wav").write_bytes(b"RIFF")
        return ["lead_vocals", "backing_vocals"]

    monkeypatch.setattr(jobs_mod, "split_vocals", fake_split)

    with caplog.at_level("INFO", logger="stemdeck.api"):
        assert client.post(f"/api/jobs/{job.id}/vocal-split").status_code == 200
    assert f"[{job.id}] vocal split started" in caplog.text
    assert re.search(
        rf"\[{job.id}\] vocal split done in \d+\.\ds: lead_vocals, backing_vocals", caplog.text
    )


def test_submit_captures_the_song_structure_setting_on_the_job(client, monkeypatch):
    """The job carries the answer, so a later change cannot reach it.

    The UI clears this toggle as soon as the user opens another song, and the
    stage that reads it runs at the very end of the pipeline. If the flag were
    not captured here, an import could lose a pass that was switched on when it
    was submitted.
    """
    from app.core import registry

    monkeypatch.setattr("app.api.jobs.get_auto_sections", lambda: True)
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).auto_sections is True

    # And the off case, so this is not just asserting a default.
    monkeypatch.setattr("app.api.jobs.get_auto_sections", lambda: False)
    r = client.post("/api/jobs", json={"url": "https://youtu.be/oHg5SJYRHA0"})
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).auto_sections is False


def test_submit_captures_the_detect_lyrics_setting_on_the_job(client, monkeypatch):
    """Detect lyrics is read when the job is created, so turning it off while
    a song waits in the queue does not change what that song gets."""
    from app.core import registry

    monkeypatch.setattr("app.api.jobs.get_auto_lyrics", lambda: True)
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200
    job = registry.get(r.json()["job_id"])
    assert job.auto_lyrics is True
    assert job.to_state()["auto_lyrics"] is True

    monkeypatch.setattr("app.api.jobs.get_auto_lyrics", lambda: False)
    r = client.post("/api/jobs", json={"url": "https://youtu.be/oHg5SJYRHA0"})
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).auto_lyrics is False


def test_detect_lyrics_setting_defaults_off_and_is_kept(client, monkeypatch):
    """Off until turned on, and kept until changed (unlike Detect structure,
    which clears itself)."""
    monkeypatch.delenv("STEMDECK_AUTO_LYRICS", raising=False)
    assert client.get("/api/settings").json()["auto_lyrics"] is False
    r = client.post("/api/settings", json={"auto_lyrics": True})
    assert r.status_code == 200 and r.json()["auto_lyrics"] is True
    assert client.get("/api/settings").json()["auto_lyrics"] is True
    client.post("/api/settings", json={"auto_lyrics": False})
    assert client.get("/api/settings").json()["auto_lyrics"] is False


# --- POST /api/jobs/{id}/resplit (#635) -------------------------------------
#
# Re-separating a finished upload from the source kept beside it. A
# link-sourced track never needed this -- re-importing its URL does the same
# thing -- but an upload had no equivalent: the file was handed over once, and
# the composer has nothing to submit for it.


@pytest.fixture
def resplit_client(tmp_path, monkeypatch):
    import app.api.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _finished_upload(tmp_path, job_id="aaaaaaaaaaaa", *, source="source.mp3"):
    """A done upload with its source still beside it, as the runner now leaves it."""
    job = Job(id=job_id, status="done", title="Demo", duration_sec=60.0)
    job.source_url = "local:Demo"
    _jobs[job.id] = job
    job_dir = tmp_path / job.id
    (job_dir / "stems").mkdir(parents=True)
    if source:
        (job_dir / source).write_bytes(b"ID3")
    return job


def test_resplit_starts_a_new_job_from_the_retained_source(resplit_client, tmp_path):
    job = _finished_upload(tmp_path)

    r = resplit_client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals", "drums"]})

    assert r.status_code == 200
    new_id = r.json()["job_id"]
    assert new_id != job.id, "re-split produces a new job, the way re-importing a link does"
    assert _jobs[new_id].selected_stems == ["vocals", "drums"]
    # Still a local: source, so its own copy is kept and it can be re-split in
    # turn -- but distinct, or the library would replace the track it came from
    # and leave that job on disk with nothing referencing it.
    assert _jobs[new_id].source_url != job.source_url
    assert _jobs[new_id].source_url.startswith("local:")
    assert _jobs[new_id].source_format == "mp3", "the format comes from the kept source"
    assert (tmp_path / new_id / "source.mp3").is_file()
    # The original is untouched: its stems, sections and beat grid survive.
    assert (tmp_path / job.id / "source.mp3").is_file()
    assert (tmp_path / job.id / "stems").is_dir()


def test_resplit_keeps_the_lead_backing_request(resplit_client, tmp_path):
    job = _finished_upload(tmp_path)

    r = resplit_client.post(
        f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals"], "vocal_split": True}
    )

    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].vocal_split_wanted is True


@pytest.mark.parametrize(
    ("source_url", "expected"),
    [
        ("local:My Song", "local:My Song (abc123)"),
        ("local:Mr. Brightside", "local:Mr. Brightside (abc123)"),
        (None, "local:track (abc123)"),
    ],
)
def test_resplit_source_names_the_same_file_without_colliding(source_url, expected):
    """The marker goes on the end. An upload's title has no extension, so a
    dot in it belongs to the title and must survive (#690)."""
    import app.api.jobs as jobs_mod

    assert jobs_mod._resplit_source_url(source_url, "abc123def456") == expected


def test_resplit_falls_back_to_every_stem_when_the_list_is_unusable(resplit_client, tmp_path):
    """Unknown names are dropped rather than trusted, matching the importer."""
    job = _finished_upload(tmp_path)

    r = resplit_client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["../etc", "nope"]})

    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].selected_stems == list(STEM_NAMES)


def test_resplit_rejects_an_over_long_stem_list(resplit_client, tmp_path):
    job = _finished_upload(tmp_path)

    r = resplit_client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals"] * 50})

    assert r.status_code == 422


def test_resplit_404s_for_an_unknown_job(resplit_client):
    r = resplit_client.post("/api/jobs/bbbbbbbbbbbb/resplit", json={"stems": []})
    assert r.status_code == 404


def test_resplit_409s_when_no_source_was_kept(resplit_client, tmp_path):
    """Every job finished before the source was retained is in this state, as
    is any link-sourced track. It is an ordinary answer, not a failure."""
    job = _finished_upload(tmp_path, source=None)

    r = resplit_client.post(f"/api/jobs/{job.id}/resplit", json={"stems": []})

    assert r.status_code == 409
    assert "source" in r.json()["detail"]


def test_resplit_ignores_a_source_with_an_extension_we_never_accept(resplit_client, tmp_path):
    """source.* is a glob, and the job directory is not a trusted input just
    because we created it. Only the extensions the importer accepts may be fed
    back into the pipeline."""
    job = _finished_upload(tmp_path, source="source.sh")

    r = resplit_client.post(f"/api/jobs/{job.id}/resplit", json={"stems": []})

    assert r.status_code == 409


@pytest.mark.parametrize(
    "job_id",
    [
        "../../etc/passwd",  # normalised away by the client, never routed here
        "..%2F..%2Fetc",  # escaped, so it arrives as a job id and must be refused
        "aaaa..aaaaaa",  # right length, wrong alphabet
        "aaaa/../aaaa",
        "a" * 200,
    ],
)
def test_resplit_never_lets_a_crafted_id_reach_the_filesystem(resplit_client, job_id):
    """JOB_ID_RE is the gate, and it runs before any I/O.

    The first case never reaches the handler at all -- the client collapses it
    and nothing answers POST at that path -- which is why this asserts on a
    refusal rather than on one status. The rest do arrive, and must be turned
    away as a missing job rather than a 500 from something touching a path.
    """
    r = resplit_client.post(f"/api/jobs/{job_id}/resplit", json={"stems": []})
    assert r.status_code in (404, 405, 422), r.status_code
    assert r.status_code != 500


# ── source_format (#690) ─────────────────────────────────────────────────────
# An upload's source_url is "local:<title>", and the title has its extension
# removed, so nothing can read the format out of it. The server records it.


def test_upload_records_its_format_apart_from_the_title(upload_client):
    data = io.BytesIO(b"fLaC" + b"\x00" * 128)
    r = upload_client.post(
        "/api/jobs",
        files={"file": ("Mr. Brightside.FLAC", data, "audio/flac")},
    )
    assert r.status_code == 200
    state = upload_client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert state["source_url"] == "local:Mr. Brightside"
    assert state["source_format"] == "flac"


def test_an_older_upload_learns_its_format_from_the_kept_source(resplit_client, tmp_path):
    job = _finished_upload(tmp_path)
    assert job.source_format is None, "recorded before the field existed"

    state = resplit_client.get(f"/api/jobs/{job.id}").json()

    assert state["source_format"] == "mp3"
    assert job.source_format == "mp3", "kept on the job, so it is persisted"


def test_an_older_upload_with_nothing_kept_has_no_format(resplit_client, tmp_path):
    job = _finished_upload(tmp_path, source=None)

    assert resplit_client.get(f"/api/jobs/{job.id}").json()["source_format"] is None


def test_a_link_has_no_format_even_with_a_source_on_disk(resplit_client, tmp_path):
    """A link's source is its download, not a file anyone chose a format for."""
    job = _finished_upload(tmp_path)
    job.source_url = "https://youtu.be/dQw4w9WgXcQ"

    assert resplit_client.get(f"/api/jobs/{job.id}").json()["source_format"] is None
