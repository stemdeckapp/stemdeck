"""Recording tracks: a mixer lane that owns takes and plays its active one.

What must not break: a new take becomes the active one, switching the active
take changes what the export plays (and misses the cache), deleting a track
removes its takes, and takes recorded before tracks existed come back after a
restart as a track each.
"""

from __future__ import annotations

import io
import json

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core import registry
from app.core.models import Job
from app.core.registry import _jobs
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdefabcdef"
SR = 44100
SONG_FRAMES = SR * 2


def _app():
    from app.main import app

    return app


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    from app.core import config

    takes_mod._renders.clear()
    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _make_job(jobs_dir) -> Job:
    job = Job(id=JOB, status="done", title="Song Title", duration_sec=2.0)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    silence = np.zeros((SONG_FRAMES, 2))
    for name in ("vocals", "drums"):
        sf.write(stems / f"{name}.wav", silence, SR, subtype="PCM_16")
    return job


async def _track(c, **body) -> dict:
    r = await c.post(f"/api/jobs/{JOB}/tracks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _take(c, track_id: str, click_sec: float) -> str:
    """A finished take on the track with one click at song time `click_sec`."""
    r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track_id})
    assert r.status_code == 201, r.text
    tid = r.json()["take_id"]
    x = np.zeros(SONG_FRAMES, dtype="<f4")
    x[int(click_sec * SR)] = 0.5
    r = await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=x.tobytes())
    assert r.status_code == 200, r.text
    body = {
        "v": 1,
        "timeline": {"first_sample_song_sec": 0.0},
        "capture": {"rate": SR, "channels": 1},
    }
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["track_id"] == track_id
    return tid


def _click(content: bytes) -> int:
    y, _ = sf.read(io.BytesIO(content))
    return int(np.argmax(np.abs(y[:, 0])))


async def _state(c) -> dict:
    return (await c.get(f"/api/jobs/{JOB}")).json()


# ─── Create, list, patch ─────────────────────────────────────────────────


async def test_create_lists_and_persists_tracks(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        a = await _track(c)
        b = await _track(c, name="Guitar", input={"device_id": "abc", "channels": [2, 3]})
        state = await _state(c)
    assert a["name"] == "Track 1" and a["order"] == 0 and a["active_take_id"] is None
    assert a["input"] is None and takes_mod.TRACK_ID_RE.match(a["id"])
    assert a["kind"] == "audio" and b["kind"] == "audio"
    assert b["name"] == "Guitar" and b["order"] == 1
    assert b["input"] == {"device_id": "abc", "device_label": None, "channels": [2, 3]}
    assert state["tracks"] == [a, b]
    on_disk = json.loads((jobs_dir / JOB / "takes" / "tracks.json").read_text(encoding="utf-8"))
    assert on_disk == {"v": 1, "tracks": [a, b]}


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "x" * 81},
        {"name": "a\nb"},
        {"armed": True},
        {"input": {"channels": []}},
        {"input": {"channels": [0, 1, 2]}},
        {"input": {"channels": [1, 1]}},
        {"input": {"channels": [64]}},
        {"input": {"channels": [0], "device_id": "x" * 257}},
        {"input": {"channels": [0], "gain": 2}},
        {"kind": "midi"},
        {"kind": 1},
    ],
)
async def test_create_refuses_bad_bodies(jobs_dir, client, body):
    _make_job(jobs_dir)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/tracks", json=body)
    assert r.status_code == 422, body


async def test_unknown_job_track_and_traversal_are_404(jobs_dir, client):
    async with client as c:
        assert (await c.post("/api/jobs/000000000000/tracks", json={})).status_code == 404
        assert (await c.post("/api/jobs/not-a-job/tracks", json={})).status_code == 404
        _make_job(jobs_dir)
        for tid in ("k00000000", "t00000000", "K0000000a", "..%2F..%2Fetc"):
            url = f"/api/jobs/{JOB}/tracks/{tid}"
            # An encoded slash splits the path before routing: no route, 405.
            allowed = (404, 405) if "%2F" in tid else (404,)
            assert (await c.patch(url, json={"name": "x"})).status_code in allowed, tid
            assert (await c.delete(url)).status_code in allowed, tid
        # The id is checked before the body: a bad body on an unknown track is 404.
        r = await c.patch(f"/api/jobs/{JOB}/tracks/k00000000", content=b"{nope")
        assert r.status_code == 404


async def test_track_limit(jobs_dir, client, monkeypatch):
    monkeypatch.setattr(takes_mod, "TRACK_MAX_PER_JOB", 2)
    _make_job(jobs_dir)
    async with client as c:
        await _track(c)
        await _track(c)
        assert (await c.post(f"/api/jobs/{JOB}/tracks", json={})).status_code == 409


