# SPDX-FileCopyrightText: 2017 Ole Martin Bjorndalen <ombdalen@gmail.com>
#
# SPDX-License-Identifier: MIT

import queue
from threading import RLock

from ..parser import Parser


class ParserQueue:
    """
    Thread safe message queue with built in MIDI parser.

    This should be avaiable to other backend implementations and perhaps
    also in the public API, but the API needs a bit of review. (Ideally This
    would replace the parser.)

    q = ParserQueue()

    q.put(msg)
    q.put_bytes([0xf8, 0, 0])

    msg = q.get()
    msg = q.poll()
    """
    def __init__(self):
        self._queue = queue.Queue()
        self._parser = Parser()
        self._parser_lock = RLock()

    def put(self, msg):
        self._queue.put(msg)

    def put_bytes(self, msg_bytes):
        with self._parser_lock:
            self._parser.feed(msg_bytes)
            for msg in self._parser:
                self.put(msg)

    # TODO: add timeout?
    def get(self):
        return self._queue.get()

    def poll(self):
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def __iter__(self):
        while True:
            return self.get()

    def iterpoll(self):
        while True:
            msg = self.poll()
            if msg is None:
                return
            else:
                yield msg
