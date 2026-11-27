# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
MIDI Parser

There is no need to use this module directly. All you need is
available in the top level module.
"""
from collections import deque

from .messages import Message
from .tokenizer import Tokenizer


class Parser:
    """
    MIDI byte stream parser

    Parses a stream of MIDI bytes and produces messages.

    Data can be put into the parser in the form of
    integers, byte arrays or byte strings.
    """
    def __init__(self, data=None):
        # For historical reasons self.messages is public and must be a
        # deque(). (It is referenced directly inside ports.)
        self.messages = deque()
        self._tok = Tokenizer()
        if data:
            self.feed(data)

    def _decode(self):
        for midi_bytes in self._tok:
            self.messages.append(Message.from_bytes(midi_bytes))

    def feed(self, data):
        """Feed MIDI data to the parser.

        Accepts any object that produces a sequence of integers in
        range 0..255, such as:

            [0, 1, 2]
            (0, 1, 2)
            [for i in range(256)]
            (for i in range(256)]
            bytearray()
        """
        self._tok.feed(data)
        self._decode()

    def feed_byte(self, byte):
        """Feed one MIDI byte into the parser.

        The byte must be an integer in range 0..255.
        """
        self._tok.feed_byte(byte)
        self._decode()

    def get_message(self):
        """Get the first parsed message.

        Returns None if there is no message yet. If you don't want to
        deal with None, you can use pending() to see how many messages
        you can get before you get None, or just iterate over the
        parser.
        """
        for msg in self:
            return msg
        else:
            return None

    def pending(self):
        """Return the number of pending messages."""
        return len(self.messages)

    __len__ = pending

    def __iter__(self):
        """Yield messages that have been parsed so far."""
        while len(self.messages) > 0:
            yield self.messages.popleft()


def parse_all(data):
    """Parse MIDI data and return a list of all messages found.

    This is typically used to parse a little bit of data with a few
    messages in it. It's best to use a Parser object for larger
    amounts of data. Also, tt's often easier to use parse() if you
    know there is only one message in the data.
    """
    return list(Parser(data))


def parse(data):
    """ Parse MIDI data and return the first message found.

    Data after the first message is ignored. Use parse_all()
    to parse more than one message.
    """
    return Parser(data).get_message()
