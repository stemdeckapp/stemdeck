"""The containment checks on every path built from a job id, and the last
jobs-API branches.

JOB_ID_RE already rejects anything that is not twelve hex characters, so these
re-checks look redundant -- and they are not. A symlink *inside* the library,
named with a perfectly valid id, resolves outside it, and every endpoint that
joins a job id onto JOBS_DIR has to notice. They are cheap and they are the
last thing between a crafted library and reading or writing arbitrary files.

Each one answers 404 rather than 403: whether the path escapes is not something
a caller is entitled to learn.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.core.models import Job
from app.core.registry import _jobs

JOB = "abcdefabcdef"
ESCAPE = "abcdefabcdee"


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A jobs dir containing one real job and one symlink that leaves it."""
    import app.api.jobs as jobs_mod

    root = tmp_path / "jobs"
    root.mkdir()
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", root)

    (root / JOB / "stems").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "stems").mkdir(parents=True)
    (root / ESCAPE).symlink_to(outside, target_is_directory=True)

    (root / "failed").mkdir()
    return root, outside


@pytest.fixture
def client(library):
    from app.main import app

    with TestClient(app) as c:
        yield c


def _done(job_id=JOB, **kw):
    job = Job(id=job_id)
    job.status = "done"
    for k, v in kw.items():
        setattr(job, k, v)
    _jobs[job_id] = job
    return job


# --------------------------------------------------------------------------
# a valid-looking id that resolves outside the library
# --------------------------------------------------------------------------


def test_a_vocal_split_on_an_escaping_id_is_not_found(client):
    _done(ESCAPE)

    r = client.post(f"/api/jobs/{ESCAPE}/vocal-split")

    assert r.status_code == 404


def test_writing_sections_to_an_escaping_id_is_not_found(client):
    _done(ESCAPE)

    r = client.patch(
        f"/api/jobs/{ESCAPE}/sections",
        json={
            "sections": [
                {
                    "id": "s1",
                    "name": "Intro",
                    "kind": "intro",
                    "start": 0.0,
                    "end": 5.0,
                    "color": "#aabbcc",
                }
            ]
        },
    )

    assert r.status_code == 404


def test_reading_beats_for_an_escaping_id_is_not_found(client):
    _done(ESCAPE)

    assert client.get(f"/api/jobs/{ESCAPE}/beats").status_code == 404


def test_failure_evidence_for_an_escaping_id_is_not_found(client, library):
    """JOB_ID_RE rejects "failed", so the quarantine dir can never be addressed
    as a job id -- but a symlink inside it still could."""
    root, outside = library
    _done(ESCAPE)
    (root / "failed" / ESCAPE).symlink_to(outside, target_is_directory=True)

    assert client.get(f"/api/jobs/{ESCAPE}/failure").status_code == 404


# --------------------------------------------------------------------------
# malformed ids on the endpoints that build a path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("job_id", ["zzzzzzzzzzzz", "ABCDEFABCDEF", "abc", "abcdefabcdef0"])
@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/jobs/{}/vocal-split"),
        ("delete", "/api/jobs/{}"),
        ("get", "/api/jobs/{}/failure"),
    ],
)
def test_an_id_that_is_not_twelve_hex_characters_is_not_found(client, job_id, method, path):
    r = getattr(client, method)(path.format(job_id))

    assert r.status_code == 404


# --------------------------------------------------------------------------
# sections: merging into an existing sidecar
# --------------------------------------------------------------------------


