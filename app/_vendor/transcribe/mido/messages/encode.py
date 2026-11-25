# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

from .specs import CHANNEL_MESSAGES, MIN_PITCHWHEEL, SPEC_BY_TYPE


def _encode_pitchwheel(msg):
    pitch = msg['pitch'] - MIN_PITCHWHEEL
    return [0xe0 | msg['channel'], pitch & 0x7f, pitch >> 7]


def _encode_sysex(msg):
    return [0xf0] + list(msg['data']) + [0xf7]


def _encode_quarter_frame(msg):
    return [0xf1, msg['frame_type'] << 4 | msg['frame_value']]


def _encode_songpos(data):
    pos = data['pos']
    return [0xf2, pos & 0x7f, pos >> 7]


def _encode_note_off(msg):
    return [0x80 | msg['channel'], msg['note'], msg['velocity']]


def _encode_note_on(msg):
    return [0x90 | msg['channel'], msg['note'], msg['velocity']]


def _encode_control_change(msg):
    return [0xb0 | msg['channel'], msg['control'], msg['value']]


_SPECIAL_CASES = {
    'pitchwheel': _encode_pitchwheel,
    'sysex': _encode_sysex,
    'quarter_frame': _encode_quarter_frame,
    'songpos': _encode_songpos,

    # These are so common that they get special cases to speed things up.
    'note_off': _encode_note_off,
    'note_on': _encode_note_on,
    'control_change': _encode_control_change,
}


def encode_message(msg):
    """Encode msg dict as a list of bytes.

    TODO: Add type and value checking.
          (Can be turned off with keyword argument.)

    This is not a part of the public API.
    """

    encode = _SPECIAL_CASES.get(msg['type'])
    if encode:
        return encode(msg)
    else:
        spec = SPEC_BY_TYPE[msg['type']]
        status_byte = spec['status_byte']

        if status_byte in CHANNEL_MESSAGES:
            status_byte |= msg['channel']

        data = [msg[name] for name in spec['value_names'] if name != 'channel']

        return [status_byte] + data
