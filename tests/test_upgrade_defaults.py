"""What an install upgrading from 0.20.x gets for the two settings 0.21.0 added.

0.20.x had neither key: every song kept WAV stems, and every import looked up
its lyrics. So a 0.20.x settings.json has no stem_format and no auto_lyrics.
The decision is that it reads exactly like a fresh install (MP3 at 128 kbps,
Detect lyrics off) and that the release notes say so. These tests pin that,
so a later change cannot give upgraders something else without failing here.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core import registry
from app.core import settings as _settings

# Every key 0.20.0 could write (v0.20.0:app/core/settings.py), minus jobs_dir
# and port, which would point the test elsewhere. allow_network stays on so
# TestClient, which is not loopback, is answered.
_SETTINGS_0_20_0 = {
    "acoustid_api_key": "",
    "allow_network": True,
    "auto_delete_days": 30,
    "auto_delete_jobs": False,
    "auto_sections": False,
    "cookies_file": "",
    "demucs_device": "auto",
    "discogs_token": "",
    "export_sample_rate": 44100,
    "max_duration_sec": 1200,
    "playlist_max_items": 25,
    "separation_quality": "standard",
    "transcribe_lyrics": "auto",
    "video_max_height": 1080,
}


@pytest.fixture(autouse=True)
def _no_deployment_overrides(monkeypatch):
    for var in (
        "STEMDECK_STEM_FORMAT",
        "STEMDECK_STEM_MP3_KBPS",
        "STEMDECK_AUTO_LYRICS",
        "STEMDECK_SETTINGS_MIRROR",
    ):
        monkeypatch.delenv(var, raising=False)
    # One test registers a job; conftest does not clear the registry.
    registry._jobs.clear()
    yield
    registry._jobs.clear()


@pytest.fixture(params=["fresh install", "upgrade from 0.20.0"])
def install(request):
    if request.param == "upgrade from 0.20.0":
        _settings._SETTINGS_PATH.write_text(json.dumps(_SETTINGS_0_20_0), encoding="utf-8")
    return request.param


@pytest.fixture
def client(install):
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def test_new_songs_store_mp3_at_128_kbps(client):
    assert (_settings.get_stem_format(), _settings.get_stem_mp3_kbps()) == ("mp3", 128)
    body = client.get("/api/settings").json()
    assert (body["stem_format"], body["stem_mp3_kbps"]) == ("mp3", 128)


def test_detect_lyrics_is_off_and_a_new_import_looks_for_none(client):
    assert _settings.get_auto_lyrics() is False
    assert client.get("/api/settings").json()["auto_lyrics"] is False
    r = client.post("/api/jobs", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 200
    assert registry.get(r.json()["job_id"]).auto_lyrics is False


def test_an_upgrade_writes_neither_choice_into_settings_json(client):
    # Nothing is decided for the user at startup: the keys stay absent, so a
    # STEMDECK_* variable set later still applies, and Settings shows the
    # default rather than a choice the user never made.
    client.get("/api/settings")
    on_disk = (
        json.loads(_settings._SETTINGS_PATH.read_text(encoding="utf-8"))
        if _settings._SETTINGS_PATH.exists()
        else {}
    )
    assert "stem_format" not in on_disk
    assert "stem_mp3_kbps" not in on_disk
    assert "auto_lyrics" not in on_disk
