#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
Forward all messages from one or more ports to server.
"""
import argparse

import mido


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    arg = parser.add_argument

    arg('address',
        metavar='ADDRESS',
        help='host:port to connect to')

    arg('ports',
        metavar='PORT',
        nargs='+',
        help='input ports to listen to')

    return parser.parse_args()


def main():
    args = parse_args()

    try:
        hostname, port = mido.sockets.parse_address(args.address)
        ports = [mido.open_input(name) for name in args.ports]

        with mido.sockets.connect(hostname, port) as server_port:
            print('Connected.')
            for message in mido.ports.multi_receive(ports):
                print(f'Sending {message}')
                server_port.send(message)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
