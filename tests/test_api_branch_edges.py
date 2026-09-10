"""The unexercised arm of a branch, across the API and core modules.

Same shape as tests/test_pipeline_branch_edges.py: every line here already
runs, and what was missing is the other answer -- the header that is absent,
the lookup that finds nothing, the cache that already holds the file. None of
them raise, so a regression in one shows up as a wrong response rather than a
failure, which is the kind this suite is least likely to notice on its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.api.queue as queue_mod
import app.api.search as search_mod
import app.api.stems as stems_mod
import app.core.config as cfg
import app.core.redact as redact_mod
import app.core.registry as reg
import app.core.settings as settings_mod
from app.core.models import Job
from app.core.registry import _deleted, _jobs

JOB = "abcdefabcdef"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    _deleted.clear()
    yield
    _jobs.clear()
    _deleted.clear()


def _tone(path: Path, seconds=1.0, rate=8000, freq=440.0):
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32), rate)


# --------------------------------------------------------------------------
# the queue snapshot
# --------------------------------------------------------------------------


def test_a_running_id_whose_job_has_been_deleted_reads_as_an_idle_queue(monkeypatch):
    """Delete-while-running drops the registry entry before the worker notices.
    Dereferencing the stale id would be an AttributeError inside the endpoint
    the whole queue UI polls."""
    monkeypatch.setattr(queue_mod.jobqueue, "snapshot", lambda: (JOB, []))

    state = queue_mod._snapshot()

    assert state["running"] is None
    assert state["queued"] == []


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------


def test_an_upload_that_declares_no_length_is_not_rejected_out_of_hand(tmp_path, monkeypatch):
    """The Content-Length pre-check is an optimisation -- it fails an obviously
    oversized upload before buffering it. A chunked upload declares no length
    at all, and skipping the pre-check has to mean "let the real limit decide",
    not "reject"."""
    from app.main import app

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod.jobqueue, "enqueue", lambda job_id: None)
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda p: 60.0)

    boundary = "----stemdeck-test-boundary"
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="song.mp3"\r\n'
            "Content-Type: audio/mpeg\r\n\r\n"
        ).encode()
        + b"ID3\x04\x00\x00\x00\x00\x00\x00"
        + b"\0" * 2048
        + (f"\r\n--{boundary}--\r\n").encode()
    )

    def _streamed():
        # An iterator body makes httpx send Transfer-Encoding: chunked, which
        # is the only way to reach the endpoint without a Content-Length.
        yield body

    with TestClient(app) as client:
        r = client.post(
            "/api/jobs",
            content=_streamed(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert r.status_code in (200, 201), r.text
    assert "content-length" not in {k.lower() for k in r.request.headers}


# --------------------------------------------------------------------------
# cancel
# --------------------------------------------------------------------------


def test_cancelling_a_vocal_split_with_no_live_worker_still_sets_the_flag(monkeypatch):
    """The worker can exit between the split failing and the cancel arriving.
    Terminating a process that is already gone -- or a stale registry entry
    whose pid has been recycled -- is the case this guard exists for."""
    from app.main import app

    job = Job(id=JOB)
    job.status = "done"
    job.vocal_split = "running"
    _jobs[JOB] = job
    monkeypatch.setattr(jobs_mod, "registry_get_proc", lambda job_id: None)

    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/cancel")

    assert r.status_code == 200
    assert job.cancel_requested is True


def test_cancelling_the_running_job_with_no_live_worker_still_finalises_it(monkeypatch):
    """Same window on the separation itself: between demucs exiting and the
    runner writing the result there is no process to terminate."""
    from app.main import app

    job = Job(id=JOB)
    job.status = "separating"
    _jobs[JOB] = job
    monkeypatch.setattr(jobs_mod.jobqueue, "running_id", lambda: JOB)
    monkeypatch.setattr(jobs_mod, "registry_get_proc", lambda job_id: None)

    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/cancel")

    assert r.status_code == 200
    assert job.cancel_requested is True


# --------------------------------------------------------------------------
# the vocal split's lane bookkeeping
# --------------------------------------------------------------------------


async def test_a_split_that_returns_a_lane_the_job_already_has_does_not_duplicate_it(
    tmp_path, monkeypatch
):
    """A retried split, or one whose stems survived a restart, hands back names
    the job already carries. Appending them again would put two identical cards
    in the player, each with its own gain fader over the same file."""
    stems = tmp_path / JOB / "stems"
    stems.mkdir(parents=True)
    _tone(stems / "vocals.wav")
    _tone(stems / "lead_vocals.wav")

    job = Job(id=JOB)
    job.status = "done"
    job.stems = [
        {"name": "vocals", "url": f"/api/jobs/{JOB}/stems/vocals.wav"},
        {"name": "lead_vocals", "url": f"/api/jobs/{JOB}/stems/lead_vocals.wav"},
    ]
    _jobs[JOB] = job

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "split_vocals", lambda j, d: ["lead_vocals", "backing_vocals"])
    monkeypatch.setattr(jobs_mod, "registry_persist", lambda d: None)

    await jobs_mod.start_vocal_split(JOB)

    names = [s["name"] for s in job.stems]
    assert names.count("lead_vocals") == 1
    assert "backing_vocals" in names


# --------------------------------------------------------------------------
# the failure report parser
# --------------------------------------------------------------------------


def test_a_stderr_tail_that_ends_in_real_output_keeps_its_last_line(tmp_path, monkeypatch):
    """The writer puts a blank line before the traceback for readability, and
    the parser drops it. A tail whose last line is not blank must keep it --
    that line is usually the one naming the failure."""
    from app.main import app

    failed = tmp_path / "failed" / JOB
    failed.mkdir(parents=True)
    (failed / "error.txt").write_text(
        "stage: separate\n"
        "--- stderr tail ---\n"
        "CUDA out of memory\n"
        "--- traceback ---\n"
        "Traceback (most recent call last):\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)

    with TestClient(app) as client:
        body = client.get(f"/api/jobs/{JOB}/failure").json()

    assert body["tail"] == ["CUDA out of memory"]
    assert body["traceback"] == ["Traceback (most recent call last):"]


# --------------------------------------------------------------------------
# the preview proxy's passthrough headers
# --------------------------------------------------------------------------


async def test_headers_the_upstream_did_not_send_are_not_invented(monkeypatch):
    """A Content-Length of "None" is worse than none at all: the browser
    believes it and truncates the audio. SoundCloud's HLS segments answer
    without either header, so this is the ordinary case there rather than an
    exotic one."""

    class _NoHeaders:
        status = 200
        headers: dict[str, str] = {}

        def read(self, _n):
            return b""

        def close(self):
            pass

    def _body():
        yield from ()

    monkeypatch.setattr(
        search_mod,
        "resolve_preview",
        lambda url: {"url": "https://x.invalid/a", "mime": "audio/mp4"},
    )
    monkeypatch.setattr(search_mod, "_proxy", lambda stream, header: (_NoHeaders(), _body))

    response = await search_mod.preview(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ", _StubRequest(method="GET")
    )

    assert "Content-Length" not in response.headers
    assert "Content-Range" not in response.headers
    assert response.headers["Accept-Ranges"] == "bytes"


# --------------------------------------------------------------------------
# core: redact, config, settings, registry
# --------------------------------------------------------------------------


def test_a_home_directory_of_slash_is_not_redacted(monkeypatch):
    """Docker runs the backend as root with HOME=/. Replacing every "/" in a
    failure report with "<home>" would destroy every path in it, which is the
    only thing the report is for."""
    monkeypatch.setattr(redact_mod.Path, "home", staticmethod(lambda: Path("/")))

    assert redact_mod.redact("failed reading /jobs/abc/stems") == "failed reading /jobs/abc/stems"


def test_a_runtime_directory_holding_none_of_the_known_binaries_is_skipped(tmp_path, monkeypatch):
    """A half-extracted runtime pack leaves the directory there with nothing in
    it. Reporting it as a runtime would tell the user the challenge solver is
    available when yt-dlp will not find one."""
    empty = tmp_path / "jsruntime"
    empty.mkdir()
    (empty / "readme.txt").write_text("nothing to run here", encoding="utf-8")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (empty,))

    assert cfg.bundled_js_runtime() is None


def test_a_mirror_that_reads_back_empty_is_not_a_recovery(tmp_path, monkeypatch, caplog):
    """An empty JSON object is what a mirror written by a crashed process looks
    like. Restoring it would overwrite the quarantined primary with nothing and
    report a recovery that returned no settings."""
    primary = tmp_path / "settings.json"
    primary.write_text("{not json", encoding="utf-8")
    mirror = tmp_path / "mirror.json"
    mirror.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(settings_mod, "_SETTINGS_PATH", primary)
    monkeypatch.setenv("STEMDECK_SETTINGS_MIRROR", str(mirror))

    assert settings_mod._load() == {}
    assert "recovered settings from mirror" not in caplog.text


def test_a_job_in_a_state_that_is_neither_finished_nor_resumable_is_dropped(tmp_path):
    """ "error" and "cancelled" jobs, and a "done" one with no title, are all
    records of something that will never run again. Putting them back would
    show a permanently stuck row in the library."""
    (tmp_path / "registry.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {"id": JOB, "status": "error", "title": "Broken"},
                    {"id": "abcdefabcdee", "status": "done", "title": None},
                ]
            }
        ),
        encoding="utf-8",
    )

    reg.restore(tmp_path)

    assert _jobs == {}


# --------------------------------------------------------------------------
# the body-size middleware and the client scheme
# --------------------------------------------------------------------------


class _StubRequest:
    """Only what these two helpers actually read off a request."""

    def __init__(self, method="POST", headers=None, scheme="http"):
        self.method = method
        self.headers = headers or {}
        self.url = type("_Url", (), {"scheme": scheme})()


async def test_a_request_with_neither_a_length_nor_chunked_encoding_is_let_through():
    """HTTP/2 carries no Transfer-Encoding at all and may omit Content-Length.
    Rejecting that as a length-less body would 411 every request from a
    reverse proxy that speaks h2 upstream."""
    import app.main as main

    async def _handler(_request):
        return "reached the handler"

    result = await main.limit_json_body_size(_StubRequest(headers={}), _handler)

    assert result == "reached the handler"


def test_a_forwarded_header_with_no_proto_falls_back_to_our_own_scheme():
    """RFC 7239 makes every parameter optional, and a proxy that sends only
    `for=` is legal. Reading a missing proto as anything but "unknown" would
    have us report a secure context the browser does not have."""
    import app.main as main

    request = _StubRequest(headers={"forwarded": "for=192.0.2.1;host=stemdeck.local"})

    assert main._client_scheme(request) == "http"


def test_a_forwarded_header_that_does_name_a_proto_is_believed():
    """So the fallback above is about the missing parameter, not about the
    header being ignored."""
    import app.main as main

    request = _StubRequest(headers={"forwarded": 'for=192.0.2.1;proto="https"'})

    assert main._client_scheme(request) == "https"


# --------------------------------------------------------------------------
# the click cache
# --------------------------------------------------------------------------


def test_a_count_in_already_in_the_cache_is_not_rendered_again(tmp_path, monkeypatch):
    """The count-in WAV is keyed by everything that shapes it, so a hit is the
    same file byte for byte. Re-rendering it would put a multi-second ffmpeg
    pass in front of every play of a track the user has already heard."""
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "click-cache")

    stems = tmp_path / JOB / "stems"
    stems.mkdir(parents=True)
    grid = {
        "beats": [i * 0.5 for i in range(32)],
        "bars": [{"beat": i * 4, "meter": 4} for i in range(8)],
        "duration": 16.0,
    }
    (stems / "beats.json").write_text(json.dumps(grid), encoding="utf-8")

    first = stems_mod._click_lane(JOB, True, 1.0, 0, 1.0, count_in_bars=1)
    assert first is not None and first.path.is_file()
    stamp = first.path.stat().st_mtime_ns

    renders: list[str] = []
    monkeypatch.setattr(
        stems_mod, "render_count_in_wav", lambda *a, **kw: renders.append("rendered")
    )

    second = stems_mod._click_lane(JOB, True, 1.0, 0, 1.0, count_in_bars=1)

    assert second is not None
    assert renders == [], "a cached count-in was rendered again"
    assert second.path.stat().st_mtime_ns == stamp
