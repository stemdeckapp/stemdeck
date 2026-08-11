from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.registry import _jobs
from app.pipeline import jobqueue
from app.pipeline.download import (
    _ALLOWED_PLAYLIST_EXTRACTORS,
    InvalidPlaylistURL,
    expand_playlist,
    validate_playlist_url,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


class _FakeYDL:
    """Stands in for YoutubeDL so nothing in this file touches the network.
    Records the options it was constructed with so the SSRF allowlist can be
    asserted on."""

    last_opts: dict = {}
    info: dict = {}

    def __init__(self, opts):
        type(self).last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        type(self).last_url = url
        return type(self).info


def _entries(n: int, *, duration: int = 200) -> list[dict]:
    return [
        {
            "url": f"https://www.youtube.com/watch?v=vid{i:08d}",
            "title": f"Track {i}",
            "duration": duration,
        }
        for i in range(n)
    ]


@pytest.fixture
def fake_ydl():
    _FakeYDL.info = {"title": "My Playlist", "entries": _entries(3)}
    with patch("app.pipeline.download.YoutubeDL", _FakeYDL):
        yield _FakeYDL


@pytest.fixture
def client(fake_ydl):
    # Stub the worker so nothing actually runs; these tests cover the API.
    with patch("app.pipeline.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


# ── URL validation ───────────────────────────────────────────────────────────


def test_accepts_a_youtube_playlist_url():
    out = validate_playlist_url("https://www.youtube.com/playlist?list=PLabcdef123")
    assert out == "https://www.youtube.com/playlist?list=PLabcdef123"


def test_accepts_a_watch_url_carrying_a_list():
    out = validate_playlist_url("https://www.youtube.com/watch?v=abc12345678&list=PLxyz987")
    assert out == "https://www.youtube.com/playlist?list=PLxyz987"


def test_accepts_a_soundcloud_set():
    url = "https://soundcloud.com/someuser/sets/my-set"
    assert validate_playlist_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/playlist?list=PLabc",
        "file:///etc/passwd",
        "http://127.0.0.1:8000/playlist?list=PLabc",
        "https://www.youtube.com/watch?v=abc12345678",  # single video, no list
        "https://www.youtube.com/playlist?list=RD1234567",  # algorithmic radio
        "https://soundcloud.com/someuser",  # a profile, not a set
        "https://soundcloud.com/someuser/a-track",
        "",
    ],
)
def test_rejects_everything_else(url):
    with pytest.raises(InvalidPlaylistURL):
        validate_playlist_url(url)


def test_playlist_url_is_still_rejected_by_the_single_track_endpoint(client):
    """Importing one track and importing a playlist stay separate paths."""
    r = client.post("/api/jobs", json={"url": "https://www.youtube.com/playlist?list=PLabcdef123"})
    assert r.status_code == 422


# ── expansion ────────────────────────────────────────────────────────────────


def test_expansion_never_allows_the_generic_extractor(fake_ydl):
    """The #173 boundary. A playlist needs youtube:tab, which the single-video
    allowlist excludes, so it has its own list -- and that list must still keep
    "generic" out, or a crafted URL could make yt-dlp fetch anything."""
    expand_playlist("https://www.youtube.com/playlist?list=PLabc", 50)
    assert fake_ydl.last_opts["allowed_extractors"] == _ALLOWED_PLAYLIST_EXTRACTORS
    assert "generic" not in fake_ydl.last_opts["allowed_extractors"]


def test_expansion_caps_what_it_asks_for(fake_ydl):
    expand_playlist("https://www.youtube.com/playlist?list=PLabc", 7)
    assert fake_ydl.last_opts["playlistend"] == 7


