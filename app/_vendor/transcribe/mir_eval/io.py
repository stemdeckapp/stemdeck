"""Functions for loading annotations from files in different formats."""

import contextlib
import numpy as np
import re
import warnings
import scipy.io.wavfile

from . import util
from . import key
from . import tempo


@contextlib.contextmanager
def _open(file_or_path, **kwargs):
    """Either open a file handle, or use an existing file-like object.

    If `file_or_path` has the `read` attribute, it will return `file_or_path`.

    Otherwise, it will attempt to open the file at the specified location.
    """
    if hasattr(file_or_path, "read"):
        yield file_or_path
    else:
        try:
            with open(file_or_path, **kwargs) as file_desc:
                yield file_desc
        except TypeError as exc:
            raise IOError(f"Invalid file-or-path object: {file_or_path}") from exc


def load_delimited(filename, converters, delimiter=r"\s+", comment="#"):
    r"""Load data from an annotation file where columns are delimited.
    The number of columns is inferred from the length of the provided converters list.

    Examples
    --------
    >>> # Load in a one-column list of event times (floats)
    >>> load_delimited('events.txt', [float])
    >>> # Load in a list of labeled events, separated by commas
    >>> load_delimited('labeled_events.csv', [float, str], ',')

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    converters : list of functions
        Each entry in column ``n`` of the file will be cast by the function
        ``converters[n]``.

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    columns : tuple of lists
        Each list in this tuple corresponds to values in one of the columns
        in the file.
    """
    # Initialize list of empty lists
    n_columns = len(converters)
    columns = tuple(list() for _ in range(n_columns))

    # Create re object for splitting lines
    splitter = re.compile(delimiter)

    # And one for comments
    if comment is None:
        commenter = None
    else:
        commenter = re.compile(f"^{comment}")

    # Note: we do io manually here for two reasons.
    #   1. The csv module has difficulties with unicode, which may lead
    #      to failures on certain annotation strings
    #
    #   2. numpy's text loader does not handle non-numeric data
    #
    with _open(filename, mode="r") as input_file:
        for row, line in enumerate(input_file, 1):
            # Skip commented lines
            if comment is not None and commenter.match(line):
                continue

            # Split each line using the supplied delimiter
            data = splitter.split(line.strip(), n_columns - 1)

            # Throw a helpful error if we got an unexpected # of columns
            if n_columns != len(data):
                raise ValueError(
                    "Expected {} columns, got {} at "
                    "{}:{:d}:\n\t{}".format(n_columns, len(data), filename, row, line)
                )

            for value, column, converter in zip(data, columns, converters):
                # Try converting the value, throw a helpful error on failure
                try:
                    converted_value = converter(value)
                except:
                    raise ValueError(
                        "Couldn't convert value {} using {} "
                        "found at {}:{:d}:\n\t{}".format(
                            value, converter.__name__, filename, row, line
                        )
                    )
                column.append(converted_value)

    # Sane output
    if n_columns == 1:
        return columns[0]
    else:
        return columns


def load_events(filename, delimiter=r"\s+", comment="#"):
    r"""Import time-stamp events from an annotation file.  The file should
    consist of a single column of numeric values corresponding to the event
    times. This is primarily useful for processing events which lack duration,
    such as beats or onsets.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    event_times : np.ndarray
        array of event times (float)

    """
    # Use our universal function to load in the events
    events = load_delimited(filename, [float], delimiter=delimiter, comment=comment)
    events = np.array(events)
    # Validate them, but throw a warning in place of an error
    try:
        util.validate_events(events)
    except ValueError as error:
        warnings.warn(error.args[0])

    return events


def load_labeled_events(filename, delimiter=r"\s+", comment="#"):
    r"""Import labeled time-stamp events from an annotation file.  The file should
    consist of two columns; the first having numeric values corresponding to
    the event times and the second having string labels for each event.  This
    is primarily useful for processing labeled events which lack duration, such
    as beats with metric beat number or onsets with an instrument label.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    event_times : np.ndarray
        array of event times (float)
    labels : list of str
        list of labels

    """
    # Use our universal function to load in the events
    events, labels = load_delimited(
        filename, [float, str], delimiter=delimiter, comment=comment
    )
    events = np.array(events)
    # Validate them, but throw a warning in place of an error
    try:
        util.validate_events(events)
    except ValueError as error:
        warnings.warn(error.args[0])

    return events, labels


