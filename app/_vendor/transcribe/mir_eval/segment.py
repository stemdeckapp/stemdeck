# CREATED:2013-08-13 12:02:42 by Brian McFee <brm2132@columbia.edu>
"""
Evaluation criteria for structural segmentation fall into two categories:
boundary annotation and structural annotation.  Boundary annotation is the task
of predicting the times at which structural changes occur, such as when a verse
transitions to a refrain.  Metrics for boundary annotation compare estimated
segment boundaries to reference boundaries.  Structural annotation is the task
of assigning labels to detected segments.  The estimated labels may be
arbitrary strings - such as A, B, C, - and they need not describe functional
concepts.  Metrics for structural annotation are similar to those used for
clustering data.

Conventions
-----------

Both boundary and structural annotation metrics require two dimensional arrays
with two columns, one for boundary start times and one for boundary end times.
Structural annotation further require lists of reference and estimated segment
labels which must have a length which is equal to the number of rows in the
corresponding list of boundary edges.  In both tasks, we assume that
annotations express a partitioning of the track into intervals.  The function
:func:`mir_eval.util.adjust_intervals` can be used to pad or crop the segment
boundaries to span the duration of the entire track.


Metrics
-------

* :func:`mir_eval.segment.detection`: An estimated boundary is considered
  correct if it falls within a window around a reference boundary
  [#turnbull2007]_
* :func:`mir_eval.segment.deviation`: Computes the median absolute time
  difference from a reference boundary to its nearest estimated boundary, and
  vice versa [#turnbull2007]_
* :func:`mir_eval.segment.pairwise`: For classifying pairs of sampled time
  instants as belonging to the same structural component [#levy2008]_
* :func:`mir_eval.segment.rand_index`: Clusters reference and estimated
  annotations and compares them by the Rand Index
* :func:`mir_eval.segment.ari`: Computes the Rand index, adjusted for chance
* :func:`mir_eval.segment.nce`: Interprets sampled reference and estimated
  labels as samples of random variables :math:`Y_R, Y_E` from which the
  conditional entropy of :math:`Y_R` given :math:`Y_E` (Under-Segmentation) and
  :math:`Y_E` given :math:`Y_R` (Over-Segmentation) are estimated
  [#lukashevich2008]_
* :func:`mir_eval.segment.mutual_information`: Computes the standard,
  normalized, and adjusted mutual information of sampled reference and
  estimated segments
* :func:`mir_eval.segment.vmeasure`: Computes the V-Measure, which is similar
  to the conditional entropy metrics, but uses the marginal distributions
  as normalization rather than the maximum entropy distribution
  [#rosenberg2007]_


References
----------
    .. [#turnbull2007] Turnbull, D., Lanckriet, G. R., Pampalk, E.,
        & Goto, M.  A Supervised Approach for Detecting Boundaries in Music
        Using Difference Features and Boosting. In ISMIR (pp. 51-54).

    .. [#levy2008] Levy, M., & Sandler, M.
        Structural segmentation of musical audio by constrained clustering.
        IEEE transactions on audio, speech, and language processing, 16(2),
        318-326.

    .. [#lukashevich2008] Lukashevich, H. M.
        Towards Quantitative Measures of Evaluating Song Segmentation.
        In ISMIR (pp. 375-380).

    .. [#rosenberg2007] Rosenberg, A., & Hirschberg, J.
        V-Measure: A Conditional Entropy-Based External Cluster Evaluation
        Measure.
        In EMNLP-CoNLL (Vol. 7, pp. 410-420).
"""

import collections
import warnings

import numpy as np
import scipy.stats
import scipy.sparse
import scipy.special

from . import util


def validate_boundary(reference_intervals, estimated_intervals, trim):
    """Check that the input annotations to a segment boundary estimation
    metric (i.e. one that only takes in segment intervals) look like valid
    segment times, and throws helpful errors if not.

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    trim : bool
        will the start and end events be trimmed?
    """
    if trim:
        # If we're trimming, then we need at least 2 intervals
        min_size = 2
    else:
        # If we're not trimming, then we only need one interval
        min_size = 1

    if len(reference_intervals) < min_size:
        warnings.warn("Reference intervals are empty.")

    if len(estimated_intervals) < min_size:
        warnings.warn("Estimated intervals are empty.")

    for intervals in [reference_intervals, estimated_intervals]:
        util.validate_intervals(intervals)


