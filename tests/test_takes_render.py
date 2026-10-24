"""Video render of a take: camera tile + backing mix + take audio -> MP4.

The end-to-end tests run the real ffmpeg on a white camera clip and read
frames back, so they check placement and the offset sign, not just that a
file appeared.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.config import ffmpeg_executable, ffprobe_executable
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import take_render
from app.pipeline.take_render import AudioLane, build_video_render_cmd, camera_filter
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdefabcdef"
SR = 44100
SONG_SEC = 3.0


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
    job = Job(id=JOB, status="done", title="Song", duration_sec=SONG_SEC)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(SR * SONG_SEC)) / SR
    for name in ("vocals", "drums"):
        x = 0.2 * np.sin(2 * np.pi * 440 * t)
        sf.write(stems / f"{name}.wav", np.stack([x, x], 1), SR, subtype="PCM_16")
    return job


def _white_camera(path, seconds=1.0, container="mp4") -> bytes:
    """A white 1 s clip. WebM is written to a pipe, as MediaRecorder writes
    it: no seek-back, so no duration, no cues and clusters of unknown size."""
    if container == "webm":
        out = subprocess.run(
            [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=white:s=320x240:r=30:d={seconds}",
                "-c:v",
                "libvpx",
                "-f",
                "webm",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
        path.write_bytes(out)
        return out
    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=white:s=320x240:r=30:d={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


async def _take_with_camera(
    c, tmp_path, first_frame_ms=1000.0, camera_offset_ms=0.0, container="mp4"
) -> str:
    track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
    r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})
    tid = r.json()["take_id"]
    audio = np.zeros((48000, 1), dtype="<f4").tobytes()
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=audio)
    ).status_code == 200
    cam = _white_camera(tmp_path / f"cam.{container}", container=container)
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
    ).status_code == 200
    body = {
        "v": 1,
        "timeline": {"first_sample_song_sec": 0.0, "tempo": 1.0},
        "capture": {"rate": 48000, "channels": 1},
        "video": {
            "mime": f"video/{container}",
            "firstFrameAtMs": first_frame_ms,
            "cameraOffsetMs": camera_offset_ms,
        },
    }
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["has_camera"] is True
    return tid


async def _wait(c, tid, timeout=180.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()
        if view["status"] in ("done", "error", "none"):
            return view
        await asyncio.sleep(0.2)
    raise AssertionError("render did not finish")


def _brightness(path, t: float) -> float:
    out = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{t}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=16:16,format=gray",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return float(np.frombuffer(out, dtype=np.uint8).mean())


def _probe(path) -> dict:
    out = subprocess.run(
        [
            ffprobe_executable(),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(out)


def _needs_x264():
    skip_without_ffmpeg()
    take_render._encoders_cached = None
    if not take_render.libx264_available():
        pytest.skip("this ffmpeg has no libx264")


# ─── Command builder (pure) ──────────────────────────────────────────────


def test_command_has_the_agreed_encode_settings(tmp_path):
    cmd = build_video_render_cmd(
        [AudioLane(tmp_path / "drums.wav", 1.0, "")],
        tmp_path / "audio.wav",
        0.8,
        tmp_path / "camera.webm",
        0.5,
        3.0,
        "16:9",
        "cover",
        False,
        44100,
    )
    joined = " ".join(cmd)
    for part in ("-c:v libx264", "-crf 20", "-pix_fmt yuv420p", "-r 30", "-c:a aac", "+faststart"):
        assert part in joined
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2:normalize=0,aresample=176400,alimiter" in graph
    assert "[1:a]volume=0.800000[a1]" in graph
    assert "s=1920x1080" in graph and "overlay=eof_action=pass" in graph
    assert cmd[cmd.index("-t") + 1] == "3.000000"


def test_camera_filter_places_and_fits():
    late = camera_filter(1.25, 1080, 1920, "contain", True, "[2:v]")
    assert "setpts=PTS-STARTPTS+(1.250000)/TB" in late
    assert "trim" not in late
    assert "pad=1080:1920" in late and "hflip" in late
    early = camera_filter(-0.5, 1920, 1080, "cover", False, "[2:v]")
    # Shifted first, cut after: the kept frames keep their own timestamps.
    assert "setpts=PTS-STARTPTS+(-0.500000)/TB,trim=start=0," in early
    assert "crop=1920:1080" in early
    assert "hflip" not in early


def test_command_cuts_every_lane_at_the_span_start(tmp_path):
    cmd = build_video_render_cmd(
        [
            AudioLane(tmp_path / "drums.wav", 1.0, "rubberband=pitch=1.1:"),
        ],
        tmp_path / "audio.wav",
        1.0,
        tmp_path / "camera.webm",
        12.5,
        4.0,
        "16:9",
        "cover",
        False,
        44100,
        start_sec=10.0,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    # Cut before the pitch shift, and the take at the same instant.
    assert "[0:a]atrim=start=10.000000,asetpts=PTS-STARTPTS,rubberband=" in graph
    assert "[1:a]atrim=start=10.000000,asetpts=PTS-STARTPTS,volume=" in graph
    # The camera's song time 12.5 s is 2.5 s into the video.
    assert "setpts=PTS-STARTPTS+(2.500000)/TB" in graph
    assert "d=4.000000" in graph and cmd[cmd.index("-t") + 1] == "4.000000"
    whole = build_video_render_cmd(
        [], tmp_path / "audio.wav", 1.0, tmp_path / "c.mp4", 0.0, 3.0, "16:9", "cover", False, 44100
    )
    assert "atrim" not in whole[whole.index("-filter_complex") + 1]


def _sidecar(first, start=None, count_in=None, end=None, nudge_ms=0.0):
    timeline = {"first_sample_song_sec": first}
    if start is not None:
        timeline["start_song_sec"] = start
    if count_in is not None:
        timeline["count_in_sec"] = count_in
    if end is not None:
        timeline["end_song_sec"] = end
    return {"timeline": timeline, "latency": {"user_nudge_ms": nudge_ms}}


@pytest.mark.parametrize(
    ("sidecar", "span", "expected"),
    [
        # From the count-in (10 s minus 2 s) to the last sample.
        (_sidecar(7.9, start=10.0, count_in=2.0, end=20.0), "take", (8.0, 12.0)),
        # A nudge moves the take, so its end moves with it.
        (_sidecar(7.9, start=10.0, count_in=2.0, end=20.0, nudge_ms=500), "take", (8.0, 11.5)),
        # A count-in before song 0 starts the video at 0: no backing before it.
        (_sidecar(-2.1, start=0.0, count_in=2.0, end=30.0), "take", (0.0, 30.0)),
        # Recorded past the end: clamped to the song.
        (_sidecar(150.0, start=152.0, count_in=0.0, end=170.0), "take", (152.0, 13.0)),
        # Stopped at once: still one second of picture.
        (_sidecar(40.0, start=40.0, count_in=0.0, end=40.2), "take", (40.0, 1.0)),
        # An older sidecar with only the first sample: from there to the song end.
        (_sidecar(5.0), "take", (5.0, 160.0)),
        (_sidecar(7.9, start=10.0, count_in=2.0, end=20.0), "song", (0.0, 165.0)),
    ],
)
def test_render_span(sidecar, span, expected):
    start, duration = takes_mod._render_span(sidecar, 165.0, span)
    assert start == pytest.approx(expected[0], abs=1e-9)
    assert duration == pytest.approx(expected[1], abs=1e-9)


# ─── Endpoint ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("container", ["mp4", "webm"])
async def test_render_places_the_camera_and_writes_a_faststart_mp4(
    jobs_dir, client, tmp_path, container
):
    """Camera frame 0 at song 1.0 s (firstFrameAtMs), offset +500 ms (video
    late, subtract): the white clip shows from 0.5 s to 1.5 s, black around
    it, and the file is the song's length."""
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=1000.0, container=container)
        assert (jobs_dir / JOB / "takes" / tid / f"camera.{container}").is_file()
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/render",
            json={"stems": ["drums"], "gains": [1.0], "camera_offset_ms": 500},
        )
        assert r.status_code == 202, r.text
        assert r.json()["status"] in ("queued", "running")
        view = await _wait(c, tid)
        assert view == {
            "status": "done",
            "progress": 1.0,
            "error": None,
            "url": f"/api/jobs/{JOB}/takes/{tid}/render.mp4",
            # What made the video (the suite runs with the CPU kill switch).
            "encoder": "libx264",
            "fallback": False,
            "fallback_reason": None,
        }
        dl = await c.get(view["url"])
    assert dl.status_code == 200 and dl.headers["content-type"] == "video/mp4"
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    info = _probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == (
        "h264",
        "yuv420p",
        1920,
        1080,
    )
    assert video["r_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert abs(float(info["format"]["duration"]) - SONG_SEC) < 0.1
    data = out.read_bytes()
    assert 0 <= data.find(b"moov") < data.find(b"mdat")  # +faststart
    assert _brightness(out, 0.25) < 20
    assert _brightness(out, 1.0) > 200
    assert _brightness(out, 2.5) < 20


async def test_render_uses_the_stored_offset_and_portrait_canvas(jobs_dir, client, tmp_path):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=0.0, camera_offset_ms=0.0)
        r = await c.patch(f"/api/jobs/{JOB}/takes/{tid}", json={"camera_offset_ms": -1500})
        assert r.json()["camera_offset_ms"] == -1500
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json={"canvas": "9:16"})
        assert r.status_code == 202, r.text
        assert (await _wait(c, tid))["status"] == "done"
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    video = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    # Offset -1500 ms: the camera was early, so it is shown 1.5 s later.
    assert _brightness(out, 1.0) < 20
    assert _brightness(out, 2.0) > 200


