# Copyright 2024 The YourMT3 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Please see the details in the LICENSE file.
import sys
import importlib
from dataclasses import dataclass, field
from typing import Set, List, Optional

if sys.version_info >= (3, 8):
    typing_module = importlib.import_module("typing")
else:
    typing_module = importlib.import_module("typing_extensions")
TypedDict = typing_module.TypedDict


@dataclass
class Note:
    is_drum: bool
    program: int  # MIDI program number (0-127)
    onset: float  # onset time in seconds
    offset: float  # offset time in seconds
    pitch: int  # MIDI note number (0-127)
    velocity: int  # (0-1) if ignore_velocity is True, otherwise (0-127)


@dataclass
class NoteEvent:
    is_drum: bool
    program: int  # [0, 127], 128 for drum but ignored in tokenizer
    time: Optional[float]  # absolute time. allow None for tie note events
    velocity: int  # currently 1 for onset, 0 for offset, drum has no offset
    pitch: int  # MIDI pitch
    activity: Optional[Set[int]] = field(default_factory=set)

    def equals_except(self, note_event, *excluded_attrs) -> bool:
        """ Check if two NoteEvent instances are equal EXCEPT for the 
        specified attributes. """
        if not isinstance(note_event, NoteEvent):
            return False

        for attr, value in self.__dict__.items():
            if attr not in excluded_attrs and value != note_event.__dict__.get(attr):
                return False
        return True

    def equals_only(self, note_event, *included_attrs) -> bool:
        """ Check if two NoteEvent instances are equal for the 
        specified attributes. """
        if not isinstance(note_event, NoteEvent):
            return False

        for attr in included_attrs:
            if self.__dict__.get(attr) != note_event.__dict__.get(attr):
                return False
        return True


class NoteEventListsBundle(TypedDict):
    """ NoteEventListsBundle:

    A TypedDict class instance that contains multiple lists of NoteEvents for multiple segments.
    
    """
    note_events: List[List[NoteEvent]]
    tie_note_events: List[List[NoteEvent]]
    start_times: List[float]


@dataclass
class EventRange:
    type: str
    min_value: int
    max_value: int


@dataclass
class Event:
    type: str
    value: int
