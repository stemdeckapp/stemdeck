"""Stem choice per mode: the rest of a song for any set of stems, and exports
that rebuild Original exactly as playback does (docs/plans/0.21.0/mode-stem-choice.md).

GET /api/jobs/{id}/rest/{set}.wav: validation (job id, set whitelist and
canonical spelling, job state, traversal), the mix (the sum of the set within
2 LSB, never clipped, Voice with and without the split), caching (built once,
rebuilt when a stem changes, one build under concurrent requests) and the cap
on files per job.

The export's lanes= parameter: Original is every stem that is neither a lane
nor removed, whatever was muted; a mode showing Drums as a lane never exports
the drums twice; a request without lanes= is exactly as before. And the
existing bug the design found: without lanes=, a transposed export with a
muted lane put that stem back inside Original.
"""

from __future__ import annotations

import asyncio
import io
import os
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import stems as stems_mod
from app.api import takes as takes_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline import backing as backing_mod
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "5e7c0a0b0c01"
SR = 44100
SEC = 2.0
FREQ = {"vocals": 440, "drums": 90, "bass": 55, "guitar": 330, "piano": 262, "other": 600}
ALL = tuple(FREQ)


def _app():
    from app.main import app

    return app


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    yield config.JOBS_DIR
    _jobs.pop(JOB, None)


@pytest.fixture
def client(jobs_dir):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://testserver"
    )


def _sine(name: str, amp: float) -> np.ndarray:
    t = np.arange(int(SR * SEC)) / SR
    x = amp * np.sin(2 * np.pi * FREQ.get(name, 700) * t)
    return np.stack([x, x], 1)


def _make_job(jobs_dir, amp=0.05, names=ALL, selected=("vocals",), status="done") -> Job:
    """A job whose six stems are sines at known frequencies, with the
    original.wav its selection would have made (the sum of the others)."""
    job = Job(id=JOB, status=status, title="Song", duration_sec=SEC)
    job.selected_stems = list(selected)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name in names:
        sf.write(stems / f"{name}.wav", _sine(name, amp), SR, subtype="PCM_16")
    rest = [n for n in ALL if n not in selected and n in names]
    if rest:
        sf.write(stems / "original.wav", sum(_sine(n, amp) for n in rest), SR, subtype="PCM_16")
    return job


def _read(path_or_bytes) -> np.ndarray:
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else str(path_or_bytes)
    data, _sr = sf.read(src, dtype="float64", always_2d=True)
    return data


def _sum(jobs_dir, names) -> np.ndarray:
    return sum(_read(jobs_dir / JOB / "stems" / f"{n}.wav") for n in names)


def _tone(x: np.ndarray, freq: float) -> float:
    """The amplitude of the `freq` sine in channel 0 (projection onto it)."""
    n = len(x)
    t = np.arange(n) / SR
    s, c = np.sin(2 * np.pi * freq * t), np.cos(2 * np.pi * freq * t)
    return float(2 * np.hypot(x[:, 0] @ s, x[:, 0] @ c) / n)


# ─── The rest endpoint: validation ───────────────────────────────────────


async def test_bad_sets_are_refused(jobs_dir, client):
    _make_job(jobs_dir)
    async with client as c:
        for bad in (
            "kazoo",
            "drums-kazoo",
            "drums-bass",  # not sorted
            "bass-bass",  # repeated
            "bass--drums",  # an empty name
            "original",
            "lead_vocals",
            "bass-drums-guitar-other-piano-vocals-vocals",
        ):
            r = await c.get(f"/api/jobs/{JOB}/rest/{bad}.wav")
            assert r.status_code == 422, bad
        assert (await c.get("/api/jobs/not-a-job/rest/bass.wav")).status_code == 404
        assert (await c.get("/api/jobs/ffffffffffff/rest/bass.wav")).status_code == 404
        assert (await c.get("/api/jobs/..%2F..%2Fetc/rest/bass.wav")).status_code == 404
        r = await c.get(f"/api/jobs/{JOB}/rest/..%2Fbass.wav")
        assert r.status_code in (404, 422)
        r = await c.get(f"/api/jobs/{JOB}/rest/bass-..%2F..%2Fx.wav")
        assert r.status_code in (404, 422)
    assert not list((jobs_dir / JOB).glob("rest_*"))


