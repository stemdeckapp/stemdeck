# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
""" note2event.py

Note tools:
• mix_notes(notes_to_mix, sort, trim_overlap, fix_offset)
    -> List[Note]
• validate_notes(notes, fix)
    -> List[Note]
• trim_overlapping_notes(notes, sort)
    -> List[Note]
• sort_notes(notes)
    -> List[Note]
• notes2pc_notes(notes, note_offs)
    -> List[Note]
• extract_program_from_notes(notes)
    -> Set[int]
• extract_notes_selected_by_programs(notes, programs, sort)
    -> List[Note]

Note to NoteEvent
• note2note_event(notes, sort, return_activity)
    -> List[NoteEvent]

NoteEvent tools:
• slice_note_events_and_ties(note_events, start_time, end_time, tidyup)
    -> Tuple[List[NoteEvent], List[NoteEvent], int])
• slice_multiple_note_events_and_ties_to_bundle(note_events, start_times, duration_sec, tidyup)
    -> List[List[NoteEvent], List[NoteEvent], int]] # Note implmented yet..
• mix_note_event_lists_bundle(note_events_to_mix, sort, start_time_to_zero)
    -> NoteEventListsBundle
• pitch_shift_note_events(note_events, semitone, use_deepcopy)
    -> List[NoteEvent]  
• separate_by_subunit_programs_from_note_event_lists_bundle(
        source_note_event_lists_bundle,
        subunit_programs)
    -> NoteEventListsBundle:
• separate_channel_by_program_group_from_note_event_lists_bundle(
        source_note_event_lists_bundle,
        num_program_groups,
        program2channel_vocab)
    -> List[NoteEventListsBundle]:

NoteEvent to Event:
• note_event2event(note_events, tie_note_events, start_time, tps, sort)
    -> List[Event]

Event tools:
• check_event_len_from_bundle(note_events_dic_a, note_events_dic_b, max_len, fast_check)
    -> bool
"""
import warnings
from copy import deepcopy
from itertools import chain
from typing import Optional, Tuple, Union, List, Set, Dict, Any

import numpy as np
from utils.note_event_dataclasses import Note, NoteEvent, NoteEventListsBundle
from utils.note_event_dataclasses import Event

DRUM_OFFSET_TIME = 0.01  # in seconds
MINIMUM_OFFSET_TIME = 0.01  # this is used to avoid zero-length notes
DRUM_PROGRAM = 128


def mix_notes(notes_to_mix: Tuple[List[Note]],
              sort: bool = True,
              trim_overlap: bool = True,
              fix_offset: bool = True) -> List[Note]:
    """
    mix_notes:
    Mixes a tuple of many lists of Note instances into a single list of Note
     instances. This processes 'notes1 + notes2 + ... + notesN' faster.
    Because Note instances use absolute timing, the Note instances in the 
    same timiming will be sorted by increasing order of program and pitch.

    Args:
    - notes_to_mix (tuple[list[Note]]): A tuple of lists of Note instances.
    - sort (bool): If True, sort the Note instances by increasing order of 
      onsets, and at the same timing, by increasing order of program and pitch.
      Default is True.

    Returns:
    - notes (list[Note]): A list of Note instances.
    """
    mixed_notes = list(chain(*notes_to_mix))
    if sort and len(mixed_notes) > 0:
        mixed_notes.sort(
            key=lambda note: (note.onset, note.is_drum, note.program, note.velocity, note.pitch, note.offset))

    # Trim overlapping notes
    if trim_overlap:
        mixed_notes = trim_overlapping_notes(mixed_notes, sort=sort)

    # fix offset >= onset the Note instances
    if fix_offset:
        mixed_notes = validate_notes(mixed_notes, fix=True)
    return mixed_notes


