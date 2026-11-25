r"""
Chord estimation algorithms produce a list of intervals and labels which denote
the chord being played over each timespan.  They are evaluated by comparing the
estimated chord labels to some reference, usually using a mapping to a chord
subalphabet (e.g. minor and major chords only, all triads, etc.).  There is no
single 'right' way to compare two sequences of chord labels.  Embracing this
reality, every conventional comparison rule is provided.  Comparisons are made
over the different components of each chord (e.g. G:maj(6)/5): the root (G),
the root-invariant active semitones as determined by the quality
shorthand (maj) and scale degrees (6), and the bass interval (5).
This submodule provides functions both for comparing a sequences of chord
labels according to some chord subalphabet mapping and for using these
comparisons to score a sequence of estimated chords against a reference.

Conventions
-----------
A sequence of chord labels is represented as a list of strings, where each
label is the chord name based on the syntax of [#harte2010towards]_.  Reference
and estimated chord label sequences should be of the same length for comparison
functions.  When converting the chord string into its constituent parts,

* Pitch class counting starts at C, e.g. C:0, D:2, E:4, F:5, etc.

* Scale degree is represented as a string of the diatonic interval, relative to
  the root note, e.g. 'b6', '#5', or '7'

* Bass intervals are represented as strings

* Chord bitmaps are positional binary vectors indicating active pitch classes
  and may be absolute or relative depending on context in the code.

If no chord is present at a given point in time, it should have the label 'N',
which is defined in the variable ``mir_eval.chord.NO_CHORD``.

Metrics
-------

* :func:`mir_eval.chord.root`: Only compares the root of the chords.

* :func:`mir_eval.chord.majmin`: Only compares major, minor, and "no chord"
  labels.

* :func:`mir_eval.chord.majmin_inv`: Compares major/minor chords, with
  inversions.  The bass note must exist in the triad.

* :func:`mir_eval.chord.mirex`: A estimated chord is considered correct if it
  shares *at least* three pitch classes in common.

* :func:`mir_eval.chord.thirds`: Chords are compared at the level of major or
  minor thirds (root and third), For example, both ('A:7', 'A:maj') and
  ('A:min', 'A:dim') are equivalent, as the third is major and minor in
  quality, respectively.

* :func:`mir_eval.chord.thirds_inv`: Same as above, with inversions (bass
  relationships).

* :func:`mir_eval.chord.triads`: Chords are considered at the level of triads
  (major, minor, augmented, diminished, suspended), meaning that, in addition
  to the root, the quality is only considered through #5th scale degree (for
  augmented chords). For example, ('A:7', 'A:maj') are equivalent, while
  ('A:min', 'A:dim') and ('A:aug', 'A:maj') are not.

* :func:`mir_eval.chord.triads_inv`: Same as above, with inversions (bass
  relationships).

* :func:`mir_eval.chord.tetrads`: Chords are considered at the level of the
  entire quality in closed voicing, i.e. spanning only a single octave;
  extended chords (9's, 11's and 13's) are rolled into a single octave with any
  upper voices included as extensions. For example, ('A:7', 'A:9') are
  equivalent but ('A:7', 'A:maj7') are not.

* :func:`mir_eval.chord.tetrads_inv`: Same as above, with inversions (bass
  relationships).

* :func:`mir_eval.chord.sevenths`: Compares according to MIREX "sevenths"
  rules; that is, only major, major seventh, seventh, minor, minor seventh and
  no chord labels are compared.

* :func:`mir_eval.chord.sevenths_inv`: Same as above, with inversions (bass
  relationships).

* :func:`mir_eval.chord.overseg`: Computes the level of over-segmentation
  between estimated and reference intervals.

* :func:`mir_eval.chord.underseg`: Computes the level of under-segmentation
  between estimated and reference intervals.

* :func:`mir_eval.chord.seg`: Computes the minimum of over- and
  under-segmentation between estimated and reference intervals.

References
----------
    .. [#harte2010towards] C. Harte. Towards Automatic Extraction of Harmony
        Information from Music Signals. PhD thesis, Queen Mary University of
        London, August 2010.
"""

import numpy as np
import warnings
import collections

import re

from mir_eval import util

BITMAP_LENGTH = 12
NO_CHORD = "N"
NO_CHORD_ENCODED = -1, np.array([0] * BITMAP_LENGTH), -1
X_CHORD = "X"
X_CHORD_ENCODED = -1, np.array([-1] * BITMAP_LENGTH), -1


class InvalidChordException(Exception):
    r"""Exception class for suspect / invalid chord labels"""

    def __init__(self, message="", chord_label=None):
        self.message = message
        self.chord_label = chord_label
        self.name = self.__class__.__name__
        super().__init__(message)


# --- Chord Primitives ---
def _pitch_classes():
    r"""Map from pitch class (str) to semitone (int)."""
    pitch_classes = ["C", "D", "E", "F", "G", "A", "B"]
    semitones = [0, 2, 4, 5, 7, 9, 11]
    return {c: s for c, s in zip(pitch_classes, semitones)}


def _scale_degrees():
    r"""Map scale degrees (str) to semitones (int)."""
    degrees = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13"]
    semitones = [0, 2, 4, 5, 7, 9, 11, 12, 14, 16, 17, 19, 21]
    return {d: s for d, s in zip(degrees, semitones)}


