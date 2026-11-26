"""Display functions"""

from collections import defaultdict
from weakref import WeakKeyDictionary

import numpy as np
from scipy.signal import spectrogram

import matplotlib as mpl
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter, MultipleLocator
from matplotlib.ticker import Formatter
from matplotlib.colors import LinearSegmentedColormap, LogNorm, ColorConverter
from matplotlib.transforms import Bbox, TransformedBbox

from .melody import freq_to_voicing
from .util import midi_to_hz, hz_to_midi


# This dictionary is used to track mir_eval-specific attributes
# attached to matplotlib axes
__AXMAP = WeakKeyDictionary()


def __get_axes(ax=None, fig=None):
    """Get or construct the target axes object for a new plot.

    Parameters
    ----------
    ax : matplotlib.pyplot.axes, optional
        If provided, return this axes object directly.

    fig : matplotlib.figure.Figure, optional
        The figure to query for axes.

        By default, uses the current figure `plt.gcf()`.

    Returns
    -------
    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the segmentation.
        If none is provided, a new set of axes is created.
    new_axes : bool
        If `True`, the axis object was newly constructed.
        If `False`, the axis object already existed.
    """
    new_axes = False

    if ax is None:
        if fig is None:
            import matplotlib.pyplot as plt

            fig = plt.gcf()

        if not fig.get_axes():
            new_axes = True
        ax = fig.gca()

    # Create a storage bucket for this axes in case we need it
    if ax not in __AXMAP:
        __AXMAP[ax] = dict()

    return ax, new_axes


