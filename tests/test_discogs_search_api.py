"""GET /api/discogs/artist?q= and GET /api/discogs/artist/{id}: a band typed in
the artist box, looked for on Discogs, and the one the user picked.

No network: conftest keeps api.discogs.com offline and the cache in a
temporary directory; the answers are recorded shapes. The token is made up
and set through the real settings store.
"""

from __future__ import annotations

import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import app.api.discogs as discogs_api
from app.core import settings
from app.pipeline import discogs, ratelimit
from tests.test_discogs import ARTIST_NIHIL, ARTIST_NIHIL_RELEASES, TOKEN, FakeDiscogs

SEARCH_NIHIL = {
    "pagination": {"page": 1, "pages": 1, "per_page": 25, "items": 4, "urls": {}},
    "results": [
        {"id": 777, "type": "artist", "title": "Nihilist", "uri": "/artist/777-Nihilist"},
        {"id": 555501, "type": "artist", "title": "Nihil (5)", "uri": "/artist/555501-Nihil-5"},
        {"id": 12, "type": "artist", "title": "Nihil", "uri": "/artist/12-Nihil"},
        {"id": 12, "type": "artist", "title": "Nihil", "uri": "/artist/12-Nihil"},
        {"id": 99, "type": "label", "title": "Nihil Records"},
        {"id": "x", "type": "artist", "title": "Broken"},
    ],
}
ARTIST_NIHIL_PLAIN = {"id": 12, "name": "Nihil", "profile": "German industrial project."}

ANSWERS = {
    "database/search?type=artist": SEARCH_NIHIL,
    "artists/555501/releases": ARTIST_NIHIL_RELEASES,
    "artists/555501": ARTIST_NIHIL,
    "artists/12": ARTIST_NIHIL_PLAIN,
    "artists/777": {"id": 777, "name": "Nihilist", "profile": ""},
}


@pytest.fixture(autouse=True)
def _limiter(monkeypatch):
    ratelimit.DISCOGS.reset()
    monkeypatch.setattr(ratelimit.DISCOGS, "interval", 0.0)
    monkeypatch.setattr(discogs, "_sleep", lambda s: None)
    yield
    ratelimit.DISCOGS.reset()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake(monkeypatch):
    answers = FakeDiscogs(ANSWERS)
    monkeypatch.setattr(discogs, "_send", answers)
    return answers


def test_search_lists_candidates_exact_names_first(client, fake):
    settings.set_discogs_token(TOKEN)
    r = client.get("/api/discogs/artist", params={"q": "NIHIL"})
    assert r.status_code == 200
    candidates = r.json()["candidates"]
    # Named exactly "Nihil" (the number set aside) first, in Discogs' order;
    # the label, the duplicate and the broken id are gone.
    assert [c["id"] for c in candidates] == [555501, 12, 777]
    assert candidates[0]["name"] == "Nihil (5)"
    assert candidates[0]["profile"].startswith("Portuguese sludge band from Porto")
    assert candidates[1]["profile"] == "German industrial project."
    assert candidates[2]["profile"] == ""
    assert all(set(c) == {"id", "name", "profile"} for c in candidates)
    assert TOKEN not in r.text
    search = next(q for q in fake.requests if "database/search" in q.full_url)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(search.full_url).query))
    assert query["type"] == "artist"
    assert query["q"] == "NIHIL"
    # The token rides in the header only, never in a URL.
    assert all(TOKEN not in q.full_url for q in fake.requests)
    assert all(q.get_header("Authorization") == f"Discogs token={TOKEN}" for q in fake.requests)


def test_search_with_nothing_found_is_an_empty_list(client, monkeypatch):
    settings.set_discogs_token(TOKEN)
    monkeypatch.setattr(discogs, "_send", FakeDiscogs({"database/search": {"results": []}}))
    r = client.get("/api/discogs/artist", params={"q": "Nobody At All"})
    assert r.status_code == 200
    assert r.json() == {"candidates": []}


def test_search_without_a_token_is_404_and_asks_nothing(client, fake):
    r = client.get("/api/discogs/artist", params={"q": "Nihil"})
    assert r.status_code == 404
    assert fake.requests == []


@pytest.mark.parametrize(
    "q",
    ["", "   ", "x" * 101, "Nihil\x00", "Ni‮hil", "a\nb"],
)
def test_search_rejects_a_bad_name(client, fake, q):
    settings.set_discogs_token(TOKEN)
    r = client.get("/api/discogs/artist", params={"q": q})
    assert r.status_code == 422
    assert r.json() == {"detail": "invalid name"}
    assert fake.requests == []


def test_search_with_discogs_offline_is_503_without_the_token(client):
    settings.set_discogs_token(TOKEN)
    r = client.get("/api/discogs/artist", params={"q": "Nihil"})
    assert r.status_code == 503
    assert TOKEN not in r.text


def test_search_is_bounded(client, monkeypatch):
    settings.set_discogs_token(TOKEN)
    monkeypatch.setattr(discogs_api, "DISCOGS_SEARCH_BUDGET_SEC", 0)

    def slow(*args, cancelled, **kwargs):
        while not cancelled():
            time.sleep(0.01)
        return None

    monkeypatch.setattr(discogs, "search_artists", slow)
    started = time.monotonic()
    r = client.get("/api/discogs/artist", params={"q": "Nihil"})
    assert r.status_code == 503
    assert time.monotonic() - started < 5


def test_the_picked_artist_is_served_in_the_artist_extra_shape(client, fake):
    settings.set_discogs_token(TOKEN)
    r = client.get("/api/discogs/artist/555501")
    assert r.status_code == 200
    body = r.json()
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
    assert body["id"] == 555501
    assert body["name"] == "Nihil"
    assert body["url"] == "https://www.discogs.com/artist/555501-Nihil-5"
    assert body["releases"][0] == {"year": "2019", "title": "Barro"}
    assert TOKEN not in r.text


def test_the_picked_artist_without_a_token_is_404(client, fake):
    assert client.get("/api/discogs/artist/555501").status_code == 404
    assert fake.requests == []


def test_an_unknown_artist_is_404(client, fake):
    settings.set_discogs_token(TOKEN)
    r = client.get("/api/discogs/artist/424242")
    assert r.status_code == 404
    assert r.json() == {"detail": "no artist details"}


@pytest.mark.parametrize("artist_id", ["0", "abc", "-5", "1" * 13, "12.5", "%2e%2e"])
def test_a_malformed_artist_id_is_404_and_asks_nothing(client, fake, artist_id):
    settings.set_discogs_token(TOKEN)
    assert client.get(f"/api/discogs/artist/{artist_id}").status_code == 404
    assert fake.requests == []


@pytest.mark.parametrize("path", ["../../etc/passwd", "..%2F..%2Fetc", "..%2F12"])
def test_a_traversing_id_never_reaches_discogs(client, fake, path):
    settings.set_discogs_token(TOKEN)
    r = client.get(f"/api/discogs/artist/{path}")
    assert r.status_code in (404, 405)
    assert fake.requests == []


def test_the_picked_artist_is_bounded(client, monkeypatch):
    settings.set_discogs_token(TOKEN)
    monkeypatch.setattr(discogs_api, "DISCOGS_SEARCH_BUDGET_SEC", 0)

    def slow(*args, cancelled, **kwargs):
        while not cancelled():
            time.sleep(0.01)
        return None

    monkeypatch.setattr(discogs, "artist_by_id", slow)
    started = time.monotonic()
    assert client.get("/api/discogs/artist/555501").status_code == 404
    assert time.monotonic() - started < 5