def validate_notes(notes: Tuple[List[Note]], minimum_offset: Optional[bool] = 0.01, fix: bool = True) -> List[Note]:
    """ validate and fix unrealistic notes """
    if len(notes) > 0:
        for note in list(notes):
            if note.onset == None:
                if fix:
                    notes.remove(note)
                continue
            elif note.offset == None:
                if fix:
                    note.offset = note.onset + MINIMUM_OFFSET_TIME
            elif note.onset > note.offset:
                warnings.warn(f'📙 Note at {note} has onset > offset.')
                if fix:
                    note.offset = max(note.offset, note.onset + MINIMUM_OFFSET_TIME)
                    print(f'✅\033[92m Fixed! Setting offset to onset + {MINIMUM_OFFSET_TIME}.\033[0m')
            elif note.is_drum is False and note.offset - note.onset < 0.01:
                # fix 13 Oct: too short notes issue for the dataset with non-MIDI annotations
                # warnings.warn(f'📙 Note at {note} has offset - onset < 0.01.')
                if fix:
                    note.offset = note.onset + MINIMUM_OFFSET_TIME
                    # print(f'✅\033[92m Fixed! Setting offset to onset + {MINIMUM_OFFSET_TIME}.\033[0m')

    return notes


def trim_overlapping_notes(notes: List[Note], sort: bool = True) -> List[Note]:
    """ Trim overlapping notes and dropping zero-length notes.
        https://github.com/magenta/mt3/blob/3deffa260ba7de3cf03cda1ea513a4d7ba7144ca/mt3/note_sequences.py#L52

        Trimming was only applied to train set, not test set in MT3.
    """
    if len(notes) <= 1:
        return notes

    trimmed_notes = []
    channels = set((note.pitch, note.program, note.is_drum) for note in notes)

    for pitch, program, is_drum in channels:
        channel_notes = [
            note for note in notes if note.pitch == pitch and note.program == program and note.is_drum == is_drum
        ]
        sorted_notes = sorted(channel_notes, key=lambda note: note.onset)

        for i in range(1, len(sorted_notes)):
            if sorted_notes[i - 1].offset > sorted_notes[i].onset:
                sorted_notes[i - 1].offset = sorted_notes[i].onset

        # Filter out zero-length notes
        valid_notes = [note for note in sorted_notes if note.onset < note.offset]

        trimmed_notes.extend(valid_notes)

    if sort:
        trimmed_notes.sort(key=lambda note: (note.onset, note.is_drum, note.program, note.velocity, note.pitch))
    return trimmed_notes


def sort_notes(notes: List[Note]) -> List[Note]:
    """ Sort notes by increasing order of onsets, and at the same timing, by increasing order of program and pitch. """
    if len(notes) > 0:
        notes.sort(key=lambda note: (note.onset, note.is_drum, note.program, note.velocity, note.pitch, note.offset))
    return notes


def notes2pc_notes(notes: List[Note], note_offset: int = 64) -> List[Note]:
    """ Convert a list of Note instances to a list of Pitch Class Set (PCS) instances. 
    This method is implemented for octave-ignore evaluation cases. """
    pc_notes = deepcopy(notes)
    for note in pc_notes:
        note.pitch = note.pitch % 12 + note_offset
    return pc_notes


def extract_program_from_notes(notes: List[Note]) -> Set[int]:
    """ Extract program numbers from a list of Note instances."""
    prg = set()
    for note in notes:
        if note.program not in prg:
            prg.add(note.program)
    return prg


def extract_notes_selected_by_programs(notes: List[Note], programs: Set[int], sort: bool = True) -> List[Note]:
    """ Extract notes selected by program numbers from a list of Note instances."""
    selected_notes = []
    for note in notes:
        if note.program in programs:
            selected_notes.append(note)
    if sort:
        selected_notes.sort(key=lambda note: (note.onset, note.is_drum, note.program, note.velocity, note.pitch))
    return selected_notes


""" 
NoteEvent data class:

Combines NoteEvent and NoteActivity for onset and offset events during Note to Event conversion.

Features:

Trackable: follow note activity by index
Sliceable: extract time ranges; time is absolute
Mergeable: combine two NoteEvent instances (re-index needed)
Mutable: mute events by program number, pitch
Transferable: easily convert to Note or Event tokens
"""


