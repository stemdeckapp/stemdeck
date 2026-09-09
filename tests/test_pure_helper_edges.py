"""Edge cases in the pure helpers behind the click track, section labels and
search results.

None of these needs a subprocess, a model or a socket -- they are ordinary
functions whose degenerate inputs simply were not being passed. Each guard here
exists because the alternative is a wrong answer rather than an exception: a
grouping that puts accents on beats the user never asked for, a section label
taken from the wrong span, a thumbnail that costs 720p of bandwidth to draw at
64 pixels.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.pipeline import click_render as cr
from app.pipeline import search as search_mod
from app.pipeline import section_refine as sr

# --------------------------------------------------------------------------
# click_render: grouping
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -4])
def test_a_bar_with_no_beats_has_no_grouping(bad):
    """Indexing a bar of zero beats would divide by zero downstream."""
    assert cr.default_grouping(bad) == []
    assert cr.normalise_grouping([2, 2], bad) == []


def test_odd_meters_lead_with_the_long_group():
    """Take Five is 3+2, and 7/8 is more often 3+2+2 than 2+2+3."""
    assert cr.default_grouping(5) == [3, 2]
    assert cr.default_grouping(7) == [3, 2, 2]


def test_compound_meters_are_felt_in_threes():
    assert cr.default_grouping(6) == [3, 3]
    assert cr.default_grouping(9) == [3, 3, 3]
    assert cr.default_grouping(12) == [3, 3, 3, 3]


@pytest.mark.parametrize("simple", [1, 2, 3, 4, 8])
def test_simple_meters_stay_one_group(simple):
    """4/4 does carry a real secondary stress on beat 3, but turning that on by
    default would change the most common meter in the app for every existing
    user."""
    assert cr.default_grouping(simple) == [simple]


def test_a_grouping_that_does_not_add_up_is_rejected_wholesale():
    """A half-understood grouping would put accents on beats the user never
    asked for; silently playing the default is the honest failure."""
    assert cr.normalise_grouping([2, 2], 5) == cr.default_grouping(5)
    assert cr.normalise_grouping([3, 3, 3], 4) == cr.default_grouping(4)


@pytest.mark.parametrize("bad", [[0, 4], [-1, 5], [2.5, 1.5], ["3", "1"], [None]])
def test_a_grouping_that_is_not_positive_integers_is_rejected(bad):
    assert cr.normalise_grouping(bad, 4) == cr.default_grouping(4)


def test_no_grouping_at_all_means_the_default():
    assert cr.normalise_grouping(None, 7) == cr.default_grouping(7)
    assert cr.normalise_grouping([], 7) == cr.default_grouping(7)


def test_a_valid_grouping_is_kept():
    assert cr.normalise_grouping([2, 3], 5) == [2, 3]


# --------------------------------------------------------------------------
# click_render: bar position
# --------------------------------------------------------------------------


def test_a_bar_mark_starting_after_the_beat_does_not_apply():
    """The loop breaks at the first mark past the index; a mark that has not
    started yet must not decide this beat's accent."""
    bars = [{"beat": 8, "beats_per_bar": 4}]

    assert cr._bar_position(2, bars, cr.ACCENT_AUTO) is None
    assert cr.is_downbeat(2, bars, cr.ACCENT_AUTO) is False


def test_a_malformed_bar_mark_yields_no_position():
    """A zero or non-integer bar length would divide by zero or raise."""
    assert cr._bar_position(4, [{"beat": 0, "beats_per_bar": 0}], cr.ACCENT_AUTO) is None
    assert cr._bar_position(4, [{"beat": 0, "beats_per_bar": "4"}], cr.ACCENT_AUTO) is None
    assert cr._bar_position(4, [{"beat": "0", "beats_per_bar": 4}], cr.ACCENT_AUTO) is None


def test_a_forced_accent_mode_ignores_the_detected_bars():
    """The user overriding the meter has to win over whatever was detected."""
    bars = [{"beat": 0, "beats_per_bar": 4}]

    assert cr._bar_position(3, bars, 3) == (0, 3)
    assert cr.is_downbeat(3, bars, 3) is True


def test_accent_off_means_no_downbeats_at_all():
    bars = [{"beat": 0, "beats_per_bar": 4}]

    assert cr._bar_position(0, bars, cr.ACCENT_OFF) is None
    assert cr.is_downbeat(0, bars, cr.ACCENT_OFF) is False


