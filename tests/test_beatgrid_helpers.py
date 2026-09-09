"""The numeric helpers the beat grid is assembled from.

test_pipeline_beatgrid.py drives compute_beat_grid end to end against rendered
audio, which is the right shape for the whole stage but leaves the individual
helpers' guard clauses unreached -- the "too few beats to judge", "no onsets at
all", "median interval is zero" cases that only arise on degenerate input.

Degenerate input is exactly what these see in practice: a track with a drum-free
breakdown, a bare repeated kick, an intro with no transients. Each helper here
documents a real tracker failure it exists to prevent, and none of those
failures raises -- they produce a grid that is wrong, which the player then
clicks to.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import BEATGRID_REFINE_HOP
from app.pipeline import beatgrid as bg

# --------------------------------------------------------------------------
# _interval_spread -- the regularity measure
# --------------------------------------------------------------------------


def test_a_grid_too_short_to_judge_reports_no_spread():
    """Fewer than five beats cannot support a quartile, and reporting a large
    spread would make a short track look unstable."""
    assert bg._interval_spread([0.0, 0.5, 1.0]) == 0.0
    assert bg._interval_spread([]) == 0.0


def test_a_perfectly_steady_grid_has_no_spread():
    beats = [i * 0.5 for i in range(20)]

    assert bg._interval_spread(beats) == pytest.approx(0.0, abs=1e-9)


def test_a_grid_with_a_drum_free_hole_still_reads_as_steady():
    """The coefficient of variation does not survive this: one 5-second hole
    produces a single enormous interval that drags cv to 0.5 on a track whose
    pulse never wavers. IQR ignores the tails."""
    beats = [i * 0.5 for i in range(20)]
    beats += [beats[-1] + 5.0]  # a breakdown with no detected beats
    beats += [beats[-1] + 0.5 * i for i in range(1, 20)]

    assert bg._interval_spread(beats) < 0.1


def test_a_genuinely_unstable_grid_reads_as_unstable():
    rng = np.random.default_rng(0)
    beats = np.cumsum(rng.uniform(0.2, 1.2, size=40)).tolist()

    assert bg._interval_spread(beats) > 0.2


def test_a_collapsed_grid_reports_infinite_spread():
    """Every beat at the same instant gives a zero median interval; dividing by
    it would be a ZeroDivisionError or a silent nan."""
    assert bg._interval_spread([1.0] * 10) == float("inf")


# --------------------------------------------------------------------------
# _onset_support -- how well a candidate grid lines up with the audio
# --------------------------------------------------------------------------


def _env(length=400, peaks=(), height=1.0):
    arr = np.zeros(length)
    for p in peaks:
        arr[p] = height
    return arr


def test_support_is_zero_when_there_is_nothing_to_measure():
    assert bg._onset_support(_env(2), 44100, [0.1, 0.2]) == 0.0
    assert bg._onset_support(_env(400), 44100, []) == 0.0


def test_a_grid_landing_on_the_transients_scores_higher_than_one_that_misses():
    """This comparison is the whole basis of the phase correction below."""
    sr = 44100
    frame_dur = BEATGRID_REFINE_HOP / sr
    hit_frames = [50, 100, 150, 200]
    env = _env(400, peaks=hit_frames)
    on_beat = [f * frame_dur for f in hit_frames]
    off_beat = [(f + 25) * frame_dur for f in hit_frames]

    assert bg._onset_support(env, sr, on_beat) > bg._onset_support(env, sr, off_beat)


def test_a_beat_a_millisecond_off_a_transient_still_counts():
    """Sampled as a local maximum over a few frames, so detection jitter does
    not read as a missed beat."""
    sr = 44100
    frame_dur = BEATGRID_REFINE_HOP / sr
    env = _env(400, peaks=[100])

    exact = bg._onset_support(env, sr, [100 * frame_dur])
    nudged = bg._onset_support(env, sr, [100 * frame_dur + 0.001])

    assert nudged == pytest.approx(exact)


# --------------------------------------------------------------------------
# _correct_grid_phase -- tempo right, phase wrong
# --------------------------------------------------------------------------


def test_a_grid_too_short_to_judge_is_left_alone():
    beats = [0.0, 0.5, 1.0]

    out, shifted = bg._correct_grid_phase(beats, _env(), 44100, 60.0)

    assert out == beats
    assert shifted is False


def test_a_collapsed_grid_is_left_alone():
    """A zero median interval means half of it is zero, and shifting by nothing
    would loop forever on the same answer."""
    beats = [1.0] * 12

    out, shifted = bg._correct_grid_phase(beats, _env(), 44100, 60.0)

    assert out == beats
    assert shifted is False


def test_a_grid_whose_shift_would_fall_off_the_end_is_left_alone():
    """Shifting past the track duration drops beats; below eight there is not
    enough left to compare against."""
    beats = [i * 0.5 for i in range(9)]

    out, shifted = bg._correct_grid_phase(beats, _env(), 44100, beats[1])

    assert out == beats
    assert shifted is False


def test_a_grid_sitting_in_the_gaps_is_shifted_onto_the_beat():
    """The failure this exists for: tempo right, phase wrong, so every click
    lands between the hits. Seen on a bare repeated-kick pattern where the
    detected grid had zero onset support and the half-beat shift had all of it."""
    sr = 44100
    frame_dur = BEATGRID_REFINE_HOP / sr
    period_frames = 40
    hits = list(range(40, 400, period_frames))
    env = _env(500, peaks=hits)
    # A grid offset by half a period -- exactly between every transient.
    off = [(f + period_frames / 2) * frame_dur for f in hits]

    out, shifted = bg._correct_grid_phase(off, env, sr, 10.0)

    assert shifted is True
    assert out != off


def test_a_grid_already_on_the_beat_is_not_shifted():
    """A correct grid on real music still has off-beat support from hi-hats on
    the eighths, so the shift has to be a large improvement, not a marginal
    one."""
    sr = 44100
    frame_dur = BEATGRID_REFINE_HOP / sr
    period_frames = 40
    hits = list(range(40, 400, period_frames))
    # Strong on the beat, weaker eighths in between -- ordinary drum programming.
    env = _env(500, peaks=hits, height=1.0)
    for f in range(40 + period_frames // 2, 400, period_frames):
        env[f] = 0.4
    on = [f * frame_dur for f in hits]

    out, shifted = bg._correct_grid_phase(on, env, sr, 10.0)

    assert shifted is False
    assert out == on


# --------------------------------------------------------------------------
# _fill_interior_gaps -- the drum-free breakdown
# --------------------------------------------------------------------------


def test_a_grid_too_short_to_have_an_interior_is_left_alone():
    beats = [0.0, 0.5, 1.0]

    out, inserted = bg._fill_interior_gaps(beats)

    assert out == beats
    assert inserted == 0


def test_a_hole_that_divides_cleanly_is_filled():
    """A metronome that stops for five seconds reads as broken, and a click is
    the one thing a player needs through a breakdown."""
    before = [i * 0.5 for i in range(8)]
    after = [before[-1] + 2.5 + i * 0.5 for i in range(8)]
    beats = before + after

    out, inserted = bg._fill_interior_gaps(beats)

    assert inserted > 0
    assert len(out) == len(beats) + inserted
    # Both ends of the gap are real detected beats, so nothing drifts outside it.
    assert out[0] == beats[0]
    assert out[-1] == beats[-1]
    assert out == sorted(out)


def test_a_steady_grid_gains_nothing():
    beats = [i * 0.5 for i in range(20)]

    out, inserted = bg._fill_interior_gaps(beats)

    assert inserted == 0
    assert out == beats


def test_a_gap_that_does_not_divide_cleanly_is_left_alone():
    """Filling it would place beats the music does not have; the editor is the
    right tool for an ambiguous hole."""
    beats = [i * 0.5 for i in range(8)]
    beats += [beats[-1] + 1.17]  # not a multiple of the local period
    beats += [beats[-1] + 0.5 * i for i in range(1, 8)]

    out, inserted = bg._fill_interior_gaps(beats)

    assert inserted == 0
    assert out == beats


# --------------------------------------------------------------------------
# _downbeats_to_bars
# --------------------------------------------------------------------------


def test_no_downbeats_means_no_bars():
    """Without bar marks the metronome accents nothing rather than guessing 4/4
    on a track that might be in 3."""
    assert bg._downbeats_to_bars([i * 0.5 for i in range(16)], []) == []


def test_evenly_spaced_downbeats_become_one_bar_mark():
    beats = [i * 0.5 for i in range(17)]
    downbeats = [beats[i] for i in range(0, 17, 4)]

    bars = bg._downbeats_to_bars(beats, downbeats)

    assert bars, "no bar mark was produced"
    assert bars[0]["beat"] == 0
    assert bars[0]["beats_per_bar"] == 4


def test_a_time_signature_change_produces_a_second_mark():
    """A single mark would make the metronome accent the wrong beat for the
    whole second half. The first mark is the track's *modal* meter, so the
    fixture gives 4/4 the clear majority and then a sustained run of 3."""
    beats = [i * 0.5 for i in range(45)]
    downbeats = [beats[i] for i in (0, 4, 8, 12, 16, 20, 24, 28, 31, 34, 37, 40)]

    bars = bg._downbeats_to_bars(beats, downbeats)

    assert len(bars) >= 2
    assert [b["beats_per_bar"] for b in bars][:2] == [4, 3]


def test_one_slipped_downbeat_does_not_rewrite_the_meter():
    """Emitting a mark per observation gave 13 marks on a plain 4/4 punk track;
    a change is only committed once the new length repeats."""
    beats = [i * 0.5 for i in range(45)]
    # One bar of 3 in the middle of otherwise steady 4/4.
    downbeats = [beats[i] for i in (0, 4, 8, 12, 15, 19, 23, 27, 31, 35, 39)]

    bars = bg._downbeats_to_bars(beats, downbeats)

    assert [b["beats_per_bar"] for b in bars] == [4]


# --------------------------------------------------------------------------
# _sanitize -- what reaches the grid from a detector
# --------------------------------------------------------------------------


def test_beats_are_sorted_deduplicated_and_clipped_to_the_track():
    out = bg._sanitize([2.0, 0.5, 0.5, -1.0, 99.0, 1.0], duration=3.0)

    assert out == sorted(out)
    assert len(set(out)) == len(out)
    assert all(0 <= t <= 3.0 for t in out)


def test_an_absent_grid_yields_nothing():
    """np.asarray(None) is a nan, which the finite filter removes. (A detector
    is only ever expected to hand over numbers, so no wider type guard exists
    here -- this covers the shape the callers can actually produce.)"""
    assert bg._sanitize(None, 10.0) == []
    assert bg._sanitize([], 10.0) == []


def test_non_finite_beat_times_are_dropped():
    out = bg._sanitize([0.5, float("nan"), 1.0, float("inf")], duration=10.0)

    assert out == [0.5, 1.0]


# --------------------------------------------------------------------------
# _fine_onsets
# --------------------------------------------------------------------------


def test_an_envelope_too_short_to_pick_from_yields_no_onsets():
    assert bg._fine_onsets(np.zeros(2), 44100) == []


def test_the_transients_in_an_envelope_are_found():
    """The onsets are what the grid snaps to, so they have to land on the
    peaks rather than near them."""
    sr = 44100
    frame_dur = BEATGRID_REFINE_HOP / sr
    env = np.zeros(2000)
    hits = list(range(100, 1900, 150))
    env[hits] = 1.0

    out = bg._fine_onsets(env, sr)

    assert out, "no onsets were picked from a clearly peaked envelope"
    assert out == sorted(out)
    # Every real transient is represented. (The converse is not asserted: the
    # picker also reports the leading frame of a mostly-silent envelope, which
    # costs nothing -- the grid snaps to the nearest onset, not to all of them.)
    for h in hits:
        assert any(abs(h * frame_dur - t) < 0.05 for t in out), f"transient at {h} was missed"


def test_only_the_strongest_onsets_survive_the_cap():
    """The cap bounds what is written to beats.json; dropping weakest-first
    keeps the transients the grid actually needs."""
    rng = np.random.default_rng(0)
    env = np.zeros(4000)
    positions = list(range(20, 3980, 20))
    env[positions] = rng.uniform(0.5, 1.0, size=len(positions))

    out = bg._fine_onsets(env, 44100, limit=10)

    assert len(out) <= 10
    assert out == sorted(out)