# Maps pitch classes (strings) to semitone indexes (ints).
PITCH_CLASSES = _pitch_classes()


def pitch_class_to_semitone(pitch_class):
    r"""Convert a pitch class to semitone.

    Parameters
    ----------
    pitch_class : str
        Spelling of a given pitch class, e.g. 'C#', 'Gbb'

    Returns
    -------
    semitone : int
        Semitone value of the pitch class.

    """
    semitone = 0
    for idx, char in enumerate(pitch_class):
        if char == "#" and idx > 0:
            semitone += 1
        elif char == "b" and idx > 0:
            semitone -= 1
        elif idx == 0:
            semitone = PITCH_CLASSES.get(char)
        else:
            raise InvalidChordException(
                "Pitch class improperly formed: %s" % pitch_class
            )
    return semitone % 12


# Maps scale degrees (strings) to semitone indexes (ints).
SCALE_DEGREES = _scale_degrees()


def scale_degree_to_semitone(scale_degree):
    r"""Convert a scale degree to semitone.

    Parameters
    ----------
    scale_degree : str
        Spelling of a relative scale degree, e.g. 'b3', '7', '#5'

    Returns
    -------
    semitone : int
        Relative semitone of the scale degree, wrapped to a single octave

    Raises
    ------
    InvalidChordException if `scale_degree` is invalid.
    """
    semitone = 0
    offset = 0
    if scale_degree.startswith("#"):
        offset = scale_degree.count("#")
        scale_degree = scale_degree.strip("#")
    elif scale_degree.startswith("b"):
        offset = -1 * scale_degree.count("b")
        scale_degree = scale_degree.strip("b")

    semitone = SCALE_DEGREES.get(scale_degree, None)
    if semitone is None:
        raise InvalidChordException(
            "Scale degree improperly formed: {}, expected one of {}.".format(
                scale_degree, list(SCALE_DEGREES.keys())
            )
        )
    return semitone + offset


def scale_degree_to_bitmap(scale_degree, modulo=False, length=BITMAP_LENGTH):
    """Create a bitmap representation of a scale degree.

    Note that values in the bitmap may be negative, indicating that the
    semitone is to be removed.

    Parameters
    ----------
    scale_degree : str
        Spelling of a relative scale degree, e.g. 'b3', '7', '#5'
    modulo : bool, default=True
        If a scale degree exceeds the length of the bit-vector, modulo the
        scale degree back into the bit-vector; otherwise it is discarded.
    length : int, default=12
        Length of the bit-vector to produce

    Returns
    -------
    bitmap : np.ndarray, in [-1, 0, 1], len=`length`
        Bitmap representation of this scale degree.
    """
    sign = 1
    if scale_degree.startswith("*"):
        sign = -1
        scale_degree = scale_degree.strip("*")
    edit_map = [0] * length
    sd_idx = scale_degree_to_semitone(scale_degree)
    if sd_idx < length or modulo:
        edit_map[sd_idx % length] = sign
    return np.array(edit_map)


# Maps quality strings to bitmaps, corresponding to relative pitch class
# semitones, i.e. vector[0] is the tonic.
QUALITIES = {
    #           1     2     3     4  5     6     7
    "maj": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0],
    "min": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
    "aug": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0],
    "dim": [1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0],
    "sus4": [1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0],
    "sus2": [1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0],
    "7": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0],
    "maj7": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1],
    "min7": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0],
    "minmaj7": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1],
    "maj6": [1, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0],
    "min6": [1, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0],
    "dim7": [1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0],
    "hdim7": [1, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 0],
    "maj9": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1],
    "min9": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0],
    "9": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0],
    "min11": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0],
    "11": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0],
    "maj13": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1],
    "min13": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 1, 0],
    "13": [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0],
    "1": [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "5": [1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0],
    "": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
}


def quality_to_bitmap(quality):
    """Return the bitmap for a given quality.

    Parameters
    ----------
    quality : str
        Chord quality name.

    Returns
    -------
    bitmap : np.ndarray
        Bitmap representation of this quality (12-dim).

    """
    if quality not in QUALITIES:
        raise InvalidChordException(
            "Unsupported chord quality shorthand: '%s' "
            "Did you mean to reduce extended chords?" % quality
        )
    return np.array(QUALITIES[quality])


# Maps extended chord qualities to the subset above, translating additional
# voicings to extensions as a set of scale degrees (strings).
# TODO(ejhumphrey): Revisit how minmaj7's are mapped. This is how TMC did it,
#   but MMV handles it like a separate quality (rather than an add7).
EXTENDED_QUALITY_REDUX = {
    "minmaj7": ("min", {"7"}),
    "maj9": ("maj7", {"9"}),
    "min9": ("min7", {"9"}),
    "9": ("7", {"9"}),
    "b9": ("7", {"b9"}),
    "#9": ("7", {"#9"}),
    "11": ("7", {"9", "11"}),
    "#11": ("7", {"9", "#11"}),
    "13": ("7", {"9", "11", "13"}),
    "b13": ("7", {"9", "11", "b13"}),
    "min11": ("min7", {"9", "11"}),
    "maj13": ("maj7", {"9", "11", "13"}),
    "min13": ("min7", {"9", "11", "13"}),
}