# --------------------------------------------------------------------------
# click_render: the count-in's local tempo
# --------------------------------------------------------------------------


def test_a_grid_too_short_to_measure_has_no_local_interval():
    assert cr._interval_near([], 0.0, 4) is None
    assert cr._interval_near([1.0], 0.0, 4) is None


def test_a_grid_of_identical_times_has_no_usable_interval():
    """Every diff is zero, so there is no tempo to count in at."""
    assert cr._interval_near([2.0, 2.0, 2.0], 0.0, 4) is None


def test_the_count_in_tempo_follows_the_music_where_playback_starts():
    """Not the track average: a song that speeds up should count in at the
    tempo in force at the loop point."""
    slow = [i * 1.0 for i in range(6)]
    fast = [slow[-1] + 0.5 * i for i in range(1, 7)]
    grid = slow + fast

    assert cr._interval_near(grid, 0.0, 4) == pytest.approx(1.0)
    assert cr._interval_near(grid, grid[7], 4) == pytest.approx(0.5)


def test_a_start_past_the_end_anchors_on_the_last_interval():
    """Never past the last interval, or the slice would be empty."""
    grid = [0.0, 0.5, 1.0]

    assert cr._interval_near(grid, 99.0, 4) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# click_render: multiplier validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, -1.0, 3.7, 100.0])
def test_an_unsupported_click_multiplier_falls_back_to_one(tmp_path, bad):
    """A multiplier of zero would divide by zero building the event list, and a
    silently wrong one changes the exported click's density."""
    beats = [i * 0.5 for i in range(16)]
    bars = [{"beat": 0, "beats_per_bar": 4}]

    at_bad = cr.render_click_wav(tmp_path / "a.wav", beats, bars, 8.0, multiplier=bad)
    at_one = cr.render_click_wav(tmp_path / "b.wav", beats, bars, 8.0, multiplier=1.0)

    assert at_bad is not None and at_one is not None
    assert at_bad.read_bytes() == at_one.read_bytes()


@pytest.mark.parametrize("bad", [0.0, -2.0, 5.5])
def test_an_unsupported_count_in_multiplier_falls_back_to_one(tmp_path, bad):
    beats = [i * 0.5 for i in range(16)]
    bars = [{"beat": 0, "beats_per_bar": 4}]

    bad_out = cr.render_count_in_wav(tmp_path / "c.wav", beats, bars, 8.0, multiplier=bad)
    one_out = cr.render_count_in_wav(tmp_path / "d.wav", beats, bars, 8.0, multiplier=1.0)

    assert bad_out is not None and one_out is not None
    assert bad_out[1] == one_out[1]
    assert bad_out[0].read_bytes() == one_out[0].read_bytes()


def test_a_click_event_outside_the_rendered_span_is_skipped(tmp_path):
    """Events past the end (or before zero) must not write outside the buffer."""
    beats = [-5.0, 0.5, 1.0, 99.0]
    out = cr.render_click_wav(tmp_path / "e.wav", beats, [], duration=2.0)

    assert out is not None and out.is_file()


# --------------------------------------------------------------------------
# section_refine: span helpers
# --------------------------------------------------------------------------


def test_a_boundary_curve_too_short_to_have_a_peak():
    assert sr._local_peak_indices(np.array([1.0, 2.0])) == []
    assert sr._local_peak_indices(np.array([])) == []


def test_only_activations_above_the_floor_are_peaks():
    """Below the floor it is noise, and snapping a section boundary to noise is
    worse than leaving the model's own placement."""
    tiny = np.array([0.0, 1e-6, 0.0])

    assert sr._local_peak_indices(tiny) == []


def test_a_clear_peak_is_found():
    curve = np.array([0.0, 0.1, 0.9, 0.1, 0.0])

    assert sr._local_peak_indices(curve) == [2]


def test_a_midpoint_past_the_last_span_falls_back_to_it():
    """Rounding can put a midpoint a hair past the final end; returning -1 would
    index the wrong span and label the section from the start of the song."""
    spans = [
        {"start": 0.0, "end": 10.0, "label": "intro"},
        {"start": 10.0, "end": 20.0, "label": "verse"},
    ]

    assert sr._span_index(spans, 999.0) == 1
    assert sr._original_label(spans, 999.0) == "verse"


