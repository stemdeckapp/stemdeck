"""The take's picture sync: measured at finish, the one placement the review
and the export share, the nudge route, and a render whose picture lands where
the camera's own sound says.

The camera files are made with ffmpeg the way MediaRecorder makes them: a
picture with the camera microphone's sound in one file. Its first frame is
placed `SHIFT_MS` from the take's raw frame 0 (the maintainer's StreamCam:
about -70 ms), while the browser's estimate is 400 ms off.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.core.takes import SYNC_REASONS, camera_start_sec, sync_block, take_entry, video_sync
from tests.ffmpeg_probe import skip_without_ffmpeg
from tests.test_av_sync import RATE, _camera_file, music

JOB = "abcdefabcdef"
SR = 44100
SONG_SEC = 10.0
SHIFT_MS = -70.0
ESTIMATE_MS = SHIFT_MS + 400.0
FIRST = 0.5  # first_sample_song_sec
# The camera's flashes, as camera frame numbers at 30 fps (1.0, 3.0, 5.0 s).
FLASH_FRAMES = (30, 90, 150)


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
    """A song of silent stems: the render's sound is then the take alone."""
    job = Job(id=JOB, status="done", title="Song", duration_sec=SONG_SEC)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    silence = np.zeros((int(SR * SONG_SEC), 2))
    for name in ("vocals", "drums"):
        sf.write(stems / f"{name}.wav", silence, SR, subtype="PCM_16")
    return job


def _beep(x: np.ndarray, at_sec: float) -> None:
    i = int(round(at_sec * RATE))
    n = int(0.04 * RATE)
    x[i : i + n] += 0.8 * np.sin(2 * np.pi * 1000 * np.arange(n) / RATE)


def _media(tmp_path, *, with_audio=True, container="mp4"):
    """(raw take bytes, camera file bytes). The take hears quiet playing and a
    beep at each flash; the camera file's picture flashes and its sound is the
    same playing and beeps, on its own clock, SHIFT_MS from the take's."""
    sec = 8.0
    shift = SHIFT_MS / 1000.0
    play = 0.1 * music(sec + 1, 41, RATE)
    raw = play[: int(sec * RATE)].copy()
    for f in FLASH_FRAMES:
        _beep(raw, f / 30 + shift)
    # cam(u) = raw(u + shift)
    s = int(round(shift * RATE))
    n = int(sec * RATE)
    cam = raw[s : s + n] if s >= 0 else np.concatenate([np.zeros(-s), raw[: n + s]])
    path = tmp_path / f"camera-{time.monotonic_ns()}.{container}"
    _camera_file(path, cam, container)
    if not with_audio:
        silent = path.with_name("v-" + path.name)
        _strip_audio(path, silent)
        path = silent
    # The flashes, drawn over the file's own picture.
    flashed = path.with_name("f-" + path.name)
    _flash(path, flashed, container)
    return raw.astype("<f4").tobytes(), flashed.read_bytes()


def _ffmpeg(*args: str) -> None:
    import subprocess

    from app.core.config import ffmpeg_executable

    subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-loglevel", "error", "-y", *args],
        check=True,
        timeout=120,
    )


def _strip_audio(src, dest) -> None:
    _ffmpeg("-i", str(src), "-map", "0:v", "-c", "copy", str(dest))


def _flash(src, dest, container: str) -> None:
    enable = "+".join(f"eq(n,{f})" for f in FLASH_FRAMES)
    vcodec = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if container == "webm":
        vcodec = ["-c:v", "libvpx", "-b:v", "500k"]
    _ffmpeg(
        "-i",
        str(src),
        "-vf",
        f"drawbox=c=white:t=fill:enable='{enable}'",
        *vcodec,
        "-c:a",
        "copy",
        str(dest),
    )


async def _take(
    c, tmp_path, *, with_audio=True, camera_audio="recorded", container="mp4", draw_age_ms=None
):
    track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
    tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
    raw, cam = _media(tmp_path, with_audio=with_audio, container=container)
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=raw)
    ).status_code == 200
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
    ).status_code == 200
    video = {"mime": f"video/{container}", "firstFrameAtMs": ESTIMATE_MS, "parts": 1}
    if draw_age_ms is not None:
        video["drawAgeMs"] = draw_age_ms
    if camera_audio is not None:
        video["cameraAudio"] = {"state": camera_audio, "label": "Test Cam"}
    body = {
        "v": 1,
        "timeline": {"first_sample_song_sec": FIRST, "tempo": 1.0},
        "latency": {"round_trip_ms": 76.0, "source": "estimated"},
        "capture": {"rate": RATE, "channels": 1},
        "video": video,
    }
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
    assert r.status_code == 200, r.text
    return tid, r.json()


