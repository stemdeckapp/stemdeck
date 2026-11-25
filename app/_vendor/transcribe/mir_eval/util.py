"""
Useful functionality required across the task submodules,
such as preprocessing, validation, and common computations.
"""

import os
import inspect
import warnings
from decorator import decorator

import numpy as np


def index_labels(labels, case_sensitive=False):
    """Convert a list of string identifiers into numerical indices.

    Parameters
    ----------
    labels : list of strings, shape=(n,)
        A list of annotations, e.g., segment or chord labels from an
        annotation file.
    case_sensitive : bool
        Set to True to enable case-sensitive label indexing
        (Default value = False)

    Returns
    -------
    indices : list, shape=(n,)
        Numerical representation of ``labels``
    index_to_label : dict
        Mapping to convert numerical indices back to labels.
        ``labels[i] == index_to_label[indices[i]]``
    """
    label_to_index = {}
    index_to_label = {}

    # If we're not case-sensitive,
    if not case_sensitive:
        labels = [str(s).lower() for s in labels]

    # First, build the unique label mapping
    for index, s in enumerate(sorted(set(labels))):
        label_to_index[s] = index
        index_to_label[index] = s

    # Remap the labels to indices
    indices = [label_to_index[s] for s in labels]

    # Return the converted labels, and the inverse mapping
    return indices, index_to_label


def generate_labels(items, prefix="__"):
    """Given an array of items (e.g. events, intervals), create a synthetic label
    for each event of the form '(label prefix)(item number)'

    Parameters
    ----------
    items : list-like
        A list or array of events or intervals
    prefix : str
        This prefix will be prepended to all synthetically generated labels
        (Default value = '__')

    Returns
    -------
    labels : list of str
        Synthetically generated labels

    """
    return [f"{prefix}{n}" for n in range(len(items))]


def intervals_to_samples(intervals, labels, offset=0, sample_size=0.1, fill_value=None):
    """Convert an array of labeled time intervals to annotated samples.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, d)
        An array of time intervals, as returned by
        :func:`mir_eval.io.load_intervals()` or
        :func:`mir_eval.io.load_labeled_intervals()`.
        The ``i`` th interval spans time ``intervals[i, 0]`` to
        ``intervals[i, 1]``.
    labels : list, shape=(n,)
        The annotation for each interval
    offset : float > 0
        Phase offset of the sampled time grid (in seconds)
        (Default value = 0)
    sample_size : float > 0
        duration of each sample to be generated (in seconds)
        (Default value = 0.1)
    fill_value : type(labels[0])
        Object to use for the label with out-of-range time points.
        (Default value = None)

    Returns
    -------
    sample_times : list
        list of sample times
    sample_labels : list
        array of labels for each generated sample

    Notes
    -----
        Intervals will be rounded down to the nearest multiple
        of ``sample_size``.
    """
    # Round intervals to the sample size
    num_samples = int(np.floor(intervals.max() / sample_size))
    sample_indices = np.arange(num_samples, dtype=np.float32)
    sample_times = (sample_indices * sample_size + offset).tolist()
    sampled_labels = interpolate_intervals(intervals, labels, sample_times, fill_value)

    return sample_times, sampled_labels


def interpolate_intervals(intervals, labels, time_points, fill_value=None):
    """Assign labels to a set of points in time given a set of intervals.

    Time points that do not lie within an interval are mapped to `fill_value`.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        An array of time intervals, as returned by
        :func:`mir_eval.io.load_intervals()`.
        The ``i`` th interval spans time ``intervals[i, 0]`` to
        ``intervals[i, 1]``.

        Intervals are assumed to be disjoint.

    labels : list, shape=(n,)
        The annotation for each interval

    time_points : array_like, shape=(m,)
        Points in time to assign labels.  These must be in
        non-decreasing order.

    fill_value : type(labels[0])
        Object to use for the label with out-of-range time points.
        (Default value = None)

    Returns
    -------
    aligned_labels : list
        Labels corresponding to the given time points.

    Raises
    ------
    ValueError
        If `time_points` is not in non-decreasing order.
    """
    # Verify that time_points is sorted
    time_points = np.asarray(time_points)

    if np.any(time_points[1:] < time_points[:-1]):
        raise ValueError("time_points must be in non-decreasing order")

    aligned_labels = [fill_value] * len(time_points)

    starts = np.searchsorted(time_points, intervals[:, 0], side="left")
    ends = np.searchsorted(time_points, intervals[:, 1], side="right")

    for start, end, lab in zip(starts, ends, labels):
        aligned_labels[start:end] = [lab] * (end - start)

    return aligned_labels


