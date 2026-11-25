"""
The goal of multiple f0 (multipitch) estimation and tracking is to identify
all of the active fundamental frequencies in each time frame in a complex music
signal.

Conventions
-----------
Multipitch estimates are represented by a timebase and a corresponding list
of arrays of frequency estimates. Frequency estimates may have any number of
frequency values, including 0 (represented by an empty array). Time values are
in units of seconds and frequency estimates are in units of Hz.

The timebase of the estimate time series should ideally match the timebase of
the reference time series, but if this is not the case, the estimate time
series is resampled using a nearest neighbor interpolation to match the
estimate. Time values in the estimate time series that are outside of the range
of the reference time series are given null (empty array) frequencies.

By default, a frequency is "correct" if it is within 0.5 semitones of a
reference frequency. Frequency values are compared by first mapping them to
log-2 semitone space, where the distance between semitones is constant.
Chroma-wrapped frequency values are computed by taking the log-2 frequency
values modulo 12 to map them down to a single octave. A chroma-wrapped
frequency estimate is correct if it's single-octave value is within 0.5
semitones of the single-octave reference frequency.

The metrics are based on those described in
[#poliner2007]_ and [#bay2009]_.

Metrics
-------
* :func:`mir_eval.multipitch.metrics`: Precision, Recall, Accuracy,
  Substitution, Miss, False Alarm, and Total Error scores based both on raw
  frequency values and values mapped to a single octave (chroma).

References
----------
.. [#poliner2007] G. E. Poliner, and D. P. W. Ellis, "A Discriminative
   Model for Polyphonic Piano Transription", EURASIP Journal on Advances in
   Signal Processing, 2007(1):154-163, Jan. 2007.
.. [#bay2009] Bay, M., Ehmann, A. F., & Downie, J. S. (2009). Evaluation of
   Multiple-F0 Estimation and Tracking Systems. In ISMIR (pp. 315-320).
"""

import numpy as np
import collections
import scipy.interpolate
from . import util
import warnings


MAX_TIME = 30000.0  # The maximum allowable time stamp (seconds)
MAX_FREQ = 5000.0  # The maximum allowable frequency (Hz)
MIN_FREQ = 20.0  # The minimum allowable frequency (Hz)


def validate(ref_time, ref_freqs, est_time, est_freqs):
    """Check that the time and frequency inputs are well-formed.

    Parameters
    ----------
    ref_time : np.ndarray
        reference time stamps in seconds
    ref_freqs : list of np.ndarray
        reference frequencies in Hz
    est_time : np.ndarray
        estimate time stamps in seconds
    est_freqs : list of np.ndarray
        estimated frequencies in Hz
    """
    util.validate_events(ref_time, max_time=MAX_TIME)
    util.validate_events(est_time, max_time=MAX_TIME)

    if ref_time.size == 0:
        warnings.warn("Reference times are empty.")
    if ref_time.ndim != 1:
        raise ValueError("Reference times have invalid dimension")
    if len(ref_freqs) == 0:
        warnings.warn("Reference frequencies are empty.")
    if est_time.size == 0:
        warnings.warn("Estimated times are empty.")
    if est_time.ndim != 1:
        raise ValueError("Estimated times have invalid dimension")
    if len(est_freqs) == 0:
        warnings.warn("Estimated frequencies are empty.")
    if ref_time.size != len(ref_freqs):
        raise ValueError("Reference times and frequencies have unequal " "lengths.")
    if est_time.size != len(est_freqs):
        raise ValueError("Estimate times and frequencies have unequal " "lengths.")

    for freq in ref_freqs:
        util.validate_frequencies(
            freq, max_freq=MAX_FREQ, min_freq=MIN_FREQ, allow_negatives=False
        )

    for freq in est_freqs:
        util.validate_frequencies(
            freq, max_freq=MAX_FREQ, min_freq=MIN_FREQ, allow_negatives=False
        )


