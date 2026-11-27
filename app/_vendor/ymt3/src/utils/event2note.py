# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
"""event2note.py:

Event to NoteEvent:
• event2note_event

NoteEvent to Note:
• note_event2note
• merge_zipped_note_events_and_ties_to_notes

"""
import warnings
from collections import Counter
from typing import List, Tuple, Optional, Dict, Counter

from utils.note_event_dataclasses import Note, NoteEvent
from utils.note_event_dataclasses import Event
from utils.note2event import validate_notes, trim_overlapping_notes

MINIMUM_OFFSET_SEC = 0.01

DECODING_ERR_TYPES = [
    'decoding_time', 'Err/Missing prg in tie', 'Err/Missing tie', 'Err/Shift out of range', 'Err/Missing prg',
    'Err/Missing vel', 'Err/Multi-tie type 1', 'Err/Multi-tie type 2', 'Err/Unknown event', 'Err/onset not found',
    'Err/active ne incomplete', 'Err/merging segment tie', 'Err/long note > 10s'
]


def event2note_event(events: List[Event],
                     start_time: float = 0.0,
                     sort: bool = True,
                     tps: int = 100) -> Tuple[List[NoteEvent], List[NoteEvent], List[Tuple[int]], Counter[str]]:
    """Convert events to note events.

    Args:
        events: A list of events.
        start_time: The start time of the segment.
        sort: Whether to sort the note events.
        tps: Ticks per second.

    Returns:
        List[NoteEvent]: A list of note events.
        List[NoteEvent]: A list of tie note events.
        List[Tuple[int]]: A list of last activity of segment. [(program, pitch), ...]. This is useful
            for validating notes within a batch of segments extracted from a file.
        Counter[str]: A dictionary of error counters.
    """
    assert (start_time >= 0.)

    # Collect tie events
    tie_index = program_state = None
    tie_note_events = []
    last_activity = []  # For activity check and last activity of segment. [(program, pitch), ...]
    error_counter = {}  # Add a dictionary to count the errors by their types

    for i, e in enumerate(events):
        try:
            if e.type == 'tie':
                tie_index = i
                break
            if e.type == 'shift':
                break
            elif e.type == 'program':
                program_state = e.value
            elif e.type == 'pitch':
                if program_state is None:
                    raise ValueError('Err/Missing prg in tie')
                tie_note_events.append(
                    NoteEvent(is_drum=False, program=program_state, time=None, velocity=1, pitch=e.value))
                last_activity.append((program_state, e.value))  # (program, pitch)
        except ValueError as ve:
            error_type = str(ve)
            error_counter[error_type] = error_counter.get(error_type, 0.) + 1

    try:
        if tie_index is None:
            raise ValueError('Err/Missing tie')
        else:
            events = events[tie_index + 1:]
    except ValueError as ve:
        error_type = str(ve)
        error_counter[error_type] = error_counter.get(error_type, 0.) + 1
        return [], [], [], error_counter

    # Collect main events:
    note_events = []
    velocity_state = None
    start_tick = round(start_time * tps)
    tick_state = start_tick
    # keep the program_state of last tie event...

    for e in events:
        try:
            if e.type == 'shift':
                if e.value <= 0 or e.value > 1000:
                    raise ValueError('Err/Shift out of range')
                # tick_state += e.value
                tick_state = start_tick + e.value
            elif e.type == 'drum':
                note_events.append(
                    NoteEvent(is_drum=True, program=128, time=tick_state / tps, velocity=1, pitch=e.value))
            elif e.type == 'program':
                program_state = e.value
            elif e.type == 'velocity':
                velocity_state = e.value
            elif e.type == 'pitch':
                if program_state is None:
                    raise ValueError('Err/Missing prg')
                elif velocity_state is None:
                    raise ValueError('Err/Missing vel')
                # Check activity
                if velocity_state > 0:
                    last_activity.append((program_state, e.value))  # (program, pitch)
                elif velocity_state == 0 and (program_state, e.value) in last_activity:
                    last_activity.remove((program_state, e.value))
                else:
                    # print(f'tick_state: {tick_state}') # <-- This displays unresolved offset errors!!
                    raise ValueError('Err/Note off without note on')
                note_events.append(
                    NoteEvent(is_drum=False,
                              program=program_state,
                              time=tick_state / tps,
                              velocity=velocity_state,
                              pitch=e.value))
            elif e.type == 'EOS':
                break
            elif e.type == 'PAD':
                continue
            elif e.type == 'UNK':
                continue
            elif e.type == 'tie':
                if tick_state == start_tick:
                    raise ValueError('Err/Multi-tie type 1')
                else:
                    raise ValueError('Err/Multi-tie type 2')
            else:
                raise ValueError(f'Err/Unknown event')
        except ValueError as ve:
            error_type = str(ve)
            error_counter[error_type] = error_counter.get(error_type, 0.) + 1

    if sort:
        note_events.sort(key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))
        tie_note_events.sort(key=lambda n_ev: (n_ev.is_drum, n_ev.program, n_ev.pitch))

    return note_events, tie_note_events, last_activity, error_counter


