# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

from .checks import check_data
from .specs import (
    CHANNEL_MESSAGES,
    MIN_PITCHWHEEL,
    SPEC_BY_STATUS,
    SYSEX_END,
    SYSEX_START,
)


def _decode_sysex_data(data):
    return {'data': tuple(data)}


def _decode_quarter_frame_data(data):
    return {'frame_type': data[0] >> 4,
            'frame_value': data[0] & 15}


def _decode_songpos_data(data):
    return {'pos': data[0] | (data[1] << 7)}


def _decode_pitchwheel_data(data):
    return {'pitch': data[0] | ((data[1] << 7) + MIN_PITCHWHEEL)}


def _make_special_cases():
    cases = {
        0xe0: _decode_pitchwheel_data,
        0xf0: _decode_sysex_data,
        0xf1: _decode_quarter_frame_data,
        0xf2: _decode_songpos_data,
    }

    for i in range(16):
        cases[0xe0 | i] = _decode_pitchwheel_data

    return cases


_SPECIAL_CASES = _make_special_cases()


def _decode_data_bytes(status_byte, data, spec):
    # Subtract 1 for status byte.
    if len(data) != (spec['length'] - 1):
        raise ValueError(
            'wrong number of bytes for {} message'.format(spec['type']))

    # TODO: better name than args?
    names = [name for name in spec['value_names'] if name != 'channel']
    args = {name: value for name, value in zip(names, data)}

    if status_byte in CHANNEL_MESSAGES:
        # Channel is stored in the lower nibble of the status byte.
        args['channel'] = status_byte & 0x0f

    return args


def decode_message(msg_bytes, time=0, check=True):
    """Decode message bytes and return messages as a dictionary.

    Raises ValueError if the bytes are out of range or the message is
    invalid.

    This is not a part of the public API.
    """
    # TODO: this function is getting long.

    if len(msg_bytes) == 0:
        raise ValueError('message is 0 bytes long')

    status_byte = msg_bytes[0]
    data = msg_bytes[1:]

    try:
        spec = SPEC_BY_STATUS[status_byte]
    except KeyError as ke:
        raise ValueError(f'invalid status byte {status_byte!r}') from ke

    msg = {
        'type': spec['type'],
        'time': time,
    }

    # Sysex.
    if status_byte == SYSEX_START:
        if len(data) < 1:
            raise ValueError('sysex without end byte')

        end = data[-1]
        data = data[:-1]
        if end != SYSEX_END:
            raise ValueError(f'invalid sysex end byte {end!r}')

    if check:
        check_data(data)

    if status_byte in _SPECIAL_CASES:
        if status_byte in CHANNEL_MESSAGES:
            msg['channel'] = status_byte & 0x0f

        msg.update(_SPECIAL_CASES[status_byte](data))
    else:
        msg.update(_decode_data_bytes(status_byte, data, spec))

    return msg