def sort_labeled_intervals(intervals, labels=None):
    """Sort intervals, and optionally, their corresponding labels
    according to start time.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        The input intervals
    labels : list, optional
        Labels for each interval

    Returns
    -------
    intervals_sorted or (intervals_sorted, labels_sorted)
        Labels are only returned if provided as input
    """
    idx = np.argsort(intervals[:, 0])

    intervals_sorted = intervals[idx]

    if labels is None:
        return intervals_sorted
    else:
        return intervals_sorted, [labels[_] for _ in idx]


def f_measure(precision, recall, beta=1.0):
    """Compute the f-measure from precision and recall scores.

    Parameters
    ----------
    precision : float in (0, 1]
        Precision
    recall : float in (0, 1]
        Recall
    beta : float > 0
        Weighting factor for f-measure
        (Default value = 1.0)

    Returns
    -------
    f_measure : float
        The weighted f-measure
    """
    if precision == 0 and recall == 0:
        return 0.0

    return (1 + beta**2) * precision * recall / ((beta**2) * precision + recall)


def intervals_to_boundaries(intervals, q=5):
    """Convert interval times into boundaries.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n_events, 2)
        Array of interval start and end-times
    q : int
        Number of decimals to round to. (Default value = 5)

    Returns
    -------
    boundaries : np.ndarray
        Interval boundary times, including the end of the final interval
    """
    return np.unique(np.ravel(np.round(intervals, decimals=q)))


def boundaries_to_intervals(boundaries):
    """Convert an array of event times into intervals

    Parameters
    ----------
    boundaries : list-like
        List-like of event times.  These are assumed to be unique
        timestamps in ascending order.

    Returns
    -------
    intervals : np.ndarray, shape=(n_intervals, 2)
        Start and end time for each interval
    """
    if not np.allclose(boundaries, np.unique(boundaries)):
        raise ValueError("Boundary times are not unique or not ascending.")

    intervals = np.asarray(list(zip(boundaries[:-1], boundaries[1:])))

    return intervals


def adjust_intervals(
    intervals,
    labels=None,
    t_min=0.0,
    t_max=None,
    start_label="__T_MIN",
    end_label="__T_MAX",
):
    """Adjust a list of time intervals to span the range ``[t_min, t_max]``.

    Any intervals lying completely outside the specified range will be removed.

    Any intervals lying partially outside the specified range will be cropped.

    If the specified range exceeds the span of the provided data in either
    direction, additional intervals will be appended.  If an interval is
    appended at the beginning, it will be given the label ``start_label``; if
    an interval is appended at the end, it will be given the label
    ``end_label``.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n_events, 2)
        Array of interval start and end-times
    labels : list, len=n_events or None
        List of labels
        (Default value = None)
    t_min : float or None
        Minimum interval start time.
        (Default value = 0.0)
    t_max : float or None
        Maximum interval end time.
        (Default value = None)
    start_label : str or float or int
        Label to give any intervals appended at the beginning
        (Default value = '__T_MIN')
    end_label : str or float or int
        Label to give any intervals appended at the end
        (Default value = '__T_MAX')

    Returns
    -------
    new_intervals : np.ndarray
        Intervals spanning ``[t_min, t_max]``
    new_labels : list
        List of labels for ``new_labels``
    """
    # When supplied intervals are empty and t_max and t_min are supplied,
    # create one interval from t_min to t_max with the label start_label
    if t_min is not None and t_max is not None and intervals.size == 0:
        return np.array([[t_min, t_max]]), [start_label]
    # When intervals are empty and either t_min or t_max are not supplied,
    # we can't append new intervals
    elif (t_min is None or t_max is None) and intervals.size == 0:
        raise ValueError("Supplied intervals are empty, can't append new" " intervals")

    if t_min is not None:
        # Find the intervals that end at or after t_min
        first_idx = np.argwhere(intervals[:, 1] >= t_min)

        if len(first_idx) > 0:
            # If we have events below t_min, crop them out
            if labels is not None:
                labels = labels[first_idx[0, 0] :]
            # Clip to the range (t_min, +inf)
            intervals = intervals[first_idx[0, 0] :]
        intervals = np.maximum(t_min, intervals)

        if intervals.min() > t_min:
            # Lowest boundary is higher than t_min:
            # add a new boundary and label
            intervals = np.vstack(([t_min, intervals.min()], intervals))
            if labels is not None:
                labels.insert(0, start_label)

    if t_max is not None:
        # Find the intervals that begin after t_max
        last_idx = np.argwhere(intervals[:, 0] > t_max)

        if len(last_idx) > 0:
            # We have boundaries above t_max.
            # Trim to only boundaries <= t_max
            if labels is not None:
                labels = labels[: last_idx[0, 0]]
            # Clip to the range (-inf, t_max)
            intervals = intervals[: last_idx[0, 0]]

        intervals = np.minimum(t_max, intervals)

        if intervals.max() < t_max:
            # Last boundary is below t_max: add a new boundary and label
            intervals = np.vstack((intervals, [intervals.max(), t_max]))
            if labels is not None:
                labels.append(end_label)

    return intervals, labels