def validate_structure(
    reference_intervals, reference_labels, estimated_intervals, estimated_labels
):
    """Check that the input annotations to a structure estimation metric (i.e.
    one that takes in both segment boundaries and their labels) look like valid
    segment times and labels, and throws helpful errors if not.

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.

    """
    for intervals, labels in [
        (reference_intervals, reference_labels),
        (estimated_intervals, estimated_labels),
    ]:
        util.validate_intervals(intervals)
        if intervals.shape[0] != len(labels):
            raise ValueError("Number of intervals does not match number " "of labels")

        # Check only when intervals are non-empty
        if intervals.size > 0:
            # Make sure intervals start at 0
            if not np.allclose(intervals.min(), 0.0):
                raise ValueError("Segment intervals do not start at 0")

    if reference_intervals.size == 0:
        warnings.warn("Reference intervals are empty.")
    if estimated_intervals.size == 0:
        warnings.warn("Estimated intervals are empty.")
    # Check only when intervals are non-empty
    if reference_intervals.size > 0 and estimated_intervals.size > 0:
        if not np.allclose(reference_intervals.max(), estimated_intervals.max()):
            raise ValueError("End times do not match")


def detection(
    reference_intervals, estimated_intervals, window=0.5, beta=1.0, trim=False
):
    """Boundary detection hit-rate.

    A hit is counted whenever an reference boundary is within ``window`` of a
    estimated boundary.  Note that each boundary is matched at most once: this
    is achieved by computing the size of a maximal matching between reference
    and estimated boundary points, subject to the window constraint.

    Examples
    --------
    >>> ref_intervals, _ = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> est_intervals, _ = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # With 0.5s windowing
    >>> P05, R05, F05 = mir_eval.segment.detection(ref_intervals,
    ...                                            est_intervals,
    ...                                            window=0.5)
    >>> # With 3s windowing
    >>> P3, R3, F3 = mir_eval.segment.detection(ref_intervals,
    ...                                         est_intervals,
    ...                                         window=3)
    >>> # Ignoring hits for the beginning and end of track
    >>> P, R, F = mir_eval.segment.detection(ref_intervals,
    ...                                      est_intervals,
    ...                                      window=0.5,
    ...                                      trim=True)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    window : float > 0
        size of the window of 'correctness' around ground-truth beats
        (in seconds)
        (Default value = 0.5)
    beta : float > 0
        weighting constant for F-measure.
        (Default value = 1.0)
    trim : boolean
        if ``True``, the first and last boundary times are ignored.
        Typically, these denote start (0) and end-markers.
        (Default value = False)

    Returns
    -------
    precision : float
        precision of estimated predictions
    recall : float
        recall of reference reference boundaries
    f_measure : float
        F-measure (weighted harmonic mean of ``precision`` and ``recall``)
    """
    validate_boundary(reference_intervals, estimated_intervals, trim)

    # Convert intervals to boundaries
    reference_boundaries = util.intervals_to_boundaries(reference_intervals)
    estimated_boundaries = util.intervals_to_boundaries(estimated_intervals)

    # Suppress the first and last intervals
    if trim:
        reference_boundaries = reference_boundaries[1:-1]
        estimated_boundaries = estimated_boundaries[1:-1]

    # If we have no boundaries, we get no score.
    if len(reference_boundaries) == 0 or len(estimated_boundaries) == 0:
        return 0.0, 0.0, 0.0

    matching = util.match_events(reference_boundaries, estimated_boundaries, window)

    precision = float(len(matching)) / len(estimated_boundaries)
    recall = float(len(matching)) / len(reference_boundaries)

    f_measure = util.f_measure(precision, recall, beta=beta)

    return precision, recall, f_measure