async def test_render_refusals(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        r = await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})
        plain = r.json()["take_id"]
        await c.put(
            f"/api/jobs/{JOB}/takes/{plain}/parts/audio?seq=0",
            content=np.zeros(4800, dtype="<f4").tobytes(),
        )
        body = {
            "v": 1,
            "timeline": {"first_sample_song_sec": 0.0},
            "capture": {"rate": 48000, "channels": 1},
        }
        await c.post(f"/api/jobs/{JOB}/takes/{plain}/finish", json=body)
        base = f"/api/jobs/{JOB}/takes/{plain}/render"
        assert (await c.post(base, json={})).status_code == 404  # no camera
        assert (await c.get(base)).json()["status"] == "none"
        assert (await c.get(f"{base}.mp4")).status_code == 404
        assert (
            await c.post("/api/jobs/000000000000/takes/t00000000/render", json={})
        ).status_code == 404

        monkeypatch.setattr(takes_mod, "camera_file", lambda folder: folder / "audio.wav")
        bad = [
            {"stems": ["drums"], "gains": []},
            {"stems": ["drums"], "gains": [9]},
            {"stems": ["nope"], "gains": [1]},
            {"stems": [plain], "gains": [1]},
            {"stems": ["vocals", "lead_vocals"], "gains": [1, 1]},
            {"canvas": "4:3"},
            {"fit": "stretch"},
            {"mirror": "yes"},
            {"camera_offset_ms": 6000},
            {"unknown": 1},
            {"fx": "!!!"},
            {"fx": "A" * 9000},
            {"fx": 12},
            {"master": "!!!"},
            {"master": "A" * 300},
            {"master": 12},
            {"master": "eyJ2IjoxfQ"},  # {"v":1}: no target or ceiling
        ]
        monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
        monkeypatch.setattr(takes_mod, "aac_available", lambda: True)
        for payload in bad:
            r = await c.post(base, json=payload)
            assert r.status_code == 422, (payload, r.text)
        r = await c.post(
            base, content=b'{"take_gain": NaN}', headers={"content-type": "application/json"}
        )
        assert r.status_code == 422

        monkeypatch.setattr(takes_mod, "libx264_available", lambda: False)
        r = await c.post(base, json={})
        assert r.status_code == 422 and "libx264" in r.json()["detail"]