def resample_multipitch(times, frequencies, target_times):
    """Resamples multipitch time series to a new timescale using nearest
    neighbor interpolation. Values in ``target_times`` outside the range
    of ``times`` return no pitch estimate.

    Parameters
    ----------
    times : np.ndarray
        Array of time stamps
    frequencies : list of np.ndarray
        List of np.ndarrays of frequency values
    target_times : np.ndarray
        Array of target time stamps

    Returns
    -------
    frequencies_resampled : list of numpy arrays
        Frequency list of lists resampled to new timebase
    """
    if target_times.size == 0:
        return []

    if times.size == 0:
        return [np.array([])] * len(target_times)

    n_times = len(frequencies)

    # scipy's interpolate doesn't handle ragged arrays. Instead, we interpolate
    # the frequency index and then map back to the frequency values.
    # This only works because we're using a nearest neighbor interpolator!
    frequency_index = np.arange(0, n_times)

    # times are already ordered so assume_sorted=True for efficiency
    # since we're interpolating the index, fill_value is set to the first index
    # that is out of range. We handle this in the next line.
    new_frequency_index = scipy.interpolate.interp1d(
        times,
        frequency_index,
        kind="nearest",
        bounds_error=False,
        assume_sorted=True,
        fill_value=n_times,
    )(target_times)

    # create array of frequencies plus additional empty element at the end for
    # target time stamps that are out of the interpolation range
    freq_vals = frequencies + [np.array([])]

    # map interpolated indices back to frequency values
    frequencies_resampled = [freq_vals[i] for i in new_frequency_index.astype(int)]

    return frequencies_resampled


def frequencies_to_midi(frequencies, ref_frequency=440.0):
    """Convert frequencies to continuous MIDI values.

    Parameters
    ----------
    frequencies : list of np.ndarray
        Original frequency values
    ref_frequency : float
        reference frequency in Hz.

    Returns
    -------
    frequencies_midi : list of np.ndarray
        Continuous MIDI frequency values.
    """
    return [69.0 + 12.0 * np.log2(freqs / ref_frequency) for freqs in frequencies]


def midi_to_chroma(frequencies_midi):
    """Wrap MIDI frequencies to a single octave (chroma).

    Parameters
    ----------
    frequencies_midi : list of np.ndarray
        Continuous MIDI note frequency values.

    Returns
    -------
    frequencies_chroma : list of np.ndarray
        Midi values wrapped to one octave.

    """
    return [np.mod(freqs, 12) for freqs in frequencies_midi]


def compute_num_freqs(frequencies):
    """Compute the number of frequencies for each time point.

    Parameters
    ----------
    frequencies : list of np.ndarray
        Frequency values

    Returns
    -------
    num_freqs : np.ndarray
        Number of frequencies at each time point.
    """
    return np.array([f.size for f in frequencies])


def compute_num_true_positives(ref_freqs, est_freqs, window=0.5, chroma=False):
    """Compute the number of true positives in an estimate given a reference.
    A frequency is correct if it is within a quartertone of the
    correct frequency.

    Parameters
    ----------
    ref_freqs : list of np.ndarray
        reference frequencies (MIDI)
    est_freqs : list of np.ndarray
        estimated frequencies (MIDI)
    window : float
        Window size, in semitones
    chroma : bool
        If True, computes distances modulo n.
        If True, ``ref_freqs`` and ``est_freqs`` should be wrapped modulo n.

    Returns
    -------
    true_positives : np.ndarray
        Array the same length as ref_freqs containing the number of true
        positives.

    """
    n_frames = len(ref_freqs)
    true_positives = np.zeros((n_frames,))

    for i, (ref_frame, est_frame) in enumerate(zip(ref_freqs, est_freqs)):
        if chroma:
            # match chroma-wrapped frequency events
            matching = util.match_events(
                ref_frame, est_frame, window, distance=util._outer_distance_mod_n
            )
        else:
            # match frequency events within tolerance window in semitones
            matching = util.match_events(ref_frame, est_frame, window)

        true_positives[i] = len(matching)

    return true_positives


