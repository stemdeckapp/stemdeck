from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.core.config import MAX_PENDING_UPLOAD_JOBS, MAX_PENDING_URL_JOBS
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
    payload = {"sections": [_section(i) for i in range(jobs_api._MAX_SECTIONS + 1)]}

    r = client.patch(f"/api/jobs/{done_job.id}/sections", json=payload)

    assert r.status_code == 422
    assert done_job.sections is None  # nothing partially applied


def test_sections_cap_clears_the_longest_legitimate_track(client, done_job):
    """0.5 s is the shortest section either the editor or normalize_sections
    allows, so a 3600 s track tops out at 7200. The cap must not reject that."""
    assert jobs_api._MAX_SECTIONS >= 3600 / 0.5

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
    from app.main import _EDITOR_BODY_LIMIT

    padded = dict(_section(0), name="V" * 64)
    count = (_EDITOR_BODY_LIMIT // len(json.dumps(padded, separators=(",", ":")))) + 500
    payload = {"sections": [dict(padded, id=f"sec{i}") for i in range(count)]}
    # Sent as the exact bytes measured, so the assertion cannot drift from what
    # actually goes on the wire and quietly stop testing the ceiling.
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert len(raw) > _EDITOR_BODY_LIMIT

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
    from app.main import _EDITOR_BODY_LIMIT

    payload = {
        "sections": [
            dict(_section(i), id=f"sec{i}", name="V" * 64) for i in range(jobs_api._MAX_SECTIONS)
        ]
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert len(raw) < _EDITOR_BODY_LIMIT

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
    from app.api.jobs import _write_json_atomic

    path = tmp_path / "metadata.json"
    _write_json_atomic(path, {"sections_source": "manual"})

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
