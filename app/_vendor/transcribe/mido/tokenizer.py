# SPDX-FileCopyrightText: 2017 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

from collections import deque
from numbers import Integral

from .messages.specs import SPEC_BY_STATUS, SYSEX_END, SYSEX_START


class Tokenizer:
    """
    Splits a MIDI byte stream into messages.
    """
    def __init__(self, data=None):
        """Create a new decoder."""

        self._status = 0
        self._bytes = []
        self._messages = deque()
        self._datalen = 0

        if data is not None:
            self.feed(data)

    def _feed_status_byte(self, status):
        if status == SYSEX_END:
            if self._status == SYSEX_START:
                self._bytes.append(SYSEX_END)
                self._messages.append(self._bytes)

            self._status = 0

        elif 0xf8 <= status <= 0xff:
            if self._status != SYSEX_START:
                # Realtime messages are only allowed inside sysex
                # messages. Reset parser.
                self._status = 0

            if status in SPEC_BY_STATUS:
                self._messages.append([status])

        elif status in SPEC_BY_STATUS:
            # New message.
            spec = SPEC_BY_STATUS[status]

            if spec['length'] == 1:
                self._messages.append([status])
                self._status = 0
            else:
                self._status = status
                self._bytes = [status]
                self._len = spec['length']
        else:
            # Undefined message. Reset parser.
            # (Undefined realtime messages are handled above.)
            # self._status = 0
            pass

    def _feed_data_byte(self, byte):
        if self._status:
            self._bytes.append(byte)
            if len(self._bytes) == self._len:
                # Complete message.
                self._messages.append(self._bytes)
                self._status = 0
        else:
            # Ignore stray data byte.
            pass

    def feed_byte(self, byte):
        """Feed MIDI byte to the decoder.

        Takes an int in range [0..255].
        """
        if not isinstance(byte, Integral):
            raise TypeError('message byte must be integer')

        if 0 <= byte <= 255:
            if byte <= 127:
                return self._feed_data_byte(byte)
            else:
                return self._feed_status_byte(byte)
        else:
            raise ValueError(f'invalid byte value {byte!r}')

    def feed(self, data):
        """Feed MIDI bytes to the decoder.

        Takes an iterable of ints in in range [0..255].
        """
        for byte in data:
            self.feed_byte(byte)

    def __len__(self):
        return len(self._messages)

    def __iter__(self):
        """Yield messages that have been parsed so far."""
        while len(self._messages):
            yield self._messages.popleft()