def deviation(reference_intervals, estimated_intervals, trim=False):
    """Compute the median deviations between reference
    and estimated boundary times.

    Examples
    --------
    >>> ref_intervals, _ = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> est_intervals, _ = mir_eval.io.load_labeled_intervals('est.lab')
    >>> r_to_e, e_to_r = mir_eval.boundary.deviation(ref_intervals,
    ...                                              est_intervals)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    trim : boolean
        if ``True``, the first and last intervals are ignored.
        Typically, these denote start (0.0) and end-of-track markers.
        (Default value = False)

    Returns
    -------
    reference_to_estimated : float
        median time from each reference boundary to the
        closest estimated boundary
    estimated_to_reference : float
        median time from each estimated boundary to the
        closest reference boundary
    """
    validate_boundary(reference_intervals, estimated_intervals, trim)

    # Convert intervals to boundaries
    reference_boundaries = util.intervals_to_boundaries(reference_intervals)
    estimated_boundaries = util.intervals_to_boundaries(estimated_intervals)

    # Suppress the first and last intervals
    if trim:
        reference_boundaries = reference_boundaries[1:-1]
        estimated_boundaries = estimated_boundaries[1:-1]

    # If we have no boundaries, we get no score.
    if len(reference_boundaries) == 0 or len(estimated_boundaries) == 0:
        return np.nan, np.nan

    dist = np.abs(np.subtract.outer(reference_boundaries, estimated_boundaries))

    estimated_to_reference = np.median(dist.min(axis=0))
    reference_to_estimated = np.median(dist.min(axis=1))

    return reference_to_estimated, estimated_to_reference


def pairwise(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
    beta=1.0,
):
    """Frame-clustering segmentation evaluation by pair-wise agreement.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> precision, recall, f = mir_eval.structure.pairwise(ref_intervals,
    ...                                                    ref_labels,
    ...                                                    est_intervals,
    ...                                                    est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)
    beta : float > 0
        beta value for F-measure
        (Default value = 1.0)

    Returns
    -------
    precision : float > 0
        Precision of detecting whether frames belong in the same cluster
    recall : float > 0
        Recall of detecting whether frames belong in the same cluster
    f : float > 0
        F-measure of detecting whether frames belong in the same cluster

    """
    validate_structure(
        reference_intervals, reference_labels, estimated_intervals, estimated_labels
    )

    # Check for empty annotations.  Don't need to check labels because
    # validate_structure makes sure they're the same size as intervals
    if reference_intervals.size == 0 or estimated_intervals.size == 0:
        return 0.0, 0.0, 0.0

    # Generate the cluster labels
    y_ref = util.intervals_to_samples(
        reference_intervals, reference_labels, sample_size=frame_size
    )[-1]

    y_ref = util.index_labels(y_ref)[0]

    # Map to index space
    y_est = util.intervals_to_samples(
        estimated_intervals, estimated_labels, sample_size=frame_size
    )[-1]

    y_est = util.index_labels(y_est)[0]

    # Build the reference label agreement matrix
    agree_ref = np.equal.outer(y_ref, y_ref)
    # Count the unique pairs
    n_agree_ref = (agree_ref.sum() - len(y_ref)) / 2.0

    # Repeat for estimate
    agree_est = np.equal.outer(y_est, y_est)
    n_agree_est = (agree_est.sum() - len(y_est)) / 2.0

    # Find where they agree
    matches = np.logical_and(agree_ref, agree_est)
    n_matches = (matches.sum() - len(y_ref)) / 2.0

    precision = n_matches / n_agree_est
    recall = n_matches / n_agree_ref
    f_measure = util.f_measure(precision, recall, beta=beta)

    return precision, recall, f_measure


def rand_index(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
    beta=1.0,
):
    """(Non-adjusted) Rand index.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> rand_index = mir_eval.structure.rand_index(ref_intervals,
    ...                                            ref_labels,
    ...                                            est_intervals,
    ...                                            est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)
    beta : float > 0
        beta value for F-measure
        (Default value = 1.0)

    Returns
    -------
    rand_index : float > 0
        Rand index
    """
    validate_structure(
        reference_intervals, reference_labels, estimated_intervals, estimated_labels
    )

    # Check for empty annotations.  Don't need to check labels because
    # validate_structure makes sure they're the same size as intervals
    if reference_intervals.size == 0 or estimated_intervals.size == 0:
        return 0.0, 0.0, 0.0

    # Generate the cluster labels
    y_ref = util.intervals_to_samples(
        reference_intervals, reference_labels, sample_size=frame_size
    )[-1]

    y_ref = util.index_labels(y_ref)[0]

    # Map to index space
    y_est = util.intervals_to_samples(
        estimated_intervals, estimated_labels, sample_size=frame_size
    )[-1]

    y_est = util.index_labels(y_est)[0]

    # Build the reference label agreement matrix
    agree_ref = np.equal.outer(y_ref, y_ref)

    # Repeat for estimate
    agree_est = np.equal.outer(y_est, y_est)

    # Find where they agree
    matches_pos = np.logical_and(agree_ref, agree_est)

    # Find where they disagree
    matches_neg = np.logical_and(~agree_ref, ~agree_est)

    n_pairs = len(y_ref) * (len(y_ref) - 1) / 2.0

    n_matches_pos = (matches_pos.sum() - len(y_ref)) / 2.0
    n_matches_neg = matches_neg.sum() / 2.0
    rand = (n_matches_pos + n_matches_neg) / n_pairs

    return rand


