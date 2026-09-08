"""Transpose on export (#592).

The studio shifts pitch in the browser with a SoundTouch AudioWorklet. The
server has no such stage, so an export came out in the original key however the
mixer was set. These pin the rules the export now follows, and in particular the
two it must never break: drums are not resampled, and an export never silently
returns the original key when it was asked for a different one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.stems import (
    PITCH_MAX,
    PITCH_MIN,
    _expand_render_lanes,
    _mixdown_cache_key,
    _parse_pitches,
    _pitch_filter,
    effective_pitch,
)
from app.core.models import Job
from app.core.registry import _jobs

JOB = "abcdefabcdef"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.api import stems as stems_mod

    monkeypatch.setattr(stems_mod, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(stems_mod, "_MIXDOWN_CACHE_DIR", tmp_path / "cache" / "mixdown")
    monkeypatch.setattr(stems_mod, "_CLICK_CACHE_DIR", tmp_path / "cache" / "click")
    from app.main import app

    return TestClient(app)


def _setup_job(tmp_path, names=("vocals", "drums", "bass", "guitar", "piano", "other")):
    job = Job(id=JOB)
    job.status = "done"
    _jobs[job.id] = job
    stems = tmp_path / JOB / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for n in names:
        (stems / f"{n}.wav").write_bytes(b"RIFF")
    return stems


# ─── The rule that matters most ──────────────────────────────────────────


def test_drums_are_never_transposed_whatever_is_asked():
    """Resampling a snare does not move it to another key, it makes it a
    different drum. static/js/pitchBus.js refuses this on the playback side, so
    honouring it here would export something the studio will not play."""
    for st in (-6, -1, 1, 6):
        assert effective_pitch("drums", st) == 0
    assert _parse_pitches("4,4", ["vocals", "drums"]) == [4, 0]


def test_every_other_lane_is_shifted_as_asked():
    for name in ("vocals", "bass", "guitar", "piano", "other", "original"):
        assert effective_pitch(name, 3) == 3
        assert effective_pitch(name, -3) == -3


def test_a_shift_beyond_the_range_is_clamped_not_honoured():
    assert effective_pitch("vocals", 99) == PITCH_MAX
    assert effective_pitch("vocals", -99) == PITCH_MIN


# ─── Parsing ─────────────────────────────────────────────────────────────


def test_no_pitches_means_no_transpose_so_old_clients_are_unchanged():
    """A caller that predates #592 omits the parameter entirely. It must keep
    its exact behaviour rather than having to opt out of a new feature."""
    assert _parse_pitches("", ["vocals", "drums"]) == [0, 0]


def test_pitches_must_line_up_with_stems():
    with pytest.raises(Exception) as e:
        _parse_pitches("1", ["vocals", "drums"])
    assert e.value.status_code == 422


@pytest.mark.parametrize("bad", ["7,0", "-7,0", "x,0", "1.5,0"])
def test_a_pitch_that_is_not_a_whole_semitone_in_range_is_refused(bad):
    with pytest.raises(Exception) as e:
        _parse_pitches(bad, ["vocals", "drums"])
    assert e.value.status_code == 422


def test_the_filter_is_a_frequency_ratio_and_absent_at_zero():
    assert _pitch_filter(0) == ""
    assert _pitch_filter(12).startswith("rubberband=pitch=2.0")
    assert _pitch_filter(-12).startswith("rubberband=pitch=0.5")


# ─── The "original" lane ─────────────────────────────────────────────────


def test_the_original_lane_is_rebuilt_from_parts_so_its_drums_survive(tmp_path):
    """ "original" is the complement lane and usually contains drums, so shifting
    that one file would resample them. Playback rebuilds the lane from
    components for exactly this reason (static/js/playbackStems.js); the export
    has to do the same or a transposed mix comes back with pitched drums."""
    _setup_job(tmp_path)
    # A job that shows an "original" lane has the rendered file too; the
    # reconstruction is about not *shifting* that file, not about its absence.
    (tmp_path / JOB / "stems" / "original.wav").write_bytes(b"RIFF")
    from app.api import stems as stems_mod

    stems_mod.JOBS_DIR = tmp_path
    lanes, unpitched = _expand_render_lanes(JOB, ["vocals", "original"], [1.0, 1.0], [2, 2])

    assert unpitched == []
    # vocals, then the five components that make up the complement.
    assert len(lanes) == 6
    by_name = {lane.path.stem: lane.pitch for lane in lanes}
    assert by_name["vocals"] == 2
    assert by_name["drums"] == 0, "the complement's drums must not be resampled"
    for n in ("bass", "guitar", "piano", "other"):
        assert by_name[n] == 2


def test_an_unshifted_original_lane_is_left_as_one_file(tmp_path):
    """Nothing to take apart when nothing is being shifted -- the cheap path
    stays cheap, and an export with no transpose renders exactly as it always
    did."""
    _setup_job(tmp_path)
    from app.api import stems as stems_mod

    stems_mod.JOBS_DIR = tmp_path
    (tmp_path / JOB / "stems" / "original.wav").write_bytes(b"RIFF")
    lanes, unpitched = _expand_render_lanes(JOB, ["vocals", "original"], [1.0, 1.0], [0, 0])
    assert len(lanes) == 2
    assert unpitched == []


def test_a_lane_the_job_never_produced_is_refused_whether_or_not_it_is_shifted(tmp_path):
    """Whether a lane exists is a property of the job, not of the request.

    Found by exporting against a real library: "original" only exists when the
    user picked a strict subset, and the reconstruction path was happy to
    synthesise one from components. So the same URL 404'd untransposed and
    rendered when transposed -- validity depending on the pitch attached to it.
    """
    _setup_job(tmp_path)  # no original.wav
    from app.api import stems as stems_mod

    stems_mod.JOBS_DIR = tmp_path
    for pitch in (0, 2):
        with pytest.raises(Exception) as e:
            _expand_render_lanes(JOB, ["vocals", "original"], [1.0, 1.0], [0, pitch])
        assert e.value.status_code == 404, f"pitch={pitch} disagreed with the other"


def test_an_original_lane_that_cannot_be_taken_apart_keeps_its_key(tmp_path):
    """An older job kept only the rendered mix. Playback makes the same
    conservative choice: melodic content stays in the old key rather than the
    drums inside it being resampled."""
    _setup_job(tmp_path, names=("vocals", "original"))
    from app.api import stems as stems_mod

    stems_mod.JOBS_DIR = tmp_path
    lanes, unpitched = _expand_render_lanes(JOB, ["vocals", "original"], [1.0, 1.0], [2, 2])
    assert unpitched == ["original"]
    assert len(lanes) == 2
    assert {lane.path.stem: lane.pitch for lane in lanes}["original"] == 0


# ─── Cache ───────────────────────────────────────────────────────────────


def test_a_transposed_export_never_serves_the_untransposed_render():
    args = (JOB, "wav", ["vocals"], [1.0], None, None, None)
    assert _mixdown_cache_key(*args) != _mixdown_cache_key(*args, [2])
    assert _mixdown_cache_key(*args, [2]) != _mixdown_cache_key(*args, [-2])


def test_an_untransposed_export_keeps_the_cache_entry_it_already_had():
    """Every export rendered before #592 must still hit its existing entry."""
    args = (JOB, "wav", ["vocals"], [1.0], None, None, None)
    assert _mixdown_cache_key(*args) == _mixdown_cache_key(*args, None)
    assert _mixdown_cache_key(*args) == _mixdown_cache_key(*args, [0])


