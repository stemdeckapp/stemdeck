"""Recorded takes: storage, upload, alignment, edit, delete, and takes as
export lanes.

The alignment tests are the ones that matter most: a take that is placed a
few milliseconds wrong sounds late, and nothing else in the suite would
notice. They put a click at a known raw frame and assert where it lands in
audio.wav, to the sample.
"""

from __future__ import annotations

import io
import json
import struct

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.models import Job
from app.core.registry import _jobs
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdefabcdef"
SR = 44100
SONG_SEC = 3.0
SONG_FRAMES = int(SR * SONG_SEC)


def _app():
    from app.main import app

    return app


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _make_job(jobs_dir, amplitude=0.0, names=("vocals", "drums", "bass")) -> Job:
    job = Job(id=JOB, status="done", title="Song Title", duration_sec=SONG_SEC)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    t = np.arange(SONG_FRAMES) / SR
    for i, name in enumerate(names):
        x = amplitude * np.sin(2 * np.pi * (440 + 3 * i) * t)
        sf.write(stems / f"{name}.wav", np.stack([x, x], 1), SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    return job


def _click_take(rate: int, seconds: float, click_frame: int, channels: int = 1) -> bytes:
    """float32 LE interleaved PCM, silent but for one full-scale sample."""
    x = np.zeros((int(rate * seconds), channels), dtype="<f4")
    x[click_frame, :] = 1.0
    return x.tobytes()


def _sidecar(first_sample_song_sec: float, rate: int = 48000, channels: int = 1, **extra):
    body = {
        "v": 1,
        "timeline": {"first_sample_song_sec": first_sample_song_sec, "tempo": 1.0},
        "latency": {"round_trip_ms": 23.4, "source": "calibrated", "user_nudge_ms": 0},
        "capture": {"rate": rate, "channels": channels, "device_label": "Test mic"},
    }
    body.update(extra)
    return body


async def _track(c, **body) -> str:
    r = await c.post(f"/api/jobs/{JOB}/tracks", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create(c, track_id: str | None = None) -> str:
    """A take on `track_id`, or on a fresh track of its own."""
    track_id = track_id or await _track(c)
    r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track_id})
    assert r.status_code == 201, r.text
    return r.json()["take_id"]


async def _upload(c, take_id: str, data: bytes, kind: str = "audio", part: int = 1 << 20):
    for seq, start in enumerate(range(0, len(data), part)):
        r = await c.put(
            f"/api/jobs/{JOB}/takes/{take_id}/parts/{kind}?seq={seq}",
            content=data[start : start + part],
            headers={"content-type": "application/octet-stream"},
        )
        assert r.status_code == 200, r.text


def _click_at(path) -> int:
    y, rate = sf.read(path, dtype="float64")
    assert rate == SR
    return int(np.argmax(np.abs(y[:, 0])))


# ─── Create ──────────────────────────────────────────────────────────────


async def test_create_returns_an_id_and_a_default_label(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        track = await _track(c)
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})
        assert r.status_code == 201
        body = r.json()
        assert takes_mod.TAKE_ID_RE.match(body["take_id"])
        assert body["label"] == "Take 1" and body["track_id"] == track
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track, "label": "Guitar"})
        assert r.json()["label"] == "Guitar"
        assert (await c.post(f"/api/jobs/{JOB}/takes", json={})).status_code == 422
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": "k00000000"})
        assert r.status_code == 422 and r.json()["detail"] == "unknown track"
    assert (jobs_dir / JOB / "takes" / body["take_id"] / "parts.json").is_file()


async def test_create_refuses_unknown_job_and_unfinished_job(jobs_dir, client):
    async with client as c:
        assert (await c.post("/api/jobs/000000000000/takes")).status_code == 404
        assert (await c.post("/api/jobs/not-a-job/takes")).status_code == 404
        job = _make_job(jobs_dir)
        job.status = "running"
        assert (await c.post(f"/api/jobs/{JOB}/takes")).status_code == 404


