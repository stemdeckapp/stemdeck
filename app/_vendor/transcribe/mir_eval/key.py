"""
Key Detection involves determining the underlying key (distribution of notes
and note transitions) in a piece of music.  Key detection algorithms are
evaluated by comparing their estimated key to a ground-truth reference key and
reporting a score according to the relationship of the keys.

Conventions
-----------
Keys are represented as strings of the form ``'(key) (mode)'``, e.g. ``'C#
major'`` or ``'Fb minor'``.  The case of the key is ignored.  Note that certain
key strings are equivalent, e.g. ``'C# major'`` and ``'Db major'``.  The mode
may only be specified as either ``'major'`` or ``'minor'``, no other mode
strings will be accepted.

Metrics
-------
* :func:`mir_eval.key.weighted_score`: Heuristic scoring of the relation of two
  keys.
"""

import collections
from . import util

KEY_TO_SEMITONE = {
    "c": 0,
    "c#": 1,
    "db": 1,
    "d": 2,
    "d#": 3,
    "eb": 3,
    "e": 4,
    "f": 5,
    "f#": 6,
    "gb": 6,
    "g": 7,
    "g#": 8,
    "ab": 8,
    "a": 9,
    "a#": 10,
    "bb": 10,
    "b": 11,
    "x": None,
}


def validate_key(key):
    """Check that a key is well-formatted, e.g. in the form ``'C# major'``.
    The Key can be 'X' if it is not possible to categorize the Key and mode
    can be 'other' if it can't be categorized as major or minor.

    Parameters
    ----------
    key : str
        Key to verify
    """
    if len(key.split()) != 2 and not (len(key.split()) and key.lower() == "x"):
        raise ValueError("'{}' is not in the form '(key) (mode)' " "or 'X'".format(key))
    if key.lower() != "x":
        key, mode = key.split()

        if key.lower() == "x":
            raise ValueError(
                "Mode {} is invalid; 'X' (Uncategorized) "
                "doesn't have mode".format(mode)
            )
        if key.lower() not in KEY_TO_SEMITONE:
            raise ValueError(
                "Key {} is invalid; should be e.g. D or C# or Eb or "
                "X (Uncategorized)".format(key)
            )
        if mode not in ["major", "minor", "other"]:
            raise ValueError(
                f"Mode '{mode}' is invalid; must be 'major', 'minor' or 'other'"
            )


def validate(reference_key, estimated_key):
    """Check that the input annotations to a metric are valid key strings and
    throws helpful errors if not.

    Parameters
    ----------
    reference_key : str
        Reference key string.
    estimated_key : str
        Estimated key string.
    """
    for key in [reference_key, estimated_key]:
        validate_key(key)


def split_key_string(key):
    """Split a key string (of the form, e.g. ``'C# major'``), into a tuple of
    ``(key, mode)`` where ``key`` is is an integer representing the semitone
    distance from C.

    Parameters
    ----------
    key : str
        String representing a key.

    Returns
    -------
    key : int
        Number of semitones above C.
    mode : str
        String representing the mode.
    """
    if key.lower() != "x":
        key, mode = key.split()
    else:
        mode = None
    return KEY_TO_SEMITONE[key.lower()], mode


def weighted_score(reference_key, estimated_key):
    """Compute a heuristic score which is weighted according to the
    relationship of the reference and estimated key, as follows:

    +------------------------------------------------------+-------+
    | Relationship                                         | Score |
    +------------------------------------------------------+-------+
    | Same key and mode                                    | 1.0   |
    +------------------------------------------------------+-------+
    | Estimated key is a perfect fifth above reference key | 0.5   |
    +------------------------------------------------------+-------+
    | Relative major/minor (same key signature)            | 0.3   |
    +------------------------------------------------------+-------+
    | Parallel major/minor (same key)                      | 0.2   |
    +------------------------------------------------------+-------+
    | Other                                                | 0.0   |
    +------------------------------------------------------+-------+

    Examples
    --------
    >>> ref_key = mir_eval.io.load_key('ref.txt')
    >>> est_key = mir_eval.io.load_key('est.txt')
    >>> score = mir_eval.key.weighted_score(ref_key, est_key)

    Parameters
    ----------
    reference_key : str
        Reference key string.
    estimated_key : str
        Estimated key string.

    Returns
    -------
    score : float
        Score representing how closely related the keys are.
    """
    validate(reference_key, estimated_key)
    reference_key, reference_mode = split_key_string(reference_key)
    estimated_key, estimated_mode = split_key_string(estimated_key)
    # If keys are the same, return 1.
    if reference_key == estimated_key and reference_mode == estimated_mode:
        return 1.0
    # If reference or estimated key are x and they are not the same key
    # then the result is 'Other'.
    if reference_key is None or estimated_key is None:
        return 0.0
    # If keys are the same mode and a perfect fifth (differ by 7 semitones)
    if estimated_mode == reference_mode and (estimated_key - reference_key) % 12 == 7:
        return 0.5
    # Estimated key is relative minor of reference key (9 semitones)
    if (
        estimated_mode != reference_mode == "major"
        and (estimated_key - reference_key) % 12 == 9
    ):
        return 0.3
    # Estimated key is relative major of reference key (3 semitones)
    if (
        estimated_mode != reference_mode == "minor"
        and (estimated_key - reference_key) % 12 == 3
    ):
        return 0.3
    # If keys are in different modes and parallel (same key name)
    if estimated_mode != reference_mode and reference_key == estimated_key:
        return 0.2
    # Otherwise return 0
    return 0.0


def evaluate(reference_key, estimated_key, **kwargs):
    """Compute all metrics for the given reference and estimated annotations.

    Examples
    --------
    >>> ref_key = mir_eval.io.load_key('reference.txt')
    >>> est_key = mir_eval.io.load_key('estimated.txt')
    >>> scores = mir_eval.key.evaluate(ref_key, est_key)

    Parameters
    ----------
    reference_key : str
        Reference key string.
    estimated_key : str
        Estimated key string.
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

    scores["Weighted Score"] = util.filter_kwargs(
        weighted_score, reference_key, estimated_key
    )

    return scores