def segments(
    intervals,
    labels,
    base=None,
    height=None,
    text=False,
    text_kw=None,
    ax=None,
    prop_cycle=None,
    **kwargs,
):
    """Plot a segmentation as a set of disjoint rectangles.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
    labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.
    base : number
        The vertical position of the base of the rectangles.
        By default, this will be the bottom of the plot.
    height : number
        The height of the rectangles.
        By default, this will be the top of the plot (minus ``base``).
        .. note:: If either `base` or `height` are provided, both must be provided.
    text : bool
        If true, each segment's label is displayed in its
        upper-left corner
    text_kw : dict
        If ``text == True``, the properties of the text
        object can be specified here.
        See ``matplotlib.pyplot.Text`` for valid parameters
    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the segmentation.
        If none is provided, a new set of axes is created.
    prop_cycle : cycle.Cycler
        An optional property cycle object to specify style properties.
        If not provided, the default property cycler will be retrieved from matplotlib.
    **kwargs
        Additional keyword arguments to pass to
        ``matplotlib.patches.Rectangle``.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    if text_kw is None:
        text_kw = dict()
    text_kw.setdefault("va", "top")
    text_kw.setdefault("clip_on", True)
    text_kw.setdefault("bbox", dict(boxstyle="round", facecolor="white"))

    # Make sure we have a numpy array
    intervals = np.atleast_2d(intervals)

    seg_def_style = dict(linewidth=1)

    ax, new_axes = __get_axes(ax=ax)

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    if new_axes:
        ax.set_yticks([])

    if base is None and height is None:
        # If neither are provided, we'll use axes coordinates to span the figure
        base, height = 0, 1
        transform = ax.get_xaxis_transform()

    elif base is not None and height is not None:
        # If both are provided, we'll use data coordinates
        transform = None
    else:
        raise ValueError("When specifying base or height, both must be provided.")

    seg_map = dict()

    for lab in labels:
        if lab in seg_map:
            continue

        try:
            properties = next(prop_iter)
        except StopIteration:
            prop_iter = iter(prop_cycle)
            __AXMAP[ax]["prop_iter"] = prop_iter
            properties = next(prop_iter)

        style = {
            k: v
            for k, v in properties.items()
            if k in ["color", "facecolor", "edgecolor", "linewidth"]
        }
        # Swap color -> facecolor here so we preserve edgecolor on rects
        style.setdefault("facecolor", style["color"])
        style.pop("color", None)
        seg_map[lab] = seg_def_style.copy()
        seg_map[lab].update(style)
        seg_map[lab].update(kwargs)
        seg_map[lab]["label"] = lab

    for ival, lab in zip(intervals, labels):
        rect = ax.axvspan(ival[0], ival[1], ymin=base, ymax=height, **seg_map[lab])
        seg_map[lab].pop("label", None)

        if text:
            ann = ax.annotate(
                lab,
                xy=(ival[0], height),
                xycoords=transform,
                xytext=(8, -10),
                textcoords="offset points",
                **text_kw,
            )
            ann.set_clip_path(rect)

    return ax


def labeled_intervals(
    intervals,
    labels,
    label_set=None,
    base=None,
    height=None,
    extend_labels=True,
    ax=None,
    tick=True,
    prop_cycle=None,
    **kwargs,
):
    """Plot labeled intervals with each label on its own row.

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.

    labels : list, shape=(n,)
        reference segment labels, in the format returned by
        :func:`mir_eval.io.load_labeled_intervals`.

    label_set : list
        An (ordered) list of labels to determine the plotting order.
        If not provided, the labels will be inferred from
        ``ax.get_yticklabels()``.
        If no ``yticklabels`` exist, then the sorted set of unique values
        in ``labels`` is taken as the label set.

    base : np.ndarray, shape=(n,), optional
        Vertical positions of each label.
        By default, labels are positioned at integers
        ``np.arange(len(labels))``.

    height : scalar or np.ndarray, shape=(n,), optional
        Height for each label.
        If scalar, the same value is applied to all labels.
        By default, each label has ``height=1``.

    extend_labels : bool
        If ``False``, only values of ``labels`` that also exist in
        ``label_set`` will be shown.

        If ``True``, all labels are shown, with those in `labels` but
        not in `label_set` appended to the top of the plot.
        A horizontal line is drawn to indicate the separation between
        values in or out of ``label_set``.

    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the intervals.
        If none is provided, a new set of axes is created.

    tick : bool
        If ``True``, sets tick positions and labels on the y-axis.

    prop_cycle : cycle.Cycler
        An optional property cycle object to specify style properties.
        If not provided, the default property cycler will be retrieved from matplotlib.

    **kwargs
        Additional keyword arguments to pass to
        `matplotlib.collection.PolyCollection`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    # Get the axes handle
    ax, new_axes = __get_axes(ax=ax)

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    # Make sure we have a numpy array
    intervals = np.atleast_2d(intervals)

    if label_set is None:
        # If we have non-empty pre-existing tick labels, use them
        # If none of the label strings have content, treat it as empty
        label_set = __AXMAP[ax].get("labels", [])
    else:
        label_set = list(label_set)

    # Put additional labels at the end, in order
    extended = False
    if extend_labels:
        ticks = label_set + sorted(set(labels) - set(label_set))
        if ticks != label_set and len(label_set) > 0:
            extended = True
    elif label_set:
        ticks = label_set
    else:
        ticks = sorted(set(labels))

    # Push the ticks up into the axmap
    __AXMAP[ax]["labels"] = ticks

    style = dict(linewidth=1)

    try:
        properties = next(prop_iter)
    except StopIteration:
        prop_iter = iter(prop_cycle)
        __AXMAP[ax]["prop_iter"] = prop_iter
        properties = next(prop_iter)

    style = {
        k: v
        for k, v in properties.items()
        if k in ["color", "facecolor", "edgecolor", "linewidth"]
    }
    # Swap color -> facecolor here so we preserve edgecolor on rects
    style.setdefault("facecolor", style["color"])
    style.pop("color", None)
    style.update(kwargs)

    if base is None:
        base = np.arange(len(ticks))

    if height is None:
        height = 1

    if np.isscalar(height):
        height = height * np.ones_like(base)

    seg_y = dict()
    for ybase, yheight, lab in zip(base, height, ticks):
        seg_y[lab] = (ybase, yheight)

    xvals = defaultdict(list)
    for ival, lab in zip(intervals, labels):
        if lab not in seg_y:
            continue
        xvals[lab].append((ival[0], ival[1] - ival[0]))

    for lab in seg_y:
        ax.broken_barh(xvals[lab], seg_y[lab], **style)
        # Pop the label after the first time we see it, so we only get
        # one legend entry
        style.pop("label", None)

    # Draw a line separating the new labels from pre-existing labels
    if extended:
        ax.axhline(len(label_set), color="k", alpha=0.5)

    if tick:
        ax.grid(True, axis="y")
        ax.set_yticks([])
        ax.set_yticks(base)
        ax.set_yticklabels(ticks, va="bottom")
        ax.yaxis.set_major_formatter(IntervalFormatter(base, ticks))

    return ax


