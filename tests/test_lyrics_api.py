"""GET /api/jobs/{id}/lyrics, the Align panel's POST .../lyrics/offset and
.../lyrics/align, and lyrics.json for older tracks through the tag backfill
(POST /api/jobs/{id}/audio-tags). No network: conftest answers LRCLIB as
offline, and tests that want an answer stand in for it."""

from __future__ import annotations

import json
import os
import random
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.pipeline.lyrics_lookup as ll
from app.core.models import Job
from app.core.registry import _jobs
from tests.test_lyrics_align import HOP, LINES, envelope, lrc

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


def test_lyrics_in_polish_are_served_as_utf8(client):
    job = _done_job(has_lyrics=True)
    polish = {
        **LYRICS,
        "track": "Małomiasteczkowy",
        "artist": "Dawid Podsiadło",
        "synced": "[00:11.56]Małomiasteczkowa twarz\n[00:31.72]Śpiewałem głośno pod prysznicem",
        "plain": "Małomiasteczkowa twarz\nŚpiewałem głośno pod prysznicem",
    }
    lyrics_file = _lyrics_file(job)
    lyrics_file.write_text(json.dumps(polish, ensure_ascii=False), encoding="utf-8")
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert json.loads(r.content.decode("utf-8")) == polish


# ── the Align panel: POST .../lyrics/offset and .../lyrics/align ──


def _with_vocals(job: Job, onsets) -> None:
    """A vocals stem whose kept envelope sings at ``onsets``, as
    test_lyrics_align.py makes them."""
    stems = jobs_mod.JOBS_DIR / job.id / "stems"
    (stems / "vocals.wav").write_bytes(b"RIFF")
    kept = stems / "vocal_envelope.json"
    kept.write_text(json.dumps({"hop": HOP, "db": envelope(onsets)}), encoding="utf-8")
    later = (stems / "vocals.wav").stat().st_mtime_ns + 10**9
    os.utime(kept, ns=(later, later))


def _synced_job(onsets=None) -> Job:
    job = _done_job(has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps({**LYRICS, "synced": lrc(LINES)}), encoding="utf-8")
    if onsets is not None:
        _with_vocals(job, onsets)
    return job


def _body(route: str) -> dict:
    return {"offset_sec": 1} if route == "offset" else {}


def test_an_offset_is_kept_and_served_beside_the_untouched_text(client):
    job = _synced_job()
    r = client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 15.94})
    assert r.status_code == 200
    assert r.json() == {"offset_sec": 15.94}
    served = client.get(f"/api/jobs/{job.id}/lyrics").json()
    assert served["offset_sec"] == 15.94
    assert served["synced"] == lrc(LINES), "their own timing stays recoverable"


def test_reset_is_an_offset_of_zero(client):
    job = _synced_job()
    client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": -3.5})
    r = client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 0})
    assert r.json() == {"offset_sec": 0.0}
    served = client.get(f"/api/jobs/{job.id}/lyrics").json()
    assert served["offset_sec"] == 0.0
    assert served["synced"] == lrc(LINES)


@pytest.mark.parametrize(
    "body",
    [{"offset_sec": 600.5}, {"offset_sec": -601}, {"offset_sec": "ten"}, {}, {"offset_sec": None}],
)
def test_an_offset_out_of_bounds_or_not_a_number_is_422(client, body):
    job = _synced_job()
    r = client.post(f"/api/jobs/{job.id}/lyrics/offset", json=body)
    assert r.status_code == 422
    assert "offset_sec" not in client.get(f"/api/jobs/{job.id}/lyrics").json()