def reduce_extended_quality(quality):
    """Map an extended chord quality to a simpler one, moving upper voices to
    a set of scale degree extensions.

    Parameters
    ----------
    quality : str
        Extended chord quality to reduce.

    Returns
    -------
    base_quality : str
        New chord quality.
    extensions : set
        Scale degrees extensions for the quality.

    """
    return EXTENDED_QUALITY_REDUX.get(quality, (quality, set()))


# --- Chord Label Parsing ---
# This monster regexp is pulled from the JAMS chord namespace,
# which is in turn derived from the context-free grammar of
# Harte et al., 2005.
CHORD_RE = re.compile(
    r"""^((N|X)|(([A-G](b*|#*))((:(maj|min|dim|aug|1|5|sus2|sus4|maj6|min6|7|maj7|min7|dim7|hdim7|minmaj7|aug7|9|maj9|min9|11|maj11|min11|13|maj13|min13)(\((\*?((b*|#*)([1-9]|1[0-3]?))(,\*?((b*|#*)([1-9]|1[0-3]?)))*)\))?)|(:\((\*?((b*|#*)([1-9]|1[0-3]?))(,\*?((b*|#*)([1-9]|1[0-3]?)))*)\)))?((/((b*|#*)([1-9]|1[0-3]?)))?)?))$"""
)  # nopep8


def validate_chord_label(chord_label):
    """Test for well-formedness of a chord label.

    Parameters
    ----------
    chord_label : str
        Chord label to validate.
    """
    if not CHORD_RE.match(chord_label):
        raise InvalidChordException("Invalid chord label: " "{}".format(chord_label))
    pass


def split(chord_label, reduce_extended_chords=False):
    """Parse a chord label into its four constituent parts:
        - root
        - quality shorthand
        - scale degrees
        - bass

    Note: Chords lacking quality AND interval information are major.
      - If a quality is specified, it is returned.
      - If an interval is specified WITHOUT a quality, the quality field is
        empty.

    Some examples::

        'C' -> ['C', 'maj', {}, '1']
        'G#:min(*b3,*5)/5' -> ['G#', 'min', {'*b3', '*5'}, '5']
        'A:(3)/6' -> ['A', '', {'3'}, '6']

    Parameters
    ----------
    chord_label : str
        A chord label.
    reduce_extended_chords : bool
        Whether to map the upper voicings of extended chords (9's, 11's, 13's)
        to semitone extensions. (Default value = False)

    Returns
    -------
    chord_parts : list
        Split version of the chord label.

    """
    chord_label = str(chord_label)
    validate_chord_label(chord_label)
    if chord_label == NO_CHORD:
        return [chord_label, "", set(), ""]

    bass = "1"
    if "/" in chord_label:
        chord_label, bass = chord_label.split("/")

    scale_degrees = set()
    omission = False
    if "(" in chord_label:
        chord_label, scale_degrees = chord_label.split("(")
        omission = "*" in scale_degrees
        scale_degrees = scale_degrees.strip(")")
        scale_degrees = {i.strip() for i in scale_degrees.split(",")}

    # Note: Chords lacking quality AND added interval information are major.
    #   If a quality shorthand is specified, it is returned.
    #   If an interval is specified WITHOUT a quality, the quality field is
    #     empty.
    #   Intervals specifying omissions MUST have a quality.
    if omission and ":" not in chord_label:
        raise InvalidChordException(
            "Intervals specifying omissions MUST have a quality."
        )
    quality = "" if scale_degrees else "maj"
    if ":" in chord_label:
        chord_root, quality_name = chord_label.split(":")
        # Extended chords (with ":"s) may not explicitly have Major qualities,
        # so only overwrite the default if the string is not empty.
        if quality_name:
            quality = quality_name.lower()
    else:
        chord_root = chord_label

    if reduce_extended_chords:
        quality, addl_scale_degrees = reduce_extended_quality(quality)
        scale_degrees.update(addl_scale_degrees)

    return [chord_root, quality, scale_degrees, bass]


def join(chord_root, quality="", extensions=None, bass=""):
    r"""Join the parts of a chord into a complete chord label.

    Parameters
    ----------
    chord_root : str
        Root pitch class of the chord, e.g. 'C', 'Eb'
    quality : str
        Quality of the chord, e.g. 'maj', 'hdim7'
        (Default value = '')
    extensions : list
        Any added or absent scaled degrees for this chord, e.g. ['4', '\*3']
        (Default value = None)
    bass : str
        Scale degree of the bass note, e.g. '5'.
        (Default value = '')

    Returns
    -------
    chord_label : str
        A complete chord label.

    """
    chord_label = chord_root
    if quality or extensions:
        chord_label += ":%s" % quality
    if extensions:
        chord_label += "(%s)" % ",".join(extensions)
    if bass and bass != "1":
        chord_label += "/%s" % bass
    validate_chord_label(chord_label)
    return chord_label


