"""The validators section refinement runs before it trusts model evidence.

test_pipeline_sections.py exercises refine_segments end to end and confirms it
falls back when the evidence is malformed. What it does not do is pin *which*
malformations are caught -- and these are guards over arrays coming out of a
neural network, where the difference between rejecting bad evidence and using
it is a section boundary placed on noise.

Every function here returns None or [] to mean "no usable evidence", and the
caller reads that as "keep the raw segments". A guard that wrongly accepts is
therefore silent: the analysis still succeeds, just with worse boundaries.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import (
    SECTION_REFINEMENT_BEAT_SNAP_SECONDS,
    SECTION_REFINEMENT_GRID_MIN_CONFIDENCE,
)
from app.pipeline import section_refine as sr

# --------------------------------------------------------------------------
# _number -- the type gate everything else is built on
# --------------------------------------------------------------------------


def test_a_real_number_passes():
    assert sr._number(1) == 1.0
    assert sr._number(2.5) == 2.5
    assert sr._number(np.float32(3.5)) == 3.5


def test_a_boolean_is_not_a_number():
    """bool is a subclass of int, so `isinstance(True, Real)` is True. A JSON
    `true` reaching a start time would otherwise become 1.0 seconds."""
    assert sr._number(True) is None
    assert sr._number(False) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_number_is_refused(bad):
    """These propagate through every later comparison and sort, silently."""
    assert sr._number(bad) is None


@pytest.mark.parametrize("bad", ["1.5", None, [], {}, object()])
def test_something_that_is_not_a_number_is_refused(bad):
    assert sr._number(bad) is None


# --------------------------------------------------------------------------
# _parse_segments
# --------------------------------------------------------------------------


def _seg(start, end, label="verse"):
    return {"start": start, "end": end, "label": label}


def test_segments_are_parsed_sorted_and_lowercased():
    out = sr._parse_segments([_seg(10, 20, "Chorus"), _seg(0, 10, " VERSE ")])

    assert [s["label"] for s in out] == ["verse", "chorus"]
    assert [s["start"] for s in out] == [0.0, 10.0]


@pytest.mark.parametrize(
    "raw, why",
    [
        (None, "not a list"),
        ([], "an empty list"),
        (["not-a-dict"], "an entry that is not an object"),
        ([{"start": 0, "end": 1}], "an entry with no label"),
        ([{"start": 0, "end": 1, "label": 7}], "a label that is not a string"),
        ([_seg("x", 1)], "a start that is not a number"),
        ([_seg(0, None)], "an end that is not a number"),
        ([_seg(5, 5)], "a zero-length span"),
        ([_seg(5, 1)], "an end before its start"),
        ([_seg(0, float("nan"))], "a non-finite bound"),
    ],
)
def test_unusable_segments_are_refused(raw, why):
    assert sr._parse_segments(raw) is None, why


def test_a_real_gap_between_segments_is_refused():
    """A gap means the model's timeline is not contiguous, so the boundaries in
    it cannot be trusted to move."""
    assert sr._parse_segments([_seg(0, 10), _seg(30, 40)]) is None


def test_a_rounding_sized_gap_is_tolerated():
    """The model's frame timing routinely disagrees with the segment bounds by
    a few milliseconds; rejecting on that would refuse every real track."""
    assert sr._parse_segments([_seg(0, 10), _seg(10.05, 20)]) is not None


def test_a_real_overlap_is_refused():
    assert sr._parse_segments([_seg(0, 10), _seg(5, 20)]) is None


# --------------------------------------------------------------------------
# _fallback -- what is kept when refinement gives up
# --------------------------------------------------------------------------


def test_the_fallback_copies_the_raw_segments():
    raw = [_seg(0, 10), _seg(10, 20)]

    out = sr._fallback(raw)

    assert out == raw
    out[0]["start"] = 99
    assert raw[0]["start"] == 0, "the fallback handed back the caller's own dicts"


def test_the_fallback_drops_entries_that_are_not_objects():
    assert sr._fallback([_seg(0, 10), "junk", None]) == [_seg(0, 10)]


def test_the_fallback_of_something_that_is_not_a_list_is_empty():
    assert sr._fallback(None) == []
    assert sr._fallback("segments") == []


# --------------------------------------------------------------------------
# _evidence_arrays -- the shape contract with the model
# --------------------------------------------------------------------------

_LABELS = ["intro", "verse", "chorus"]


def _evidence(frames=8, labels=3, emb=(2, None, 4)):
    a, _, c = emb
    return {
        "segment": np.linspace(0, 1, frames).tolist(),
        "label": np.zeros((labels, frames)).tolist(),
    }, np.random.default_rng(0).normal(size=(a, frames, c)).tolist()


def test_well_formed_evidence_is_accepted():
    activations, embeddings = _evidence()

    out = sr._evidence_arrays(activations, embeddings, _LABELS)

    assert out is not None
    boundary, labels, features = out
    assert boundary.shape == (8,)
    assert labels.shape == (3, 8)
    assert features.shape[0] == 8


def test_a_four_dimensional_embedding_is_averaged_down():
    """The model emits an extra trailing axis in some configurations; collapsing
    it is what lets the same code read both."""
    activations, _ = _evidence()
    embeddings = np.random.default_rng(0).normal(size=(2, 8, 4, 5)).tolist()

    assert sr._evidence_arrays(activations, embeddings, _LABELS) is not None


def test_activations_that_are_not_a_mapping_are_refused():
    _, embeddings = _evidence()
    assert sr._evidence_arrays(None, embeddings, _LABELS) is None
    assert sr._evidence_arrays([1, 2, 3], embeddings, _LABELS) is None


def test_evidence_that_will_not_become_an_array_is_refused():
    """A ragged list raises inside numpy rather than returning something odd."""
    activations = {"segment": [[1, 2], [3]], "label": [[0.0]]}
    assert sr._evidence_arrays(activations, [[1]], _LABELS) is None


def test_a_boundary_curve_of_the_wrong_shape_is_refused():
    _, embeddings = _evidence()
    assert sr._evidence_arrays({"segment": [[1, 2]], "label": []}, embeddings, _LABELS) is None
    # A single frame cannot carry a boundary.
    assert sr._evidence_arrays({"segment": [0.5], "label": []}, embeddings, _LABELS) is None


def test_label_probabilities_that_do_not_match_the_label_vocabulary_are_refused():
    """Indexing this array by label position is how a section gets its name;
    a mismatch would name sections from the wrong row."""
    activations, embeddings = _evidence()
    activations["label"] = np.zeros((2, 8)).tolist()  # 2 rows for 3 labels

    assert sr._evidence_arrays(activations, embeddings, _LABELS) is None


def test_embeddings_that_do_not_line_up_with_the_frames_are_refused():
    activations, _ = _evidence()
    embeddings = np.zeros((2, 99, 4)).tolist()  # 99 frames, boundary has 8

    assert sr._evidence_arrays(activations, embeddings, _LABELS) is None


def test_embeddings_of_the_wrong_rank_are_refused():
    activations, _ = _evidence()
    assert sr._evidence_arrays(activations, np.zeros((8, 4)).tolist(), _LABELS) is None


def test_empty_embedding_axes_are_refused():
    activations, _ = _evidence()
    assert sr._evidence_arrays(activations, np.zeros((0, 8, 4)).tolist(), _LABELS) is None
    assert sr._evidence_arrays(activations, np.zeros((2, 8, 0)).tolist(), _LABELS) is None


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_non_finite_evidence_is_refused(bad):
    """A single NaN anywhere poisons the median-centring below it and every
    novelty score computed from it."""
    activations, embeddings = _evidence()
    activations["segment"][3] = bad
    assert sr._evidence_arrays(activations, embeddings, _LABELS) is None

    activations, embeddings = _evidence()
    embeddings[0][2][1] = bad
    assert sr._evidence_arrays(activations, embeddings, _LABELS) is None


# --------------------------------------------------------------------------
# _trusted_beats
# --------------------------------------------------------------------------


def _grid(beats, confidence=None):
    if confidence is None:
        confidence = SECTION_REFINEMENT_GRID_MIN_CONFIDENCE + 5
    return {"confidence": confidence, "beats": beats}


def test_a_confident_ascending_grid_is_trusted():
    assert sr._trusted_beats(_grid([0.0, 0.5, 1.0])) == [0.0, 0.5, 1.0]


def test_a_grid_below_the_confidence_floor_is_not_trusted():
    """Snapping a section boundary to a beat the detector is unsure about moves
    it somewhere worse than where the model put it."""
    low = SECTION_REFINEMENT_GRID_MIN_CONFIDENCE - 1
    assert sr._trusted_beats(_grid([0.0, 0.5], confidence=low)) == []


def test_a_grid_with_no_confidence_at_all_is_not_trusted():
    assert sr._trusted_beats({"beats": [0.0, 0.5]}) == []


@pytest.mark.parametrize(
    "grid, why",
    [
        (None, "not a mapping"),
        ("grid", "a string"),
        (_grid("beats"), "beats that are not a list"),
        (_grid([0.0, "x"]), "a beat that is not a number"),
        (_grid([0.0, 0.5, 0.5]), "a repeated beat"),
        (_grid([0.0, 1.0, 0.5]), "beats out of order"),
        (_grid([0.0, float("nan")]), "a non-finite beat"),
    ],
)
def test_an_untrustworthy_grid_yields_no_beats(grid, why):
    assert sr._trusted_beats(grid) == [], why


def test_an_empty_beat_list_is_simply_empty():
    assert sr._trusted_beats(_grid([])) == []


# --------------------------------------------------------------------------
# _nearest_beat
# --------------------------------------------------------------------------


def test_the_nearest_beat_within_the_snap_window_is_returned():
    beats = [0.0, 1.0, 2.0, 3.0]

    assert sr._nearest_beat(1.0 + SECTION_REFINEMENT_BEAT_SNAP_SECONDS / 2, beats) == 1.0
    assert sr._nearest_beat(1.9, beats) == 2.0


def test_a_boundary_far_from_every_beat_does_not_snap():
    """Beyond the window the model's own placement is the better answer."""
    beats = [0.0, 10.0]

    assert sr._nearest_beat(5.0, beats) is None