class IntervalFormatter(Formatter):
    """Ticker formatter for labeled interval plots.

    Parameters
    ----------
    base : array-like of int
        The base positions of each label

    ticks : array-like of string
        The labels for the ticks
    """

    def __init__(self, base, ticks):
        self._map = {int(k): v for k, v in zip(base, ticks)}

    def __call__(self, x, pos=None):
        """Map the input position to its corresponding interval label"""
        return self._map.get(int(x), "")


def hierarchy(intervals_hier, labels_hier, levels=None, ax=None, **kwargs):
    """Plot a hierarchical segmentation

    Parameters
    ----------
    intervals_hier : list of np.ndarray
        A list of segmentation intervals.  Each element should be
        an n-by-2 array of segment intervals, in the format returned by
        :func:`mir_eval.io.load_intervals` or
        :func:`mir_eval.io.load_labeled_intervals`.
        Segmentations should be ordered by increasing specificity.
    labels_hier : list of list-like
        A list of segmentation labels.  Each element should
        be a list of labels for the corresponding element in
        `intervals_hier`.
    levels : list of string
        Each element ``levels[i]`` is a label for the ```i`` th segmentation.
        This is used in the legend to denote the levels in a segment hierarchy.
    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the intervals.
        If none is provided, a new set of axes is created.
    **kwargs
        Additional keyword arguments to `labeled_intervals`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    # This will break if a segment label exists in multiple levels
    if levels is None:
        levels = list(range(len(intervals_hier)))

    # Get the axes handle
    ax, _ = __get_axes(ax=ax)

    # Count the pre-existing patches
    n_patches = len(ax.patches)

    for ints, labs, key in zip(intervals_hier[::-1], labels_hier[::-1], levels[::-1]):
        labeled_intervals(ints, labs, label=key, ax=ax, **kwargs)

    return ax


def events(
    times,
    labels=None,
    base=None,
    height=None,
    ax=None,
    text_kw=None,
    prop_cycle=None,
    **kwargs,
):
    """Plot event times as a set of vertical lines

    Parameters
    ----------
    times : np.ndarray, shape=(n,)
        event times, in the format returned by
        :func:`mir_eval.io.load_events` or
        :func:`mir_eval.io.load_labeled_events`.
    labels : list, shape=(n,), optional
        event labels, in the format returned by
        :func:`mir_eval.io.load_labeled_events`.
    base : number
        The vertical position of the base of the line.
        By default, this will be the bottom of the plot.
    height : number
        The height of the lines.
        By default, this will be the top of the plot (minus `base`).
        .. note:: If either `base` or `height` are provided, both must be provided.
    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the segmentation.
        If none is provided, a new set of axes is created.
    text_kw : dict
        If `labels` is provided, the properties of the text
        objects can be specified here.
        See `matplotlib.pyplot.Text` for valid parameters
    prop_cycle : cycle.Cycler
        An optional property cycle object to specify style properties.
        If not provided, the default property cycler will be retrieved from matplotlib.
    **kwargs
        Additional keyword arguments to pass to
        `matplotlib.pyplot.vlines`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    if text_kw is None:
        text_kw = dict()
    text_kw.setdefault("va", "top")
    text_kw.setdefault("clip_on", True)
    text_kw.setdefault("bbox", dict(boxstyle="round", facecolor="white"))

    # make sure we have an array for times
    times = np.asarray(times)

    # Get the axes handle
    ax, new_axes = __get_axes(ax=ax)

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    if base is None and height is None:
        # If neither are provided, we'll use axes coordinates to span the figure
        base, height = 0, 1
        transform = ax.get_xaxis_transform()

    elif base is not None and height is not None:
        # If both are provided, we'll use data coordinates
        transform = None
    else:
        raise ValueError("When specifying base or height, both must be provided.")

    # Advance the property iterator if we can, restart it if we must
    try:
        properties = next(prop_iter)
    except StopIteration:
        prop_iter = iter(prop_cycle)
        __AXMAP[ax]["prop_iter"] = prop_iter
        properties = next(prop_iter)

    style = {
        k: v for k, v in properties.items() if k in ["color", "linestyle", "linewidth"]
    }
    style.update(kwargs)

    # If the user provided 'colors', don't override it with 'color'
    if "colors" in style:
        style.pop("color", None)

    lines = ax.vlines(times, base, base + height, transform=transform, **style)

    if labels:
        for path, lab in zip(lines.get_paths(), labels):
            ax.annotate(
                lab,
                xy=(path.vertices[0][0], height),
                xycoords=transform,
                xytext=(8, -10),
                textcoords="offset points",
                **text_kw,
            )

    if new_axes:
        ax.set_yticks([])

    return ax