def _contingency_matrix(reference_indices, estimated_indices):
    """Compute the contingency matrix of a true labeling vs an estimated one.

    Parameters
    ----------
    reference_indices : np.ndarray
        Array of reference indices
    estimated_indices : np.ndarray
        Array of estimated indices

    Returns
    -------
    contingency_matrix : np.ndarray
        Contingency matrix, shape=(#reference indices, #estimated indices)
    .. note:: Based on sklearn.metrics.cluster.contingency_matrix

    """
    ref_classes, ref_class_idx = np.unique(reference_indices, return_inverse=True)
    est_classes, est_class_idx = np.unique(estimated_indices, return_inverse=True)
    n_ref_classes = ref_classes.shape[0]
    n_est_classes = est_classes.shape[0]
    # Using coo_matrix is faster than histogram2d
    return scipy.sparse.coo_matrix(
        (np.ones(ref_class_idx.shape[0]), (ref_class_idx, est_class_idx)),
        shape=(n_ref_classes, n_est_classes),
        dtype=np.int64,
    ).toarray()


def _adjusted_rand_index(reference_indices, estimated_indices):
    """Compute the Rand index, adjusted for change.

    Parameters
    ----------
    reference_indices : np.ndarray
        Array of reference indices
    estimated_indices : np.ndarray
        Array of estimated indices

    Returns
    -------
    ari : float
        Adjusted Rand index
    .. note:: Based on sklearn.metrics.cluster.adjusted_rand_score

    """
    n_samples = len(reference_indices)
    ref_classes = np.unique(reference_indices)
    est_classes = np.unique(estimated_indices)
    # Special limit cases: no clustering since the data is not split;
    # or trivial clustering where each document is assigned a unique cluster.
    # These are perfect matches hence return 1.0.
    if (
        ref_classes.shape[0] == est_classes.shape[0] == 1
        or ref_classes.shape[0] == est_classes.shape[0] == 0
        or (ref_classes.shape[0] == est_classes.shape[0] == len(reference_indices))
    ):
        return 1.0

    contingency = _contingency_matrix(reference_indices, estimated_indices)

    # Compute the ARI using the contingency data
    sum_comb_c = sum(
        scipy.special.comb(n_c, 2, exact=1) for n_c in contingency.sum(axis=1)
    )
    sum_comb_k = sum(
        scipy.special.comb(n_k, 2, exact=1) for n_k in contingency.sum(axis=0)
    )

    sum_comb = sum(
        scipy.special.comb(n_ij, 2, exact=1) for n_ij in contingency.flatten()
    )
    prod_comb = (sum_comb_c * sum_comb_k) / float(scipy.special.comb(n_samples, 2))
    mean_comb = (sum_comb_k + sum_comb_c) / 2.0
    return (sum_comb - prod_comb) / (mean_comb - prod_comb)


def ari(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
):
    """Compute the Adjusted Rand Index (ARI) for frame clustering segmentation evaluation.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> ari_score = mir_eval.structure.ari(ref_intervals, ref_labels,
    ...                                    est_intervals, est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)

    Returns
    -------
    ari_score : float > 0
        Adjusted Rand index between segmentations.

    """
    validate_structure(
        reference_intervals, reference_labels, estimated_intervals, estimated_labels
    )

    # Check for empty annotations.  Don't need to check labels because
    # validate_structure makes sure they're the same size as intervals
    if reference_intervals.size == 0 or estimated_intervals.size == 0:
        return 0.0, 0.0, 0.0

    # Generate the cluster labels
    y_ref = util.intervals_to_samples(
        reference_intervals, reference_labels, sample_size=frame_size
    )[-1]

    y_ref = util.index_labels(y_ref)[0]

    # Map to index space
    y_est = util.intervals_to_samples(
        estimated_intervals, estimated_labels, sample_size=frame_size
    )[-1]

    y_est = util.index_labels(y_est)[0]

    return _adjusted_rand_index(y_ref, y_est)