def test_snapping_against_no_beats_is_none():
    assert sr._nearest_beat(1.0, []) is None


def test_a_boundary_beyond_the_last_beat_still_considers_it():
    beats = [0.0, 1.0]

    assert sr._nearest_beat(1.0 + SECTION_REFINEMENT_BEAT_SNAP_SECONDS / 2, beats) == 1.0


def test_a_boundary_before_the_first_beat_still_considers_it():
    beats = [1.0, 2.0]

    assert sr._nearest_beat(1.0 - SECTION_REFINEMENT_BEAT_SNAP_SECONDS / 2, beats) == 1.0


# --------------------------------------------------------------------------
# _embedding_novelty
# --------------------------------------------------------------------------


def test_novelty_is_zero_where_a_full_window_does_not_fit():
    """Near either end there is not enough context to compare; a partial window
    would report novelty from the edge of the array rather than the music."""
    # fps=1 keeps the 8-second window at 8 frames, so these arrays are long
    # enough for the in-range cases below to be about the audio rather than
    # about running off the end.
    features = np.ones((10, 4))

    assert sr._embedding_novelty(features, 0, fps=1.0) == 0.0
    assert sr._embedding_novelty(features, len(features), fps=1.0) == 0.0


def test_novelty_is_zero_across_a_silent_region():
    """Two zero-norm windows have no direction to differ in, and normalising by
    them would divide by zero."""
    features = np.zeros((100, 4))

    assert sr._embedding_novelty(features, 50, fps=1.0) == 0.0


def test_a_genuine_change_of_material_scores_higher_than_a_steady_one():
    steady = np.ones((100, 4))
    changing = np.vstack([np.ones((50, 4)), -np.ones((50, 4))])

    assert sr._embedding_novelty(changing, 50, fps=1.0) > sr._embedding_novelty(steady, 50, fps=1.0)
