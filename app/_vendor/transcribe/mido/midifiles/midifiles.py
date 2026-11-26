# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
MIDI file reading and playback.

References:

http://home.roadrunner.com/~jgglatt/
http://home.roadrunner.com/~jgglatt/tech/miditech.htm
http://home.roadrunner.com/~jgglatt/tech/midifile.htm

http://www.sonicspot.com/guide/midifiles.html
http://www.ccarh.org/courses/253/assignment/midifile/
https://code.google.com/p/binasc/wiki/mainpage
http://stackoverflow.com/questions/2984608/midi-delta-time
http://www.recordingblogs.com/sa/tabid/82/EntryId/44/MIDI-Part-XIII-Delta-time-a
http://www.sonicspot.com/guide/midifiles.html
"""

import string
import struct
import time
from numbers import Integral

from ..messages import SPEC_BY_STATUS, Message
from .meta import MetaMessage, build_meta_message, encode_variable_int, meta_charset
from .tracks import MidiTrack, fix_end_of_track, merge_tracks
from .units import tick2second

# The default tempo is 120 BPM.
# (500000 microseconds per beat (quarter note).)
DEFAULT_TEMPO = 500000
DEFAULT_TICKS_PER_BEAT = 480

# Maximum message length to attempt to read.
MAX_MESSAGE_LENGTH = 1000000


def print_byte(byte, pos=0):
    char = chr(byte)
    if char.isspace() or char not in string.printable:
        char = '.'

    print(f'  {pos:06x}: {byte:02x}  {char}')  # noqa: T201


class DebugFileWrapper:
    def __init__(self, file):
        self.file = file

    def read(self, size):
        data = self.file.read(size)

        for byte in data:
            print_byte(byte, self.file.tell())

        return data

    def tell(self):
        return self.file.tell()


def read_byte(self):
    byte = self.read(1)
    if byte == b'':
        raise EOFError
    else:
        return ord(byte)


def read_bytes(infile, size):
    if size > MAX_MESSAGE_LENGTH:
        raise OSError('Message length {} exceeds maximum length {}'.format(
            size, MAX_MESSAGE_LENGTH))
    return [read_byte(infile) for _ in range(size)]


def _dbg(text=''):
    print(text)  # noqa: T201


# We can't use the chunk module for two reasons:
#
# 1. we may have mixed big and little endian chunk sizes. (RIFF is
# little endian while MTrk is big endian.)
#
# 2. the chunk module assumes that chunks are padded to the nearest
# multiple of 2. This is not true of MIDI files.

def read_chunk_header(infile):
    header = infile.read(8)
    if len(header) < 8:
        raise EOFError

    # TODO: check for b'RIFF' and switch endian?

    return struct.unpack('>4sL', header)


def read_file_header(infile):
    name, size = read_chunk_header(infile)

    if name != b'MThd':
        raise OSError('MThd not found. Probably not a MIDI file')
    else:
        data = infile.read(size)

        if len(data) < 6:
            raise EOFError

        return struct.unpack('>hhh', data[:6])


def read_message(infile, status_byte, peek_data, delta, clip=False):
    try:
        spec = SPEC_BY_STATUS[status_byte]
    except LookupError as le:
        raise OSError(f'undefined status byte 0x{status_byte:02x}') from le

    # Subtract 1 for status byte.
    size = spec['length'] - 1 - len(peek_data)
    data_bytes = peek_data + read_bytes(infile, size)

    if clip:
        data_bytes = [byte if byte < 127 else 127 for byte in data_bytes]
    else:
        for byte in data_bytes:
            if byte > 127:
                raise OSError('data byte must be in range 0..127')

    return Message.from_bytes([status_byte] + data_bytes, time=delta)


def read_sysex(infile, delta, clip=False):
    length = read_variable_int(infile)
    data = read_bytes(infile, length)

    # Strip start and end bytes.
    # TODO: is this necessary?
    if data and data[0] == 0xf0:
        data = data[1:]
    if data and data[-1] == 0xf7:
        data = data[:-1]

    if clip:
        data = [byte if byte < 127 else 127 for byte in data]

    return Message('sysex', data=data, time=delta)


def read_variable_int(infile):
    delta = 0

    while True:
        byte = read_byte(infile)
        delta = (delta << 7) | (byte & 0x7f)
        if byte < 0x80:
            return delta


def read_meta_message(infile, delta):
    meta_type = read_byte(infile)
    length = read_variable_int(infile)
    data = read_bytes(infile, length)
    return build_meta_message(meta_type, data, delta)


def read_track(infile, debug=False, clip=False):
    track = MidiTrack()

    name, size = read_chunk_header(infile)

    if name != b'MTrk':
        raise OSError('no MTrk header at start of track')

    if debug:
        _dbg(f'-> size={size}')
        _dbg()

    start = infile.tell()
    last_status = None

    while True:
        # End of track reached.
        if infile.tell() - start == size:
            break

        if debug:
            _dbg('Message:')

        delta = read_variable_int(infile)

        if debug:
            _dbg(f'-> delta={delta}')

        status_byte = read_byte(infile)

        if status_byte < 0x80:
            if last_status is None:
                raise OSError('running status without last_status')
            peek_data = [status_byte]
            status_byte = last_status
        else:
            if status_byte != 0xff:
                # Meta messages don't set running status.
                last_status = status_byte
            peek_data = []

        if status_byte == 0xff:
            msg = read_meta_message(infile, delta)
        elif status_byte in [0xf0, 0xf7]:
            # TODO: I'm not quite clear on the difference between
            # f0 and f7 events.
            msg = read_sysex(infile, delta, clip)
        else:
            msg = read_message(infile, status_byte, peek_data, delta, clip)

        track.append(msg)

        if debug:
            _dbg(f'-> {msg!r}')
            _dbg()

    return track


def write_chunk(outfile, name, data):
    """Write an IFF chunk to the file.

    `name` must be a bytestring."""
    outfile.write(name)
    outfile.write(struct.pack('>L', len(data)))
    outfile.write(data)


def write_track(outfile, track):
    data = bytearray()

    running_status_byte = None
    for msg in fix_end_of_track(track):
        if not isinstance(msg.time, Integral):
            raise ValueError('message time must be int in MIDI file')
        if msg.time < 0:
            raise ValueError('message time must be non-negative in MIDI file')

        if msg.is_realtime:
            raise ValueError('realtime messages are not allowed in MIDI files')

        data.extend(encode_variable_int(msg.time))

        if msg.is_meta:
            data.extend(msg.bytes())
            running_status_byte = None
        elif msg.type == 'sysex':
            data.append(0xf0)
            # length (+ 1 for end byte (0xf7))
            data.extend(encode_variable_int(len(msg.data) + 1))
            data.extend(msg.data)
            data.append(0xf7)
            running_status_byte = None
        else:
            msg_bytes = msg.bytes()
            status_byte = msg_bytes[0]

            if status_byte == running_status_byte:
                data.extend(msg_bytes[1:])
            else:
                data.extend(msg_bytes)

            if status_byte < 0xf0:
                running_status_byte = status_byte
            else:
                running_status_byte = None

    write_chunk(outfile, b'MTrk', data)


def get_seconds_per_tick(tempo, ticks_per_beat):
    # Tempo is given in microseconds per beat (default 500000).
    # At this tempo there are (500000 / 1000000) == 0.5 seconds
    # per beat. At the default resolution of 480 ticks per beat
    # this is:
    #
    #    (500000 / 1000000) / 480 == 0.5 / 480 == 0.0010417
    #
    return (tempo / 1000000.0) / ticks_per_beat


class MidiFile:
    def __init__(self, filename=None, file=None,
                 type=1, ticks_per_beat=DEFAULT_TICKS_PER_BEAT,
                 charset='latin1',
                 debug=False,
                 clip=False,
                 tracks=None
                 ):

        self.filename = filename
        self.type = type
        self.ticks_per_beat = ticks_per_beat
        self.charset = charset
        self.debug = debug
        self.clip = clip

        self.tracks = []
        self._merged_track = None

        if type not in range(3):
            raise ValueError(
                f'invalid format {format} (must be 0, 1 or 2)')

        if tracks is not None:
            self.tracks = tracks
        elif file is not None:
            self._load(file)
        elif self.filename is not None:
            with open(filename, 'rb') as file:
                self._load(file)

    @property
    def merged_track(self):
        # The tracks of type 2 files are not in sync, so they can
        # not be played back like this.
        if self.type == 2:
            raise TypeError("can't merge tracks in type 2 (asynchronous) file")

        if self._merged_track is None:
            self._merged_track = merge_tracks(self.tracks, skip_checks=True)
        return self._merged_track

    @merged_track.deleter
    def merged_track(self):
        self._merged_track = None

    def add_track(self, name=None):
        """Add a new track to the file.

        This will create a new MidiTrack object and append it to the
        track list.
        """
        track = MidiTrack()
        if name is not None:
            track.name = name
        self.tracks.append(track)
        del self.merged_track  # uncache merged track
        return track

    def _load(self, infile):
        if self.debug:
            infile = DebugFileWrapper(infile)

        with meta_charset(self.charset):
            if self.debug:
                _dbg('Header:')

            (self.type,
             num_tracks,
             self.ticks_per_beat) = read_file_header(infile)

            if self.debug:
                _dbg('-> type={}, tracks={}, ticks_per_beat={}'.format(
                    self.type, num_tracks, self.ticks_per_beat))
                _dbg()

            for i in range(num_tracks):
                if self.debug:
                    _dbg(f'Track {i}:')

                self.tracks.append(read_track(infile,
                                              debug=self.debug,
                                              clip=self.clip))
                # TODO: used to ignore EOFError. I hope things still work.

    @property
    def length(self):
        """Playback time in seconds.

        This will be computed by going through every message in every
        track and adding up delta times.
        """
        if self.type == 2:
            raise ValueError('impossible to compute length'
                             ' for type 2 (asynchronous) file')

        return sum(msg.time for msg in self)

    def __iter__(self):
        tempo = DEFAULT_TEMPO
        for msg in self.merged_track:
            # Convert message time from absolute time
            # in ticks to relative time in seconds.
            if msg.time > 0:
                delta = tick2second(msg.time, self.ticks_per_beat, tempo)
            else:
                delta = 0

            yield msg.copy(skip_checks=True, time=delta)

            if msg.type == 'set_tempo':
                tempo = msg.tempo

    def play(self, meta_messages=False, now=time.time):
        """Play back all tracks.

        The generator will sleep between each message by
        default. Messages are yielded with correct timing. The time
        attribute is set to the number of seconds slept since the
        previous message.

        By default you will only get normal MIDI messages. Pass
        meta_messages=True if you also want meta messages.

        You will receive copies of the original messages, so you can
        safely modify them without ruining the tracks.

        By default the system clock is used for the timing of yielded
        MIDI events. To use a different clock (e.g. to synchronize to
        an audio stream), pass now=time_fn where time_fn is a zero
        argument function that yields the current time in seconds.
        """
        start_time = now()
        input_time = 0.0

        for msg in self:
            input_time += msg.time

            playback_time = now() - start_time
            duration_to_next_event = input_time - playback_time

            if duration_to_next_event > 0.0:
                time.sleep(duration_to_next_event)

            if isinstance(msg, MetaMessage) and not meta_messages:
                continue
            else:
                yield msg

    def save(self, filename=None, file=None):
        """Save to a file.

        If file is passed the data will be saved to that file. This is
        typically an in-memory file or and already open file like sys.stdout.

        If filename is passed the data will be saved to that file.

        Raises ValueError if both file and filename are None,
        or if a type 0 file has != one track.
        """
        if self.type == 0 and len(self.tracks) != 1:
            raise ValueError('type 0 file must have exactly 1 track')

        if file is not None:
            self._save(file)
        elif filename is not None:
            with open(filename, 'wb') as file:
                self._save(file)
        else:
            raise ValueError('requires filename or file')

    def _save(self, outfile):
        with meta_charset(self.charset):
            header = struct.pack('>hhh', self.type,
                                 len(self.tracks),
                                 self.ticks_per_beat)

            write_chunk(outfile, b'MThd', header)

            for track in self.tracks:
                write_track(outfile, track)

    def print_tracks(self, meta_only=False):
        """Prints out all messages in a .midi file.

        May take argument meta_only to show only meta messages.

        Use:
        print_tracks() -> will print all messages
        print_tracks(meta_only=True) -> will print only MetaMessages
        """
        for i, track in enumerate(self.tracks):
            print(f'=== Track {i}')  # noqa: T201
            for msg in track:
                if isinstance(msg, MetaMessage) or not meta_only:
                    print(f'{msg!r}')  # noqa: T201

    def __repr__(self):
        if self.tracks:
            tracks_str = ',\n'.join(repr(track) for track in self.tracks)
            tracks_str = '  ' + tracks_str.replace('\n', '\n  ')
            tracks_str = f', tracks=[\n{tracks_str}\n]'
        else:
            tracks_str = ''

        return '{}(type={}, ticks_per_beat={}{})'.format(
            self.__class__.__name__,
            self.type,
            self.ticks_per_beat,
            tracks_str,
        )

    # The context manager has no purpose but is kept around since it was
    # used in examples in the past.
    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        return False
