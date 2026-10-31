"""GET /api/jobs/{id}/suggest/eq and /suggest/master.

- Validation before any ffmpeg: a bad job id, an unknown job, a traversal
  attempt are 404; an unknown layout, mode, lane, target, a target of another
  instrument and a malformed fx are 422.
- The happy paths answer the documented shape; a lane is measured once and
  answered from the cache for every other target and mode.
- Merge keeps the lane's current bands; the master's own EQ is not part of
  what is measured (the same cache entry with or without it).
- Rendering a lane through its suggestion (the export's ffmpeg filters) and
  asking again gives almost nothing more to do.
"""

from __future__ import annotations

import base64
import json
import subprocess

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import suggest as suggest_mod
from app.core.config import ffmpeg_executable
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import eq_fit as F
from app.pipeline.fx_filters import Band, Eq, eq_filter
from tests.ffmpeg_probe import skip_without_ffmpeg
from tests.test_eq_suggest import BALANCED, shaped_noise

JOB = "abcdef0f0f03"
SR = 44100


def fx_param(lanes: dict | None = None, master: list | None = None) -> str:
    body = {"v": 1, "lanes": lanes or {}}
    if master is not None:
        body["master"] = master
    raw = json.dumps(body).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest_mod, "_SUGGEST_CACHE_DIR", tmp_path / "cache" / "suggest")
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    from app.core import config

    return config.JOBS_DIR


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def no_ffmpeg(monkeypatch):
    async def refuse(*args, **kwargs):
        raise AssertionError("ffmpeg ran before validation")

    monkeypatch.setattr(suggest_mod, "_run_analysis", refuse)


@pytest.fixture
def count_runs(monkeypatch):
    calls = []
    real = suggest_mod._run_analysis

    async def counted(inputs, graph):
        calls.append(graph)
        return await real(inputs, graph)

    monkeypatch.setattr(suggest_mod, "_run_analysis", counted)
    return calls


def _job(jobs_dir, lanes: dict[str, np.ndarray], job_id: str = JOB) -> None:
    job = Job(id=job_id, status="done", title="Suggest", duration_sec=8.0)
    _jobs[job.id] = job
    stems = jobs_dir / job_id / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name, x in lanes.items():
        sf.write(stems / f"{name}.wav", x, SR, subtype="PCM_16")
        job.stems.append({"name": name, "url": f"/api/jobs/{job_id}/stems/{name}.wav"})


def _lanes():
    return {
        "other": shaped_noise(
            shape_of("other.balanced"), seconds=8, extra=[Band("bell", 250.0, 6.0, 1.0, 12)]
        ),
        "bass": shaped_noise(shape_of("bass.balanced"), seconds=8, seed=2),
    }


def shape_of(target_id: str):
    from app.pipeline.eq_targets import get_target

    return get_target(target_id).bands_db


# ---------------------------------------------------------------------------
# Validation (no ffmpeg runs)
# ---------------------------------------------------------------------------

LANE_BAD = [
    "lane=other&layout=pedal",
    "lane=other&mode=sideways",
    "lane=bass&layout=bass&mode=merge",
    "lane=mix",
    "lane=..%2F..%2Fsecret",
    "lane=vocals",
    "lane=other&target=nope.balanced",
    "lane=other&target=bass.balanced",
    "lane=other&fx=%25%25%25",
    "lane=other&fx=" + fx_param({"other": [{"id": "eq", "b": [["bell", 1e9, 0, 1, 12]]}]}),
    "lane=" + "a" * 40,
]


@pytest.mark.parametrize("query", LANE_BAD)
async def test_lane_route_refuses_bad_input_before_ffmpeg(client, jobs_dir, no_ffmpeg, query):
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?{query}")
    assert r.status_code == 422, r.text
    assert "Traceback" not in r.text and str(jobs_dir) not in r.text


