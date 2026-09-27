"""POST /api/jobs/{id}/audio-tags: tags for a track imported before they were read (#699).

No network anywhere here: yt-dlp and ffprobe are patched. What matters is the
validation order, that a stored URL which is not one we accept is never
fetched, that a failed lookup is an ordinary "nothing found", and that found
tags outlive a restart (the registry and metadata.json both carry them).
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
from app.core.models import Job
from app.core.registry import _jobs

LITHIUM_URL = "https://www.youtube.com/watch?v=pkcJEvMcnEg"
QUEEN = {"id": "Q15862", "name": "Queen", "englishName": "Queen"}
NIRVANA = {"id": "Q11649", "name": "Nirvana", "englishName": "Nirvana"}


def _tags_identity(artist, title, album=None, duration=None):
    """The identity a track gets from its tags alone: what every lookup here
    finds, since conftest keeps AcoustID and MusicBrainz offline and no
    AcoustID key is set."""
    return {
        "source": "tags",
        "score": 0.0,
        "recording_mbid": None,
        "title": title,
        "artist": artist,
        "artist_mbids": [],
        "album": album,
        "release_group_mbid": None,
        "release_group_type": None,
        "secondary_types": [],
        "year": None,
        "duration": duration,
    }


# What yt-dlp really returned for it: no music fields, the band as channel.
LITHIUM_INFO = {
    "artist": None,
    "artists": None,
    "track": None,
    "album": None,
    "channel": "Nirvana",
    "uploader": "Nirvana",
    "title": "Nirvana - Lithium (Official Music Video)",
}


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    yield
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _done_job(job_id="aaaaaaaaaaaa", *, source_url=LITHIUM_URL, **fields) -> Job:
    job = Job(id=job_id, status="done", title="Lithium", source_url=source_url, **fields)
    _jobs[job.id] = job
    (jobs_mod.JOBS_DIR / job.id / "stems").mkdir(parents=True)
    return job


def test_a_malformed_id_is_404(client):
    with patch("app.api.jobs.fetch_audio_tags") as fetch:
        assert client.post("/api/jobs/not-a-job/audio-tags").status_code == 404
        assert client.post("/api/jobs/aaaa..aaaaaa/audio-tags").status_code == 404
    fetch.assert_not_called()


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fetc", "aaaa/../aaaa"])
def test_a_traversing_id_never_reaches_a_lookup(client, job_id):
    """Some of these are collapsed before routing and nothing answers POST
    there (405); the rest arrive as a job id and are refused (404). Neither
    may be a 500, and none may reach the filesystem or the network."""
    with (
        patch("app.api.jobs.fetch_audio_tags") as fetch,
        patch("app.api.jobs.probe_tags") as probe,
    ):
        r = client.post(f"/api/jobs/{job_id}/audio-tags")
    assert r.status_code in (404, 405), r.status_code
    fetch.assert_not_called()
    probe.assert_not_called()


def test_an_unknown_job_is_404(client):
    assert client.post("/api/jobs/bbbbbbbbbbbb/audio-tags").status_code == 404


def test_a_job_that_is_not_finished_is_409(client):
    job = _done_job()
    job.status = "separating"
    with patch("app.api.jobs.fetch_audio_tags") as fetch:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 409
    fetch.assert_not_called()


def test_tags_and_band_already_there_are_returned_untouched(client):
    job = _done_job(audio_tags={"artist": "Queen", "title": "Bicycle Race"}, artist=QUEEN)
    with (
        patch("app.api.jobs.fetch_audio_tags") as fetch,
        patch("app.api.jobs.probe_tags") as probe,
        patch("app.pipeline.identify.find_band") as find,
    ):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": {"artist": "Queen", "title": "Bicycle Race"},
        "artist": QUEEN,
        "has_lyrics": False,
        "identity": _tags_identity("Queen", "Bicycle Race"),
        "work": None,
    }
    fetch.assert_not_called()
    probe.assert_not_called()
    find.assert_not_called()


def test_tags_with_no_artist_look_up_no_band(client):
    job = _done_job(audio_tags={"title": "Bicycle Race"})
    with patch("app.pipeline.identify.find_band") as find:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json() == {
        "audio_tags": {"title": "Bicycle Race"},
        "artist": None,
        "has_lyrics": False,
        "identity": None,
        "work": None,
    }
    find.assert_not_called()


def test_tags_already_there_get_their_band(client):
    """A track imported after tags were read but before the pipeline found
    bands has tags and no band: the band is looked up from the tags it has,
    and kept wherever the tags are kept."""
    job = _done_job(audio_tags={"artist": "Queen", "title": "Bicycle Race"})
    meta_path = jobs_mod.JOBS_DIR / job.id / "metadata.json"
    meta_path.write_text(json.dumps({"title": "Lithium", "bpm": 123}), encoding="utf-8")
    with (
        patch("app.api.jobs.fetch_audio_tags") as fetch,
        patch("app.pipeline.identify.find_band", return_value=QUEEN) as find,
    ):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": {"artist": "Queen", "title": "Bicycle Race"},
        "artist": QUEEN,
        "has_lyrics": False,
        "identity": _tags_identity("Queen", "Bicycle Race"),
        "work": None,
    }
    fetch.assert_not_called()
    assert find.call_args.args == ("Queen",)
    assert job.artist == QUEEN
    registry = json.loads((jobs_mod.JOBS_DIR / "registry.json").read_text(encoding="utf-8"))
    [record] = [j for j in registry["jobs"] if j["id"] == job.id]
    assert record["artist"] == QUEEN
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta == {
        "title": "Lithium",
        "bpm": 123,
        "artist": QUEEN,
        "identity": _tags_identity("Queen", "Bicycle Race"),
    }


def test_tags_found_now_get_their_band_too(client, fake_ydl):
    job = _done_job()
    fake_ydl.replies = [dict(LITHIUM_INFO)]
    with patch("app.pipeline.identify.find_band", return_value=NIRVANA) as find:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["artist"] == NIRVANA
    assert find.call_args.args == ("Nirvana",)
    assert job.artist == NIRVANA
    assert job.audio_tags["artist"] == "Nirvana"


def test_a_band_that_cannot_be_found_leaves_the_tags_kept(client):
    """Offline (conftest's stand-in for the network): the tags found are kept,
    the band is not, and nothing about it is an error."""
    job = _done_job(source_url="local:Lithium")
    (jobs_mod.JOBS_DIR / job.id / "source.flac").write_bytes(b"fLaC")
    found = {"artist": "Nirvana", "title": "Lithium"}
    with patch("app.api.jobs.probe_tags", return_value=found):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": found,
        "artist": None,
        "has_lyrics": False,
        "identity": _tags_identity("Nirvana", "Lithium"),
        "work": None,
    }
    assert job.audio_tags == found
    assert job.artist is None


def test_a_lookup_already_running_for_the_track_is_409(client):
    job = _done_job()
    jobs_mod._TAG_LOOKUPS.add(job.id)
    with patch("app.api.jobs.fetch_audio_tags") as fetch:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 409
    fetch.assert_not_called()


def test_an_upload_is_read_from_its_kept_source(client):
    job = _done_job(source_url="local:Lithium")
    source = jobs_mod.JOBS_DIR / job.id / "source.flac"
    source.write_bytes(b"fLaC")
    found = {"artist": "Nirvana", "title": "Lithium", "album": "Nevermind"}
    with (
        patch("app.api.jobs.probe_tags", return_value=found) as probe,
        patch("app.api.jobs.fetch_audio_tags") as fetch,
    ):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": found,
        "artist": None,
        "has_lyrics": False,
        "identity": _tags_identity("Nirvana", "Lithium", "Nevermind"),
        "work": None,
    }
    probe.assert_called_once_with(source.resolve())
    fetch.assert_not_called()
    assert job.audio_tags == found


def test_an_upload_with_no_kept_source_has_nothing_to_read(client):
    job = _done_job(source_url="local:Lithium")
    with patch("app.api.jobs.probe_tags") as probe:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": None,
        "artist": None,
        "has_lyrics": False,
        "identity": None,
        "work": None,
    }
    probe.assert_not_called()


class _FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: records what it was asked, fetches nothing."""

    calls: list[tuple[dict, str, bool]] = []
    replies: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        type(self).calls.append((self.opts, url, download))
        reply = type(self).replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_ydl():
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.replies = []
    with patch("app.pipeline.download.YoutubeDL", _FakeYoutubeDL):
        yield _FakeYoutubeDL


def test_a_link_is_looked_up_without_downloading_and_the_tags_kept(client, fake_ydl):
    job = _done_job()
    meta_path = jobs_mod.JOBS_DIR / job.id / "metadata.json"
    meta_path.write_text(
        json.dumps({"title": "Lithium", "bpm": 123, "audio_tags": None}), encoding="utf-8"
    )
    fake_ydl.replies = [dict(LITHIUM_INFO)]

    r = client.post(f"/api/jobs/{job.id}/audio-tags")

    expected = {"artist": "Nirvana", "title": "Lithium (Official Music Video)"}
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": expected,
        "artist": None,
        "has_lyrics": False,
        "identity": _tags_identity("Nirvana", "Lithium"),
        "work": None,
    }
    [(opts, url, download)] = fake_ydl.calls
    assert url == LITHIUM_URL
    assert download is False, "metadata only"
    assert opts["allowed_extractors"] == ["youtube", "soundcloud"], "the SSRF boundary (#173)"
    assert "cookiefile" not in opts, "cookies only after a bot check (#432)"
    assert job.audio_tags == expected
    # Survives a restart: in the registry, and in metadata.json with nothing
    # else in that file changed.
    registry = json.loads((jobs_mod.JOBS_DIR / "registry.json").read_text(encoding="utf-8"))
    [record] = [j for j in registry["jobs"] if j["id"] == job.id]
    assert record["audio_tags"] == expected
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta == {
        "title": "Lithium",
        "bpm": 123,
        "audio_tags": expected,
        "identity": _tags_identity("Nirvana", "Lithium"),
    }