def adjust_events(events, labels=None, t_min=0.0, t_max=None, label_prefix="__"):
    """Adjust the given list of event times to span the range
    ``[t_min, t_max]``.

    Any event times outside of the specified range will be removed.

    If the times do not span ``[t_min, t_max]``, additional events will be
    added with the prefix ``label_prefix``.

    Parameters
    ----------
    events : np.ndarray
        Array of event times (seconds)
    labels : list or None
        List of labels
        (Default value = None)
    t_min : float or None
        Minimum valid event time.
        (Default value = 0.0)
    t_max : float or None
        Maximum valid event time.
        (Default value = None)
    label_prefix : str
        Prefix string to use for synthetic labels
        (Default value = '__')

    Returns
    -------
    new_times : np.ndarray
        Event times corrected to the given range.

    """
    if t_min is not None:
        first_idx = np.argwhere(events >= t_min)

        if len(first_idx) > 0:
            # We have events below t_min
            # Crop them out
            if labels is not None:
                labels = labels[first_idx[0, 0] :]
            events = events[first_idx[0, 0] :]

        if events[0] > t_min:
            # Lowest boundary is higher than t_min:
            # add a new boundary and label
            events = np.concatenate(([t_min], events))
            if labels is not None:
                labels.insert(0, "%sT_MIN" % label_prefix)

    if t_max is not None:
        last_idx = np.argwhere(events > t_max)

        if len(last_idx) > 0:
            # We have boundaries above t_max.
            # Trim to only boundaries <= t_max
            if labels is not None:
                labels = labels[: last_idx[0, 0]]
            events = events[: last_idx[0, 0]]

        if events[-1] < t_max:
            # Last boundary is below t_max: add a new boundary and label
            events = np.concatenate((events, [t_max]))
            if labels is not None:
                labels.append("%sT_MAX" % label_prefix)

    return events, labels


def intersect_files(flist1, flist2):
    """Return the intersection of two sets of filepaths, based on the file name
    (after the final '/') and ignoring the file extension.

    Examples
    --------
     >>> flist1 = ['/a/b/abc.lab', '/c/d/123.lab', '/e/f/xyz.lab']
     >>> flist2 = ['/g/h/xyz.npy', '/i/j/123.txt', '/k/l/456.lab']
     >>> sublist1, sublist2 = mir_eval.util.intersect_files(flist1, flist2)
     >>> print sublist1
     ['/e/f/xyz.lab', '/c/d/123.lab']
     >>> print sublist2
     ['/g/h/xyz.npy', '/i/j/123.txt']

    Parameters
    ----------
    flist1 : list
        first list of filepaths
    flist2 : list
        second list of filepaths

    Returns
    -------
    sublist1 : list
        subset of filepaths with matching stems from ``flist1``
    sublist2 : list
        corresponding filepaths from ``flist2``

    """

    def fname(abs_path):
        """Return the filename given an absolute path.

        Parameters
        ----------
        abs_path

        Returns
        -------
        filename

        """
        return os.path.splitext(os.path.split(abs_path)[-1])[0]

    fmap = {fname(f): f for f in flist1}
    pairs = [list(), list()]
    for f in flist2:
        if fname(f) in fmap:
            pairs[0].append(fmap[fname(f)])
            pairs[1].append(f)

    return pairs


