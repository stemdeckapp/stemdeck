"""Streamer Mode's export: the take render with the recorded canvas as the
picture (fit "fill"), the output size and frame rate, the trim range, the
mix on or off and the click; and no cover endpoint (nothing draws the
album art).

The renders run the real ffmpeg and are probed, as in test_takes_render.
"""

from __future__ import annotations

import asyncio
import subprocess

import httpx
import numpy as np
import pytest
import soundfile as sf
from pydantic import ValidationError

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.config import ffmpeg_executable
from app.core.registry import _jobs
from app.pipeline.take_render import (
    CANVAS_SIZES,
    AudioLane,
    build_video_render_cmd,
    camera_filter,
    canvas_size,
)
from tests.ffmpeg_probe import skip_without_ffmpeg
from tests.test_takes_render import (
    JOB,
    SR,
    _audio_rms,
    _make_job,
    _needs_x264,
    _probe,
    _sidecar,
    _take_with_camera,
    _wait,
)


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


# ─── Sizes and the command (pure) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("canvas", "size", "expected"),
    [
        ("16:9", "720", (1280, 720)),
        ("16:9", "1080", (1920, 1080)),
        ("9:16", "720", (720, 1280)),
        ("9:16", "1080", (1080, 1920)),
        ("1:1", "720", (720, 720)),
        ("1:1", "1080", (1080, 1080)),
    ],
)
def test_canvas_sizes(canvas, size, expected):
    assert canvas_size(canvas, size) == expected
    if size == "1080" and canvas in CANVAS_SIZES:
        assert CANVAS_SIZES[canvas] == expected


