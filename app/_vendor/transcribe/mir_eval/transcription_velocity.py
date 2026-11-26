"""
Transcription evaluation, as defined in :mod:`mir_eval.transcription`, does not
take into account the velocities of reference and estimated notes. This
submodule implements a variant of
:func:`mir_eval.transcription.precision_recall_f1_overlap` which
additionally considers note velocity when determining whether a note is
correctly transcribed. This is done by defining a new function
:func:`mir_eval.transcription_velocity.match_notes` which first calls
:func:`mir_eval.transcription.match_notes` to get a note matching based on
onset, offset, and pitch. Then, we follow the evaluation procedure described in
[#hawthorne2018onsets]_ to test whether an estimated note should be considered
correct:

1. Reference velocities are re-scaled to the range [0, 1].

2. A linear regression is performed to estimate global scale and offset
   parameters which minimize the L2 distance between matched estimated and
   (rescaled) reference notes.

3. The scale and offset parameters are used to rescale estimated
   velocities.

4. An estimated/reference note pair which has been matched according to the
   onset, offset, and pitch is further only considered correct if the rescaled
   velocities are within a predefined threshold, defaulting to 0.1.

:func:`mir_eval.transcription_velocity.match_notes` is used to define a new
variant :func:`mir_eval.transcription_velocity.precision_recall_f1_overlap`
which considers velocity.

Conventions
-----------

This submodule follows the conventions of :mod:`mir_eval.transcription` and
additionally requires velocities to be provided as MIDI velocities in the range
[0, 127].

Metrics
-------

* :func:`mir_eval.transcription_velocity.precision_recall_f1_overlap`: The
  precision, recall, F-measure, and Average Overlap Ratio of the note
  transcription, where an estimated note is considered correct if its pitch,
  onset, velocity and (optionally) offset are sufficiently close to a reference
  note.

References
----------
  .. [#hawthorne2018onsets] Curtis Hawthorne, Erich Elsen, Jialin Song, Adam
      Roberts, Ian Simon, Colin Raffel, Jesse Engel, Sageev Oore, and Douglas
      Eck, "Onsets and Frames: Dual-Objective Piano Transcription", Proceedings
      of the 19th International Society for Music Information Retrieval
      Conference, 2018.
"""

import collections
import numpy as np
from . import transcription
from . import util


def validate(
    ref_intervals,
    ref_pitches,
    ref_velocities,
    est_intervals,
    est_pitches,
    est_velocities,
):
    """Check that the input annotations have valid time intervals, pitches,
    and velocities, and throws helpful errors if not.

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n,2)
        Array of reference notes time intervals (onset and offset times)
    ref_pitches : np.ndarray, shape=(n,)
        Array of reference pitch values in Hertz
    ref_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of reference notes
    est_intervals : np.ndarray, shape=(m,2)
        Array of estimated notes time intervals (onset and offset times)
    est_pitches : np.ndarray, shape=(m,)
        Array of estimated pitch values in Hertz
    est_velocities : np.ndarray, shape=(m,)
        Array of MIDI velocities (i.e. between 0 and 127) of estimated notes
    """
    transcription.validate(ref_intervals, ref_pitches, est_intervals, est_pitches)
    # Check that velocities have the same length as intervals/pitches
    if not ref_velocities.shape[0] == ref_pitches.shape[0]:
        raise ValueError(
            "Reference velocities must have the same length as "
            "pitches and intervals."
        )
    if not est_velocities.shape[0] == est_pitches.shape[0]:
        raise ValueError(
            "Estimated velocities must have the same length as "
            "pitches and intervals."
        )
    # Check that the velocities are positive
    if ref_velocities.size > 0 and np.min(ref_velocities) < 0:
        raise ValueError("Reference velocities must be positive.")
    if est_velocities.size > 0 and np.min(est_velocities) < 0:
        raise ValueError("Estimated velocities must be positive.")