def note2note_event(notes: List[Note], sort: bool = True, return_activity: bool = True) -> List[NoteEvent]:
    """
    note2note_event:
    Converts a list of Note instances to a list of NoteEvent instances.

    Args:
    - notes (List[Note]): A list of Note instances.
    - sort (bool): Sort the NoteEvent instances by increasing order of onsets,
      and at the same timing, by increasing order of program and pitch.
      Default is True. If return_activity is set to True, NoteEvent instances
      are sorted regardless of this argument.
    - return_activity (bool): If True, return a list of NoteActivity instances

    Returns:
    - note_events (List[NoteEvent]): A list of NoteEvent instances.

    """
    note_events = []
    for note in notes:
        # for each note, add onset and offset events
        note_events.append(NoteEvent(note.is_drum, note.program, note.onset, note.velocity, note.pitch))
        if note.is_drum == 0:  # (drum has no offset!)
            note_events.append(NoteEvent(note.is_drum, note.program, note.offset, 0, note.pitch))

    if sort or return_activity:
        note_events.sort(key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))

    if return_activity:
        # activity stores the indices of previous notes that are still active
        activity = set()  # mutable class
        for i, ne in enumerate(note_events):
            # set a copy of the activity set ti the current note event
            ne.activity = activity.copy()

            if ne.is_drum:
                continue  # drum's offset and activity are not tracked
            elif ne.velocity == 1:
                activity.add(i)
            elif ne.velocity == 0:
                # search for the index of matching onset event
                matched_onset_event_index = None
                for j in activity:
                    if note_events[j].equals_only(ne, 'is_drum', 'program', 'pitch'):
                        matched_onset_event_index = j
                        break
                if matched_onset_event_index is not None:
                    activity.remove(matched_onset_event_index)
                else:
                    raise ValueError(f'📕 note2note_event: no matching onset event for {ne}')
            else:
                raise ValueError(f'📕 Invalid velocity: {ne.velocity} expected 0 or 1')
        if len(activity) > 0:
            # if there are still active notes at the end of the sequence
            warnings.warn(f'📙 note2note_event: {len(activity)} notes are still \
                          active at the end of the sequence. Please validate \
                          the input Note instances. ')
    return note_events


def slice_note_events_and_ties(note_events: List[NoteEvent],
                               start_time: float,
                               end_time: float,
                               tidyup: bool = False) -> Tuple[List[NoteEvent], List[NoteEvent], int]:
    """
    Extracts a specific subsequence of note events and tie note events for the
    first note event in the subsequence.
    
    Args:
    - note_events (List[NoteEvent]): List of NoteEvent instances.
    - start_time (float): The start time of the subsequence in seconds.
    - end_time (float): The end time of the subsequence in seconds.
    - tidyup (Optional[bool]): If True, sort the resulting lists of NoteEvents,
        and remove the activity attribute of sliced_note_event, and remove the
        time and activity attributes of tie_note_events. Default is False.
        Avoid using tidyup=True without deepcopying the original note_events.

    Note:
    - The activity attribute of returned sliced_note_events, and the time and
      activity attributes of tie_note_events are not valid after slicing. 
      Thus, they should be ignored in the downstream processing. 

    Returns:
    - sliced_note_events (List[NoteEvent]): List of NoteEvent instances in the
                                            specified range.
    - tie_note_events (List[NoteEvent]): List of NoteEvent instances that are
                                          active (tie) at start_time.
    - start_time (float): Just bypass the start time from the input argument.
    """
    if start_time > end_time:
        raise ValueError(f'📕 slice_note_events: start_time {start_time} \
                          is greater than end_time {end_time}')
    elif len(note_events) == 0:
        warnings.warn('📙 slice_note_events: empty note_events as input')
        return [], [], start_time

    # Get start_index and end_index
    start_index, end_index = None, None
    found_start = False
    for i, ne in enumerate(note_events):
        if not found_start and ne.time >= start_time and ne.time < end_time:
            start_index = i
            found_start = True

        if ne.time >= end_time:
            end_index = i
            break

    # Get tie_note_events
    if start_index == None:
        if end_index == 0:
            tie_note_events = []
        elif end_index == None:
            tie_note_events = []
        else:
            tie_note_events = [note_events[i] for i in note_events[end_index].activity]
    else:
        tie_note_events = [note_events[i] for i in note_events[start_index].activity]
    """ modifying note events here is dangerous, due to mutability of original note_events!! """
    if tidyup:
        for tne in tie_note_events:
            tne.time = None
            tne.activity = None

    tie_note_events.sort(key=lambda n_ev: (n_ev.program, n_ev.pitch))

    # Get sliced note_events
    if start_index is None:
        sliced_note_events = []
    else:
        sliced_note_events = note_events[start_index:end_index]

    if tidyup:
        for sne in sliced_note_events:
            sne.activity = None

    sliced_note_events.sort(key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))
    return sliced_note_events, tie_note_events, start_time