async def test_create_refuses_a_bad_label(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        track = await _track(c)
        for label in ("", "x" * 81, "bad\nlabel"):
            r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track, "label": label})
            assert r.status_code == 422, label


async def test_create_stops_at_the_take_limit(jobs_dir, client, monkeypatch):
    monkeypatch.setattr(takes_mod, "TAKE_MAX_PER_JOB", 2)
    _make_job(jobs_dir)
    async with client as c:
        track = await _track(c)
        await _create(c, track)
        await _create(c, track)
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})
        assert r.status_code == 409


# ─── Parts ───────────────────────────────────────────────────────────────


async def test_parts_must_arrive_in_order_and_a_repeat_is_a_no_op(jobs_dir, client):
    _make_job(jobs_dir)
    url = f"/api/jobs/{JOB}/takes/{{}}/parts/audio?seq={{}}"
    async with client as c:
        tid = await _create(c)
        assert (await c.put(url.format(tid, 0), content=b"\0" * 8)).json()["bytes"] == 8
        dup = await c.put(url.format(tid, 0), content=b"\0" * 8)
        assert dup.status_code == 200 and dup.json()["duplicate"] is True
        assert dup.json()["bytes"] == 8
        assert (await c.put(url.format(tid, 2), content=b"\0" * 8)).status_code == 409
        assert (await c.put(url.format(tid, 1), content=b"\0" * 8)).json()["bytes"] == 16
        state = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/parts")).json()
    assert state["audio"] == {"next_seq": 2, "bytes": 16}
    assert state["camera"] == {"next_seq": 0, "bytes": 0}
    assert (jobs_dir / JOB / "takes" / tid / "audio.part").stat().st_size == 16


async def test_part_validation(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        base = f"/api/jobs/{JOB}/takes/{tid}/parts"
        assert (await c.put(f"{base}/video?seq=0", content=b"x")).status_code == 404
        assert (await c.put(f"{base}/audio?seq=0", content=b"")).status_code == 422
        assert (await c.put(f"{base}/audio?seq=0", content=b"123")).status_code == 422
        assert (await c.put(f"{base}/audio?seq=-1", content=b"1234")).status_code == 422
        assert (await c.put(f"{base}/audio?seq=100001", content=b"1234")).status_code == 422
        assert (await c.put(f"{base}/audio", content=b"1234")).status_code == 422


async def test_part_over_4_mb_is_refused(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        r = await c.put(
            f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=b"\0" * (4 * 1024 * 1024 + 4)
        )
        assert r.status_code == 413


async def test_total_size_is_bounded_by_the_song(jobs_dir, client, monkeypatch):
    monkeypatch.setattr(takes_mod, "TAKE_SLACK_SEC", 0)
    monkeypatch.setattr(takes_mod, "TAKE_CAMERA_MAX_BYTES_PER_SEC", 10)
    _make_job(jobs_dir)  # 3 s song -> camera cap 30 bytes
    async with client as c:
        tid = await _create(c)
        base = f"/api/jobs/{JOB}/takes/{tid}/parts/camera"
        assert (await c.put(f"{base}?seq=0", content=b"\0" * 20)).status_code == 200
        assert (await c.put(f"{base}?seq=1", content=b"\0" * 20)).status_code == 413


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/{job}/takes/../../etc/parts/audio?seq=0",
        "/api/jobs/{job}/takes/..%2F..%2Fetc/parts/audio?seq=0",
        "/api/jobs/{job}/takes/t1234/parts/audio?seq=0",
        "/api/jobs/{job}/takes/T0000000A/parts/audio?seq=0",
        "/api/jobs/..%2F..%2F/takes/t00000000/parts/audio?seq=0",
    ],
)
async def test_traversal_and_malformed_ids_are_404(jobs_dir, client, path):
    _make_job(jobs_dir)
    async with client as c:
        r = await c.put(path.format(job=JOB), content=b"1234")
    # Dots, literal or with an encoded slash, are collapsed or split into
    # extra segments before routing, so those land on no take route at all
    # (405); the malformed ids reach the take routes and are refused by the
    # id checks. Never 2xx and never 500.
    assert r.status_code in ((404, 405) if ".." in path else (404,))
    assert not list(jobs_dir.parent.rglob("*.part"))