# ─── Finish measures ─────────────────────────────────────────────────────


@pytest.mark.parametrize("container", ["mp4", "webm"])
async def test_finish_measures_the_picture_from_the_camera_sound(
    jobs_dir, client, tmp_path, container
):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, entry = await _take(c, tmp_path, container=container)
    sync = entry["video_sync"]
    assert sync["source"] == "measured"
    assert sync["base"] == "measured"
    assert sync["reason"] == "ok"
    assert abs(sync["offsetMs"] - SHIFT_MS) < 10.0, sync
    assert sync["estimateMs"] == ESTIMATE_MS
    assert sync["nudgeMs"] == 0
    assert 0.5 <= sync["confidence"] <= 1.0
    assert sync["pictureOffsetMs"] == sync["offsetMs"]
    assert entry["camera_start_sec"] == pytest.approx(FIRST + sync["offsetMs"] / 1000, abs=1e-4)
    stored = json.loads((jobs_dir / JOB / "takes" / tid / "take.json").read_text("utf-8"))
    assert stored["video"]["sync"]["base"] == "measured"
    assert "pictureOffsetMs" not in stored["video"]["sync"]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("no-mic", "no-camera-mic"),
        ("denied", "camera-mic-denied"),
        ("not-asked", "camera-mic-not-asked"),
        ("failed", "camera-mic-failed"),
        ("unsupported", "no-camera-audio"),
        (None, "no-camera-audio"),
    ],
)
async def test_a_camera_without_sound_keeps_the_estimate_and_says_why(
    jobs_dir, client, tmp_path, state, reason
):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        _tid, entry = await _take(c, tmp_path, with_audio=False, camera_audio=state)
    sync = entry["video_sync"]
    assert sync["source"] == "estimated"
    assert sync["reason"] == reason
    assert sync["offsetMs"] == ESTIMATE_MS
    assert sync["confidence"] is None
    assert entry["camera_start_sec"] == pytest.approx(FIRST + ESTIMATE_MS / 1000, abs=1e-4)


