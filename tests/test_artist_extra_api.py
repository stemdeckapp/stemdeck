"""GET /api/jobs/{id}/artist-extra: a band's Discogs profile for the artist box.

No network: conftest keeps api.discogs.com offline; the answers here are the
recorded shapes in test_discogs.py. The token is made up and set through the
real settings store, so the endpoint reads it the way it does in the app.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
from app.core import settings
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import discogs, ratelimit
from tests.test_discogs import NIHIL_ANSWERS, TOKEN, FakeDiscogs

NIHIL_TAGS = {"artist": "NIHIL", "title": "Barro"}


@pytest.fixture(autouse=True)
def _isolate():
    _jobs.clear()
    jobs_mod._DISCOGS_LOOKUPS.clear()
    ratelimit.DISCOGS.reset()
    yield
    _jobs.clear()
    jobs_mod._DISCOGS_LOOKUPS.clear()
    ratelimit.DISCOGS.reset()


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(ratelimit.DISCOGS, "interval", 0.0)
    answers = FakeDiscogs(NIHIL_ANSWERS)
    monkeypatch.setattr(discogs, "_send", answers)
    return answers


def _job(job_id="59507f8b7d08", **fields) -> Job:
    job = Job(
        id=job_id,
        status="done",
        title="NIHIL | Barro",
        audio_tags=NIHIL_TAGS,
        identity={
            "source": "tags",
            "score": 0.0,
            "title": "Barro",
            "artist": "NIHIL",
            "artist_mbids": [],
        },
        **fields,
    )
    _jobs[job.id] = job
    return job


def test_the_band_profile_is_served(client, fake):
    settings.set_discogs_token(TOKEN)
    _job()
    r = client.get("/api/jobs/59507f8b7d08/artist-extra")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 555501
    assert body["name"] == "Nihil"
    assert body["url"] == "https://www.discogs.com/artist/555501-Nihil-5"
    assert body["profile"][0].startswith("Portuguese sludge band")
    assert body["members"]["current"] == ["Rui Barros", "Ana Lima"]
    assert {"kind": "bandcamp", "url": "https://nihil.bandcamp.com/"} in body["links"]
    assert body["releases"][0] == {"year": "2019", "title": "Barro"}
    assert set(body) == {
        "id",
        "name",
        "real_name",
        "profile",
        "members",
        "groups",
        "links",
        "releases",
        "url",
    }
    # The token is sent to Discogs, and nowhere else.
    assert TOKEN not in r.text
    assert all(req.get_header("Authorization") == f"Discogs token={TOKEN}" for req in fake.requests)


def test_no_token_is_404_and_asks_discogs_nothing(client, fake):
    _job()
    r = client.get("/api/jobs/59507f8b7d08/artist-extra")
    assert r.status_code == 404
    assert fake.requests == []


def test_nothing_confident_is_404(client, monkeypatch):
    settings.set_discogs_token(TOKEN)
    monkeypatch.setattr(ratelimit.DISCOGS, "interval", 0.0)
    monkeypatch.setattr(discogs, "_send", FakeDiscogs({"database/search": {"results": []}}))
    _job()
    r = client.get("/api/jobs/59507f8b7d08/artist-extra")
    assert r.status_code == 404
    assert r.json() == {"detail": "no artist details"}


def test_discogs_offline_is_404_not_500(client):
    settings.set_discogs_token(TOKEN)
    _job()
    r = client.get("/api/jobs/59507f8b7d08/artist-extra")
    assert r.status_code == 404
    assert TOKEN not in r.text


def test_an_unknown_job_is_404(client, fake):
    settings.set_discogs_token(TOKEN)
    assert client.get("/api/jobs/bbbbbbbbbbbb/artist-extra").status_code == 404
    assert fake.requests == []


@pytest.mark.parametrize("job_id", ["not-a-job", "AAAAAAAAAAAA", "aaaa..aaaaaa"])
def test_a_malformed_id_is_404(client, fake, job_id):
    settings.set_discogs_token(TOKEN)
    assert client.get(f"/api/jobs/{job_id}/artist-extra").status_code == 404
    assert fake.requests == []


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fetc", "aaaa/../aaaa"])
def test_a_traversing_id_never_reaches_a_lookup(client, fake, job_id):
    settings.set_discogs_token(TOKEN)
    r = client.get(f"/api/jobs/{job_id}/artist-extra")
    assert r.status_code in (404, 405)
    assert fake.requests == []


def test_a_slow_lookup_answers_within_the_budget(client, monkeypatch):
    settings.set_discogs_token(TOKEN)
    monkeypatch.setattr(jobs_mod, "DISCOGS_LOOKUP_BUDGET_SEC", 0)

    def slow(*args, cancelled, **kwargs):
        import time

        while not cancelled():
            time.sleep(0.01)
        return None

    monkeypatch.setattr(discogs, "artist_for_track", slow)
    _job()
    r = client.get("/api/jobs/59507f8b7d08/artist-extra")
    assert r.status_code == 404


def test_the_answer_is_kept_so_a_second_open_asks_nothing(client, fake):
    settings.set_discogs_token(TOKEN)
    _job()
    assert client.get("/api/jobs/59507f8b7d08/artist-extra").status_code == 200
    asked = len(fake.requests)
    assert client.get("/api/jobs/59507f8b7d08/artist-extra").status_code == 200
    assert len(fake.requests) == asked