def compute_accuracy(true_positives, n_ref, n_est):
    """Compute accuracy metrics.

    Parameters
    ----------
    true_positives : np.ndarray
        Array containing the number of true positives at each time point.
    n_ref : np.ndarray
        Array containing the number of reference frequencies at each time
        point.
    n_est : np.ndarray
        Array containing the number of estimate frequencies at each time point.

    Returns
    -------
    precision : float
        ``sum(true_positives)/sum(n_est)``
    recall : float
        ``sum(true_positives)/sum(n_ref)``
    acc : float
        ``sum(true_positives)/sum(n_est + n_ref - true_positives)``

    """
    true_positive_sum = float(true_positives.sum())

    n_est_sum = n_est.sum()
    if n_est_sum > 0:
        precision = true_positive_sum / n_est.sum()
    else:
        warnings.warn("Estimate frequencies are all empty.")
        precision = 0.0

    n_ref_sum = n_ref.sum()
    if n_ref_sum > 0:
        recall = true_positive_sum / n_ref.sum()
    else:
        warnings.warn("Reference frequencies are all empty.")
        recall = 0.0

    acc_denom = (n_est + n_ref - true_positives).sum()
    if acc_denom > 0:
        acc = true_positive_sum / acc_denom
    else:
        acc = 0.0

    return precision, recall, acc


def compute_err_score(true_positives, n_ref, n_est):
    """Compute error score metrics.

    Parameters
    ----------
    true_positives : np.ndarray
        Array containing the number of true positives at each time point.
    n_ref : np.ndarray
        Array containing the number of reference frequencies at each time
        point.
    n_est : np.ndarray
        Array containing the number of estimate frequencies at each time point.

    Returns
    -------
    e_sub : float
        Substitution error
    e_miss : float
        Miss error
    e_fa : float
        False alarm error
    e_tot : float
        Total error

    """
    n_ref_sum = float(n_ref.sum())

    if n_ref_sum == 0:
        warnings.warn("Reference frequencies are all empty.")
        return 0.0, 0.0, 0.0, 0.0

    # Substitution error
    e_sub = (np.min([n_ref, n_est], axis=0) - true_positives).sum() / n_ref_sum

    # compute the max of (n_ref - n_est) and 0
    e_miss_numerator = n_ref - n_est
    e_miss_numerator[e_miss_numerator < 0] = 0
    # Miss error
    e_miss = e_miss_numerator.sum() / n_ref_sum

    # compute the max of (n_est - n_ref) and 0
    e_fa_numerator = n_est - n_ref
    e_fa_numerator[e_fa_numerator < 0] = 0
    # False alarm error
    e_fa = e_fa_numerator.sum() / n_ref_sum

    # total error
    e_tot = (np.max([n_ref, n_est], axis=0) - true_positives).sum() / n_ref_sum

    return e_sub, e_miss, e_fa, e_tot