def match_notes(
    ref_intervals,
    ref_pitches,
    ref_velocities,
    est_intervals,
    est_pitches,
    est_velocities,
    onset_tolerance=0.05,
    pitch_tolerance=50.0,
    offset_ratio=0.2,
    offset_min_tolerance=0.05,
    strict=False,
    velocity_tolerance=0.1,
):
    """Match notes, taking note velocity into consideration.

    This function first calls :func:`mir_eval.transcription.match_notes` to
    match notes according to the supplied intervals, pitches, onset, offset,
    and pitch tolerances. The velocities of the matched notes are then used to
    estimate a slope and intercept which can rescale the estimated velocities
    so that they are as close as possible (in L2 sense) to their matched
    reference velocities. Velocities are then normalized to the range [0, 1]. A
    estimated note is then further only considered correct if its velocity is
    within ``velocity_tolerance`` of its matched (according to pitch and
    timing) reference note.

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n,2)
        Array of reference notes time intervals (onset and offset times)
    ref_pitches : np.ndarray, shape=(n,)
        Array of reference pitch values in Hertz
    ref_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of reference notes
    est_intervals : np.ndarray, shape=(m,2)
        Array of estimated notes time intervals (onset and offset times)
    est_pitches : np.ndarray, shape=(m,)
        Array of estimated pitch values in Hertz
    est_velocities : np.ndarray, shape=(m,)
        Array of MIDI velocities (i.e. between 0 and 127) of estimated notes
    onset_tolerance : float > 0
        The tolerance for an estimated note's onset deviating from the
        reference note's onset, in seconds. Default is 0.05 (50 ms).
    pitch_tolerance : float > 0
        The tolerance for an estimated note's pitch deviating from the
        reference note's pitch, in cents. Default is 50.0 (50 cents).
    offset_ratio : float > 0 or None
        The ratio of the reference note's duration used to define the
        offset_tolerance. Default is 0.2 (20%), meaning the
        ``offset_tolerance`` will equal the ``ref_duration * 0.2``, or 0.05 (50
        ms), whichever is greater. If ``offset_ratio`` is set to ``None``,
        offsets are ignored in the matching.
    offset_min_tolerance : float > 0
        The minimum tolerance for offset matching. See offset_ratio description
        for an explanation of how the offset tolerance is determined. Note:
        this parameter only influences the results if ``offset_ratio`` is not
        ``None``.
    strict : bool
        If ``strict=False`` (the default), threshold checks for onset, offset,
        and pitch matching are performed using ``<=`` (less than or equal). If
        ``strict=True``, the threshold checks are performed using ``<`` (less
        than).
    velocity_tolerance : float > 0
        Estimated notes are considered correct if, after rescaling and
        normalization to [0, 1], they are within ``velocity_tolerance`` of a
        matched reference note.

    Returns
    -------
    matching : list of tuples
        A list of matched reference and estimated notes.
        ``matching[i] == (i, j)`` where reference note ``i`` matches estimated
        note ``j``.
    """
    # Compute note matching as usual using standard transcription function
    matching = transcription.match_notes(
        ref_intervals,
        ref_pitches,
        est_intervals,
        est_pitches,
        onset_tolerance,
        pitch_tolerance,
        offset_ratio,
        offset_min_tolerance,
        strict,
    )

    # Rescale reference velocities to the range [0, 1]
    min_velocity, max_velocity = np.min(ref_velocities), np.max(ref_velocities)
    # Make the smallest possible range 1 to avoid divide by zero
    velocity_range = max(1, max_velocity - min_velocity)
    ref_velocities = (ref_velocities - min_velocity) / float(velocity_range)

    # Convert matching list-of-tuples to array for fancy indexing
    matching = np.array(matching)
    # When there is no matching, return an empty list
    if matching.size == 0:
        return []
    # Grab velocities for matched notes
    ref_matched_velocities = ref_velocities[matching[:, 0]]
    est_matched_velocities = est_velocities[matching[:, 1]]
    # Find slope and intercept of line which produces best least-squares fit
    # between matched est and ref velocities
    slope, intercept = np.linalg.lstsq(
        np.vstack([est_matched_velocities, np.ones(len(est_matched_velocities))]).T,
        ref_matched_velocities,
        rcond=None,
    )[0]
    # Re-scale est velocities to match ref
    est_matched_velocities = slope * est_matched_velocities + intercept
    # Compute the absolute error of (rescaled) estimated velocities vs.
    # normalized reference velocities. Error will be in [0, 1]
    velocity_diff = np.abs(est_matched_velocities - ref_matched_velocities)
    # Check whether each error is within the provided tolerance
    velocity_within_tolerance = velocity_diff < velocity_tolerance
    # Only keep matches whose velocity was within the provided tolerance
    matching = matching[velocity_within_tolerance]
    # Convert back to list-of-tuple format
    matching = [tuple(_) for _ in matching]

    return matching