async def test_a_job_still_processing_has_no_rest(jobs_dir, client):
    _make_job(jobs_dir, status="processing")
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/rest/bass-drums.wav")
    assert r.status_code == 404 and "not ready" in r.json()["detail"]


async def test_a_missing_stem_is_refused_not_mixed_without(jobs_dir, client):
    _make_job(jobs_dir, names=("vocals", "drums", "bass"))
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/rest/bass-guitar.wav")
    assert r.status_code == 404
    assert str(jobs_dir) not in r.text, "no paths in errors"
    assert not list((jobs_dir / JOB).glob("rest_*.wav"))


def test_the_parser_has_one_spelling_per_set():
    assert backing_mod.parse_rest_set("bass-drums-other") == ("bass", "drums", "other")
    assert backing_mod.rest_name(["other", "drums", "bass", "drums"]) == "bass-drums-other"
    for bad in ("", "drums-bass", "a", "-bass", "bass-"):
        with pytest.raises(ValueError):
            backing_mod.parse_rest_set(bad)


# ─── The mix ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "names", [("bass", "guitar", "other", "piano"), ("drums",), ("bass", "vocals"), ALL]
)
async def test_the_rest_is_the_sum_of_its_stems(jobs_dir, client, names):
    _make_job(jobs_dir)
    spelled = "-".join(sorted(names))
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/rest/{spelled}.wav")
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
        head = await c.head(f"/api/jobs/{JOB}/rest/{spelled}.wav")
        assert head.status_code == 200
        ranged = await c.get(f"/api/jobs/{JOB}/rest/{spelled}.wav", headers={"Range": "bytes=0-99"})
        assert ranged.status_code == 206 and len(ranged.content) == 100
    out = _read(jobs_dir / JOB / f"rest_{spelled}.wav")
    want = _sum(jobs_dir, names)
    assert out.shape == want.shape
    assert np.max(np.abs(out - want)) <= 2 / 32768, "the sum of its stems within 2 LSB"
    for n in ALL:
        if n in names:
            assert _tone(out, FREQ[n]) == pytest.approx(0.05, rel=0.02)
        else:
            assert _tone(out, FREQ[n]) < 0.0005, f"{n} is not in the set"


async def test_a_loud_rest_is_scaled_never_clipped(jobs_dir):
    _make_job(jobs_dir, amp=0.35)
    path, built = backing_mod.ensure_rest(JOB, jobs_dir / JOB, ("bass", "drums", "guitar", "other"))
    assert built
    assert np.max(np.abs(_read(path))) <= 0.9991
    assert '"gain": 1.0' not in path.with_suffix(".json").read_text(encoding="utf-8")


def test_voice_in_a_set_is_vocals_wav_never_the_split_on_top(jobs_dir):
    _make_job(jobs_dir, names=(*ALL, "lead_vocals", "backing_vocals"))
    stems = jobs_dir / JOB / "stems"
    names = [p.name for p in backing_mod.rest_inputs(stems, ("bass", "vocals"))]
    assert names == ["bass.wav", "vocals.wav"]
    names = [p.name for p in backing_mod.rest_inputs(stems, ("bass", "drums"))]
    assert "lead_vocals.wav" not in names and "backing_vocals.wav" not in names


def test_without_vocals_wav_the_split_stands_in_for_voice(jobs_dir):
    _make_job(
        jobs_dir,
        names=("drums", "bass", "guitar", "piano", "other", "lead_vocals", "backing_vocals"),
    )
    stems = jobs_dir / JOB / "stems"
    names = {p.name for p in backing_mod.rest_inputs(stems, ("bass", "vocals"))}
    assert names == {"bass.wav", "lead_vocals.wav", "backing_vocals.wav"}


# ─── Caching, concurrency and the cap ────────────────────────────────────