def test_a_bot_check_is_retried_with_cookies(client, fake_ydl, tmp_path):
    job = _done_job()
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    fake_ydl.replies = [
        RuntimeError("Sign in to confirm you're not a bot"),
        dict(LITHIUM_INFO),
    ]
    with patch("app.pipeline.download.get_cookies_file", return_value=cookies):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["audio_tags"]["artist"] == "Nirvana"
    first, second = fake_ydl.calls
    assert "cookiefile" not in first[0]
    assert second[0]["cookiefile"] == cookies


def test_a_stored_url_that_is_not_one_we_accept_is_never_fetched(client, fake_ydl):
    job = _done_job(source_url="https://example.com/lithium.mp3")
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": None,
        "artist": None,
        "has_lyrics": False,
        "identity": None,
        "work": None,
    }
    assert fake_ydl.calls == []


def test_a_failed_lookup_is_nothing_found(client):
    job = _done_job()
    with patch("app.api.jobs.fetch_audio_tags", side_effect=RuntimeError("HTTP Error 403")):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": None,
        "artist": None,
        "has_lyrics": False,
        "identity": None,
        "work": None,
    }
    assert "403" not in r.text
    assert job.audio_tags is None
    assert job.id not in jobs_mod._TAG_LOOKUPS, "the guard is released on failure"


def test_a_lookup_that_takes_too_long_is_nothing_found(client, monkeypatch):
    job = _done_job()
    monkeypatch.setattr(jobs_mod, "TIMEOUT_FETCH_TAGS", 0.05)
    with patch("app.api.jobs.fetch_audio_tags", side_effect=lambda url: time.sleep(0.5)):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json() == {
        "audio_tags": None,
        "artist": None,
        "has_lyrics": False,
        "identity": None,
        "work": None,
    }
    assert job.audio_tags is None
