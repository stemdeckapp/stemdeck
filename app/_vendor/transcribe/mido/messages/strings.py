# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

from .specs import SPEC_BY_TYPE, make_msgdict


def msg2str(msg, include_time=True):
    type_ = msg['type']
    spec = SPEC_BY_TYPE[type_]

    words = [type_]

    for name in spec['value_names']:
        value = msg[name]

        if name == 'data':
            value = '({})'.format(','.join(str(byte) for byte in value))
        words.append(f'{name}={value}')

    if include_time:
        words.append('time={}'.format(msg['time']))

    return str.join(' ', words)


def _parse_time(value):
    # Convert to int if possible.
    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    raise ValueError(f'invalid time {value!r}')


def _parse_data(value):
    if not value.startswith('(') and value.endswith(')'):
        raise ValueError('missing parentheses in data message')

    try:
        return [int(byte) for byte in value[1:-1].split(',')]
    except ValueError as ve:
        raise ValueError('unable to parse data bytes') from ve


def str2msg(text):
    """Parse str format and return message dict.

    No type or value checking is done. The caller is responsible for
    calling check_msgdict().
    """
    words = text.split()
    type_ = words[0]
    args = words[1:]

    msg = {}

    for arg in args:
        name, value = arg.split('=', 1)
        if name == 'time':
            value = _parse_time(value)
        elif name == 'data':
            value = _parse_data(value)
        else:
            value = int(value)

        msg[name] = value

    return make_msgdict(type_, msg)