async def test_unknown_take_is_404_everywhere(jobs_dir, client):
    _make_job(jobs_dir)
    tid = "t00000000"
    async with client as c:
        assert (
            await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=b"1234")
        ).status_code == 404
        assert (
            await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(0))
        ).status_code == 404
        assert (
            await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"label": "x"})
        ).status_code == 404
        assert (await c.delete(f"/api/jobs/{JOB}/takes/{tid}")).status_code == 404
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/audio.wav")).status_code == 404


# ─── Finish and alignment ────────────────────────────────────────────────


async def test_finish_renders_the_stem_format_and_lands_the_click_on_its_sample(jobs_dir, client):
    """Golden alignment test (05 section 10). A 48 kHz mono take whose raw
    frame 0 sits 0.5 s before song 0 (count-in), click at raw frame 72000
    (1.5 s into the capture): the click belongs at song 1.0 s, sample 44100."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 4.0, 72000))
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(-0.5))
        assert r.status_code == 200, r.text
        entry = r.json()
        state = (await c.get(f"/api/jobs/{JOB}")).json()
    assert entry["id"] == tid and entry["label"] == "Take 1"
    assert entry["url"] == f"/api/jobs/{JOB}/takes/{tid}/audio.wav"
    assert entry["user_nudge_ms"] == 0 and entry["has_camera"] is False
    assert state["takes"] == [entry]

    folder = jobs_dir / JOB / "takes" / tid
    info = sf.info(str(folder / "audio.wav"))
    assert (info.samplerate, info.channels, info.subtype, info.frames) == (
        SR,
        2,
        "PCM_16",
        SONG_FRAMES,
    )
    assert abs(_click_at(folder / "audio.wav") - 44100) <= 1
    y, _ = sf.read(folder / "audio.wav")
    assert np.allclose(y[:, 0], y[:, 1])  # mono copied to both channels

    raw = sf.info(str(folder / "audio.raw.wav"))
    assert (raw.samplerate, raw.channels, raw.subtype, raw.frames) == (48000, 1, "FLOAT", 192000)
    sidecar = json.loads((folder / "take.json").read_text(encoding="utf-8"))
    assert sidecar["id"] == tid and sidecar["v"] == 1 and "created_at" in sidecar
    assert not (folder / "audio.part").exists()
    peaks = json.loads((jobs_dir / JOB / "stems" / "peaks.json").read_text(encoding="utf-8"))
    assert tid in peaks


async def test_a_take_that_starts_late_is_delayed_and_padded(jobs_dir, client):
    """Stereo, 44.1 kHz, raw frame 0 at song 0.25 s, click at raw frame 4410:
    song 0.35 s, sample 15435. The take ends before the song: padded."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(44100, 1.0, 4410, channels=2))
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(0.25, rate=44100, channels=2)
        )
        assert r.status_code == 200, r.text
    wav = jobs_dir / JOB / "takes" / tid / "audio.wav"
    assert sf.info(str(wav)).frames == SONG_FRAMES
    assert abs(_click_at(wav) - 15435) <= 1


