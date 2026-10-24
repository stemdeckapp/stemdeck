"""The edges of app/api/stems.py that the main suites do not reach: the cached
MP3 transcode (real ffmpeg), the master output note, the rubberband probe, the
click lane's render failures, and _expand_mode_lanes when a part of Original
is missing on disk.
"""

from __future__ import annotations

import json
import os
import subprocess

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi import HTTPException

from app.api import stems as stems_mod
from app.core.models import Job
from app.core.registry import _jobs
from app.pipeline.mastering import Loudness
from tests.ffmpeg_probe import skip_without_ffmpeg

JOB = "ed9e5a0b0c01"
SR = 44100
ALL = ("vocals", "drums", "bass", "guitar", "piano", "other")


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    monkeypatch.setattr(stems_mod, "_MASTER_PLAN_DIR", tmp_path / "cache" / "master")
    yield config.JOBS_DIR
    _jobs.pop(JOB, None)


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _make_job(jobs_dir, names=ALL, selected=("vocals",), status="done", seconds=1.0) -> Job:
    job = Job(id=JOB, status=status, title="Song", duration_sec=seconds)
    job.selected_stems = list(selected)
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name in names:
        t = np.arange(int(SR * seconds)) / SR
        x = 0.05 * np.sin(2 * np.pi * 440 * t)
        sf.write(stems / f"{name}.wav", np.stack([x, x], 1), SR, subtype="PCM_16")
    return job


# --- _ensure_cached_mp3 (GET /stems/{name}.mp3 without a range) -----------------


async def test_mp3_is_transcoded_once_and_then_served_from_disk(jobs_dir, client, monkeypatch):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    url = f"/api/jobs/{JOB}/stems/vocals.mp3"

    async with client as c:
        first = await c.get(url)
        assert first.status_code == 200, first.text
        assert first.headers["content-type"] == "audio/mpeg"
        assert first.content[:3] == b"ID3" or first.content[0] == 0xFF
        cached = jobs_dir / JOB / "stems" / "vocals.mp3"
        assert cached.is_file()

        async def no_spawn(*_a, **_k):
            raise AssertionError("a fresh cache entry must not be transcoded again")

        monkeypatch.setattr(stems_mod.asyncio, "create_subprocess_exec", no_spawn)
        second = await c.get(url)

    assert second.status_code == 200
    assert second.content == first.content
    assert "immutable" in second.headers["cache-control"]


async def test_a_cache_older_than_its_stem_is_transcoded_again(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir)
    stems = jobs_dir / JOB / "stems"
    stale = stems / "vocals.mp3"
    stale.write_bytes(b"stale")
    old = (stems / "vocals.wav").stat().st_mtime - 100
    os.utime(stale, (old, old))

    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/vocals.mp3")

    assert r.status_code == 200
    assert r.content != b"stale"
    assert stale.read_bytes() == r.content


async def test_a_failed_transcode_is_a_500_and_leaves_no_file(jobs_dir, client):
    skip_without_ffmpeg()
    _make_job(jobs_dir, names=("drums", "bass"))
    (jobs_dir / JOB / "stems" / "vocals.wav").write_bytes(b"RIFF but not audio")

    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/vocals.mp3")

    assert r.status_code == 500
    assert r.json()["detail"] == "mp3 transcode failed"
    leftovers = [p.name for p in (jobs_dir / JOB / "stems").iterdir() if "mp3" in p.name]
    assert leftovers == []


async def test_a_transcode_that_runs_too_long_is_a_504_and_is_cleaned_up(jobs_dir, monkeypatch):
    skip_without_ffmpeg()
    # Ten minutes of stereo: long enough that ffmpeg has opened its temp file
    # and is still encoding when the timeout fires.
    _make_job(jobs_dir, names=(), seconds=600.0)
    src = jobs_dir / JOB / "stems" / "vocals.wav"
    noise = np.random.default_rng(1).uniform(-0.3, 0.3, (SR * 600, 2))
    sf.write(src, noise, SR, subtype="PCM_16")
    monkeypatch.setattr(stems_mod, "TIMEOUT_FFMPEG", 1.0)

    with pytest.raises(HTTPException) as e:
        await stems_mod._ensure_cached_mp3(src)

    assert e.value.status_code == 504
    assert [p.name for p in src.parent.iterdir() if "mp3" in p.name] == []


# --- the master output note ---------------------------------------------------------


def test_master_output_note_round_trips(jobs_dir):
    stems_mod._write_master_output("k1", Loudness(-9.5, -1.2, 6.0))
    assert stems_mod._read_master_output("k1") == Loudness(-9.5, -1.2, 6.0)


def test_master_output_note_missing_or_unreadable_is_none(jobs_dir):
    assert stems_mod._read_master_output("nothing-here") is None
    note = stems_mod._output_note_path("bad-json")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("{not json", encoding="utf-8")
    assert stems_mod._read_master_output("bad-json") is None
    stems_mod._output_note_path("bad-keys").write_text(
        json.dumps({"loudness": 1}), encoding="utf-8"
    )
    assert stems_mod._read_master_output("bad-keys") is None


# --- the rubberband probe -------------------------------------------------------------


def _probe(monkeypatch, result):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(cmd, 0, stdout=result, stderr="")

    monkeypatch.setattr(stems_mod, "_rubberband_cached", None)
    monkeypatch.setattr(stems_mod.subprocess, "run", fake_run)
    return calls


def test_rubberband_is_found_in_the_filter_list_and_probed_once(monkeypatch):
    calls = _probe(monkeypatch, " ... rubberband        A->A       Apply time-stretching\n")
    assert stems_mod._rubberband_available() is True
    assert stems_mod._rubberband_available() is True
    assert len(calls) == 1
    assert "-filters" in calls[0]