def test_a_non_finite_offset_is_422(client):
    job = _synced_job()
    for literal in (b"NaN", b"Infinity"):
        r = client.post(
            f"/api/jobs/{job.id}/lyrics/offset",
            content=b'{"offset_sec": ' + literal + b"}",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422


def test_the_bounds_themselves_are_accepted(client):
    job = _synced_job()
    for value in (600, -600):
        r = client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": value})
        assert r.json() == {"offset_sec": float(value)}


@pytest.mark.parametrize("route", ["offset", "align"])
def test_an_unknown_or_malformed_job_is_404(client, route):
    for job_id in ("bbbbbbbbbbbb", "not-a-job"):
        r = client.post(f"/api/jobs/{job_id}/lyrics/{route}", json=_body(route))
        assert r.status_code == 404


@pytest.mark.parametrize("job_id", ["../../etc/passwd", "..%2F..%2Fetc", "aaaa/../aaaa"])
@pytest.mark.parametrize("route", ["offset", "align"])
def test_a_traversing_id_is_refused_by_the_edits(client, job_id, route):
    r = client.post(f"/api/jobs/{job_id}/lyrics/{route}", json=_body(route))
    assert r.status_code in (404, 405), r.status_code


@pytest.mark.parametrize("route", ["offset", "align"])
def test_a_track_without_lyrics_is_404_and_nothing_is_written(client, route):
    job = _done_job()
    r = client.post(f"/api/jobs/{job.id}/lyrics/{route}", json=_body(route))
    assert r.status_code == 404
    assert r.json() == {"detail": "no lyrics"}
    assert not _lyrics_file(job).exists()


@pytest.mark.parametrize("route", ["offset", "align"])
def test_text_only_lyrics_have_no_timing_to_move(client, route):
    job = _done_job(has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps({**LYRICS, "synced": ""}), encoding="utf-8")
    r = client.post(f"/api/jobs/{job.id}/lyrics/{route}", json=_body(route))
    assert r.status_code == 409


def test_old_lyrics_without_an_offset_are_served_as_they_were(client):
    """lyrics.json from before the Align panel has no offset_sec, and the
    answer does not grow one: the page reads a missing one as 0."""
    job = _done_job(has_lyrics=True)
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").json() == LYRICS


def test_a_damaged_offset_is_dropped_not_the_lyrics(client):
    job = _done_job(has_lyrics=True)
    for bad in (1e9, "12", True, None):
        _lyrics_file(job).write_text(json.dumps({**LYRICS, "offset_sec": bad}), encoding="utf-8")
        assert client.get(f"/api/jobs/{job.id}/lyrics").json() == LYRICS


def test_auto_detect_keeps_a_confident_estimate(client):
    job = _synced_job([t + 16 for t in LINES])
    r = client.post(f"/api/jobs/{job.id}/lyrics/align")
    assert r.status_code == 200
    body = r.json()
    assert body["confident"] is True
    assert body["offset_sec"] == pytest.approx(16, abs=0.1)
    served = client.get(f"/api/jobs/{job.id}/lyrics").json()
    assert served["offset_sec"] == body["offset_sec"]
    assert served["synced"] == lrc(LINES)


def test_auto_detect_ignores_the_offset_already_kept(client):
    """Estimated from the lyrics' own timing, whatever the user had set."""
    job = _synced_job([t - 4 for t in LINES])
    client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 30})
    body = client.post(f"/api/jobs/{job.id}/lyrics/align", json={}).json()
    assert body["offset_sec"] == pytest.approx(-4, abs=0.1)


def test_auto_detect_that_cannot_tell_leaves_the_lyrics_as_they_are(client):
    rng = random.Random(7)
    job = _synced_job(sorted(rng.uniform(0, 190) for _ in range(40)))
    client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 2.5})
    before = _lyrics_file(job).read_bytes()
    r = client.post(f"/api/jobs/{job.id}/lyrics/align")
    assert r.status_code == 200
    assert r.json() == {"confident": False, "offset_sec": None}
    assert _lyrics_file(job).read_bytes() == before


def test_auto_detect_without_a_vocals_stem_cannot_tell(client):
    job = _synced_job()
    r = client.post(f"/api/jobs/{job.id}/lyrics/align")
    assert r.json() == {"confident": False, "offset_sec": None}


def test_auto_detect_for_lyrics_the_browser_keeps_saves_nothing(client):
    """The tab's own LRCLIB find is sent along; lyrics.json is not needed,
    and none is written."""
    job = _done_job()
    _with_vocals(job, [t + 7 for t in LINES])
    r = client.post(f"/api/jobs/{job.id}/lyrics/align", json={"synced": lrc(LINES)})
    assert r.status_code == 200
    assert r.json()["confident"] is True
    assert r.json()["offset_sec"] == pytest.approx(7, abs=0.1)
    assert not _lyrics_file(job).exists()


def test_auto_detect_bounds_the_text_it_is_sent(client):
    job = _done_job()
    r = client.post(f"/api/jobs/{job.id}/lyrics/align", json={"synced": "x" * 100_001})
    assert r.status_code == 422
    r = client.post(f"/api/jobs/{job.id}/lyrics/align", json={"synced": "x" * 500_000})
    assert r.status_code == 413
    r = client.post(f"/api/jobs/{job.id}/lyrics/align", json=["not", "an", "object"])
    assert r.status_code == 422


def test_auto_detect_waits_for_the_track_to_be_separated(client):
    job = _synced_job([t + 16 for t in LINES])
    job.status = "running"
    assert client.post(f"/api/jobs/{job.id}/lyrics/align").status_code == 409