def note_event2note(
    note_events: List[NoteEvent],
    tie_note_events: Optional[List[NoteEvent]] = None,
    sort: bool = True,
    fix_offset: bool = True,
    trim_overlap: bool = True,
) -> Tuple[List[Note], Counter[str]]:
    """Convert note events to notes.

    Returns:
        List[Note]: A list of merged note events.
        Counter[str]: A dictionary of error counters.
    """

    notes = []
    active_note_events = {}

    error_counter = {}  # Add a dictionary to count the errors by their types

    if tie_note_events is not None:
        for ne in tie_note_events:
            active_note_events[(ne.pitch, ne.program)] = ne

    if sort:
        note_events.sort(key=lambda ne: (ne.time, ne.is_drum, ne.pitch, ne.velocity, ne.program))

    for ne in note_events:
        try:
            if ne.time == None:
                continue
            elif ne.is_drum:
                if ne.velocity == 1:
                    notes.append(
                        Note(is_drum=True,
                             program=128,
                             onset=ne.time,
                             offset=ne.time + MINIMUM_OFFSET_SEC,
                             pitch=ne.pitch,
                             velocity=1))
                else:
                    continue
            elif ne.velocity == 1:
                active_ne = active_note_events.get((ne.pitch, ne.program))
                if active_ne is not None:
                    active_note_events.pop((ne.pitch, ne.program))
                    notes.append(
                        Note(False, active_ne.program, active_ne.time, ne.time, active_ne.pitch, active_ne.velocity))
                active_note_events[(ne.pitch, ne.program)] = ne

            elif ne.velocity == 0:
                active_ne = active_note_events.pop((ne.pitch, ne.program), None)
                if active_ne is not None:
                    notes.append(
                        Note(False, active_ne.program, active_ne.time, ne.time, active_ne.pitch, active_ne.velocity))
                else:
                    raise ValueError('Err/onset not found')
        except ValueError as ve:
            error_type = str(ve)
            error_counter[error_type] = error_counter.get(error_type, 0.) + 1

    for ne in active_note_events.values():
        try:
            if ne.velocity == 1:
                if ne.program == None or ne.pitch == None:
                    raise ValueError('Err/active ne incomplete')
                elif ne.time == None:
                    continue
                else:
                    notes.append(
                        Note(is_drum=False,
                             program=ne.program,
                             onset=ne.time,
                             offset=ne.time + MINIMUM_OFFSET_SEC,
                             pitch=ne.pitch,
                             velocity=1))
        except ValueError as ve:
            error_type = str(ve)
            error_counter[error_type] = error_counter.get(error_type, 0.) + 1

    if fix_offset:
        for n in list(notes):
            try:
                if n.offset - n.onset > 10:
                    n.offset = n.onset + MINIMUM_OFFSET_SEC
                    raise ValueError('Err/long note > 10s')
            except ValueError as ve:
                error_type = str(ve)
                error_counter[error_type] = error_counter.get(error_type, 0.) + 1

    if sort:
        notes.sort(key=lambda note: (note.onset, note.is_drum, note.program, note.velocity, note.pitch))

    if fix_offset:
        notes = validate_notes(notes, fix=True)

    if trim_overlap:
        notes = trim_overlapping_notes(notes, sort=True)

    return notes, error_counter


def merge_zipped_note_events_and_ties_to_notes(zipped_note_events_and_ties,
                                               force_note_off_missing_tie=True,
                                               fix_offset=True) -> Tuple[List[Note], Counter[str]]:
    """Merge zipped note events and ties.
    
    Args:
        zipped_note_events_and_ties: A list of tuples of (note events, tie note events, last_activity, start time).
        force_note_off_missing_tie: Whether to force note off for missing tie note events.
        fix_offset: Whether to fix the offset of notes.

    Returns:
        List[Note]: A list of merged note events.
        Counter[str]: A dictionary of error counters.
    """
    merged_note_events = []
    prev_last_activity = None
    seg_merge_err_cnt = Counter()
    for nes, tie_nes, last_activity, start_time in zipped_note_events_and_ties:
        if prev_last_activity is not None and force_note_off_missing_tie:
            # Check mismatch between prev_last_activity and current tie_note_events
            prog_pitch_tie = set([(ne.program, ne.pitch) for ne in tie_nes])
            for prog_pitch_pla in prev_last_activity:  # (program, pitch) of previous last active notes
                if prog_pitch_pla not in prog_pitch_tie:
                    # last acitve notes of previous segment is missing in tie information.
                    # We create a note off event for these notes at the beginning of current note events.
                    merged_note_events.append(
                        NoteEvent(is_drum=False,
                                  program=prog_pitch_pla[0],
                                  time=start_time,
                                  velocity=0,
                                  pitch=prog_pitch_pla[1]))
                    seg_merge_err_cnt['Err/merging segment tie'] += 1
            else:
                pass
        merged_note_events += nes
        prev_last_activity = last_activity

    # merged_note_events to notes
    notes, err_cnt = note_event2note(merged_note_events, tie_note_events=None, fix_offset=fix_offset)

    # gather error counts
    err_cnt.update(seg_merge_err_cnt)
    return notes, err_cnt