def pitch(
    times, frequencies, midi=False, unvoiced=False, ax=None, prop_cycle=None, **kwargs
):
    """Visualize pitch contours

    Parameters
    ----------
    times : np.ndarray, shape=(n,)
        Sample times of frequencies

    frequencies : np.ndarray, shape=(n,)
        frequencies (in Hz) of the pitch contours.
        Voicing is indicated by sign (positive for voiced,
        non-positive for non-voiced).

    midi : bool
        If `True`, plot on a MIDI-numbered vertical axis.
        Otherwise, plot on a linear frequency axis.

    unvoiced : bool
        If `True`, unvoiced pitch contours are plotted and indicated
        by transparency.

        Otherwise, unvoiced pitch contours are omitted from the display.

    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the pitch contours.
        If none is provided, a new set of axes is created.

    prop_cycle : cycle.Cycler
        An optional property cycle object to specify style properties.
        If not provided, the default property cycler will be retrieved from matplotlib.

    **kwargs
        Additional keyword arguments to `matplotlib.pyplot.plot`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    ax, _ = __get_axes(ax=ax)

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    times = np.asarray(times)

    # First, segment into contiguously voiced contours
    frequencies, voicings = freq_to_voicing(np.asarray(frequencies, dtype=np.float64))
    voicings = voicings.astype(bool)

    # Here are all the change-points
    v_changes = 1 + np.flatnonzero(voicings[1:] != voicings[:-1])
    v_changes = np.unique(np.concatenate([[0], v_changes, [len(voicings)]]))

    # Set up arrays of slices for voiced and unvoiced regions
    v_slices, u_slices = [], []
    for start, end in zip(v_changes, v_changes[1:]):
        idx = slice(start, end)
        # A region is voiced if its starting sample is voiced
        # It's unvoiced if none of the samples in the region are voiced.
        if voicings[start]:
            v_slices.append(idx)
        elif frequencies[idx].all():
            u_slices.append(idx)

    # Now we just need to plot the contour
    try:
        style = next(prop_iter)
    except StopIteration:
        prop_iter = iter(prop_cycle)
        __AXMAP[ax]["prop_iter"] = prop_iter
        style = next(prop_iter)
    style.update(kwargs)

    if midi:
        idx = frequencies > 0
        frequencies[idx] = hz_to_midi(frequencies[idx])

        # Tick at integer midi notes
        ax.yaxis.set_minor_locator(MultipleLocator(1))

    for idx in v_slices:
        ax.plot(times[idx], frequencies[idx], **style)
        style.pop("label", None)

    # Plot the unvoiced portions
    if unvoiced:
        style["alpha"] = style.get("alpha", 1.0) * 0.5
        for idx in u_slices:
            ax.plot(times[idx], frequencies[idx], **style)

    return ax


def multipitch(
    times, frequencies, midi=False, unvoiced=False, ax=None, prop_cycle=None, **kwargs
):
    """Visualize multiple f0 measurements

    Parameters
    ----------
    times : np.ndarray, shape=(n,)
        Sample times of frequencies

    frequencies : list of np.ndarray
        frequencies (in Hz) of the pitch measurements.
        Voicing is indicated by sign (positive for voiced,
        non-positive for non-voiced).

        `times` and `frequencies` should be in the format produced by
        :func:`mir_eval.io.load_ragged_time_series`

    midi : bool
        If `True`, plot on a MIDI-numbered vertical axis.
        Otherwise, plot on a linear frequency axis.

    unvoiced : bool
        If `True`, unvoiced pitches are plotted and indicated
        by transparency.

        Otherwise, unvoiced pitches are omitted from the display.

    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the pitch contours.
        If none is provided, a new set of axes is created.

    prop_cycle : cycle.Cycler
        An optional property cycle object to specify style properties.
        If not provided, the default property cycler will be retrieved from matplotlib.

    **kwargs
        Additional keyword arguments to `plt.scatter`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    # Get the axes handle
    ax, _ = __get_axes(ax=ax)

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    # Set up a style for the plot
    try:
        style_voiced = next(prop_iter)
    except StopIteration:
        prop_iter = iter(prop_cycle)
        __AXMAP[ax]["prop_iter"] = prop_iter
        style_voiced = next(prop_iter)

    style_voiced.update(kwargs)

    style_unvoiced = style_voiced.copy()
    style_unvoiced.pop("label", None)
    style_unvoiced["alpha"] = style_unvoiced.get("alpha", 1.0) * 0.5

    # We'll collect all times and frequencies first, then plot them
    voiced_times = []
    voiced_freqs = []

    unvoiced_times = []
    unvoiced_freqs = []

    for t, freqs in zip(times, frequencies):
        if not len(freqs):
            continue

        freqs, voicings = freq_to_voicing(np.asarray(freqs, dtype=np.float64))

        # Discard all 0-frequency measurements
        idx = freqs > 0
        freqs = freqs[idx]
        voicings = voicings[idx].astype(bool)

        if midi:
            freqs = hz_to_midi(freqs)

        n_voiced = sum(voicings)
        voiced_times.extend([t] * int(n_voiced))
        voiced_freqs.extend(freqs[voicings])
        unvoiced_times.extend([t] * (len(freqs) - n_voiced))
        unvoiced_freqs.extend(freqs[~voicings])

    # Plot the voiced frequencies
    ax.scatter(voiced_times, voiced_freqs, **style_voiced)

    # Plot the unvoiced frequencies
    if unvoiced:
        ax.scatter(unvoiced_times, unvoiced_freqs, **style_unvoiced)

    # Tick at integer midi notes
    if midi:
        ax.yaxis.set_minor_locator(MultipleLocator(1))
    return ax


