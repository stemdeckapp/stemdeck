"""Make YourMT3+'s weights-only file from the checkpoint its authors published.

The app does this itself on the first ymt3 run (app/pipeline/midi.py: the
download, then app/pipeline/ymt3_convert.py). This script is the same step by
hand, to check that the conversion is reproducible and to record what it
gives: the checkpoint's size and SHA-256 against the pinned ones
(config.YMT3_CHECKPOINT_*), the conversion (ymt3.convert_checkpoint: torch's
weights-only loader, the model's tensors only, sorted, written as safetensors
with a fixed header), then the result's size and SHA-256. The same checkpoint
gives the same bytes on every computer with the same torch, so the result's
SHA-256 is pinned too (config.YMT3_WEIGHTS_*), and a run refuses any other.

    uv run python scripts/ymt3_weights.py --ckpt last.ckpt --out weights.safetensors
    uv run python scripts/ymt3_weights.py --download --ckpt last.ckpt --out ...

``--download`` fetches the pinned checkpoint first (the address names the
Hugging Face revision, so its bytes cannot change), with the app's own
verified download (model_download.fetch_verified_file). The script prints
both files' sizes and SHA-256, whether each matches its pin, and exits 1 when
one does not. Nothing is written anywhere but ``--out`` (and ``--ckpt`` with
``--download``).

Measured 2026-11-26 (torch 2.6.0, Windows, CPU and CUDA builds alike):
655 tensors, 188 412 704 bytes, SHA-256
dbe76ceca4a7edddbe3e05468861567d67a257e50c8dd9824273efc0f198f292, in about
4 s; read by the safetensors package 0.8.0 too.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ckpt", type=Path, required=True, help="the published last.ckpt")
    parser.add_argument("--out", type=Path, required=True, help="the weights file to write")
    parser.add_argument("--download", action="store_true", help="fetch the checkpoint first")
    args = parser.parse_args(argv)

    from app.core.config import (
        YMT3_CHECKPOINT_SHA256,
        YMT3_CHECKPOINT_SIZE,
        YMT3_CHECKPOINT_URL,
        YMT3_WEIGHTS_SHA256,
        YMT3_WEIGHTS_SIZE,
    )
    from app.core.pinned_file import sha256_of
    from app.pipeline import ymt3

    if args.download:
        from app.core.model_download import fetch_verified_file

        args.ckpt.parent.mkdir(parents=True, exist_ok=True)
        fetch_verified_file(
            YMT3_CHECKPOINT_URL, str(args.ckpt), YMT3_CHECKPOINT_SHA256, YMT3_CHECKPOINT_SIZE
        )
    ckpt_size = args.ckpt.stat().st_size
    ckpt_sha = sha256_of(args.ckpt)
    ckpt_ok = ckpt_size == YMT3_CHECKPOINT_SIZE and ckpt_sha == YMT3_CHECKPOINT_SHA256
    print(f"checkpoint {args.ckpt}: {ckpt_size} bytes, sha256 {ckpt_sha}, pinned: {ckpt_ok}")
    if not ckpt_ok:
        print("not the pinned checkpoint: not converted", file=sys.stderr)
        return 1
    started = time.monotonic()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    count = ymt3.convert_checkpoint(args.ckpt, args.out)
    took = time.monotonic() - started
    size = args.out.stat().st_size
    sha = sha256_of(args.out)
    ok = size == YMT3_WEIGHTS_SIZE and sha == YMT3_WEIGHTS_SHA256
    print(f"weights {args.out}: {count} tensors, {size} bytes, sha256 {sha}, in {took:.1f} s")
    print(f"matches the pinned weights (config.YMT3_WEIGHTS_*): {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
