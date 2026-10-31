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
  "@@DONE@@"                -- success, both files written
  "@@ERROR@@<json object>"  -- failure; process exits(1). The object is
      {"message": str(exception), "code": one of
      app.core.vocal_split_outcome.CODES}. The code is decided here, where the
      step that failed is known (#832): an import that fails is a missing
      dependency, a model that will not fetch or load is the model.
"""

from __future__ import annotations

import json
import os
import sys

from app.core.config import VOCAL_SPLIT_MODEL
from app.core.process import arm_parent_watchdog


def _import_separator():
    """The audio-separator class. Where a missing or broken dependency shows."""
    from audio_separator.separator import Separator

    return Separator


def _load(separator) -> None:
    """Fetch the model if it is not cached yet, and load it. The architecture
    module, and with it onnx2torch and torchvision (#778), is imported here."""
    from app.core.config import MODELS_DIR
    from app.core.model_cache import load_or_heal, vocal_split_artifacts

    # This is the only path Docker ever takes: there is no warmup step there, so
    # the model is fetched here on first use. A download cut short leaves a file
    # that audio-separator will keep accepting as present and keep failing to
    # load, which is #502 with no setup step to have caught it earlier.
    load_or_heal(
        lambda: separator.load_model(model_filename=VOCAL_SPLIT_MODEL),
        lambda: vocal_split_artifacts(MODELS_DIR, VOCAL_SPLIT_MODEL),
    )


def _separate(separator, vocals_path: str) -> None:
    # Karaoke-family models label their two outputs "Vocals" (the lead vocal
    # that survived a second isolation pass) and "Instrumental" (everything in
    # the input that wasn't lead vocal -- since the input here is already an
    # isolated vocals stem, that's the backing vocals/harmonies). Renaming at
    # the library boundary avoids depending on its default filename format.
    separator.separate(
        vocals_path,
        {"Vocals": "lead_vocals", "Instrumental": "backing_vocals"},
    )


def _run(device: str, vocals_path: str, out_dir: str, phase: list[str]) -> None:
    """Run the split. ``phase[0]`` names the step in progress, so the caller
    can say which one failed."""
    if device == "cpu":
        # audio-separator/onnxruntime pick their execution provider mostly on
        # their own; hiding CUDA devices is the reliable way to force the CPU
        # provider when the caller explicitly resolved to "cpu" (matches the
        # separation stage's own device policy in app.core.settings).
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    phase[0] = "import"
    separator_cls = _import_separator()

    from app.core.config import MODELS_DIR

    phase[0] = "load"
    separator = separator_cls(
        log_level=40,  # logging.ERROR -- only the lines we emit ourselves matter
        model_file_dir=str(MODELS_DIR / "audio-separator"),
        output_dir=out_dir,
        output_format="WAV",
    )
    _load(separator)
    phase[0] = "separate"
    _separate(separator, vocals_path)


def _error_line(phase: str, exc: BaseException) -> str:
    try:
        from app.pipeline.vocal_split_status import failure_code

        code = failure_code(phase, exc)
    except Exception:
        # The classifier is a nicety. Failing to load it must not cost the
        # parent the error itself.
        code = "worker_failed"
    return "@@ERROR@@" + json.dumps({"message": str(exc), "code": code})


def main() -> None:
    # A Force-Quit of the app otherwise orphans this process holding the GPU:
    # onnxruntime reads nothing from stdin mid-inference, so EOF never arrives
    # and nothing else bounds it (#519).
    arm_parent_watchdog()
    if len(sys.argv) < 4:
        sys.stderr.write("@@ERROR@@usage: vocal_split_worker <device> <vocals_wav> <out_dir>\n")
        sys.stderr.flush()
        sys.exit(1)
    device, vocals_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    phase = ["import"]
    try:
        _run(device, vocals_path, out_dir, phase)
    except Exception as e:
        sys.stderr.write(_error_line(phase[0], e) + "\n")
        sys.stderr.flush()
        sys.exit(1)
    sys.stderr.write("@@DONE@@\n")
    sys.stderr.flush()


if __name__ == "__main__":
    main()