def test_command_takes_the_size_and_frame_rate(tmp_path):
    cmd = build_video_render_cmd(
        [AudioLane(tmp_path / "drums.wav", 1.0, "")],
        tmp_path / "audio.wav",
        1.0,
        tmp_path / "camera.webm",
        0.0,
        10.0,
        "9:16",
        "fill",
        False,
        48000,
        size="720",
        fps=60,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "color=c=black:s=720x1280:r=60:" in graph
    assert "fps=60,format=yuv420p" in graph
    assert cmd[cmd.index("-r") + 1] == "60"
    # A composed scene is only resized: nothing cut, nothing padded.
    assert "scale=720:1280," in graph and "crop=" not in graph and "pad=" not in graph
    assert "-movflags" in cmd and "+faststart" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_command_defaults_are_the_camera_takes(tmp_path):
    cmd = build_video_render_cmd(
        [],
        tmp_path / "audio.wav",
        1.0,
        tmp_path / "camera.mp4",
        0.0,
        5.0,
        "16:9",
        "cover",
        False,
        48000,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "s=1920x1080:r=30:" in graph and cmd[cmd.index("-r") + 1] == "30"


def test_command_refuses_another_frame_rate(tmp_path):
    with pytest.raises(ValueError):
        build_video_render_cmd(
            [],
            tmp_path / "a.wav",
            1.0,
            tmp_path / "c.mp4",
            0.0,
            1.0,
            "16:9",
            "fill",
            False,
            48000,
            fps=25,
        )


def test_fill_scales_without_keeping_the_aspect():
    f = camera_filter(0.0, 1080, 1080, "fill", False, "[1:v]")
    assert "scale=1080:1080," in f and "force_original_aspect_ratio" not in f


@pytest.mark.parametrize(
    ("range_in", "range_out", "expected"),
    [
        # Inside the take (10 s to 20 s): the range itself.
        (12.0, 15.0, (12.0, 3.0)),
        # Wider than the take: cut to what was recorded.
        (0.0, 60.0, (10.0, 10.0)),
        # Too short: one second at least.
        (12.0, 12.2, (12.0, 1.0)),
        # Starting after the take ended: the last second of the take.
        (25.0, 30.0, (19.0, 1.0)),
    ],
)
def test_range_span_is_the_trim_inside_the_take(range_in, range_out, expected):
    sidecar = _sidecar(10.0, start=10.0, count_in=0.0, end=20.0)
    start, duration = takes_mod._render_span(sidecar, 165.0, "range", range_in, range_out)
    assert start == pytest.approx(expected[0], abs=1e-9)
    assert duration == pytest.approx(expected[1], abs=1e-9)


# ─── Endpoint ────────────────────────────────────────────────────────────


async def test_streamer_render_refusals(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
    monkeypatch.setattr(takes_mod, "aac_available", lambda: True)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}/render"
        bad = [
            {"size": "480"},
            {"size": 1080},
            {"fps": 25},
            {"fps": "30"},
            {"canvas": "4:5"},
            {"fit": "stretch"},
            {"span": "range"},  # no range given
            {"span": "range", "range_in": 2.0, "range_out": 1.0},
            {"span": "range", "range_in": -1.0, "range_out": 1.0},
            {"click": "yes"},
            {"range_in": float("inf")},
        ]
        for payload in bad:
            r = await c.post(
                base, content=_json(payload), headers={"content-type": "application/json"}
            )
            assert r.status_code == 422, (payload, r.text)
        # No libx264: said so, for the raw-recording fallback.
        monkeypatch.setattr(takes_mod, "libx264_available", lambda: False)
        r = await c.post(base, json={"canvas": "1:1", "fit": "fill"})
        assert r.status_code == 422 and "libx264" in r.json()["detail"]


async def test_streamer_render_refuses_4k(jobs_dir, client, tmp_path, monkeypatch):
    """4K is not offered (maintainer decision: a 1080p camera only gets
    bigger): a crafted 2160 request gets a 422 that names the sizes; 720 and
    1080, at 60 fps too, are accepted."""
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
    monkeypatch.setattr(takes_mod, "aac_available", lambda: True)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        base = f"/api/jobs/{JOB}/takes/{tid}/render"
        for fps in (30, 60):
            r = await c.post(
                base, json={"canvas": "16:9", "fit": "fill", "size": "2160", "fps": fps}
            )
            assert r.status_code == 422
            detail = r.json()["detail"]
            assert detail.startswith("size:") and "'720' or '1080'" in detail, detail
    with pytest.raises(ValidationError):
        takes_mod.RenderBody(size="2160", fps=30)
    assert takes_mod.RenderBody(size="1080", fps=60).fps == 60
    assert takes_mod.RenderBody(size="720", fps=60).size == "720"
    with pytest.raises(KeyError):
        canvas_size("16:9", "2160")


def _json(payload: dict) -> bytes:
    import json

    return json.dumps(payload, allow_nan=True).encode()


async def test_streamer_render_rejects_traversal(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        for path in (
            "/api/jobs/..%2F..%2Fetc/takes/t00000000/render",
            f"/api/jobs/{JOB}/takes/..%2F..%2Fetc/render",
            "/api/jobs/ABC/takes/t00000000/render",
        ):
            r = await c.post(path, json={"fit": "fill"})
            # Dots are collapsed or split into extra segments before routing
            # (405, as in test_takes_api); malformed ids reach the route and
            # are refused (404). Never 2xx and never 500.
            assert r.status_code in ((404, 405) if ".." in path else (404,)), (path, r.status_code)


@pytest.mark.parametrize(
    ("canvas", "size", "fps", "dims"),
    [
        ("9:16", "720", 24, (720, 1280)),
        ("1:1", "720", 60, (720, 720)),
        ("16:9", "1080", 30, (1920, 1080)),
    ],
)
async def test_streamer_render_size_rate_and_trim(
    jobs_dir, client, tmp_path, canvas, size, fps, dims
):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path, first_frame_ms=0.0)
        body = {
            "stems": ["drums"],
            "gains": [1.0],
            "canvas": canvas,
            "fit": "fill",
            "size": size,
            "fps": fps,
            "span": "range",
            "range_in": 0.5,
            "range_out": 2.0,
        }
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json=body)
        assert r.status_code == 202, r.text
        view = await _wait(c, tid)
        assert view["status"] == "done", view
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    info = _probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert (video["width"], video["height"]) == dims
    assert video["r_frame_rate"] == f"{fps}/1"
    assert audio["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(1.5, abs=0.1)
    assert info["format"]["format_name"].startswith("mov,mp4")


async def _render_rms(c, jobs, tid, **extra) -> float:
    body = {"canvas": "16:9", "fit": "fill", "size": "720", "span": "song", **extra}
    r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json=body)
    assert r.status_code == 202, r.text
    view = await _wait(c, tid)
    assert view["status"] == "done", view
    return _audio_rms(jobs / JOB / "takes" / tid / "render.mp4")


async def test_mix_on_has_the_stems_mix_off_has_only_the_take(jobs_dir, client, tmp_path):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)  # the take itself is silence
        mixed = await _render_rms(c, jobs_dir, tid, stems=["drums", "vocals"], gains=[1.0, 1.0])
        alone = await _render_rms(c, jobs_dir, tid, stems=[], gains=[])
    assert mixed > 0.05
    assert alone < 0.005


