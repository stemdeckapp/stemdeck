"""The master in the exports: master= on mixdown.{ext}, video.mp4, the Make
video render, and GET mixdown/loudness.

- Malformed or hostile master values are a 422 with a safe message before
  any ffmpeg runs, on every route that takes one.
- The loudness route follows the endpoint rules (404 for a bad or unknown
  job and a traversal attempt, 422 for bad input) and is answered from the
  plan cache the second time.
- The master is part of the render cache key; no master keeps the graph and
  the bytes of an export without one.
- A region export gets the whole song's gain; renders are deterministic.
- A lossy master is decoded and re-rendered while it is over its ceiling.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import subprocess
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.core.config import ffmpeg_executable
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import mastering
from app.pipeline.mastering import Loudness, MasterSettings, encode_master_param
from tests.ffmpeg_probe import skip_without_ffmpeg, skip_without_vorbis

JOB = "abcdef0f0f02"
SR = 44100
STREAMING = encode_master_param(MasterSettings(-14.0, -1.0))


def raw_param(body) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    monkeypatch.setattr(stems_mod, "_MASTER_PLAN_DIR", tmp_path / "cache" / "master")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def no_ffmpeg(monkeypatch):
    """Fails the test if anything would measure or render."""

    async def refuse(*args, **kwargs):
        raise AssertionError("ffmpeg ran before validation")

    monkeypatch.setattr(stems_mod, "run_plan", refuse)
    monkeypatch.setattr(stems_mod, "_render_to_file", refuse)


def _job(jobs_dir, seconds: float = 4.0, video: bool = False) -> Path:
    """Two lanes, a melody and noise, summing to about -17 LUFS."""
    n = int(SR * seconds)
    job = Job(id=JOB, status="done", title="Master", duration_sec=seconds)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    t = np.arange(n) / SR
    rng = np.random.default_rng(1)
    lanes = {
        "vocals": 0.2 * np.sin(2 * np.pi * 440 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t)),
        "bass": 0.05 * rng.standard_normal(n),
    }
    for name, x in lanes.items():
        sf.write(stems / f"{name}.wav", np.stack([x, x], 1), SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    if video:
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
                f"color=c=black:s=64x64:d={seconds}:r=10",
                "-c:v",
                "mpeg4",
                "-an",
                str(jobs_dir / JOB / "video.mp4"),
            ],
            check=True,
            timeout=60,
        )
    return stems


Q = "stems=vocals,bass&gains=1,1"

BAD = [
    "%%%",
    "a" * (mastering.MASTER_PARAM_MAX_CHARS + 1),
    raw_param({"v": 1, "target": -14, "ceiling": -1, "x": "; rm -rf /"}),
    raw_param({"v": 1, "target": "volume=10", "ceiling": -1}),
    raw_param('{"v": 1, "target": NaN, "ceiling": -1}'),
    raw_param({"v": 1, "target": -14, "ceiling": 3}),
    raw_param({"v": 1, "target": -14, "ceiling": -1, "comp": 9}),
    raw_param({"v": 9, "target": -14, "ceiling": -1}),
    raw_param("[]"),
]


@pytest.mark.parametrize("value", BAD)
async def test_a_malformed_master_is_a_422_on_mixdown(jobs_dir, client, no_ffmpeg, value):
    _job(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}&master={value}")
    assert r.status_code == 422
    body = r.text
    assert "Traceback" not in body and "rm -rf" not in body


@pytest.mark.parametrize("value", BAD[:4])
async def test_a_malformed_master_is_a_422_on_video_and_loudness(
    jobs_dir, client, no_ffmpeg, value
):
    _job(jobs_dir)
    (jobs_dir / JOB / "video.mp4").write_bytes(b"not parsed before validation")
    async with client as c:
        v = await c.get(f"/api/jobs/{JOB}/video.mp4?{Q}&master={value}")
        lo = await c.get(f"/api/jobs/{JOB}/mixdown/loudness?{Q}&master={value}")
    assert v.status_code == 422
    assert lo.status_code == 422


async def test_loudness_route_rejects_bad_jobs_and_missing_master(jobs_dir, client, no_ffmpeg):
    _job(jobs_dir)
    async with client as c:
        bad_id = await c.get(f"/api/jobs/NOTAJOB/mixdown/loudness?{Q}&master={STREAMING}")
        unknown = await c.get(f"/api/jobs/abcdef999999/mixdown/loudness?{Q}&master={STREAMING}")
        traversal = await c.get(f"/api/jobs/..%2F..%2Fetc/mixdown/loudness?{Q}&master={STREAMING}")
        missing = await c.get(f"/api/jobs/{JOB}/mixdown/loudness?{Q}")
        empty = await c.get(f"/api/jobs/{JOB}/mixdown/loudness?{Q}&master=")
        lane = await c.get(
            f"/api/jobs/{JOB}/mixdown/loudness?stems=..%2Fsecret&gains=1&master={STREAMING}"
        )
    assert bad_id.status_code == 404
    assert unknown.status_code == 404
    assert traversal.status_code == 404
    assert missing.status_code == 422
    assert empty.status_code == 422
    assert lane.status_code == 422


async def test_loudness_route_returns_the_plan_and_caches_it(jobs_dir, client, monkeypatch):
    skip_without_ffmpeg()
    _job(jobs_dir)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown/loudness?{Q}&master={STREAMING}")
        assert r.status_code == 200, r.text
        plan = r.json()

        async def refuse(*args, **kwargs):
            raise AssertionError("measured twice")

        monkeypatch.setattr(stems_mod, "run_plan", refuse)
        again = await c.get(f"/api/jobs/{JOB}/mixdown/loudness?{Q}&master={STREAMING}")
        # The export with the same mix and master uses that plan as well.
        wav = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}&master={STREAMING}")
    assert again.json() == plan
    assert set(plan) >= {"pre", "post", "gain_db", "target", "ceiling", "reached", "comp_ratio"}
    assert plan["target"] == -14.0 and plan["ceiling"] == -1.0
    assert plan["post"]["i"] == pytest.approx(-14.0, abs=0.1)
    assert plan["post"]["tp"] <= -1.0
    assert plan["gain_db"] == pytest.approx(-14.0 - plan["pre"]["i"], abs=0.2)
    assert wav.status_code == 200
    assert wav.headers["x-stemdeck-master-gain"] == f"{plan['gain_db']:.2f}"


def test_the_master_is_in_the_cache_key():
    base = ("j", "wav", ["vocals"], [1.0], None, None, None, [0], [], "")
    off = stems_mod._mixdown_cache_key(*base)
    on = stems_mod._mixdown_cache_key(*base, "m:volume=2.0,alimiter")
    other = stems_mod._mixdown_cache_key(*base, "m:volume=2.1,alimiter")
    assert len({off, on, other}) == 3
    assert stems_mod._RENDER_CACHE_VERSION == "6"


async def test_no_master_renders_the_same_bytes_as_before(jobs_dir, client, monkeypatch):
    """No master and an empty master= are the export without a master: the
    graph ends in the export limiter, and the bytes are the same."""
    skip_without_ffmpeg()
    _job(jobs_dir)
    seen: list[list[str]] = []
    real = stems_mod._render_to_file

    async def spy(cmd, *a, **k):
        seen.append(cmd)
        return await real(cmd, *a, **k)

    monkeypatch.setattr(stems_mod, "_render_to_file", spy)
    async with client as c:
        a = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}")
        cache = stems_mod._MIXDOWN_CACHE_DIR
        for f in cache.iterdir():
            f.unlink()
        b = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}&master=")
    assert a.content == b.content
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert graph.endswith(f"amix=inputs=2:normalize=0,{stems_mod.export_limiter(False)}[mix]")
    assert "x-stemdeck-master-gain" not in a.headers


async def test_a_master_render_is_deterministic(jobs_dir, client):
    skip_without_ffmpeg()
    _job(jobs_dir)
    async with client as c:
        a = await c.get(f"/api/jobs/{JOB}/mixdown.flac?{Q}&master={STREAMING}")
        for f in stems_mod._MIXDOWN_CACHE_DIR.iterdir():
            f.unlink()
        for f in stems_mod._MASTER_PLAN_DIR.iterdir():
            f.unlink()
        b = await c.get(f"/api/jobs/{JOB}/mixdown.flac?{Q}&master={STREAMING}")
    assert a.status_code == b.status_code == 200
    xa = sf.read(io.BytesIO(a.content))[0]
    xb = sf.read(io.BytesIO(b.content))[0]
    assert np.array_equal(xa, xb)


async def test_a_region_gets_the_whole_songs_gain(jobs_dir, client):
    skip_without_ffmpeg()
    _job(jobs_dir, seconds=6.0)
    async with client as c:
        full = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}&master={STREAMING}")
        region = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{Q}&start=1&end=3&master={STREAMING}")
    assert full.status_code == region.status_code == 200
    assert region.headers["x-stemdeck-master-gain"] == full.headers["x-stemdeck-master-gain"]
    xf = sf.read(io.BytesIO(full.content))[0]
    xr = sf.read(io.BytesIO(region.content))[0]
    assert len(xr) == 2 * SR
    # Same samples as the full master over that span (the limiter is idle on
    # this material), so the excerpt is what the export will sound like.
    assert np.max(np.abs(xr[SR // 2 : -SR // 2] - xf[SR + SR // 2 : 3 * SR - SR // 2])) < 2e-4


@pytest.mark.parametrize("path", ["mixdown.mp3", "mixdown.ogg", "video.mp4"])
async def test_a_mastered_lossy_export_is_decoded_and_under_its_ceiling(jobs_dir, client, path):
    skip_without_ffmpeg()
    if path.endswith(".ogg"):
        skip_without_vorbis()
    _job(jobs_dir, video=True)
    loud = encode_master_param(MasterSettings(-9.0, -1.0, 0.5))
    async with client as c:
        url = f"/api/jobs/{JOB}/{path}?{Q}&master={loud}"
        r = await c.get(url)
        assert r.status_code == 200, r.text
        post = r.headers["x-stemdeck-loudness-post"]
        tp = float(post.split("tp=")[1])
        assert tp <= -1.0, (url, post)
        i = float(post.split(";")[0].split("=")[1])
        assert i == pytest.approx(-9.0, abs=0.6)


def test_lossy_retry_trims_or_switches_the_aac_coder(monkeypatch, tmp_path):
    """Over the ceiling: an MP3 is rendered again with its output trimmed by
    the overshoot; an MP4 first switches to the alternative AAC coder."""
    calls: list[tuple[float, bool]] = []
    readings = iter([Loudness(-9.0, -0.4), Loudness(-9.2, -1.05)])

    async def fake_render(cmd, suffix, context="", cache_path=None):
        p = tmp_path / f"r{len(calls)}{suffix}"
        p.write_bytes(b"x")
        return p

    async def fake_measure(path):
        return next(readings)

    def build(trim, alt):
        calls.append((round(trim, 3), alt))
        return ["ffmpeg"]

    monkeypatch.setattr(stems_mod, "_render_to_file", fake_render)
    monkeypatch.setattr(stems_mod, "measure_file", fake_measure)
    path, out = asyncio.run(stems_mod._render_master_verified(build, ".mp3", "t", -1.0, None, None))
    assert calls == [(0.0, False), (round(0.6 + stems_mod.LOSSY_RETRY_MARGIN_DB, 3), False)]
    assert out.tp == -1.05

    calls.clear()
    readings = iter([Loudness(-9.0, 2.5), Loudness(-9.0, -1.2)])
    path, out = asyncio.run(
        stems_mod._render_master_verified(build, ".mp4", "t", -1.0, None, None, True)
    )
    assert calls == [(0.0, False), (0.0, True)]
    assert out.tp == -1.2


def test_take_render_carries_the_master_chain():
    from app.pipeline.take_render import AudioLane, build_video_render_cmd

    chain = mastering.master_chain(0.0, None, 2.0, -1.5, 44100)
    cmd = build_video_render_cmd(
        [AudioLane(Path("a.wav"), 1.0, "")],
        Path("take.wav"),
        1.0,
        Path("cam.webm"),
        0.0,
        5.0,
        "16:9",
        "cover",
        False,
        44100,
        master_chain=chain,
        trim_db=0.5,
        alt_aac=True,
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert f"{chain},volume=" in graph
    assert "EXPORT" not in graph and graph.count("alimiter") == 1
    assert cmd[cmd.index("-aac_coder") + 1] == "fast"


async def test_a_measurement_whose_client_left_is_stopped(jobs_dir, monkeypatch):
    """The studio drops a measurement when the mix changes again; the server
    stops it (its ffmpeg is killed by the cancel) instead of finishing it."""
    from fastapi import HTTPException

    _job(jobs_dir)
    state = {"cancelled": False}

    async def slow_plan(*args, **kwargs):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    class GoneRequest:
        async def is_disconnected(self):
            return True

    monkeypatch.setattr(stems_mod, "master_plan", slow_plan)
    monkeypatch.setattr(stems_mod, "_DISCONNECT_POLL_SEC", 0.01)
    with pytest.raises(HTTPException) as err:
        await stems_mod.get_mixdown_loudness(
            GoneRequest(),
            JOB,
            stems="vocals,bass",
            gains="1,1",
            pitches="",
            fx="",
            master=STREAMING,
            lossy=False,
        )
    assert err.value.status_code == 499
    assert state["cancelled"] is True
