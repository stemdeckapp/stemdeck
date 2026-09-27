"""The AcoustID key setting, and identification through the tag backfill.

The key is the user's: GET and POST /api/settings say only whether one is set
and its last four characters, a rejected one is never echoed back, and none of
it reaches the log. POST /api/jobs/{id}/audio-tags identifies an older track
the way the pipeline does, fingerprinting the upload it kept or, for a link
whose download is gone, its stems summed. No network: every service is a stub.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.pipeline.artist_lookup as al
import app.pipeline.identify as ident
import app.pipeline.musicbrainz as mb
from app.core import settings as _settings
from app.core.models import Job
from app.core.registry import _jobs
from tests.test_identify import (
    FINGERPRINT,
    QUEEN,
    QUEEN_MBID,
    REC,
    AcoustID,
    MusicBrainz,
    _wikidata_entity,
)

KEY = "Zx9Yw8Vu7T"


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    yield
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()


# ── the setting ──


def test_unset_by_default(client):
    data = client.get("/api/settings").json()
    assert data["acoustid_api_key_set"] is False
    assert data["acoustid_api_key_tail"] is None
    assert "acoustid_api_key" not in data


def test_the_key_is_saved_and_never_handed_back(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/api/settings", json={"acoustid_api_key": f"  {KEY}  "})
    assert r.status_code == 200
    assert r.json()["acoustid_api_key_set"] is True
    assert r.json()["acoustid_api_key_tail"] == "7T"
    assert KEY not in r.text
    got = client.get("/api/settings")
    assert KEY not in got.text
    assert got.json()["acoustid_api_key_tail"] == KEY[-2:]
    assert _settings.get_acoustid_api_key() == KEY
    assert KEY not in caplog.text


def test_the_key_survives_a_restart():
    _settings.set_acoustid_api_key(KEY)
    _settings._state = None  # the next read comes from settings.json
    assert _settings.get_acoustid_api_key() == KEY


@pytest.mark.parametrize("value", ["", None])
def test_clearing_the_key(client, value):
    _settings.set_acoustid_api_key(KEY)
    r = client.post("/api/settings", json={"acoustid_api_key": value})
    assert r.status_code == 200
    assert r.json()["acoustid_api_key_set"] is False
    assert _settings.get_acoustid_api_key() is None


@pytest.mark.parametrize(
    "value", ["has space s", "q1", "short7x", "x" * 65, "<script>", 12345, ["a"]]
)
def test_a_key_that_cannot_be_one_is_refused_without_echoing_it(client, value):
    _settings.set_acoustid_api_key(KEY)
    r = client.post("/api/settings", json={"acoustid_api_key": value})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid AcoustID key"
    if isinstance(value, str):
        assert value not in r.text
    assert _settings.get_acoustid_api_key() == KEY, "the old key stays"


# ── the backfill ──


def _done_job(job_id, *, source_url, **fields) -> Job:
    job = Job(
        id=job_id,
        status="done",
        title="Queen - Bohemian Rhapsody (Official Video)",
        source_url=source_url,
        duration_sec=355.0,
        **fields,
    )
    _jobs[job.id] = job
    stems = jobs_mod.JOBS_DIR / job.id / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums", "bass", "other"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    (stems / "original.wav").write_bytes(b"RIFF")
    (jobs_mod.JOBS_DIR / job.id / "metadata.json").write_text(
        json.dumps({"title": job.title}), encoding="utf-8"
    )
    return job


@pytest.fixture
def services(monkeypatch):
    fingerprinted: list[list[Path]] = []

    def fingerprint(audio, **kw):
        fingerprinted.append(list(audio))
        return FINGERPRINT

    monkeypatch.setattr(ident, "fingerprint", fingerprint)
    monkeypatch.setattr(ident, "_acoustid_request", AcoustID())
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz())
    monkeypatch.setattr(al, "_fetch_json", lambda params: _wikidata_entity())
    _settings.set_acoustid_api_key(KEY)
    return fingerprinted


def test_an_old_link_is_identified_from_its_stems(client, services):
    job = _done_job(
        "cccccccccc01",
        source_url="https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
        audio_tags={"artist": "Queen", "title": "Bohemian Rhapsody"},
    )
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    data = r.json()
    # Backward compatible: the keys it always had, plus identity.
    assert {"audio_tags", "artist", "identity"} <= set(data)
    assert data["identity"]["source"] == "acoustid"
    assert data["identity"]["recording_mbid"] == REC
    assert data["identity"]["artist_mbids"] == [QUEEN_MBID]
    assert data["artist"] == QUEEN
    # The stems summed, never original.wav (only the stems not chosen) and
    # never anything outside the job.
    [audio] = services
    stems = (jobs_mod.JOBS_DIR / job.id / "stems").resolve()
    assert audio == [stems / f"{n}.wav" for n in ("vocals", "drums", "bass", "other")]
    assert job.identity == data["identity"]
    meta = json.loads((jobs_mod.JOBS_DIR / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["identity"] == data["identity"]
    assert meta["artist"] == QUEEN
    registry = json.loads((jobs_mod.JOBS_DIR / "registry.json").read_text(encoding="utf-8"))
    [record] = [j for j in registry["jobs"] if j["id"] == job.id]
    assert record["identity"] == data["identity"]


def test_an_old_upload_is_identified_from_the_file_it_kept(client, services):
    job = _done_job(
        "cccccccccc02",
        source_url="local:Queen - Bohemian Rhapsody",
        audio_tags={"artist": "Queen"},
    )
    source = jobs_mod.JOBS_DIR / job.id / "source.flac"
    source.write_bytes(b"fLaC")
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["identity"]["source"] == "acoustid"
    assert services == [[source.resolve()]]


def test_a_job_already_identified_is_not_asked_again(client, services):
    known = {
        "source": "tags",
        "score": 0.0,
        "recording_mbid": None,
        "title": "Bohemian Rhapsody",
        "artist": "Queen",
        "artist_mbids": [],
        "album": None,
        "release_group_mbid": None,
        "release_group_type": None,
        "secondary_types": [],
        "duration": None,
    }
    job = _done_job(
        "cccccccccc03",
        source_url="local:x",
        audio_tags={"artist": "Queen"},
        identity=known,
        artist=QUEEN,
    )
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["identity"] == known
    assert services == [], "nothing fingerprinted"


def test_identification_that_times_out_answers_with_what_there_is(client, services, monkeypatch):
    import time

    monkeypatch.setattr(jobs_mod, "TIMEOUT_IDENTIFY_BACKFILL", 0.1)
    monkeypatch.setattr(
        ident, "identify_and_find_band", lambda *a, **k: time.sleep(0.5) or (None, None)
    )
    monkeypatch.setattr(jobs_mod, "identify_and_find_band", ident.identify_and_find_band)
    job = _done_job("cccccccccc04", source_url="local:x", audio_tags={"artist": "Queen"})
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json()["identity"] is None
    assert job.id not in jobs_mod._TAG_LOOKUPS


def test_a_traversing_id_is_never_identified(client, services):
    for job_id in ("../../etc/passwd", "..%2F..%2Fetc", "cccc..cccccc"):
        r = client.post(f"/api/jobs/{job_id}/audio-tags")
        assert r.status_code in (404, 405)
    assert services == []
