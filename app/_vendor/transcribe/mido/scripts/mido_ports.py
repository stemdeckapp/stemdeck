#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2013 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

"""
List available PortMidi ports.
"""

import os

import mido


def print_ports(heading, port_names):
    print(heading)
    for name in port_names:
        print(f"    '{name}'")
    print()


def main():
    print()
    print_ports('Available input Ports:', mido.get_input_names())
    print_ports('Available output Ports:', mido.get_output_names())

    for name in ['MIDO_DEFAULT_INPUT',
                 'MIDO_DEFAULT_OUTPUT',
                 'MIDO_DEFAULT_IOPORT',
                 'MIDO_BACKEND']:
        try:
            value = os.environ[name]
            print(f'{name}={value!r}')
        except LookupError:
            print(f'{name} not set.')
    print()
    print(f'Using backend {mido.backend.name}.')
    print()


if __name__ == '__main__':
    main()
