"""app/pipeline/midi_notes.py: what is done to the engine's notes."""

from __future__ import annotations

from app.pipeline import midi_notes as mn


def _n(start, end, pitch, amp):
    return (start, end, pitch, amp, None)


def test_a_bass_note_and_its_own_octave_are_one_note():
    # E1 and the octave partial Basic Pitch hears above it, weaker.
    notes = [_n(0.0, 1.0, 28, 0.8), _n(0.02, 0.9, 40, 0.4)]
    assert mn.one_voice(notes) == [_n(0.0, 1.0, 28, 0.8)]


def test_one_voice_keeps_the_stronger_of_two_overlapping_notes():
    # A loud long note and a faint short one inside it: the faint one goes,
    # whichever is lower.
    notes = [_n(0.0, 0.2, 30, 0.2), _n(0.0, 1.0, 42, 0.9)]
    assert mn.one_voice(notes) == [_n(0.0, 1.0, 42, 0.9)]


def test_one_voice_keeps_a_line_and_ends_each_note_at_the_next():
    # Legato: each note rings 30 ms into the next, under the tolerance.
    notes = [_n(0.0, 0.53, 40, 0.7), _n(0.5, 1.03, 43, 0.7), _n(1.0, 1.5, 45, 0.7)]
    out = mn.one_voice(notes)
    assert [n[2] for n in out] == [40, 43, 45]
    assert [n[1] for n in out] == [0.5, 1.0, 1.5]


def test_one_voice_of_nothing_is_nothing():
    assert mn.one_voice([]) == []


# -- onset_notes, against Basic Pitch's own decoder


def _engine():
    import sys

    from app.pipeline.midi_file import VENDOR_DIR

    sys.path.insert(0, str(VENDOR_DIR))
    try:
        from basic_pitch import note_creation
    finally:
        sys.path.remove(str(VENDOR_DIR))
    return note_creation


def test_onset_notes_is_basic_pitchs_decoding_without_melodia():
    import numpy as np

    nc = _engine()
    rng = np.random.default_rng(7)
    # Smooth random activations: no two neighbouring frames are equal, so
    # there is no plateau for the two decoders to differ on.
    frames = np.clip(rng.random((400, 88)).cumsum(axis=0) % 1.0, 0, 1)
    onsets = rng.random((400, 88)) ** 3
    mine = mn.onset_notes(frames.copy(), onsets.copy(), 0.6, 0.3, 5)
    theirs = nc.output_to_notes_polyphonic(
        frames.copy(), onsets.copy(), onset_thresh=0.6, frame_thresh=0.3, min_note_len=5,
        infer_onsets=False, max_freq=None, min_freq=None, melodia_trick=False,
    )  # fmt: skip
    assert len(mine) > 50
    assert [(a, b, p) for a, b, p, _ in mine] == [(int(a), int(b), int(p)) for a, b, p, _ in theirs]
    assert np.allclose([n[3] for n in mine], [n[3] for n in theirs])


def test_the_strongest_onset_on_a_two_frame_plateau_is_a_note():
    import numpy as np

    nc = _engine()
    frames = np.zeros((60, 88))
    onsets = np.zeros((60, 88))
    frames[10:40, 48] = 0.8  # A4 held for 30 frames
    onsets[9:13, 48] = [0.3, 0.9, 0.9, 0.2]  # its onset peak, two frames wide
    mine = mn.onset_notes(frames, onsets, 0.6, 0.3, 5)
    assert [(a, p) for a, _b, p, _ in mine] == [(10, 69)]
    # Basic Pitch's own peak picking finds no peak there, so no note.
    theirs = nc.output_to_notes_polyphonic(
        frames, onsets, onset_thresh=0.6, frame_thresh=0.3, min_note_len=5,
        infer_onsets=False, max_freq=None, min_freq=None, melodia_trick=False,
    )  # fmt: skip
    assert theirs == []


def _pedal_reference(notes, pedals):
    """The benchmark's own rule (scripts/midi_bench.py references and the
    piano_hr spike), as plainly as it can be written: the first pedal that
    holds the release, then the next start of the same key."""
    out = []
    notes = sorted(notes, key=lambda n: n[0])
    for i, n in enumerate(notes):
        end = n[1]
        for a, b in pedals:
            if a <= end < b:
                end = b
                break
        for m in notes[i + 1 :]:
            if m[2] == n[2] and m[0] > n[0]:
                end = min(end, m[0])
                break
        out.append((n[0], max(end, n[1]), n[2], n[3]))
    return out


def test_the_pedal_holds_a_released_note_until_it_lifts_or_the_key_returns():
    pedals = [(0.8, 1.5), (3.0, 4.0)]
    notes = [
        (0.5, 0.9, 60, 100),  # released under the first pedal: held to 1.5
        (1.0, 1.2, 64, 64),  # the same
        (1.3, 1.4, 60, 90),  # 60 again: the first 60 ends here, at 1.3
        (2.0, 2.5, 67, 70),  # no pedal: as played
        (3.5, 5.0, 69, 80),  # released after the pedal lifts: as played
    ]
    held = mn.pedal_extend(notes, pedals)
    assert held == [
        (0.5, 1.3, 60, 100),
        (1.0, 1.5, 64, 64),
        (1.3, 1.5, 60, 90),
        (2.0, 2.5, 67, 70),
        (3.5, 5.0, 69, 80),
    ]
    assert held == _pedal_reference(notes, pedals)
    assert mn.pedal_extend(notes, []) == _pedal_reference(notes, [])


def test_pedal_extend_is_the_benchmark_rule_on_many_notes():
    import random

    rng = random.Random(7)
    notes = []
    for _ in range(600):
        start = round(rng.uniform(0, 60), 3)
        notes.append((start, start + round(rng.uniform(0.05, 2), 3), rng.randint(40, 80), 90))
    pedals, at = [], 0.0
    while at < 60:
        down = at + rng.uniform(0.2, 2)
        up = down + rng.uniform(0.2, 3)
        pedals.append((round(down, 3), round(up, 3)))
        at = up
    assert mn.pedal_extend(notes, pedals) == _pedal_reference(notes, pedals)