async def test_patch_renames_sets_input_and_reorders(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        a, b, d = await _track(c), await _track(c), await _track(c)
        url = f"/api/jobs/{JOB}/tracks/{d['id']}"
        r = await c.patch(url, json={"name": "Bass", "order": 0, "input": {"channels": [1]}})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "Bass" and r.json()["order"] == 0
        order = [t["id"] for t in (await _state(c))["tracks"]]
        assert order == [d["id"], a["id"], b["id"]]
        r = await c.patch(url, json={"input": None})
        assert r.json()["input"] is None
        r = await c.patch(url, json={"order": 31})
        assert r.json()["order"] == 2  # past the end: moved to the end
        assert (await c.patch(url, json={"order": 99})).status_code == 422
        assert (await c.patch(url, json={})).status_code == 422
        assert (await c.patch(url, json={"name": None})).status_code == 422
        assert (await c.patch(url, json={"order": -1})).status_code == 422


async def test_a_track_can_be_a_video_track(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        v = await _track(c, kind="video")
        assert v["kind"] == "video"
        url = f"/api/jobs/{JOB}/tracks/{v['id']}"
        r = await c.patch(url, json={"kind": "audio"})
        assert r.status_code == 200 and r.json()["kind"] == "audio"
        r = await c.patch(url, json={"kind": None})
        assert r.json()["kind"] == "audio"  # cleared means the default
        assert (await c.patch(url, json={"kind": "midi"})).status_code == 422
        r = await c.patch(url, json={"kind": "video"})
        state = await _state(c)
    assert [t["kind"] for t in state["tracks"]] == ["video"]


# ─── Takes on tracks ─────────────────────────────────────────────────────


async def test_a_new_take_becomes_active_and_the_old_one_can_be_chosen(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c))["id"]
        first = await _take(c, track, 0.5)
        assert (await _state(c))["tracks"][0]["active_take_id"] == first
        second = await _take(c, track, 1.0)
        state = await _state(c)
        assert state["tracks"][0]["active_take_id"] == second
        assert [t["id"] for t in state["takes"] if t["track_id"] == track] == [first, second]
        assert [t["label"] for t in state["takes"]] == ["Take 1", "Take 2"]

        url = f"/api/jobs/{JOB}/tracks/{track}"
        r = await c.patch(url, json={"active_take_id": first})
        assert r.status_code == 200 and r.json()["active_take_id"] == first
        other = (await _track(c))["id"]
        foreign = await _take(c, other, 0.2)
        r = await c.patch(url, json={"active_take_id": foreign})
        assert r.status_code == 422
        r = await c.patch(url, json={"active_take_id": "t00000000"})
        assert r.status_code == 422


async def test_deleting_the_active_take_falls_back_to_the_newest(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c))["id"]
        first = await _take(c, track, 0.5)
        second = await _take(c, track, 1.0)
        await c.patch(f"/api/jobs/{JOB}/tracks/{track}", json={"active_take_id": second})
        assert (await c.delete(f"/api/jobs/{JOB}/takes/{second}")).status_code == 200
        assert (await _state(c))["tracks"][0]["active_take_id"] == first
        assert (await c.delete(f"/api/jobs/{JOB}/takes/{first}")).status_code == 200
        assert (await _state(c))["tracks"][0]["active_take_id"] is None


async def test_deleting_a_track_deletes_its_takes_and_stops_renders(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c))["id"]
        keep = (await _track(c))["id"]
        done = await _take(c, track, 0.5)
        kept = await _take(c, keep, 0.5)
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})
        unfinished = r.json()["take_id"]
        run = takes_mod._RenderRun(status="running")
        takes_mod._renders[(JOB, done)] = run
        r = await c.delete(f"/api/jobs/{JOB}/tracks/{track}")
        assert r.status_code == 200, r.text
        assert r.json() == {
            "track_id": track,
            "status": "deleted",
            "takes_deleted": sorted([done, unfinished]),
        }
        state = await _state(c)
    assert run.cancel.is_set() and (JOB, done) not in takes_mod._renders
    assert [t["id"] for t in state["tracks"]] == [keep]
    assert [t["id"] for t in state["takes"]] == [kept]
    assert state["tracks"][0]["order"] == 0
    takes = jobs_dir / JOB / "takes"
    assert not (takes / done).exists() and not (takes / unfinished).exists()
    assert (takes / kept).is_dir()
    peaks = json.loads((jobs_dir / JOB / "stems" / "peaks.json").read_text(encoding="utf-8"))
    assert done not in peaks and kept in peaks


# ─── Export ──────────────────────────────────────────────────────────────