def load_intervals(filename, delimiter=r"\s+", comment="#"):
    r"""Import intervals from an annotation file.  The file should consist of two
    columns of numeric values corresponding to start and end time of each
    interval.  This is primarily useful for processing events which span a
    duration, such as segmentation, chords, or instrument activation.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    intervals : np.ndarray, shape=(n_events, 2)
        array of event start and end times

    """
    # Use our universal function to load in the events
    starts, ends = load_delimited(
        filename, [float, float], delimiter=delimiter, comment=comment
    )
    # Stack into an interval matrix
    intervals = np.array([starts, ends]).T
    # Validate them, but throw a warning in place of an error
    try:
        util.validate_intervals(intervals)
    except ValueError as error:
        warnings.warn(error.args[0])

    return intervals


def load_labeled_intervals(filename, delimiter=r"\s+", comment="#"):
    r"""Import labeled intervals from an annotation file.  The file should consist
    of three columns: Two consisting of numeric values corresponding to start
    and end time of each interval and a third corresponding to the label of
    each interval.  This is primarily useful for processing events which span a
    duration, such as segmentation, chords, or instrument activation.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    intervals : np.ndarray, shape=(n_events, 2)
        array of event start and end time
    labels : list of str
        list of labels

    """
    # Use our universal function to load in the events
    starts, ends, labels = load_delimited(
        filename, [float, float, str], delimiter=delimiter, comment=comment
    )
    # Stack into an interval matrix
    intervals = np.array([starts, ends]).T
    # Validate them, but throw a warning in place of an error
    try:
        util.validate_intervals(intervals)
    except ValueError as error:
        warnings.warn(error.args[0])

    return intervals, labels


def load_time_series(filename, delimiter=r"\s+", comment="#"):
    r"""Import a time series from an annotation file.  The file should consist of
    two columns of numeric values corresponding to the time and value of each
    sample of the time series.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    times : np.ndarray
        array of timestamps (float)
    values : np.ndarray
        array of corresponding numeric values (float)

    """
    # Use our universal function to load in the events
    times, values = load_delimited(
        filename, [float, float], delimiter=delimiter, comment=comment
    )
    times = np.array(times)
    values = np.array(values)

    return times, values


def load_patterns(filename):
    """Load the patterns contained in the filename and puts them into a list
    of patterns, each pattern being a list of occurrence, and each
    occurrence being a list of (onset, midi) pairs.

    The input file must be formatted as described in MIREX 2013:
    http://www.music-ir.org/mirex/wiki/2013:Discovery_of_Repeated_Themes_%26_Sections

    Parameters
    ----------
    filename : str or `os.Pathlike`
        The input file path containing the patterns of a given piece using the
        MIREX 2013 format.

    Returns
    -------
    pattern_list : list
        The list of patterns, containing all their occurrences,
        using the following format::

            onset_midi = (onset_time, midi_number)
            occurrence = [onset_midi1, ..., onset_midiO]
            pattern = [occurrence1, ..., occurrenceM]
            pattern_list = [pattern1, ..., patternN]

        where ``N`` is the number of patterns, ``M[i]`` is the number of
        occurrences of the ``i`` th pattern, and ``O[j]`` is the number of
        onsets in the ``j``'th occurrence.  E.g.::

            occ1 = [(0.5, 67.0), (1.0, 67.0), (1.5, 67.0), (2.0, 64.0)]
            occ2 = [(4.5, 65.0), (5.0, 65.0), (5.5, 65.0), (6.0, 62.0)]
            pattern1 = [occ1, occ2]

            occ1 = [(10.5, 67.0), (11.0, 67.0), (11.5, 67.0), (12.0, 64.0),
                    (12.5, 69.0), (13.0, 69.0), (13.5, 69.0), (14.0, 67.0),
                    (14.5, 76.0), (15.0, 76.0), (15.5, 76.0), (16.0, 72.0)]
            occ2 = [(18.5, 67.0), (19.0, 67.0), (19.5, 67.0), (20.0, 62.0),
                    (20.5, 69.0), (21.0, 69.0), (21.5, 69.0), (22.0, 67.0),
                    (22.5, 77.0), (23.0, 77.0), (23.5, 77.0), (24.0, 74.0)]
            pattern2 = [occ1, occ2]

            pattern_list = [pattern1, pattern2]
    """
    # List with all the patterns
    pattern_list = []
    # Current pattern, which will contain all occs
    pattern = []
    # Current occurrence, containing (onset, midi)
    occurrence = []
    with _open(filename, mode="r") as input_file:
        for line in input_file.readlines():
            if "pattern" in line:
                if occurrence != []:
                    pattern.append(occurrence)
                if pattern != []:
                    pattern_list.append(pattern)
                occurrence = []
                pattern = []
                continue
            if "occurrence" in line:
                if occurrence != []:
                    pattern.append(occurrence)
                occurrence = []
                continue
            string_values = line.split(",")
            onset_midi = (float(string_values[0]), float(string_values[1]))
            occurrence.append(onset_midi)

        # Add last occurrence and pattern to pattern_list
        if occurrence != []:
            pattern.append(occurrence)
        if pattern != []:
            pattern_list.append(pattern)

    return pattern_list