"""
class NoteEventListsBundle(TypedDict):
    note_events: List[List[NoteEvent]]
    tie_note_events: List[List[NoteEvent]]
    start_time: List[int]
"""


def slice_multiple_note_events_and_ties_to_bundle(note_events: List[NoteEvent],
                                                  start_times: List[float],
                                                  duration_sec: float,
                                                  tidyup: bool = False) -> NoteEventListsBundle:
    """
    Extracts N subsequence of note events and tie-note events by taking
    a list of N start_time and a list of N end_time.
    """
    sliced_note_events_list = []
    sliced_tie_note_events_list = []
    for start_time in start_times:
        end_time = start_time + duration_sec
        sliced_note_events, tie_note_events, _ = slice_note_events_and_ties(note_events, start_time, end_time, tidyup)
        sliced_note_events_list.append(sliced_note_events)
        sliced_tie_note_events_list.append(tie_note_events)
    return NoteEventListsBundle({
        'note_events': sliced_note_events_list,
        'tie_note_events': sliced_tie_note_events_list,
        'start_times': start_times
    })


def mix_note_event_lists_bundle(
    note_event_lists_bundle_to_mix: NoteEventListsBundle,
    sort: bool = True,
    start_time_to_zero: bool = True,
    use_deepcopy: bool = False,
) -> NoteEventListsBundle:
    """
    Mixes a tuple of many lists of NoteEvent instances into a single list of NoteEvent
    instances. This processes 'note_events1 + note_events2 + ... + note_eventsN'.
    Because each NoteEvent list instance may have different start time, it is recommended
    to set start_time_to_zero to True.

    Known issue:
    - Solution for overlapping note_events is not implemented yet.
    - Currently, it is assumed that programs have no overlap among note_events_to_mix.
    - For faster processing, use_deepcopy is set to False by default.  

    Args:
    - note_events_bundle_to_mix (NoteEventListsBundle):
      A dictionary with keys ('note_events', 'tie_note_events', 'start_time').
      See NoteEventListsBundle in utils/note_event_dataclasses.py for more details.
    - sort (bool): If True, sort the NoteEvent instances by increasing order of onsets,
      and at the same timing, by increasing order of program and pitch.
      Default is True.
    - start_time_to_zero (bool): If True, set the start time of each list of NoteEvents to 0.
      Default is True.
    - use_deepcopy (bool): If True, use deepcopy() to avoid modifying the original NoteEvent

    Returns:
    - mixed_note_events_dic (NoteEventListsBundle): A dictionary with keys ('note_events', 'tie_note_events', 'start_time').
    """
    if use_deepcopy is True:
        note_events_to_mix = deepcopy(note_event_lists_bundle_to_mix["note_events"])
        tie_note_events_to_mix = deepcopy(note_event_lists_bundle_to_mix["tie_note_events"])
    else:
        note_events_to_mix = note_event_lists_bundle_to_mix["note_events"]
        tie_note_events_to_mix = note_event_lists_bundle_to_mix["tie_note_events"]
    start_times = note_event_lists_bundle_to_mix["start_times"]

    # Reset start time to zero
    if start_time_to_zero is True:
        for note_events, tie_note_events, start_time in zip(note_events_to_mix, tie_note_events_to_mix, start_times):
            for ne in note_events:
                ne.time -= start_time
                assert ne.time >= 0, f'📕 mix_note_events: negative time {ne.time}'
            """modifying tie note events here is dangerous, due to mutability of linked note_events"""
            # for tne in tie_note_events:
            #     tne.time = None
            #     tne.activity = None

    # Mix
    mixed_note_events = list(chain(*note_events_to_mix))
    mixed_tie_note_events = list(chain(*tie_note_events_to_mix))

    # Sort
    if sort is True:
        mixed_note_events.sort(key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))
        mixed_tie_note_events.sort(key=lambda n_ev: (n_ev.program, n_ev.pitch))

    mixed_note_events_dic = NoteEventListsBundle({
        'note_events': [mixed_note_events],
        'tie_note_events': [mixed_tie_note_events],
        'start_times': [0.]
    })
    return mixed_note_events_dic