# --- Chords to Numerical Representations ---
def encode(chord_label, reduce_extended_chords=False, strict_bass_intervals=False):
    """Translate a chord label to numerical representations for evaluation.

    Parameters
    ----------
    chord_label : str
        Chord label to encode.
    reduce_extended_chords : bool
        Whether to map the upper voicings of extended chords (9's, 11's, 13's)
        to semitone extensions.
        (Default value = False)
    strict_bass_intervals : bool
        Whether to require that the bass scale degree is present in the chord.
        (Default value = False)

    Returns
    -------
    root_number : int
        Absolute semitone of the chord's root.
    semitone_bitmap : np.ndarray, dtype=int
        12-dim vector of relative semitones in the chord spelling.
    bass_number : int
        Relative semitone of the chord's bass note, e.g. 0=root, 7=fifth, etc.
    """
    if chord_label == NO_CHORD:
        return NO_CHORD_ENCODED
    if chord_label == X_CHORD:
        return X_CHORD_ENCODED
    chord_root, quality, scale_degrees, bass = split(
        chord_label, reduce_extended_chords=reduce_extended_chords
    )

    root_number = pitch_class_to_semitone(chord_root)
    bass_number = scale_degree_to_semitone(bass) % 12

    semitone_bitmap = quality_to_bitmap(quality)
    semitone_bitmap[0] = 1

    for scale_degree in scale_degrees:
        semitone_bitmap += scale_degree_to_bitmap(scale_degree, reduce_extended_chords)

    semitone_bitmap = (semitone_bitmap > 0).astype(np.int64)
    if not semitone_bitmap[bass_number] and strict_bass_intervals:
        raise InvalidChordException(
            "Given bass scale degree is absent from this chord: " "%s" % chord_label,
            chord_label,
        )
    else:
        semitone_bitmap[bass_number] = 1
    return root_number, semitone_bitmap, bass_number


def encode_many(chord_labels, reduce_extended_chords=False):
    """Translate a set of chord labels to numerical representations for sane
    evaluation.

    Parameters
    ----------
    chord_labels : list
        Set of chord labels to encode.
    reduce_extended_chords : bool
        Whether to map the upper voicings of extended chords (9's, 11's, 13's)
        to semitone extensions.
        (Default value = False)

    Returns
    -------
    root_number : np.ndarray, dtype=int
        Absolute semitone of the chord's root.
    interval_bitmap : np.ndarray, dtype=int
        12-dim vector of relative semitones in the given chord quality.
    bass_number : np.ndarray, dtype=int
        Relative semitones of the chord's bass notes.

    """
    num_items = len(chord_labels)
    roots, basses = np.zeros([2, num_items], dtype=np.int64)
    semitones = np.zeros([num_items, 12], dtype=np.int64)
    local_cache = dict()
    for i, label in enumerate(chord_labels):
        result = local_cache.get(label, None)
        if result is None:
            result = encode(label, reduce_extended_chords)
            local_cache[label] = result
        roots[i], semitones[i], basses[i] = result
    return roots, semitones, basses


def rotate_bitmap_to_root(bitmap, chord_root):
    """Circularly shift a relative bitmap to its absolute pitch classes.

    For clarity, the best explanation is an example. Given 'G:Maj', the root
    and quality map are as follows::

        root=5
        quality=[1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0]  # Relative chord shape

    After rotating to the root, the resulting bitmap becomes::

        abs_quality = [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1]  # G, B, and D

    Parameters
    ----------
    bitmap : np.ndarray, shape=(12,)
        Bitmap of active notes, relative to the given root.
    chord_root : int
        Absolute pitch class number.

    Returns
    -------
    bitmap : np.ndarray, shape=(12,)
        Absolute bitmap of active pitch classes.

    """
    bitmap = np.asarray(bitmap)
    assert bitmap.ndim == 1, "Currently only 1D bitmaps are supported."
    idxs = list(np.nonzero(bitmap))
    idxs[-1] = (idxs[-1] + chord_root) % 12
    abs_bitmap = np.zeros_like(bitmap)
    abs_bitmap[tuple(idxs)] = 1
    return abs_bitmap


def rotate_bitmaps_to_roots(bitmaps, roots):
    """Circularly shift a relative bitmaps to absolute pitch classes.

    See :func:`rotate_bitmap_to_root` for more information.

    Parameters
    ----------
    bitmaps : np.ndarray, shape=(N, 12)
        Bitmap of active notes, relative to the given root.
    roots : np.ndarray, shape=(N,)
        Absolute pitch class number.

    Returns
    -------
    bitmap : np.ndarray, shape=(N, 12)
        Absolute bitmaps of active pitch classes.

    """
    abs_bitmaps = []
    for bitmap, chord_root in zip(bitmaps, roots):
        abs_bitmaps.append(rotate_bitmap_to_root(bitmap, chord_root))
    return np.asarray(abs_bitmaps)


# --- Comparison Routines ---
def validate(reference_labels, estimated_labels):
    """Check that the input annotations to a comparison function look like
    valid chord labels.

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.
    """
    N = len(reference_labels)
    M = len(estimated_labels)
    if N != M:
        raise ValueError(
            "Chord comparison received different length lists: "
            "len(reference)=%d\tlen(estimates)=%d" % (N, M)
        )
    for labels in [reference_labels, estimated_labels]:
        for chord_label in labels:
            validate_chord_label(chord_label)
    # When either label list is empty, warn the user
    if len(reference_labels) == 0:
        warnings.warn("Reference labels are empty")
    if len(estimated_labels) == 0:
        warnings.warn("Estimated labels are empty")