def _mutual_info_score(reference_indices, estimated_indices, contingency=None):
    """Compute the mutual information between two sequence labelings.

    Parameters
    ----------
    reference_indices : np.ndarray
        Array of reference indices
    estimated_indices : np.ndarray
        Array of estimated indices
    contingency : np.ndarray
        Pre-computed contingency matrix.  If None, one will be computed.
        (Default value = None)

    Returns
    -------
    mi : float
        Mutual information
    .. note:: Based on sklearn.metrics.cluster.mutual_info_score

    """
    if contingency is None:
        contingency = _contingency_matrix(reference_indices, estimated_indices).astype(
            float
        )
    contingency_sum = np.sum(contingency)
    pi = np.sum(contingency, axis=1)
    pj = np.sum(contingency, axis=0)
    outer = np.outer(pi, pj)
    nnz = contingency != 0.0
    # normalized contingency
    contingency_nm = contingency[nnz]
    log_contingency_nm = np.log(contingency_nm)
    contingency_nm /= contingency_sum
    # log(a / b) should be calculated as log(a) - log(b) for
    # possible loss of precision
    log_outer = -np.log(outer[nnz]) + np.log(pi.sum()) + np.log(pj.sum())
    mi = (
        contingency_nm * (log_contingency_nm - np.log(contingency_sum))
        + contingency_nm * log_outer
    )
    return mi.sum()


def _entropy(labels):
    """Calculate the entropy for a labeling.

    Parameters
    ----------
    labels : list-like
        List of labels.

    Returns
    -------
    entropy : float
        Entropy of the labeling.
    .. note:: Based on sklearn.metrics.cluster.entropy

    """
    if len(labels) == 0:
        return 1.0
    label_idx = np.unique(labels, return_inverse=True)[1]
    pi = np.bincount(label_idx).astype(np.float64)
    pi = pi[pi > 0]
    pi_sum = np.sum(pi)
    # log(a / b) should be calculated as log(a) - log(b) for
    # possible loss of precision
    return -np.sum((pi / pi_sum) * (np.log(pi) - np.log(pi_sum)))


def _adjusted_mutual_info_score(reference_indices, estimated_indices):
    """Compute the mutual information between two sequence labelings, adjusted for
    chance.

    Parameters
    ----------
    reference_indices : np.ndarray
        Array of reference indices
    estimated_indices : np.ndarray
        Array of estimated indices

    Returns
    -------
    ami : float <= 1.0
        Mutual information
    .. note:: Based on sklearn.metrics.cluster.adjusted_mutual_info_score
        and sklearn.metrics.cluster.expected_mutual_info_score

    """
    n_samples = len(reference_indices)
    ref_classes = np.unique(reference_indices)
    est_classes = np.unique(estimated_indices)
    # Special limit cases: no clustering since the data is not split.
    # This is a perfect match hence return 1.0.
    if (
        ref_classes.shape[0] == est_classes.shape[0] == 1
        or ref_classes.shape[0] == est_classes.shape[0] == 0
    ):
        return 1.0
    contingency = _contingency_matrix(reference_indices, estimated_indices).astype(
        float
    )
    # Calculate the MI for the two clusterings
    mi = _mutual_info_score(
        reference_indices, estimated_indices, contingency=contingency
    )
    # The following code is based on
    # sklearn.metrics.cluster.expected_mutual_information
    R, C = contingency.shape
    N = float(n_samples)
    a = np.sum(contingency, axis=1).astype(np.int32)
    b = np.sum(contingency, axis=0).astype(np.int32)
    # There are three major terms to the EMI equation, which are multiplied to
    # and then summed over varying nij values.
    # While nijs[0] will never be used, having it simplifies the indexing.
    nijs = np.arange(0, max(np.max(a), np.max(b)) + 1, dtype="float")
    # Stops divide by zero warnings. As its not used, no issue.
    nijs[0] = 1
    # term1 is nij / N
    term1 = nijs / N
    # term2 is log((N*nij) / (a * b)) == log(N * nij) - log(a * b)
    # term2 uses the outer product
    log_ab_outer = np.log(np.outer(a, b))
    # term2 uses N * nij
    log_Nnij = np.log(N * nijs)
    # term3 is large, and involved many factorials. Calculate these in log
    # space to stop overflows.
    gln_a = scipy.special.gammaln(a + 1)
    gln_b = scipy.special.gammaln(b + 1)
    gln_Na = scipy.special.gammaln(N - a + 1)
    gln_Nb = scipy.special.gammaln(N - b + 1)
    gln_N = scipy.special.gammaln(N + 1)
    gln_nij = scipy.special.gammaln(nijs + 1)
    # start and end values for nij terms for each summation.
    start = np.array([[v - N + w for w in b] for v in a], dtype="int")
    start = np.maximum(start, 1)
    end = np.minimum(np.resize(a, (C, R)).T, np.resize(b, (R, C))) + 1
    # emi itself is a summation over the various values.
    emi = 0
    for i in range(R):
        for j in range(C):
            for nij in range(start[i, j], end[i, j]):
                term2 = log_Nnij[nij] - log_ab_outer[i, j]
                # Numerators are positive, denominators are negative.
                gln = (
                    gln_a[i]
                    + gln_b[j]
                    + gln_Na[i]
                    + gln_Nb[j]
                    - gln_N
                    - gln_nij[nij]
                    - scipy.special.gammaln(a[i] - nij + 1)
                    - scipy.special.gammaln(b[j] - nij + 1)
                    - scipy.special.gammaln(N - a[i] - b[j] + nij + 1)
                )
                term3 = np.exp(gln)
                emi += term1[nij] * term2 * term3
    # Calculate entropy for each labeling
    h_true, h_pred = _entropy(reference_indices), _entropy(estimated_indices)
    ami = (mi - emi) / (max(h_true, h_pred) - emi)
    return ami


