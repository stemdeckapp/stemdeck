"""Isolated All-In-One inference worker for automatic song sections."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
from pathlib import Path

from app.pipeline.section_refine import refine_segments

_HEARTBEAT_SECONDS = 10

# Hugging Face populates its cache with symlinks. Creating one on Windows needs
# either elevation or Developer Mode, and the resulting WinError 1314 is an
# OSError rather than the PermissionError the hub falls back on, so the
# download crashes instead of copying. StemDeck runs unelevated by design, so
# the checkpoints are copied unconditionally: they total about 10 MB, and a
# deterministic cache is worth more than the saved space.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--stems-dir", type=Path, required=True)
    parser.add_argument("--identifier", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--beat-grid", type=Path)
    return parser


def _heartbeat(stop: threading.Event) -> None:
    while not stop.wait(_HEARTBEAT_SECONDS):
        print("SECTION_HEARTBEAT", file=sys.stderr, flush=True)


def _result_segments(result: object) -> list[dict[str, object]]:
    if isinstance(result, list):
        if len(result) != 1:
            raise RuntimeError("section model returned an unexpected result count")
        result = result[0]
    segments = getattr(result, "segments", None)
    if not isinstance(segments, list):
        raise RuntimeError("section model returned no segments")
    return [
        {
            "start": float(segment.start),
            "end": float(segment.end),
            "label": str(segment.label),
        }
        for segment in segments
    ]


def _load_beat_grid(path: Path | None) -> object | None:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("section refinement ignored an unreadable beat grid", file=sys.stderr, flush=True)
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    for path in (args.stems_dir / f"{name}.wav" for name in ("bass", "drums", "other", "vocals")):
        if not path.is_file():
            raise FileNotFoundError("required section-analysis stem is missing")

    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
    heartbeat.start()
    try:
        # Third-party diagnostics must stay off stdout. The parent accepts
        # exactly one compact JSON line there so malformed output cannot be
        # mistaken for section data.
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from allin1_infer.config import HARMONIX_LABELS
            from allin1_infer.helpers import run_inference
            from allin1_infer.models import load_pretrained_model
            from allin1_infer.spectrogram import extract_spectrograms

            spec_paths = extract_spectrograms(
                [args.stems_dir],
                args.stems_dir / "spec",
                multiprocess=False,
            )
            model = load_pretrained_model(model_name=args.model, device="cpu")
            with torch.no_grad():
                result = run_inference(
                    path=Path(f"{args.identifier}.wav"),
                    spec_path=spec_paths[0],
                    model=model,
                    device="cpu",
                    include_activations=True,
                    include_embeddings=True,
                )
        raw_segments = _result_segments(result)
        try:
            segments = refine_segments(
                raw_segments,
                getattr(result, "activations", None),
                getattr(result, "embeddings", None),
                getattr(result, "activation_fps", None),
                _load_beat_grid(args.beat_grid),
                HARMONIX_LABELS,
            )
        except Exception as exc:
            print(
                f"section refinement fell back after {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            segments = raw_segments
        print(json.dumps({"segments": segments}, separators=(",", ":")), flush=True)
        return 0
    finally:
        stop.set()
        heartbeat.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
