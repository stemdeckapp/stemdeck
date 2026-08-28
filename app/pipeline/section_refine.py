"""Conservative evidence refinement for automatic functional sections.

All-In-One predicts boundaries independently from functional labels. This
module preserves those boundaries even when neighboring labels are equal,
adds only suppressed peaks supported by a real embedding change, aligns close
predictions to a trustworthy beat grid, and replaces ambiguous labels with a
neutral ``part`` label.

The functions are deliberately independent from All-In-One classes so the
numeric behavior can be covered with small deterministic arrays.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from numbers import Real

import numpy as np

from app.core.config import (
    SECTION_REFINEMENT_BEAT_SNAP_SECONDS,
    SECTION_REFINEMENT_GRID_MIN_CONFIDENCE,
    SECTION_REFINEMENT_MIN_ACTIVATION,
    SECTION_REFINEMENT_MIN_LABEL_MARGIN,
    SECTION_REFINEMENT_MIN_NOVELTY,
    SECTION_REFINEMENT_MIN_SEGMENT_SECONDS,
    SECTION_REFINEMENT_NOVELTY_WINDOW_SECONDS,
    SECTION_REFINEMENT_RECURRENCE_LABEL_MARGIN,
    SECTION_REFINEMENT_RECURRENCE_SIMILARITY,
)

_SENTINELS = frozenset(("start", "end"))
_NEUTRAL_LABEL = "part"
_BOUNDARY_TOLERANCE_SECONDS = 0.25


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _fallback(raw_segments: object) -> list[dict[str, object]]:
    if not isinstance(raw_segments, list):
        return []
    return [dict(segment) for segment in raw_segments if isinstance(segment, dict)]


def _parse_segments(raw_segments: object) -> list[dict[str, object]] | None:
    if not isinstance(raw_segments, list) or not raw_segments:
        return None
    parsed: list[dict[str, object]] = []
    for item in raw_segments:
        if not isinstance(item, dict) or not isinstance(item.get("label"), str):
            return None
        start = _number(item.get("start"))
        end = _number(item.get("end"))
        if start is None or end is None or end <= start:
            return None
        parsed.append({"start": start, "end": end, "label": item["label"].strip().lower()})
    parsed.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    if any(
        abs(float(right["start"]) - float(left["end"])) > _BOUNDARY_TOLERANCE_SECONDS
        for left, right in zip(parsed, parsed[1:], strict=False)
    ):
        return None
    return parsed


def _evidence_arrays(
    activations: object,
    embeddings: object,
    label_names: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if not isinstance(activations, dict):
        return None
    try:
        boundary = np.asarray(activations.get("segment"), dtype=float)
        label_probabilities = np.asarray(activations.get("label"), dtype=float)
        embedding_values = np.asarray(embeddings, dtype=float)
    except (TypeError, ValueError):
        return None
    if boundary.ndim != 1 or boundary.size < 2:
        return None
    if label_probabilities.shape != (len(label_names), boundary.size):
        return None
    if embedding_values.ndim == 4:
        embedding_values = embedding_values.mean(axis=-1)
    if (
        embedding_values.ndim != 3
        or embedding_values.shape[1] != boundary.size
        or embedding_values.shape[0] < 1
        or embedding_values.shape[2] < 1
    ):
        return None
    if not (
        np.isfinite(boundary).all()
        and np.isfinite(label_probabilities).all()
        and np.isfinite(embedding_values).all()
    ):
        return None
    frame_features = np.transpose(embedding_values, (1, 0, 2)).reshape(boundary.size, -1)
    center = np.median(frame_features, axis=0, keepdims=True)
    centered = frame_features - center
    scale = np.median(np.abs(centered), axis=0, keepdims=True)
    return boundary, label_probabilities, centered / np.maximum(scale, 1e-6)


def _trusted_beats(beat_grid: object) -> list[float]:
    if not isinstance(beat_grid, dict):
        return []
    confidence = _number(beat_grid.get("confidence"))
    raw_beats = beat_grid.get("beats")
    if confidence is None or confidence < SECTION_REFINEMENT_GRID_MIN_CONFIDENCE:
        return []
    if not isinstance(raw_beats, list):
        return []
    beats: list[float] = []
    for value in raw_beats:
        beat = _number(value)
        if beat is None or (beats and beat <= beats[-1]):
            return []
        beats.append(beat)
    return beats


def _nearest_beat(value: float, beats: list[float]) -> float | None:
    if not beats:
        return None
    index = bisect.bisect_left(beats, value)
    choices = beats[max(0, index - 1) : min(len(beats), index + 1)]
    if not choices:
        return None
    nearest = min(choices, key=lambda beat: abs(beat - value))
    return nearest if abs(nearest - value) <= SECTION_REFINEMENT_BEAT_SNAP_SECONDS else None


def _embedding_novelty(features: np.ndarray, frame: int, fps: float) -> float:
    window = max(1, round(SECTION_REFINEMENT_NOVELTY_WINDOW_SECONDS * fps))
    if frame - window < 0 or frame + window > len(features):
        return 0.0
    left = features[frame - window : frame].mean(axis=0)
    right = features[frame : frame + window].mean(axis=0)
    left_norm = float(np.linalg.norm(left))
    right_norm = float(np.linalg.norm(right))
    if left_norm <= 1e-9 or right_norm <= 1e-9:
        return 0.0
    similarity = float(np.dot(left, right) / (left_norm * right_norm))
    return 1.0 - max(-1.0, min(1.0, similarity))


def _local_peak_indices(boundary: np.ndarray) -> list[int]:
    if len(boundary) < 3:
        return []
    peaks = np.flatnonzero(
        (boundary[1:-1] > boundary[:-2])
        & (boundary[1:-1] >= boundary[2:])
        & (boundary[1:-1] >= SECTION_REFINEMENT_MIN_ACTIVATION)
    )
    return [int(index + 1) for index in peaks]


def _span_index(spans: list[dict[str, object]], midpoint: float) -> int:
    for index, span in enumerate(spans):
        if float(span["start"]) <= midpoint < float(span["end"]):
            return index
    return len(spans) - 1


def _is_bracket(spans: list[dict[str, object]], index: int) -> bool:
    """Is this span a non-musical marker rather than a section?

    ``start`` and ``end`` are ordinary classes in the label set, and the model
    does assign them mid-song: one real track predicted a 34-second ``start``
    at 74 s and a 32-second ``end`` at 245 s. Only a sentinel at an extreme of
    the timeline is actually bracketing it. Anywhere else it is just a class
    the semantic head chose, over real music that still deserves a boundary
    search and a real label.
    """
    return str(spans[index]["label"]) in _SENTINELS and index in (0, len(spans) - 1)


def _original_label(spans: list[dict[str, object]], midpoint: float) -> str:
    return str(spans[_span_index(spans, midpoint)]["label"])


def _mean_label(
    probabilities: np.ndarray,
    label_names: Sequence[str],
    start: float,
    end: float,
    fps: float,
) -> tuple[str, float]:
    lo = max(0, min(probabilities.shape[1] - 1, round(start * fps)))
    hi = max(lo + 1, min(probabilities.shape[1], round(end * fps)))
    mean = probabilities[:, lo:hi].mean(axis=1)
    eligible = [index for index, name in enumerate(label_names) if name not in _SENTINELS]
    if not eligible:
        return _NEUTRAL_LABEL, 0.0
    ranked = sorted(eligible, key=lambda index: float(mean[index]), reverse=True)
    best = ranked[0]
    second = ranked[1] if len(ranked) > 1 else best
    margin = float(mean[best] - mean[second]) if second != best else float(mean[best])
    label = str(label_names[best]).strip().lower()
    return (label if margin >= SECTION_REFINEMENT_MIN_LABEL_MARGIN else _NEUTRAL_LABEL), margin


def _segment_vector(features: np.ndarray, start: float, end: float, fps: float) -> np.ndarray:
    lo = max(0, min(len(features) - 1, round(start * fps)))
    hi = max(lo + 1, min(len(features), round(end * fps)))
    vector = features[lo:hi].mean(axis=0)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 1e-9 else np.zeros_like(vector)


def _regularize_neutral_labels(
    records: list[dict[str, object]],
    features: np.ndarray,
    fps: float,
) -> None:
    vectors = [
        _segment_vector(features, float(record["start"]), float(record["end"]), fps)
        for record in records
    ]
    for index, record in enumerate(records):
        if record["label"] != _NEUTRAL_LABEL:
            continue
        matches: list[tuple[float, int]] = []
        duration = float(record["end"]) - float(record["start"])
        for other_index, other in enumerate(records):
            if abs(other_index - index) <= 1 or other["label"] in _SENTINELS | {_NEUTRAL_LABEL}:
                continue
            if float(other["margin"]) < SECTION_REFINEMENT_RECURRENCE_LABEL_MARGIN:
                continue
            other_duration = float(other["end"]) - float(other["start"])
            if other_duration < duration / 2 or other_duration > duration * 2:
                continue
            similarity = float(np.dot(vectors[index], vectors[other_index]))
            if similarity >= SECTION_REFINEMENT_RECURRENCE_SIMILARITY:
                matches.append((similarity, other_index))
        matches.sort(reverse=True)
        if not matches:
            continue
        if len(matches) > 1 and matches[0][0] - matches[1][0] < 0.05:
            continue
        record["label"] = records[matches[0][1]]["label"]


def refine_segments(
    raw_segments: object,
    activations: object,
    embeddings: object,
    activation_fps: object,
    beat_grid: object,
    label_names: Sequence[str],
) -> list[dict[str, object]]:
    """Return compact refined model segments, or the untouched upstream list."""
    fallback = _fallback(raw_segments)
    spans = _parse_segments(raw_segments)
    fps = _number(activation_fps)
    evidence = _evidence_arrays(activations, embeddings, label_names)
    if spans is None or fps is None or fps <= 0 or evidence is None:
        return fallback
    boundary_activation, label_probabilities, features = evidence
    duration = len(boundary_activation) / fps
    beats = _trusted_beats(beat_grid)

    boundaries = [float(spans[0]["start"])]
    boundaries.extend(
        (float(left["end"]) + float(right["start"])) / 2
        for left, right in zip(spans, spans[1:], strict=False)
    )
    boundaries.append(float(spans[-1]["end"]))

    if beats:
        boundaries = [
            value if index in (0, len(boundaries) - 1) else (_nearest_beat(value, beats) or value)
            for index, value in enumerate(boundaries)
        ]

    # A beat grid aligns accepted candidates; it never decides whether one is
    # accepted. Gating discovery on a trustworthy grid silently disabled
    # refinement for rubato, live, and free-time material, which is exactly the
    # material whose upstream spans most need splitting.
    for frame in sorted(
        _local_peak_indices(boundary_activation),
        key=lambda index: float(boundary_activation[index]),
        reverse=True,
    ):
        candidate = frame / fps
        snapped = _nearest_beat(candidate, beats) if beats else None
        position = candidate if snapped is None else snapped
        if _is_bracket(spans, _span_index(spans, candidate)):
            continue
        if (
            min(abs(position - boundary) for boundary in boundaries)
            < SECTION_REFINEMENT_MIN_SEGMENT_SECONDS
        ):
            continue
        if (
            position < SECTION_REFINEMENT_MIN_SEGMENT_SECONDS
            or duration - position < SECTION_REFINEMENT_MIN_SEGMENT_SECONDS
        ):
            continue
        if _embedding_novelty(features, frame, fps) < SECTION_REFINEMENT_MIN_NOVELTY:
            continue
        boundaries.append(position)

    boundaries = sorted(set(boundaries))
    if any(right - left <= 0 for left, right in zip(boundaries, boundaries[1:], strict=False)):
        return fallback

    records: list[dict[str, object]] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        midpoint = (start + end) / 2
        if _is_bracket(spans, _span_index(spans, midpoint)):
            label, margin = _original_label(spans, midpoint), 1.0
        else:
            label, margin = _mean_label(label_probabilities, label_names, start, end, fps)
        records.append({"start": start, "end": end, "label": label, "margin": margin})

    _regularize_neutral_labels(records, features, fps)
    return [
        {"start": float(record["start"]), "end": float(record["end"]), "label": record["label"]}
        for record in records
    ]