def _normalized_mutual_info_score(reference_indices, estimated_indices):
    """Compute the mutual information between two sequence labelings, adjusted for
    chance.

    Parameters
    ----------
    reference_indices : np.ndarray
        Array of reference indices
    estimated_indices : np.ndarray
        Array of estimated indices

    Returns
    -------
    nmi : float <= 1.0
        Normalized mutual information
    .. note:: Based on sklearn.metrics.cluster.normalized_mutual_info_score

    """
    ref_classes = np.unique(reference_indices)
    est_classes = np.unique(estimated_indices)
    # Special limit cases: no clustering since the data is not split.
    # This is a perfect match hence return 1.0.
    if (
        ref_classes.shape[0] == est_classes.shape[0] == 1
        or ref_classes.shape[0] == est_classes.shape[0] == 0
    ):
        return 1.0
    contingency = _contingency_matrix(reference_indices, estimated_indices).astype(
        float
    )
    contingency = np.array(contingency, dtype="float")
    # Calculate the MI for the two clusterings
    mi = _mutual_info_score(
        reference_indices, estimated_indices, contingency=contingency
    )
    # Calculate the expected value for the mutual information
    # Calculate entropy for each labeling
    h_true, h_pred = _entropy(reference_indices), _entropy(estimated_indices)
    nmi = mi / max(np.sqrt(h_true * h_pred), 1e-10)
    return nmi


def mutual_information(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
):
    """Frame-clustering segmentation: mutual information metrics.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> mi, ami, nmi = mir_eval.structure.mutual_information(ref_intervals,
    ...                                                      ref_labels,
    ...                                                      est_intervals,
    ...                                                      est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)

    Returns
    -------
    MI : float > 0
        Mutual information between segmentations
    AMI : float
        Adjusted mutual information between segmentations.
    NMI : float > 0
        Normalize mutual information between segmentations

    """
    validate_structure(
        reference_intervals, reference_labels, estimated_intervals, estimated_labels
    )

    # Check for empty annotations.  Don't need to check labels because
    # validate_structure makes sure they're the same size as intervals
    if reference_intervals.size == 0 or estimated_intervals.size == 0:
        return 0.0, 0.0, 0.0

    # Generate the cluster labels
    y_ref = util.intervals_to_samples(
        reference_intervals, reference_labels, sample_size=frame_size
    )[-1]

    y_ref = util.index_labels(y_ref)[0]

    # Map to index space
    y_est = util.intervals_to_samples(
        estimated_intervals, estimated_labels, sample_size=frame_size
    )[-1]

    y_est = util.index_labels(y_est)[0]

    # Mutual information
    mutual_info = _mutual_info_score(y_ref, y_est)

    # Adjusted mutual information
    adj_mutual_info = _adjusted_mutual_info_score(y_ref, y_est)

    # Normalized mutual information
    norm_mutual_info = _normalized_mutual_info_score(y_ref, y_est)

    return mutual_info, adj_mutual_info, norm_mutual_info


