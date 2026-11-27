# SPDX-FileCopyrightText: 2016 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""Mido amidi backend

Very experimental backend using amidi to access the ALSA rawmidi
interface.

TODO:

* use parser instead of from_hex()?
* default port name
* do sysex messages work?
* starting amidi for every message sent is costly
"""
import select
import subprocess
import threading

from ..messages import Message
from ._common import InputMethods, OutputMethods, PortMethods

"""
Dir Device    Name
IO  hw:1,0,0  UM-1 MIDI 1
IO  hw:2,0,0  nanoKONTROL2 MIDI 1
IO  hw:2,0,0  MPK mini MIDI 1
"""


def get_devices():
    devices = []

    lines = subprocess.check_output(
        ["amidi", "-l"],
        encoding="utf-8",
    ).splitlines()
    for line in lines[1:]:
        mode, device, name = line.strip().split(None, 2)

        devices.append({'name': name.strip(),
                        'device': device,
                        'is_input': 'I' in mode,
                        'is_output': 'O' in mode,
                        })

    return devices


def _get_device(name, mode):
    for dev in get_devices():
        if name == dev['name'] and dev[mode]:
            return dev
    else:
        raise OSError(f'unknown port {name!r}')


class Input(PortMethods, InputMethods):
    def __init__(self, name=None, **kwargs):
        self.name = name
        self.closed = False

        self._proc = None
        self._poller = select.poll()
        self._lock = threading.RLock()

        dev = _get_device(self.name, 'is_input')
        self._proc = subprocess.Popen(['amidi', '-d',
                                       '-p', dev['device']],
                                      stdout=subprocess.PIPE)

        self._poller.register(self._proc.stdout, select.POLLIN)

    def _read_message(self):
        line = self._proc.stdout.readline().strip().decode('ascii')
        if line:
            return Message.from_hex(line)
        else:
            # The first line is sometimes blank.
            return None

    def receive(self, block=True):
        if not block:
            return self.poll()

        while True:
            msg = self.poll()
            if msg:
                return msg

            # Wait for message.
            self._poller.poll()

    def poll(self):
        with self._lock:
            while self._poller.poll(0):
                msg = self._read_message()
                if msg is not None:
                    return msg

    def close(self):
        if not self.closed:
            if self._proc:
                self._proc.kill()
                self._proc = None
            self.closed = True


class Output(PortMethods, OutputMethods):
    def __init__(self, name=None, autoreset=False, **kwargs):
        self.name = name
        self.autoreset = autoreset
        self.closed = False

        self._dev = _get_device(self.name, 'is_output')

    def send(self, msg):
        proc = subprocess.Popen(['amidi', '--send-hex', msg.hex(),
                                 '-p', self._dev['device']])
        proc.wait()

    def close(self):
        if not self.closed:
            if self.autoreset:
                self.reset()

            self.closed = True
