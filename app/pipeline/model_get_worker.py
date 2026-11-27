"""One built-in model downloaded for Settings > Models, in a process of its own.

Run as ``python -m app.pipeline.model_get_worker --model <id> --token <hex>``
by app/core/model_get.py, which can stop it at any moment. The model's plan
(model_get.plan_for) says which files, from where, checked against what, and
which family's download fetches each; the module docstring there has the
list and the rules this follows:

- a file that is already its pinned file is left alone;
- every other one is fetched into the run's own folder beside its
  destination (model_get.staging_dir), verified on the way in, flushed and
  synced, and only then renamed into place, then that folder synced;
- YourMT3+'s checkpoint is then made into its weights file by
  ymt3_convert.convert, into the run's folder too, renamed into place, and
  the checkpoint removed.

The free space is checked first, per disk, for what is still to fetch.

Protocol: the real stdout carries JSON lines only (model_fetch's):

    {"event": "progress", "bytes": n, "phase": "download" | "prepare"}
        at most every half second while bytes come, and after each file
    {"event": "done"}
    {"event": "error", "code": "network" | "disk" | "checksum" | "io"
                                | "prepare" | "failed"}

What went wrong, in full, goes to stderr, which the parent keeps as a tail
for its log; the page only ever sees the code.
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from app.core import model_get
from app.core.model_get import Plan, PlanFile
from app.core.process import arm_parent_watchdog

# The most often a progress line is written while bytes come. The parent
# reads a quiet spell as a stall (config.TIMEOUT_MODEL_GET_STALL), and a
# read that waits longer than TIMEOUT_MODEL_DOWNLOAD_READ is given up here.
_PROGRESS_EVERY_SEC = 0.5
# Room kept free beyond the files themselves, so a download never fills a
# disk to its last byte.
_SPARE_BYTES = 64 * 1024 * 1024

Emit = Callable[..., None]


class Failed(Exception):
    """The run ends with this code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _write_code(exc: OSError) -> str:
    return "disk" if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else "io"