def test_entries_are_revalidated(fake_ydl):
    """Entry URLs are data from an external service, not something to trust."""
    fake_ydl.info = {
        "title": "Mixed",
        "entries": [
            {"url": "https://www.youtube.com/watch?v=good1234567", "title": "Fine"},
            {"url": "https://evil.example.com/pwn", "title": "Hostile"},
            {"url": "", "title": "Deleted video"},
        ],
    }
    out = expand_playlist("https://www.youtube.com/playlist?list=PLabc", 50)
    assert [i["url"] for i in out["items"]] == ["https://www.youtube.com/watch?v=good1234567"]
    assert out["unavailable"] == 2


def test_playlist_title_falls_back(fake_ydl):
    fake_ydl.info = {"entries": _entries(1)}
    assert (
        expand_playlist("https://www.youtube.com/playlist?list=PLabc", 50)["playlist_title"]
        == "Playlist"
    )


# ── preview ──────────────────────────────────────────────────────────────────


def test_preview_reports_the_shape_without_creating_jobs(client):
    r = client.post(
        "/api/playlist/preview", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["playlist_title"] == "My Playlist"
    assert body["total_found"] == 3
    assert body["will_queue"] == 3
    assert _jobs == {}, "preview must not create anything"


def test_preview_counts_tracks_that_are_too_long(client, fake_ydl, monkeypatch):
    import app.api.playlist as playlist_mod

    monkeypatch.setattr(playlist_mod, "get_max_duration_sec", lambda: 100)
    fake_ydl.info = {"title": "Long", "entries": _entries(2, duration=5000)}
    body = client.post(
        "/api/playlist/preview", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    ).json()
    assert body["skipped_too_long"] == 2
    assert body["will_queue"] == 0


def test_preview_422s_on_a_non_playlist(client):
    r = client.post("/api/playlist/preview", json={"url": "https://example.com/x"})
    assert r.status_code == 422


# ── create ───────────────────────────────────────────────────────────────────


def test_creates_one_job_per_track_in_order(client):
    body = client.post(
        "/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    ).json()
    assert body["queued"] == 3
    assert [j["title"] for j in body["jobs"]] == ["Track 0", "Track 1", "Track 2"]
    assert len(_jobs) == 3
    assert all(j.status == "queued" for j in _jobs.values())


def test_created_jobs_carry_their_title_before_downloading(client):
    """So queue rows read as track names immediately rather than URLs."""
    body = client.post(
        "/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    ).json()
    job = _jobs[body["jobs"][0]["job_id"]]
    assert job.title == "Track 0"
    assert job.source_url.startswith("https://www.youtube.com/watch?v=")


def test_returns_the_playlist_title_for_the_folder(client):
    body = client.post(
        "/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    ).json()
    assert body["playlist_title"] == "My Playlist"


def test_partially_fills_when_the_queue_is_nearly_full(client, fake_ydl, monkeypatch):
    """The preview already showed a number; a 503 after the user agreed is worse
    than queueing what fits and saying how many did not."""
    import app.api.playlist as playlist_mod

    monkeypatch.setattr(playlist_mod, "MAX_PENDING_JOBS", 2)
    fake_ydl.info = {"title": "Big", "entries": _entries(5)}
    body = client.post(
        "/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
    ).json()
    assert body["queued"] == 2
    assert body["skipped_no_capacity"] == 3


def test_503_only_when_there_is_no_room_at_all(client, monkeypatch):
    import app.api.playlist as playlist_mod

    monkeypatch.setattr(playlist_mod, "_capacity_left", lambda: 0)
    r = client.post("/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"})
    assert r.status_code == 503


def test_422_when_nothing_in_the_playlist_is_importable(client, fake_ydl):
    fake_ydl.info = {"title": "Empty", "entries": []}
    r = client.post("/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"})
    assert r.status_code == 422


def test_every_created_job_is_enqueued(client, fake_ydl):
    """Playlist jobs go through the same queue as any other import."""
    seen: list[str] = []
    with patch("app.pipeline.jobqueue.enqueue", seen.append):
        body = client.post(
            "/api/playlist", json={"url": "https://www.youtube.com/playlist?list=PLabc"}
        ).json()
    assert seen == [j["job_id"] for j in body["jobs"]]
    assert jobqueue.running_id() is None
