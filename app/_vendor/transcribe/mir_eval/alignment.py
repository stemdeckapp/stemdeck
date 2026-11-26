"""
Alignment models are given a sequence of events along with a piece of audio, and then return a
sequence of timestamps, with one timestamp for each event, indicating the position of this event
in the audio. The events are listed in order of occurrence in the audio, so that output
timestamps have to be monotonically increasing.
Evaluation usually involves taking the series of predicted and ground truth timestamps and
comparing their distance, usually on a pair-wise basis, e.g. taking the median absolute error in
seconds.

Conventions
-----------
Timestamps should be provided in the form of a 1-dimensional array of onset
times in seconds in increasing order.

Metrics
-------
* :func:`mir_eval.alignment.absolute_error`: Median absolute error and average absolute error
* :func:`mir_eval.alignment.percentage_correct`: Percentage of correct timestamps,
  where a timestamp is counted
  as correct if it lies within a certain tolerance window around the ground truth timestamp
* :func:`mir_eval.alignment.pcs`: Percentage of correct segments: Percentage of overlap between
  predicted segments and ground truth segments, where segments are defined by (start time,
  end time) pairs
* :func:`mir_eval.alignment.perceptual_metric`: metric based on human synchronicity perception as
  measured in the paper "User-centered evaluation of lyrics to audio alignment",
  N. Lizé-Masclef, A. Vaglio, M. Moussallam, ISMIR 2021

References
----------
  .. [#lizemasclef2021] N. Lizé-Masclef, A. Vaglio, M. Moussallam.
    "User-centered evaluation of lyrics to audio alignment",
    International Society for Music Information Retrieval (ISMIR) conference,
    2021.

  .. [#mauch2010] M. Mauch, F: Hiromasa, M. Goto.
    "Lyrics-to-audio alignment and phrase-level segmentation using
    incomplete internet-style chord annotations",
    Frontiers in Proceedings of the Sound Music Computing Conference (SMC), 2010.

  .. [#dzhambazov2017] G. Dzhambazov.
    "Knowledge-Based Probabilistic Modeling For Tracking Lyrics In Music Audio Signals",
    PhD Thesis, 2017.

  .. [#fujihara2011] H. Fujihara, M. Goto, J. Ogata, H. Okuno.
    "LyricSynchronizer: Automatic synchronization system between musical audio signals and lyrics",
    IEEE Journal of Selected Topics in Signal Processing, VOL. 5, NO. 6, 2011

"""

import collections
from typing import Optional

import numpy as np
from scipy.stats import skewnorm

from mir_eval.util import filter_kwargs


def validate(reference_timestamps: np.ndarray, estimated_timestamps: np.ndarray):
    """Check that the input annotations to a metric look like valid onset time
    arrays, and throws helpful errors if not.

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamp locations, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamp locations, in seconds
    """
    # We need to have 1D numpy arrays
    if not isinstance(reference_timestamps, np.ndarray):
        raise ValueError(
            "Reference timestamps need to be a numpy array, but got"
            f" {type(reference_timestamps)}"
        )
    if not isinstance(estimated_timestamps, np.ndarray):
        raise ValueError(
            "Estimated timestamps need to be a numpy array, but got"
            f" {type(estimated_timestamps)}"
        )
    if reference_timestamps.ndim != 1:
        raise ValueError(
            "Reference timestamps need to be a one-dimensional vector, but got"
            f" {reference_timestamps.ndim} dimensions"
        )
    if estimated_timestamps.ndim != 1:
        raise ValueError(
            "Estimated timestamps need to be a one-dimensional vector, but got"
            f" {estimated_timestamps.ndim} dimensions"
        )

    # If reference or estimated timestamps are empty, cannot compute metric
    if reference_timestamps.size == 0:
        raise ValueError("Reference timestamps are empty.")
    if estimated_timestamps.size != reference_timestamps.size:
        raise ValueError(
            "Number of timestamps must be the same in prediction and ground"
            f" truth, but found {estimated_timestamps.size} in prediction and"
            f" {reference_timestamps.size} in ground truth"
        )

    # Check monotonicity
    if not np.all(reference_timestamps[1:] - reference_timestamps[:-1] >= 0):
        raise ValueError("Reference timestamps are not monotonically increasing!")
    if not np.all(estimated_timestamps[1:] - estimated_timestamps[:-1] >= 0):
        raise ValueError("Estimated timestamps are not monotonically increasing!")

    # Check positivity (need for correct PCS metric calculation)
    if not np.all(reference_timestamps >= 0):
        raise ValueError("Reference timestamps can not be below 0!")
    if not np.all(estimated_timestamps >= 0):
        raise ValueError("Estimated timestamps can not be below 0!")