def _existing(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _check_space(needs: dict[Path, int]) -> None:
    """Raise Failed("disk") when a disk cannot hold what goes to it."""
    by_disk: dict[int, tuple[Path, int]] = {}
    for folder, size in needs.items():
        root = _existing(folder)
        try:
            dev = root.stat().st_dev
        except OSError:
            continue
        seen = by_disk.get(dev)
        by_disk[dev] = (root, size + (seen[1] if seen else 0))
    for root, size in by_disk.values():
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            continue
        if free < size + _SPARE_BYTES:
            print(f"not enough free space: {size} bytes needed, {free} free", file=sys.stderr)
            raise Failed("disk")


def _fetch(f: PlanFile, dst: Path, on_bytes: Callable[[int], None]) -> None:
    """``f`` downloaded to ``dst``, verified, through its family's download."""
    from app.core.config import MODEL_DOWNLOAD_ATTEMPTS, TIMEOUT_MODEL_DOWNLOAD_READ
    from app.core.model_download import fetch_pinned_hub_file, fetch_verified_file

    timeout, attempts = TIMEOUT_MODEL_DOWNLOAD_READ, MODEL_DOWNLOAD_ATTEMPTS
    if f.via == "hub":
        if not fetch_pinned_hub_file(
            f.url,
            str(dst),
            read_timeout=timeout,
            attempts=attempts,
            progress=False,
            on_bytes=on_bytes,
        ):
            print(f"not a pinned torch.hub file: {f.url}", file=sys.stderr)
            raise Failed("failed")
        return
    if f.via == "kim":
        from app.core.stem_models import KIM_CHECKPOINT
        from app.pipeline import mel_roformer

        if (f.url, f.sha256, f.size) != (
            KIM_CHECKPOINT.url,
            KIM_CHECKPOINT.sha256,
            KIM_CHECKPOINT.size,
        ):
            raise Failed("failed")
        mel_roformer.ensure_checkpoint(
            read_timeout=timeout, attempts=attempts, path=dst, on_bytes=on_bytes
        )
        return
    fetch_verified_file(
        f.url,
        str(dst),
        f.sha256,
        f.size,
        read_timeout=timeout,
        attempts=attempts,
        progress=False,
        on_bytes=on_bytes,
    )


def _place(src: Path, dest: Path) -> None:
    """Rename a verified file from the run's folder into place, durably."""
    from app.core.settings import fsync_dir

    os.replace(src, dest)
    fsync_dir(dest.parent)


def _drop_empty(stage: Path) -> None:
    """The run's folder once its file has moved out. One that is not empty
    (another file of the run is still to land there) stays, and the parent
    removes it whatever happens."""
    try:
        stage.rmdir()
    except OSError:
        pass


def _convert(plan: Plan, token: str, emit: Emit, done_bytes: int) -> None:
    """YourMT3+'s weights from its checkpoint (ymt3_convert.convert, as a
    MIDI run's prepare), placed, then the checkpoint removed."""
    from app.core.config import YMT3_WEIGHTS_SHA256, YMT3_WEIGHTS_SIZE
    from app.pipeline import ymt3_convert

    assert plan.convert_to is not None
    checkpoint = plan.files[0].dest
    stage = model_get.staging_dir(plan.convert_to.parent, token)
    stage.mkdir(parents=True, exist_ok=True)
    out = stage / plan.convert_to.name
    emit("progress", bytes=done_bytes, phase="prepare")
    said: list[str] = []

    def relay(event: str, **fields: object) -> None:
        # convert() speaks model_fetch's protocol; only its error is kept.
        if event == "error":
            said.append(str(fields.get("code")))

    rc = ymt3_convert.convert(checkpoint, out, YMT3_WEIGHTS_SHA256, YMT3_WEIGHTS_SIZE, emit=relay)
    if rc != 0 or not out.is_file():
        code = said[-1] if said else "convert"
        raise Failed(code if code in ("disk", "io") else "prepare")
    _place(out, plan.convert_to)
    _drop_empty(stage)
    try:
        checkpoint.unlink(missing_ok=True)
    except OSError as exc:
        # The weights are all a run needs; a checkpoint left is only space.
        print(f"could not remove the checkpoint: {exc}", file=sys.stderr)


def run(plan: Plan, token: str, emit: Emit) -> int:
    """The download, to its end. Returns the exit code; the last line said
    is "done" or "error"."""
    from app.core.model_download import CHECKSUM_REASON, ModelDownloadError, file_matches

    try:
        model_get.sweep_stale_staging(plan)
        if plan.convert_to is not None:
            from app.pipeline import ymt3

            if ymt3.weights_ok(plan.convert_to):
                emit("progress", bytes=plan.total, phase="download")
                emit("done")
                return 0
        need = [f for f in plan.files if not file_matches(f.dest, f.sha256, f.size)]
        done_bytes = plan.total - sum(f.size for f in need)
        emit("progress", bytes=done_bytes, phase="download")
        needs: dict[Path, int] = {}
        for f in need:
            needs[f.dest.parent] = needs.get(f.dest.parent, 0) + f.size
        if plan.convert_to is not None:
            from app.core.config import YMT3_WEIGHTS_SIZE

            folder = plan.convert_to.parent
            needs[folder] = needs.get(folder, 0) + YMT3_WEIGHTS_SIZE
        _check_space(needs)

        from app.core.config import MODEL_DOWNLOAD_ATTEMPTS, TIMEOUT_MODEL_DOWNLOAD_READ
        from app.core.model_download import bounded_downloads

        # The socket default timeout every read here inherits, as a job's.
        with bounded_downloads(TIMEOUT_MODEL_DOWNLOAD_READ, MODEL_DOWNLOAD_ATTEMPTS):
            for f in need:
                stage = model_get.staging_dir(f.dest.parent, token)
                stage.mkdir(parents=True, exist_ok=True)
                tmp = stage / f.dest.name
                last = [0.0]

                def on_bytes(received: int, base: int = done_bytes, last: list = last) -> None:
                    now = time.monotonic()
                    if now - last[0] >= _PROGRESS_EVERY_SEC:
                        last[0] = now
                        emit("progress", bytes=base + received, phase="download")

                _fetch(f, tmp, on_bytes)
                if not file_matches(tmp, f.sha256, f.size):
                    # Every family's download verifies already; a file that
                    # still is not the pinned one never takes the final name.
                    print("the file downloaded is not the pinned one", file=sys.stderr)
                    raise Failed("checksum")
                _place(tmp, f.dest)
                _drop_empty(stage)
                done_bytes += f.size
                emit("progress", bytes=done_bytes, phase="download")
        if plan.convert_to is not None:
            _convert(plan, token, emit, done_bytes)
    except Failed as exc:
        emit("error", code=exc.code)
        return 3
    except ModelDownloadError as exc:
        print(str(exc), file=sys.stderr)
        emit("error", code="checksum" if exc.reason == CHECKSUM_REASON else "network")
        return 3
    except OSError as exc:
        print(f"could not write the file: {exc}", file=sys.stderr)
        emit("error", code=_write_code(exc))
        return 4
    except Exception as exc:  # noqa: BLE001 -- the parent must hear an ending
        print(f"download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        emit("error", code="failed")
        return 5
    emit("progress", bytes=plan.total)
    emit("done")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model", required=True)
    parser.add_argument("--token", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    from app.core import model_fetch

    model_fetch._claim_stdout()
    # Hard exit when the server is gone: nobody would use the file.
    arm_parent_watchdog()
    args = _parser().parse_args(argv)
    plan = model_get.plan_for(args.model)
    try:
        model_get.staging_dir(Path("."), args.token)
    except ValueError:
        plan = None
    if plan is None:
        model_fetch.emit("error", code="failed")
        return 3
    return run(plan, args.token, model_fetch.emit)


if __name__ == "__main__":
    sys.exit(main())