async def test_a_track_lane_plays_its_active_take_and_switching_misses_the_cache(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c))["id"]
        first = await _take(c, track, 0.5)
        await _take(c, track, 1.0)
        url = f"/api/jobs/{JOB}/mixdown.wav?stems=drums,{track}&gains=1,1"
        r = await c.get(url)
        assert r.status_code == 200, r.text
        assert abs(_click(r.content) - SR) <= 1  # the second (active) take
        await c.patch(f"/api/jobs/{JOB}/tracks/{track}", json={"active_take_id": first})
        r = await c.get(url)
        assert abs(_click(r.content) - SR // 2) <= 1
        # Take-id lanes still work beside track lanes.
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems={first}&gains=1")
        assert r.status_code == 200


async def test_a_track_without_a_take_is_refused(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c))["id"]
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems={track}&gains=1")
        assert r.status_code == 422 and r.json()["detail"] == "track has no take"
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=k00000000&gains=1")
        assert r.status_code == 422


async def test_a_track_goes_in_the_zip_by_name(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await _track(c, name="Lead Guitar"))["id"]
        await _take(c, track, 0.5)
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format=wav&stems=drums,{track}")
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert f"Song_Title_Lead_Guitar_{track}.wav" in names


def test_cache_key_names_the_active_take(jobs_dir, monkeypatch):
    job = _make_job(jobs_dir)
    folder = jobs_dir / JOB / "takes"
    for tid in ("t00000001", "t00000002"):
        (folder / tid).mkdir(parents=True)
        (folder / tid / "audio.wav").write_bytes(b"RIFF")
        job.takes.append({"id": tid, "track_id": "k00000001"})
    job.tracks = [{"id": "k00000001", "active_take_id": "t00000001"}]
    a = stems_mod._take_versions(JOB, ["drums", "k00000001"])
    job.tracks = [{"id": "k00000001", "active_take_id": "t00000002"}]
    b = stems_mod._take_versions(JOB, ["drums", "k00000001"])
    assert a != b and a[0].startswith("k00000001=t00000001@")


async def test_the_render_refuses_the_track_that_plays_the_take(jobs_dir, client, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(takes_mod, "camera_file", lambda folder: folder / "audio.wav")
    monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
    monkeypatch.setattr(takes_mod, "aac_available", lambda: True)
    async with client as c:
        track = (await _track(c))["id"]
        tid = await _take(c, track, 0.5)
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/render", json={"stems": [track], "gains": [1]}
        )
    assert r.status_code == 422


# ─── Migration and restart ───────────────────────────────────────────────


def _legacy_take(jobs_dir, tid: str, created: float) -> dict:
    """A finished take written before tracks existed: no track_id anywhere."""
    folder = jobs_dir / JOB / "takes" / tid
    folder.mkdir(parents=True)
    sf.write(folder / "audio.wav", np.zeros((SONG_FRAMES, 2)), SR, subtype="PCM_16")
    sidecar = {"v": 1, "id": tid, "label": f"Take {tid[-1]}", "created_at": created}
    (folder / "take.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return {
        "id": tid,
        "label": sidecar["label"],
        "created_at": created,
        "url": f"/api/jobs/{JOB}/takes/{tid}/audio.wav",
        "user_nudge_ms": 0.0,
        "has_camera": False,
        "camera_offset_ms": 0.0,
        "camera_url": None,
    }


def test_legacy_takes_become_a_track_each_on_restart(jobs_dir):
    job = _make_job(jobs_dir)
    job.takes = [_legacy_take(jobs_dir, "t00000001", 1.0), _legacy_take(jobs_dir, "t00000002", 2.0)]
    registry.persist(jobs_dir)
    _jobs.clear()

    registry.restore(jobs_dir)
    restored = _jobs[JOB]
    assert [t["name"] for t in restored.tracks] == ["Take 1", "Take 2"]
    assert [t["active_take_id"] for t in restored.tracks] == ["t00000001", "t00000002"]
    assert [t["track_id"] for t in restored.takes] == [t["id"] for t in restored.tracks]
    sidecar = json.loads(
        (jobs_dir / JOB / "takes" / "t00000001" / "take.json").read_text(encoding="utf-8")
    )
    assert sidecar["track_id"] == restored.tracks[0]["id"]

    # A second restart changes nothing: the migration is done once.
    tracks = restored.tracks
    _jobs.clear()
    registry.restore(jobs_dir)
    assert _jobs[JOB].tracks == tracks


def test_tracks_come_back_from_disk_without_a_registry_record(jobs_dir):
    job = _make_job(jobs_dir)
    _legacy_take(jobs_dir, "t00000001", 1.0)
    (jobs_dir / JOB / "metadata.json").write_text('{"title": "Song Title"}', encoding="utf-8")
    del job
    _jobs.clear()
    registry.registry_path(jobs_dir).unlink(missing_ok=True)

    registry.restore(jobs_dir)
    restored = _jobs[JOB]
    assert len(restored.tracks) == 1
    assert restored.tracks[0]["active_take_id"] == "t00000001"
    assert restored.takes[0]["track_id"] == restored.tracks[0]["id"]