# ─── Endpoint ────────────────────────────────────────────────────────────


def test_asking_for_a_transpose_an_ffmpeg_cannot_do_says_so(client, tmp_path, monkeypatch):
    """librubberband is GPL and plenty of builds omit it. Returning the original
    key would look exactly like the bug this feature exists to fix, so the
    export refuses and says why instead."""
    from app.api import stems as stems_mod

    _setup_job(tmp_path)
    monkeypatch.setattr(stems_mod, "_rubberband_available", lambda: False)
    r = client.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals&gains=1.0&pitches=2")
    assert r.status_code == 422
    assert "transpose" in r.json()["detail"]


def test_an_export_with_no_transpose_never_probes_for_rubberband(client, tmp_path, monkeypatch):
    """A build with no pitch support must keep exporting normally."""
    from app.api import stems as stems_mod

    _setup_job(tmp_path)
    called = []
    monkeypatch.setattr(stems_mod, "_rubberband_available", lambda: called.append(1) or False)
    r = client.get(f"/api/jobs/{JOB}/mixdown.wav?stems=vocals&gains=1.0")
    assert r.status_code != 422
    assert called == [], "the probe ran for an export that was never transposed"


def test_a_drums_only_transpose_is_not_treated_as_a_transpose(client, tmp_path, monkeypatch):
    """Asking to shift drums resolves to no shift at all, so the export must not
    then refuse itself on a build that cannot pitch-shift."""
    from app.api import stems as stems_mod

    _setup_job(tmp_path)
    monkeypatch.setattr(stems_mod, "_rubberband_available", lambda: False)
    r = client.get(f"/api/jobs/{JOB}/mixdown.wav?stems=drums&gains=1.0&pitches=3")
    assert r.status_code != 422
