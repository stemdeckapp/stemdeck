"""From Basic Pitch's activations to the notes StemDeck writes.

Two steps the vendored engine (app/_vendor/transcribe, kept byte for byte as
published) does not do the way StemDeck needs, run by the worker
(midi_worker.output_notes):

- onset_notes: Basic Pitch's own polyphonic decoding without its "melodia
  trick", with one defect fixed. Its onset peaks come from
  scipy.signal.argrelmax, which needs a frame strictly above both
  neighbours. get_infered_onsets rescales the frame-difference onsets so
  that their maximum equals the onset maximum, and where the two land on
  adjacent frames of one pitch they make a two-frame plateau at the file's
  strongest onset, which argrelmax skips. On a lone A440 the only real note
  was lost that way; the melodia trick used to find it again from the frame
  activation alone, along with many false notes (config.MIDI_DETAIL_PRESETS).
  Here the first frame of a plateau is the peak.
- one_voice: one note at a time, for the bass.

Note events are (start s, end s, MIDI pitch, amplitude, None): the pitch
bends Basic Pitch can add are never written (midi_file.note_ticks).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Basic Pitch's note_creation: the lowest pitch (A0) and the last bin, and how
# many frames under the frame threshold end a note.
MIDI_OFFSET = 21
MAX_FREQ_IDX = 87
ENERGY_TOL = 11


def onset_peaks(onsets):
    """(frame, bin) of every onset peak, latest first: a frame above the one
    before it and not below the one after it, so a plateau counts once, at
    its first frame. The first and last frames are never peaks, as with
    argrelmax."""
    import numpy as np

    mid = onsets[1:-1]
    peak = (mid > onsets[:-2]) & (mid >= onsets[2:])
    frames, bins = np.nonzero(peak)
    frames = frames + 1
    order = np.lexsort((bins, frames))[::-1]
    return frames[order], bins[order]


def onset_notes(frames, onsets, onset_thresh: float, frame_thresh: float, min_note_len: int):
    """Basic Pitch's output_to_notes_polyphonic with melodia_trick off, on
    activations already limited to the stem's range and with inferred onsets
    added (note_creation.constrain_frequency, get_infered_onsets): each onset
    peak at or over ``onset_thresh``, latest first, is a note that lasts while
    its frame activation stays over ``frame_thresh`` (ENERGY_TOL frames of
    grace), longer than ``min_note_len`` frames. Returns (start frame, end
    frame, MIDI pitch, amplitude)."""
    import numpy as np

    n_frames = frames.shape[0]
    peak_frames, peak_bins = onset_peaks(onsets)
    strong = onsets[peak_frames, peak_bins] >= onset_thresh
    remaining = np.array(frames, dtype=float, copy=True)
    notes = []
    for start, pitch_bin in zip(peak_frames[strong], peak_bins[strong], strict=True):
        if start >= n_frames - 1:
            continue
        i = start + 1
        k = 0
        while i < n_frames - 1 and k < ENERGY_TOL:
            k = k + 1 if remaining[i, pitch_bin] < frame_thresh else 0
            i += 1
        i -= k
        if i - start <= min_note_len:
            continue
        remaining[start:i, pitch_bin] = 0
        if pitch_bin < MAX_FREQ_IDX:
            remaining[start:i, pitch_bin + 1] = 0
        if pitch_bin > 0:
            remaining[start:i, pitch_bin - 1] = 0
        amplitude = float(np.mean(frames[start:i, pitch_bin]))
        notes.append((int(start), int(i), int(pitch_bin) + MIDI_OFFSET, amplitude))
    return notes


def _overlap(a: Sequence[Any], b: Sequence[Any]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def one_voice(notes: list, tolerance: float = 0.05) -> list:
    """One note at a time, for a line played one note at a time (a bass).

    Basic Pitch decodes every pitch on its own, so a bass note's own octave
    partial comes out as a second note sounding with it. Measured on the
    BabySlakh tuning tracks (scripts/midi_bench.py), 2736 of the 3952 extra
    bass notes were such an octave, and the decoded bass had two or more
    notes at once 35% of the time against 0% in the reference.

    Notes are taken strongest first (amplitude times length); a note that
    overlaps one already taken by more than ``tolerance`` seconds, or by more
    than half its own length, is dropped. What is left is in time order, and
    a note still sounding when the next one starts ends there."""
    ranked = sorted(
        range(len(notes)),
        key=lambda i: -float(notes[i][3]) * (float(notes[i][1]) - float(notes[i][0])),
    )
    taken: list[int] = []
    for i in ranked:
        n = notes[i]
        allowed = min(tolerance, 0.5 * (float(n[1]) - float(n[0])))
        if all(_overlap(n, notes[j]) <= allowed for j in taken):
            taken.append(i)
    kept = sorted((notes[i] for i in taken), key=lambda n: (float(n[0]), int(n[2])))
    out = []
    for k, n in enumerate(kept):
        if k + 1 < len(kept) and float(n[1]) > float(kept[k + 1][0]):
            n = (n[0], kept[k + 1][0], *tuple(n[2:]))
        out.append(n)
    return out
