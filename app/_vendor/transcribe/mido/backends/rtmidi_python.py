# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Backend for rtmidi-python:

https://pypi.python.org/pypi/rtmidi-python

To use this backend copy (or link) it to somewhere in your Python path
and call:

    mido.set_backend('mido.backends.rtmidi_python')

or set shell variable $MIDO_BACKEND to mido.backends.rtmidi_python

TODO:

* add support for APIs.

* active_sensing is still filtered. (The same is true for
  mido.backends.rtmidi.)There may be a way to remove this filtering.

"""
import queue

import rtmidi_python as rtmidi

# TODO: change this to a relative import if the backend is included in
# the package.
from ..ports import BaseInput, BaseOutput


def get_devices(api=None, **kwargs):
    devices = {}

    input_names = rtmidi.MidiIn().ports
    output_names = rtmidi.MidiOut().ports

    for name in input_names + output_names:
        if name not in devices:
            devices[name] = {
                'name': name,
                'is_input': name in input_names,
                'is_output': name in output_names,
            }

    return list(devices.values())


class PortCommon:
    def _open(self, virtual=False, **kwargs):

        self._queue = queue.Queue()
        self._callback = None

        # rtapi = _get_api_id(api)
        opening_input = hasattr(self, 'receive')

        if opening_input:
            self._rt = rtmidi.MidiIn()
            self._rt.ignore_types(False, False, True)
            self.callback = kwargs.get('callback')
        else:
            self._rt = rtmidi.MidiOut()  # rtapi=rtapi)
            # Turn of ignore of sysex, time and active_sensing.

        ports = self._rt.ports

        if virtual:
            if self.name is None:
                raise OSError('virtual port must have a name')
            self._rt.open_virtual_port(self.name)
        else:
            if self.name is None:
                # TODO: this could fail if list is empty.
                # In RtMidi, the default port is the first port.
                try:
                    self.name = ports[0]
                except IndexError as ie:
                    raise OSError('no ports available') from ie

            try:
                port_id = ports.index(self.name)
            except ValueError as ve:
                raise OSError(f'unknown port {self.name!r}') from ve

            try:
                self._rt.open_port(port_id)
            except RuntimeError as err:
                raise OSError(*err.args) from err

        # api = _api_to_name[self._rt.get_current_api()]
        api = ''
        self._device_type = f'RtMidi/{api}'
        if virtual:
            self._device_type = f'virtual {self._device_type}'

    @property
    def callback(self):
        return self._callback

    @callback.setter
    def callback(self, func):
        self._callback = func
        if func is None:
            self._rt.callback = None
        else:
            self._rt.callback = self._callback_wrapper

    def _callback_wrapper(self, msg_bytes, timestamp):
        self._parser.feed(msg_bytes)
        for message in self._parser:
            if self.callback:
                self.callback(message)

    def _close(self):
        self._rt.close_port()
        del self._rt  # Virtual ports are closed when this is deleted.


class Input(PortCommon, BaseInput):
    def _receive(self, block=True):
        # Since there is no blocking read in RtMidi, the block
        # flag is ignored and the enclosing receive() takes care
        # of blocking.

        while True:
            message_data, timestamp = self._rt.get_message()
            if message_data is None:
                break
            else:
                self._parser.feed(message_data)


class Output(PortCommon, BaseOutput):
    def _send(self, message):
        self._rt.send_message(message.bytes())
