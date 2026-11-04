"""A song rebuilt from its folder keeps what the user set on it.

When registry.json is missing, empty, corrupt or cut off, restore() rebuilds
each song from its folder. Its favourite, Trash, versions, import choices and
source lived only in registry.json, so the rebuild reset them all: a binned
song came back into the library, versions (and the mixes keyed by their ids)
were gone, and a link song could no longer fill its missing stems. Each song's
metadata.json now keeps a copy, and the rebuild reads it back.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import registry as _registry
from app.core.models import Job
from app.core.registry import registry_path
from app.main import app
from app.pipeline.runner import _write_metadata

SONG = "b1b2c3d4e5f6"
OLD = "0a20c0ffee01"
FIXTURE = Path(__file__).parent / "fixtures" / "upgrade-0.20.0" / "library" / OLD
SAVED = {
    "favorite": True,
    "trashed_at": 1790900000.5,
    "versions": [
        {"id": "main", "name": None, "stems": ["vocals", "drums"], "created_at": 1790800000.0},
        {"id": "ab12cd34", "name": "Karaoke", "stems": ["drums"], "created_at": 1790850000.0},
    ],
    "active_version": "ab12cd34",
    "vocal_split_wanted": True,
    "selected_stems": ["vocals", "drums"],
    "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "source_format": "mp3",
    "separation_model": "htdemucs_6s",
    "separation_quality": "best",
    "auto_lyrics": False,
    "created_at": 1790800000.0,
    "has_video": True,
    "video_status": "ok",
}


@pytest.fixture
def jobs_dir() -> Path:
    """The jobs folder the API writes to (the conftest points it at a temp dir)."""
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture(autouse=True)
def _empty_registry():
    _registry._jobs.clear()
    _registry._deleted.clear()
    yield
    _registry._jobs.clear()
    _registry._deleted.clear()


def _finished(jobs_dir: Path, **fields) -> Job:
    """A finished song as the pipeline leaves it: stems, metadata.json, and a
    registry entry."""
    stems = jobs_dir / SONG / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums", "bass", "guitar", "piano", "other"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    job = Job(id=SONG, status="done", progress=1.0, title="Song", **fields)
    _write_metadata(job, jobs_dir / SONG)
    _registry.register(job)
    _registry.persist(jobs_dir)
    return job


def _restart(jobs_dir: Path) -> Job:
    _registry._jobs.clear()
    _registry.restore(jobs_dir)
    job = _registry.get(SONG)
    assert job is not None and job.status == "done"
    return job


def _assert_kept(job: Job, names=tuple(SAVED)) -> None:
    for name in names:
        assert getattr(job, name) == SAVED[name], name


@pytest.mark.parametrize("damage", ["missing", "empty", "corrupt", "cut off"])
def test_a_rebuilt_song_keeps_what_the_user_set(jobs_dir, damage):
    _finished(jobs_dir, **SAVED)
    path = registry_path(jobs_dir)
    whole = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_bytes(b"")
    elif damage == "corrupt":
        path.write_bytes(b"\x00\xff not json")
    else:
        path.write_bytes(whole[: len(whole) // 2])
    damaged = path.read_bytes() if path.exists() else None

    _assert_kept(_restart(jobs_dir))

    if damaged is not None:
        [kept] = sorted(jobs_dir.glob("registry.json.corrupt-*"))
        assert kept.read_bytes() == damaged, "the damaged file is kept as it was"
    # And the rebuilt registry holds them again, written atomically.
    _assert_kept(Job.from_record(json.loads(path.read_text(encoding="utf-8"))["jobs"][0]))


def test_changes_made_after_the_song_finished_survive_a_rebuild(jobs_dir):
    _finished(jobs_dir, selected_stems=["vocals", "drums"], source_url="local:Song")
    client = TestClient(app)
    assert client.put(f"/api/jobs/{SONG}/favorite", json={"favorite": True}).status_code == 200
    made = client.post(
        f"/api/jobs/{SONG}/versions", json={"name": "Drums", "stems": ["drums"], "select": True}
    )
    assert made.status_code == 201
    vid = made.json()["version"]["id"]
    assert (
        client.patch(f"/api/jobs/{SONG}/versions/{vid}", json={"name": "Beat"}).status_code == 200
    )
    gone = client.post(f"/api/jobs/{SONG}/versions", json={"stems": ["bass"]}).json()["version"]
    assert client.delete(f"/api/jobs/{SONG}/versions/{gone['id']}").status_code == 200
    assert client.post(f"/api/jobs/{SONG}/versions/main/select").status_code == 200
    assert client.post(f"/api/jobs/{SONG}/trash").status_code == 200
    before = _registry.get(SONG)
    registry_path(jobs_dir).unlink()

    job = _restart(jobs_dir)

    assert job.favorite is True
    assert job.trashed_at == before.trashed_at
    assert job.versions == before.versions
    assert [v["name"] for v in job.versions] == [None, "Beat"]
    assert job.active_version == "main"
    assert job.source_url == "local:Song"


def test_taking_it_out_of_the_trash_survives_a_rebuild(jobs_dir):
    _finished(jobs_dir, trashed_at=1790900000.0)
    assert TestClient(app).post(f"/api/jobs/{SONG}/restore").status_code == 200
    registry_path(jobs_dir).unlink()

    assert _restart(jobs_dir).trashed_at is None


def test_saved_values_from_disk_are_checked_like_a_registry_record(jobs_dir):
    """metadata.json is read from disk, so a damaged or hand-edited one is
    untrusted: a value of the wrong type is dropped, never loaded."""
    _finished(jobs_dir)
    meta_path = jobs_dir / SONG / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        favorite="yes",
        versions=[{"id": "../x", "stems": ["drums"]}],
        active_version="../../etc",
        vocal_split_wanted="true",
        selected_stems=["../../escape", "drums", 7],
        source_url=5,
        separation_model=["htdemucs_6s"],
        # Python's json writes and reads these; no JSON response can carry them.
        trashed_at=float("inf"),
        created_at=float("nan"),
    )
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    registry_path(jobs_dir).unlink()

    job = _restart(jobs_dir)

    assert job.favorite is None
    assert job.trashed_at is None
    assert job.versions is None
    assert job.active_version is None
    assert job.vocal_split_wanted is False
    assert job.selected_stems == ["drums"]
    assert job.source_url is None
    assert job.separation_model is None
    assert job.created_at == (jobs_dir / SONG).stat().st_mtime


def test_a_metadata_file_that_is_not_an_object_does_not_stop_the_other_folders(jobs_dir):
    _finished(jobs_dir, **SAVED)
    (jobs_dir / OLD / "stems").mkdir(parents=True)
    (jobs_dir / OLD / "stems" / "vocals.wav").write_bytes(b"RIFF")
    (jobs_dir / OLD / "metadata.json").write_text("[]", encoding="utf-8")
    registry_path(jobs_dir).unlink()

    _assert_kept(_restart(jobs_dir))
    assert _registry.get(OLD) is not None


def test_an_upgrade_copies_the_registry_fields_into_every_song_folder_once(jobs_dir):
    """Songs finished before metadata.json kept a copy: the first start with a
    version 1 registry copies them in, so a registry lost later keeps them."""
    (jobs_dir / OLD / "stems").mkdir(parents=True)
    (jobs_dir / OLD / "stems" / "vocals.wav").write_bytes(b"RIFF")
    shutil.copy(FIXTURE / "metadata.json", jobs_dir / OLD / "metadata.json")
    old = Job(id=OLD, status="done", title="Old Song From 0.20.0", **SAVED)
    registry_path(jobs_dir).write_text(
        json.dumps({"version": 1, "jobs": [old.to_record()]}), encoding="utf-8"
    )

    _registry._jobs.clear()
    _registry.restore(jobs_dir)

    meta = json.loads((jobs_dir / OLD / "metadata.json").read_text(encoding="utf-8"))
    assert meta["title"] == "Old Song From 0.20.0", "nothing else in it changes"
    assert meta["sections_source"] == "manual"
    assert json.loads(registry_path(jobs_dir).read_text(encoding="utf-8"))["version"] == 2

    registry_path(jobs_dir).unlink()
    _registry._jobs.clear()
    _registry.restore(jobs_dir)
    _assert_kept(_registry.get(OLD), _registry.SAVED_FIELDS)


def test_back_from_a_downgrade_the_copy_keeps_what_0_20_0_dropped(jobs_dir):
    """0.20.0 rewrote the registry without versions, and as version 1: the
    copy takes the fields its record holds and keeps the rest of metadata.json."""
    _finished(jobs_dir, **SAVED)
    record = Job(id=SONG, status="done", title="Song", favorite=False).to_record()
    for name in ("versions", "active_version", "vocal_split_wanted", "separation_quality"):
        del record[name]
    registry_path(jobs_dir).write_text(
        json.dumps({"version": 1, "jobs": [record]}), encoding="utf-8"
    )

    _restart(jobs_dir)

    meta = json.loads((jobs_dir / SONG / "metadata.json").read_text(encoding="utf-8"))
    assert meta["favorite"] is False, "the registry held it, so it is the newer value"
    assert meta["versions"] == SAVED["versions"]
    assert meta["active_version"] == SAVED["active_version"]
    assert meta["separation_quality"] == "best"


# The fields 0.20.0's registry.persist writes: the fixture is its own output.
FIELDS_0_20_0 = set(
    json.loads((FIXTURE.parent / "registry.json").read_text(encoding="utf-8"))["jobs"][0]
)


def _saved_by_0_20_0(jobs_dir: Path) -> None:
    """registry.json as 0.20.0 rewrites it on its first save: version 1, and
    every record cut down to the fields 0.20.0 knows."""
    path = registry_path(jobs_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    data["jobs"] = [{k: v for k, v in r.items() if k in FIELDS_0_20_0} for r in data["jobs"]]
    path.write_text(json.dumps(data), encoding="utf-8")


def test_a_downgrade_to_0_20_0_and_back_loses_nothing(jobs_dir):
    """0.20.0 loads a 0.21 library but its first save drops every newer field.
    Back on 0.21 the song has its versions, stem format and takes again, and
    the track still plays the take it played."""
    from app.core.takes import make_track, recover_takes, write_tracks

    take = jobs_dir / SONG / "takes" / "t0000aaaa"
    take.mkdir(parents=True)
    (take / "audio.wav").write_bytes(b"RIFF")
    (take / "take.json").write_text(
        json.dumps({"track_id": "k0000aaaa", "label": "Take 1", "created_at": 1790900000.0}),
        encoding="utf-8",
    )
    track = {**make_track("k0000aaaa", "Guitar", 0), "active_take_id": "t0000aaaa"}
    write_tracks(jobs_dir / SONG, [track])
    newer = {
        "stem_format": "mp3",
        "stem_mp3_kbps": 192,
        "loudness_version": 2,
        "loudness_remeasured": "stems",
    }
    _finished(jobs_dir, **SAVED, **newer, takes=recover_takes(jobs_dir / SONG), tracks=[track])
    assert FIELDS_0_20_0.isdisjoint({*newer, "versions", "takes", "tracks"}), "0.20.0's fields"

    _saved_by_0_20_0(jobs_dir)
    job = _restart(jobs_dir)

    _assert_kept(job)
    for name, value in newer.items():
        assert getattr(job, name) == value, name
    assert [t["id"] for t in job.takes] == ["t0000aaaa"]
    assert [t["active_take_id"] for t in job.tracks] == ["t0000aaaa"]
    on_disk = json.loads((jobs_dir / SONG / "takes" / "tracks.json").read_text(encoding="utf-8"))
    assert [t["active_take_id"] for t in on_disk["tracks"]] == ["t0000aaaa"]
    # And the registry holds them again, so a second start needs no metadata.
    record = json.loads(registry_path(jobs_dir).read_text(encoding="utf-8"))["jobs"][0]
    saved = Job.from_record(record)
    _assert_kept(saved)
    assert saved.stem_format == "mp3" and [t["id"] for t in saved.takes] == ["t0000aaaa"]


def test_a_song_that_finished_as_the_process_died_keeps_the_registry_fields(jobs_dir):
    """Saved as running, stems complete on disk: the registry, the newer
    source, says what the user chose."""
    _finished(jobs_dir)
    running = Job(id=SONG, status="separating", title="Song", **SAVED)
    registry_path(jobs_dir).write_text(
        json.dumps({"version": 2, "jobs": [running.to_record()]}), encoding="utf-8"
    )

    _assert_kept(_restart(jobs_dir), _registry.SAVED_FIELDS)


def test_a_folder_from_0_20_0_rebuilds_as_before_and_the_log_says_so(jobs_dir, caplog):
    """An upgrade: 0.20.0's metadata.json has none of the saved fields. The
    song comes back with what the folder holds, and the log says which songs
    came back without their favourite, Trash and versions."""
    (jobs_dir / OLD / "stems").mkdir(parents=True)
    (jobs_dir / OLD / "stems" / "vocals.wav").write_bytes(b"RIFF")
    shutil.copy(FIXTURE / "metadata.json", jobs_dir / OLD / "metadata.json")
    _finished(jobs_dir, **SAVED)
    registry_path(jobs_dir).unlink()

    with caplog.at_level(logging.WARNING, logger="stemdeck.registry"):
        _restart(jobs_dir)

    old = _registry.get(OLD)
    assert old.title == "Old Song From 0.20.0"
    assert old.favorite is None and old.trashed_at is None and old.versions is None
    [line] = [r.getMessage() for r in caplog.records if "rebuilt" in r.getMessage()]
    assert "2 song(s)" in line
    assert "1 without" in line and OLD in line


def test_a_registry_that_loads_is_not_overridden_by_metadata(jobs_dir):
    """The registry stays the record: metadata.json only fills in a song the
    registry has lost."""
    _finished(jobs_dir, favorite=True)
    _registry.set_favorite(SONG, False)
    _registry.persist(jobs_dir)

    assert _restart(jobs_dir).favorite is False