def pitch_shift_note_events(note_events: List[NoteEvent], semitone: int, use_deepcopy: bool = False) -> List[NoteEvent]:
    """
    Apply pitch shift to NoteEvent instances:
    
    Args:
    - note_events (List[NoteEvent]): A list of NoteEvent instances. Typically 'note_events' or
      'tie_note_events' can be an input.
    - semitone (int): The number of semitones to shift. Positive value shifts up, negative value
    - use_deepcopy (bool): If True, use deepcopy() to avoid modifying the original NoteEvent
    
    Returns:
    - note_events (List[NoteEvent]): A list of NoteEvent instances with pitch shifted. Drums are
      excluded from pitch shift processing.
    """
    if semitone == 0:
        return note_events

    if use_deepcopy is True:
        note_events = deepcopy(note_events)

    for ne in note_events:
        if ne.is_drum is False:
            new_pitch = ne.pitch + semitone
            if new_pitch >= 0 and new_pitch < 128:
                ne.pitch = new_pitch
    return note_events


def separate_by_subunit_programs_from_note_event_lists_bundle(source_note_event_lists_bundle: NoteEventListsBundle,
                                                              subunit_programs: List[List[int]],
                                                              start_time_to_zero: bool = True,
                                                              sort: bool = True) -> NoteEventListsBundle:
    src_note_events = source_note_event_lists_bundle['note_events']
    src_tie_note_events = source_note_event_lists_bundle['tie_note_events']
    src_start_times = source_note_event_lists_bundle['start_times']

    # Reset start time to zero
    if start_time_to_zero is True and not all(t == 0. for t in src_start_times):
        for nes, tnes, start_time in zip(src_note_events, src_tie_note_events, src_start_times):
            for ne in nes:
                ne.time -= start_time
                assert ne.time >= 0, f'📕 mix_note_events: negative time {ne.time}'
            for tne in tnes:
                tne.time = None
                tne.activity = None
        src_start_times = [0. for i in range(len(src_start_times))]

    num_subunits = len(subunit_programs)
    result_note_events = [[] for _ in range(num_subunits)]
    result_tie_note_events = [[] for _ in range(num_subunits)]
    result_start_times = [0. for _ in range(num_subunits)]

    # Convert subunit_programs to list of sets for faster lookups
    subunit_program_sets = [set(sp) for sp in subunit_programs]

    for nes, tnes in zip(src_note_events, src_tie_note_events):
        for ne in nes:
            if ne.is_drum:
                target_indices = [i for i, sp_set in enumerate(subunit_program_sets) if DRUM_PROGRAM in sp_set]
            else:
                target_indices = [i for i, sp_set in enumerate(subunit_program_sets) if ne.program in sp_set]
            for i in target_indices:
                result_note_events[i].append(ne)

        for tne in tnes:
            target_indices = [i for i, sp_set in enumerate(subunit_program_sets) if tne.program in sp_set]
            for i in target_indices:
                result_tie_note_events[i].append(tne)

    # Sort
    if sort is True:
        for nes, tnes in zip(result_note_events, result_tie_note_events):
            nes.sort(key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))
            tnes.sort(key=lambda n_ev: (n_ev.program, n_ev.pitch))

    return {
        'note_events': result_note_events,  # List[List[NoteEvent]]
        'tie_note_events': result_tie_note_events,  # List[List[NoteEvent]]
        'start_times': result_start_times,  # List[float]
    }