async def test_a_running_render_blocks_a_second_one_and_an_edit(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        run = takes_mod._RenderRun(status="running")
        takes_mod._renders[(JOB, tid)] = run
        base = f"/api/jobs/{JOB}/takes/{tid}"
        assert (await c.patch(base, json={"label": "x"})).status_code == 409
        if take_render.libx264_available():
            assert (await c.post(f"{base}/render", json={})).status_code == 409
        r = await c.post(f"{base}/render/cancel")
        assert r.json()["status"] == "none"
        assert run.cancel.is_set()
        assert (await c.delete(base)).status_code == 200


async def test_deleting_a_take_stops_its_running_render(jobs_dir, client, tmp_path):
    """A delete is never refused for a render: the render stops first (the
    fix pass; it used to answer 409 and leave the take)."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        run = takes_mod._RenderRun(status="running")
        takes_mod._renders[(JOB, tid)] = run
        assert (await c.delete(f"/api/jobs/{JOB}/takes/{tid}")).status_code == 200
        assert run.cancel.is_set() and (JOB, tid) not in takes_mod._renders


def test_deleting_a_job_stops_its_renders():
    run = takes_mod._RenderRun(status="running")
    other = takes_mod._RenderRun(status="running")
    takes_mod._renders[(JOB, "t00000001")] = run
    takes_mod._renders[("bbbbbbbbbbbb", "t00000001")] = other
    takes_mod.cancel_job_renders(JOB)
    assert run.cancel.is_set() and not other.cancel.is_set()
    assert (JOB, "t00000001") not in takes_mod._renders
    takes_mod._renders.clear()


async def test_cancel_stops_a_real_render(jobs_dir, client, tmp_path, monkeypatch):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json={})
        assert r.status_code == 202
        await c.post(f"/api/jobs/{JOB}/takes/{tid}/render/cancel")
        await asyncio.sleep(1.5)
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()["status"] in (
            "none",
            "done",  # a 3 s render on a fast machine can win the race
        )
    leftovers = list((jobs_dir / JOB / "takes" / tid).glob(".render.mp4.*"))
    assert leftovers == []


# ─── Sync, measured in the rendered file ─────────────────────────────────


def _flash_camera(path, flash_frame=30, seconds=2.0) -> bytes:
    """Black 30 fps clip with one white frame, as H.264 in MP4."""
    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s=320x240:r=30:d={seconds},"
            f"drawbox=c=white:t=fill:enable='eq(n,{flash_frame})'",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


def _frame_means(path) -> np.ndarray:
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
        timeout=60,
    ).stdout
    return np.frombuffer(out, dtype=np.uint8).reshape(-1, 256).mean(axis=1)


def _decoded_audio(path) -> np.ndarray:
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
        timeout=60,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def _onsets(x: np.ndarray, threshold=0.1) -> list[int]:
    """First sample of each burst over the threshold after 0.3 s of quiet."""
    loud = np.flatnonzero(np.abs(x) > threshold)
    if not len(loud):
        return []
    keep = [int(loud[0])]
    keep += [int(b) for a, b in zip(loud[:-1], loud[1:], strict=True) if b - a > 0.3 * SR]
    return keep


async def _flash_take(c, tmp_path, *, first_frame_ms, timeline=None, beep_at=None) -> str:
    """A take whose camera flashes at its frame 30 (1.0 s into the clip) and
    whose audio, captured at 44.1 kHz from song 0, has a 1 kHz burst at
    `beep_at` seconds."""
    track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
    tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
    audio = np.zeros(int(SR * SONG_SEC), dtype="<f4")
    if beep_at is not None:
        i = int(round(beep_at * SR))
        n = int(0.04 * SR)
        audio[i : i + n] = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(n) / SR)
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=audio.tobytes())
    ).status_code == 200
    cam = _flash_camera(tmp_path / f"flash-{tid}.mp4")
    assert (
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
    ).status_code == 200
    body = {
        "v": 1,
        "timeline": {"first_sample_song_sec": 0.0, "tempo": 1.0, **(timeline or {})},
        "capture": {"rate": SR, "channels": 1},
        "video": {"mime": "video/mp4", "firstFrameAtMs": first_frame_ms},
    }
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
    assert r.status_code == 200, r.text
    return tid


async def _render(c, tid, **body) -> None:
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json=body)
    assert r.status_code == 202, r.text
    assert (await _wait(c, tid))["status"] == "done"


@pytest.mark.parametrize(
    ("first_frame_ms", "offset_ms", "flash_frame"),
    [
        # Camera frame 0 at song -5 ms: the flash (clip 1.000 s) is at song
        # 0.995 s, nearest video frame 30. Cutting the front first and
        # restarting at 0 put it at frame 29 (0.967 s): 33 ms early.
        (-5.0, 0, 30),
        # +120 ms (video late, subtract): 0.875 s, frame 26.25 -> 26.
        (-5.0, 120, 26),
        # -120 ms: 1.115 s, frame 33.45 -> 33.
        (-5.0, -120, 33),
        # Camera starting after song 0: 1.250 s, frame 37.5 -> 38 (or 37).
        (250.0, 0, 38),
    ],
)
async def test_the_flash_lands_on_the_nearest_video_frame(
    jobs_dir, client, tmp_path, first_frame_ms, offset_ms, flash_frame
):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _flash_take(c, tmp_path, first_frame_ms=first_frame_ms)
        await _render(c, tid, camera_offset_ms=offset_ms)
    means = _frame_means(jobs_dir / JOB / "takes" / tid / "render.mp4")
    bright = np.flatnonzero(means > 128)
    assert len(bright) == 1, means
    if flash_frame == 38:
        assert int(bright[0]) in (37, 38)  # exactly half a frame: either is nearest
    else:
        assert int(bright[0]) == flash_frame


async def test_the_take_span_keeps_the_take_on_its_sample(jobs_dir, client, tmp_path):
    """A take from song 1.0 s (count-in 0.5 s from 1.5 s) to 2.6 s: the video
    is 1.0 s to 2.6 s, the take's burst at song 1.8 s is at 0.8 s in the file
    to the sample, and the camera's flash (song 0.995 s, before the span)
    is not in it."""
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _flash_take(
            c,
            tmp_path,
            first_frame_ms=-5.0,
            timeline={"start_song_sec": 1.5, "count_in_sec": 0.5, "end_song_sec": 2.6},
            beep_at=1.8,
        )
        await _render(c, tid)
        folder = jobs_dir / JOB / "takes" / tid
        out = folder / "render.mp4"
        # The playable take has the burst where it was captured (song 1.8 s);
        # the same onset detector runs on both files, so they compare exactly.
        playable, rate = sf.read(folder / "audio.wav", dtype="float32")
        assert rate == SR
        (at,) = _onsets(playable[:, 0])
        # The sine starts at 0 and passes the threshold on its third sample.
        assert at == round(1.8 * SR) + 2
        info = _probe(out)
        assert abs(float(info["format"]["duration"]) - 1.6) < 0.05
        onsets = _onsets(_decoded_audio(out))
        assert len(onsets) == 1
        assert abs(onsets[0] - (at - SR)) <= 1, onsets[0] - (at - SR)
        assert (_frame_means(out) < 20).all()  # flash is before the span

        # The whole song on request: 3 s, burst at 1.8 s, flash at frame 30.
        await _render(c, tid, span="song")
        assert abs(float(_probe(out)["format"]["duration"]) - SONG_SEC) < 0.05
        onsets = _onsets(_decoded_audio(out))
        assert len(onsets) == 1 and abs(onsets[0] - at) <= 1
        assert int(np.argmax(_frame_means(out))) == 30

        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json={"span": "half"})
        assert r.status_code == 422


# ─── Framing, measured in the rendered file ──────────────────────────────


def _chart_camera(path) -> bytes:
    """A 16:9 chart, 640x360: red top-left, green top-right, grey elsewhere."""
    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=640x360:r=30:d=2,"
            "drawbox=x=0:y=0:w=160:h=90:c=red:t=fill,"
            "drawbox=x=480:y=0:w=160:h=90:c=lime:t=fill",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path.read_bytes()


def _rgb_frame(path, t=1.0) -> np.ndarray:
    info = next(s for s in _probe(path)["streams"] if s["codec_type"] == "video")
    out = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            str(t),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return np.frombuffer(out, np.uint8).reshape(info["height"], info["width"], 3).astype(int)


def _colour(px) -> str:
    r, g, b = px
    if max(r, g, b) < 40:
        return "black"
    if r > 150 and g < 100 and b < 100:
        return "red"
    if g > 150 and r < 100 and b < 100:
        return "green"
    return "grey"


@pytest.mark.parametrize(
    ("canvas", "fit", "mirror", "expect"),
    [
        # Not mirrored by default: red stays top-left.
        ("16:9", "cover", False, {(100, 60): "red", (1820, 60): "green", (960, 540): "grey"}),
        # mirror=true flips it.
        ("16:9", "cover", True, {(100, 60): "green", (1820, 60): "red"}),
        # Portrait, contain: the picture is 1080x608 in the middle, black above
        # and below; its corners are the chart's.
        (
            "9:16",
            "contain",
            False,
            {(540, 300): "black", (540, 1600): "black", (60, 700): "red", (1020, 700): "green"},
        ),
        # Portrait, cover: the middle third of the width fills the canvas, so
        # the corner blocks are cropped away and there is no black band.
        ("9:16", "cover", False, {(540, 30): "grey", (30, 30): "grey", (1050, 30): "grey"}),
    ],
)
async def test_framing(jobs_dir, client, tmp_path, canvas, fit, mirror, expect):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
        audio = np.zeros(SR, dtype="<f4").tobytes()
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=audio)
        cam = _chart_camera(tmp_path / "chart.mp4")
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
        body = {
            "v": 1,
            "timeline": {"first_sample_song_sec": 0.0},
            "capture": {"rate": SR, "channels": 1},
            "video": {"mime": "video/mp4", "firstFrameAtMs": 0, "mirrorPreview": True},
        }
        assert (await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)).status_code == 200
        await _render(c, tid, canvas=canvas, fit=fit, mirror=mirror)
    img = _rgb_frame(jobs_dir / JOB / "takes" / tid / "render.mp4")
    assert img.shape[:2] == ((1080, 1920) if canvas == "16:9" else (1920, 1080))
    got = {xy: _colour(img[xy[1], xy[0]]) for xy in expect}
    assert got == expect


@pytest.mark.parametrize(("sent", "kept"), [(1, True), (2, False), (None, True)])
async def test_a_take_missing_camera_parts_keeps_its_sound_only(
    jobs_dir, client, tmp_path, sent, kept
):
    """One camera part on the server. The video block says how many were
    sent: when that is more, the end of the recording is missing, and the
    take is kept without its video instead of with a short one."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        track = (await c.post(f"/api/jobs/{JOB}/tracks", json={})).json()["id"]
        tid = (await c.post(f"/api/jobs/{JOB}/takes", json={"track_id": track})).json()["take_id"]
        audio = np.zeros(4800, dtype="<f4").tobytes()
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/audio?seq=0", content=audio)
        cam = _white_camera(tmp_path / "cam.mp4")
        await c.put(f"/api/jobs/{JOB}/takes/{tid}/parts/camera?seq=0", content=cam)
        video = {"mime": "video/mp4", "firstFrameAtMs": 0}
        if sent is not None:
            video["parts"] = sent
        body = {
            "v": 1,
            "timeline": {"first_sample_song_sec": 0.0},
            "capture": {"rate": 48000, "channels": 1},
            "video": video,
        }
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/finish", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["has_camera"] is kept
    folder = jobs_dir / JOB / "takes" / tid
    assert (folder / "camera.mp4").is_file() is kept
    assert not (folder / "camera.part").exists()
    assert ("video" in json.loads((folder / "take.json").read_text())) is kept


def _audio_rms(path) -> float:
    pcm = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-f",
            "f32le",
            "-ac",
            "1",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    x = np.frombuffer(pcm, dtype="<f4")
    return float(np.sqrt(np.mean(x[int(0.5 * SR) : int(2.5 * SR)] ** 2)))


async def test_render_applies_the_lane_and_master_eq(jobs_dir, client, tmp_path):
    """The Make video render takes the studio EQ: a 48 dB/oct high cut at 100
    Hz on the drums lane (a 440 Hz tone) takes it down by about 50 dB in the
    video's audio; the same cut on the master does the same; none: unchanged."""
    _needs_x264()
    _make_job(jobs_dir)
    cut = {"id": "eq", "out": 0, "b": [["highcut", 100, 0, 0.707, 48]]}

    def wire(body):
        raw = json.dumps({"v": 1, **body}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    levels = {}
    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=0.0)
        for label, body in (
            ("none", {}),
            ("lane", {"fx": wire({"lanes": {"drums": [cut]}})}),
            ("master", {"fx": wire({"lanes": {}, "master": [cut]})}),
        ):
            r = await c.post(
                f"/api/jobs/{JOB}/takes/{tid}/render",
                json={"stems": ["drums"], "gains": [1.0], "span": "song", **body},
            )
            assert r.status_code == 202, r.text
            assert (await _wait(c, tid))["status"] == "done"
            levels[label] = _audio_rms(jobs_dir / JOB / "takes" / tid / "render.mp4")
    assert levels["none"] > 0.05
    assert 20 * np.log10(levels["lane"] / levels["none"]) < -40
    assert 20 * np.log10(levels["master"] / levels["none"]) < -40


async def test_render_applies_the_master(jobs_dir, client, tmp_path, monkeypatch):
    """The Make video render takes the master: measured on the whole song
    with the take, its audio lands on the target and under the ceiling."""
    _needs_x264()
    _make_job(jobs_dir)
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "_MASTER_PLAN_DIR", tmp_path / "master")
    from app.pipeline.mastering import MasterSettings, encode_master_param

    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=0.0)
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/render",
            json={
                "stems": ["drums"],
                "gains": [1.0],
                "span": "song",
                "master": encode_master_param(MasterSettings(-20.0, -2.0)),
            },
        )
        assert r.status_code == 202, r.text
        assert (await _wait(c, tid))["status"] == "done"
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    from app.pipeline.mastering import measure_file

    got = await measure_file(str(out))
    assert got.i == pytest.approx(-20.0, abs=0.3)
    assert got.tp <= -2.0