def nce(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
    beta=1.0,
    marginal=False,
):
    """Frame-clustering segmentation: normalized conditional entropy

    Computes cross-entropy of cluster assignment, normalized by the
    max-entropy.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> S_over, S_under, S_F = mir_eval.structure.nce(ref_intervals,
    ...                                               ref_labels,
    ...                                               est_intervals,
    ...                                               est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)
    beta : float > 0
        beta for F-measure
        (Default value = 1.0)
    marginal : bool
        If `False`, normalize conditional entropy by uniform entropy.
        If `True`, normalize conditional entropy by the marginal entropy.
        (Default value = False)

    Returns
    -------
    S_over
        Over-clustering score:

        - For `marginal=False`, ``1 - H(y_est | y_ref) / log(|y_est|)``

        - For `marginal=True`, ``1 - H(y_est | y_ref) / H(y_est)``

        If `|y_est|==1`, then `S_over` will be 0.
    S_under
        Under-clustering score:

        - For `marginal=False`, ``1 - H(y_ref | y_est) / log(|y_ref|)``

        - For `marginal=True`, ``1 - H(y_ref | y_est) / H(y_ref)``

        If `|y_ref|==1`, then `S_under` will be 0.
    S_F
        F-measure for (S_over, S_under)
    """
    validate_structure(
        reference_intervals, reference_labels, estimated_intervals, estimated_labels
    )

    # Check for empty annotations.  Don't need to check labels because
    # validate_structure makes sure they're the same size as intervals
    if reference_intervals.size == 0 or estimated_intervals.size == 0:
        return 0.0, 0.0, 0.0

    # Generate the cluster labels
    y_ref = util.intervals_to_samples(
        reference_intervals, reference_labels, sample_size=frame_size
    )[-1]

    y_ref = util.index_labels(y_ref)[0]

    # Map to index space
    y_est = util.intervals_to_samples(
        estimated_intervals, estimated_labels, sample_size=frame_size
    )[-1]

    y_est = util.index_labels(y_est)[0]

    # Make the contingency table: shape = (n_ref, n_est)
    contingency = _contingency_matrix(y_ref, y_est).astype(float)

    # Normalize by the number of frames
    contingency = contingency / len(y_ref)

    # Compute the marginals
    p_est = contingency.sum(axis=0)
    p_ref = contingency.sum(axis=1)

    # H(true | prediction) = sum_j P[estimated = j] *
    # sum_i P[true = i | estimated = j] log P[true = i | estimated = j]
    # entropy sums over axis=0, which is true labels

    true_given_est = p_est.dot(scipy.stats.entropy(contingency, base=2))
    pred_given_ref = p_ref.dot(scipy.stats.entropy(contingency.T, base=2))

    if marginal:
        # Normalize conditional entropy by marginal entropy
        z_ref = scipy.stats.entropy(p_ref, base=2)
        z_est = scipy.stats.entropy(p_est, base=2)
    else:
        z_ref = np.log2(contingency.shape[0])
        z_est = np.log2(contingency.shape[1])

    score_under = 0.0
    if z_ref > 0:
        score_under = 1.0 - true_given_est / z_ref

    score_over = 0.0
    if z_est > 0:
        score_over = 1.0 - pred_given_ref / z_est

    f_measure = util.f_measure(score_over, score_under, beta=beta)

    return score_over, score_under, f_measure