def separate_channel_by_program_group_from_note_event_lists_bundle(source_note_event_lists_bundle: NoteEventListsBundle,
                                                                   num_program_groups: int,
                                                                   program2channel_vocab: Dict[int, Dict[str, Any]],
                                                                   start_time_to_zero: bool = False,
                                                                   sort: bool = True) -> List[NoteEventListsBundle]:
    """
    Args:
    - source_note_event_lists_bundle (NoteEventListsBundle):
        A dictionary with keys ('note_events', 'tie_note_events', 'start_time').
        See NoteEventListsBundle in utils/note_event_dataclasses.py for more details.
    - num_program_groups (int): The number of program groups to separate. Typically this is the length
        of program_vocab + 1 (for drums).
    - program2channel_vocab (Dict[int, Dict[str, Union[List[int], np.ndarray]]]):
        A dictionary with keys (program, channel, instrument_group, primary_program).
        See program2channel_vocab in utils/utils.py, create_program2channel_vocab() for more details.
        example:
            program2channel_vocab[program_int] = {
                        "channel": (int),
                        "instrument_group": (str),
                        "primary_program": (int),
            }
    - start_time_to_zero (bool): If True, set the start time of each list of NoteEvents to 0.
        Default is False.
    - sort (bool): If True, sort the NoteEvent instances by increasing order of onsets,
        and at the same timing, by increasing order of program and pitch.
        Default is True.
    
    Returns:
    - result_list_bundle List[NoteEventListsBundle]: A list of NoteEventListsBundle instances with length
        of batch_sz.
        NoteEventListsBundle is a dictionary with keys ('note_events', 'tie_note_events', 'start_time').
        See NoteEventListsBundle in utils/note_event_dataclasses.py for more details.

    """
    src_note_events = source_note_event_lists_bundle['note_events']
    src_tie_note_events = source_note_event_lists_bundle['tie_note_events']
    src_start_times = source_note_event_lists_bundle['start_times']

    # Reset start time to zero
    if start_time_to_zero is True and not all(t == 0. for t in src_start_times):
        for nes, tnes, start_time in zip(src_note_events, src_tie_note_events, src_start_times):
            """modifying time of note events is only for mixing events within training. test set should keep the original time"""
            for ne in nes:
                ne.time -= start_time
                assert ne.time >= 0, f'📕 mix_note_events: negative time {ne.time}'
            """modifying tie note events here is dangerous, due to mutability of linked note_events"""
            # for tne in tnes:
            #     tne.time = None
            #     tne.activity = None
        src_start_times = [0. for i in range(len(src_start_times))]

    batch_sz = len(src_note_events)
    result_list_bundle = [{
        "note_events": [[] for _ in range(num_program_groups)],
        "tie_note_events": [[] for _ in range(num_program_groups)],
        "start_times": [src_start_times[b] for _ in range(num_program_groups)],
    } for b in range(batch_sz)]
    """ Example of program2channel_vocab
        {
            0: {'channel': 0, 'instrument_group': 'Piano', 'primary_program': 0},
            1: {'channel': 1, 'instrument_group': 'Chromatic Percussion', 'primary_program': 8},
            ...
            100: {'channel': 11, 'instrument_group': 'Singing Voice', 'primary_program': 100},
            128: {'channel': 12, 'instrument_group': 'Drums', 'primary_program': 128}
        }
    """
    # Separate by program_vocab
    for b, (nes, tnes) in enumerate(zip(src_note_events, src_tie_note_events)):
        for ne in nes:
            program = DRUM_PROGRAM if ne.is_drum else ne.program
            mapping_info = program2channel_vocab.get(program, None)
            if mapping_info is not None:
                ch = mapping_info["channel"]
                result_list_bundle[b]["note_events"][ch].append(ne)
            else:
                # Temporary fix for program > 95, such as gunshot and FX. TODO: FX class
                pass

        for tne in tnes:
            mapping_info = program2channel_vocab.get(tne.program)
            if mapping_info is not None:
                ch = mapping_info["channel"]
                result_list_bundle[b]["tie_note_events"][ch].append(tne)
            else:
                # Temporary fix for program > 95, such as gunshot and FX. TODO: FX class
                pass

        # Sort
        if sort:
            for ch in range(num_program_groups):
                result_list_bundle[b]["note_events"][ch].sort(
                    key=lambda n_ev: (n_ev.time, n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))
                result_list_bundle[b]["tie_note_events"][ch].sort(key=lambda n_ev: (n_ev.program, n_ev.pitch))

    return result_list_bundle  # List[NoteEventListsBundle] with length of batch_sz