def merge_labeled_intervals(x_intervals, x_labels, y_intervals, y_labels):
    r"""Merge the time intervals of two sequences.

    Parameters
    ----------
    x_intervals : np.ndarray
        Array of interval times (seconds)
    x_labels : list or None
        List of labels
    y_intervals : np.ndarray
        Array of interval times (seconds)
    y_labels : list or None
        List of labels

    Returns
    -------
    new_intervals : np.ndarray
        New interval times of the merged sequences.
    new_x_labels : list
        New labels for the sequence ``x``
    new_y_labels : list
        New labels for the sequence ``y``

    """
    align_check = [
        x_intervals[0, 0] == y_intervals[0, 0],
        x_intervals[-1, 1] == y_intervals[-1, 1],
    ]
    if False in align_check:
        raise ValueError(
            "Time intervals do not align; did you mean to call "
            "'adjust_intervals()' first?"
        )
    time_boundaries = np.unique(np.concatenate([x_intervals, y_intervals], axis=0))
    output_intervals = np.array([time_boundaries[:-1], time_boundaries[1:]]).T

    x_labels_out, y_labels_out = [], []
    x_label_range = np.arange(len(x_labels))
    y_label_range = np.arange(len(y_labels))
    for t0, _ in output_intervals:
        x_idx = x_label_range[(t0 >= x_intervals[:, 0])]
        x_labels_out.append(x_labels[x_idx[-1]])
        y_idx = y_label_range[(t0 >= y_intervals[:, 0])]
        y_labels_out.append(y_labels[y_idx[-1]])
    return output_intervals, x_labels_out, y_labels_out


def _bipartite_match(graph):
    """Find maximum cardinality matching of a bipartite graph (U,V,E).
    The input format is a dictionary mapping members of U to a list
    of their neighbors in V.

    The output is a dict M mapping members of V to their matches in U.

    Parameters
    ----------
    graph : dictionary : left-vertex -> list of right vertices
        The input bipartite graph.  Each edge need only be specified once.

    Returns
    -------
    matching : dictionary : right-vertex -> left vertex
        A maximal bipartite matching.

    """
    # Adapted from:
    #
    # Hopcroft-Karp bipartite max-cardinality matching and max independent set
    # David Eppstein, UC Irvine, 27 Apr 2002

    # initialize greedy matching (redundant, but faster than full search)
    matching = {}
    for u in graph:
        for v in graph[u]:
            if v not in matching:
                matching[v] = u
                break

    while True:
        # structure residual graph into layers
        # pred[u] gives the neighbor in the previous layer for u in U
        # preds[v] gives a list of neighbors in the previous layer for v in V
        # unmatched gives a list of unmatched vertices in final layer of V,
        # and is also used as a flag value for pred[u] when u is in the first
        # layer
        preds = {}
        unmatched = []
        pred = {u: unmatched for u in graph}
        for v in matching:
            del pred[matching[v]]
        layer = list(pred)

        # repeatedly extend layering structure by another pair of layers
        while layer and not unmatched:
            new_layer = {}
            for u in layer:
                for v in graph[u]:
                    if v not in preds:
                        new_layer.setdefault(v, []).append(u)
            layer = []
            for v in new_layer:
                preds[v] = new_layer[v]
                if v in matching:
                    layer.append(matching[v])
                    pred[matching[v]] = v
                else:
                    unmatched.append(v)

        # did we finish layering without finding any alternating paths?
        if not unmatched:
            unlayered = {}
            for u in graph:
                for v in graph[u]:
                    if v not in preds:
                        unlayered[v] = None
            return matching

        def recurse(v):
            """Recursively search backward through layers to find alternating
            paths.  recursion returns true if found path, false otherwise
            """
            if v in preds:
                L = preds[v]
                del preds[v]
                for u in L:
                    if u in pred:
                        pu = pred[u]
                        del pred[u]
                        if pu is unmatched or recurse(pu):
                            matching[v] = u
                            return True
            return False

        for v in unmatched:
            recurse(v)


def _outer_distance_mod_n(ref, est, modulus=12):
    """Compute the absolute outer distance modulo n.
    Using this distance, d(11, 0) = 1 (modulo 12)

    Parameters
    ----------
    ref : np.ndarray, shape=(n,)
        Array of reference values.
    est : np.ndarray, shape=(m,)
        Array of estimated values.
    modulus : int
        The modulus.
        12 by default for octave equivalence.

    Returns
    -------
    outer_distance : np.ndarray, shape=(n, m)
        The outer circular distance modulo n.

    """
    ref_mod_n = np.mod(ref, modulus)
    est_mod_n = np.mod(est, modulus)
    abs_diff = np.abs(np.subtract.outer(ref_mod_n, est_mod_n))
    return np.minimum(abs_diff, modulus - abs_diff)