def test_a_midpoint_inside_a_span_finds_it():
    spans = [
        {"start": 0.0, "end": 10.0, "label": "intro"},
        {"start": 10.0, "end": 20.0, "label": "verse"},
    ]

    assert sr._span_index(spans, 5.0) == 0
    assert sr._original_label(spans, 15.0) == "verse"


def test_a_sentinel_only_brackets_at_the_edge_of_the_timeline():
    """start and end are ordinary classes the model does assign mid-song -- one
    real track predicted a 34-second "start" at 74 s. Only one at an extreme is
    actually bracketing the timeline."""
    spans = [
        {"start": 0.0, "end": 5.0, "label": "start"},
        {"start": 5.0, "end": 50.0, "label": "start"},
        {"start": 50.0, "end": 60.0, "label": "end"},
    ]

    assert sr._is_bracket(spans, 0) is True
    assert sr._is_bracket(spans, 1) is False, "a mid-song sentinel is real music"
    assert sr._is_bracket(spans, 2) is True


def test_a_real_label_at_the_edge_is_not_a_bracket():
    spans = [{"start": 0.0, "end": 5.0, "label": "intro"}]

    assert sr._is_bracket(spans, 0) is False


def test_a_label_vocabulary_of_nothing_but_sentinels_yields_the_neutral_label():
    """There is no musical class to choose, and naming a section "start" would
    put a bracket marker in the editor."""
    probabilities = np.ones((2, 10))

    label, margin = sr._mean_label(probabilities, ["start", "end"], 0.0, 1.0, 10.0)

    assert label == sr._NEUTRAL_LABEL
    assert margin == 0.0


def test_a_confident_label_is_taken_and_an_ambiguous_one_is_not():
    """Two classes within a hair of each other is not a name worth showing."""
    names = ["intro", "verse", "chorus"]

    confident = np.zeros((3, 10))
    confident[2, :] = 1.0
    label, margin = sr._mean_label(confident, names, 0.0, 1.0, 10.0)
    assert label == "chorus"
    assert margin > 0

    ambiguous = np.full((3, 10), 0.33)
    ambiguous[1, :] = 0.34
    label, _ = sr._mean_label(ambiguous, names, 0.0, 1.0, 10.0)
    assert label == sr._NEUTRAL_LABEL


def test_a_label_window_is_clamped_to_the_available_frames():
    """A section running past the last activation frame must not produce an
    empty slice, which would make mean() return nan."""
    probabilities = np.zeros((2, 5))
    probabilities[0, :] = 1.0

    label, margin = sr._mean_label(probabilities, ["verse", "chorus"], 0.0, 999.0, 10.0)

    assert label == "verse"
    assert not np.isnan(margin)


# --------------------------------------------------------------------------
# search: thumbnail selection
# --------------------------------------------------------------------------


def test_a_flat_thumbnail_field_is_used_as_is():
    assert search_mod._thumbnail({"thumbnail": "https://img/t.jpg"}) == "https://img/t.jpg"


def test_the_smallest_usable_thumbnail_is_chosen():
    """The dropdown renders these at about 64px, so the 720p variant would cost
    bandwidth and decode time for nothing."""
    entry = {
        "thumbnails": [
            {"url": "tiny", "width": 40},
            {"url": "right", "width": 160},
            {"url": "huge", "width": 1280},
        ]
    }

    assert search_mod._thumbnail(entry) == "right"


def test_when_every_thumbnail_is_too_small_the_largest_is_taken():
    """Something to draw beats an empty square."""
    entry = {"thumbnails": [{"url": "a", "width": 20}, {"url": "b", "width": 90}]}

    assert search_mod._thumbnail(entry) == "b"


def test_thumbnails_with_no_width_do_not_break_the_sort():
    entry = {"thumbnails": [{"url": "a"}, {"url": "b", "width": 200}]}

    assert search_mod._thumbnail(entry) == "b"


def test_no_thumbnail_at_all_is_none_rather_than_an_error():
    assert search_mod._thumbnail({}) is None
    assert search_mod._thumbnail({"thumbnails": []}) is None
    assert search_mod._thumbnail({"thumbnails": [{"no_url": 1}]}) is None
    assert search_mod._thumbnail({"thumbnails": None}) is None
