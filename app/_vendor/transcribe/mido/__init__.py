# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
MIDI Objects for Python

Mido is a library for working with MIDI messages and ports. It's
designed to be as straight forward and Pythonic as possible.

Creating messages:

    Message(type, **parameters) -- create a new message
    MetaMessage(type, **parameters) -- create a new meta message
    UnknownMetaMessage(type_byte, data=None, time=0)

Ports:

    open_input(name=None, virtual=False, callback=None) -- open an input port
    open_output(name=None, virtual=False,               -- open an output port
                autoreset=False)
    open_ioport(name=None, virtual=False,        -- open an I/O port (capable
                callback=None, autoreset=False)     of both input and output)

    get_input_names() -- return a list of names of available input ports
    get_output_names() -- return a list of names of available output ports
    get_ioport_names() -- return a list of names of available I/O ports

MIDI files:

    MidiFile(filename, **kwargs) -- open a MIDI file
    MidiTrack()  -- a MIDI track
    bpm2tempo()  -- convert beats per minute to MIDI file tempo
    tempo2bpm()  -- convert MIDI file tempo to beats per minute
    merge_tracks(tracks)  -- merge tracks into one track

SYX files:

    read_syx_file(filename)  -- read a SYX file
    write_syx_file(filename, messages,
                   plaintext=False)  -- write a SYX file
Parsing MIDI streams:

    parse(bytes) -- parse a single message bytes
                    (any iterable that generates integers in 0..127)
    parse_all(bytes) -- parse all messages bytes
    Parser -- MIDI parser class

Parsing objects serialized with str(message):

    parse_string(string) -- parse a string containing a message
    parse_string_stream(iterable) -- parse strings from an iterable and
                                     generate messages

Sub modules:

    ports -- useful tools for working with ports

For more on MIDI, see:

    http://www.midi.org/


Getting started:

    >>> import mido
    >>> m = mido.Message('note_on', note=60, velocity=64)
    >>> m
    <message note_on channel=0, note=60, velocity=64, time=0>
    >>> m.type
    'note_on'
    >>> m.channel = 6
    >>> m.note = 19
    >>> m.copy(velocity=120)
    <message note_on channel=0, note=60, velocity=64, time=0>
    >>> s = mido.Message('sysex', data=[byte for byte in range(5)])
    >>> s.data
    (0, 1, 2, 3, 4)
    >>> s.hex()
    'F0 00 01 02 03 04 F7'
    >>> len(s)
    7

    >>> default_input = mido.open_input()
    >>> default_input.name
    'MPK mini MIDI 1'
    >>> output = mido.open_output('SD-20 Part A')
    >>>
    >>> for message in default_input:
    ...     output.send(message)

    >>> get_input_names()
    ['MPK mini MIDI 1', 'SH-201']
"""

from . import ports, sockets
from .backends.backend import Backend
from .messages import (
    MAX_PITCHWHEEL,
    MAX_SONGPOS,
    MIN_PITCHWHEEL,
    MIN_SONGPOS,
    Message,
    format_as_string,
    parse_string,
    parse_string_stream,
)
from .midifiles import (
    KeySignatureError,
    MetaMessage,
    MidiFile,
    MidiTrack,
    UnknownMetaMessage,
    bpm2tempo,
    merge_tracks,
    second2tick,
    tempo2bpm,
    tick2second,
)
from .parser import Parser, parse, parse_all
from .syx import read_syx_file, write_syx_file
from .version import version_info

__all__ = [
    "KeySignatureError",
    "MAX_PITCHWHEEL",
    "MAX_SONGPOS",
    "MIN_PITCHWHEEL",
    "MIN_SONGPOS",
    "Message",
    "MetaMessage",
    "MidiFile",
    "MidiTrack",
    "Parser",
    "UnknownMetaMessage",
    "bpm2tempo",
    "format_as_string",
    "merge_tracks",
    "parse",
    "parse_all",
    "parse_string",
    "parse_string_stream",
    "ports",
    "read_syx_file",
    "second2tick",
    "sockets",
    "tempo2bpm",
    "tick2second",
    "version_info",
    "write_syx_file",
]


def set_backend(name=None, load=False):
    """Set current backend.

    name can be a module name like 'mido.backends.rtmidi' or
    a Backend object.

    If no name is passed, the default backend will be used.

    This will replace all the open_*() and get_*_name() functions
    in top level mido module. The module will be loaded the first
    time one of those functions is called."""

    glob = globals()

    if isinstance(name, Backend):
        backend = name
    else:
        backend = Backend(name, load=load, use_environ=True)
    glob['backend'] = backend

    for name in dir(backend):
        if name.split('_')[0] in ['open', 'get']:
            glob[name] = getattr(backend, name)


set_backend()