async def test_finish_keeps_the_frame_age_and_refuses_one_out_of_range(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        _tid, entry = await _take(c, tmp_path, draw_age_ms=40.0)
        sync = entry["video_sync"]
        assert sync["drawAgeMs"] == 40.0
        assert sync["pictureOffsetMs"] == pytest.approx(sync["offsetMs"] - 40.0)
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
        body = {
            "v": 1,
            "timeline": {"first_sample_song_sec": FIRST, "tempo": 1.0},
            "capture": {"rate": RATE, "channels": 1},
            "video": {"mime": "video/mp4", "drawAgeMs": 1500},
        }
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
        assert r.status_code == 422


@pytest.mark.parametrize(
    ("result", "state", "reason", "base"),
    [
        # The camera sound failed part way (cameraRecorder.js): a miss says so.
        (("silent", False), "failed", "camera-mic-failed", "estimated"),
        (("low-confidence", False), "failed", "camera-mic-failed", "estimated"),
        # What was recorded before it failed still matched: measured.
        (("ok", True), "failed", "ok", "measured"),
        # A recorded sound that did not match keeps the measurement's reason.
        (("low-confidence", False), "recorded", "low-confidence", "estimated"),
    ],
)
async def test_the_browser_word_on_the_camera_sound_explains_a_miss(
    jobs_dir, client, tmp_path, monkeypatch, result, state, reason, base
):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    why, ok = result
    monkeypatch.setattr(
        takes_mod.av_sync,
        "measure_take_offset",
        lambda *_a, **_k: takes_mod.av_sync.SyncResult(ok, why, -70.0 if ok else None, 0.9),
    )
    async with client as c:
        _tid, entry = await _take(c, tmp_path, camera_audio=state)
    assert entry["video_sync"]["reason"] == reason
    assert entry["video_sync"]["base"] == base


async def test_a_failing_measurement_never_costs_the_take(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)

    def boom(*_a, **_k):
        raise MemoryError("synthetic")

    monkeypatch.setattr(takes_mod.av_sync, "measure_take_offset", boom)
    async with client as c:
        _tid, entry = await _take(c, tmp_path)
    assert entry["has_camera"] is True
    assert entry["video_sync"]["reason"] == "measure-failed"
    assert entry["video_sync"]["offsetMs"] == ESTIMATE_MS


async def test_a_sent_sync_block_is_ignored(jobs_dir, client, tmp_path, monkeypatch):
    """video.sync is the server's: a browser cannot claim a measurement."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(
        takes_mod.av_sync,
        "measure_take_offset",
        lambda *_a, **_k: takes_mod.av_sync.SyncResult(False, "silent"),
    )
    async with client as c:
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
        raw, cam = _media(tmp_path)
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=raw)
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
        forged = {"base": "measured", "offsetMs": 999, "reason": "ok", "nudgeMs": 0}
        body = {
            "v": 1,
            "timeline": {"first_sample_song_sec": FIRST, "tempo": 1.0},
            "capture": {"rate": RATE, "channels": 1},
            "video": {"mime": "video/mp4", "firstFrameAtMs": ESTIMATE_MS, "sync": forged},
        }
        entry = (await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)).json()
    assert entry["video_sync"]["base"] == "estimated"
    assert entry["video_sync"]["reason"] == "silent"
    assert entry["video_sync"]["offsetMs"] == ESTIMATE_MS


# ─── One placement ───────────────────────────────────────────────────────


def _sidecar(video):
    return {
        "id": "t0000aaaa",
        "timeline": {"first_sample_song_sec": 2.0},
        "latency": {"round_trip_ms": 100.0, "user_nudge_ms": 20.0},
        "video": video,
    }


def test_an_older_take_is_placed_by_its_estimate():
    """No sync block (a take finished before it existed): the estimate, as
    the review always placed it, and the round trip is not added."""
    side = _sidecar({"firstFrameAtMs": 456.0, "cameraOffsetMs": 30.0})
    sync = video_sync(side["video"])
    assert sync["source"] == "estimated" and sync["reason"] == "legacy"
    assert sync["pictureOffsetMs"] == 426.0
    assert camera_start_sec(side) == pytest.approx(2.0 - 0.02 + 0.426)
    entry = take_entry(JOB, side, True)
    assert entry["camera_start_sec"] == pytest.approx(2.406)
    assert entry["video_sync"]["reason"] == "legacy"
    assert take_entry(JOB, side, False)["video_sync"] is None


def test_the_measured_offset_and_both_nudges_add_up():
    block = sync_block(
        estimate_ms=456.0, measured_ms=-70.0, confidence=0.9, reason="ok", nudge_ms=15.0
    )
    side = _sidecar({"firstFrameAtMs": 456.0, "cameraOffsetMs": 10.0, "sync": block})
    sync = video_sync(side["video"])
    assert sync["source"] == "manual" and sync["base"] == "measured"
    assert sync["pictureOffsetMs"] == pytest.approx(-70.0 + 15.0 - 10.0)
    assert camera_start_sec(side) == pytest.approx(1.98 - 0.065)
    # A render request's own camera_offset_ms replaces the stored one.
    assert camera_start_sec(side, 0.0) == pytest.approx(1.98 - 0.055)


def test_the_canvas_frame_age_moves_the_picture_earlier():
    """Performance records drawn frames: what each shows was captured
    drawAgeMs before it was drawn, measured or estimated base alike."""
    block = sync_block(estimate_ms=300.0, measured_ms=-70.0, confidence=0.9, reason="ok")
    side = _sidecar({"firstFrameAtMs": 300.0, "drawAgeMs": 48.5, "sync": block})
    sync = video_sync(side["video"])
    assert sync["drawAgeMs"] == 48.5
    assert sync["pictureOffsetMs"] == pytest.approx(-118.5)
    assert camera_start_sec(side) == pytest.approx(1.98 - 0.1185)
    legacy = video_sync({"firstFrameAtMs": 300.0, "drawAgeMs": 5000})
    assert legacy["drawAgeMs"] == 1000.0  # bounded
    assert video_sync({"firstFrameAtMs": 300.0})["drawAgeMs"] == 0.0


@pytest.mark.parametrize(
    "stored",
    [
        {"base": "measured", "offsetMs": "x", "nudgeMs": 1e9, "confidence": 7},
        {"base": "nonsense", "reason": "ok", "offsetMs": 5},
        {"reason": "made-up"},
        {"nudgeMs": None, "measuredAt": "yesterday", "metrics": [1, 2]},
    ],
)
def test_a_damaged_sync_block_reads_safely(stored):
    sync = video_sync({"firstFrameAtMs": 100.0, "sync": stored})
    assert sync["reason"] in SYNC_REASONS
    assert -2000 <= sync["nudgeMs"] <= 2000
    assert sync["confidence"] is None or 0 <= sync["confidence"] <= 1
    assert isinstance(sync["pictureOffsetMs"], float)
    if sync["base"] == "estimated":
        assert sync["offsetMs"] == 100.0
        assert sync["reason"] != "ok"


# ─── The route ───────────────────────────────────────────────────────────


async def test_the_nudge_moves_the_picture_and_drops_the_render(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, entry = await _take(c, tmp_path)
        render = jobs_dir / JOB / "takes" / tid / "render.mp4"
        render.write_bytes(b"old")
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"nudgeMs": 40})
        assert r.status_code == 200, r.text
        moved = r.json()
        assert not render.exists()
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()["status"] == "none"
        got = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/sync")).json()
        job_take = next(t for t in _jobs[JOB].takes if t["id"] == tid)
    sync = moved["video_sync"]
    assert sync["source"] == "manual" and sync["base"] == "measured"
    assert sync["nudgeMs"] == 40
    assert sync["pictureOffsetMs"] == pytest.approx(entry["video_sync"]["offsetMs"] + 40)
    assert moved["camera_start_sec"] == pytest.approx(entry["camera_start_sec"] + 0.04, abs=2e-4)
    assert got["video_sync"] == sync
    assert got["camera_start_sec"] == moved["camera_start_sec"]
    assert job_take["video_sync"] == sync


async def test_the_same_nudge_keeps_the_render(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        render = jobs_dir / JOB / "takes" / tid / "render.mp4"
        render.write_bytes(b"old")
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"nudgeMs": 0})
        assert r.status_code == 200
    assert render.exists()
    assert r.json()["video_sync"]["source"] == "measured"


async def test_remeasure_keeps_the_nudge(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"nudgeMs": -25})
        monkeypatch.setattr(
            takes_mod.av_sync,
            "measure_take_offset",
            lambda *_a, **_k: takes_mod.av_sync.SyncResult(False, "low-confidence", None, 0.2),
        )
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"remeasure": True})
    sync = r.json()["video_sync"]
    assert sync["base"] == "estimated" and sync["reason"] == "low-confidence"
    assert sync["confidence"] == 0.2
    assert sync["nudgeMs"] == -25 and sync["source"] == "manual"
    assert sync["offsetMs"] == ESTIMATE_MS


def _raise_memory_error(*_a, **_k):
    raise MemoryError("synthetic")


async def test_a_remeasure_that_cannot_run_changes_nothing(jobs_dir, client, tmp_path, monkeypatch):
    """ffmpeg failing on a re-measure: 503, and the measured block, the
    nudge and the render stay as they were; take.json is not written."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, entry = await _take(c, tmp_path)
        folder = jobs_dir / JOB / "takes" / tid
        render = folder / "render.mp4"
        render.write_bytes(b"old")
        side_before = (folder / "take.json").read_bytes()
        mtime = (folder / "take.json").stat().st_mtime_ns
        for broken in (
            lambda *_a, **_k: takes_mod.av_sync.SyncResult(False, "measure-failed"),
            _raise_memory_error,
        ):
            monkeypatch.setattr(takes_mod.av_sync, "measure_take_offset", broken)
            for body in ({"remeasure": True}, {"remeasure": True, "nudgeMs": 30}):
                r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json=body)
                assert r.status_code == 503, r.text
                assert r.json()["detail"] == "measurement could not run: nothing changed"
        got = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/sync")).json()
    assert render.read_bytes() == b"old"
    assert (folder / "take.json").read_bytes() == side_before
    assert (folder / "take.json").stat().st_mtime_ns == mtime
    assert got["video_sync"] == entry["video_sync"]
    assert got["video_sync"]["base"] == "measured"