async def test_nudge_is_subtracted_and_re_renders_from_the_raw_capture(jobs_dir, client):
    """Positive nudge = the take was late, so it moves earlier."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 4.0, 72000))
        await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(-0.5))
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"user_nudge_ms": 100})
        assert r.status_code == 200, r.text
        assert r.json()["user_nudge_ms"] == 100
        wav = jobs_dir / JOB / "takes" / tid / "audio.wav"
        assert abs(_click_at(wav) - (44100 - 4410)) <= 1
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"user_nudge_ms": -100})
        assert abs(_click_at(wav) - (44100 + 4410)) <= 1
        # A nudge sent at finish is applied the same way.
        tid2 = await _create(c)
        await _upload(c, tid2, _click_take(48000, 4.0, 72000))
        body = _sidecar(-0.5)
        body["latency"]["user_nudge_ms"] = 100
        await c.post(f"/api/jobs/{JOB}/takes/{tid2}/finish", json=body)
    assert abs(_click_at(jobs_dir / JOB / "takes" / tid2 / "audio.wav") - (44100 - 4410)) <= 1


async def test_a_take_entirely_outside_the_song_is_silence_of_the_right_length(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 0.5, 100))
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(-2.0))
        assert r.status_code == 200, r.text
    y, _ = sf.read(jobs_dir / JOB / "takes" / tid / "audio.wav")
    assert len(y) == SONG_FRAMES and not y.any()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(v=2),
        lambda b: b["timeline"].update(tempo=1.25),
        lambda b: b["timeline"].pop("first_sample_song_sec"),
        lambda b: b["timeline"].update(first_sample_song_sec=1e9),
        lambda b: b["capture"].update(channels=3),
        lambda b: b["capture"].update(rate=1000),
        lambda b: b["latency"].update(user_nudge_ms=5000),
        lambda b: b["latency"].update(source="guessed"),
        lambda b: b.update(session={"blob": "x" * 9000}),
        lambda b: b.update(video={"cameraOffsetMs": 99999}),
        lambda b: b.update(label="a\x00b"),
    ],
)
async def test_finish_refuses_a_bad_sidecar(jobs_dir, client, mutate):
    _make_job(jobs_dir)
    body = _sidecar(0.0)
    mutate(body)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 0.1, 0))
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
    assert r.status_code == 422, r.text


async def test_finish_refuses_non_finite_numbers(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 0.1, 0))
        raw = json.dumps(_sidecar(0.0)).replace(
            '"first_sample_song_sec": 0.0', '"first_sample_song_sec": NaN'
        )
        assert "NaN" in raw
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/finish",
            content=raw,
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 422


async def test_finish_refuses_missing_or_ragged_audio_and_can_be_retried(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        url = f"/api/jobs/{JOB}/takes/{tid}/finish"
        assert (await c.post(url, json=_sidecar(0.0))).status_code == 422  # no audio
        await _upload(c, tid, b"\0" * 12)  # 3 samples: not whole stereo frames
        assert (await c.post(url, json=_sidecar(0.0, channels=2))).status_code == 422
        # The same parts are still there; a corrected sidecar finishes.
        assert (await c.post(url, json=_sidecar(0.0, channels=1))).status_code == 200
        assert (await c.post(url, json=_sidecar(0.0))).status_code == 409
        r = await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=1", content=b"1234")
        assert r.status_code == 409


async def test_camera_container_is_sniffed_from_the_bytes(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _create(c)
        await _upload(c, tid, _click_take(48000, 0.5, 0))
        await _upload(c, tid, b"\x1a\x45\xdf\xa3" + b"\0" * 60, kind="camera")
        body = _sidecar(0.0, video={"mime": "video/mp4", "file": "../evil", "cameraOffsetMs": 40})
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
        assert r.status_code == 200, r.text
        entry = r.json()
        cam = await c.get(f"/api/jobs/{JOB}/takes/{tid}/camera")
        bad = await _create(c)
        await _upload(c, bad, _click_take(48000, 0.5, 0))
        await _upload(c, bad, b"not a video at all", kind="camera")
        refused = await c.post(f"/api/jobs/{JOB}/takes/{bad}/finish", json=_sidecar(0.0, video={}))
        # Without a video block the camera upload was incomplete: discarded.
        partial = await _create(c)
        await _upload(c, partial, _click_take(48000, 0.5, 0))
        await _upload(c, partial, b"\x1a\x45\xdf\xa3" + b"\0" * 60, kind="camera")
        no_video = await c.post(f"/api/jobs/{JOB}/takes/{partial}/finish", json=_sidecar(0.0))
    folder = jobs_dir / JOB / "takes" / tid
    assert (folder / "camera.webm").is_file() and not (folder / "camera.part").exists()
    assert entry["has_camera"] is True and entry["camera_offset_ms"] == 40
    assert entry["camera_url"] == f"/api/jobs/{JOB}/takes/{tid}/camera"
    assert json.loads((folder / "take.json").read_text())["video"]["file"] == "camera.webm"
    assert cam.status_code == 200 and cam.headers["content-type"] == "video/webm"
    assert refused.status_code == 422
    assert no_video.status_code == 200 and no_video.json()["has_camera"] is False
    assert not list((jobs_dir / JOB / "takes" / partial).glob("camera*"))


# ─── Edit, delete, serve ─────────────────────────────────────────────────


async def _finished_take(c, jobs_dir, first=0.0) -> str:
    tid = await _create(c)
    await _upload(c, tid, _click_take(48000, 1.0, 24000))
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=_sidecar(first))
    assert r.status_code == 200, r.text
    return tid


async def test_patch_renames_and_validates(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _finished_take(c, jobs_dir)
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"label": "Solo"})
        assert r.status_code == 200 and r.json()["label"] == "Solo"
        assert (await c.get(f"/api/jobs/{JOB}")).json()["takes"][0]["label"] == "Solo"
        assert (await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={})).status_code == 422
        assert (await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"bogus": 1})).status_code == 422
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"user_nudge_ms": 2001})
        assert r.status_code == 422
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"camera_offset_ms": 10})
        assert r.status_code == 422  # no camera
        unfinished = await _create(c)
        r = await c.patch(f"/api/jobs/{JOB}/takes/{unfinished}", json={"label": "x"})
        assert r.status_code == 409


async def test_delete_removes_folder_lane_and_peaks(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _finished_take(c, jobs_dir)
        unfinished = await _create(c)
        r = await c.delete(f"/api/jobs/{JOB}/takes/{tid}")
        assert r.status_code == 200 and r.json() == {"take_id": tid, "status": "deleted"}
        assert (await c.delete(f"/api/jobs/{JOB}/takes/{unfinished}")).status_code == 200
        assert (await c.get(f"/api/jobs/{JOB}")).json()["takes"] == []
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/audio.wav")).status_code == 404
    assert not (jobs_dir / JOB / "takes" / tid).exists()
    assert not (jobs_dir / JOB / "takes" / unfinished).exists()
    peaks = json.loads((jobs_dir / JOB / "stems" / "peaks.json").read_text(encoding="utf-8"))
    assert tid not in peaks


async def test_audio_is_served_with_range_support_only_when_finished(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        unfinished = await _create(c)
        assert (await c.get(f"/api/jobs/{JOB}/takes/{unfinished}/audio.wav")).status_code == 404
        tid = await _finished_take(c, jobs_dir)
        full = await c.get(f"/api/jobs/{JOB}/takes/{tid}/audio.wav")
        head = await c.head(f"/api/jobs/{JOB}/takes/{tid}/audio.wav")
        part = await c.get(
            f"/api/jobs/{JOB}/takes/{tid}/audio.wav", headers={"range": "bytes=0-99"}
        )
    assert full.status_code == 200 and full.headers["content-type"] == "audio/wav"
    assert "Song_Title_Take_1.wav" in full.headers["content-disposition"]
    assert head.status_code == 200
    assert part.status_code == 206 and len(part.content) == 100


async def test_takes_survive_a_registry_rebuild_from_disk(jobs_dir, client):
    """_recover_done_job rebuilds a job from its folder; the takes are files
    first, so they come back with it."""
    skip_without_ffmpeg()
    from app.core.registry import _recover_done_job

    _make_job(jobs_dir)
    async with client as c:
        tid = await _finished_take(c, jobs_dir)
        await _create(c)  # unfinished: not a lane
        entry = (await c.get(f"/api/jobs/{JOB}")).json()["takes"][0]
    recovered = _recover_done_job(jobs_dir / JOB)
    assert recovered is not None and recovered.takes == [entry]
    assert Job.from_record(_jobs[JOB].to_record()).takes == [entry]
    assert entry["id"] == tid


async def test_deleting_the_job_removes_its_takes(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        await _finished_take(c, jobs_dir)
        assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 200
    assert not (jobs_dir / JOB).exists()


# ─── Takes as export lanes ───────────────────────────────────────────────


async def test_take_is_a_mixdown_lane_and_a_nudge_misses_the_cache(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _finished_take(c, jobs_dir)
        url = f"/api/jobs/{JOB}/mixdown.wav?stems=vocals,{tid}&gains=1,0.5"
        r1 = await c.get(url)
        assert r1.status_code == 200, r1.text
        y, _ = sf.read(io.BytesIO(r1.content))
        # Click at song 0.5 s at gain 0.5 (vocals are silent in this fixture).
        assert abs(int(np.argmax(np.abs(y[:, 0]))) - 22050) <= 1
        assert 0.4 < np.abs(y).max() <= 0.5 + 1e-3
        await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"user_nudge_ms": 100})
        r2 = await c.get(url)
        y2, _ = sf.read(io.BytesIO(r2.content))
        assert abs(int(np.argmax(np.abs(y2[:, 0]))) - (22050 - 4410)) <= 1
        assert (
            await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=t0000aaaa&gains=1")
        ).status_code == 422
        unfinished = await _create(c)
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems={unfinished}&gains=1")
        assert r.status_code == 422


async def test_take_goes_in_the_zip_by_label(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _finished_take(c, jobs_dir)
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format=wav&stems=drums,{tid}")
        default = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format=wav")
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert f"Song_Title_Take_1_{tid}.wav" in names and "Song_Title_drums.wav" in names
    assert not any(tid in n for n in zipfile.ZipFile(io.BytesIO(default.content)).namelist())


def test_cache_key_carries_take_versions():
    a = stems_mod._mixdown_cache_key(JOB, "wav", ["t00000000"], [1.0], None, None)
    b = stems_mod._mixdown_cache_key(
        JOB, "wav", ["t00000000"], [1.0], None, None, lane_versions=["t00000000@1:2"]
    )
    assert a != b


# ─── Export clipping ─────────────────────────────────────────────────────


async def test_a_loud_sum_no_longer_clips(jobs_dir, client):
    """Three sines at 0.7 sum to about 2.1: before the limiter the WAV held
    tens of thousands of samples at full scale (the mastering study measured
    77 on a real song). Now none, and the peak is at the -1 dBFS ceiling."""
    skip_without_ffmpeg()
    _make_job(jobs_dir, amplitude=0.7)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals,drums,bass&gains=1,1,1")
        one = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals&gains=4")
    for resp in (r, one):
        assert resp.status_code == 200
        y, _ = sf.read(io.BytesIO(resp.content), dtype="int16")
        assert int((np.abs(y.astype(int)) >= 32767).sum()) == 0
        peak_db = 20 * np.log10(np.abs(y).max() / 32768)
        assert -1.1 < peak_db <= -0.99


async def test_a_mix_under_the_ceiling_is_unchanged(jobs_dir, client):
    """The limiter is idle below -1 dBFS: the export equals the plain sum."""
    skip_without_ffmpeg()
    _make_job(jobs_dir, amplitude=0.2)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals,drums&gains=1,1")
    y, _ = sf.read(io.BytesIO(r.content), dtype="int16")
    stems = jobs_dir / JOB / "stems"
    a, _ = sf.read(stems / "vocals.wav", dtype="int16")
    b, _ = sf.read(stems / "drums.wav", dtype="int16")
    expected = a.astype(int) + b.astype(int)
    assert len(y) == len(expected)
    assert np.abs(y.astype(int) - expected).max() <= 1


def test_float_wav_header_is_what_soundfile_reads(tmp_path):
    from app.pipeline.take_render import assemble_raw_wav

    pcm = tmp_path / "a.part"
    data = np.array([[0.5, -0.25], [1.0, 0.0]], dtype="<f4")
    pcm.write_bytes(data.tobytes())
    assemble_raw_wav(pcm, tmp_path / "a.wav", 48000, 2)
    y, rate = sf.read(tmp_path / "a.wav", dtype="float32")
    assert rate == 48000 and np.array_equal(y, data)
    assert struct.unpack("<H", (tmp_path / "a.wav").read_bytes()[20:22])[0] == 3