async def test_click_adds_a_lane_and_is_skipped_without_a_grid(
    jobs_dir, client, tmp_path, monkeypatch
):
    _needs_x264()
    _make_job(jobs_dir)
    click = tmp_path / "click.wav"
    t = np.arange(int(SR * 3.0)) / SR
    tone = 0.3 * np.sin(2 * np.pi * 1000 * t)
    sf.write(click, np.stack([tone, tone], 1), SR, subtype="PCM_16")
    calls = []

    def fake_click(job_id, enabled, multiplier, accent, gain, *rest, **kw):
        calls.append((job_id, enabled, multiplier, accent, gain))
        return stems_mod._ClickLane(click, gain, 0.0, False)

    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        monkeypatch.setattr(stems_mod, "_click_lane", fake_click)
        with_click = await _render_rms(c, jobs_dir, tid, stems=[], gains=[], click=True)
        monkeypatch.setattr(stems_mod, "_click_lane", lambda *a, **k: None)
        no_grid = await _render_rms(c, jobs_dir, tid, stems=[], gains=[], click=True)
        without = await _render_rms(c, jobs_dir, tid, stems=[], gains=[], click=False)
    assert calls and calls[0][:2] == (JOB, True)
    assert with_click > 0.05
    assert no_grid < 0.005 and without < 0.005


async def test_the_rendered_click_is_at_the_live_level_on_each_channel(
    jobs_dir, client, tmp_path, monkeypatch
):
    """The click lane is mono, as rendered; each channel of the video carries
    it at its volume, as the app plays it (#833), not 3 dB lower."""
    _needs_x264()
    _make_job(jobs_dir)
    click = tmp_path / "click.wav"
    t = np.arange(int(SR * 3.0)) / SR
    tone = 0.3 * np.sin(2 * np.pi * 1000 * t)
    sf.write(click, tone, SR, subtype="PCM_16")
    monkeypatch.setattr(
        stems_mod,
        "_click_lane",
        lambda j, e, m, a, gain, *r, **k: stems_mod._ClickLane(click, gain, 0.0, False),
    )
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        body = {"canvas": "16:9", "fit": "fill", "size": "720", "span": "song"}
        r = await c.post(
            f"/api/jobs/{JOB}/takes/{tid}/render",
            json={**body, "stems": ["drums"], "gains": [0.0], "click": True},
        )
        assert r.status_code == 202, r.text
        assert (await _wait(c, tid))["status"] == "done"
    out = jobs_dir / JOB / "takes" / tid / "render.mp4"
    pcm = subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-vn",
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    x = np.frombuffer(pcm, dtype="<f4").reshape(-1, 2)[int(0.5 * SR) : int(2.5 * SR)]
    live = 0.3 / np.sqrt(2) * takes_mod.RENDER_CLICK_GAIN
    for ch in (0, 1):
        got = float(np.sqrt(np.mean(x[:, ch] ** 2)))
        assert 20 * np.log10(got / live) == pytest.approx(0.0, abs=0.2), (ch, got, live)
    audio = next(s for s in _probe(out)["streams"] if s["codec_type"] == "audio")
    assert audio["channels"] == 2


async def test_cancel_stops_a_streamer_render(jobs_dir, client, tmp_path):
    _needs_x264()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        body = {"canvas": "16:9", "fit": "fill", "size": "1080", "fps": 60, "span": "song"}
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json=body)
        assert r.status_code == 202
        await c.post(f"/api/jobs/{JOB}/takes/{tid}/render/cancel")
        await asyncio.sleep(1.5)
        status = (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()["status"]
        assert status in ("none", "done")
    assert list((jobs_dir / JOB / "takes" / tid).glob(".render.mp4.*")) == []


# ─── No cover endpoint ───────────────────────────────────────────────────


async def test_no_cover_endpoint(jobs_dir):
    """Performance draws no album art any more (its Track Info layer is gone),
    so nothing fetches a song's remote picture: the route is not there."""
    job = _make_job(jobs_dir)
    job.thumbnail = "https://i.ytimg.com/vi/x/hqdefault.jpg"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    ) as c:
        r = await c.get(f"/api/jobs/{JOB}/cover")
    assert r.status_code in (404, 405)
    assert not (jobs_dir / JOB / "cover.img").exists()
    _jobs.pop(JOB, None)


