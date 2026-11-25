# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Input and Output ports for PortMidi.

There is no need to use this module directly. All you need is
available in the toplevel module.

PortMidi documentation:
http://portmedia.sourceforge.net/portmidi/doxygen/
"""
import ctypes
import threading

from ..ports import BaseInput, BaseOutput, sleep
from . import portmidi_init as pm

_state = {'port_count': 0}


def _refresh_port_list():
    if _state['port_count'] == 0:
        # If no ports are open we can reboot PortMidi
        # to refresh the port list. This is a hack, but it's
        # the only way to get an up-to-date list.
        pm.lib.Pm_Terminate()
        pm.lib.Pm_Initialize()


def _check_error(return_value):
    """Raise IOError if return_value < 0.

    The exception will be raised with the error message from PortMidi.
    """
    if return_value == pm.pmHostError:
        raise OSError('PortMidi Host Error: '
                      '{}'.format(pm.get_host_error_message()))
    elif return_value < 0:
        raise OSError(pm.lib.Pm_GetErrorText(return_value))


def _get_device(device_id):
    info_pointer = pm.lib.Pm_GetDeviceInfo(device_id)
    if not info_pointer:
        raise OSError('PortMidi device with id={} not found'.format(
            device_id))
    info = info_pointer.contents

    return {
        'id': device_id,
        'interface': info.interface.decode('utf-8'),
        'name': info.name.decode('utf-8'),
        'is_input': info.is_input,
        'is_output': info.is_output,
        'opened': bool(info.opened),
    }


def _get_default_device(get_input):
    if get_input:
        device_id = pm.lib.Pm_GetDefaultInputDeviceID()
    else:
        device_id = pm.lib.Pm_GetDefaultOutputDeviceID()

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
            if not device['is_input']:
                continue
        else:
            if not device['is_output']:
                continue

        if device['opened']:
            raise OSError(f'port already opened: {name!r}')

        return device
    else:
        raise OSError(f'unknown port {name!r}')


def get_devices(**kwargs):
    """Return a list of devices as dictionaries."""
    _refresh_port_list()
    return [_get_device(i) for i in range(pm.lib.Pm_CountDevices())]


class PortCommon:
    """
    Mixin with common things for input and output ports.
    """
    def _open(self, **kwargs):
        _refresh_port_list()

        if kwargs.get('virtual'):
            raise ValueError('virtual ports are not supported'
                             ' by the PortMidi backend')

        self._stream = pm.PortMidiStreamPtr()

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
            _check_error(pm.lib.Pm_OpenInput(
                         ctypes.byref(self._stream),
                         device['id'],  # Input device
                         pm.null,       # Input driver info
                         1000,          # Buffer size
                         pm.NullTimeProcPtr,  # Time callback
                         pm.null))      # Time info
        else:
            _check_error(pm.lib.Pm_OpenOutput(
                         ctypes.byref(self._stream),
                         device['id'],  # Output device
                         pm.null,       # Output diver info
                         0,             # Buffer size
                                        # (ignored when latency == 0?)
                         pm.NullTimeProcPtr,  # Default to internal clock
                         pm.null,       # Time info
                         0))            # Latency

        # This is set when we return, but the callback thread needs
        # it to be False now (or it will just return right away.)
        self.closed = False
        _state['port_count'] += 1

        if self.is_input:
            self._thread = None
            self.callback = kwargs.get('callback')

        self._device_type = 'PortMidi/{}'.format(device['interface'])

    def _close(self):
        self.callback = None
        _check_error(pm.lib.Pm_Close(self._stream))
        _state['port_count'] -= 1


class Input(PortCommon, BaseInput):
    """
    PortMidi Input port
    """
    def _receive(self, block=True):
        # Since there is no blocking read in PortMidi, the block
        # flag is ignored and the enclosing receive() takes care
        # of blocking.

        # Allocate buffer.
        # I get hanging notes if MAX_EVENTS > 1, so I'll have to
        # resort to calling Pm_Read() in a loop until there are no
        # more pending events.
        max_events = 1
        BufferType = pm.PmEvent * max_events
        read_buffer = BufferType()

        # Read available data from the stream and feed it to the parser.
        while pm.lib.Pm_Poll(self._stream):
            # TODO: this should be allocated once
            # Read one message. Should return 1.
            # If num_events < 0, an error occured.
            length = 1  # Buffer length
            num_events = pm.lib.Pm_Read(self._stream, read_buffer, length)
            _check_error(num_events)

            # Get the event
            event = read_buffer[0]
            # print('Received: {:x}'.format(event.message))

            # The bytes of the message are stored like this:
            #    0x00201090 -> (0x90, 0x10, 0x10)
            # (TODO: not sure if this is correct.)
            packed_message = event.message & 0xffffffff

            for _i in range(4):
                byte = packed_message & 0xff
                self._parser.feed_byte(byte)
                packed_message >>= 8

    @property
    def callback(self):
        return self._callback

    @callback.setter
    def callback(self, func):
        self._callback = func
        if func is None:
            self._stop_thread()
        else:
            self._start_thread()

    def _start_thread(self):
        """Start callback thread if not already running."""
        if not self._thread:
            self._stop_event = None
            self._thread = threading.Thread(
                target=self._thread_main)
            self._thread.daemon = True
            self._thread.start()

    def _stop_thread(self):
        """Stop callback thread if running."""
        if self._thread:
            # Ask callback thread to stop.
            self._stop_event = threading.Event()
            self._stop_event.wait()
            self._thread = None

    def _thread_main(self):
        # TODO: exceptions do not propagate to the main thread, so if
        # something goes wrong here there is no way to detect it, and
        # there is no warning. (An unknown variable, for example, will
        # just make the thread stop silently.)

        # Receive messages from port until it's closed
        # or some exception occurs.
        try:
            while not self._stop_event:
                self._receive()
                for message in self._parser:
                    if self.callback:
                        self.callback(message)
                sleep()
        finally:
            # Inform parent thread that we are done.
            if self._stop_event:
                self._stop_event.set()

    def _close(self):
        self._stop_thread()
        PortCommon._close(self)


class Output(PortCommon, BaseOutput):
    """
    PortMidi output port
    """

    def _send(self, message):
        if message.type == 'sysex':
            # Sysex messages are written as a string.
            string = ctypes.c_char_p(bytes(message.bin()))
            timestamp = 0  # Ignored when latency = 0
            _check_error(pm.lib.Pm_WriteSysEx(self._stream, timestamp, string))
        else:
            # The bytes of a message as packed into a 32 bit integer.
            packed_message = 0
            for byte in reversed(message.bytes()):
                packed_message <<= 8
                packed_message |= byte

            timestamp = 0  # Ignored when latency = 0
            _check_error(pm.lib.Pm_WriteShort(self._stream,
                                              timestamp,
                                              packed_message))