def absolute_error(reference_timestamps, estimated_timestamps):
    """Compute the absolute deviations between estimated and reference timestamps,
    and then returns the median and average over all events

    Examples
    --------
    >>> reference_timestamps = mir_eval.io.load_events('reference.txt')
    >>> estimated_timestamps = mir_eval.io.load_events('estimated.txt')
    >>> mae, aae = mir_eval.align.absolute_error(reference_onsets, estimated_timestamps)

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamps, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamps, in seconds

    Returns
    -------
    mae : float
        Median absolute error
    aae: float
        Average absolute error
    """
    validate(reference_timestamps, estimated_timestamps)
    deviations = np.abs(reference_timestamps - estimated_timestamps)
    return np.median(deviations), np.mean(deviations)


def percentage_correct(reference_timestamps, estimated_timestamps, window=0.3):
    """Compute the percentage of correctly predicted timestamps. A timestamp is predicted
    correctly if its position doesn't deviate more than the window parameter from the ground
    truth timestamp.

    Examples
    --------
    >>> reference_timestamps = mir_eval.io.load_events('reference.txt')
    >>> estimated_timestamps = mir_eval.io.load_events('estimated.txt')
    >>> pc = mir_eval.align.percentage_correct(reference_onsets, estimated_timestamps, window=0.2)

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamps, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamps, in seconds
    window : float
        Window size, in seconds
        (Default value = .3)

    Returns
    -------
    pc : float
        Percentage of correct timestamps
    """
    validate(reference_timestamps, estimated_timestamps)
    deviations = np.abs(reference_timestamps - estimated_timestamps)
    return np.mean(deviations <= window)


def percentage_correct_segments(
    reference_timestamps, estimated_timestamps, duration: Optional[float] = None
):
    """Calculate the percentage of correct segments (PCS) metric.

    It constructs segments out of predicted and estimated timestamps separately
    out of each given timestamp vector and calculates the percentage of overlap between correct
    segments compared to the total duration.

    WARNING: This metrics behaves differently depending on whether "duration" is given!

    If duration is not given (default case), the computation follows the MIREX lyrics alignment
    challenge 2020. For a timestamp vector with entries (t1,t2, ... tN), segments with
    the following (start, end) boundaries are created: (t1, t2), ... (tN-1, tN).
    After the segments are created, the overlap between the reference and estimated segments is
    determined and divided by the total duration, which is the distance between the
    first and last timestamp in the reference.

    If duration is given, the segment boundaries are instead (0, t1), (t1, t2), ... (tN, duration).
    The overlap is computed in the same way, but then divided by the duration parameter given to
    this function.
    This method follows the original paper [#fujihara2011] more closely, where the metric was
    proposed.
    As a result, this variant of the metrics punishes cases where the first estimated timestamp
    is too early or the last estimated timestamp is too late, whereas the MIREX variant does not.
    On the other hand, the MIREX metric is invariant to how long the eventless beginning and end
    parts of the audio are, which might be a desirable property.

    Examples
    --------
    >>> reference_timestamps = mir_eval.io.load_events('reference.txt')
    >>> estimated_timestamps = mir_eval.io.load_events('estimated.txt')
    >>> pcs = mir_eval.align.percentage_correct_segments(reference_timestamps, estimated_timestamps)

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamps, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamps, in seconds
    duration : float
        Optional. Total duration of audio (seconds). WARNING: Metric is computed differently
        depending on whether this is provided or not - see documentation above!

    Returns
    -------
    pcs : float
        Percentage of time where ground truth and predicted segments overlap
    """
    validate(reference_timestamps, estimated_timestamps)
    if duration is not None:
        duration = float(duration)
        if duration <= 0:
            raise ValueError(
                f"Positive duration needs to be provided, but got {duration}"
            )
        if np.max(reference_timestamps) > duration:
            raise ValueError(
                "Expected largest reference timestamp"
                f"{np.max(reference_timestamps)} to not be "
                f"larger than duration {duration}"
            )
        if np.max(estimated_timestamps) > duration:
            raise ValueError(
                "Expected largest estimated timestamp "
                f"{np.max(estimated_timestamps)} to not be "
                f"larger than duration {duration}"
            )

        ref_starts = np.concatenate([[0], reference_timestamps])
        ref_ends = np.concatenate([reference_timestamps, [duration]])
        est_starts = np.concatenate([[0], estimated_timestamps])
        est_ends = np.concatenate([estimated_timestamps, [duration]])
    else:
        # MIREX lyrics alignment 2020 style:
        # Ignore regions before start and after end reference timestamp
        duration = reference_timestamps[-1] - reference_timestamps[0]
        if duration <= 0:
            raise ValueError(
                f"Reference timestamps are all identical, can not compute PCS"
                f" metric!"
            )

        ref_starts = reference_timestamps[:-1]
        ref_ends = reference_timestamps[1:]
        est_starts = estimated_timestamps[:-1]
        est_ends = estimated_timestamps[1:]

    overlap_starts = np.maximum(ref_starts, est_starts)
    overlap_ends = np.minimum(ref_ends, est_ends)
    overlap_duration = np.sum(np.maximum(overlap_ends - overlap_starts, 0))
    return overlap_duration / duration