@util.deprecated(version="0.8.1", version_removed="0.9.0")
def load_wav(path, mono=True):
    """Load a .wav file as a numpy array using ``scipy.io.wavfile``.

    .. warning:: This function is deprecatred in mir_eval 0.8.1
        and will be removed in 0.9.0.
        We recommend using a dedicated audio IO library such as
        `soundfile` instead.

    Parameters
    ----------
    path : str or `os.Pathlike`
        Path to a .wav file
    mono : bool
        If the provided .wav has more than one channel, it will be
        converted to mono if ``mono=True``. (Default value = True)

    Returns
    -------
    audio_data : np.ndarray
        Array of audio samples, normalized to the range [-1., 1.]
    fs : int
        Sampling rate of the audio data
    """
    fs, audio_data = scipy.io.wavfile.read(path)
    # Make float in range [-1, 1]
    if audio_data.dtype == "int8":
        audio_data = audio_data / float(2**8)
    elif audio_data.dtype == "int16":
        audio_data = audio_data / float(2**16)
    elif audio_data.dtype == "int32":
        audio_data = audio_data / float(2**24)
    else:
        raise ValueError("Got unexpected .wav data type " "{}".format(audio_data.dtype))
    # Optionally convert to mono
    if mono and audio_data.ndim != 1:
        audio_data = audio_data.mean(axis=1)
    return audio_data, fs


def load_valued_intervals(filename, delimiter=r"\s+", comment="#"):
    r"""Import valued intervals from an annotation file. The file should
    consist of three columns: Two consisting of numeric values corresponding to
    start and end time of each interval and a third, also of numeric values,
    corresponding to the value of each interval. This is primarily useful for
    processing events which span a duration and have a numeric value, such as
    piano-roll notes which have an onset, offset, and a pitch value.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    intervals : np.ndarray, shape=(n_events, 2)
        Array of event start and end times
    values : np.ndarray, shape=(n_events,)
        Array of values

    """
    # Use our universal function to load in the events
    starts, ends, values = load_delimited(
        filename, [float, float, float], delimiter=delimiter, comment=comment
    )
    # Stack into an interval matrix
    intervals = np.array([starts, ends]).T
    # Validate them, but throw a warning in place of an error
    try:
        util.validate_intervals(intervals)
    except ValueError as error:
        warnings.warn(error.args[0])

    # return values as np.ndarray
    values = np.array(values)

    return intervals, values


def load_key(filename, delimiter=r"\s+", comment="#"):
    r"""Load key labels from an annotation file. The file should
    consist of two string columns: One denoting the key scale degree
    (semitone), and the other denoting the mode (major or minor).  The file
    should contain only one row.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    key : str
        Key label, in the form ``'(key) (mode)'``

    """
    # Use our universal function to load the key and mode strings
    scale, mode = load_delimited(
        filename, [str, str], delimiter=delimiter, comment=comment
    )
    if len(scale) != 1:
        raise ValueError("Key file should contain only one line.")
    scale, mode = scale[0], mode[0]
    # Join with a space
    key_string = f"{scale} {mode}"
    # Validate them, but throw a warning in place of an error
    try:
        key.validate_key(key_string)
    except ValueError as error:
        warnings.warn(error.args[0])

    return key_string


