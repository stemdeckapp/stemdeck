"""Regression tests: data written before song identification, and the API
shapes clients built before it, keep working.

registry.json, metadata.json and settings.json from the release before
identification (bc88527) carry none of identity, work, has_lyrics,
acoustid_api_key or transcribe_lyrics, and the oldest records carry no
audio_tags, artist or source_format either. They must load, list, play,
re-split and delete exactly as they did. The job state, the library listing,
the queue state and the settings payload must keep every field they had,
with the same meaning, because the mobile UI (static/mobile/) and older
desktop builds read them. Re-split and delete must treat lyrics.json, the
candidates file and LRCLIB's "not found" marker like any other job file.

No network: conftest answers every service as offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
from app.core import registry as _registry
from app.core import settings as _settings
from app.core.models import Job
from app.core.registry import _jobs
from app.core.registry import restore as restore_registry

# Every key GET /api/jobs/{id} and GET /api/jobs answered with before
# identification (bc88527 Job.to_state).
OLD_STATE_KEYS = {
    "job_id",
    "status",
    "progress",
    "stage",
    "title",
    "duration",
    "thumbnail",
    "bpm",
    "key",
    "scale",
    "key_confidence",
    "lufs",
    "peak_db",
    "dynamic_range",
    "tempo_stability",
    "stem_presence",
    "sections",
    "sections_source",
    "tags",
    "stems",
    "selected_stems",
    "mix_url",
    "source_url",
    "source_format",
    "audio_tags",
    "artist",
    "has_video",
    "video_status",
    "error",
    "error_detail",
    "compute_device",
    "gpu_fallback",
    "stage_timings",
    "vocal_split",
    "trashed_at",
    "created_at",
}
# The queue view's record, unchanged since before identification.
OLD_QUEUE_KEYS = {
    "job_id",
    "status",
    "progress",
    "stage",
    "title",
    "thumbnail",
    "source_url",
    "error",
}
# Every key GET /api/settings answered with before identification.
OLD_SETTINGS_KEYS = {
    "allow_network",
    "auto_delete_jobs",
    "auto_delete_days",
    "auto_sections",
    "auto_delete_days_min",
    "auto_delete_days_max",
    "max_duration_sec",
    "max_duration_min_sec",
    "max_duration_max_sec",
    "playlist_max_items",
    "video_max_height",
    "export_sample_rate",
    "separation_quality",
    "cookies_file",
    "port",
    "demucs_device",
    "demucs_device_resolved",
    "demucs_devices_available",
    "lan_addresses",
}
# A registry record exactly as bc88527 wrote it: the fields of that Job.
OLD_ID = "abcdef00c001"
OLDEST_ID = "abcdef00c002"
STEMS = ["vocals", "drums", "bass", "other"]


def _old_record(job_id: str, **overrides) -> dict:
    record = {
        "id": job_id,
        "status": "done",
        "progress": 1.0,
        "stage_message": "Done",
        "title": "Old Song",
        "duration_sec": 200.0,
        "thumbnail": None,
        "bpm": 120,
        "key": "C",
        "scale": "Major",
        "key_confidence": 80,
        "lufs": -9.0,
        "peak_db": -0.5,
        "dynamic_range": 8.5,
        "tempo_stability": 90,
        "stem_presence": {"vocals": 100, "drums": 80, "bass": 60, "other": 40},
        "sections": None,
        "auto_sections": False,
        "sections_source": None,
        "tags": ["music"],
        "stems": [{"name": n, "url": f"/api/jobs/{job_id}/stems/{n}.wav"} for n in STEMS],
        "selected_stems": list(STEMS),
        "mix_url": None,
        "source_url": "local:Old Song",
        "source_format": "mp3",
        "audio_tags": {"artist": "Queen", "title": "Old Song"},
        "artist": {"id": "Q15862", "name": "Queen", "englishName": "Queen"},
        "has_video": False,
        "video_status": None,
        "error": None,
        "error_detail": None,
        "compute_device": "cpu",
        "gpu_fallback": False,
        "stage_timings": {"prepare": 0.5, "analyze": 1.0, "artist_wait": 0.0},
        "vocal_split": "none",
        "trashed_at": None,
        "queue_position": 0,
        "resume_attempts": 0,
        "created_at": 1_700_000_000.0,
    }
    record.update(overrides)
    return record


def _oldest_record(job_id: str) -> dict:
    """Before audio tags, the band and the source format were recorded."""
    record = _old_record(job_id, created_at=1_600_000_000.0, title="Oldest Song")
    for key in ("audio_tags", "artist", "source_format", "resume_attempts", "trashed_at"):
        record.pop(key)
    return record


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    yield
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()


@pytest.fixture
def jobs_dir() -> Path:
    return jobs_mod.JOBS_DIR


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _stems_on_disk(jobs_dir: Path, job_id: str) -> Path:
    job_dir = jobs_dir / job_id
    (job_dir / "stems").mkdir(parents=True, exist_ok=True)
    for name in STEMS:
        (job_dir / "stems" / f"{name}.wav").write_bytes(b"RIFF" + name.encode())
    return job_dir


def _write_old_registry(jobs_dir: Path, *records: dict) -> None:
    (jobs_dir / "registry.json").write_text(
        json.dumps({"version": 1, "jobs": list(records)}, indent=2), encoding="utf-8"
    )


@pytest.fixture
def old_library(jobs_dir):
    """Two tracks from before identification, restored the way a start does."""
    old_dir = _stems_on_disk(jobs_dir, OLD_ID)
    (old_dir / "source.mp3").write_bytes(b"ID3old")
    _stems_on_disk(jobs_dir, OLDEST_ID)
    _write_old_registry(jobs_dir, _old_record(OLD_ID), _oldest_record(OLDEST_ID))
    restore_registry(jobs_dir)
    _registry._pending_resume.clear()
    return jobs_dir


# ── old registry.json ──


def test_an_old_registry_restores_with_the_new_fields_at_their_defaults(old_library):
    for job_id in (OLD_ID, OLDEST_ID):
        job = _jobs[job_id]
        assert job.status == "done"
        assert (job.identity, job.work, job.has_lyrics) == (None, None, False)
    old = _jobs[OLD_ID]
    assert old.audio_tags == {"artist": "Queen", "title": "Old Song"}
    assert old.artist == {"id": "Q15862", "name": "Queen", "englishName": "Queen"}
    assert old.stage_timings == {"prepare": 0.5, "analyze": 1.0, "artist_wait": 0.0}
    oldest = _jobs[OLDEST_ID]
    assert (oldest.audio_tags, oldest.artist, oldest.source_format) == (None, None, None)


def test_an_old_library_lists_with_every_field_it_had(client, old_library):
    listed = client.get("/api/jobs").json()
    assert [j["job_id"] for j in listed] == [OLDEST_ID, OLD_ID], "oldest first, as before"
    for state in listed:
        assert set(state) >= OLD_STATE_KEYS
        assert state["status"] == "done"
        assert [s["name"] for s in state["stems"]] == STEMS
        assert state["has_lyrics"] is False
        assert state["identity"] is None and state["work"] is None
    old = next(s for s in listed if s["job_id"] == OLD_ID)
    record = _old_record(OLD_ID)
    for state_key, record_key in (
        ("title", "title"),
        ("duration", "duration_sec"),
        ("bpm", "bpm"),
        ("key", "key"),
        ("stem_presence", "stem_presence"),
        ("selected_stems", "selected_stems"),
        ("source_url", "source_url"),
        ("source_format", "source_format"),
        ("audio_tags", "audio_tags"),
        ("artist", "artist"),
        ("stage_timings", "stage_timings"),
        ("created_at", "created_at"),
    ):
        assert old[state_key] == record[record_key], state_key


@pytest.mark.parametrize("job_id", [OLD_ID, OLDEST_ID])
def test_an_old_track_plays(client, old_library, job_id):
    state = client.get(f"/api/jobs/{job_id}")
    assert state.status_code == 200
    assert state.json()["status"] == "done"
    for name in STEMS:
        stem = client.get(f"/api/jobs/{job_id}/stems/{name}.wav")
        assert stem.status_code == 200
        assert stem.content == b"RIFF" + name.encode()


@pytest.mark.parametrize("job_id", [OLD_ID, OLDEST_ID])
def test_an_old_track_has_no_lyrics_rather_than_an_error(client, old_library, job_id):
    r = client.get(f"/api/jobs/{job_id}/lyrics")
    assert r.status_code == 404
    assert r.json() == {"detail": "no lyrics"}


def test_an_old_upload_resplits(client, old_library, jobs_dir):
    r = client.post(f"/api/jobs/{OLD_ID}/resplit", json={"stems": ["vocals"]})
    assert r.status_code == 200
    new = _jobs[r.json()["job_id"]]
    assert new.status == "queued"
    assert new.source_url.startswith("local:Old Song (")
    assert new.audio_tags == {"artist": "Queen", "title": "Old Song"}
    assert new.artist == _jobs[OLD_ID].artist
    assert (new.identity, new.work, new.has_lyrics) == (None, None, False)
    new_dir = jobs_dir / new.id
    assert sorted(p.name for p in new_dir.iterdir()) == ["source.mp3"]
    # The track it came from is untouched.
    assert _jobs[OLD_ID].status == "done"
    assert (jobs_dir / OLD_ID / "source.mp3").read_bytes() == b"ID3old"


@pytest.mark.parametrize("job_id", [OLD_ID, OLDEST_ID])
def test_an_old_track_deletes(client, old_library, jobs_dir, job_id):
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json() == {"job_id": job_id, "status": "deleted"}
    assert not (jobs_dir / job_id).exists()
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert job_id not in {rec["id"] for rec in registry["jobs"]}
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


def test_an_old_registry_survives_a_save_and_a_restart(old_library, jobs_dir):
    """Saved by this build and read back, nothing the old record had is lost."""
    before = {job_id: _jobs[job_id].to_state() for job_id in (OLD_ID, OLDEST_ID)}
    _registry.persist(jobs_dir)
    _jobs.clear()
    restore_registry(jobs_dir)
    _registry._pending_resume.clear()
    for job_id, state in before.items():
        assert _jobs[job_id].to_state() == state
    saved = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    [record] = [r for r in saved["jobs"] if r["id"] == OLD_ID]
    assert set(_old_record(OLD_ID)) <= set(record), "a field an older build reads was dropped"


# ── old metadata.json (a job the registry lost) ──


def test_an_orphan_with_old_metadata_is_recovered(client, jobs_dir):
    """metadata.json as bc88527 wrote it, and no registry entry: the job is
    recovered from disk as before, with the new fields at their defaults."""
    job_dir = _stems_on_disk(jobs_dir, "abcdef00c003")
    meta = {
        "title": "Orphan",
        "thumbnail": None,
        "audio_tags": {"artist": "Queen"},
        "artist": {"id": "Q15862", "name": "Queen", "englishName": "Queen"},
        "duration_sec": 180.0,
        "bpm": 100,
        "key": "D",
        "scale": "Natural Minor",
        "key_confidence": 70,
        "lufs": -10.0,
        "peak_db": -1.0,
        "dynamic_range": 9.0,
        "tempo_stability": 88,
        "stem_presence": {"vocals": 100},
        "sections": None,
        "sections_source": None,
        "tags": None,
        "has_video": False,
        "video_status": None,
        "compute_device": "cpu",
        "gpu_fallback": False,
        "stage_timings": {"analyze": 1.0},
    }
    (job_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    restore_registry(jobs_dir)
    job = _jobs["abcdef00c003"]
    assert job.status == "done"
    assert (job.title, job.bpm, job.audio_tags) == ("Orphan", 100, {"artist": "Queen"})
    assert job.artist == meta["artist"]
    assert (job.identity, job.work, job.has_lyrics) == (None, None, False)
    state = client.get("/api/jobs/abcdef00c003").json()
    assert set(state) >= OLD_STATE_KEYS
    assert client.get("/api/jobs/abcdef00c003/stems/vocals.wav").status_code == 200


# ── old settings.json ──


OLD_SETTINGS = {
    "auto_sections": True,
    "auto_delete_jobs": True,
    "auto_delete_days": 14,
    "separation_quality": "best",
    "max_duration_sec": 1200,
}


def _old_settings_file(extra: dict | None = None) -> Path:
    path = _settings._SETTINGS_PATH
    path.write_text(json.dumps({**OLD_SETTINGS, **(extra or {})}), encoding="utf-8")
    _settings._state = None
    return path


def test_old_settings_load_with_the_new_ones_at_their_defaults(client):
    _old_settings_file()
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= OLD_SETTINGS_KEYS
    for key, value in OLD_SETTINGS.items():
        assert body[key] == value, key
    assert body["transcribe_lyrics"] == "auto"
    assert body["acoustid_api_key_set"] is False


def test_changing_an_old_setting_keeps_the_rest_and_adds_no_new_ones(client):
    path = _old_settings_file()
    r = client.post("/api/settings", json={"auto_sections": False})
    assert r.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {**OLD_SETTINGS, "auto_sections": False}


def test_a_key_already_on_disk_is_never_handed_back(client):
    """Whatever the request, no response carries the stored key itself."""
    key = "Zx9Yw8Vu7TqRsP"
    path = _old_settings_file({"acoustid_api_key": key})
    responses = [
        client.get("/api/settings"),
        client.post("/api/settings", json={"auto_sections": False}),
        client.post("/api/settings", json={"transcribe_lyrics": "off"}),
        client.post("/api/settings", json={"acoustid_api_key": 5}),
    ]
    for r in responses:
        assert key not in r.text
        assert key[:-4] not in r.text
    assert responses[0].json()["acoustid_api_key_set"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["acoustid_api_key"] == key


@pytest.mark.parametrize(
    "junk",
    [
        {"acoustid_api_key": 12345, "transcribe_lyrics": "maybe"},
        {"acoustid_api_key": None, "transcribe_lyrics": None},
        {"acoustid_api_key": ["a"], "transcribe_lyrics": 1},
        {"acoustid_api_key": "bad key with spaces", "transcribe_lyrics": "ON "},
    ],
)
def test_hand_edited_new_settings_fall_back_instead_of_failing(client, junk):
    _old_settings_file(junk)
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["acoustid_api_key_set"] is False
    assert body["transcribe_lyrics"] == "auto"
    assert body["separation_quality"] == "best"


# ── API shapes older clients read ──


def test_a_new_jobs_state_keeps_every_old_field_with_its_old_default():
    state = Job(id="abcdef00c004").to_state()
    assert set(state) >= OLD_STATE_KEYS
    assert state["audio_tags"] is None and state["artist"] is None
    assert state["has_video"] is False and state["vocal_split"] == "none"
    assert state["stems"] == [] and state["selected_stems"] == []


def test_the_queue_record_is_unchanged():
    assert set(Job(id="abcdef00c005").to_queue_state()) == OLD_QUEUE_KEYS


def test_the_tag_backfill_still_answers_its_old_keys(client, jobs_dir):
    """Older desktop builds read only audio_tags and artist from it."""
    job = Job(
        id="abcdef00c006",
        status="done",
        title="Queen - Old Song",
        source_url="local:Old Song",
        audio_tags={"artist": "Queen", "title": "Old Song"},
    )
    _jobs[job.id] = job
    _stems_on_disk(jobs_dir, job.id)
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    body = r.json()
    assert {"audio_tags", "artist"} <= set(body)
    assert body["audio_tags"] == {"artist": "Queen", "title": "Old Song"}


# ── re-split and delete of a job with lyrics files ──


LYRICS = {
    "v": 1,
    "source": "lrclib",
    "track": "Old Song",
    "artist": "Queen",
    "album": "",
    "duration": 200.0,
    "synced": "[00:01.00]Line one",
    "plain": "Line one",
    "instrumental": False,
    "timing": "exact",
    "others": [],
    "lrclib_id": 3,
}


def _done_upload(jobs_dir: Path, job_id: str, **fields) -> Job:
    job = Job(
        id=job_id,
        status="done",
        title="Old Song",
        duration_sec=200.0,
        source_url="local:Old Song",
        source_format="mp3",
        audio_tags={"artist": "Queen", "title": "Old Song"},
        stems=[{"name": n, "url": f"/api/jobs/{job_id}/stems/{n}.wav"} for n in STEMS],
        **fields,
    )
    _jobs[job.id] = job
    job_dir = _stems_on_disk(jobs_dir, job_id)
    (job_dir / "source.mp3").write_bytes(b"ID3")
    return job


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _marker(others: list | None = None) -> dict:
    import time

    return {"v": 1, "searched_at": time.time(), "others": others or []}


@pytest.mark.parametrize(
    "files",
    [
        ("lyrics.json",),
        ("lyrics_candidates.json",),
        ("lyrics.json", "lyrics_candidates.json"),
    ],
)
def test_a_job_with_lyrics_files_deletes_whole(client, jobs_dir, files):
    job = _done_upload(jobs_dir, "abcdef00c010", has_lyrics="lyrics.json" in files)
    job_dir = jobs_dir / job.id
    for name in files:
        _write(job_dir / name, LYRICS if name == "lyrics.json" else _marker())
    # A temp file a crashed atomic write left behind goes too.
    (job_dir / ".lyrics.json.0123.tmp").write_text("{", encoding="utf-8")
    r = client.delete(f"/api/jobs/{job.id}")
    assert r.status_code == 200
    assert not job_dir.exists()
    assert client.get(f"/api/jobs/{job.id}/lyrics").status_code == 404
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert job.id not in {rec["id"] for rec in registry["jobs"]}


def test_a_resplit_of_a_job_lrclib_had_nothing_for(client, jobs_dir):
    """The marker goes with it, no lyrics are claimed, and the source keeps
    its own marker."""
    job = _done_upload(jobs_dir, "abcdef00c011")
    marker = _marker()
    _write(jobs_dir / job.id / "lyrics_candidates.json", marker)
    r = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals", "drums"]})
    assert r.status_code == 200
    new = _jobs[r.json()["job_id"]]
    assert new.has_lyrics is False
    new_dir = jobs_dir / new.id
    assert sorted(p.name for p in new_dir.iterdir()) == ["lyrics_candidates.json", "source.mp3"]
    assert json.loads((new_dir / "lyrics_candidates.json").read_text(encoding="utf-8")) == marker
    assert (jobs_dir / job.id / "lyrics_candidates.json").is_file()
    assert new.selected_stems == ["vocals", "drums"]


def test_a_resplit_outlives_the_track_it_came_from(client, jobs_dir):
    """Deleting the original after a re-split leaves the re-split's own
    lyrics, stems source and state intact."""
    job = _done_upload(jobs_dir, "abcdef00c012", has_lyrics=True)
    _write(jobs_dir / job.id / "lyrics.json", LYRICS)
    _write(jobs_dir / job.id / "lyrics_candidates.json", _marker())
    new_id = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": []}).json()["job_id"]
    assert _jobs[new_id].has_lyrics is True
    assert _jobs[new_id].selected_stems == ["vocals", "drums", "bass", "guitar", "piano", "other"]

    assert client.delete(f"/api/jobs/{job.id}").status_code == 200
    assert not (jobs_dir / job.id).exists()
    new_dir = jobs_dir / new_id
    assert json.loads((new_dir / "lyrics.json").read_text(encoding="utf-8")) == LYRICS
    assert (new_dir / "source.mp3").read_bytes() == b"ID3"
    assert client.get(f"/api/jobs/{new_id}").json()["has_lyrics"] is True


def test_a_resplit_whose_lyrics_cannot_be_copied_still_starts(client, jobs_dir, monkeypatch):
    """Copying the lyrics is a courtesy: a failure there must not cost the
    re-split the user asked for."""
    job = _done_upload(jobs_dir, "abcdef00c013", has_lyrics=True)
    _write(jobs_dir / job.id / "lyrics.json", LYRICS)
    real_copy2 = jobs_mod.shutil.copy2

    def copy2(src, dst, *args, **kwargs):
        if Path(src).name.startswith("lyrics"):
            raise PermissionError("locked")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr("app.pipeline.lyrics_lookup.shutil.copy2", copy2)
    r = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": ["vocals"]})
    assert r.status_code == 200
    new = _jobs[r.json()["job_id"]]
    assert new.has_lyrics is False
    assert (jobs_dir / new.id / "source.mp3").is_file()
    assert not (jobs_dir / new.id / "lyrics.json").exists()