def karaoke_perceptual_metric(reference_timestamps, estimated_timestamps):
    """Metric based on human synchronicity perception as measured in the paper
    "User-centered evaluation of lyrics to audio alignment" [#lizemasclef2021]

    The parameters of this function were tuned on data collected through a user Karaoke-like
    experiment
    It reflects human judgment of how "synchronous" lyrics and audio stimuli are perceived
    in that setup.
    Beware that this metric is non-symmetrical and by construction it is also not equal to 1 at 0.

    Examples
    --------
    >>> reference_timestamps = mir_eval.io.load_events('reference.txt')
    >>> estimated_timestamps = mir_eval.io.load_events('estimated.txt')
    >>> score = mir_eval.align.karaoke_perceptual_metric(reference_onsets, estimated_timestamps)

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamps, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamps, in seconds

    Returns
    -------
    perceptual_score : float
        Perceptual score, averaged over all timestamps
    """
    validate(reference_timestamps, estimated_timestamps)
    offsets = estimated_timestamps - reference_timestamps

    # Score offsets using a certain skewed normal distribution
    skewness = 1.12244251
    localisation = -0.22270315
    scale = 0.29779424
    normalisation_factor = 1.6857
    perceptual_scores = (1.0 / normalisation_factor) * skewnorm.pdf(
        offsets, skewness, loc=localisation, scale=scale
    )

    return np.mean(perceptual_scores)


def evaluate(reference_timestamps, estimated_timestamps, **kwargs):
    """Compute all metrics for the given reference and estimated annotations.

    Examples
    --------
    >>> reference_timestamps = mir_eval.io.load_events('reference.txt')
    >>> estimated_timestamps = mir_eval.io.load_events('estimated.txt')
    >>> duration = max(np.max(reference_timestamps), np.max(estimated_timestamps)) + 10
    >>> scores = mir_eval.align.evaluate(reference_onsets, estimated_timestamps, duration)

    Parameters
    ----------
    reference_timestamps : np.ndarray
        reference timestamp locations, in seconds
    estimated_timestamps : np.ndarray
        estimated timestamp locations, in seconds
    **kwargs
        Additional keyword arguments which will be passed to the
        appropriate metric or preprocessing functions.

    Returns
    -------
    scores : dict
        Dictionary of scores, where the key is the metric name (str) and
        the value is the (float) score achieved.
    """
    # Compute all metrics
    scores = collections.OrderedDict()

    scores["pc"] = filter_kwargs(
        percentage_correct, reference_timestamps, estimated_timestamps, **kwargs
    )
    scores["mae"], scores["aae"] = absolute_error(
        reference_timestamps, estimated_timestamps
    )
    scores["pcs"] = filter_kwargs(
        percentage_correct_segments,
        reference_timestamps,
        estimated_timestamps,
        **kwargs,
    )
    scores["perceptual"] = karaoke_perceptual_metric(
        reference_timestamps, estimated_timestamps
    )

    return scores
