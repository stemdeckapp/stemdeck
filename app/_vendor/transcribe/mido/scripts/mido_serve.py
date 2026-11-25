#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Serve one or more output ports. Every message received on any of the
connected sockets will be sent to every output port.
"""
import argparse

import mido
from mido import sockets
from mido.ports import MultiPort


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    arg = parser.add_argument

    arg('address',
        metavar='ADDRESS',
        help='host:port to serve on')

    arg('ports',
        metavar='PORT',
        nargs='+',
        help='output port to serve')

    return parser.parse_args()


def main():
    args = parse_args()

    try:
        out = MultiPort([mido.open_output(name) for name in args.ports])

        (hostname, port) = sockets.parse_address(args.address)
        with sockets.PortServer(hostname, port) as server:
            for message in server:
                print(f'Received {message}')
                out.send(message)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