async def test_a_second_request_does_not_rebuild(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)
    builds = []
    real = backing_mod._mix

    def counted(paths, out):
        builds.append(out)
        return real(paths, out)

    monkeypatch.setattr(backing_mod, "_mix", counted)
    async with client as c:
        a = await c.get(f"/api/jobs/{JOB}/rest/bass-other.wav")
        b = await c.get(f"/api/jobs/{JOB}/rest/bass-other.wav")
    assert a.status_code == b.status_code == 200 and a.content == b.content
    assert len(builds) == 1


async def test_concurrent_requests_build_once(jobs_dir, client, monkeypatch):
    _make_job(jobs_dir)
    builds = []
    real = backing_mod._mix

    def slow(paths, out):
        builds.append(out)
        time.sleep(0.3)
        return real(paths, out)

    monkeypatch.setattr(backing_mod, "_mix", slow)
    async with client as c:
        rs = await asyncio.gather(
            *(c.get(f"/api/jobs/{JOB}/rest/drums-piano.wav") for _ in range(4))
        )
    assert [r.status_code for r in rs] == [200] * 4
    assert len(builds) == 1
    assert len({r.content for r in rs}) == 1
    assert not list((jobs_dir / JOB).glob(".rest_*.tmp.wav"))


def test_changed_stems_make_a_new_rest(jobs_dir):
    _make_job(jobs_dir)
    path, built = backing_mod.ensure_rest(JOB, jobs_dir / JOB, ("bass", "drums"))
    assert built
    assert backing_mod.ensure_rest(JOB, jobs_dir / JOB, ("bass", "drums")) == (path, False)
    drums = jobs_dir / JOB / "stems" / "drums.wav"
    sf.write(drums, _sine("guitar", 0.02), SR, subtype="PCM_16")
    os.utime(drums, ns=(drums.stat().st_atime_ns, drums.stat().st_mtime_ns + 10_000_000))
    _, rebuilt = backing_mod.ensure_rest(JOB, jobs_dir / JOB, ("bass", "drums"))
    assert rebuilt


def test_at_most_four_rest_files_per_job_oldest_go_first(jobs_dir):
    _make_job(jobs_dir)
    job_dir = jobs_dir / JOB
    sets = [("bass",), ("drums",), ("guitar",), ("other",), ("piano",), ("vocals",)]
    for i, s in enumerate(sets):
        path, _ = backing_mod.ensure_rest(JOB, job_dir, s)
        # Distinct mtimes, in order, whatever the filesystem's resolution.
        stamp = 1_700_000_000_000_000_000 + i * 1_000_000_000
        os.utime(path, ns=(stamp, stamp))
    kept = sorted(p.name for p in job_dir.glob("rest_*.wav"))
    assert len(kept) == backing_mod.REST_MAX_FILES
    assert kept == ["rest_guitar.wav", "rest_other.wav", "rest_piano.wav", "rest_vocals.wav"]
    assert len(list(job_dir.glob("rest_*.json"))) == backing_mod.REST_MAX_FILES
    # Asking again for a kept one makes it the newest: it survives the next.
    backing_mod.ensure_rest(JOB, job_dir, ("guitar",))
    backing_mod.ensure_rest(JOB, job_dir, ("bass",))
    kept = {p.name for p in job_dir.glob("rest_*.wav")}
    assert "rest_guitar.wav" in kept and "rest_bass.wav" in kept
    assert "rest_other.wav" not in kept
    # The backing (Streamer) is never counted or pruned.
    backing_mod.ensure_backing(JOB, job_dir, "bass")
    backing_mod.ensure_rest(JOB, job_dir, ("drums",))
    assert (job_dir / "backing_without_bass.wav").is_file()


# ─── lanes=: parsing ─────────────────────────────────────────────────────


def test_lanes_param_parses_and_refuses_unknown_names():
    assert stems_mod.parse_lanes_param(None) is None
    assert stems_mod.parse_lanes_param("") == frozenset()
    assert stems_mod.parse_lanes_param("drums,vocals") == {"drums", "vocals"}
    for bad in ("original", "kazoo", "drums,drums", "lead_vocals", "../drums"):
        with pytest.raises(Exception) as e:
            stems_mod.parse_lanes_param(bad)
        assert e.value.status_code == 422