def vmeasure(
    reference_intervals,
    reference_labels,
    estimated_intervals,
    estimated_labels,
    frame_size=0.1,
    beta=1.0,
):
    """Frame-clustering segmentation: v-measure

    Computes cross-entropy of cluster assignment, normalized by the
    marginal-entropy.

    This is equivalent to `nce(..., marginal=True)`.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> # Trim or pad the estimate to match reference timing
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.util.adjust_intervals(ref_intervals,
    ...                                               ref_labels,
    ...                                               t_min=0)
    >>> (est_intervals,
    ...  est_labels) = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, t_min=0, t_max=ref_intervals.max())
    >>> V_precision, V_recall, V_F = mir_eval.structure.vmeasure(ref_intervals,
    ...                                                          ref_labels,
    ...                                                          est_intervals,
    ...                                                          est_labels)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    reference_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    estimated_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    frame_size : float > 0
        length (in seconds) of frames for clustering
        (Default value = 0.1)
    beta : float > 0
        beta for F-measure
        (Default value = 1.0)

    Returns
    -------
    V_precision
        Over-clustering score:
        ``1 - H(y_est | y_ref) / H(y_est)``

        If `|y_est|==1`, then `V_precision` will be 0.
    V_recall
        Under-clustering score:
        ``1 - H(y_ref | y_est) / H(y_ref)``

        If `|y_ref|==1`, then `V_recall` will be 0.
    V_F
        F-measure for (V_precision, V_recall)
    """
    return nce(
        reference_intervals,
        reference_labels,
        estimated_intervals,
        estimated_labels,
        frame_size=frame_size,
        beta=beta,
        marginal=True,
    )


def evaluate(ref_intervals, ref_labels, est_intervals, est_labels, **kwargs):
    """Compute all metrics for the given reference and estimated annotations.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> scores = mir_eval.segment.evaluate(ref_intervals, ref_labels,
    ...                                    est_intervals, est_labels)

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n, 2)
        reference segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    ref_labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    est_intervals : np.ndarray, shape=(m, 2)
        estimated segment intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    est_labels : list, shape=(m,)
        estimated segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    **kwargs
        Additional keyword arguments which will be passed to the
        appropriate metric or preprocessing functions.

    Returns
    -------
    scores : dict
        Dictionary of scores, where the key is the metric name (str) and
        the value is the (float) score achieved.
    """
    # Adjust timespan of estimations relative to ground truth
    ref_intervals, ref_labels = util.adjust_intervals(
        ref_intervals, labels=ref_labels, t_min=0.0
    )

    est_intervals, est_labels = util.adjust_intervals(
        est_intervals, labels=est_labels, t_min=0.0, t_max=ref_intervals.max()
    )

    # Now compute all the metrics
    scores = collections.OrderedDict()

    # Boundary detection
    # Force these values for window
    kwargs["window"] = 0.5
    (
        scores["Precision@0.5"],
        scores["Recall@0.5"],
        scores["F-measure@0.5"],
    ) = util.filter_kwargs(detection, ref_intervals, est_intervals, **kwargs)

    kwargs["window"] = 3.0
    (
        scores["Precision@3.0"],
        scores["Recall@3.0"],
        scores["F-measure@3.0"],
    ) = util.filter_kwargs(detection, ref_intervals, est_intervals, **kwargs)

    # Boundary deviation
    scores["Ref-to-est deviation"], scores["Est-to-ref deviation"] = util.filter_kwargs(
        deviation, ref_intervals, est_intervals, **kwargs
    )

    # Pairwise clustering
    (
        scores["Pairwise Precision"],
        scores["Pairwise Recall"],
        scores["Pairwise F-measure"],
    ) = util.filter_kwargs(
        pairwise, ref_intervals, ref_labels, est_intervals, est_labels, **kwargs
    )

    # Rand index
    scores["Rand Index"] = util.filter_kwargs(
        rand_index, ref_intervals, ref_labels, est_intervals, est_labels, **kwargs
    )
    # Adjusted rand index
    scores["Adjusted Rand Index"] = util.filter_kwargs(
        ari, ref_intervals, ref_labels, est_intervals, est_labels, **kwargs
    )

    # Mutual information metrics
    (
        scores["Mutual Information"],
        scores["Adjusted Mutual Information"],
        scores["Normalized Mutual Information"],
    ) = util.filter_kwargs(
        mutual_information,
        ref_intervals,
        ref_labels,
        est_intervals,
        est_labels,
        **kwargs
    )

    # Conditional entropy metrics
    (
        scores["NCE Over"],
        scores["NCE Under"],
        scores["NCE F-measure"],
    ) = util.filter_kwargs(
        nce, ref_intervals, ref_labels, est_intervals, est_labels, **kwargs
    )

    # V-measure metrics
    scores["V Precision"], scores["V Recall"], scores["V-measure"] = util.filter_kwargs(
        vmeasure, ref_intervals, ref_labels, est_intervals, est_labels, **kwargs
    )

    return scores