def match_events(ref, est, window, distance=None):
    """Compute a maximum matching between reference and estimated event times,
    subject to a window constraint.

    Given two lists of event times ``ref`` and ``est``, we seek the largest set
    of correspondences ``(ref[i], est[j])`` such that
    ``distance(ref[i], est[j]) <= window``, and each
    ``ref[i]`` and ``est[j]`` is matched at most once.

    This is useful for computing precision/recall metrics in beat tracking,
    onset detection, and segmentation.

    Parameters
    ----------
    ref : np.ndarray, shape=(n,)
        Array of reference values
    est : np.ndarray, shape=(m,)
        Array of estimated values
    window : float > 0
        Size of the window.
    distance : function
        function that computes the outer distance of ref and est.
        By default uses ``|ref[i] - est[j]|``

    Returns
    -------
    matching : list of tuples
        A list of matched reference and event numbers.
        ``matching[i] == (i, j)`` where ``ref[i]`` matches ``est[j]``.

    """
    if distance is not None:
        # Compute the indices of feasible pairings
        hits = np.where(distance(ref, est) <= window)
    else:
        hits = _fast_hit_windows(ref, est, window)

    # Construct the graph input
    G = {}
    for ref_i, est_i in zip(*hits):
        if est_i not in G:
            G[est_i] = []
        G[est_i].append(ref_i)

    # Compute the maximum matching
    matching = sorted(_bipartite_match(G).items())

    return matching


def _fast_hit_windows(ref, est, window):
    """Fast calculation of windowed hits for time events.

    Given two lists of event times ``ref`` and ``est``, and a
    tolerance window, computes a list of pairings
    ``(i, j)`` where ``|ref[i] - est[j]| <= window``.

    This is equivalent to, but more efficient than the following:

    >>> hit_ref, hit_est = np.where(np.abs(np.subtract.outer(ref, est))
    ...                             <= window)

    Parameters
    ----------
    ref : np.ndarray, shape=(n,)
        Array of reference values
    est : np.ndarray, shape=(m,)
        Array of estimated values
    window : float >= 0
        Size of the tolerance window

    Returns
    -------
    hit_ref : np.ndarray
    hit_est : np.ndarray
        indices such that ``|hit_ref[i] - hit_est[i]| <= window``
    """
    ref = np.asarray(ref)
    est = np.asarray(est)
    ref_idx = np.argsort(ref)
    ref_sorted = ref[ref_idx]

    left_idx = np.searchsorted(ref_sorted, est - window, side="left")
    right_idx = np.searchsorted(ref_sorted, est + window, side="right")

    hit_ref, hit_est = [], []

    for j, (start, end) in enumerate(zip(left_idx, right_idx)):
        hit_ref.extend(ref_idx[start:end])
        hit_est.extend([j] * (end - start))

    return hit_ref, hit_est


def validate_intervals(intervals):
    """Check that an (n, 2) interval ndarray is well-formed, and raises errors
    if not.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        Array of interval start/end locations.
    """
    # Validate interval shape
    if intervals.ndim != 2 or intervals.shape[1] != 2:
        raise ValueError(
            "Intervals should be n-by-2 numpy ndarray, "
            "but shape={}".format(intervals.shape)
        )

    # Make sure no times are negative
    if (intervals < 0).any():
        raise ValueError("Negative interval times found")

    # Make sure all intervals have strictly positive duration
    if (intervals[:, 1] <= intervals[:, 0]).any():
        raise ValueError("All interval durations must be strictly positive")


def validate_events(events, max_time=30000.0):
    """Check that a 1-d event location ndarray is well-formed, and raises
    errors if not.

    Parameters
    ----------
    events : np.ndarray, shape=(n,)
        Array of event times
    max_time : float
        If an event is found above this time, a ValueError will be raised.
        (Default value = 30000.)
    """
    # Make sure no event times are huge
    if (events > max_time).any():
        raise ValueError(
            "An event at time {} was found which is greater than "
            "the maximum allowable time of max_time = {} (did you"
            " supply event times in "
            "seconds?)".format(events.max(), max_time)
        )
    # Make sure event locations are 1-d np ndarrays
    if events.ndim != 1:
        raise ValueError(
            "Event times should be 1-d numpy ndarray, "
            "but shape={}".format(events.shape)
        )
    # Make sure event times are increasing
    if (np.diff(events) < 0).any():
        raise ValueError("Events should be in increasing order.")