def test_render_body_takes_lanes():
    assert takes_mod.RenderBody(lanes=["drums"]).lanes == ["drums"]
    assert takes_mod.RenderBody().lanes is None
    with pytest.raises(ValueError):
        takes_mod.RenderBody(lanes=["a"] * 7)


# ─── lanes=: what Original holds ─────────────────────────────────────────


def _pairs(lanes):
    return {(lane.name, lane.path.name) for lane in lanes}


def test_the_existing_bug_a_transposed_export_puts_a_muted_lane_back(jobs_dir):
    """The design's suspicion (2.5, point 2), settled: the client leaves a
    muted lane out of the request, and without lanes= Original is the
    complement of the request, so a transposed export put the muted Drums
    back inside Original. Real before this change (the legacy call below);
    the client now always sends lanes=, which fixes it."""
    _make_job(jobs_dir, selected=("vocals", "drums"))
    # Practice: Original, Vocals and Drums; Drums muted, so not requested.
    names = ["original", "vocals"]
    legacy, _ = stems_mod._expand_render_lanes(JOB, names, [1.0, 1.0], [2, 2])
    assert ("original", "drums.wav") in _pairs(legacy), "the bug: drums back in Original"
    fixed, _ = stems_mod._expand_render_lanes(
        JOB, names, [1.0, 1.0], [2, 2], lanes=frozenset({"vocals", "drums"})
    )
    assert ("original", "drums.wav") not in _pairs(fixed)
    assert {("original", f"{n}.wav") for n in ("bass", "guitar", "piano", "other")} <= _pairs(fixed)
    # Drums are never transposed, wherever they are.
    assert all(lane.pitch == 0 for lane in fixed if lane.path.name == "drums.wav")


def test_a_mode_with_drums_as_a_lane_never_uses_the_jobs_original(jobs_dir):
    _make_job(jobs_dir, selected=("vocals",))  # original.wav holds the drums
    names = ["original", "vocals", "drums"]
    legacy, _ = stems_mod._expand_render_lanes(JOB, names, [1.0] * 3, [0] * 3)
    assert ("original", "original.wav") in _pairs(legacy), "drums twice before lanes="
    lanes, _ = stems_mod._expand_render_lanes(
        JOB, names, [1.0] * 3, [0] * 3, lanes=frozenset({"vocals", "drums"})
    )
    got = _pairs(lanes)
    assert ("original", "original.wav") not in got
    assert ("original", "drums.wav") not in got and ("drums", "drums.wav") in got
    assert sum(1 for _, p in got if p == "drums.wav") == 1


def test_lanes_equal_to_the_jobs_selection_keep_the_old_render(jobs_dir):
    _make_job(jobs_dir, selected=("vocals",))
    names = ["original", "vocals"]
    old, _ = stems_mod._expand_render_lanes(JOB, names, [1.0, 0.8], [0, 0])
    new, _ = stems_mod._expand_render_lanes(
        JOB, names, [1.0, 0.8], [0, 0], lanes=frozenset({"vocals"})
    )
    assert old == new


def test_a_six_stem_song_gets_an_original_when_the_mode_has_fewer_lanes(jobs_dir):
    _make_job(jobs_dir, selected=ALL)
    assert not (jobs_dir / JOB / "stems" / "original.wav").exists()
    lanes, _ = stems_mod._expand_render_lanes(
        JOB, ["original", "vocals"], [1.0, 1.0], [0, 0], lanes=frozenset({"vocals"})
    )
    assert {p for n, p in _pairs(lanes) if n == "original"} == {
        f"{n}.wav" for n in ("drums", "bass", "guitar", "piano", "other")
    }
    # Without lanes= the old answer stands: there is no original lane.
    with pytest.raises(Exception) as e:
        stems_mod._expand_render_lanes(JOB, ["original", "vocals"], [1.0, 1.0], [0, 0])
    assert e.value.status_code == 404


def test_a_requested_stem_that_is_not_a_lane_is_refused(jobs_dir):
    _make_job(jobs_dir)
    with pytest.raises(Exception) as e:
        stems_mod._expand_render_lanes(
            JOB, ["original", "drums"], [1.0, 1.0], [0, 0], lanes=frozenset({"vocals"})
        )
    assert e.value.status_code == 422


