"""Which boundary candidates refinement accepts, and which it throws out.

refine_segments walks the model's boundary-activation peaks strongest-first and
applies four independent rejection rules before adding one. Each rule exists
because accepting a bad candidate splits a section where the music does not,
and the editor then shows a boundary the user has to drag back.

None of this raises: a rule that stops working produces a worse structure and
nothing else, so the tests assert on the boundaries that came out.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import SECTION_REFINEMENT_MIN_NOVELTY, SECTION_REFINEMENT_MIN_SEGMENT_SECONDS
from app.pipeline import section_refine as sr

FPS = 10.0
LABELS = ["intro", "verse", "chorus", "part", "start", "end"]


def _evidence(frames, peaks=(), novelty_at=(), label_row=1):
    """Boundary activation with peaks at the given frames, plus embeddings that
    change sharply only where asked."""
    boundary = np.full(frames, 0.01)
    for f in peaks:
        boundary[f - 1] = 0.2
        boundary[f] = 0.9
        boundary[f + 1] = 0.2

    labels = np.full((len(LABELS), frames), 0.01)
    labels[label_row, :] = 0.9

    # One embedding channel that flips sign at each novelty point, so
    # _embedding_novelty scores high there and near zero everywhere else.
    feat = np.ones((1, frames, 4))
    for f in novelty_at:
        feat[0, f:, :] *= -1
    return {"segment": boundary.tolist(), "label": labels.tolist()}, feat.tolist()


def _spans(*bounds, label="verse"):
    out = []
    for start, end in zip(bounds, bounds[1:], strict=False):
        out.append({"start": float(start), "end": float(end), "label": label})
    return out


def _refine(spans, activations, embeddings, beat_grid=None):
    return sr.refine_segments(spans, activations, embeddings, FPS, beat_grid, LABELS)


# --------------------------------------------------------------------------
# a candidate that is accepted, so the rejections below are decisions
# --------------------------------------------------------------------------


def test_a_strong_novel_candidate_well_inside_the_track_is_accepted():
    frames = 1200  # 120 s
    activations, embeddings = _evidence(frames, peaks=[600], novelty_at=[600])

    out = _refine(_spans(0, 120), activations, embeddings)

    assert len(out) == 2, "the boundary was not added"
    assert out[0]["end"] == pytest.approx(60.0, abs=1.0)


# --------------------------------------------------------------------------
# the four rejection rules
# --------------------------------------------------------------------------


def test_a_candidate_too_close_to_an_existing_boundary_is_dropped():
    """The model already put a boundary there; a second one a second away is a
    duplicate the user would have to merge back."""
    frames = 1200
    # A peak 2 s from the existing internal boundary at 60 s.
    activations, embeddings = _evidence(frames, peaks=[620], novelty_at=[620])

    out = _refine(_spans(0, 60, 120), activations, embeddings)

    assert len(out) == 2, "a duplicate boundary was added next to an existing one"


def test_a_candidate_too_close_to_the_start_of_the_track_is_dropped():
    """A two-second opening section is not a section."""
    frames = 1200
    near_start = int(SECTION_REFINEMENT_MIN_SEGMENT_SECONDS * FPS) - 20
    activations, embeddings = _evidence(frames, peaks=[near_start], novelty_at=[near_start])

    out = _refine(_spans(0, 120), activations, embeddings)

    assert len(out) == 1


def test_a_candidate_too_close_to_the_end_of_the_track_is_dropped():
    frames = 1200
    near_end = frames - int(SECTION_REFINEMENT_MIN_SEGMENT_SECONDS * FPS) + 20
    activations, embeddings = _evidence(frames, peaks=[near_end], novelty_at=[near_end])

    out = _refine(_spans(0, 120), activations, embeddings)

    assert len(out) == 1


def test_a_candidate_with_no_change_of_material_under_it_is_dropped():
    """A boundary activation peak with identical audio either side of it is the
    model reacting to something that is not a section change."""
    frames = 1200
    activations, embeddings = _evidence(frames, peaks=[600], novelty_at=[])

    out = _refine(_spans(0, 120), activations, embeddings)

    assert len(out) == 1
    assert sr._embedding_novelty(np.ones((frames, 4)), 600, FPS) < SECTION_REFINEMENT_MIN_NOVELTY


def test_a_candidate_inside_a_bracket_span_is_dropped():
    """A sentinel at an extreme of the timeline brackets it rather than being
    music; splitting inside one produces a section made of nothing."""
    frames = 1200
    activations, embeddings = _evidence(frames, peaks=[300], novelty_at=[300])
    spans = [
        {"start": 0.0, "end": 60.0, "label": "start"},
        {"start": 60.0, "end": 120.0, "label": "verse"},
    ]

    out = _refine(spans, activations, embeddings)

    assert all(not (r["start"] < 30.0 < r["end"] and r["start"] > 0) for r in out)


def test_a_record_inside_a_bracket_span_keeps_its_original_label():
    """Re-labelling a bracket from the semantic head would name it as music."""
    frames = 1200
    activations, embeddings = _evidence(frames, peaks=[], novelty_at=[])
    spans = [
        {"start": 0.0, "end": 40.0, "label": "start"},
        {"start": 40.0, "end": 120.0, "label": "chorus"},
    ]

    out = _refine(spans, activations, embeddings)

    assert out[0]["label"] == "start"


# --------------------------------------------------------------------------
# degenerate output falls back
# --------------------------------------------------------------------------


def test_boundaries_that_collapse_fall_back_to_the_upstream_spans(monkeypatch):
    """Two boundaries at the same instant would make a zero-length section; the
    upstream list is the safer answer."""
    frames = 1200
    activations, embeddings = _evidence(frames, peaks=[600], novelty_at=[600])
    spans = _spans(0, 120)

    monkeypatch.setattr(sr, "_nearest_beat", lambda value, beats: 0.0)

    out = sr.refine_segments(
        spans,
        activations,
        embeddings,
        FPS,
        {"confidence": 99, "beats": [0.0, 0.5, 1.0]},
        LABELS,
    )

    assert out == spans


# --------------------------------------------------------------------------
# _regularize_neutral_labels
# --------------------------------------------------------------------------


def _records(*specs):
    return [{"start": s, "end": e, "label": lab, "margin": m} for s, e, lab, m in specs]


def test_a_neutral_section_takes_the_label_of_the_part_it_matches():
    """The semantic head is unsure on a repeat it already named elsewhere;
    borrowing that name beats leaving a bare "part" in the timeline."""
    features = np.zeros((400, 4))
    features[0:100] = [1.0, 0.0, 0.0, 0.0]
    features[100:200] = [0.0, 1.0, 0.0, 0.0]
    features[200:300] = [0.0, 0.0, 1.0, 0.0]
    features[300:400] = [1.0, 0.0, 0.0, 0.0]  # same material as the first

    records = _records(
        (0.0, 10.0, "chorus", 0.9),
        (10.0, 20.0, "verse", 0.9),
        (20.0, 30.0, "verse", 0.9),
        (30.0, 40.0, sr._NEUTRAL_LABEL, 0.01),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[3]["label"] == "chorus"


def test_a_neighbouring_section_is_never_the_match():
    """Adjacent spans are separate structural predictions; letting one absorb
    the other would undo the boundary the model just drew."""
    features = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (400, 1))

    records = _records(
        (0.0, 10.0, "chorus", 0.9),
        (10.0, 20.0, sr._NEUTRAL_LABEL, 0.01),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[1]["label"] == sr._NEUTRAL_LABEL


def test_a_match_the_model_was_unsure_of_is_not_borrowed():
    """Copying a label the head itself barely committed to spreads its doubt.

    Only the first record is a candidate at all -- the middle one neighbours the
    neutral span -- so the borrow either comes from it or does not happen."""
    features = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (400, 1))

    records = _records(
        (0.0, 10.0, "chorus", 0.01),  # below the recurrence margin
        (10.0, 20.0, "verse", 0.9),
        (20.0, 30.0, sr._NEUTRAL_LABEL, 0.01),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[2]["label"] == sr._NEUTRAL_LABEL


@pytest.mark.parametrize(
    "candidate_length, why",
    [(100.0, "ten times longer than the neutral span"), (1.0, "a tenth of its length")],
)
def test_a_match_of_a_very_different_length_is_not_borrowed(candidate_length, why):
    """A four-bar fill and a two-minute chorus are not the same section however
    similar their material. The rule is symmetric: too long and too short are
    both refused."""
    features = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (2000, 1))

    records = _records(
        (0.0, candidate_length, "chorus", 0.9),
        (120.0, 130.0, "verse", 0.9),
        (130.0, 140.0, sr._NEUTRAL_LABEL, 0.01),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[2]["label"] == sr._NEUTRAL_LABEL, why


def test_two_equally_good_matches_leave_the_label_alone():
    """Picking either would be a coin toss presented to the user as an answer."""
    features = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (600, 1))

    records = _records(
        (0.0, 10.0, "chorus", 0.9),
        (10.0, 20.0, "bridge", 0.9),
        (20.0, 30.0, "verse", 0.9),
        (30.0, 40.0, "solo", 0.9),
        (40.0, 50.0, sr._NEUTRAL_LABEL, 0.01),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[4]["label"] == sr._NEUTRAL_LABEL


def test_a_section_that_is_already_named_is_left_alone():
    features = np.tile(np.array([1.0, 0.0, 0.0, 0.0]), (400, 1))

    records = _records(
        (0.0, 10.0, "chorus", 0.9),
        (10.0, 20.0, "verse", 0.9),
        (20.0, 30.0, "verse", 0.9),
        (30.0, 40.0, "bridge", 0.9),
    )

    sr._regularize_neutral_labels(records, features, fps=10.0)

    assert records[3]["label"] == "bridge"
