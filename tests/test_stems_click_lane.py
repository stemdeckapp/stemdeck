"""The export's click lane, and how the vocal split changes what "Original" is.

_click_lane is the only path by which a click reaches an exported file: the
metronome is synthesised in the browser during playback and never touches the
server, so an export that wants one has to render an equivalent WAV here. Every
way that can come back empty was uncovered, and each returns None -- so a
failure is an export silently missing its click rather than an error.
"""

from __future__ import annotations

import json

import pytest

import app.api.stems as stems_mod
from app.core.models import Job
from app.core.registry import _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    yield
    _jobs.clear()


def _register(job_id: str) -> Job:
    """_expand_render_lanes validates each lane against the job, not just the
    filesystem: an "original" lane a job never produced must not be exportable."""
    job = Job(id=job_id)
    job.status = "done"
    _jobs[job_id] = job
    return job


@pytest.fixture
def job_with_grid(tmp_path, monkeypatch):
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "click-cache")
    (tmp_path / "click-cache").mkdir()
    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)

    def _write(**over):
        grid = {
            "version": 1,
            "beats": [i * 0.5 for i in range(16)],
            "bars": [{"beat": 0, "beats_per_bar": 4}],
            "duration": 8.0,
        }
        grid.update(over)
        (stems / "beats.json").write_text(json.dumps(grid), encoding="utf-8")

    _write()
    return "abcdefabcdef", stems, _write


def _lane(job_id, **kw):
    kw.setdefault("enabled", True)
    kw.setdefault("multiplier", 1.0)
    kw.setdefault("accent_mode", 0)
    kw.setdefault("gain", 1.0)
    return stems_mod._click_lane(job_id, **kw)


# --------------------------------------------------------------------------
# when there is nothing to render
# --------------------------------------------------------------------------


def test_no_lane_when_the_click_is_off_and_no_count_in_was_asked_for(job_with_grid):
    """The common case -- most exports want neither."""
    job_id, _, _ = job_with_grid

    assert _lane(job_id, enabled=False, count_in_bars=0) is None


def test_no_lane_for_a_job_with_no_beat_grid(tmp_path, monkeypatch):
    """An older job separated before beat detection existed, or one whose grid
    stage failed. The export still has to succeed, just without a click."""
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    (tmp_path / "abcdefabcdef" / "stems").mkdir(parents=True)

    assert _lane("abcdefabcdef") is None


def test_no_lane_when_the_grid_has_no_beats_in_it(job_with_grid):
    """A grid file that exists but is empty would render a silent click track
    and add an input ffmpeg has to mix for nothing."""
    job_id, _, write = job_with_grid
    write(beats=[])

    assert _lane(job_id) is None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def test_a_click_lane_is_rendered_and_reused(job_with_grid):
    """The file is cached under the job's id and settings, so a second export
    with the same options does not re-render it."""
    job_id, _, _ = job_with_grid

    first = _lane(job_id)
    assert first is not None and first.path.is_file()
    stamp = first.path.stat().st_mtime_ns

    second = _lane(job_id)
    assert second is not None
    assert second.path == first.path
    assert second.path.stat().st_mtime_ns == stamp, "the click was re-rendered"


def test_a_count_in_lane_reports_the_lead_it_baked_in(job_with_grid):
    """The caller delays every stem by exactly this, so the file and the stems
    share one origin."""
    job_id, _, _ = job_with_grid

    lane = _lane(job_id, enabled=False, count_in_bars=1)

    assert lane is not None
    assert lane.lead_in > 0
    assert lane.count_in is True


def test_a_plain_click_lane_has_no_lead_in(job_with_grid):
    """It spans the whole track in source time and is lined up by the region
    trim instead."""
    job_id, _, _ = job_with_grid

    lane = _lane(job_id)

    assert lane is not None
    assert lane.lead_in == 0.0
    assert lane.count_in is False


def test_the_click_gain_is_clamped_to_something_sane(job_with_grid):
    """A gain out of range would either silence the click or clip the mix."""
    job_id, _, _ = job_with_grid

    assert _lane(job_id, gain=99.0).gain == 4.0
    assert _lane(job_id, gain=-5.0).gain == 0.0


def test_a_click_render_that_raises_costs_the_click_not_the_export(
    job_with_grid, monkeypatch, caplog
):
    """The export is otherwise fine; failing it over a metronome would lose the
    user a multi-minute render."""
    job_id, _, _ = job_with_grid

    def _boom(*a, **kw):
        raise RuntimeError("out of disk")

    monkeypatch.setattr(stems_mod, "render_click_wav", _boom)

    assert _lane(job_id) is None
    assert "click render failed" in caplog.text


def test_a_count_in_render_that_raises_costs_the_count_in_not_the_export(
    job_with_grid, monkeypatch, caplog
):
    job_id, _, _ = job_with_grid

    def _boom(*a, **kw):
        raise RuntimeError("out of disk")

    monkeypatch.setattr(stems_mod, "render_count_in_wav", _boom)

    assert _lane(job_id, enabled=False, count_in_bars=1) is None
    assert "count-in render failed" in caplog.text


def test_a_renderer_that_declines_yields_no_lane(job_with_grid, monkeypatch):
    """render_click_wav returns None when the grid is too short to click to."""
    job_id, _, _ = job_with_grid
    monkeypatch.setattr(stems_mod, "render_click_wav", lambda *a, **kw: None)

    assert _lane(job_id) is None


def test_a_count_in_renderer_that_declines_yields_no_lane(job_with_grid, monkeypatch):
    job_id, _, _ = job_with_grid
    monkeypatch.setattr(stems_mod, "render_count_in_wav", lambda *a, **kw: None)

    assert _lane(job_id, enabled=False, count_in_bars=1) is None


def test_the_rendered_click_is_never_evicted_by_its_own_arrival(job_with_grid, monkeypatch):
    """A render larger than the cache budget used to evict itself the instant it
    was written, and ffmpeg was then handed a missing -i (#512)."""
    job_id, _, _ = job_with_grid
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_BYTES", 1)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_MAX_FILES", 1)

    lane = _lane(job_id)

    assert lane is not None
    assert lane.path.is_file(), "the click evicted itself before ffmpeg could read it"


# --------------------------------------------------------------------------
# the split vocals stand in for the base vocals lane
# --------------------------------------------------------------------------


def test_a_complete_split_makes_vocals_part_of_the_selection(tmp_path, monkeypatch):
    """Exporting lead+backing already contains everything vocals held, so
    vocals must not also be summed into the Original complement -- that would
    play the vocal twice. Mirrors buildPlaybackStems on the client."""
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums", "lead_vocals", "backing_vocals"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    _register("abcdefabcdef")

    lanes, unpitched = stems_mod._expand_render_lanes(
        "abcdefabcdef",
        ["lead_vocals", "backing_vocals", "drums"],
        [1.0, 1.0, 1.0],
        [0, 0, 0],
    )

    assert {lane.path.stem for lane in lanes} == {"lead_vocals", "backing_vocals", "drums"}


def test_only_one_half_of_a_split_does_not_stand_in_for_vocals(tmp_path, monkeypatch):
    """Half a split is not the vocal, so the base lane still belongs in the
    complement."""
    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    for name in ("vocals", "drums", "lead_vocals"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    _register("abcdefabcdef")

    lanes, _ = stems_mod._expand_render_lanes(
        "abcdefabcdef", ["lead_vocals", "drums"], [1.0, 1.0], [0, 0]
    )

    assert {lane.path.stem for lane in lanes} == {"lead_vocals", "drums"}