def metrics(ref_time, ref_freqs, est_time, est_freqs, **kwargs):
    """Compute multipitch metrics. All metrics are computed at the 'macro' level
    such that the frame true positive/false positive/false negative rates are
    summed across time and the metrics are computed on the combined values.

    Examples
    --------
    >>> ref_time, ref_freqs = mir_eval.io.load_ragged_time_series(
    ...     'reference.txt')
    >>> est_time, est_freqs = mir_eval.io.load_ragged_time_series(
    ...     'estimated.txt')
    >>> metris_tuple = mir_eval.multipitch.metrics(
    ...     ref_time, ref_freqs, est_time, est_freqs)

    Parameters
    ----------
    ref_time : np.ndarray
        Time of each reference frequency value
    ref_freqs : list of np.ndarray
        List of np.ndarrays of reference frequency values
    est_time : np.ndarray
        Time of each estimated frequency value
    est_freqs : list of np.ndarray
        List of np.ndarrays of estimate frequency values
    **kwargs
        Additional keyword arguments which will be passed to the
        appropriate metric or preprocessing functions.

    Returns
    -------
    precision : float
        Precision (TP/(TP + FP))
    recall : float
        Recall (TP/(TP + FN))
    accuracy : float
        Accuracy (TP/(TP + FP + FN))
    e_sub : float
        Substitution error
    e_miss : float
        Miss error
    e_fa : float
        False alarm error
    e_tot : float
        Total error
    precision_chroma : float
        Chroma precision
    recall_chroma : float
        Chroma recall
    accuracy_chroma : float
        Chroma accuracy
    e_sub_chroma : float
        Chroma substitution error
    e_miss_chroma : float
        Chroma miss error
    e_fa_chroma : float
        Chroma false alarm error
    e_tot_chroma : float
        Chroma total error

    """
    validate(ref_time, ref_freqs, est_time, est_freqs)

    # resample est_freqs if est_times is different from ref_times
    if est_time.size != ref_time.size or not np.allclose(est_time, ref_time):
        warnings.warn(
            "Estimate times not equal to reference times. "
            "Resampling to common time base."
        )
        est_freqs = resample_multipitch(est_time, est_freqs, ref_time)

    # convert frequencies from Hz to continuous midi note number
    ref_freqs_midi = frequencies_to_midi(ref_freqs)
    est_freqs_midi = frequencies_to_midi(est_freqs)

    # compute chroma wrapped midi number
    ref_freqs_chroma = midi_to_chroma(ref_freqs_midi)
    est_freqs_chroma = midi_to_chroma(est_freqs_midi)

    # count number of occurrences
    n_ref = compute_num_freqs(ref_freqs_midi)
    n_est = compute_num_freqs(est_freqs_midi)

    # compute the number of true positives
    true_positives = util.filter_kwargs(
        compute_num_true_positives, ref_freqs_midi, est_freqs_midi, **kwargs
    )

    # compute the number of true positives ignoring octave mistakes
    true_positives_chroma = util.filter_kwargs(
        compute_num_true_positives,
        ref_freqs_chroma,
        est_freqs_chroma,
        chroma=True,
        **kwargs
    )

    # compute accuracy metrics
    precision, recall, accuracy = compute_accuracy(true_positives, n_ref, n_est)

    # compute error metrics
    e_sub, e_miss, e_fa, e_tot = compute_err_score(true_positives, n_ref, n_est)

    # compute accuracy metrics ignoring octave mistakes
    precision_chroma, recall_chroma, accuracy_chroma = compute_accuracy(
        true_positives_chroma, n_ref, n_est
    )

    # compute error metrics ignoring octave mistakes
    e_sub_chroma, e_miss_chroma, e_fa_chroma, e_tot_chroma = compute_err_score(
        true_positives_chroma, n_ref, n_est
    )

    return (
        precision,
        recall,
        accuracy,
        e_sub,
        e_miss,
        e_fa,
        e_tot,
        precision_chroma,
        recall_chroma,
        accuracy_chroma,
        e_sub_chroma,
        e_miss_chroma,
        e_fa_chroma,
        e_tot_chroma,
    )


def evaluate(ref_time, ref_freqs, est_time, est_freqs, **kwargs):
    """Evaluate two multipitch (multi-f0) transcriptions, where the first is
    treated as the reference (ground truth) and the second as the estimate to
    be evaluated (prediction).

    Examples
    --------
    >>> ref_time, ref_freq = mir_eval.io.load_ragged_time_series('ref.txt')
    >>> est_time, est_freq = mir_eval.io.load_ragged_time_series('est.txt')
    >>> scores = mir_eval.multipitch.evaluate(ref_time, ref_freq,
    ...                                       est_time, est_freq)

    Parameters
    ----------
    ref_time : np.ndarray
        Time of each reference frequency value
    ref_freqs : list of np.ndarray
        List of np.ndarrays of reference frequency values
    est_time : np.ndarray
        Time of each estimated frequency value
    est_freqs : list of np.ndarray
        List of np.ndarrays of estimate frequency values
    **kwargs
        Additional keyword arguments which will be passed to the
        appropriate metric or preprocessing functions.

    Returns
    -------
    scores : dict
        Dictionary of scores, where the key is the metric name (str) and
        the value is the (float) score achieved.

    """
    scores = collections.OrderedDict()

    (
        scores["Precision"],
        scores["Recall"],
        scores["Accuracy"],
        scores["Substitution Error"],
        scores["Miss Error"],
        scores["False Alarm Error"],
        scores["Total Error"],
        scores["Chroma Precision"],
        scores["Chroma Recall"],
        scores["Chroma Accuracy"],
        scores["Chroma Substitution Error"],
        scores["Chroma Miss Error"],
        scores["Chroma False Alarm Error"],
        scores["Chroma Total Error"],
    ) = util.filter_kwargs(metrics, ref_time, ref_freqs, est_time, est_freqs, **kwargs)

    return scores
