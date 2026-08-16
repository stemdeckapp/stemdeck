"""GET /api/jobs/{id}/failure — the quarantined evidence, minus the private bits.

The pipeline has always written jobs/failed/<id>/error.txt on a failure (#277)
and nothing ever read it back, so a bug report could carry the classified cause
and one truncated stderr line at most. These tests pin the two things that make
the endpoint safe to feed into a public GitHub issue: it serves the technical
keys and the stderr tail, and it never serves the track title or source URL.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

JOB = "abcdefabcdef"

TITLE = "Someone's Private Demo Take 3"
SOURCE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

ERROR_TXT = f"""time: 2026-08-16T18:00:00+00:00
job: {JOB}
title: {TITLE}
source: {SOURCE}
stage: Error: Processing failed
device: cuda, then cpu
model: htdemucs_6s
cause: out-of-memory
timings: {{"download": 4.2, "separate": 61.0}}
exception: SeparationError('demucs failed: exit status 1')

--- stderr tail ---
torch.OutOfMemoryError: CUDA out of memory.
Tried to allocate 2.40 GiB
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    from app.main import app

    return TestClient(app)


@pytest.fixture
def quarantined(tmp_path):
    d = tmp_path / "failed" / JOB
    d.mkdir(parents=True)
    (d / "error.txt").write_text(ERROR_TXT, encoding="utf-8")
    return d


def test_serves_the_technical_fields(client, quarantined):
    r = client.get(f"/api/jobs/{JOB}/failure")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == JOB
    assert body["cause"] == "out-of-memory"
    assert body["device"] == "cuda, then cpu"
    assert body["model"] == "htdemucs_6s"
    # "stage: Error: Processing failed" must keep everything after the first
    # colon, or the most useful field arrives as a bare "Error".
    assert body["stage"] == "Error: Processing failed"
    assert "SeparationError" in body["exception"]


def test_serves_the_stderr_tail(client, quarantined):
    body = client.get(f"/api/jobs/{JOB}/failure").json()
    assert body["tail"] == [
        "torch.OutOfMemoryError: CUDA out of memory.",
        "Tried to allocate 2.40 GiB",
    ]


def test_never_serves_the_title_or_source_url(client, quarantined):
    """The whole point of parsing error.txt instead of serving it: these issues
    are public, and what the user was working on is theirs to disclose."""
    r = client.get(f"/api/jobs/{JOB}/failure")
    assert TITLE not in r.text
    assert SOURCE not in r.text
    body = r.json()
    assert "title" not in body
    assert "source" not in body


def test_404_when_the_job_never_failed(client, tmp_path):
    assert client.get(f"/api/jobs/{JOB}/failure").status_code == 404


def test_404_for_a_malformed_job_id(client):
    assert client.get("/api/jobs/not-a-job-id/failure").status_code == 404


def test_traversal_out_of_the_quarantine_is_refused(client):
    """The id pattern already rejects separators; this pins it at the route so a
    future loosening of JOB_ID_RE cannot turn this into an arbitrary file read."""
    for evil in ("../../etc/passwd", "..%2f..%2fsecret", "failed"):
        assert client.get(f"/api/jobs/{evil}/failure").status_code == 404