@pytest.mark.parametrize(
    ("asked", "served"),
    [
        ("Song - Artist (Streamer).mp4", "Song - Artist (Streamer).mp4"),
        ("Nirvana: Come? (Streamer).mp4", "Nirvana Come (Streamer).mp4"),
        ("../../etc/passwd", None),
        ("clip.webm", None),
        (".mp4", None),
        ("a" * 300 + ".mp4", None),
    ],
)
def test_requested_download_name_is_made_safe(asked, served):
    assert takes_mod._requested_name(asked) == served


async def test_render_file_takes_the_asked_name(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        folder = jobs_dir / JOB / "takes" / tid
        (folder / "render.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        base = f"/api/jobs/{JOB}/takes/{tid}/render.mp4"
        r = await c.get(base, params={"name": "Song - Artist (Streamer).mp4"})
        assert r.status_code == 200
        assert "Streamer" in r.headers["content-disposition"]
        r = await c.get(base, params={"name": "../../evil.sh"})
        assert r.status_code == 200 and "evil" not in r.headers["content-disposition"]


# ─── Security follow-ups ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("asked", "served"),
    [
        ("\u202egpj.mp4", "gpj.mp4"),
        ("Song\u200b - Artist (Streamer).mp4", "Song - Artist (Streamer).mp4"),
        (" ..Song (Streamer).mp4", "Song (Streamer).mp4"),
        ("Song (Streamer).mp4. ", "Song (Streamer).mp4"),
        ("a\r\nContent-Type: text/html\r\n.mp4", "aContent-Type text html.mp4"),
        ('x"; filename="evil.sh.mp4', "x ; filename= evil.sh.mp4"),
    ],
)
def test_requested_name_drops_format_and_control_characters(asked, served):
    assert takes_mod._requested_name(asked) == served


def test_no_size_sets_a_preset(tmp_path):
    def cmd(size):
        return build_video_render_cmd(
            [],
            tmp_path / "a.wav",
            1.0,
            tmp_path / "c.mp4",
            0.0,
            1.0,
            "16:9",
            "fill",
            False,
            48000,
            size=size,
        )

    for size in ("720", "1080"):
        assert "-preset" not in cmd(size)


async def test_two_quick_render_posts_start_one(jobs_dir, client, tmp_path, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
    monkeypatch.setattr(takes_mod, "aac_available", lambda: True)
    started = []
    real_prepare = takes_mod._prepare_render

    async def slow_prepare(*a, **k):
        await asyncio.sleep(0.3)
        return await real_prepare(*a, **k)

    async def fake_task(run, job, cmd, dest, duration, retry=None):
        started.append(cmd)
        run.status = "running"

    monkeypatch.setattr(takes_mod, "_prepare_render", slow_prepare)
    monkeypatch.setattr(takes_mod, "_render_task", fake_task)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        url = f"/api/jobs/{JOB}/takes/{tid}/render"
        a, b = await asyncio.gather(c.post(url, json={}), c.post(url, json={}))
        assert sorted([a.status_code, b.status_code]) == [202, 409]
        await asyncio.sleep(0.1)
        assert len(started) == 1
        # Cancel reaches a run that is still queued.
        r = await c.post(f"{url}/cancel")
        assert r.status_code == 200 and (JOB, tid) not in takes_mod._renders
    takes_mod._renders.clear()


async def test_a_failed_render_start_leaves_nothing_registered(
    jobs_dir, client, tmp_path, monkeypatch
):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    monkeypatch.setattr(takes_mod, "libx264_available", lambda: True)
    monkeypatch.setattr(takes_mod, "aac_available", lambda: True)

    async def broken(*a, **k):
        raise takes_mod.HTTPException(status_code=422, detail="this ffmpeg build cannot transpose")

    monkeypatch.setattr(takes_mod, "_prepare_render", broken)
    async with client as c:
        tid = await _take_with_camera(c, tmp_path)
        r = await c.post(f"/api/jobs/{JOB}/takes/{tid}/render", json={})
        assert r.status_code == 422
        assert (JOB, tid) not in takes_mod._renders
        assert (await c.get(f"/api/jobs/{JOB}/takes/{tid}/render")).json()["status"] == "none"
