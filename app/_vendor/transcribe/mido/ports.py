# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Useful tools for working with ports
"""
import random
import threading
import time

from .messages import Message
from .parser import Parser

# How many seconds to sleep before polling again.
_DEFAULT_SLEEP_TIME = 0.001
_sleep_time = _DEFAULT_SLEEP_TIME


# TODO: document this more.
def sleep():
    """Sleep for N seconds.

    This is used in ports when polling and waiting for messages. N can
    be set with set_sleep_time()."""
    time.sleep(_sleep_time)


def set_sleep_time(seconds=_DEFAULT_SLEEP_TIME):
    """Set the number of seconds sleep() will sleep."""
    global _sleep_time
    _sleep_time = seconds


def get_sleep_time():
    """Get number of seconds sleep() will sleep."""
    return _sleep_time


def reset_messages():
    """Yield "All Notes Off" and "Reset All Controllers" for all channels"""
    ALL_NOTES_OFF = 123
    RESET_ALL_CONTROLLERS = 121
    for channel in range(16):
        for control in [ALL_NOTES_OFF, RESET_ALL_CONTROLLERS]:
            yield Message('control_change', channel=channel, control=control)


def panic_messages():
    """Yield "All Sounds Off" for all channels.

    This will mute all sounding notes regardless of
    envelopes. Useful when notes are hanging and nothing else
    helps.
    """
    ALL_SOUNDS_OFF = 120
    for channel in range(16):
        yield Message('control_change',
                      channel=channel, control=ALL_SOUNDS_OFF)


class DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class BasePort:
    """
    Abstract base class for Input and Output ports.
    """
    is_input = False
    is_output = False
    _locking = True

    def __init__(self, name=None, **kwargs):
        if hasattr(self, 'closed'):
            # __init__() called twice (from BaseInput and BaseOutput).
            # This stops _open() from being called twice.
            return

        self.name = name
        if self._locking:
            self._lock = threading.RLock()
        else:
            self._lock = DummyLock()
        self.closed = True
        self._open(**kwargs)
        self.closed = False

    def _open(self, **kwargs):
        pass

    def _close(self):
        pass

    def close(self):
        """Close the port.

        If the port is already closed, nothing will happen.  The port
        is automatically closed when the object goes out of scope or
        is garbage collected.
        """
        with self._lock:
            if not self.closed:
                if hasattr(self, 'autoreset') and self.autoreset:
                    try:
                        self.reset()
                    except OSError:
                        pass

                self._close()
                self.closed = True

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()
        return False

    def __repr__(self):
        if self.closed:
            state = 'closed'
        else:
            state = 'open'

        capabilities = self.is_input, self.is_output
        port_type = {(True, False): 'input',
                     (False, True): 'output',
                     (True, True): 'I/O port',
                     (False, False): 'mute port',
                     }[capabilities]

        name = self.name or ''

        try:
            device_type = self._device_type
        except AttributeError:
            device_type = self.__class__.__name__

        return '<{} {} {!r} ({})>'.format(
            state, port_type, name, device_type)


class BaseInput(BasePort):
    """Base class for input port.

    Subclass and override _receive() to create a new input port type.
    (See portmidi.py for an example of how to do this.)
    """
    is_input = True

    def __init__(self, name='', **kwargs):
        """Create an input port.

        name is the port name, as returned by input_names(). If
        name is not passed, the default input is used instead.
        """
        BasePort.__init__(self, name, **kwargs)
        self._parser = Parser()
        self._messages = self._parser.messages  # Shortcut.

    def _check_callback(self):
        if hasattr(self, 'callback') and self.callback is not None:
            raise ValueError('a callback is set for this port')

    def _receive(self, block=True):
        pass

    def iter_pending(self):
        """Iterate through pending messages."""
        while True:
            msg = self.poll()
            if msg is None:
                return
            else:
                yield msg

    def receive(self, block=True):
        """Return the next message.

        This will block until a message arrives.

        If you pass block=False it will not block and instead return
        None if there is no available message.

        If the port is closed and there are no pending messages IOError
        will be raised. If the port closes while waiting inside receive(),
        IOError will be raised. TODO: this seems a bit inconsistent. Should
        different errors be raised? What's most useful here?
        """
        if not self.is_input:
            raise ValueError('Not an input port')

        self._check_callback()

        # If there is a message pending, return it right away.
        with self._lock:
            if self._messages:
                return self._messages.popleft()

        if self.closed:
            if block:
                raise ValueError('receive() called on closed port')
            else:
                return None

        while True:
            with self._lock:
                msg = self._receive(block=block)
                if msg:
                    return msg

                if self._messages:
                    return self._messages.popleft()
                elif not block:
                    return None
                elif self.closed:
                    raise OSError('port closed during receive()')

            sleep()

    def poll(self):
        """Receive the next pending message or None

        This is the same as calling `receive(block=False)`."""
        return self.receive(block=False)

    def __iter__(self):
        """Iterate through messages until the port closes."""
        # This could have simply called receive() in a loop, but that
        # could result in a "port closed during receive()" error which
        # is hard to catch here.
        self._check_callback()
        while True:
            try:
                yield self.receive()
            except OSError:
                if self.closed:
                    # The port closed before or inside receive().
                    # (This makes the assumption that this is the reason,
                    # at the risk of masking other errors.)
                    return
                else:
                    raise


class BaseOutput(BasePort):
    """
    Base class for output port.

    Subclass and override _send() to create a new port type.  (See
    portmidi.py for how to do this.)
    """
    is_output = True

    def __init__(self, name='', autoreset=False, **kwargs):
        """Create an output port

        name is the port name, as returned by output_names(). If
        name is not passed, the default output is used instead.
        """
        BasePort.__init__(self, name, **kwargs)
        self.autoreset = autoreset

    def _send(self, msg):
        pass

    def send(self, msg):
        """Send a message on the port.

        A copy of the message will be sent, so you can safely modify
        the original message without any unexpected consequences.
        """
        if not self.is_output:
            raise ValueError('Not an output port')
        elif not isinstance(msg, Message):
            raise TypeError('argument to send() must be a Message')
        elif self.closed:
            raise ValueError('send() called on closed port')

        with self._lock:
            self._send(msg.copy())

    def reset(self):
        """Send "All Notes Off" and "Reset All Controllers" on all channels"""
        if self.closed:
            return

        for msg in reset_messages():
            self.send(msg)

    def panic(self):
        """Send "All Sounds Off" on all channels.

        This will mute all sounding notes regardless of
        envelopes. Useful when notes are hanging and nothing else
        helps.
        """
        if self.closed:
            return

        for msg in panic_messages():
            self.send(msg)


class BaseIOPort(BaseInput, BaseOutput):
    def __init__(self, name='', **kwargs):
        """Create an IO port.

        name is the port name, as returned by ioport_names().
        """
        BaseInput.__init__(self, name, **kwargs)
        BaseOutput.__init__(self, name, **kwargs)


class IOPort(BaseIOPort):
    """Input / output port.

    This is a convenient wrapper around an input port and an output
    port which provides the functionality of both. Every method call
    is forwarded to the appropriate port.
    """

    _locking = False

    def __init__(self, input, output):
        self.input = input
        self.output = output

        # We use str() here in case name is None.
        self.name = f'{str(input.name)} + {str(output.name)}'
        self._messages = self.input._messages
        self.closed = False
        self._lock = DummyLock()

    def _close(self):
        self.input.close()
        self.output.close()

    def _send(self, message):
        self.output.send(message)

    def _receive(self, block=True):
        return self.input.receive(block=block)


class EchoPort(BaseIOPort):
    def _send(self, message):
        self._messages.append(message)

    __iter__ = BaseIOPort.iter_pending


class MultiPort(BaseIOPort):
    def __init__(self, ports, yield_ports=False):
        BaseIOPort.__init__(self, 'multi')
        self.ports = list(ports)
        self.yield_ports = yield_ports

    def _send(self, message):
        for port in self.ports:
            if not port.closed:
                # TODO: what if a SocketPort connection closes in-between here?
                port.send(message)

    def _receive(self, block=True):
        self._messages.extend(multi_receive(self.ports,
                                            yield_ports=self.yield_ports,
                                            block=block))


def multi_receive(ports, yield_ports=False, block=True):
    """Receive messages from multiple ports.

    Generates messages from ever input port. The ports are polled in
    random order for fairness, and all messages from each port are
    yielded before moving on to the next port.

    If yield_ports=True, (port, message) is yielded instead of just
    the message.

    If block=False only pending messages will be yielded.
    """
    ports = list(ports)
    while True:
        # Make a shuffled copy of the port list.
        random.shuffle(ports)

        for port in ports:
            if not port.closed:
                for message in port.iter_pending():
                    if yield_ports:
                        yield port, message
                    else:
                        yield message

        if block:
            sleep()
        else:
            break


def multi_iter_pending(ports, yield_ports=False):
    """Iterate through all pending messages in ports.

    This is the same as calling multi_receive(ports, block=False).
    The function is kept around for backwards compatability.
    """
    return multi_receive(ports, yield_ports=yield_ports, block=False)


def multi_send(ports, msg):
    """Send message on all ports."""
    for port in ports:
        port.send(msg)
