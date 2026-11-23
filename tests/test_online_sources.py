"""Online sources (#1089): search and link import are opt-in, and the server
enforces it.

Off by default, off for a settings file from before the key existed, and off
for anything stored that is not a JSON true. While off, every endpoint that
would start yt-dlp answers 403 "online_sources_off", and the download stage
refuses too, so a link queued earlier is not fetched either. A local upload
never depends on it. Turning it on is saved and applies to the next request,
without a restart.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.api import search as api_search
from app.core import settings as _settings
from app.core.models import Job
from app.core.online_sources import (
    ONLINE_SOURCES_OFF_CODE,
    ONLINE_SOURCES_OFF_DETAIL,
    OnlineSourcesOff,
    require_online_sources,
)
from app.core.registry import _jobs
from app.pipeline import download as dl_mod
from app.pipeline import preview as preview_mod
from app.pipeline import search as search_mod
from app.pipeline.errors import classify_failure

LINK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PLAYLIST = "https://www.youtube.com/playlist?list=PLabcdefghijklmnop"
JOB = "0a0a0a0a0a01"


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    api_search._clear_cache()
    preview_mod._clear_cache()
    yield
    _jobs.clear()
    api_search._clear_cache()
    preview_mod._clear_cache()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path)
    with (
        patch("app.api.jobs.jobqueue.enqueue", lambda job_id, **_: None),
        patch("app.api.playlist.jobqueue.enqueue", lambda job_id, **_: None),
        patch("app.api.jobs._probe_duration", return_value=60.0),
    ):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _no_yt_dlp(*_a, **_k):
    raise AssertionError("yt-dlp must not run while online sources are off")


@pytest.fixture
def yt_dlp_forbidden(monkeypatch):
    """Any YoutubeDL built anywhere fails the test."""
    for mod in (dl_mod, search_mod, preview_mod):
        monkeypatch.setattr(mod, "YoutubeDL", _no_yt_dlp)


def _refused(r) -> None:
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": ONLINE_SOURCES_OFF_DETAIL, "code": ONLINE_SOURCES_OFF_CODE}


def _stopped_link_job() -> Job:
    job = Job(id=JOB, status="stopped", source_url=LINK, title="Song")
    _jobs[job.id] = job
    return job


def _done_link_job(tmp_path) -> Job:
    job = Job(id=JOB, status="done", source_url=LINK, title="Song", selected_stems=["vocals"])
    stems = tmp_path / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    (stems / "vocals.wav").write_bytes(b"RIFF-vocals")
    job.stems.append({"name": "vocals", "url": f"/api/jobs/{JOB}/stems/vocals.wav"})
    _jobs[job.id] = job
    return job


# ── the default ─────────────────────────────────────────────────────────────


def test_a_fresh_install_has_them_off(client):
    assert _settings.get_online_sources() is False
    assert client.get("/api/settings").json()["online_sources"] is False


def test_a_settings_file_without_the_key_has_them_off(client):
    # An install from before the setting existed: it never accepted anything.
    _settings._SETTINGS_PATH.write_text(
        json.dumps({"allow_network": True, "cookies_file": "", "max_duration_sec": 1200}),
        encoding="utf-8",
    )
    _settings._state = None
    assert _settings.get_online_sources() is False
    assert client.get("/api/settings").json()["online_sources"] is False


@pytest.mark.parametrize("stored", ["true", 1, "1", None, [], {}])
def test_only_a_stored_json_true_turns_them_on(client, stored):
    _settings._SETTINGS_PATH.write_text(json.dumps({"online_sources": stored}), encoding="utf-8")
    _settings._state = None
    assert _settings.get_online_sources() is False


# ── turning it on and off ───────────────────────────────────────────────────


def test_turning_them_on_is_saved_and_applies_to_the_next_request(client, monkeypatch):
    monkeypatch.setattr(
        api_search, "search", lambda q, s, k, n: {"items": [], "max_duration_sec": 1200}
    )
    _refused(client.post("/api/search", json={"query": "daft punk"}))

    r = client.post("/api/settings", json={"online_sources": True})
    assert r.status_code == 200
    assert r.json()["online_sources"] is True
    on_disk = json.loads(_settings._SETTINGS_PATH.read_text(encoding="utf-8"))
    assert on_disk["online_sources"] is True

    # No restart: the very next request is let through.
    assert client.post("/api/search", json={"query": "daft punk"}).status_code == 200

    # A restart reads it back from the file.
    _settings._state = None
    assert _settings.get_online_sources() is True


def test_turning_them_off_applies_to_the_next_request(client, monkeypatch, online_sources_on):
    monkeypatch.setattr(
        api_search, "search", lambda q, s, k, n: {"items": [], "max_duration_sec": 1200}
    )
    assert client.post("/api/search", json={"query": "daft punk"}).status_code == 200
    assert client.post("/api/settings", json={"online_sources": False}).status_code == 200
    _refused(client.post("/api/search", json={"query": "another one"}))


@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [True]])
def test_only_true_or_false_is_taken(client, value):
    r = client.post("/api/settings", json={"online_sources": value})
    assert r.status_code == 422
    assert r.json()["detail"] == "online_sources must be true or false"
    assert _settings.get_online_sources() is False


# ── every endpoint that would start yt-dlp refuses while off ────────────────


GATED = [
    ("POST", "/api/search", {"json": {"query": "daft punk"}}),
    ("GET", f"/api/search/preview?url={LINK}", {}),
    ("POST", "/api/playlist/preview", {"json": {"url": PLAYLIST}}),
    ("POST", "/api/playlist", {"json": {"url": PLAYLIST}}),
    ("POST", "/api/jobs", {"json": {"url": LINK}}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), GATED)
def test_each_endpoint_refuses_while_off(client, yt_dlp_forbidden, method, path, kwargs):
    _refused(client.request(method, path, **kwargs))
    assert _jobs == {}, "a refused request must not register anything"


@pytest.mark.parametrize(("method", "path", "kwargs"), GATED)
def test_the_refusal_comes_before_the_body_is_read(client, method, path, kwargs):
    # Not a 422 for a bad body: the answer while off is the same for any body.
    _refused(client.request(method, path, content=b"not json"))


def test_extract_of_a_stopped_link_refuses_while_off(client, yt_dlp_forbidden):
    job = _stopped_link_job()
    _refused(client.post(f"/api/jobs/{JOB}/extract"))
    assert job.status == "stopped", "the refusal must leave the job as it was"


def test_fill_stems_of_a_link_refuses_while_off(client, tmp_path, yt_dlp_forbidden):
    job = _done_link_job(tmp_path)
    _refused(client.post(f"/api/jobs/{JOB}/fill-stems", json={"stems": ["drums"]}))
    assert job.stem_fill == "none", "the refusal must not start a fill"


def test_the_source_list_is_not_gated(client):
    # It lists what search could offer and asks nothing of anyone.
    assert client.get("/api/search/sources").status_code == 200


# ── and each one works as before while on ───────────────────────────────────


def test_search_works_while_on(client, monkeypatch, online_sources_on):
    monkeypatch.setattr(
        api_search,
        "search",
        lambda q, s, k, n: {"items": [{"title": "Around the World"}], "max_duration_sec": 1200},
    )
    r = client.post("/api/search", json={"query": "daft punk"})
    assert r.status_code == 200
    assert r.json()["items"] == [{"title": "Around the World"}]


def test_preview_gets_past_the_gate_while_on(client, monkeypatch, online_sources_on):
    def nothing_playable(url):
        raise preview_mod.PreviewUnavailable("no audio")

    monkeypatch.setattr(api_search, "resolve_preview", nothing_playable)
    assert client.get(f"/api/search/preview?url={LINK}").status_code == 404


def test_playlists_work_while_on(client, monkeypatch, online_sources_on):
    expanded = {
        "playlist_title": "Mix",
        "playlist_url": PLAYLIST,
        "items": [{"url": LINK, "title": "One", "duration": 200, "thumbnail": None}],
        "unavailable": 0,
        "truncated": False,
    }
    monkeypatch.setattr("app.api.playlist.expand_playlist", lambda url, limit: expanded)
    assert client.post("/api/playlist/preview", json={"url": PLAYLIST}).json()["will_queue"] == 1
    r = client.post("/api/playlist", json={"url": PLAYLIST})
    assert r.status_code == 200
    assert r.json()["queued"] == 1


def test_a_link_import_works_while_on(client, online_sources_on):
    r = client.post("/api/jobs", json={"url": LINK})
    assert r.status_code == 200
    assert _jobs[r.json()["job_id"]].source_url == LINK


def test_extract_of_a_stopped_link_works_while_on(client, online_sources_on):
    _stopped_link_job()
    r = client.post(f"/api/jobs/{JOB}/extract")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"


# ── a local upload never depends on it ──────────────────────────────────────


def test_a_local_upload_works_while_off(client, yt_dlp_forbidden):
    assert _settings.get_online_sources() is False
    data = io.BytesIO(b"ID3" + b"\x00" * 128)
    r = client.post("/api/jobs", files={"file": ("song.mp3", data, "audio/mpeg")})
    assert r.status_code == 200, r.text
    assert _jobs[r.json()["job_id"]].source_url.startswith("local:")


# ── the pipeline refuses too ────────────────────────────────────────────────


def test_no_youtubedl_options_are_built_while_off():
    with pytest.raises(OnlineSourcesOff):
        dl_mod._base_ydl_opts(dl_mod._ALLOWED_EXTRACTORS)


def test_a_queued_link_is_not_fetched_while_off(tmp_path, yt_dlp_forbidden):
    # Queued before the setting was turned off, or kept from before the update.
    job = Job(id=JOB, status="queued", source_url=LINK)
    with pytest.raises(OnlineSourcesOff):
        dl_mod.download(job, LINK, tmp_path)
    assert job.status == "queued", "it must not claim to be downloading"


def test_the_failure_is_classified_and_not_retried():
    exc = OnlineSourcesOff()
    assert classify_failure(repr(exc)) == "online-sources-off"
    assert dl_mod._is_retriable(exc) is False


def test_search_and_preview_refuse_at_the_source_too(yt_dlp_forbidden):
    with pytest.raises(OnlineSourcesOff):
        search_mod.search("daft punk", "youtube", "track")
    with pytest.raises(OnlineSourcesOff):
        preview_mod.resolve(LINK)
    with pytest.raises(OnlineSourcesOff):
        dl_mod.expand_playlist(PLAYLIST, 10)
    with pytest.raises(OnlineSourcesOff):
        dl_mod.fetch_audio_tags(LINK)


def test_require_lets_through_once_on(online_sources_on):
    require_online_sources()
    assert dl_mod._base_ydl_opts(dl_mod._ALLOWED_EXTRACTORS)["allowed_extractors"]


def test_a_links_tag_lookup_finds_nothing_while_off(monkeypatch):
    # The rest of the tag lookup (MusicBrainz, the band, lyrics) is not gated;
    # only asking the link, which is yt-dlp, is.
    monkeypatch.setattr(jobs_api, "fetch_audio_tags", _no_yt_dlp)
    job = Job(id=JOB, status="done", source_url=LINK)
    assert jobs_api._lookup_audio_tags(job) is None