def test_command_puts_the_eq_in_the_lanes_take_and_master(tmp_path):
    cmd = build_video_render_cmd(
        [
            AudioLane(
                tmp_path / "drums.wav",
                1.0,
                "",
                "biquad=b0=1.0:b1=0.0:b2=0.0:a0=1.0:a1=0.0:a2=0.0:r=f64,",
            )
        ],
        tmp_path / "audio.wav",
        1.0,
        tmp_path / "camera.webm",
        0.0,
        3.0,
        "16:9",
        "cover",
        False,
        44100,
        take_fx="volume=0.5,",
        master_fx="volume=0.25,",
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]biquad=" in graph
    assert "[1:a]volume=0.5,volume=1.000000[a1]" in graph
    assert "normalize=0,volume=0.25,aresample" in graph


# ─── Filmstrip (the frames a video track draws along its lane) ──────────────


def _test_camera(path, seconds, container):
    """A moving test pattern; WebM through a pipe as MediaRecorder writes it,
    so it has no duration in its header."""
    src = ["-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={seconds}"]
    if container == "webm":
        out = subprocess.run(
            [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                *src,
                "-c:v",
                "libvpx",
                "-b:v",
                "300k",
                "-f",
                "webm",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout
        path.write_bytes(out)
    else:
        subprocess.run(
            [ffmpeg_executable(), "-nostdin", "-loglevel", "error", "-y", *src, str(path)],
            check=True,
        )


@pytest.mark.parametrize("container", ["mp4", "webm"])
def test_filmstrip_has_one_frame_a_second_at_the_cameras_aspect(tmp_path, container):
    skip_without_ffmpeg()
    camera = tmp_path / f"camera.{container}"
    try:
        _test_camera(camera, 5, container)
    except subprocess.CalledProcessError:
        pytest.skip(f"this ffmpeg cannot write {container}")
    meta = takes_mod.build_filmstrip(camera, tmp_path)
    assert meta["interval"] == 1.0
    assert meta["count"] == 5
    assert meta["frame_height"] == 72 and meta["frame_width"] == 96  # 4:3
    assert abs(meta["duration"] - 5.0) < 0.2
    probe = json.loads(
        subprocess.run(
            [
                ffprobe_executable(),
                "-v",
                "error",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(tmp_path / "filmstrip.jpg"),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )["streams"][0]
    assert (probe["width"], probe["height"]) == (5 * 96, 72)
    # Built once: asked again, it is read back, not rebuilt.
    before = (tmp_path / "filmstrip.jpg").stat().st_mtime_ns
    assert takes_mod.build_filmstrip(camera, tmp_path) == meta
    assert (tmp_path / "filmstrip.jpg").stat().st_mtime_ns == before


def test_filmstrip_of_a_long_take_stays_within_the_frame_cap(tmp_path, monkeypatch):
    skip_without_ffmpeg()
    monkeypatch.setattr(takes_mod, "FILMSTRIP_MAX_FRAMES", 4)
    camera = tmp_path / "camera.mp4"
    _test_camera(camera, 10, "mp4")
    meta = takes_mod.build_filmstrip(camera, tmp_path)
    assert meta["count"] == 4 and meta["interval"] == 2.5


def test_take_entry_says_where_the_camera_starts_in_the_song():
    from app.core.takes import take_entry

    sidecar = {
        "id": "t00000001",
        "timeline": {"first_sample_song_sec": 10.0},
        "latency": {"user_nudge_ms": 100.0},
        "video": {"firstFrameAtMs": 50.0, "cameraOffsetMs": -20.0},
    }
    entry = take_entry(JOB, sidecar, True)
    # 10.0 - 0.1 (nudge) + 0.05 (first frame) + 0.02 (offset -20 ms)
    assert entry["camera_start_sec"] == pytest.approx(9.97)
    assert take_entry(JOB, sidecar, False)["camera_start_sec"] is None


def test_filmstrip_of_a_take_ending_between_seconds_has_no_black_frame(tmp_path):
    """6.5 s of camera: six whole seconds, six frames. A seventh, for the last
    half second, came out black (ffmpeg gives no frame for it and the tile
    filter pads the gap), and a zoomed-out lane showed only that one."""
    skip_without_ffmpeg()
    camera = tmp_path / "camera.mp4"
    _test_camera(camera, 6.5, "mp4")
    meta = takes_mod.build_filmstrip(camera, tmp_path)
    assert meta["count"] == 6
    w, h = meta["frame_width"], meta["frame_height"]
    raw = subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-i",
            str(tmp_path / "filmstrip.jpg"),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    img = np.frombuffer(raw, dtype=np.uint8).reshape(h, w * meta["count"])
    for i in range(meta["count"]):
        assert img[:, i * w : (i + 1) * w].mean() > 20, f"frame {i} is black"