def test_saved_sections_are_merged_into_the_existing_metadata(client, library):
    """metadata.json also carries the title, BPM and key. Overwriting the file
    with just the sections would lose everything recovery depends on."""
    root, _ = library
    _done()
    (root / JOB / "metadata.json").write_text(
        json.dumps({"title": "Get Lucky", "bpm": 116}), encoding="utf-8"
    )

    r = client.patch(
        f"/api/jobs/{JOB}/sections",
        json={
            "sections": [
                {
                    "id": "s1",
                    "name": "Intro",
                    "kind": "intro",
                    "start": 0.0,
                    "end": 5.0,
                    "color": "#aabbcc",
                }
            ]
        },
    )

    assert r.status_code == 200
    meta = json.loads((root / JOB / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Get Lucky"
    assert meta["bpm"] == 116
    assert meta["sections_source"] == "manual"
    assert len(meta["sections"]) == 1


def test_hand_edited_sections_are_marked_as_manual(client, library):
    """So a later automatic pass does not silently replace what the user drew."""
    root, _ = library
    job = _done()

    client.patch(
        f"/api/jobs/{JOB}/sections",
        json={
            "sections": [
                {
                    "id": "s1",
                    "name": "Verse",
                    "kind": "verse",
                    "start": 0.0,
                    "end": 5.0,
                    "color": "#aabbcc",
                }
            ]
        },
    )

    assert job.sections_source == "manual"


# --------------------------------------------------------------------------
# the vocal split's presence cards
# --------------------------------------------------------------------------


def test_a_finished_split_extends_the_presence_map(client, library, monkeypatch):
    """The two new cards would otherwise read "--". vocals rides along in the
    scan so presence_for_split can recover the scale the base stems were
    normalised against."""
    import app.api.jobs as jobs_mod

    root, _ = library
    job = _done(stem_presence={"vocals": 80, "drums": 100})
    job.stems = [{"name": "vocals", "url": "u"}, {"name": "drums", "url": "u"}]

    monkeypatch.setattr(jobs_mod, "split_vocals", lambda j, d: ["lead_vocals", "backing_vocals"])
    monkeypatch.setattr(
        jobs_mod,
        "merge_stem_peaks",
        lambda d, names: {"vocals": 0.4, "lead_vocals": 0.3, "backing_vocals": 0.1},
    )

    r = client.post(f"/api/jobs/{JOB}/vocal-split")

    assert r.status_code == 200
    assert job.vocal_split == "done"
    assert set(job.stem_presence) >= {"vocals", "drums", "lead_vocals", "backing_vocals"}
    # The base values are kept, not replaced.
    assert job.stem_presence["drums"] == 100
    # And the new lanes became real stems.
    assert {s["name"] for s in job.stems} >= {"lead_vocals", "backing_vocals"}


def test_a_split_whose_scale_cannot_be_recovered_leaves_presence_alone(
    client, library, monkeypatch
):
    """Without a reference the cards read "--" rather than showing a number
    derived from nothing."""
    import app.api.jobs as jobs_mod

    job = _done(stem_presence={"drums": 100})
    job.stems = [{"name": "drums", "url": "u"}]

    monkeypatch.setattr(jobs_mod, "split_vocals", lambda j, d: ["lead_vocals", "backing_vocals"])
    monkeypatch.setattr(jobs_mod, "merge_stem_peaks", lambda d, names: {})

    client.post(f"/api/jobs/{JOB}/vocal-split")

    assert job.stem_presence == {"drums": 100}


# --------------------------------------------------------------------------
# the upload size check that runs after buffering
# --------------------------------------------------------------------------


def test_a_file_larger_than_the_limit_is_refused_once_it_is_on_disk(tmp_path, monkeypatch):
    """The Content-Length pre-check only catches an obviously oversized body
    (limit + 4 KB). Anything between the two is caught here."""
    import app.api.jobs as jobs_mod
    import app.core.config as cfg
    from app.main import app

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_mod.jobqueue, "enqueue", lambda job_id: None)
    monkeypatch.setattr(jobs_mod, "_probe_duration", lambda p: 60.0)
    # A limit the body clears on Content-Length but fails on measurement.
    monkeypatch.setattr(jobs_mod, "_MAX_UPLOAD_BYTES", 1000)

    with TestClient(app) as c:
        r = c.post("/api/jobs", files={"file": ("big.wav", io.BytesIO(b"x" * 2000), "audio/wav")})

    assert r.status_code == 422
    assert "400 MB" in r.json()["detail"]
    # Refused before a job directory is even created, so there is nothing to
    # clean up. (tmp_path also holds conftest's own _jobs_root, hence the
    # explicit shape rather than "the directory is empty".)
    import re

    assert not [p for p in tmp_path.iterdir() if re.fullmatch(r"[a-f0-9]{12}", p.name)]