def precision_recall_f1_overlap(
    ref_intervals,
    ref_pitches,
    ref_velocities,
    est_intervals,
    est_pitches,
    est_velocities,
    onset_tolerance=0.05,
    pitch_tolerance=50.0,
    offset_ratio=0.2,
    offset_min_tolerance=0.05,
    strict=False,
    velocity_tolerance=0.1,
    beta=1.0,
):
    """Compute the Precision, Recall and F-measure of correct vs incorrectly
    transcribed notes, and the Average Overlap Ratio for correctly transcribed
    notes (see :func:`mir_eval.transcription.average_overlap_ratio`).
    "Correctness" is determined based on note onset, velocity, pitch and
    (optionally) offset. An estimated note is considered correct if

    1. Its onset is within ``onset_tolerance`` (default +-50ms) of a
       reference note
    2. Its pitch (F0) is within +/- ``pitch_tolerance`` (default one
       quarter tone, 50 cents) of the corresponding reference note
    3. Its velocity, after normalizing reference velocities to the range
       [0, 1] and globally rescaling estimated velocities to minimize L2
       distance between matched reference notes, is within
       ``velocity_tolerance`` (default 0.1) the corresponding reference note
    4. If ``offset_ratio`` is ``None``, note offsets are ignored in the
       comparison. Otherwise, on top of the above requirements, a correct
       returned note is required to have an offset value within
       `offset_ratio`` (default 20%) of the reference note's duration around
       the reference note's offset, or within ``offset_min_tolerance``
       (default 50 ms), whichever is larger.

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n,2)
        Array of reference notes time intervals (onset and offset times)
    ref_pitches : np.ndarray, shape=(n,)
        Array of reference pitch values in Hertz
    ref_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of reference notes
    est_intervals : np.ndarray, shape=(m,2)
        Array of estimated notes time intervals (onset and offset times)
    est_pitches : np.ndarray, shape=(m,)
        Array of estimated pitch values in Hertz
    est_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of estimated notes
    onset_tolerance : float > 0
        The tolerance for an estimated note's onset deviating from the
        reference note's onset, in seconds. Default is 0.05 (50 ms).
    pitch_tolerance : float > 0
        The tolerance for an estimated note's pitch deviating from the
        reference note's pitch, in cents. Default is 50.0 (50 cents).
    offset_ratio : float > 0 or None
        The ratio of the reference note's duration used to define the
        offset_tolerance. Default is 0.2 (20%), meaning the
        ``offset_tolerance`` will equal the ``ref_duration * 0.2``, or
        ``offset_min_tolerance`` (0.05 by default, i.e. 50 ms), whichever is
        greater. If ``offset_ratio`` is set to ``None``, offsets are ignored in
        the evaluation.
    offset_min_tolerance : float > 0
        The minimum tolerance for offset matching. See ``offset_ratio``
        description for an explanation of how the offset tolerance is
        determined. Note: this parameter only influences the results if
        ``offset_ratio`` is not ``None``.
    strict : bool
        If ``strict=False`` (the default), threshold checks for onset, offset,
        and pitch matching are performed using ``<=`` (less than or equal). If
        ``strict=True``, the threshold checks are performed using ``<`` (less
        than).
    velocity_tolerance : float > 0
        Estimated notes are considered correct if, after rescaling and
        normalization to [0, 1], they are within ``velocity_tolerance`` of a
        matched reference note.
    beta : float > 0
        Weighting factor for f-measure (default value = 1.0).

    Returns
    -------
    precision : float
        The computed precision score
    recall : float
        The computed recall score
    f_measure : float
        The computed F-measure score
    avg_overlap_ratio : float
        The computed Average Overlap Ratio score
    """
    validate(
        ref_intervals,
        ref_pitches,
        ref_velocities,
        est_intervals,
        est_pitches,
        est_velocities,
    )
    # When reference notes are empty, metrics are undefined, return 0's
    if len(ref_pitches) == 0 or len(est_pitches) == 0:
        return 0.0, 0.0, 0.0, 0.0

    matching = match_notes(
        ref_intervals,
        ref_pitches,
        ref_velocities,
        est_intervals,
        est_pitches,
        est_velocities,
        onset_tolerance,
        pitch_tolerance,
        offset_ratio,
        offset_min_tolerance,
        strict,
        velocity_tolerance,
    )

    precision = float(len(matching)) / len(est_pitches)
    recall = float(len(matching)) / len(ref_pitches)
    f_measure = util.f_measure(precision, recall, beta=beta)

    avg_overlap_ratio = transcription.average_overlap_ratio(
        ref_intervals, est_intervals, matching
    )

    return precision, recall, f_measure, avg_overlap_ratio


