"""Snapping the coarse grid onto the real transients, and the guards around it.

_refine_beats is what turns beat_track's 512-sample hop grid into something a
click can play against: the tracker's quantisation error changes from beat to
beat, so the click jitters against a drummer who is perfectly steady. That
jitter is what a listener hears as flam, and none of the snapping or its
rejection rules were covered.

Every guard here falls back to the coarse grid rather than raising, so getting
one wrong produces a worse grid and no failure anywhere.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import BEATGRID_REFINE_HOP
from app.pipeline import beatgrid as bg

SR = 44100
FRAME = BEATGRID_REFINE_HOP / SR


def _env_with_peaks(frames: int, peaks, height=1.0, floor=0.0):
    arr = np.full(frames, floor, dtype=float)
    for p in peaks:
        arr[p] = height
    return arr


# --------------------------------------------------------------------------
# the guards that fall back to the coarse grid
# --------------------------------------------------------------------------


def test_no_beats_to_refine_is_answered_immediately():
    out, refined = bg._refine_beats(None, SR, [], beat_interval=0.5, env=np.zeros(100))

    assert out == []
    assert refined == 0


def test_an_envelope_too_short_to_search_leaves_the_grid_alone():
    coarse = [0.5, 1.0, 1.5]

    out, refined = bg._refine_beats(None, SR, coarse, 0.5, env=np.zeros(2))

    assert out == coarse
    assert refined == 0


def test_an_envelope_with_nothing_above_the_threshold_leaves_the_grid_alone():
    """Nothing played anywhere near these beats, so there is no transient to
    snap to and interpolation is the better answer."""
    coarse = [0.1, 0.2, 0.3, 0.4]

    out, refined = bg._refine_beats(None, SR, coarse, 0.1, env=np.zeros(500))

    assert out == coarse
    assert refined == 0


def test_a_single_anchor_is_not_enough_to_redefine_a_grid():
    """One snapped beat cannot define a grid; the coarse result is trusted."""
    peak_frame = 100
    env = _env_with_peaks(600, [peak_frame])
    # Only the first beat sits near the single transient.
    coarse = [peak_frame * FRAME, 5.0, 9.0]

    out, refined = bg._refine_beats(None, SR, coarse, 0.02, env=env)

    assert out == coarse
    assert refined <= 1


# --------------------------------------------------------------------------
# snapping
# --------------------------------------------------------------------------


def test_beats_are_moved_onto_the_transients_they_belong_to():
    """The whole point: the tracker's hop grid carries up to ~12 ms of error,
    and the error changes from beat to beat."""
    peaks = [100, 200, 300, 400]
    env = _env_with_peaks(600, peaks)
    # Each beat a couple of frames off its transient, in alternating directions.
    coarse = [(p + off) * FRAME for p, off in zip(peaks, [2, -2, 1, -1], strict=True)]

    out, refined = bg._refine_beats(None, SR, coarse, 0.1, env=env)

    assert refined == len(peaks)
    for p, t in zip(peaks, out, strict=True):
        assert abs(t - p * FRAME) < FRAME, "a beat did not land on its transient"


def test_snapping_reaches_sub_frame_precision():
    """Parabolic interpolation over the peak's neighbours: a beat between two
    frames must not be quantised back onto one of them."""
    env = np.zeros(600)
    # An asymmetric peak, so the true maximum lies between frames 200 and 201.
    env[199], env[200], env[201] = 0.5, 1.0, 0.9
    coarse = [200 * FRAME, 200 * FRAME + 0.0001]

    out, _ = bg._refine_beats(None, SR, coarse, 0.1, env=env)

    offsets = [t / FRAME - 200 for t in out]
    assert any(abs(o) > 1e-9 for o in offsets), "the snap was quantised to the frame grid"
    assert all(abs(o) <= 0.5 for o in offsets)


def test_a_peak_too_far_away_to_be_this_beat_is_rejected():
    """The search window is deliberately wider than the move limit so argmax can
    find the genuine peak among nearby artifacts; this check throws the result
    out when what it found was too far away."""
    env = _env_with_peaks(4000, [2000])
    # A beat nowhere near that transient, with a tight interval so max_move is small.
    coarse = [10 * FRAME, 20 * FRAME, 30 * FRAME]

    out, refined = bg._refine_beats(None, SR, coarse, 0.001, env=env)

    assert out == coarse
    assert refined == 0


# --------------------------------------------------------------------------
# _enforce_grid_consistency
# --------------------------------------------------------------------------


def test_a_grid_too_short_to_have_an_interior_is_left_alone():
    assert bg._enforce_grid_consistency([0.0, 0.5]) == ([0.0, 0.5], 0)


def test_a_collapsed_grid_is_left_alone():
    """Zero median interval means every tolerance is zero and every beat looks
    like an outlier."""
    assert bg._enforce_grid_consistency([1.0, 1.0, 1.0, 1.0]) == ([1.0, 1.0, 1.0, 1.0], 0)


def test_a_steady_grid_is_not_corrected():
    beats = [i * 0.5 for i in range(10)]

    out, corrected = bg._enforce_grid_consistency(beats)

    assert corrected == 0
    assert out == pytest.approx(beats)


def test_an_interior_beat_off_the_grid_is_pulled_back():
    beats = [i * 0.5 for i in range(10)]
    beats[4] += 0.2  # well outside the tolerance

    out, corrected = bg._enforce_grid_consistency(beats)

    assert corrected >= 1
    assert out[4] == pytest.approx(2.0, abs=0.01)


def test_a_first_beat_off_the_grid_is_extrapolated_from_the_two_inside_it():
    """Endpoints have one neighbour, so the midpoint rule cannot reach them."""
    beats = [i * 0.5 for i in range(10)]
    # Outside the endpoint tolerance (interval * 0.10 = 0.05) but small enough
    # that the neighbour's own midpoint prediction still holds -- a larger shift
    # drags out[1] with it, and the extrapolation then agrees with the corruption.
    beats[0] -= 0.08

    out, corrected = bg._enforce_grid_consistency(beats)

    assert corrected >= 1
    assert out[0] == pytest.approx(0.0, abs=0.01)


def test_a_last_beat_off_the_grid_is_extrapolated_too():
    beats = [i * 0.5 for i in range(10)]
    beats[-1] += 0.08

    out, corrected = bg._enforce_grid_consistency(beats)

    assert corrected >= 1
    assert out[-1] == pytest.approx(4.5, abs=0.01)


# --------------------------------------------------------------------------
# _extend_to_track_edges
# --------------------------------------------------------------------------


def test_an_irregular_grid_is_not_extended():
    """Extrapolating from a tempo that is not steady would place beats the
    music does not have, at the two places a listener notices most."""
    rng = np.random.default_rng(0)
    ragged = np.cumsum(rng.uniform(0.2, 1.5, size=30)).tolist()

    out, head, tail = bg._extend_to_track_edges(ragged, 60.0, bg._interval_spread(ragged))

    assert (head, tail) == (0, 0)
    assert out == ragged


def test_a_grid_too_short_to_extrapolate_from_is_left_alone():
    out, head, tail = bg._extend_to_track_edges([1.0, 1.5], 10.0, 0.0)

    assert (head, tail) == (0, 0)


def test_a_collapsed_grid_is_not_extended():
    """A zero interval would loop forever adding beats at the same instant."""
    out, head, tail = bg._extend_to_track_edges([2.0] * 8, 10.0, 0.0)

    assert (head, tail) == (0, 0)


def test_a_steady_grid_is_extended_to_both_edges():
    """Detection only places beats where there is evidence, so an intro with no
    drums leaves the click silent exactly where a player counts themselves in."""
    beats = [2.0 + i * 0.5 for i in range(20)]

    out, head, tail = bg._extend_to_track_edges(beats, 20.0, 0.0)

    assert head > 0 and tail > 0
    assert out[0] < beats[0]
    assert out[-1] > beats[-1]
    assert out == sorted(out)
    assert out[0] >= 0.0
    assert out[-1] <= 20.0