def test_the_pipeline_leaves_lyrics_aligned_by_hand_alone(client):
    """A hand-set offset is the user's timing: lyrics_align.py never moves
    the text beneath it, which would move the lines twice."""
    from app.pipeline.lyrics_align import align_lyrics

    job = _synced_job([t + 16 for t in LINES])
    client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 3})
    before = _lyrics_file(job).read_bytes()
    assert align_lyrics(job, jobs_mod.JOBS_DIR / job.id) == "exact"
    assert _lyrics_file(job).read_bytes() == before


def test_a_resplit_carries_the_users_alignment(client, monkeypatch):
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(ROWS))
    job = _synced_job()
    (jobs_mod.JOBS_DIR / job.id / "source.mp3").write_bytes(b"ID3")
    client.post(f"/api/jobs/{job.id}/lyrics/offset", json={"offset_sec": 12.3})
    r = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals"]})
    new_id = r.json()["job_id"]
    assert client.get(f"/api/jobs/{new_id}/lyrics").json()["offset_sec"] == 12.3


# ── lyrics kept before a lookup was held to the track ──


def test_kept_lyrics_of_another_song_are_dropped_when_asked_for(client):
    """NIHIL "Barro" kept Renato Vianna's "Joao de Barro" from a fuzzy search
    before a lookup had to match the track's song and artist."""
    job = _done_job(has_lyrics=True, audio_tags={"artist": "NIHIL", "title": "Barro"})
    wrong = {**LYRICS, "track": "Joao de Barro", "artist": "Renato Vianna"}
    _lyrics_file(job).write_text(json.dumps(wrong), encoding="utf-8")
    r = client.get(f"/api/jobs/{job.id}/lyrics")
    assert r.status_code == 404
    assert not _lyrics_file(job).exists(), "dropped for good"
    assert job.has_lyrics is False


def test_the_same_song_by_another_artist_in_the_same_script_is_dropped(client):
    job = _done_job(has_lyrics=True, audio_tags=TAGS)
    cover = {**LYRICS, "track": "Metropolis", "artist": "Motorhead"}
    _lyrics_file(job).write_text(json.dumps(cover), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 404


def test_an_artist_in_another_script_stands(client):
    """Matched through the artist's aliases when the lyrics were found."""
    job = _done_job(has_lyrics=True, audio_tags={"artist": "Jay Chou", "title": "Sunny Day"})
    native = {**LYRICS, "track": "Sunny Day", "artist": "周杰倫"}
    _lyrics_file(job).write_text(json.dumps(native), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 200


def test_lyrics_found_under_another_of_the_artists_names_stand(client):
    """The lookup held them to the artist's other names (Cat Stevens for
    Yusuf Islam), which this check cannot ask for again: it takes the mark
    the lookup left."""
    job = _done_job(has_lyrics=True, audio_tags={"artist": "Yusuf Islam", "title": "Wild World"})
    alias = {**LYRICS, "track": "Wild World", "artist": "Cat Stevens", "by_alias": True}
    _lyrics_file(job).write_text(json.dumps(alias), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 200
    assert _lyrics_file(job).exists()


@pytest.mark.parametrize(
    "work", [{"offset_sec": 2.5}, {"user_synced": LYRICS["synced"]}], ids=["offset", "user_synced"]
)
def test_lyrics_the_user_worked_on_are_never_dropped(client, work):
    job = _done_job(has_lyrics=True, audio_tags=TAGS)
    cover = {**LYRICS, "track": "Metropolis", "artist": "Motorhead", **work}
    _lyrics_file(job).write_text(json.dumps(cover), encoding="utf-8")
    client.get(f"/api/jobs/{job.id}/lyrics")
    assert _lyrics_file(job).exists()
    assert job.has_lyrics is True


def test_the_tracks_own_lyrics_and_a_transcription_are_kept(client):
    job = _done_job(has_lyrics=True, audio_tags=TAGS)
    _lyrics_file(job).write_text(json.dumps(LYRICS), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 200
    heard = {
        **LYRICS,
        "source": "whisper",
        "track": "Anything",
        "artist": "Anyone",
        "lrclib_id": None,
    }
    _lyrics_file(job).write_text(json.dumps(heard), encoding="utf-8")
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 200


def test_other_versions_of_another_song_are_not_offered(client):
    job = _done_job(has_lyrics=True, audio_tags=TAGS)
    others = [
        {**LYRICS, "lrclib_id": 7, "track": "Metropolis Part 2"},
        {**LYRICS, "lrclib_id": 8, "track": "Metropolis", "duration": 600.0},
    ]
    _lyrics_file(job).write_text(json.dumps({**LYRICS, "others": others}), encoding="utf-8")
    got = client.get(f"/api/jobs/{job.id}/lyrics").json()
    assert [o["lrclib_id"] for o in got["others"]] == [8]
