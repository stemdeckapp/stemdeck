"""YourMT3+'s checkpoint made into its weights-only file, in a process of its own.

Run as ``python -m app.pipeline.ymt3_convert --ckpt <last.ckpt> --out <file>
--sha256 <hex> --size <bytes>``, by the parent of a MIDI run after the
checkpoint was downloaded and checked (app/pipeline/midi.py), and by
scripts/ymt3_weights.py by hand. Its own process so that the parent can stop
it at once and never holds the checkpoint's 561 MB in its own memory.

The file is ymt3.convert_checkpoint's: the checkpoint read with torch's
weights-only loader, only its model tensors kept, written sorted as a
safetensors file under a temporary name in the destination's folder, flushed
and synced. It takes its name only when its size and SHA-256 are the pinned
ones (config.YMT3_WEIGHTS_*): the conversion is deterministic, so any other
bytes mean another checkpoint or another torch, and are removed, never used.

No network: the conversion needs none, and this process refuses it, as the
MIDI worker does.

Protocol: the real stdout carries JSON lines only, as midi_worker's does:

    {"event": "progress", "pct": 0..100}
    {"event": "done", "tensors": n}
    {"event": "error", "code": "convert" | "disk" | "io"}

"convert": the checkpoint could not be read as this model's, or what it
made is not the pinned file.
"""

from __future__ import annotations

import argparse
import errno
import os
import sys
import uuid
from pathlib import Path

from app.core.process import arm_parent_watchdog
from app.pipeline import midi_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ckpt", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--size", type=int, required=True)
    return parser


def _write_code(exc: OSError) -> str:
    return "disk" if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else "io"


def convert(ckpt: Path, out: Path, sha256: str, size: int, emit=midi_worker.emit) -> int:
    """Convert, check, and put the file in place. Returns the exit code."""
    from app.core.pinned_file import sha256_of
    from app.core.settings import fsync_dir
    from app.pipeline import ymt3

    emit("progress", pct=0)
    temp = out.parent / f".{out.name}.{uuid.uuid4().hex}.tmp"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            count = ymt3.convert_checkpoint(ckpt, temp)
        except OSError as exc:
            print(f"could not write the weights: {exc}", file=sys.stderr)
            emit("error", code=_write_code(exc))
            return 4
        except Exception as exc:  # noqa: BLE001 -- a file that is not this model's
            print(f"conversion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            emit("error", code="convert")
            return 3
        emit("progress", pct=90)
        got = temp.stat().st_size
        digest = sha256_of(temp)
        if got != size or digest != sha256:
            print(
                f"the weights made are not the pinned file: {got} bytes, sha256 {digest}",
                file=sys.stderr,
            )
            emit("error", code="convert")
            return 3
        os.replace(temp, out)
        fsync_dir(out.parent)
    except OSError as exc:
        print(f"could not put the weights in place: {exc}", file=sys.stderr)
        emit("error", code=_write_code(exc))
        return 4
    finally:
        temp.unlink(missing_ok=True)
    emit("progress", pct=100)
    emit("done", tensors=count)
    return 0


def main(argv: list[str] | None = None) -> int:
    midi_worker._claim_stdout()
    midi_worker._block_network()
    # Hard exit when the server is gone: nobody would use the file.
    arm_parent_watchdog()
    args = _parser().parse_args(argv)
    if not args.ckpt.is_file():
        midi_worker.emit("error", code="convert")
        return 3
    return convert(args.ckpt, args.out, args.sha256, args.size)


if __name__ == "__main__":
    sys.exit(main())