def piano_roll(intervals, pitches=None, midi=None, ax=None, **kwargs):
    """Plot a quantized piano roll as intervals

    Parameters
    ----------
    intervals : np.ndarray, shape=(n, 2)
        timing intervals for notes

    pitches : np.ndarray, shape=(n,), optional
        pitches of notes (in Hz).

    midi : np.ndarray, shape=(n,), optional
        pitches of notes (in MIDI numbers).

        At least one of ``pitches`` or ``midi`` must be provided.

    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the intervals.
        If none is provided, a new set of axes is created.

    **kwargs
        Additional keyword arguments to :func:`labeled_intervals`.

    Returns
    -------
    ax : matplotlib.pyplot.axes._subplots.AxesSubplot
        A handle to the (possibly constructed) plot axes
    """
    if midi is None:
        if pitches is None:
            raise ValueError("At least one of `midi` or `pitches` " "must be provided.")

        midi = hz_to_midi(pitches)

    scale = np.arange(128)
    ax = labeled_intervals(
        intervals,
        np.round(midi).astype(int),
        label_set=scale,
        tick=False,
        ax=ax,
        **kwargs,
    )

    # Minor tick at each semitone
    ax.yaxis.set_minor_locator(MultipleLocator(1))

    return ax


def separation(
    sources,
    fs=22050,
    labels=None,
    alpha=0.75,
    ax=None,
    rasterized=True,
    edgecolors="None",
    shading="gouraud",
    prop_cycle=None,
    **kwargs,
):
    """Source-separation visualization

    Parameters
    ----------
    sources : np.ndarray, shape=(nsrc, nsampl)
        A list of waveform buffers corresponding to each source
    fs : number > 0
        The sampling rate
    labels : list of strings
        An optional list of descriptors corresponding to each source
    alpha : float in [0, 1]
        Maximum alpha (opacity) of spectrogram values.
    ax : matplotlib.pyplot.axes
        An axis handle on which to draw the spectrograms.
        If none is provided, a new set of axes is created.
    rasterized : bool
        If `True`, the spectrogram is rasterized.
    edgecolors : str or None
        The color of the edges of the spectrogram patches.
        Set to "None" (default) to disable edge coloring.
    shading : str
        The shading method to use for the spectrogram.
        See `matplotlib.pyplot.pcolormesh` for valid options.
    prop_cycle : cycle.Cycler
        An optional property cycle object to specify colors for each signal.
        If not provided, the default property cycler will be retrieved from matplotlib.
    **kwargs
        Additional keyword arguments to ``scipy.signal.spectrogram``

    Returns
    -------
    ax
        The axis handle for this plot
    """
    # Get the axes handle
    ax, new_axes = __get_axes(ax=ax)

    # Make sure we have at least two dimensions
    sources = np.atleast_2d(sources)

    if labels is None:
        labels = [f"Source {_:d}" for _ in range(len(sources))]

    kwargs.setdefault("scaling", "spectrum")

    # The cumulative spectrogram across sources
    # is used to establish the reference power
    # for each individual source
    cumspec = None
    specs = []
    for i, src in enumerate(sources):
        freqs, times, spec = spectrogram(src, fs=fs, **kwargs)

        specs.append(spec)
        if cumspec is None:
            cumspec = spec.copy()
        else:
            cumspec += spec

    ref_max = cumspec.max()
    ref_min = ref_max * 1e-6

    color_conv = ColorConverter()

    if prop_cycle is None:
        __AXMAP[ax].setdefault("prop_cycle", mpl.rcParams["axes.prop_cycle"])
        __AXMAP[ax].setdefault("prop_iter", iter(mpl.rcParams["axes.prop_cycle"]))
    elif "prop_iter" not in __AXMAP[ax]:
        __AXMAP[ax]["prop_cycle"] = prop_cycle
        __AXMAP[ax]["prop_iter"] = iter(prop_cycle)

    prop_cycle = __AXMAP[ax]["prop_cycle"]
    prop_iter = __AXMAP[ax]["prop_iter"]

    for i, spec in enumerate(specs):
        # For each source, grab a new color from the cycler
        # Then construct a colormap that interpolates from
        # [transparent white -> new color]
        # Advance the property iterator if we can, restart it if we must
        try:
            properties = next(prop_iter)
        except StopIteration:
            prop_iter = iter(prop_cycle)
            __AXMAP[ax]["prop_iter"] = prop_iter
            properties = next(prop_iter)

        color = color_conv.to_rgba(properties["color"], alpha=alpha)
        cmap = LinearSegmentedColormap.from_list(
            labels[i], [(1.0, 1.0, 1.0, 0.0), color]
        )

        ax.pcolormesh(
            times,
            freqs,
            spec,
            cmap=cmap,
            norm=LogNorm(vmin=ref_min, vmax=ref_max),
            rasterized=rasterized,
            edgecolors=edgecolors,
            shading=shading,
        )

        # Attach a 0x0 rect to the axis with the corresponding label
        # This way, it will show up in the legend
        ax.add_patch(
            Rectangle((times.min(), freqs.min()), 0, 0, color=color, label=labels[i])
        )

    return ax