@pytest.mark.parametrize(
    "url",
    [
        "/api/jobs/not-a-job/suggest/eq?lane=other",
        "/api/jobs/abcdef0f0fff/suggest/eq?lane=other",  # well formed, unknown
        "/api/jobs/../../etc/passwd/suggest/eq?lane=other",
        "/api/jobs/..%2F..%2Fetc/suggest/eq?lane=other",
        "/api/jobs/not-a-job/suggest/master?stems=other&gains=1",
        "/api/jobs/abcdef0f0fff/suggest/master?stems=other&gains=1",
    ],
)
async def test_unknown_jobs_and_traversal_are_404(client, jobs_dir, no_ffmpeg, url):
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(url)
    assert r.status_code == 404


async def test_a_lane_the_job_does_not_have_is_404(client, jobs_dir, no_ffmpeg):
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=guitar&layout=guitar")
    assert r.status_code == 404


async def test_a_job_that_is_not_done_is_404(client, jobs_dir, no_ffmpeg):
    _job(jobs_dir, _lanes())
    _jobs[JOB].status = "running"
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=other")
    assert r.status_code == 404


MASTER_BAD = [
    "stems=other,bass&gains=1",
    "stems=other&gains=9",
    "stems=nope&gains=1",
    "stems=other&gains=1&target=bass.balanced",
    "stems=other&gains=1&mode=sideways",
    "stems=other&gains=1&pitches=99",
    "stems=other&gains=1&fx=%25%25",
]


@pytest.mark.parametrize("query", MASTER_BAD)
async def test_master_route_refuses_bad_input_before_ffmpeg(client, jobs_dir, no_ffmpeg, query):
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/master?{query}")
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Happy paths and the cache
# ---------------------------------------------------------------------------


async def test_lane_suggestion_shape_and_cache(client, jobs_dir, count_runs):
    skip_without_ffmpeg()
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=other")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["lane"] == "other" and body["target"] == "other.balanced"
        assert {ch["id"] for ch in body["choices"]} == {
            "other.balanced",
            "other.warm",
            "other.bright",
        }
        assert len(body["freqs"]) == 31 and len(body["levels"]["after"]) == 31
        assert 1 <= len(body["bands"]) <= F.MAX_BANDS_LANE
        cut = [b for b in body["bands"] if b["type"] == "bell" and 160 <= b["f"] <= 400]
        assert cut and cut[0]["g"] < 0
        assert body["distance"]["suggested"]["rms"] < body["distance"]["raw"]["rms"]
        assert abs(body["loudness"]["suggested_change"]) < 0.15
        assert body["eq"]["out"] == body["out"]
        assert body["why"][0]["key"].startswith("suggest.why.")
        assert len(count_runs) == 1
        # Another target, another mode, the same measurement.
        r2 = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=other&target=other.bright")
        r3 = await c.get(
            f"/api/jobs/{JOB}/suggest/eq?lane=other&mode=merge&fx="
            + fx_param(
                {"other": [{"id": "eq", "out": 0, "b": [["highshelf", 8000, 2, 0.707, 12]]}]}
            )
        )
        assert r2.status_code == 200 and r3.status_code == 200
        assert len(count_runs) == 1
    cache = jobs_dir.parent / "cache" / "suggest"
    files = [p for p in (suggest_mod._SUGGEST_CACHE_DIR).iterdir()]
    assert len(files) == 1
    merged = r3.json()["eq"]["bands"]
    assert merged[0] == {"type": "highshelf", "f": 8000.0, "g": 2.0, "q": 0.707, "slope": 12}
    del cache


async def test_pedal_layout_answers_with_its_knobs(client, jobs_dir):
    skip_without_ffmpeg()
    _job(jobs_dir, _lanes())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=bass&layout=bass&target=bass.bright")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    slots = {s.id for s in F.LAYOUTS["bass"]}
    assert all(b["slot"] in slots for b in body["bands"])


