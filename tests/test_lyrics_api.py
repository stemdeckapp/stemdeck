"""GET /api/jobs/{id}/lyrics, and lyrics.json for older tracks through the tag
backfill (POST /api/jobs/{id}/audio-tags). No network: conftest answers LRCLIB
as offline, and tests that want an answer stand in for it."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.pipeline.lyrics_lookup as ll
from app.core.models import Job
from app.core.registry import _jobs

LYRICS = {
    "v": 1,
    "source": "lrclib",
    "track": "Metropolis",
    "artist": "Dream Theater",
    "album": "Images and Words",
    "duration": 572.0,
    "synced": "[00:01.00]The smile of dawn",
    "plain": "The smile of dawn",
    "instrumental": False,
    "timing": "exact",
    "others": [],
    "lrclib_id": 3,
}
DT = {"id": "Q162586", "name": "Dream Theater", "englishName": "Dream Theater"}
TAGS = {"artist": "Dream Theater", "title": "Metropolis"}


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


def _done_job(job_id="aaaaaaaaaaaa", **fields) -> Job:
    job = Job(
        id=job_id,
        status="done",
        title="Metropolis",
        duration_sec=572.0,
        source_url="local:Metropolis",
        **fields,
    )
    _jobs[job.id] = job
    (jobs_mod.JOBS_DIR / job.id / "stems").mkdir(parents=True)
    return job


def _lyrics_file(job: Job):
    return jobs_mod.JOBS_DIR / job.id / "lyrics.json"


class Lrclib:
    def __init__(self, rows):
        self.rows = rows
        self.asked: list[str] = []

    def __call__(self, endpoint, params):
        self.asked.append(endpoint)
        return None if endpoint == "get" else self.rows


ROWS = [
    {
        "id": 3,
        "trackName": "Metropolis",
        "artistName": "Dream Theater",
        "albumName": "Images and Words",
        "duration": 572,
        "instrumental": False,
        "syncedLyrics": "[00:01.00]The smile of dawn",
        "plainLyrics": "The smile of dawn",
    }
]


# ── GET .../lyrics ──


def test_a_tracks_lyrics_are_served(client):
    job = _done_job(has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 200
    assert r.json() == LYRICS
    assert r.headers["cache-control"] == "no-cache"


def test_the_text_is_not_in_the_jobs_state(client):
    job = _done_job(has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    state = client.get(f"/api/jobs/{job.id}").json()
    assert state["has_lyrics"] is True
    assert "smile of dawn" not in json.dumps(state)


def test_a_track_without_lyrics_is_404(client):
    job = _done_job()
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 404
    assert r.json() == {"detail": "no lyrics"}


def test_a_damaged_file_is_404_not_500(client):
    job = _done_job()
    _lyrics_file(job).write_text("{not json", encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 404
    _lyrics_file(job).write_text(json.dumps({**LYRICS, "source": "elsewhere"}), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 404


def test_an_unknown_job_is_404(client):
    assert client.get("/api/jobs/bbbbbbbbbbbb/lyrics").status_code == 404


def test_a_malformed_id_is_404(client):
    assert client.get("/api/jobs/not-a-job/lyrics").status_code == 404
    assert client.get("/api/jobs/aaaa..aaaaaa/lyrics").status_code == 404


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fetc", "aaaa/../aaaa"])
def test_a_traversing_id_is_refused(client, job_id):
    """Some of these are collapsed before routing and land on another route;
    the rest arrive as a job id and are refused. None may be a 500 or serve a
    file from outside the jobs directory."""
    r = client.get(f"/api/jobs/{job_id}/lyrics")
    assert r.status_code in (404, 405), r.status_code


def test_a_file_outside_the_jobs_directory_is_never_read(client, tmp_path):
    """lyrics.json resolving elsewhere (a link planted in the job's
    directory) is refused before anything is read."""
    job = _done_job()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "lyrics.json").write_text(json.dumps(LYRICS), encoding="utf-8")
    with (
        patch("app.api.jobs.read_lyrics") as read,
        patch.object(jobs_mod, "lyrics_path", return_value=elsewhere / "lyrics.json"),
    ):
        assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 404
    read.assert_not_called()


# ── the backfill ──


def test_an_older_track_gets_its_lyrics_through_the_backfill(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT)
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json().items() >= {"audio_tags": TAGS, "artist": DT, "has_lyrics": True}.items()
    assert json.loads(_lyrics_file(job).read_text(encoding="utf-8"))["lrclib_id"] == 3
    assert job.has_lyrics is True
    registry = json.loads((jobs_mod.JOBS_DIR / "registry.json").read_text(encoding="utf-8"))
    [record] = [j for j in registry["jobs"] if j["id"] == job.id]
    assert record["has_lyrics"] is True
    assert client.get(f"/api/jobs/{job.id}/lyrics").json()["lrclib_id"] == 3


def test_a_track_that_has_lyrics_asks_nothing(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT, has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json().items() >= {"audio_tags": TAGS, "artist": DT, "has_lyrics": True}.items()
    assert lrclib.asked == []


def test_no_connection_leaves_an_older_track_without_lyrics(client):
    job = _done_job(audio_tags=TAGS, artist=DT)
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json().items() >= {"audio_tags": TAGS, "artist": DT, "has_lyrics": False}.items()
    assert not _lyrics_file(job).exists()


def test_tags_found_now_are_what_the_lyrics_are_looked_up_by(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job()
    (jobs_mod.JOBS_DIR / job.id / "source.flac").write_bytes(b"fLaC")
    with patch("app.api.jobs.probe_tags", return_value=TAGS):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["has_lyrics"] is True
    assert lrclib.asked[:2] == ["get", "search"]


def test_embedded_lyrics_are_kept_without_asking_lrclib(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags={**TAGS, "lyrics": "Our own words"}, artist=DT)
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["has_lyrics"] is True
    kept = json.loads(_lyrics_file(job).read_text(encoding="utf-8"))
    assert (kept["source"], kept["plain"]) == ("file", "Our own words")
    assert lrclib.asked == []


def test_nothing_known_asks_nothing(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags={"album": "Images and Words"})
    job.title = "A video"
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["has_lyrics"] is False
    assert lrclib.asked == []


# ── when LRCLIB had nothing the length of the track ──


def _candidates(job: Job, others, searched_at=None):
    data = {"v": 1, "searched_at": searched_at or time.time(), "others": others}
    (jobs_mod.JOBS_DIR / job.id / "lyrics_candidates.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


LIVE = {**LYRICS, "lrclib_id": 9, "duration": 769.0, "album": "Live"}
LIVE.pop("others")


def test_versions_not_kept_come_with_the_404(client):
    job = _done_job()
    _candidates(job, [LIVE])
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 404
    assert r.json()["detail"] == "no lyrics"
    assert [o["lrclib_id"] for o in r.json()["others"]] == [9]


def test_a_marker_with_nothing_to_offer_is_a_plain_404(client):
    job = _done_job()
    _candidates(job, [])
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 404
    assert r.json() == {"detail": "no lyrics"}


def test_a_transcription_is_served_with_the_versions_not_kept(client):
    job = _done_job(has_lyrics=True)
    whisper = {**LYRICS, "source": "whisper", "lrclib_id": None}
    _lyrics_file(job).write_text(json.dumps(whisper), encoding="utf-8")
    _candidates(job, [LIVE])
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 200
    assert r.json()["source"] == "whisper"
    assert [o["lrclib_id"] for o in r.json()["others"]] == [9]


def test_the_backfill_does_not_ask_again_while_lrclibs_nothing_is_recent(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT)
    _candidates(job, [], searched_at=time.time() - 29 * 86400)
    assert client.post(f"/api/jobs/{job.id}/audio-tags").json()["has_lyrics"] is False
    assert lrclib.asked == []


def test_the_backfill_asks_again_once_lrclibs_nothing_is_old(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT)
    _candidates(job, [], searched_at=time.time() - 31 * 86400)
    assert client.post(f"/api/jobs/{job.id}/audio-tags").json()["has_lyrics"] is True
    assert not (jobs_mod.JOBS_DIR / job.id / "lyrics_candidates.json").exists(), "superseded"


def test_the_backfill_records_lrclibs_nothing(client, monkeypatch):
    lrclib = Lrclib([])
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT)
    assert client.post(f"/api/jobs/{job.id}/audio-tags").json()["has_lyrics"] is False
    asked = len(lrclib.asked)
    assert asked > 0
    marker = json.loads(
        (jobs_mod.JOBS_DIR / job.id / "lyrics_candidates.json").read_text(encoding="utf-8")
    )
    assert marker["others"] == [] and marker["searched_at"] > 0
    client.post(f"/api/jobs/{job.id}/audio-tags")
    assert len(lrclib.asked) == asked, "not asked a second time"


def test_no_connection_records_nothing(client):
    job = _done_job(audio_tags=TAGS, artist=DT)
    client.post(f"/api/jobs/{job.id}/audio-tags")
    assert not (jobs_mod.JOBS_DIR / job.id / "lyrics_candidates.json").exists()


# ── a re-split is the same recording ──


def test_a_resplit_takes_its_sources_lyrics_and_marker(client, monkeypatch):
    lrclib = Lrclib(ROWS)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _done_job(audio_tags=TAGS, artist=DT, has_lyrics=True)
    (jobs_mod.JOBS_DIR / job.id / "source.mp3").write_bytes(b"ID3")
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    _candidates(job, [LIVE])
    r = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals"]})
    assert r.status_code == 200
    new_id = r.json()["job_id"]
    assert _jobs[new_id].has_lyrics is True
    new_dir = jobs_mod.JOBS_DIR / new_id
    assert json.loads((new_dir / "lyrics.json").read_text(encoding="utf-8")) == LYRICS
    assert (new_dir / "lyrics_candidates.json").is_file()
    assert lrclib.asked == []


def test_the_backfill_moves_a_version_of_another_length_onto_the_track(client, monkeypatch):
    """An older track has its vocals stem, so the timing is moved at once."""
    rows = [{**ROWS[0], "duration": 600}]
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(rows))
    job = _done_job(audio_tags=TAGS, artist=DT)
    with patch("app.pipeline.lyrics_lookup.align_lyrics", return_value="shifted") as align:
        assert client.post(f"/api/jobs/{job.id}/audio-tags").json()["has_lyrics"] is True
    assert align.call_count == 1
    assert json.loads(_lyrics_file(job).read_text(encoding="utf-8"))["timing"] == "unverified"
