"""One pinned model file downloaded in a process of its own.

Run as ``python -m app.core.model_fetch --url <https> --dst <path>
--sha256 <hex> --size <bytes>``. For a model the code that needs it runs in a
process that must not reach the network (the MIDI worker): the parent runs
this first, and can stop it at any moment, mid-read included, by stopping the
process, which a download in one of its own threads could not do while a
read waits on a quiet network.

The download is model_download.fetch_verified_file: redirects to https only,
each read given up after TIMEOUT_MODEL_DOWNLOAD_READ seconds, tried
MODEL_DOWNLOAD_ATTEMPTS times, kept only when its size and SHA-256 are the
pinned ones, flushed and synced under a temporary name and only then renamed
into place. A process stopped mid-file leaves only that ".partial", which the
next download into the folder removes once it is old enough to be no one's.

Protocol: the real stdout carries JSON lines only, as midi_worker's does:

    {"event": "progress", "pct": 0..100}     at most every half second,
                                             at least every ten while bytes come
    {"event": "done"}
    {"event": "error", "code": "download" | "disk" | "io"}

Each try that fails says so on stderr, which the parent keeps as a tail.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import time
from pathlib import Path

from app.core.process import arm_parent_watchdog

_proto = None
# The most often a progress line is written, and the longest it waits.
_PROGRESS_EVERY_SEC = 0.5
_HEARTBEAT_SEC = 10.0


def _claim_stdout() -> None:
    """Descriptor 1 for the protocol only (as midi_worker._claim_stdout)."""
    global _proto
    sys.stdout.flush()
    fd = os.dup(1)
    os.dup2(2, 1)
    _proto = os.fdopen(fd, "w", encoding="utf-8", newline="\n", buffering=1)
    sys.stdout = sys.stderr


def emit(event: str, **fields: object) -> None:
    out = _proto if _proto is not None else sys.__stdout__
    out.write(json.dumps({"event": event, **fields}, separators=(",", ":")) + "\n")
    out.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--dst", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    return parser


def fetch(url: str, dst: Path, sha256: str, size: int) -> int:
    from app.core.config import MODEL_DOWNLOAD_ATTEMPTS, TIMEOUT_MODEL_DOWNLOAD_READ
    from app.core.model_download import ModelDownloadError, fetch_verified_file

    last = [0.0, -1]

    def on_bytes(received: int) -> None:
        # A new percent at most every half second, and the same one again
        # every ten seconds on a slow line: the parent reads a quiet minute
        # as a stall, and a slow download is not one.
        pct = min(100, received * 100 // size)
        now = time.monotonic()
        quiet = now - last[0]
        if (pct != last[1] and quiet >= _PROGRESS_EVERY_SEC) or quiet >= _HEARTBEAT_SEC:
            last[0], last[1] = now, pct
            emit("progress", pct=pct)

    emit("progress", pct=0)
    try:
        fetch_verified_file(
            url,
            str(dst),
            sha256,
            size,
            read_timeout=TIMEOUT_MODEL_DOWNLOAD_READ,
            attempts=MODEL_DOWNLOAD_ATTEMPTS,
            progress=False,
            on_bytes=on_bytes,
        )
    except ModelDownloadError as exc:
        print(str(exc), file=sys.stderr)
        emit("error", code="download")
        return 3
    except OSError as exc:
        print(f"could not write the file: {exc}", file=sys.stderr)
        full = exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
        emit("error", code="disk" if full else "io")
        return 4
    emit("progress", pct=100)
    emit("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    _claim_stdout()
    # Hard exit when the server is gone: nobody would use the file.
    arm_parent_watchdog()
    args = _parser().parse_args(argv)
    return fetch(args.url, args.dst, args.sha256, args.size)


if __name__ == "__main__":
    sys.exit(main())