def weighted_accuracy(comparisons, weights):
    """Compute the weighted accuracy of a list of chord comparisons.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> # Here, we're using the "thirds" function to compare labels
    >>> # but any of the comparison functions would work.
    >>> comparisons = mir_eval.chord.thirds(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    comparisons : np.ndarray
        List of chord comparison scores, in [0, 1] or -1
    weights : np.ndarray
        Weights (not necessarily normalized) for each comparison.
        This can be a list of interval durations

    Returns
    -------
    score : float
        Weighted accuracy

    """
    N = len(comparisons)
    # There should be as many weights as comparisons
    if weights.shape[0] != N:
        raise ValueError(
            "weights and comparisons should be of the same"
            " length. len(weights) = {} but len(comparisons)"
            " = {}".format(weights.shape[0], N)
        )
    if (weights < 0).any():
        raise ValueError("Weights should all be positive.")
    if np.sum(weights) == 0:
        warnings.warn("No nonzero weights, returning 0")
        return 0
    # Find all comparison scores which are valid
    valid_idx = comparisons >= 0
    # If no comparable chords were provided, warn and return 0
    if valid_idx.sum() == 0:
        warnings.warn(
            "No reference chords were comparable " "to estimated chords, returning 0."
        )
        return 0
    # Remove any uncomparable labels
    comparisons = comparisons[valid_idx]
    weights = weights[valid_idx]
    # Normalize the weights
    total_weight = float(np.sum(weights))
    normalized_weights = np.asarray(weights, dtype=float) / total_weight
    # Score is the sum of all weighted comparisons
    return np.sum(comparisons * normalized_weights)