def load_tempo(filename, delimiter=r"\s+", comment="#"):
    r"""Load tempo estimates from an annotation file in MIREX format.
    The file should consist of three numeric columns: the first two
    correspond to tempo estimates (in beats-per-minute), and the third
    denotes the relative confidence of the first value compared to the
    second (in the range [0, 1]). The file should contain only one row.

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    tempi : np.ndarray, non-negative
        The two tempo estimates
    weight : float [0, 1]
        The relative importance of ``tempi[0]`` compared to ``tempi[1]``
    """
    # Use our universal function to load the key and mode strings
    t1, t2, weight = load_delimited(
        filename, [float, float, float], delimiter=delimiter, comment=comment
    )

    weight = weight[0]
    tempi = np.concatenate([t1, t2])

    if len(t1) != 1:
        raise ValueError("Tempo file should contain only one line.")

    # Validate them, but throw a warning in place of an error
    try:
        tempo.validate_tempi(tempi)
    except ValueError as error:
        warnings.warn(error.args[0])

    if not 0 <= weight <= 1:
        raise ValueError(f"Invalid weight: {weight}")

    return tempi, weight


def load_ragged_time_series(
    filename, dtype=float, delimiter=r"\s+", header=False, comment="#"
):
    r"""Load data from a delimited time series annotation file with
    a variable number of columns.

    This function assumes that column 0 contains time stamps and
    columns 1 through n contain values.
    n may be variable from time stamp to time stamp.

    Examples
    --------
    >>> # Load a ragged list of tab-delimited multi-f0 midi notes
    >>> times, vals = load_ragged_time_series('multif0.txt', dtype=int,
                                              delimiter='\t')
    >>> # Load a raggled list of space delimited multi-f0 values with a header
    >>> times, vals = load_ragged_time_series('labeled_events.csv',
                                              header=True)

    Parameters
    ----------
    filename : str or `os.Pathlike`
        Path to the annotation file

    dtype : function
        Data type to apply to values columns.

    delimiter : str
        Separator regular expression.
        By default, lines will be split by any amount of whitespace.

    header : bool
        Indicates whether a header row is present or not.
        By default, assumes no header is present.

    comment : str or None
        Comment regular expression.
        Any lines beginning with this string or pattern will be ignored.

        Setting to `None` disables comments.

    Returns
    -------
    times : np.ndarray
        array of timestamps (float)
    values : list of np.ndarray
        list of arrays of corresponding values

    """
    # Initialize empty lists
    times = []
    values = []

    # Create re object for splitting lines
    splitter = re.compile(delimiter)

    # And one for comments
    if comment is None:
        commenter = None
    else:
        commenter = re.compile(f"^{comment}")

    if header:
        start_row = 1
    else:
        start_row = 0
    with _open(filename, mode="r") as input_file:
        for row, line in enumerate(input_file, start_row):
            # If this is a comment line, skip it
            if comment is not None and commenter.match(line):
                continue

            # Split each line using the supplied delimiter
            data = splitter.split(line.strip())
            try:
                converted_time = float(data[0])
            except (TypeError, ValueError) as exe:
                raise ValueError(
                    "Couldn't convert value {} using {} "
                    "found at {}:{:d}:\n\t{}".format(
                        data[0], float.__name__, filename, row, line
                    )
                ) from exe
            times.append(converted_time)

            # cast values to a numpy array. time stamps with no values are cast
            # to an empty array.
            try:
                converted_value = np.array(data[1:], dtype=dtype)
            except (TypeError, ValueError) as exe:
                raise ValueError(
                    "Couldn't convert value {} using {} "
                    "found at {}:{:d}:\n\t{}".format(
                        data[1:], dtype.__name__, filename, row, line
                    )
                ) from exe
            values.append(converted_value)

    return np.array(times), values
