"""Persistent demucs worker (#309).

Run as its own process: `python -m app.pipeline.demucs_worker <device>`.
Loads the model once, then serves jobs one at a time over stdin/stderr --
eliminating the torch import + model load + CUDA kernel warmup cost that
dominated repeated per-job subprocess spawns on GPU (measured 35-42% of the
separate stage on an RTX 3080; see #288/#309). The parent (separate.py)
keeps this process alive across consecutive successful jobs on the same
device and only tears it down on cancel or a genuine failure.

Protocol:
  - Parent writes one JSON line to stdin per job:
      {"source": "<path>", "job_dir": "<path>", "shifts": 1}
  - Normal demucs progress (tqdm, \\r-delimited "NN%" lines) streams to
    stderr exactly as it would from the demucs CLI -- apply_model(progress=
    True) is the same call the CLI itself makes, unchanged from what
    separate.py's reader already parsed from a one-shot subprocess.
  - On completion the worker writes one more stderr line and, only on
    failure, exits:
      "@@DONE@@"                          -- job ok, worker keeps serving
      "@@ERROR@@<json-encoded message>"   -- job failed, worker exits(1)
    A job failure always exits the worker rather than trying to keep
    serving: after an exception mid-inference (OOM-adjacent or not), GPU
    memory / CUDA context state for future jobs isn't something we can
    vouch for, so the parent respawns fresh rather than risk reusing a
    worker in an unknown state. This matches today's behavior, where any
    failure already meant "process is dead, next attempt spawns fresh" --
    the reuse win only applies to the happy path.
  - EOF on stdin (parent closed the pipe) ends the worker's loop cleanly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.core.config import DEMUCS_MODEL


def _run_one_job(model, device: str, req: dict) -> None:
    from demucs.apply import apply_model
    from demucs.audio import save_audio
    from demucs.separate import load_track

    source = Path(req["source"])
    job_dir = Path(req["job_dir"])
    shifts = int(req.get("shifts", 1))

    # Identical to demucs.separate.main()'s per-track body (same functions,
    # same default split/overlap/segment/clip/bit-depth) -- we're not
    # reimplementing the audio pipeline, just calling it repeatedly on an
    # already-loaded model instead of once per fresh process.
    wav = load_track(source, model.audio_channels, model.samplerate)
    ref = wav.mean(0)
    wav = wav - ref.mean()
    wav = wav / ref.std()
    sources = apply_model(
        model,
        wav[None],
        device=device,
        shifts=shifts,
        split=True,
        overlap=0.25,
        progress=True,
        num_workers=0,
        segment=None,
    )[0]
    sources = sources * ref.std()
    sources = sources + ref.mean()

    out_dir = job_dir / DEMUCS_MODEL / source.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    for stem_tensor, name in zip(sources, model.sources, strict=True):
        save_audio(
            stem_tensor,
            str(out_dir / f"{name}.wav"),
            samplerate=model.samplerate,
            clip="rescale",
            bits_per_sample=16,
            as_float=False,
        )


def main() -> None:
    device = sys.argv[1] if len(sys.argv) > 1 else "cpu"

    from demucs.pretrained import get_model

    model = get_model(DEMUCS_MODEL)
    model.eval()
    model.cpu()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            _run_one_job(model, device, req)
        except Exception as e:
            sys.stderr.write(f"@@ERROR@@{json.dumps(str(e))}\n")
            sys.stderr.flush()
            sys.exit(1)
        sys.stderr.write("@@DONE@@\n")
        sys.stderr.flush()


if __name__ == "__main__":
    main()