def thirds(reference_labels, estimated_labels):
    """Compare chords along root & third relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.thirds(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones = encode_many(reference_labels, False)[:2]
    est_roots, est_semitones = encode_many(estimated_labels, False)[:2]

    eq_roots = ref_roots == est_roots
    eq_thirds = ref_semitones[:, 3] == est_semitones[:, 3]
    comparison_scores = (eq_roots * eq_thirds).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def thirds_inv(reference_labels, estimated_labels):
    """Score chords along root, third, & bass relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.thirds_inv(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones, ref_bass = encode_many(reference_labels, False)
    est_roots, est_semitones, est_bass = encode_many(estimated_labels, False)

    eq_root = ref_roots == est_roots
    eq_bass = ref_bass == est_bass
    eq_third = ref_semitones[:, 3] == est_semitones[:, 3]
    comparison_scores = (eq_root * eq_third * eq_bass).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def triads(reference_labels, estimated_labels):
    """Compare chords along triad (root & quality to #5) relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.triads(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones = encode_many(reference_labels, False)[:2]
    est_roots, est_semitones = encode_many(estimated_labels, False)[:2]

    eq_roots = ref_roots == est_roots
    eq_semitones = np.all(np.equal(ref_semitones[:, :8], est_semitones[:, :8]), axis=1)
    comparison_scores = (eq_roots * eq_semitones).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def triads_inv(reference_labels, estimated_labels):
    """Score chords along triad (root, quality to #5, & bass) relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.triads_inv(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones, ref_bass = encode_many(reference_labels, False)
    est_roots, est_semitones, est_bass = encode_many(estimated_labels, False)

    eq_roots = ref_roots == est_roots
    eq_basses = ref_bass == est_bass
    eq_semitones = np.all(np.equal(ref_semitones[:, :8], est_semitones[:, :8]), axis=1)
    comparison_scores = (eq_roots * eq_semitones * eq_basses).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def tetrads(reference_labels, estimated_labels):
    """Compare chords along tetrad (root & full quality) relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.tetrads(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones = encode_many(reference_labels, False)[:2]
    est_roots, est_semitones = encode_many(estimated_labels, False)[:2]

    eq_roots = ref_roots == est_roots
    eq_semitones = np.all(np.equal(ref_semitones, est_semitones), axis=1)
    comparison_scores = (eq_roots * eq_semitones).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def tetrads_inv(reference_labels, estimated_labels):
    """Compare chords along tetrad (root, full quality, & bass) relationships.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.tetrads_inv(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones, ref_bass = encode_many(reference_labels, False)
    est_roots, est_semitones, est_bass = encode_many(estimated_labels, False)

    eq_roots = ref_roots == est_roots
    eq_basses = ref_bass == est_bass
    eq_semitones = np.all(np.equal(ref_semitones, est_semitones), axis=1)
    comparison_scores = (eq_roots * eq_semitones * eq_basses).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def root(reference_labels, estimated_labels):
    """Compare chords according to roots.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.root(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0], or -1 if the comparison is out of
        gamut.
    """
    validate(reference_labels, estimated_labels)
    ref_roots, ref_semitones = encode_many(reference_labels, False)[:2]
    est_roots = encode_many(estimated_labels, False)[0]
    comparison_scores = (ref_roots == est_roots).astype(np.float64)

    # Ignore 'X' chords
    comparison_scores[np.any(ref_semitones < 0, axis=1)] = -1.0
    return comparison_scores


def mirex(reference_labels, estimated_labels):
    """Compare chords along MIREX rules.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.mirex(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0]

    """
    validate(reference_labels, estimated_labels)
    # TODO(?): Should this be an argument?
    min_intersection = 3
    ref_data = encode_many(reference_labels, False)
    ref_chroma = rotate_bitmaps_to_roots(ref_data[1], ref_data[0])
    est_data = encode_many(estimated_labels, False)
    est_chroma = rotate_bitmaps_to_roots(est_data[1], est_data[0])

    eq_chroma = (ref_chroma * est_chroma).sum(axis=-1)

    # Chroma matching for set bits
    comparison_scores = (eq_chroma >= min_intersection).astype(np.float64)

    # No-chord matching; match -1 roots, SKIP_CHORDS dropped next
    no_root = np.logical_and(ref_data[0] == -1, est_data[0] == -1)
    comparison_scores[no_root] = 1.0

    # Skip chords where the number of active semitones `n` is
    #   0 < n < `min_intersection`.
    ref_semitone_count = (ref_data[1] > 0).sum(axis=1)
    skip_idx = np.logical_and(
        ref_semitone_count > 0, ref_semitone_count < min_intersection
    )
    # Also ignore 'X' chords.
    np.logical_or(skip_idx, np.any(ref_data[1] < 0, axis=1), skip_idx)
    comparison_scores[skip_idx] = -1.0
    return comparison_scores


def majmin(reference_labels, estimated_labels):
    """Compare chords along major-minor rules. Chords with qualities outside
    Major/minor/no-chord are ignored.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.majmin(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0], or -1 if the comparison is out of
        gamut.

    """
    validate(reference_labels, estimated_labels)
    maj_semitones = np.array(QUALITIES["maj"][:8])
    min_semitones = np.array(QUALITIES["min"][:8])

    ref_roots, ref_semitones, _ = encode_many(reference_labels, False)
    est_roots, est_semitones, _ = encode_many(estimated_labels, False)

    eq_root = ref_roots == est_roots
    eq_quality = np.all(np.equal(ref_semitones[:, :8], est_semitones[:, :8]), axis=1)
    comparison_scores = (eq_root * eq_quality).astype(np.float64)

    # Test for Major / Minor / No-chord
    is_maj = np.all(np.equal(ref_semitones[:, :8], maj_semitones), axis=1)
    is_min = np.all(np.equal(ref_semitones[:, :8], min_semitones), axis=1)
    is_none = np.logical_and(ref_roots < 0, np.all(ref_semitones == 0, axis=1))

    # Only keep majors, minors, and Nones (NOR)
    comparison_scores[(is_maj + is_min + is_none) == 0] = -1

    # Disable chords that disrupt this quality (apparently)
    # ref_voicing = np.all(np.equal(ref_qualities[:, :8],
    #                               ref_notes[:, :8]), axis=1)
    # comparison_scores[ref_voicing == 0] = -1
    # est_voicing = np.all(np.equal(est_qualities[:, :8],
    #                               est_notes[:, :8]), axis=1)
    # comparison_scores[est_voicing == 0] = -1
    return comparison_scores


def majmin_inv(reference_labels, estimated_labels):
    """Compare chords along major-minor rules, with inversions. Chords with
    qualities outside Major/minor/no-chord are ignored, and the bass note must
    exist in the triad (bass in [1, 3, 5]).

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.majmin_inv(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0], or -1 if the comparison is out of
        gamut.

    """
    validate(reference_labels, estimated_labels)
    maj_semitones = np.array(QUALITIES["maj"][:8])
    min_semitones = np.array(QUALITIES["min"][:8])

    ref_roots, ref_semitones, ref_bass = encode_many(reference_labels, False)
    est_roots, est_semitones, est_bass = encode_many(estimated_labels, False)

    eq_root_bass = (ref_roots == est_roots) * (ref_bass == est_bass)
    eq_semitones = np.all(np.equal(ref_semitones[:, :8], est_semitones[:, :8]), axis=1)
    comparison_scores = (eq_root_bass * eq_semitones).astype(np.float64)

    # Test for Major / Minor / No-chord
    is_maj = np.all(np.equal(ref_semitones[:, :8], maj_semitones), axis=1)
    is_min = np.all(np.equal(ref_semitones[:, :8], min_semitones), axis=1)
    is_none = np.logical_and(ref_roots < 0, np.all(ref_semitones == 0, axis=1))

    # Only keep majors, minors, and Nones (NOR)
    comparison_scores[(is_maj + is_min + is_none) == 0] = -1

    # Disable inversions that are not part of the quality
    valid_inversion = np.ones(ref_bass.shape, dtype=bool)
    bass_idx = ref_bass >= 0
    valid_inversion[bass_idx] = ref_semitones[bass_idx, ref_bass[bass_idx]]
    comparison_scores[valid_inversion == 0] = -1
    return comparison_scores


def sevenths(reference_labels, estimated_labels):
    """Compare chords along MIREX 'sevenths' rules. Chords with qualities
    outside [maj, maj7, 7, min, min7, N] are ignored.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.sevenths(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0], or -1 if the comparison is out of
        gamut.

    """
    validate(reference_labels, estimated_labels)
    seventh_qualities = ["maj", "min", "maj7", "7", "min7", ""]
    valid_semitones = np.array([QUALITIES[name] for name in seventh_qualities])

    ref_roots, ref_semitones = encode_many(reference_labels, False)[:2]
    est_roots, est_semitones = encode_many(estimated_labels, False)[:2]

    eq_root = ref_roots == est_roots
    eq_semitones = np.all(np.equal(ref_semitones, est_semitones), axis=1)
    comparison_scores = (eq_root * eq_semitones).astype(np.float64)

    # Test for reference chord inclusion
    is_valid = np.array(
        [
            np.all(np.equal(ref_semitones, semitones), axis=1)
            for semitones in valid_semitones
        ]
    )
    # Drop if NOR
    comparison_scores[np.sum(is_valid, axis=0) == 0] = -1
    return comparison_scores


def sevenths_inv(reference_labels, estimated_labels):
    """Compare chords along MIREX 'sevenths' rules. Chords with qualities
    outside [maj, maj7, 7, min, min7, N] are ignored.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> est_intervals, est_labels = mir_eval.util.adjust_intervals(
    ...     est_intervals, est_labels, ref_intervals.min(),
    ...     ref_intervals.max(), mir_eval.chord.NO_CHORD,
    ...     mir_eval.chord.NO_CHORD)
    >>> (intervals,
    ...  ref_labels,
    ...  est_labels) = mir_eval.util.merge_labeled_intervals(
    ...      ref_intervals, ref_labels, est_intervals, est_labels)
    >>> durations = mir_eval.util.intervals_to_durations(intervals)
    >>> comparisons = mir_eval.chord.sevenths_inv(ref_labels, est_labels)
    >>> score = mir_eval.chord.weighted_accuracy(comparisons, durations)

    Parameters
    ----------
    reference_labels : list, len=n
        Reference chord labels to score against.
    estimated_labels : list, len=n
        Estimated chord labels to score against.

    Returns
    -------
    comparison_scores : np.ndarray, shape=(n,), dtype=float
        Comparison scores, in [0.0, 1.0], or -1 if the comparison is out of
        gamut.

    """
    validate(reference_labels, estimated_labels)
    seventh_qualities = ["maj", "min", "maj7", "7", "min7", ""]
    valid_semitones = np.array([QUALITIES[name] for name in seventh_qualities])

    ref_roots, ref_semitones, ref_basses = encode_many(reference_labels, False)
    est_roots, est_semitones, est_basses = encode_many(estimated_labels, False)

    eq_roots_basses = (ref_roots == est_roots) * (ref_basses == est_basses)
    eq_semitones = np.all(np.equal(ref_semitones, est_semitones), axis=1)
    comparison_scores = (eq_roots_basses * eq_semitones).astype(np.float64)

    # Test for Major / Minor / No-chord
    is_valid = np.array(
        [
            np.all(np.equal(ref_semitones, semitones), axis=1)
            for semitones in valid_semitones
        ]
    )
    comparison_scores[np.sum(is_valid, axis=0) == 0] = -1

    # Disable inversions that are not part of the quality
    valid_inversion = np.ones(ref_basses.shape, dtype=bool)
    bass_idx = ref_basses >= 0
    valid_inversion[bass_idx] = ref_semitones[bass_idx, ref_basses[bass_idx]]
    comparison_scores[valid_inversion == 0] = -1
    return comparison_scores


def directional_hamming_distance(reference_intervals, estimated_intervals):
    """Compute the directional hamming distance between reference and
    estimated intervals as defined by [#harte2010towards]_ and used for MIREX
    'OverSeg', 'UnderSeg' and 'MeanSeg' measures.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> overseg = 1 - mir_eval.chord.directional_hamming_distance(
    ...     ref_intervals, est_intervals)
    >>> underseg = 1 - mir_eval.chord.directional_hamming_distance(
    ...     est_intervals, ref_intervals)
    >>> seg = min(overseg, underseg)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2), dtype=float
        Reference chord intervals to score against.
    estimated_intervals : np.ndarray, shape=(m, 2), dtype=float
        Estimated chord intervals to score against.

    Returns
    -------
    directional hamming distance : float
        directional hamming distance between reference intervals and
        estimated intervals.
    """
    util.validate_intervals(estimated_intervals)
    util.validate_intervals(reference_intervals)

    # make sure chord intervals do not overlap
    if (
        len(reference_intervals) > 1
        and (reference_intervals[:-1, 1] > reference_intervals[1:, 0]).any()
    ):
        raise ValueError("Chord Intervals must not overlap")

    est_ts = np.unique(estimated_intervals.flatten())
    seg = 0.0
    for start, end in reference_intervals:
        dur = end - start
        between_start_end = est_ts[(est_ts >= start) & (est_ts < end)]
        seg_ts = np.hstack([start, between_start_end, end])
        seg += dur - np.diff(seg_ts).max()
    return seg / (reference_intervals[-1, 1] - reference_intervals[0, 0])


def overseg(reference_intervals, estimated_intervals):
    """Compute the MIREX 'OverSeg' score.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> score = mir_eval.chord.overseg(ref_intervals, est_intervals)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2), dtype=float
        Reference chord intervals to score against.
    estimated_intervals : np.ndarray, shape=(m, 2), dtype=float
        Estimated chord intervals to score against.

    Returns
    -------
    oversegmentation score : float
        Comparison score, in [0.0, 1.0], where 1.0 means no oversegmentation.
    """
    return 1 - directional_hamming_distance(reference_intervals, estimated_intervals)


def underseg(reference_intervals, estimated_intervals):
    """Compute the MIREX 'UnderSeg' score.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> score = mir_eval.chord.underseg(ref_intervals, est_intervals)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2), dtype=float
        Reference chord intervals to score against.
    estimated_intervals : np.ndarray, shape=(m, 2), dtype=float
        Estimated chord intervals to score against.

    Returns
    -------
    undersegmentation score : float
        Comparison score, in [0.0, 1.0], where 1.0 means no undersegmentation.
    """
    return 1 - directional_hamming_distance(estimated_intervals, reference_intervals)


def seg(reference_intervals, estimated_intervals):
    """Compute the MIREX 'MeanSeg' score.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> score = mir_eval.chord.seg(ref_intervals, est_intervals)

    Parameters
    ----------
    reference_intervals : np.ndarray, shape=(n, 2), dtype=float
        Reference chord intervals to score against.
    estimated_intervals : np.ndarray, shape=(m, 2), dtype=float
        Estimated chord intervals to score against.

    Returns
    -------
    segmentation score : float
        Comparison score, in [0.0, 1.0], where 1.0 means perfect segmentation.
    """
    return min(
        underseg(reference_intervals, estimated_intervals),
        overseg(reference_intervals, estimated_intervals),
    )


def merge_chord_intervals(intervals, labels):
    """
    Merge consecutive chord intervals if they represent the same chord.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2), dtype=float
        Chord intervals to be merged, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    labels : list, shape=(n,)
        Chord labels to be merged, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.

    Returns
    -------
    merged_ivs : np.ndarray, shape=(k, 2), dtype=float
        Merged chord intervals, k <= n

    """
    roots, semitones, basses = encode_many(labels, True)
    merged_ivs = []
    prev_rt = None
    prev_st = None
    prev_ba = None
    for s, e, rt, st, ba in zip(
        intervals[:, 0], intervals[:, 1], roots, semitones, basses
    ):
        if rt != prev_rt or (st != prev_st).any() or ba != prev_ba:
            prev_rt, prev_st, prev_ba = rt, st, ba
            merged_ivs.append([s, e])
        else:
            merged_ivs[-1][-1] = e
    return np.array(merged_ivs)


def evaluate(ref_intervals, ref_labels, est_intervals, est_labels, **kwargs):
    """Compute weighted accuracy for all comparison functions for the given
    reference and estimated annotations.

    Examples
    --------
    >>> (ref_intervals,
    ...  ref_labels) = mir_eval.io.load_labeled_intervals('ref.lab')
    >>> (est_intervals,
    ...  est_labels) = mir_eval.io.load_labeled_intervals('est.lab')
    >>> scores = mir_eval.chord.evaluate(ref_intervals, ref_labels,
    ...                                  est_intervals, est_labels)

    Parameters
    ----------
    ref_intervals : np.ndarray, shape=(n, 2)
        Reference chord intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    ref_labels : list, shape=(n,)
        reference chord labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    est_intervals : np.ndarray, shape=(m, 2)
        estimated chord intervals, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    est_labels : list, shape=(m,)
        estimated chord labels, in the format returned by
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
    # Append or crop estimated intervals so their span is the same as reference
    est_intervals, est_labels = util.adjust_intervals(
        est_intervals,
        est_labels,
        ref_intervals.min(),
        ref_intervals.max(),
        NO_CHORD,
        NO_CHORD,
    )
    # use merged intervals for segmentation evaluation
    merged_ref_intervals = merge_chord_intervals(ref_intervals, ref_labels)
    merged_est_intervals = merge_chord_intervals(est_intervals, est_labels)
    # Adjust the labels so that they span the same intervals
    intervals, ref_labels, est_labels = util.merge_labeled_intervals(
        ref_intervals, ref_labels, est_intervals, est_labels
    )
    # Convert intervals to durations (used as weights)
    durations = util.intervals_to_durations(intervals)

    # Store scores for each comparison function
    scores = collections.OrderedDict()

    scores["thirds"] = weighted_accuracy(thirds(ref_labels, est_labels), durations)
    scores["thirds_inv"] = weighted_accuracy(
        thirds_inv(ref_labels, est_labels), durations
    )
    scores["triads"] = weighted_accuracy(triads(ref_labels, est_labels), durations)
    scores["triads_inv"] = weighted_accuracy(
        triads_inv(ref_labels, est_labels), durations
    )
    scores["tetrads"] = weighted_accuracy(tetrads(ref_labels, est_labels), durations)
    scores["tetrads_inv"] = weighted_accuracy(
        tetrads_inv(ref_labels, est_labels), durations
    )
    scores["root"] = weighted_accuracy(root(ref_labels, est_labels), durations)
    scores["mirex"] = weighted_accuracy(mirex(ref_labels, est_labels), durations)
    scores["majmin"] = weighted_accuracy(majmin(ref_labels, est_labels), durations)
    scores["majmin_inv"] = weighted_accuracy(
        majmin_inv(ref_labels, est_labels), durations
    )
    scores["sevenths"] = weighted_accuracy(sevenths(ref_labels, est_labels), durations)
    scores["sevenths_inv"] = weighted_accuracy(
        sevenths_inv(ref_labels, est_labels), durations
    )
    scores["underseg"] = underseg(merged_ref_intervals, merged_est_intervals)
    scores["overseg"] = overseg(merged_ref_intervals, merged_est_intervals)
    scores["seg"] = min(scores["overseg"], scores["underseg"])

    return scores