def note_event2event(note_events: List[NoteEvent],
                     tie_note_events: Optional[List[NoteEvent]] = None,
                     start_time: float = 0.,
                     tps: int = 100,
                     sort: bool = True) -> List[Event]:
    """ note_event2event:
    Converts a list of NoteEvent instances to a list of Event instances.
    - NoteEvent instances have absolute time within a file, while Event instances
        have 'shift' events of absolute time within a segment.
    - Tie NoteEvent instances are prepended to output list of Event instances,
        and closed by a 'tie' event.
    - If start_time is not provided, start_time=0 in seconds by default. 
    - If there is non-tie note_event instances before the start_time, raises an error.

    Args:
    - note_events (list[NoteEvent]): A list of NoteEvent instances.
    - tie_note_events (Optional[list[NoteEvent]]): A list of tie NoteEvent instances.
        See slice_note_events_and_ties() for more details. Default is None.
    - start_time (float): Start time in seconds. Default is 0. Any non-tie NoteEvent 
        instances should have time >= start_time. 
    - tps (Optional[int]): Ticks per second. Default is 100.
    - sort (bool): If True, sort the Event instances by increasing order of
        onsets, and at the same timing, by increasing order of program and pitch.
        Default is False.

    Returns:
    - events (list[Event]): A list of Event instances.
    """
    if sort:
        if tie_note_events != None:
            tie_note_events.sort(key=lambda n_ev: (n_ev.program, n_ev.pitch))
        note_events.sort(
            key=lambda n_ev: (round(n_ev.time * tps), n_ev.is_drum, n_ev.program, n_ev.velocity, n_ev.pitch))

    # Initialize event list and state variables
    events = []
    start_tick = round(start_time * tps)
    tick_state = start_tick

    program_state = None

    # Prepend tie events
    if tie_note_events:
        for tne in tie_note_events:
            if tne.program != program_state:
                events.append(Event(type='program', value=tne.program))
                program_state = tne.program
            events.append(Event(type='pitch', value=tne.pitch))

    # Any tie events (can be empty) are closed by a 'tie' event
    events.append(Event(type='tie', value=0))

    # Translate NoteEvent to Event in the list
    velocity_state = None  # reset state variables
    for ne in note_events:
        if ne.is_drum and ne.velocity == 0:  # <-- bug fix
            continue  # drum's offset should be ignored, and should not cause shift

        # Process time shift and update tick_state
        ne_tick = round(ne.time * tps)
        if ne_tick > tick_state:
            # shift_ticks = ne_tick - tick_state
            shift_ticks = ne_tick - start_tick
            events.append(Event(type='shift', value=shift_ticks))
            tick_state = ne_tick
        elif ne_tick == tick_state:
            pass
        else:
            raise ValueError(
                f'NoteEvent tick_state {ne_tick} of time {ne.time} is smaller than tick_state {tick_state}.')

        # Process program change and update program_state
        if ne.is_drum and ne.velocity == 1:
            # drum events have no program and offset but velocity 1
            if velocity_state != 1 or velocity_state == None:
                events.append(Event(type='velocity', value=1))
                velocity_state = 1
            events.append(Event(type='drum', value=ne.pitch))
        else:
            if ne.program != program_state or program_state == None:
                events.append(Event(type='program', value=ne.program))
                program_state = ne.program

            if ne.velocity != velocity_state or velocity_state == None:
                events.append(Event(type='velocity', value=ne.velocity))
                velocity_state = ne.velocity

            events.append(Event(type='pitch', value=ne.pitch))

    return events


def check_event_len_from_bundle(note_events_dic_a: Dict,
                                note_events_dic_b: Dict,
                                max_len: int,
                                fast_check: bool = True) -> bool:
    """
    Check if the total length of events converted from note_events_dic exceeds the max length.
    This is used in cross augmentation. See augment.py for more the usage.
    
    Args:
    - note_events_dic_a (Dict): A dictionary with keys ('note_events', 'tie_note_events', 'start_time').
    - note_events_dic_b (Dict): A dictionary with keys ('note_events', 'tie_note_events', 'start_time').
    - max_len (int): Maximum length of events.
    - fast_check (bool): If True, check the total length of note_events only. Default is True.

    Returns:
    - bool: True (passed) or False (failed)
    """
    if fast_check is True:
        ne_len_a = sum([len(ne) for ne in note_events_dic_a['note_events']])
        ne_len_b = sum([len(ne) for ne in note_events_dic_b['note_events']])
        total_note_events_len = ne_len_a + ne_len_b

    if fast_check is False or total_note_events_len >= max_len // 3:
        event_len_a = 0
        for ne, tne, start_time in zip(note_events_dic_a['note_events'], note_events_dic_a['tie_note_events'],
                                       note_events_dic_a['start_times']):
            event_len_a += len(note_event2event(ne, tne, start_time))

        event_len_b = 0
        for ne, tne, start_time in zip(note_events_dic_b['note_events'], note_events_dic_b['tie_note_events'],
                                       note_events_dic_b['start_times']):
            event_len_b += len(note_event2event(ne, tne, start_time))

        total_events_len = event_len_a + event_len_b
        if total_events_len >= max_len:
            return False  # failed
    else:
        return True  # passed
