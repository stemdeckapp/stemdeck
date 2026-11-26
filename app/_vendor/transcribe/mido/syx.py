# SPDX-FileCopyrightText: 2014 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Read and write SYX file format
"""
import re

from .parser import Parser


def read_syx_file(filename):
    """Read sysex messages from SYX file.

    Returns a list of sysex messages.

    This handles both the text (hexadecimal) and binary
    formats. Messages other than sysex will be ignored. Raises
    ValueError if file is plain text and byte is not a 2-digit hex
    number.
    """
    with open(filename, 'rb') as infile:
        data = infile.read()

    if len(data) == 0:
        # Empty file.
        return []

    parser = Parser()

    if data[0] == 240:
        # Binary format.
        parser.feed(data)
    else:
        text = data.decode('latin1')
        data = bytearray.fromhex(re.sub(r'\s', ' ', text))
        parser.feed(data)

    return [msg for msg in parser if msg.type == 'sysex']


def write_syx_file(filename, messages, plaintext=False):
    """Write sysex messages to a SYX file.

    Messages other than sysex will be skipped.

    By default this will write the binary format.  Pass
    ``plaintext=True`` to write the plain text format (hex encoded
    ASCII text).
    """
    messages = [m for m in messages if m.type == 'sysex']

    if plaintext:
        with open(filename, 'w') as outfile:
            for message in messages:
                outfile.write(message.hex())
                outfile.write('\n')
    else:
        with open(filename, 'wb') as outfile:
            for message in messages:
                outfile.write(message.bin())
