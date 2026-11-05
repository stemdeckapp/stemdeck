"""The master (app/pipeline/mastering.py): filter strings, the master= parser,
and the render maths on synthetic signals of known loudness.

The render tests go through the real export route and the bundled ffmpeg,
and check the result with two independent oracles: pyloudnorm (BS.1770, the
stereo sum) for loudness, and a 4x numpy oversample for the true peak.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json

import httpx
import numpy as np
import pyloudnorm
import pytest
import soundfile as sf
from scipy.signal import resample_poly

from app.api import stems as stems_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import mastering as m
from app.pipeline.mastering import MasterError, MasterSettings, encode_master_param
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "abcdef0f0f01"
SR = 44100


def raw_param(body) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# ─── The parameter ───────────────────────────────────────────────────────


def test_parse_round_trips_and_canonicalises():
    s = m.parse_master_param(raw_param({"v": 1, "target": -14.04, "ceiling": -1.01, "comp": 0.333}))
    assert s == MasterSettings(target=-14.0, ceiling=-1.0, comp=0.33)
    assert m.parse_master_param(encode_master_param(s)) == s
    assert m.parse_master_param(raw_param({"v": 1, "target": None, "ceiling": -1})) == (
        MasterSettings(None, -1.0, 0.0)
    )
    assert m.parse_master_param("") is None
    assert m.parse_master_param(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "!!!",
        "a" * (m.MASTER_PARAM_MAX_CHARS + 1),
        raw_param("not json"),
        raw_param([1, 2]),
        raw_param({"v": 1, "target": -14, "ceiling": -1, "extra": 1}),
        raw_param({"v": 2, "target": -14, "ceiling": -1}),
        raw_param({"v": True, "target": -14, "ceiling": -1}),
        raw_param({"target": -14, "ceiling": -1}),
        raw_param({"v": 1, "ceiling": -1}),
        raw_param({"v": 1, "target": -14}),
        raw_param({"v": 1, "target": "-14", "ceiling": -1}),
        raw_param({"v": 1, "target": -14, "ceiling": True}),
        raw_param({"v": 1, "target": -14, "ceiling": -1, "comp": False}),
        raw_param('{"v": 1, "target": NaN, "ceiling": -1}'),
        raw_param('{"v": 1, "target": -14, "ceiling": -Infinity}'),
        raw_param('{"v": 1, "target": -1e999, "ceiling": -1}'),
        raw_param({"v": 1, "target": -5, "ceiling": -1}),
        raw_param({"v": 1, "target": -40, "ceiling": -1}),
        raw_param({"v": 1, "target": -14, "ceiling": 0}),
        raw_param({"v": 1, "target": -14, "ceiling": -10}),
        raw_param({"v": 1, "target": -14, "ceiling": -1, "comp": 2}),
        raw_param({"v": 1, "target": -14, "ceiling": -1, "comp": -0.1}),
        raw_param({"v": 1, "target": [-14], "ceiling": -1}),
        raw_param("[" * 5000 + "]" * 5000),
        "eyJ2IjoxfQ==",  # padding is not in the alphabet
        base64.urlsafe_b64encode(b"\xff\xfe").decode().rstrip("="),
    ],
)
def test_malformed_master_values_are_refused(raw):
    with pytest.raises(MasterError) as err:
        m.parse_master_param(raw)
    assert str(err.value).startswith("master: ")


# ─── Filter strings ──────────────────────────────────────────────────────


def test_limiter_is_oversampled_with_level_disabled_and_no_latency():
    f = m.limiter_filter(-1.0, 44100)
    assert f.startswith("aresample=176400,alimiter=")
    assert f.endswith(",aresample=44100")
    assert ":level=disabled:" in f and "latency=1" in f
    # The limit sits LIMITER_MARGIN_DB under the ceiling.
    limit = float(f.split("limit=")[1].split(":")[0])
    assert limit == pytest.approx(10 ** ((-1.0 - m.LIMITER_MARGIN_DB) / 20), rel=1e-6)


def test_master_chain_order_and_off_states():
    chain = m.master_chain(0.5, -18.0, 3.0, -1.0, 44100)
    assert chain.index("acompressor=") < chain.index("volume=") < chain.index("alimiter=")
    no_comp = m.master_chain(0.0, None, 3.0, -1.0, 44100)
    assert "acompressor" not in no_comp
    no_gain = m.master_chain(0.0, None, 0.0, -1.0, 44100)
    assert no_gain == m.limiter_filter(-1.0, 44100)


def test_filter_strings_carry_only_numbers():
    chain = m.master_chain(1.0, -60.0, 20.0, -0.1, 48000)
    for part in chain.split(","):
        name, _, opts = part.partition("=")
        assert name in {"acompressor", "volume", "aresample", "alimiter"}
        for opt in opts.split(":"):
            value = opt.split("=")[-1]
            assert value in {"rms", "average", "disabled"} or float(value) == float(value)


def test_compressor_threshold_follows_the_measured_loudness():
    assert m.comp_threshold_db(0.0, -12.0) is None
    assert m.comp_threshold_db(0.5, -12.0) == pytest.approx(-13.0)
    assert m.comp_threshold_db(1.0, -12.0) == pytest.approx(-16.0)
    # Clamped to acompressor's range.
    assert m.comp_threshold_db(1.0, -69.0) == m.COMP_THRESHOLD_FLOOR_DB
    assert m.comp_threshold_db(0.1, 0.0) == 0.0
    assert m.comp_ratio(0.5) == 2.0


def test_lossy_ceiling_never_above_minus_1_5():
    s = MasterSettings(-14.0, -0.5, 0.0)
    assert m.effective_ceiling(s, lossy=True) == -1.5
    assert m.effective_ceiling(s, lossy=False) == -0.5
    assert m.effective_ceiling(MasterSettings(-14.0, -2.0), lossy=True) == -2.0


def test_meter_output_parser():
    text = "\n".join(
        [
            "[a @ 0] lavfi.r128.I=-30.000",
            "[a @ 0] lavfi.r128.LRA=0.000",
            "[a @ 0] lavfi.r128.true_peak=0.100",
            "[a @ 0] lavfi.r128.I=-14.123",
            "[a @ 0] lavfi.r128.LRA=5.500",
            "[a @ 0] lavfi.r128.true_peak=0.891",
        ]
    )
    got = m.parse_meter(text)
    assert got.i == -14.123 and got.lra == 5.5
    assert got.tp == pytest.approx(20 * np.log10(0.891), abs=1e-3)
    silent = m.parse_meter("lavfi.r128.I=-70.000\nlavfi.r128.true_peak=0.000")
    assert silent.i is None and silent.tp is None
    assert m.parse_meter("garbage") == m.Loudness(None, None, None)
    assert m.parse_meter("lavfi.r128.I=nan").i is None


# ─── The plan's correction pass (no ffmpeg: a fake meter) ────────────────


def test_correction_steps_along_the_measured_slope(monkeypatch):
    """A limiter that turns 1 dB of gain into 0.6 LU above -10 LUFS: the
    first pass lands short, the corrections reach the target."""

    def fake_loudness(chain: str) -> float:
        gain = 0.0
        if "volume=" in chain:
            lin = float(chain.split("volume=")[1].split(",")[0])
            gain = 20 * np.log10(lin)
        raw = -16.0 + gain
        return raw if raw <= -10 else -10 + 0.6 * (raw + 10)

    async def fake_measure(inputs, graph, chain, rate):
        return m.Loudness(round(fake_loudness(chain), 3), -1.1)

    monkeypatch.setattr(m, "_measure", fake_measure)
    plan = asyncio.run(m.run_plan([], "", MasterSettings(-9.0, -1.0, 0.0), -1.0, 44100))
    assert plan.pre.i == -16.0
    assert abs(plan.post.i - -9.0) <= m.CORRECTION_TOL_LU
    assert plan.reached
    assert plan.passes <= 2 + m.CORRECTION_PASSES


def test_no_target_means_no_gain_and_silence_means_no_gain(monkeypatch):
    async def fake_measure(inputs, graph, chain, rate):
        return m.Loudness(None, None)

    monkeypatch.setattr(m, "_measure", fake_measure)
    plan = asyncio.run(m.run_plan([], "", MasterSettings(-14.0, -1.0, 0.5), -1.0, 44100))
    assert plan.gain_db == 0.0 and plan.passes == 3 and not plan.reached


# ─── Rendered: synthetic signals through the export route ────────────────


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


def _job(jobs_dir, lanes: dict[str, np.ndarray]) -> None:
    first = next(iter(lanes.values()))
    job = Job(id=JOB, status="done", title="Synthetic", duration_sec=len(first) / SR)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name, x in lanes.items():
        sf.write(stems / f"{name}.wav", x, SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{JOB}/stems/{name}.wav"})


def _lufs(x: np.ndarray) -> float:
    return float(pyloudnorm.Meter(SR).integrated_loudness(x))


def _true_peak_db(x: np.ndarray) -> float:
    up = resample_poly(x, 4, 1, axis=0)
    return float(20 * np.log10(np.abs(up).max()))


async def _master(c, names, settings: MasterSettings, ext="wav") -> tuple[np.ndarray, dict]:
    q = f"stems={','.join(names)}&gains={','.join('1' for _ in names)}"
    r = await c.get(f"/api/jobs/{JOB}/mixdown.{ext}?{q}&master={encode_master_param(settings)}")
    assert r.status_code == 200, r.text
    x, sr = sf.read(io.BytesIO(r.content), dtype="float64", always_2d=True)
    assert sr == SR
    return x, dict(r.headers)


def _stereo_sine(level_db: float, seconds: float = 6.0, f: float = 997.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    x = 10 ** (level_db / 20) * np.sin(2 * np.pi * f * t)
    return np.stack([x, x], 1)


async def test_a_stereo_sine_reaches_the_streaming_target(jobs_dir, client):
    skip_without_ffmpeg()
    _job(jobs_dir, {"vocals": _stereo_sine(-30.0)})
    async with client as c:
        x, headers = await _master(c, ["vocals"], MasterSettings(-14.0, -1.0))
    assert _lufs(x) == pytest.approx(-14.0, abs=0.1)
    assert _true_peak_db(x) <= -1.0
    assert headers["x-stemdeck-master-gain"].startswith("1")  # about +16 dB


async def test_a_clipped_hot_mix_is_brought_down_under_the_ceiling(jobs_dir, client):
    skip_without_ffmpeg()
    rng = np.random.default_rng(3)
    n = SR * 6
    hot = np.clip(2.5 * rng.standard_normal((n, 2)) * 0.4, -1, 1)  # clipped noise, about -3 LUFS
    _job(jobs_dir, {"drums": hot})
    async with client as c:
        x, headers = await _master(c, ["drums"], MasterSettings(-14.0, -1.0))
        louder, _ = await _master(c, ["drums"], MasterSettings(-9.0, -1.0, 0.5))
    assert _lufs(x) == pytest.approx(-14.0, abs=0.15)
    assert _true_peak_db(x) <= -1.0
    assert float(headers["x-stemdeck-master-gain"]) < -5
    assert _lufs(louder) == pytest.approx(-9.0, abs=0.15)
    assert _true_peak_db(louder) <= -1.0


async def test_a_very_quiet_mix_is_raised_by_the_gain_cap_and_says_so(jobs_dir, client):
    skip_without_ffmpeg()
    rng = np.random.default_rng(5)
    quiet = 10 ** (-55 / 20) * rng.standard_normal((SR * 6, 2))  # about -55 LUFS
    _job(jobs_dir, {"other": quiet})
    async with client as c:
        x, headers = await _master(c, ["other"], MasterSettings(-14.0, -1.0))
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown/loudness?stems=other&gains=1"
            f"&master={encode_master_param(MasterSettings(-14.0, -1.0))}"
        )
    plan = r.json()
    assert plan["gain_db"] == m.GAIN_MAX_DB
    assert plan["reached"] is False
    # Within 0.5 LU: white noise keeps a tenth of its energy above 20 kHz,
    # which the 4x resample round trip around the limiter trims a little
    # (0.36 LU on this signal; nothing audible is up there in music), and the
    # capped gain leaves no correction pass to make it up.
    assert plan["post"]["i"] == pytest.approx(plan["pre"]["i"] + m.GAIN_MAX_DB, abs=0.5)
    assert _lufs(x) == pytest.approx(plan["post"]["i"], abs=0.5)
    assert headers["x-stemdeck-master-gain"] == "20.00"


async def test_silence_stays_silence(jobs_dir, client):
    skip_without_ffmpeg()
    _job(jobs_dir, {"bass": np.zeros((SR * 3, 2))})
    async with client as c:
        x, headers = await _master(c, ["bass"], MasterSettings(-14.0, -1.0, 0.5))
    assert np.abs(x).max() == 0.0
    assert len(x) == SR * 3
    assert headers["x-stemdeck-loudness-pre"] == "i=none;tp=none"
    assert headers["x-stemdeck-master-gain"] == "0.00"


async def test_a_mono_lane_reaches_the_target(jobs_dir, client):
    """A mono file exports mono; BS.1770 weights its one channel as one."""
    skip_without_ffmpeg()
    t = np.arange(SR * 6) / SR
    rng = np.random.default_rng(9)
    mono = 0.05 * np.sin(2 * np.pi * 220 * t) + 0.02 * rng.standard_normal(len(t))
    _job(jobs_dir, {"vocals": mono})
    async with client as c:
        x, _ = await _master(c, ["vocals"], MasterSettings(-16.0, -2.0))
    assert x.shape[1] == 1
    assert _lufs(x[:, 0]) == pytest.approx(-16.0, abs=0.1)
    assert _true_peak_db(x) <= -2.0


async def test_prevent_clipping_changes_no_loudness_on_a_quiet_mix(jobs_dir, client):
    skip_without_ffmpeg()
    _job(jobs_dir, {"vocals": _stereo_sine(-20.0)})
    async with client as c:
        x, headers = await _master(c, ["vocals"], MasterSettings(None, -1.0))
    src = _stereo_sine(-20.0)
    assert _lufs(x) == pytest.approx(_lufs(src), abs=0.02)
    assert headers["x-stemdeck-master-gain"] == "0.00"


async def test_the_glue_compressor_is_measured_after_and_still_reaches_target(jobs_dir, client):
    """Gain computed before a compressor lands short; measured after it, the
    target is met (01-mastering 4.1)."""
    skip_without_ffmpeg()
    rng = np.random.default_rng(11)
    t = np.arange(SR * 8) / SR
    # Loud and soft halves, so the compressor works on one of them.
    env = np.where(t < 4, 0.3, 0.05)
    x = env[:, None] * rng.standard_normal((len(t), 2)) * 0.5
    _job(jobs_dir, {"other": x})
    async with client as c:
        y, _ = await _master(c, ["other"], MasterSettings(-14.0, -1.0, 1.0))
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown/loudness?stems=other&gains=1"
            f"&master={encode_master_param(MasterSettings(-14.0, -1.0, 1.0))}"
        )
    plan = r.json()
    assert plan["comp_threshold_db"] is not None and plan["comp_ratio"] == 3.0
    assert plan["comp_i"] < plan["pre"]["i"]  # the compressor took loudness
    assert _lufs(y) == pytest.approx(-14.0, abs=0.15)
    assert _true_peak_db(y) <= -1.0