async def test_a_quiet_lane_says_so(client, jobs_dir):
    skip_without_ffmpeg()
    lanes = _lanes()
    lanes["other"] = lanes["other"] * 10 ** (-48 / 20)
    _job(jobs_dir, lanes)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=other")
    assert r.status_code == 200
    assert r.json()["status"] == "quiet"


async def test_master_suggestion_and_its_cache(client, jobs_dir, count_runs):
    skip_without_ffmpeg()
    _job(jobs_dir, _lanes())
    q = "stems=other,bass&gains=1,0.5"
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/suggest/master?{q}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok" and body["target"] == "mix.balanced"
        assert len(body["choices"]) == 4
        assert body["master"]["preset"] in ("gentle", "streaming", "louder", "custom")
        assert body["master"]["limit_db"] <= F.LIMIT_ALLOWANCE_DB
        assert body["analysis"]["i"] is not None and body["analysis"]["tp"] is not None
        assert len(body["bands"]) <= F.MAX_BANDS_MASTER
        # The master EQ is what is suggested, so it is not measured: the
        # same entry answers with or without one.
        master = [{"id": "eq", "out": 0, "b": [["bell", 300, -3, 1, 12]]}]
        r2 = await c.get(
            f"/api/jobs/{JOB}/suggest/master?{q}&fx={fx_param(master=master)}&mode=merge"
        )
        assert r2.status_code == 200
        assert r2.json()["eq"]["bands"][0]["f"] == 300.0
        assert len(count_runs) == 1
        # A lane's EQ is part of the mix: measured again.
        lane_eq = {"other": [{"id": "eq", "out": 0, "b": [["bell", 250, -6, 1, 12]]}]}
        r3 = await c.get(f"/api/jobs/{JOB}/suggest/master?{q}&fx={fx_param(lane_eq)}")
        assert r3.status_code == 200
        assert len(count_runs) == 2
        # The lane's cut is heard by the master's measurement: less to cut there.
        cut_before = sum(-b["g"] for b in body["bands"] if 160 <= b["f"] <= 400 and b["g"] < 0)
        cut_after = sum(-b["g"] for b in r3.json()["bands"] if 160 <= b["f"] <= 400 and b["g"] < 0)
        assert cut_after < cut_before or cut_before == 0


def _render(src, dst, chain):
    subprocess.run(
        [ffmpeg_executable(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-af", chain, "-c:a", "pcm_s16le", str(dst)],
        check=True,
        timeout=120,
    )  # fmt: skip


async def test_suggesting_again_after_rendering_the_suggestion_changes_little(client, jobs_dir):
    skip_without_ffmpeg()
    x = shaped_noise(
        BALANCED,
        seconds=8,
        extra=[Band("bell", 315.0, 6.0, 1.0, 12), Band("highshelf", 5000.0, -6.0, 0.707, 12)],
    )
    _job(jobs_dir, {"original": x})
    async with client as c:
        first = (await c.get(f"/api/jobs/{JOB}/suggest/eq?lane=original")).json()
        assert first["status"] == "ok" and first["bands"]
        bands = tuple(Band(b["type"], b["f"], b["g"], b["q"], b["slope"]) for b in first["bands"])
        src = jobs_dir / JOB / "stems" / "original.wav"
        other = "abcdef0f0f04"
        _job(jobs_dir, {}, job_id=other)
        dst = jobs_dir / other / "stems" / "original.wav"
        _render(src, dst, eq_filter(Eq(first["out"], bands), SR))
        again = (await c.get(f"/api/jobs/{other}/suggest/eq?lane=original")).json()
    assert again["status"] == "ok"
    assert len(again["bands"]) <= 1
    assert all(abs(b["g"]) <= 1.5 for b in again["bands"] if b["type"] != "lowcut")
    assert again["distance"]["raw"]["rms"] == pytest.approx(
        first["distance"]["suggested"]["rms"], abs=0.35
    )
