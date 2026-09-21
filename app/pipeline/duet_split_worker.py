"""On-demand duet split worker.

Run as its own process: `python -m app.pipeline.duet_split_worker <device>
<vocals_wav> <out_dir>`. Like vocal_split_worker.py and unlike demucs_worker.py
this is **not** persistent -- the split is an occasional, user-triggered action
on an already-done job, so there is no repeat model-load cost worth amortising.

Runs the BS-Roformer male/female model (or STEMDECK_DUET_MODEL's override) via
`audio-separator` on an already-isolated vocals stem, producing voice_1.wav +
voice_2.wav in out_dir.

Protocol, matching demucs_worker.py's contract so the parent's stderr reader
can be reused: writes progress/log lines to stderr, then exactly one of:
  "@@DONE@@"                        -- success, both files written
  "@@ERROR@@<json-encoded message>" -- failure; process exits(1)
"""

from __future__ import annotations

import json
import os
import sys

from app.core.config import DUET_SPLIT_MODEL
from app.core.process import arm_parent_watchdog


def _run(device: str, vocals_path: str, out_dir: str) -> None:
    """Split by register, unless the singers share one.

    The two models answer different questions. The register model separates by
    vocal weight and gives 30-40 dB of rejection when the singers sit in
    different ranges, which is the better result whenever it applies. The
    same-register model reaches about 14.5 dB but does not care about range.

    Picking between them automatically turns out not to be possible from the
    audio alone: a wrong separation is confident and uncorrelated, exactly like
    a right one, and every blind measure tried scored the register model's
    failure on this project's test song as healthy (see
    duet_medleyvox.register_split_collapsed for the four that were tried and
    what each missed).

    So the automatic path handles only the unambiguous failure -- one stem
    essentially silent -- and STEMDECK_DUET_SAME_REGISTER is how someone who has
    listened says which one they want.
    """
    from pathlib import Path

    from app.core.config import DUET_SAME_REGISTER
    from app.pipeline.duet_medleyvox import register_split_collapsed

    if DUET_SAME_REGISTER:
        sys.stderr.write("duet: same-register model requested\n")
        _run_same_register(device, vocals_path, out_dir)
        return

    _run_register(device, vocals_path, out_dir)

    a, b = Path(out_dir) / "voice_1.wav", Path(out_dir) / "voice_2.wav"
    if not (a.is_file() and b.is_file()):
        raise RuntimeError("register split produced no stems")
    if not register_split_collapsed(a, b):
        return

    sys.stderr.write("duet: register split collapsed into one stem, trying same-register\n")
    _run_same_register(device, vocals_path, out_dir)


def _run_same_register(device: str, vocals_path: str, out_dir: str) -> None:
    """Separate two singers of the same range, overwriting the register stems."""
    from pathlib import Path

    import soundfile as sf

    from app.core.config import MODELS_DIR
    from app.pipeline.duet_medleyvox import load_models, separate

    wav, sr = sf.read(vocals_path, dtype="float32", always_2d=True)
    models = load_models(MODELS_DIR, device)
    stems = separate(models, wav, sr, device)

    for name, y in zip(("voice_1.wav", "voice_2.wav"), stems, strict=True):
        sf.write(str(Path(out_dir) / name), y.astype("float32"), sr)
    e = [float((s.astype(float) ** 2).sum()) for s in stems]
    total = sum(e) or 1.0
    sys.stderr.write(
        f"duet: same-register split, energy {100 * e[0] / total:.0f}/{100 * e[1] / total:.0f}\n"
    )


def _run_register(device: str, vocals_path: str, out_dir: str) -> None:
    if device == "cpu":
        # audio-separator/onnxruntime pick their execution provider mostly on
        # their own; hiding CUDA devices is the reliable way to force the CPU
        # provider when the caller explicitly resolved to "cpu" (matches the
        # separation stage's own device policy in app.core.settings).
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    from audio_separator.separator import Separator

    from app.core.config import MODELS_DIR
    from app.core.model_cache import duet_split_artifacts, load_or_heal

    separator = Separator(
        log_level=40,  # logging.ERROR -- only the lines we emit ourselves matter
        model_file_dir=str(MODELS_DIR / "audio-separator"),
        output_dir=out_dir,
        output_format="WAV",
    )
    # This is the only path Docker ever takes: there is no warmup step there, so
    # the model is fetched here on first use. A download cut short leaves a file
    # that audio-separator will keep accepting as present and keep failing to
    # load, which is #502 with no setup step to have caught it earlier.
    load_or_heal(
        lambda: separator.load_model(model_filename=DUET_SPLIT_MODEL),
        lambda: duet_split_artifacts(MODELS_DIR, DUET_SPLIT_MODEL),
    )
    # The model labels its two outputs "Male" and "Female", which is the label
    # set it was trained on rather than a claim about who is singing: what it
    # actually keys on is vocal weight/register. Renaming at the library
    # boundary avoids depending on audio-separator's default filename format.
    separator.separate(
        vocals_path,
        {"Male": "voice_1", "Female": "voice_2"},
    )


def main() -> None:
    # A Force-Quit of the app otherwise orphans this process holding the GPU:
    # onnxruntime reads nothing from stdin mid-inference, so EOF never arrives
    # and nothing else bounds it (#519).
    arm_parent_watchdog()
    if len(sys.argv) < 4:
        sys.stderr.write("@@ERROR@@usage: duet_split_worker <device> <vocals_wav> <out_dir>\n")
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