def _busy_sequence(monkeypatch, answers):
    """_render_busy answering `answers` in turn, then the last one."""
    seq = list(answers)

    def busy(*_a):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(takes_mod, "_render_busy", busy)


@pytest.mark.parametrize(
    ("path", "body"),
    [("/sync", {"nudgeMs": 40}), ("", {"camera_offset_ms": 40})],
)
async def test_a_render_queued_meanwhile_is_seen_under_the_lock(
    jobs_dir, client, tmp_path, monkeypatch, path, body
):
    """Free when the request arrives, queued by the time it holds the take's
    lock: 409, nothing written, the render left alone."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        folder = jobs_dir / JOB / "takes" / tid
        render = folder / "render.mp4"
        render.write_bytes(b"old")
        side_before = (folder / "take.json").read_bytes()
        _busy_sequence(monkeypatch, [False, True])
        method = c.post if path else c.patch
        r = await method(f"/api/jobs/{JOB}/takes/{tid}{path}", json=body)
    assert r.status_code == 409 and r.json()["detail"] == "video render running"
    assert render.read_bytes() == b"old"
    assert (folder / "take.json").read_bytes() == side_before


@pytest.mark.parametrize(
    ("path", "body"),
    [("/sync", {"nudgeMs": 40}), ("", {"camera_offset_ms": 40})],
)
async def test_a_moved_picture_never_drops_a_running_render(
    jobs_dir, client, tmp_path, monkeypatch, path, body
):
    """A render running once the move is written stays registered, so its
    Cancel still reaches it; a finished run's view is dropped as before."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        run = object()
        takes_mod._renders[(JOB, tid)] = run
        _busy_sequence(monkeypatch, [False, False, True])
        method = c.post if path else c.patch
        r = await method(f"/api/jobs/{JOB}/takes/{tid}{path}", json=body)
        assert r.status_code == 200, r.text
        assert takes_mod._renders.get((JOB, tid)) is run
        _busy_sequence(monkeypatch, [False])
        body2 = {k: v + 10 for k, v in body.items()}
        r = await method(f"/api/jobs/{JOB}/takes/{tid}{path}", json=body2)
        assert r.status_code == 200
        assert (JOB, tid) not in takes_mod._renders


