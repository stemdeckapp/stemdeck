# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Mido ports for pygame.midi.

Pygame uses PortMidi, so this is perhaps not very useful.

http://www.pygame.org/docs/ref/midi.html
"""

from pygame import midi

from ..ports import BaseInput, BaseOutput


def _get_device(device_id):
    keys = ['interface', 'name', 'is_input', 'is_output', 'opened']
    info = dict(zip(keys, midi.get_device_info(device_id)))
    # TODO: correct encoding?
    info['name'] = info['name'].decode('utf-8')
    info['id'] = device_id
    return info


def _get_default_device(get_input):
    if get_input:
        device_id = midi.get_default_input_id()
    else:
        device_id = midi.get_default_output_id()

    if device_id < 0:
        raise OSError('no default port found')

    return _get_device(device_id)


def _get_named_device(name, get_input):
    # Look for the device by name and type (input / output)
    for device in get_devices():
        if device['name'] != name:
            continue

        # Skip if device is the wrong type
        if get_input:
            if device['is_output']:
                continue
        else:
            if device['is_input']:
                continue

        if device['opened']:
            raise OSError(f'port already opened: {name!r}')

        return device
    else:
        raise OSError(f'unknown port: {name!r}')


def get_devices(**kwargs):
    midi.init()
    return [_get_device(device_id) for device_id in range(midi.get_count())]


class PortCommon:
    """
    Mixin with common things for input and output ports.
    """

    def _open(self, **kwargs):
        if kwargs.get('virtual'):
            raise ValueError('virtual ports are not supported'
                             ' by the Pygame backend')
        elif kwargs.get('callback'):
            raise ValueError('callbacks are not supported'
                             ' by the Pygame backend')

        midi.init()

        if self.name is None:
            device = _get_default_device(self.is_input)
            self.name = device['name']
        else:
            device = _get_named_device(self.name, self.is_input)

        if device['opened']:
            if self.is_input:
                devtype = 'input'
            else:
                devtype = 'output'
            raise OSError('{} port {!r} is already open'.format(devtype,
                                                                self.name))
        if self.is_input:
            self._port = midi.Input(device['id'])
        else:
            self._port = midi.Output(device['id'])

        self._device_type = 'Pygame/{}'.format(device['interface'])

    def _close(self):
        self._port.close()


class Input(PortCommon, BaseInput):
    """
    PortMidi Input port
    """

    def _receive(self, block=True):
        # I get hanging notes if MAX_EVENTS > 1, so I'll have to
        # resort to calling Pm_Read() in a loop until there are no
        # more pending events.

        while self._port.poll():
            bytes, time = self._port.read(1)[0]
            self._parser.feed(bytes)


class Output(PortCommon, BaseOutput):
    """
    PortMidi output port
    """

    def _send(self, message):
        if message.type == 'sysex':
            # Python 2 version of Pygame accepts a bytes or list here
            # while Python 3 version requires bytes.
            # According to the docs it should accept both so this may be
            # a bug in Pygame:
            # https://www.pygame.org/docs/ref/midi.html#pygame.midi.Output.write_sys_ex
            self._port.write_sys_ex(midi.time(), bytes(message.bin()))
        else:
            self._port.write_short(*message.bytes())
