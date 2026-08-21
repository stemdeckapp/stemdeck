"""On-demand lead/backing vocal split worker (#275).

Run as its own process: `python -m app.pipeline.vocal_split_worker <device>
<vocals_wav> <out_dir>`. Unlike demucs_worker.py, this is **not** persistent --
the split is an occasional, user-triggered action on an already-done job, not
the hot path every job takes, so there is no repeat-model-load cost worth
amortizing (see ml-pipeline.md / #309 for why that reasoning does not apply
here the way it does to Demucs).

Runs UVR-MDX-NET Karaoke 2 (or STEMDECK_KARAOKE_MODEL's override) via the
`audio-separator` package on an already-isolated vocals stem, producing
lead_vocals.wav + backing_vocals.wav in out_dir.

Protocol, matching demucs_worker.py's contract so the parent's stderr reader
can be reused: writes progress/log lines to stderr, then exactly one of:
  "@@DONE@@"                        -- success, both files written
  "@@ERROR@@<json-encoded message>" -- failure; process exits(1)
"""

from __future__ import annotations

import json
import os
import sys

from app.core.config import VOCAL_SPLIT_MODEL


def _run(device: str, vocals_path: str, out_dir: str) -> None:
    if device == "cpu":
        # audio-separator/onnxruntime pick their execution provider mostly on
        # their own; hiding CUDA devices is the reliable way to force the CPU
        # provider when the caller explicitly resolved to "cpu" (matches the
        # separation stage's own device policy in app.core.settings).
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    from audio_separator.separator import Separator

    from app.core.config import MODELS_DIR

    separator = Separator(
        log_level=40,  # logging.ERROR -- only the lines we emit ourselves matter
        model_file_dir=str(MODELS_DIR / "audio-separator"),
        output_dir=out_dir,
        output_format="WAV",
    )
    separator.load_model(model_filename=VOCAL_SPLIT_MODEL)
    # Karaoke-family models label their two outputs "Vocals" (the lead vocal
    # that survived a second isolation pass) and "Instrumental" (everything in
    # the input that wasn't lead vocal -- since the input here is already an
    # isolated vocals stem, that's the backing vocals/harmonies). Renaming at
    # the library boundary avoids depending on its default filename format.
    separator.separate(
        vocals_path,
        {"Vocals": "lead_vocals", "Instrumental": "backing_vocals"},
    )


def main() -> None:
    if len(sys.argv) < 4:
        sys.stderr.write("@@ERROR@@usage: vocal_split_worker <device> <vocals_wav> <out_dir>\n")
        sys.stderr.flush()
        sys.exit(1)
    device, vocals_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        _run(device, vocals_path, out_dir)
    except Exception as e:
        sys.stderr.write(f"@@ERROR@@{json.dumps(str(e))}\n")
        sys.stderr.flush()
        sys.exit(1)
    sys.stderr.write("@@DONE@@\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