@pytest.mark.parametrize(
    "result",
    [
        " ... atempo   A->A   Adjust audio tempo.\n",
        " ... rubberbandish   A->A   not the filter\n",
        OSError("no ffmpeg"),
        subprocess.TimeoutExpired("ffmpeg", 10),
    ],
)
def test_rubberband_is_unavailable_without_the_filter_or_a_working_ffmpeg(monkeypatch, result):
    calls = _probe(monkeypatch, result)
    assert stems_mod._rubberband_available() is False
    assert stems_mod._rubberband_available() is False
    assert len(calls) == 1


# --- _click_lane: grids without beats and renders that fail ------------------------------


def _grid(jobs_dir, beats=(0.5, 1.0, 1.5, 2.0)):
    job = Job(id=JOB, status="done")
    _jobs[job.id] = job
    stems = jobs_dir / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    (stems / "beats.json").write_text(
        json.dumps({"beats": list(beats), "bars": [{"beat": 0, "beats_per_bar": 4}],
                    "duration": 3.0}),
        encoding="utf-8",
    )  # fmt: skip


def test_click_lane_is_none_for_a_grid_without_beats(jobs_dir, monkeypatch):
    _grid(jobs_dir, beats=())
    rendered: list[str] = []
    monkeypatch.setattr(stems_mod, "render_click_wav", lambda *a, **k: rendered.append("c"))
    monkeypatch.setattr(stems_mod, "render_count_in_wav", lambda *a, **k: rendered.append("n"))

    assert stems_mod._click_lane(JOB, True, 1.0, 0, 0.6) is None
    assert stems_mod._click_lane(JOB, False, 1.0, 0, 0.6, count_in_bars=1) is None
    assert rendered == []  # nothing to click to: no render is even attempted


@pytest.mark.parametrize("outcome", ["raise", "none"])
@pytest.mark.parametrize("count_in", [0, 1])
def test_click_lane_is_none_when_its_render_fails(jobs_dir, monkeypatch, outcome, count_in):
    _grid(jobs_dir)

    def render(*_a, **_k):
        if outcome == "raise":
            raise MemoryError("too long")
        return None

    monkeypatch.setattr(stems_mod, "render_click_wav", render)
    monkeypatch.setattr(stems_mod, "render_count_in_wav", render)

    assert stems_mod._click_lane(JOB, True, 1.0, 0, 0.6, count_in_bars=count_in) is None
    assert not list((stems_mod._CLICK_CACHE_DIR).glob("*.wav"))


def test_click_lane_count_in_reuses_a_cached_render(jobs_dir, monkeypatch):
    _grid(jobs_dir)
    first = stems_mod._click_lane(JOB, True, 1.0, 0, 0.6, count_in_bars=1)
    assert first is not None and first.path.is_file()

    def must_not_render(*_a, **_k):
        raise AssertionError("a cached count-in must not be rendered again")

    monkeypatch.setattr(stems_mod, "render_count_in_wav", must_not_render)
    again = stems_mod._click_lane(JOB, True, 1.0, 0, 0.6, count_in_bars=1)
    assert again == first


# --- _expand_mode_lanes: Original when its parts are not all on disk ---------------------


def _pairs(lanes):
    return {(lane.name, lane.path.name, lane.pitch) for lane in lanes}


def test_original_falls_back_to_the_jobs_file_unpitched_when_a_part_is_gone(jobs_dir):
    _make_job(jobs_dir, names=(*ALL, "original"), selected=("vocals",))
    (jobs_dir / JOB / "stems" / "bass.wav").unlink()

    lanes, unpitched = stems_mod._expand_mode_lanes(
        JOB, ["original", "vocals"], [1.0, 1.0], [2, 2], frozenset(), frozenset({"vocals"})
    )

    assert ("original", "original.wav", 0) in _pairs(lanes)
    assert ("vocals", "vocals.wav", 2) in _pairs(lanes)
    assert unpitched == ["original"]


def test_original_whose_parts_are_gone_and_differ_from_the_file_is_a_422(jobs_dir):
    _make_job(jobs_dir, names=(*ALL, "original"), selected=("vocals",))
    (jobs_dir / JOB / "stems" / "bass.wav").unlink()

    # Drums as a lane: Original is no longer the set the job's file holds.
    with pytest.raises(HTTPException) as e:
        stems_mod._expand_mode_lanes(
            JOB,
            ["original", "drums"],
            [1.0, 1.0],
            [0, 0],
            frozenset(),
            frozenset({"vocals", "drums"}),
        )
    assert e.value.status_code == 422


def test_a_removed_stem_in_the_request_is_left_out(jobs_dir):
    _make_job(jobs_dir)

    lanes, _ = stems_mod._expand_mode_lanes(
        JOB,
        ["original", "vocals", "bass"],
        [1.0, 1.0, 1.0],
        [0, 0, 0],
        frozenset({"bass"}),
        frozenset({"vocals"}),
    )

    assert all(lane.path.name != "bass.wav" for lane in lanes)
    assert ("vocals", "vocals.wav", 0) in _pairs(lanes)


def test_original_of_a_job_that_is_not_done_is_a_404(jobs_dir):
    _make_job(jobs_dir, status="separating")

    with pytest.raises(HTTPException) as e:
        stems_mod._expand_mode_lanes(
            JOB, ["original"], [1.0], [0], frozenset(), frozenset({"vocals"})
        )
    assert e.value.status_code == 404
    assert e.value.detail == "job not ready"