async def test_an_older_take_can_be_nudged_and_measured(jobs_dir, client, tmp_path):
    """A take on disk with no sync block: nudged, it stays "legacy"; asked to
    measure, it is measured."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        path = jobs_dir / JOB / "takes" / tid / "take.json"
        side = json.loads(path.read_text("utf-8"))
        del side["video"]["sync"]
        path.write_text(json.dumps(side), "utf-8")
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"nudgeMs": 10})
        assert r.json()["video_sync"]["reason"] == "legacy"
        assert r.json()["video_sync"]["pictureOffsetMs"] == ESTIMATE_MS + 10
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/sync", json={"remeasure": True})
    sync = r.json()["video_sync"]
    assert sync["base"] == "measured" and sync["nudgeMs"] == 10
    assert abs(sync["offsetMs"] - SHIFT_MS) < 10.0


async def test_sync_refusals(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid, _entry = await _take(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}/sync"
        for body, detail in (
            ({}, "nothing to change"),
            ({"remeasure": False}, "nothing to change"),
            ({"nudgeMs": 2000.5}, None),
            ({"nudgeMs": -2001}, None),
            ({"nudgeMs": "40"}, None),
            ({"remeasure": "yes"}, None),
            ({"nudgeMs": 1, "offsetMs": 5}, None),
        ):
            r = await c.post(base, json=body)
            assert r.status_code == 422, (body, r.text)
            if detail:
                assert r.json()["detail"] == detail
        r = await c.post(
            base, content=b'{"nudgeMs": NaN}', headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "body must be JSON with finite numbers"
        # Ids and paths.
        assert (
            await c.post("/api/jobs/nope/takes/t0000aaaa/sync", json={"nudgeMs": 1})
        ).status_code == 404
        assert (
            await c.post(f"/api/jobs/{JOB}/takes/t0000aaaa/sync", json={"nudgeMs": 1})
        ).status_code == 404
        for evil in ("..%2F..%2Fstems", "%2E%2E", "t0000aaa%2F", "T0000AAAA"):
            r = await c.post(f"/api/jobs/{JOB}/takes/{evil}/sync", json={"nudgeMs": 1})
            # Dots are collapsed or split into extra segments before routing
            # (405, as in test_takes_api); malformed ids reach the route and
            # are refused (404). Never 2xx and never 500.
            assert r.status_code in ((404, 405) if "2" in evil else (404,)), (evil, r.status_code)
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}x/sync")).status_code == 404
        # Not finished yet.
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        open_tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()[
            "take_id"
        ]
        r = await c.post(f"/api/jobs/{JOB}/takes/{open_tid}/sync", json={"nudgeMs": 1})
        assert r.status_code == 409 and r.json()["detail"] == "take not finished"
        assert (await c.get(f"/api/jobs/{JOB}/takes/{open_tid}/sync")).status_code == 404
        # A render running.
        # (Put back by hand: monkeypatch.undo() would also undo the
        # conftest's jobs-folder isolation.)
        busy = takes_mod._render_busy
        monkeypatch.setattr(takes_mod, "_render_busy", lambda *_a: True)
        r = await c.post(base, json={"nudgeMs": 1})
        assert r.status_code == 409 and r.json()["detail"] == "video render running"
        monkeypatch.setattr(takes_mod, "_render_busy", busy)
        # No camera.
        (jobs_dir / JOB / "takes" / tid / "camera.mp4").unlink()
        r = await c.post(base, json={"nudgeMs": 1})
        assert r.status_code == 422 and r.json()["detail"] == "take has no camera"
        assert (await c.get(base)).status_code == 422


async def test_a_cross_site_sync_is_refused(jobs_dir, client, tmp_path):
    """The global CSRF middleware covers the route like every other POST."""
    _make_job(jobs_dir)
    async with client as c:
        r = await c.post(
            f"/api/jobs/{JOB}/takes/t0000aaaa/sync",
            json={"nudgeMs": 1},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
    assert r.status_code == 403


# ─── The render puts the picture where the sound says ────────────────────


def _frame_means(path) -> np.ndarray:
    import subprocess

    from app.core.config import ffmpeg_executable

    out = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            "scale=16:16,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, 256).mean(axis=1)


def _audio(path) -> np.ndarray:
    import subprocess

    from app.core.config import ffmpeg_executable

    out = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SR),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def _starts(flags: np.ndarray, gap: int) -> list[int]:
    idx = np.flatnonzero(flags)
    if not len(idx):
        return []
    keep = [int(idx[0])]
    keep += [int(b) for a, b in zip(idx[:-1], idx[1:], strict=True) if b - a > gap]
    return keep


async def _wait(c, tid, timeout=240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()
        if view["status"] in ("done", "error", "none"):
            return view
        await asyncio.sleep(0.2)
    raise AssertionError("render did not finish")


@pytest.mark.parametrize("fit", ["fill", "cover"])
async def test_the_export_puts_each_flash_on_its_beep(jobs_dir, client, tmp_path, fit):
    """The browser's estimate is 400 ms off; the measured offset puts every
    flash on its beep in the MP4 to within one output frame at 60 fps, for
    Performance's fit "fill" and the studio's "cover" alike."""
    skip_without_ffmpeg()
    from app.pipeline.take_render import libx264_available

    if not libx264_available():
        pytest.skip("this ffmpeg has no libx264")
    _make_job(jobs_dir)
    async with client as c:
        tid, entry = await _take(c, tmp_path)
        assert entry["video_sync"]["source"] == "measured"
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/render",
            json={"canvas": "16:9", "fit": fit, "size": "720", "fps": 60, "span": "song"},
        )
        assert r.status_code == 202, r.text
        assert (await _wait(c, tid))["status"] == "done"
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    flashes = [f / 60 for f in _starts(_frame_means(out) > 200, 3)]
    sound = _audio(out)
    beeps = [i / SR for i in _starts(np.abs(sound) > 0.4, int(0.3 * SR))]
    assert len(flashes) == len(FLASH_FRAMES), flashes
    assert len(beeps) == len(FLASH_FRAMES), beeps
    for flash, beep in zip(flashes, beeps, strict=True):
        # The flash is the first output frame showing it: up to one frame
        # (16.7 ms) after the instant, plus the measurement's error.
        assert -0.012 <= flash - beep <= 1 / 60 + 0.012, (flash, beep)