def test_lanes_with_the_split_and_with_a_removed_stem(jobs_dir):
    _make_job(jobs_dir, names=(*ALL, "lead_vocals", "backing_vocals"))
    names = ["original", "lead_vocals", "backing_vocals"]
    lanes, _ = stems_mod._expand_render_lanes(
        JOB, names, [1.0] * 3, [0] * 3, lanes=frozenset({"vocals"})
    )
    got = _pairs(lanes)
    assert ("original", "vocals.wav") not in got
    assert {("lead_vocals", "lead_vocals.wav"), ("backing_vocals", "backing_vocals.wav")} <= got
    # Streamer's "I play: Bass" on top: bass is in neither.
    lanes, _ = stems_mod._expand_render_lanes(
        JOB, names, [1.0] * 3, [0] * 3, frozenset({"bass"}), lanes=frozenset({"vocals"})
    )
    assert all(p != "bass.wav" for _, p in _pairs(lanes))
    # Every stem a lane: Original holds nothing and is left out.
    lanes, _ = stems_mod._expand_render_lanes(
        JOB, ["original", "drums"], [1.0, 1.0], [0, 0], lanes=frozenset(ALL)
    )
    assert _pairs(lanes) == {("drums", "drums.wav")}


def test_lanes_does_not_open_a_path_outside_the_job(jobs_dir):
    _make_job(jobs_dir)
    with pytest.raises(Exception) as e:
        stems_mod._expand_render_lanes(
            "../../etc", ["original"], [1.0], [0], lanes=frozenset({"vocals"})
        )
    assert e.value.status_code == 404


# ─── lanes=: real exports ────────────────────────────────────────────────


async def test_a_real_export_counts_the_drums_once(jobs_dir, client):
    """Practice shows Drums as a lane on a song extracted with Vocals only, so
    the job's original.wav holds the drums. The export must carry them once,
    as playback does."""
    skip_without_ffmpeg()
    _make_job(jobs_dir, selected=("vocals",))
    q = "stems=original,vocals,drums&gains=1,1,1"
    async with client as c:
        r_old = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}")
        r_new = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}&lanes=vocals,drums")
        r_bad = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}&lanes=vocals")
    assert r_old.status_code == r_new.status_code == 200
    assert r_bad.status_code == 422
    old, new = _read(r_old.content), _read(r_new.content)
    assert _tone(old, FREQ["drums"]) == pytest.approx(0.10, rel=0.05), "before: drums twice"
    assert _tone(new, FREQ["drums"]) == pytest.approx(0.05, rel=0.05), "now: drums once"
    for n in ("vocals", "bass", "guitar", "piano", "other"):
        assert _tone(new, FREQ[n]) == pytest.approx(0.05, rel=0.05), n


async def test_a_muted_original_and_a_muted_lane_stay_out_of_the_export(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir, selected=("vocals",))
    # Drums muted (not requested), Original playing: no drums anywhere.
    async with client as c:
        r = await c.get(
            f"/api/jobs/{JOB}/mixdown.wav?stems=original,vocals&gains=1,1&lanes=drums,vocals"
        )
    assert r.status_code == 200
    out = _read(r.content)
    assert _tone(out, FREQ["drums"]) < 0.001
    assert _tone(out, FREQ["bass"]) == pytest.approx(0.05, rel=0.05)


async def test_lanes_is_part_of_the_cache_key(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir, selected=("vocals",))
    q = "stems=original,vocals&gains=1,1"
    async with client as c:
        a = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}")
        b = await c.get(f"/api/jobs/{JOB}/mixdown.wav?{q}&lanes=drums,vocals")
    assert _tone(_read(a.content), FREQ["drums"]) > 0.04
    assert _tone(_read(b.content), FREQ["drums"]) < 0.001
    key = stems_mod._mixdown_cache_key
    assert key(JOB, "wav", ["original"], [1.0], None, None) == key(
        JOB, "wav", ["original"], [1.0], None, None, lanes_fragment=""
    )
    assert key(JOB, "wav", ["original"], [1.0], None, None) != key(
        JOB, "wav", ["original"], [1.0], None, None, lanes_fragment="l:drums"
    )