def __ticker_midi_note(x, pos):
    """Format midi notes for ticker decoration.

    Inputs x are interpreted as midi numbers, and converted
    to [NOTE][OCTAVE]+[cents].
    """
    NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

    cents = float(np.mod(x, 1.0))
    if cents >= 0.5:
        cents = cents - 1.0
        x = x + 0.5

    idx = int(x % 12)

    octave = int(x / 12) - 1

    if cents == 0:
        return f"{NOTES[idx]:s}{octave:2d}"
    return f"{NOTES[idx]:s}{octave:2d}{int(cents * 100):+02d}"


def __ticker_midi_hz(x, pos):
    """Format midi pitches for ticker decoration.

    Inputs x are interpreted as midi numbers, and converted
    to Hz.
    """
    return f"{midi_to_hz(x):g}"


def ticker_notes(ax=None):
    """Set the y-axis of the given axes to MIDI notes

    Parameters
    ----------
    ax : matplotlib.pyplot.axes
        The axes handle to apply the ticker.
        By default, uses the current axes handle.

    """
    ax, _ = __get_axes(ax=ax)

    ax.yaxis.set_major_formatter(FMT_MIDI_NOTE)
    # Get the tick labels and reset the vertical alignment
    for tick in ax.yaxis.get_ticklabels():
        tick.set_verticalalignment("baseline")


def ticker_pitch(ax=None):
    """Set the y-axis of the given axes to MIDI frequencies

    Parameters
    ----------
    ax : matplotlib.pyplot.axes
        The axes handle to apply the ticker.
        By default, uses the current axes handle.
    """
    ax, _ = __get_axes(ax=ax)

    ax.yaxis.set_major_formatter(FMT_MIDI_HZ)


# Instantiate ticker objects; we don't need more than one of each
FMT_MIDI_NOTE = FuncFormatter(__ticker_midi_note)
FMT_MIDI_HZ = FuncFormatter(__ticker_midi_hz)
