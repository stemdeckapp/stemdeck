"""The studio EQ in the exports: GET mixdown.{ext} and video.mp4 with fx=.

- A lane's EQ changes the rendered file by the response the live engine has
  (measured with an FFT on the export, against fx_filters' maths).
- An EQ that changes nothing (flat bands, bypassed, or lanes not in the mix)
  renders the very same bytes as no EQ at all: the graph is untouched.
- Malformed or hostile fx values are a 422 with a safe message, before any
  ffmpeg runs; a valid one is part of the render cache key.
- Order in the graph: EQ first in the lane (before the pitch shift, as live),
  master EQ after the mix and before the export limiter.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf
from scipy.signal import csd, welch

from app.api import stems as stems_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.fx_filters import FX_PARAM_MAX_CHARS, Band, Eq, eq_response_db
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdef0e0e01"
SR = 44100


def fx(lanes: dict | None = None, master: list | None = None) -> str:
    body: dict = {"v": 1, "lanes": lanes or {}}
    if master is not None:
        body["master"] = master
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")


def bell(f, g, q=1.0):
    return {"id": "eq", "out": 0, "b": [["bell", f, g, q, 12]]}


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _job(jobs_dir, seconds: float = 4.0, video: bool = False) -> Path:
    """Two quiet noise lanes (far under the limiter ceiling), different seeds."""
    n = int(SR * seconds)
    job = Job(id=JOB, status="done", title="Noise", duration_sec=seconds)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(("vocals", "bass")):
        x = 0.02 * np.random.default_rng(i + 1).standard_normal((n, 2))
        sf.write(stems / f"{name}.wav", x, SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})
    if video:
        (jobs_dir / JOB / "video.mp4").write_bytes(b"not parsed before validation")
    return stems


def _lane(stems: Path, name: str) -> np.ndarray:
    return sf.read(stems / f"{name}.wav", dtype="float64")[0][:, 0]


def _gain_db(x: np.ndarray, y: np.ndarray, freqs) -> np.ndarray:
    f, pxy = csd(x, y, fs=SR, nperseg=4410, noverlap=2205)
    _, pxx = welch(x, fs=SR, nperseg=4410, noverlap=2205)
    h = 20 * np.log10(np.abs(pxy / pxx))
    return np.interp(freqs, f, h)


async def _wav(c, query: str) -> bytes:
    r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{query}")
    assert r.status_code == 200, r.text
    return r.content


async def test_a_lane_eq_shapes_the_export_like_the_live_engine(jobs_dir, client):
    skip_without_ffmpeg()
    stems = _job(jobs_dir)
    eq = {"id": "eq", "out": -1.5, "b": [["bell", 1000, 9, 1.4, 12], ["lowcut", 150, 0, 0.707, 24]]}
    async with client as c:
        out = await _wav(c, f"stems=vocals&gains=1&fx={fx({'vocals': [eq]})}")
    y = sf.read(io.BytesIO(out), dtype="float64")[0][:, 0]
    x = _lane(stems, "vocals")
    freqs = [60, 100, 200, 500, 1000, 2000, 5000, 10000]
    measured = _gain_db(x, y, freqs)
    expected = eq_response_db(
        Eq(-1.5, (Band("bell", 1000, 9, 1.4, 12), Band("lowcut", 150, 0, 0.707, 24))),
        [float(f) for f in freqs],
        SR,
    )
    # The export is 16-bit and the estimate has 10 Hz bins: 0.3 dB, and
    # within 1 dB down in the cut's stopband.
    for f, m, e in zip(freqs, measured, expected, strict=True):
        assert abs(m - e) < (1.0 if e < -20 else 0.3), (f, m, e)


async def test_flat_bypassed_and_absent_eq_render_the_same_bytes(jobs_dir, client):
    """The null test: an fx that changes nothing leaves the graph exactly as
    it was, so the file is bit-identical to an export with no fx."""
    skip_without_ffmpeg()
    _job(jobs_dir)
    flat = {"id": "eq", "out": 0, "b": [["bell", 1000, 0, 1, 12], ["tilt", 800, 0, 1, 12]]}
    async with client as c:
        plain = await _wav(c, "stems=vocals,bass&gains=1,0.8")
        assert await _wav(c, f"stems=vocals,bass&gains=1,0.8&fx={fx({'vocals': [flat]})}") == plain
        assert (
            await _wav(c, f"stems=vocals,bass&gains=1,0.8&fx={fx({'other': [bell(1000, 6)]})}")
            == plain
        )
        assert await _wav(c, f"stems=vocals,bass&gains=1,0.8&fx={fx({}, [])}") == plain
        assert await _wav(c, "stems=vocals,bass&gains=1,0.8&fx=") == plain
        boosted = await _wav(c, f"stems=vocals,bass&gains=1,0.8&fx={fx({'bass': [bell(1000, 6)]})}")
    assert boosted != plain


async def test_master_eq_applies_to_the_whole_mix(jobs_dir, client):
    skip_without_ffmpeg()
    stems = _job(jobs_dir)
    async with client as c:
        out = await _wav(c, f"stems=vocals,bass&gains=1,1&fx={fx({}, [bell(2000, -12, 2)])}")
    y = sf.read(io.BytesIO(out), dtype="float64")[0][:, 0]
    x = _lane(stems, "vocals") + _lane(stems, "bass")
    measured = _gain_db(x, y, [2000, 200])
    assert measured[0] == pytest.approx(-12, abs=0.3)
    assert measured[1] == pytest.approx(0, abs=0.3)


async def test_fx_is_in_the_render_cache_key(jobs_dir, client, tmp_path):
    skip_without_ffmpeg()
    _job(jobs_dir)
    cache = tmp_path / "cache" / "mixdown"
    q = "stems=vocals&gains=1"
    async with client as c:
        await _wav(c, q)
        await _wav(c, f"{q}&fx={fx({'vocals': [bell(1000, 6)]})}")
        assert len(list(cache.glob("*.wav"))) == 2
        await _wav(c, f"{q}&fx={fx({'vocals': [bell(1000, 6)]})}")
        assert len(list(cache.glob("*.wav"))) == 2  # served from the cache
        await _wav(c, f"{q}&fx={fx({'vocals': [bell(1000, 7)]})}")
        assert len(list(cache.glob("*.wav"))) == 3


def test_cache_version_bumped():
    # "5" for the EQ; task F's master bumped it again.
    assert int(stems_mod._RENDER_CACHE_VERSION) >= 5


@pytest.mark.parametrize(
    "value",
    [
        "!!!",
        "A" * (FX_PARAM_MAX_CHARS + 1),
        fx({"vocals": [{"id": "eq", "out": 0, "b": [["bell;rm -rf /", 1000, 1, 1, 12]]}]}),
        fx({"vocals": [{"id": "eq", "out": 0, "b": [["bell", "1000[x]", 1, 1, 12]]}]}),
        fx({"vocals": [{"id": "eq", "out": 0, "b": [["bell", 99999, 1, 1, 12]]}]}),
        fx({"../../x": [bell(1000, 3)]}),
        fx({"vocals": [{"id": "compressor"}]}),
        base64.urlsafe_b64encode(b'{"v":1,"lanes":{"vocals":[{"id":"eq","out":NaN}]}}')
        .decode()
        .rstrip("="),
    ],
)
async def test_malformed_fx_is_a_422_before_any_render(jobs_dir, client, value, monkeypatch):
    _job(jobs_dir, seconds=0.5, video=True)

    async def no_render(*a, **k):
        raise AssertionError("rendered despite a bad fx")

    monkeypatch.setattr(stems_mod, "_render_to_file", no_render)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals&gains=1&fx={value}")
        assert r.status_code == 422, r.text
        detail = json.dumps(r.json())
        assert "Traceback" not in detail and "\\\\" not in detail
        v = await c.get(f"/api/jobs/{JOB}/video.mp4?stems=vocals&gains=1&fx={value}")
        assert v.status_code == 422, v.text


async def test_too_many_sections_is_a_422(jobs_dir, client, monkeypatch):
    """Each biquad is about 150 characters of filter graph and Windows caps a
    command line at 32767, so a request over the section budget is refused
    rather than failing inside the render."""
    skip_without_ffmpeg()
    _job(jobs_dir, seconds=0.5)
    steep = {"id": "eq", "out": 0, "b": [["lowcut", 40 + i, 0, 0.707, 48] for i in range(12)]}
    lanes = {"vocals": [steep], "bass": [steep]}
    q = f"stems=vocals,bass&gains=1,1&fx={fx(lanes, [steep])}"
    async with client as c:
        # 12 bands x 4 sections on two lanes and the master: 144, fine.
        assert (await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}")).status_code == 200
        from app.pipeline import fx_filters

        monkeypatch.setattr(fx_filters, "MAX_TOTAL_SECTIONS", 143)
        r = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}")
        assert r.status_code == 422 and "too many EQ bands" in r.json()["detail"]


async def test_eq_goes_before_the_pitch_shift_and_master_before_the_limiter(
    jobs_dir, client, monkeypatch
):
    _job(jobs_dir, seconds=0.5)
    seen: list[list[str]] = []

    async def capture(cmd, suffix, context="", cache_path=None):
        seen.append(cmd)
        raise RuntimeError("captured")

    monkeypatch.setattr(stems_mod, "_render_to_file", capture)
    monkeypatch.setattr(stems_mod, "_rubberband_available", lambda: True)
    async with client as c:
        with pytest.raises(RuntimeError):
            await c.get(
                f"/api/jobs/{JOB}/mixdown.wav?stems=vocals,bass&gains=1,1&pitches=2,0"
                f"&fx={fx({'vocals': [bell(1000, 6)]}, [bell(3000, -3)])}"
            )
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    lanes = graph.split(";")
    assert lanes[0].startswith("[0:a]biquad=") and "rubberband" in lanes[0]
    assert lanes[0].index("biquad") < lanes[0].index("rubberband")
    assert lanes[1].startswith("[1:a]volume=")  # bass: no EQ, untouched
    mix = lanes[-1]
    assert mix.index("amix") < mix.index("biquad") < mix.index("alimiter")