def evaluate(
    ref_intervals,
    ref_pitches,
    ref_velocities,
    est_intervals,
    est_pitches,
    est_velocities,
    **kwargs
):
    """Compute all metrics for the given reference and estimated annotations.

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n,2)
        Array of reference notes time intervals (onset and offset times)
    ref_pitches : np.ndarray, shape=(n,)
        Array of reference pitch values in Hertz
    ref_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of reference notes
    est_intervals : np.ndarray, shape=(m,2)
        Array of estimated notes time intervals (onset and offset times)
    est_pitches : np.ndarray, shape=(m,)
        Array of estimated pitch values in Hertz
    est_velocities : np.ndarray, shape=(n,)
        Array of MIDI velocities (i.e. between 0 and 127) of estimated notes
    **kwargs
        Additional keyword arguments which will be passed to the
        appropriate metric or preprocessing functions.

    Returns
    -------
    scores : dict
        Dictionary of scores, where the key is the metric name (str) and
        the value is the (float) score achieved.
    """
    # Compute all the metrics
    scores = collections.OrderedDict()

    # Precision, recall and f-measure taking note offsets into account
    kwargs.setdefault("offset_ratio", 0.2)
    if kwargs["offset_ratio"] is not None:
        (
            scores["Precision"],
            scores["Recall"],
            scores["F-measure"],
            scores["Average_Overlap_Ratio"],
        ) = util.filter_kwargs(
            precision_recall_f1_overlap,
            ref_intervals,
            ref_pitches,
            ref_velocities,
            est_intervals,
            est_pitches,
            est_velocities,
            **kwargs
        )

    # Precision, recall and f-measure NOT taking note offsets into account
    kwargs["offset_ratio"] = None
    (
        scores["Precision_no_offset"],
        scores["Recall_no_offset"],
        scores["F-measure_no_offset"],
        scores["Average_Overlap_Ratio_no_offset"],
    ) = util.filter_kwargs(
        precision_recall_f1_overlap,
        ref_intervals,
        ref_pitches,
        ref_velocities,
        est_intervals,
        est_pitches,
        est_velocities,
        **kwargs
    )

    return scores
