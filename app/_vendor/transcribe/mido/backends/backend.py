# SPDX-FileCopyrightText: 2014 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import importlib
import os

from .. import ports

DEFAULT_BACKEND = 'mido.backends.rtmidi'


class Backend:
    """
    Wrapper for backend module.

    A backend module implements classes for input and output ports for
    a specific MIDI library. The Backend object wraps around the
    object and provides convenient 'open_*()' and 'get_*_names()'
    functions.
    """
    def __init__(self, name=None, api=None, load=False, use_environ=True):
        self.name = name or os.environ.get('MIDO_BACKEND', DEFAULT_BACKEND)
        self.api = api
        self.use_environ = use_environ
        self._module = None

        # Split out api (if present).
        if api:
            self.api = api
        elif self.name and '/' in self.name:
            self.name, self.api = self.name.split('/', 1)
        else:
            self.api = None

        if load:
            self.load()

    @property
    def module(self):
        """A reference module implementing the backend.

        This will always be a valid reference to a module. Accessing
        this property will load the module. Use .loaded to check if
        the module is loaded.
        """
        self.load()
        return self._module

    @property
    def loaded(self):
        """Return True if the module is loaded."""
        return self._module is not None

    def load(self):
        """Load the module.

        Does nothing if the module is already loaded.

        This function will be called if you access the 'module'
        property."""
        if not self.loaded:
            self._module = importlib.import_module(self.name)

    def _env(self, name):
        if self.use_environ:
            return os.environ.get(name)
        else:
            return None

    def _add_api(self, kwargs):
        if self.api and 'api' not in kwargs:
            kwargs['api'] = self.api
        return kwargs

    def open_input(self, name=None, virtual=False, callback=None, **kwargs):
        """Open an input port.

        If the environment variable MIDO_DEFAULT_INPUT is set,
        it will override the default port.

        virtual=False
          Passing True opens a new port that other applications can
          connect to. Raises IOError if not supported by the backend.

        callback=None
          A callback function to be called when a new message arrives.
          The function should take one argument (the message).
          Raises IOError if not supported by the backend.
        """
        kwargs.update(dict(virtual=virtual, callback=callback))

        if name is None:
            name = self._env('MIDO_DEFAULT_INPUT')

        return self.module.Input(name, **self._add_api(kwargs))

    def open_output(self, name=None, virtual=False, autoreset=False, **kwargs):
        """Open an output port.

        If the environment variable MIDO_DEFAULT_OUTPUT is set,
        it will override the default port.

        virtual=False
          Passing True opens a new port that other applications can
          connect to. Raises IOError if not supported by the backend.

        autoreset=False
          Automatically send all_notes_off and reset_all_controllers
          on all channels. This is the same as calling `port.reset()`.
        """
        kwargs.update(dict(virtual=virtual, autoreset=autoreset))

        if name is None:
            name = self._env('MIDO_DEFAULT_OUTPUT')

        return self.module.Output(name, **self._add_api(kwargs))

    def open_ioport(self, name=None, virtual=False,
                    callback=None, autoreset=False, **kwargs):
        """Open a port for input and output.

        If the environment variable MIDO_DEFAULT_IOPORT is set,
        it will override the default port.

        virtual=False
          Passing True opens a new port that other applications can
          connect to. Raises IOError if not supported by the backend.

        callback=None
          A callback function to be called when a new message arrives.
          The function should take one argument (the message).
          Raises IOError if not supported by the backend.

        autoreset=False
          Automatically send all_notes_off and reset_all_controllers
          on all channels. This is the same as calling `port.reset()`.
        """
        kwargs.update(dict(virtual=virtual, callback=callback,
                           autoreset=autoreset))

        if name is None:
            name = self._env('MIDO_DEFAULT_IOPORT') or None

        if hasattr(self.module, 'IOPort'):
            # Backend has a native IOPort. Use it.
            return self.module.IOPort(name, **self._add_api(kwargs))
        else:
            # Backend has no native IOPort. Use the IOPort wrapper
            # in midi.ports.
            #
            # We need an input and an output name.

            # MIDO_DEFAULT_IOPORT overrides the other two variables.
            if name:
                input_name = output_name = name
            else:
                input_name = self._env('MIDO_DEFAULT_INPUT')
                output_name = self._env('MIDO_DEFAULT_OUTPUT')

            kwargs = self._add_api(kwargs)

            return ports.IOPort(self.module.Input(input_name, **kwargs),
                                self.module.Output(output_name, **kwargs))

    def _get_devices(self, **kwargs):
        if hasattr(self.module, 'get_devices'):
            return self.module.get_devices(**self._add_api(kwargs))
        else:
            return []

    def get_input_names(self, **kwargs):
        """Return a list of all input port names."""
        devices = self._get_devices(**self._add_api(kwargs))
        names = [device['name'] for device in devices if device['is_input']]
        return names

    def get_output_names(self, **kwargs):
        """Return a list of all output port names."""
        devices = self._get_devices(**self._add_api(kwargs))
        names = [device['name'] for device in devices if device['is_output']]
        return names

    def get_ioport_names(self, **kwargs):
        """Return a list of all I/O port names."""
        devices = self._get_devices(**self._add_api(kwargs))
        inputs = [device['name'] for device in devices if device['is_input']]
        outputs = {
            device['name'] for device in devices if device['is_output']}
        return [name for name in inputs if name in outputs]

    def __repr__(self):
        if self.loaded:
            status = 'loaded'
        else:
            status = 'not loaded'

        if self.api:
            name = f'{self.name}/{self.api}'
        else:
            name = self.name

        return f'<backend {name} ({status})>'
