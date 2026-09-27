"""Isolated Whisper worker: a vocals stem in, timed words out.

Run as ``python -m app.pipeline.transcribe_worker --audio <vocals.wav> ...``.
A fresh process per job, like the section worker, rather than a persistent
one like Demucs: transcription runs once per import at most, and only when no
lyrics were found, so a resident model would hold VRAM for nothing. The
process exiting is what gives every byte of GPU memory back, which also keeps
it clear of the Demucs worker's own allocation on the same card.

Output contract: exactly one compact JSON line on stdout,
``{"language", "language_probability", "model", "device", "segments",
"peak_vram_mb"}``, each segment ``{"start", "end", "text", "words"}`` and each
word ``{"start", "end", "word", "probability"}``. Everything else, including
third-party chatter and Whisper's own progress bars, goes to stderr, where
the parent reads ``@@PHASE@@<name>`` lines and percentages for the stage text.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
from pathlib import Path

from app.core.process import arm_parent_watchdog

# Whisper works on 16 kHz mono and decides a language from 30 s of it.
_SAMPLE_RATE = 16000
_WINDOW_SECONDS = 30


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--fallback-model", required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--min-free-mb", type=int, required=True)
    parser.add_argument("--small-min-free-mb", type=int, required=True)
    parser.add_argument("--silence-sec", type=float, required=True)
    return parser


def _phase(name: str) -> None:
    print(f"@@PHASE@@{name}", file=sys.stderr, flush=True)


def load_audio(path: Path):
    """The stem as 16 kHz mono float32. Read with soundfile and resampled
    here rather than through whisper.load_audio, which shells out to whatever
    ``ffmpeg`` is first on PATH instead of the binary StemDeck verified."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate != _SAMPLE_RATE:
        g = math.gcd(int(rate), _SAMPLE_RATE)
        mono = resample_poly(mono, _SAMPLE_RATE // g, int(rate) // g)
    return np.ascontiguousarray(mono, dtype=np.float32)


def loudest_window(audio, seconds: int = _WINDOW_SECONDS) -> tuple[int, int]:
    """Sample bounds of the ``seconds`` long stretch with the most energy.

    Whisper decides the language from the first 30 s it is given, which for
    a vocals stem is often an intro with nobody singing, and a language
    guessed from silence is a coin toss. The stretch where the singer is
    loudest is where the language is actually audible.
    """
    import numpy as np

    size = seconds * _SAMPLE_RATE
    if len(audio) <= size:
        return 0, len(audio)
    hop = _SAMPLE_RATE // 2
    frames = len(audio) // hop
    energy = np.square(audio[: frames * hop].astype(np.float64)).reshape(frames, hop).sum(axis=1)
    span = size // hop
    cumulative = np.concatenate(([0.0], np.cumsum(energy)))
    sums = cumulative[span:] - cumulative[:-span]
    start = int(np.argmax(sums)) * hop
    return start, start + size


def pick_model(args: argparse.Namespace, free_mb: int | None) -> tuple[str, str]:
    """(device, model) given the free VRAM, None meaning not on a GPU."""
    if args.device != "cuda" or free_mb is None:
        return "cpu", args.fallback_model
    if free_mb >= args.min_free_mb:
        return "cuda", args.model
    if free_mb >= args.small_min_free_mb:
        return "cuda", args.fallback_model
    return "cpu", args.fallback_model


def _free_vram_mb(torch) -> int | None:
    try:
        free, _total = torch.cuda.mem_get_info()
    except Exception:
        return None
    return int(free // (1024 * 1024))


def _model_file(whisper, name: str, root: Path) -> Path | None:
    url = getattr(whisper, "_MODELS", {}).get(name)
    return root / os.path.basename(url) if url else None


def load_model(whisper, torch, name: str, device: str, root: Path):
    """The model on ``device``, in fp16 on a GPU.

    The checkpoint is stored in fp16 and on a GPU inference runs in fp16, but
    whisper.load_model widens the weights to fp32 first: turbo then holds
    3.2 GB of VRAM instead of 1.6. Measured on an RTX 3080 with Nirvana's
    "Lithium" (255 s), the peak reserved fell from 5254 MB to 2172 MB with
    the same words. Whisper's LayerNorm computes in fp32, so it keeps its
    fp32 weights.
    """
    model = whisper.load_model(name, device="cpu", download_root=str(root))
    if device != "cuda":
        return model
    model.half()
    for module in model.modules():
        if isinstance(module, torch.nn.LayerNorm):
            module.float()
    return model.to(device)


def transcribe(args: argparse.Namespace) -> dict:
    import torch
    import whisper

    audio = load_audio(args.audio)
    free_mb = _free_vram_mb(torch) if args.device == "cuda" and torch.cuda.is_available() else None
    device, model_name = pick_model(args, free_mb)
    if args.device == "cuda" and device != "cuda":
        print(f"not enough free VRAM ({free_mb} MB), transcribing on the CPU", file=sys.stderr)

    model_file = _model_file(whisper, model_name, args.download_root)
    if model_file is not None and not model_file.is_file():
        _phase("download")
    else:
        _phase("load")
    model = load_model(whisper, torch, model_name, device, args.download_root)

    if model.is_multilingual:
        start, end = loudest_window(audio)
        mel = whisper.log_mel_spectrogram(
            whisper.pad_or_trim(audio[start:end]), n_mels=model.dims.n_mels
        ).to(model.device)
        if device == "cuda":
            mel = mel.half()
        _tokens, probs = model.detect_language(mel)
        language = max(probs, key=probs.get)
    else:  # an English-only model set through the environment
        language, probs = "en", {"en": 1.0}

    _phase("transcribe")
    result = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        fp16=device == "cuda",
        # Progress bars (to stderr), but not every decoded line.
        verbose=False,
        # Carrying the previous window's text forward is what makes Whisper
        # repeat a line forever over a long instrumental; lyrics lose little
        # without it.
        condition_on_previous_text=False,
        hallucination_silence_threshold=args.silence_sec,
    )
    peak = None
    if device == "cuda":
        peak = int(torch.cuda.max_memory_allocated() // (1024 * 1024))
    segments = []
    for seg in result.get("segments", []):
        words = [
            {
                "start": float(w["start"]),
                "end": float(w["end"]),
                "word": str(w["word"]),
                "probability": float(w.get("probability", 0.0)),
            }
            for w in seg.get("words") or []
        ]
        segments.append(
            {
                "start": float(seg["start"]),
                "end": float(seg["end"]),
                "text": str(seg.get("text", "")),
                "words": words,
                "no_speech_prob": float(seg.get("no_speech_prob", 0.0)),
                "avg_logprob": float(seg.get("avg_logprob", 0.0)),
                "compression_ratio": float(seg.get("compression_ratio", 0.0)),
            }
        )
    return {
        "language": language,
        "language_probability": round(float(probs[language]), 4),
        "model": model_name,
        "device": device,
        "segments": segments,
        "peak_vram_mb": peak,
    }


def main(argv: list[str] | None = None) -> int:
    # A GPU pass that outlives its parent holds VRAM with nobody to collect
    # the answer (#519).
    arm_parent_watchdog()
    args = _parser().parse_args(argv)
    if not args.audio.is_file():
        raise FileNotFoundError("vocals stem is missing")
    # Third-party diagnostics stay off stdout: the parent accepts exactly one
    # JSON line there, so nothing else can be mistaken for the transcript.
    with contextlib.redirect_stdout(sys.stderr):
        out = transcribe(args)
    print(json.dumps(out, separators=(",", ":"), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