def validate_frequencies(frequencies, max_freq, min_freq, allow_negatives=False):
    """Check that a 1-d frequency ndarray is well-formed, and raises
    errors if not.

    Parameters
    ----------
    frequencies : np.ndarray, shape=(n,)
        Array of frequency values
    max_freq : float
        If a frequency is found above this pitch, a ValueError will be raised.
        (Default value = 5000.)
    min_freq : float
        If a frequency is found below this pitch, a ValueError will be raised.
        (Default value = 20.)
    allow_negatives : bool
        Whether or not to allow negative frequency values.
    """
    # If flag is true, map frequencies to their absolute value.
    if allow_negatives:
        frequencies = np.abs(frequencies)
    # Make sure no frequency values are huge
    if (np.abs(frequencies) > max_freq).any():
        raise ValueError(
            "A frequency of {} was found which is greater than "
            "the maximum allowable value of max_freq = {} (did "
            "you supply frequency values in "
            "Hz?)".format(frequencies.max(), max_freq)
        )
    # Make sure no frequency values are tiny
    if (np.abs(frequencies) < min_freq).any():
        raise ValueError(
            "A frequency of {} was found which is less than the "
            "minimum allowable value of min_freq = {} (did you "
            "supply frequency values in "
            "Hz?)".format(frequencies.min(), min_freq)
        )
    # Make sure frequency values are 1-d np ndarrays
    if frequencies.ndim != 1:
        raise ValueError(
            "Frequencies should be 1-d numpy ndarray, "
            "but shape={}".format(frequencies.shape)
        )


def has_kwargs(function):
    r"""Determine whether a function has \*\*kwargs.

    Parameters
    ----------
    function : callable
        The function to test

    Returns
    -------
    True if function accepts arbitrary keyword arguments.
    False otherwise.
    """
    sig = inspect.signature(function)

    for param in list(sig.parameters.values()):
        if param.kind == param.VAR_KEYWORD:
            return True

    return False


def filter_kwargs(_function, *args, **kwargs):
    r"""Given a function and args and keyword args to pass to it, call the function
    but using only the keyword arguments which it accepts.  This is equivalent
    to redefining the function with an additional \*\*kwargs to accept slop
    keyword args.

    If the target function already accepts \*\*kwargs parameters, no filtering
    is performed.

    Parameters
    ----------
    _function : callable
        Function to call.  Can take in any number of args or kwargs
    *args
    **kwargs
        Arguments and keyword arguments to _function.
    """
    if has_kwargs(_function):
        return _function(*args, **kwargs)

    # Get the list of function arguments
    func_code = _function.__code__
    function_args = func_code.co_varnames[: func_code.co_argcount]
    # Construct a dict of those kwargs which appear in the function
    filtered_kwargs = {}
    for kwarg, value in list(kwargs.items()):
        if kwarg in function_args:
            filtered_kwargs[kwarg] = value
    # Call the function with the supplied args and the filtered kwarg dict
    return _function(*args, **filtered_kwargs)


def intervals_to_durations(intervals):
    """Convert an array of n intervals to their n durations.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        An array of time intervals, as returned by
        :func:`mir_eval.io.load_intervals()`.
        The ``i`` th interval spans time ``intervals[i, 0]`` to
        ``intervals[i, 1]``.

    Returns
    -------
    durations : np.ndarray, shape=(n,)
        Array of the duration of each interval.

    """
    validate_intervals(intervals)
    return np.abs(np.diff(intervals, axis=-1)).flatten()


def hz_to_midi(freqs):
    """Convert Hz to MIDI numbers

    Parameters
    ----------
    freqs : number or ndarray
        Frequency/frequencies in Hz

    Returns
    -------
    midi : number or ndarray
        MIDI note numbers corresponding to input frequencies.
        Note that these may be fractional.
    """
    return 12.0 * (np.log2(freqs) - np.log2(440.0)) + 69.0


def midi_to_hz(midi):
    """Convert MIDI numbers to Hz

    Parameters
    ----------
    midi : number or ndarray
        MIDI notes

    Returns
    -------
    freqs : number or ndarray
        Frequency/frequencies in Hz corresponding to `midi`
    """
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def deprecated(*, version, version_removed):
    """Mark a function as deprecated.

    Using the decorated (old) function will result in a warning.
    """

    def __wrapper(func, *args, **kwargs):
        """Warn the user, and then proceed."""
        warnings.warn(
            f"{func.__module__}.{func.__name__}\n\tDeprecated as of mir_eval version {version}."
            f"\n\tIt will be removed in mir_eval version {version_removed}.",
            category=FutureWarning,
            stacklevel=3,  # Would be 2, but the decorator adds a level
        )
        return func(*args, **kwargs)

    return decorator(__wrapper)
