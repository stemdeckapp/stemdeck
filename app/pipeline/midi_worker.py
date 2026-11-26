"""Isolated note worker: one stem in, one .mid out.

Run as ``python -m app.pipeline.midi_worker --audio <stem> --out <file.mid>
--stem <one of config.MIDI_STEMS> --params <json> --tempo <json>
--max-seconds <n> --engine basic_pitch|piano_hr|ymt3 ...``. A fresh process
per request, like the lyrics transcription worker: a run is asked for by
hand, seconds to minutes of work, so nothing stays resident between them.

Protocol: the real stdout carries JSON lines only, one object each:

    {"event": "progress", "pct": 0..100}   (a torch engine's first one after
                                            the start also has "device")
    {"event": "done", "notes": n, "seconds_audio": s, "device": "cpu"|"cuda",
     "gpu_peak_mb": n}                     (the last on CUDA only: the most
                                            GPU memory torch reserved)
    {"event": "error", "code": "<code>"}   (and "phase": "import" when the
                                            engine itself failed to import)

and nothing else. Basic Pitch prints to stdout, logs three warnings at import
and its dependencies may write from C, so before anything third-party is
imported the process moves its stdout file descriptor aside for the protocol
and points descriptor 1 at stderr: whatever else writes "to stdout", Python
or native, lands in stderr, which the parent keeps only as a diagnostic tail.

Three engines, each a vendored copy put first on sys.path:

- basic_pitch (app/_vendor/transcribe): always its ONNX model by explicit
  path, on the CPU. TensorFlow, CoreML and TFLite are blocked from
  importing, so an install that happens to have one cannot change which
  model runs.
- piano_hr (app/_vendor/piano_hr, app/pipeline/piano_hr.py): the piano only,
  on torch, from a checkpoint the parent has already downloaded and checked
  against its pinned SHA-256 (--checkpoint), loaded with weights_only.
  --device cuda runs it on the GPU when torch has one and it has
  --gpu-min-free-mb free, else on the CPU; "done" says which it was.
  matplotlib, which the package imported for nothing, is blocked.
- ymt3 (app/_vendor/ymt3, app/pipeline/ymt3.py): every stem but the vocals,
  on torch, from the weights-only file the parent has already made and
  checked against its pinned SHA-256 (--checkpoint), read as tensors only.
  Its code imports transformers and pytorch_lightning: the vendored
  stand-ins (app/_vendor/ymt3/shim) go first on sys.path, every module the
  engine imported is checked to come from the vendored folders, and the
  real Lightning, wandb, torchmetrics and matplotlib are blocked. --device as
  piano_hr.

The worker never needs the network, and refuses it: every socket connect and
name lookup raises, and so does os.system (the package's own downloader is a
wget through it, in a class left out of the vendored copy).

Error codes: too_long (the file's header says it is longer than
--max-seconds, refused before anything is decoded), no_audio (missing or
unreadable), engine (the engine failed to import, load or run), disk (no
space left), io (any other error writing the file).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from pathlib import Path

from app.core.config import MIDI_ENGINE_STEMS, MIDI_STEMS
from app.core.process import arm_parent_watchdog

# The real stdout, moved aside for the protocol (see _claim_stdout).
_proto = None


def _claim_stdout() -> None:
    """Keep descriptor 1 for the protocol under a new number, and make 1 a
    copy of 2. Python's sys.stdout follows, so print() goes to stderr too."""
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


def _no_network(*_args: object, **_kwargs: object):
    raise OSError(errno.EACCES, "the MIDI worker has no network access")


def _block_network() -> None:
    """Defense in depth: nothing in a transcription needs the network, so any
    attempt to reach it fails instead of succeeding quietly."""
    import socket

    socket.socket.connect = _no_network  # type: ignore[method-assign]
    socket.socket.connect_ex = _no_network  # type: ignore[method-assign]
    socket.socket.sendto = _no_network  # type: ignore[method-assign]
    socket.create_connection = _no_network  # type: ignore[assignment]
    socket.getaddrinfo = _no_network  # type: ignore[assignment]
    socket.gethostbyname = _no_network  # type: ignore[assignment]
    # A shell command could reach the network around the socket module.
    os.system = _no_network  # type: ignore[assignment]


# Model runtimes Basic Pitch tries before ONNX when they import, and
# matplotlib, which nothing in a transcription draws with. None in
# sys.modules makes an import of them raise ImportError at once.
_BLOCKED_RUNTIMES = ("tensorflow", "coremltools", "tflite_runtime", "matplotlib")
# For ymt3: matplotlib, and what YourMT3's training code imported, none of
# which may load in a worker, so a vendored file that still reached for one
# fails loudly instead. Not Basic Pitch's runtimes: einops picks its tensor
# library by the names in sys.modules, and would try a None tensorflow.
_BLOCKED_YMT3 = (
    "matplotlib",
    "lightning",
    "lightning_fabric",
    "lightning_utilities",
    "torchmetrics",
    "wandb",
    "deepspeed",
)


def _put_first(path: Path) -> None:
    value = str(path)
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)


def engine_paths(engine: str, args: argparse.Namespace) -> list[Path]:
    """The folders an engine's vendored code is imported from, the first to
    be searched first: none for Basic Pitch (it is in --vendor)."""
    if engine == "piano_hr" and args.piano_vendor is not None:
        return [args.piano_vendor]
    if engine == "ymt3" and args.ymt3_vendor is not None:
        return [args.ymt3_vendor / "shim", args.ymt3_vendor / "src"]
    return []


def _install_engine(
    vendor_dir: Path, engine_dirs: list[Path] | None = None, engine: str = "basic_pitch"
) -> None:
    """Put the vendored engines first on sys.path: ``vendor_dir`` (Basic
    Pitch, and mido, which the file writer needs for every engine), then
    ``engine_dirs`` in front of it, the first of them first, with the
    engine's blocked packages made unimportable."""
    import logging

    for name in _BLOCKED_YMT3 if engine == "ymt3" else _BLOCKED_RUNTIMES:
        sys.modules[name] = None  # type: ignore[assignment]
    _put_first(vendor_dir)
    for folder in reversed(engine_dirs or []):
        _put_first(folder)
    # The root logger answers its first warning by configuring itself
    # (logging.lastResort, then basicConfig on the module-level helpers).
    # Basic Pitch logs one per runtime it does not find; a handler of our own
    # at ERROR keeps those out and still shows a real failure.
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)
    root.setLevel(logging.ERROR)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--stem", choices=MIDI_STEMS, required=True)
    parser.add_argument("--params", required=True)
    parser.add_argument("--tempo", required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--engine", choices=tuple(MIDI_ENGINE_STEMS), default="basic_pitch")
    parser.add_argument("--piano-vendor", type=Path)
    parser.add_argument("--ymt3-vendor", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--gpu-min-free-mb", type=int, default=0)
    return parser


def audio_seconds(path: Path) -> float | None:
    """The file's length from its header, without decoding it, or None when
    the header cannot be read."""
    import soundfile as sf

    try:
        info = sf.info(str(path))
    except Exception:
        return None
    if not info.samplerate or info.frames <= 0:
        return None
    return float(info.frames) / float(info.samplerate)


def _write_code(exc: OSError) -> str:
    return "disk" if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) else "io"


def load_model(model_path: Path):
    """The vendored engine's ONNX model, refused when it loads as anything
    else. The engine must already be on sys.path (_install_engine)."""
    from basic_pitch import inference

    model = inference.Model(str(model_path))
    if model.model_type != inference.Model.MODEL_TYPES.ONNX:
        raise RuntimeError(f"loaded as {model.model_type}, not ONNX")
    return model


def model_output(model, audio: Path, on_pct=None) -> dict:
    """The model's frame, onset and contour activations over the whole file.

    inference.run_inference, window by window, so each window can be a
    progress line (``on_pct``, 5 to 90). The same constants and the same
    calls in the same order: the output is Basic Pitch's own."""
    import numpy as np
    from basic_pitch import inference
    from basic_pitch.constants import AUDIO_N_SAMPLES, FFT_HOP

    n_overlapping_frames = 30
    overlap_len = n_overlapping_frames * FFT_HOP
    hop_size = AUDIO_N_SAMPLES - overlap_len
    output: dict[str, list] = {"note": [], "onset": [], "contour": []}
    total = None
    original_length = 0
    windows = inference.get_audio_input(audio, overlap_len, hop_size)
    for done, (window, _times, original_length) in enumerate(windows, start=1):
        if total is None:
            padded = original_length + overlap_len // 2
            total = max(1, -(-padded // hop_size))
            if on_pct is not None:
                on_pct(5)
        for key, value in model.predict(window).items():
            output[key].append(value)
        if on_pct is not None:
            on_pct(5 + min(85, 85 * done // total))
    if total is None:
        raise RuntimeError("the audio has no samples")
    return {
        key: inference.unwrap_output(
            np.concatenate(output[key]), original_length, n_overlapping_frames
        )
        for key in output
    }


def output_notes(output: dict, params: dict) -> list:
    """Note events (start s, end s, MIDI pitch, amplitude, None) from the
    model's activations, under ``params`` (midi.run_params).

    Basic Pitch's own steps where they are right (the stem's frequency range,
    inferred onsets, frame times), StemDeck's decoding where they are not
    (midi_notes.onset_notes), then one voice for a stem that asks for it."""
    import numpy as np
    from basic_pitch import note_creation
    from basic_pitch.constants import AUDIO_SAMPLE_RATE, FFT_HOP

    from app.pipeline import midi_notes

    min_note_len = int(
        np.round(params["minimum_note_length"] / 1000 * (AUDIO_SAMPLE_RATE / FFT_HOP))
    )
    # constrain_frequency zeroes the activations outside the range in place:
    # a caller that decodes one output more than once passes a copy each time.
    onsets, frames = note_creation.constrain_frequency(
        output["onset"], output["note"], params["maximum_frequency"], params["minimum_frequency"]
    )
    onsets = note_creation.get_infered_onsets(onsets, frames)
    found = midi_notes.onset_notes(
        frames, onsets, params["onset_threshold"], params["frame_threshold"], min_note_len
    )
    times = note_creation.model_frames_to_time(frames.shape[0])
    notes = [(times[a], times[b], pitch, amp, None) for a, b, pitch, amp in found]
    if params.get("one_voice"):
        notes = midi_notes.one_voice(notes)
    return notes


def pick_device(asked: str, min_free_mb: int) -> str:
    """The device a torch engine runs on: "cuda" when asked for and torch has
    a GPU with ``min_free_mb`` free right now (a separation may hold most of it),
    else "cpu". Said on stderr, so the parent's log shows why."""
    if asked != "cuda":
        return "cpu"
    import torch

    if not torch.cuda.is_available():
        print("no CUDA device: running on the CPU", file=sys.stderr)
        return "cpu"
    try:
        free, _total = torch.cuda.mem_get_info()
    except Exception as exc:  # noqa: BLE001 -- an unreadable card is not one to use
        print(f"GPU memory unreadable ({type(exc).__name__}): running on the CPU", file=sys.stderr)
        return "cpu"
    if free < min_free_mb * 2**20:
        print(
            f"{free // 2**20} MB free on the GPU, {min_free_mb} MB needed: running on the CPU",
            file=sys.stderr,
        )
        return "cpu"
    return "cuda"


def _from_vendored(names: tuple[str, ...], folders: list[Path]) -> None:
    """Every module whose top-level name is in ``names`` came from one of
    ``folders``: a package of the same name installed elsewhere would
    otherwise be run in its place without a word."""
    roots = [f.resolve() for f in folders]
    for name, module in list(sys.modules.items()):
        if module is None or name.split(".")[0] not in names:
            continue
        where = getattr(module, "__file__", None)
        if where is None or not any(Path(where).resolve().is_relative_to(r) for r in roots):
            raise ImportError(f"{name} did not come from the vendored engine ({where})")


# The top-level packages of the ymt3 engine's vendored folders.
_YMT3_PACKAGES = ("config", "model", "utils", "transformers", "pytorch_lightning")


def _import_engine(engine: str, folders: list[Path] | None = None) -> None:
    import numpy  # noqa: F401

    if engine == "piano_hr":
        import torch  # noqa: F401
        from piano_transcription_inference import models, utilities  # noqa: F401
    elif engine == "ymt3":
        import einops  # noqa: F401
        import torch  # noqa: F401
        import torchaudio  # noqa: F401
        from model import ymt3  # noqa: F401
        from utils import event2note, note2event, task_manager  # noqa: F401

        _from_vendored(_YMT3_PACKAGES, folders or [])
    else:
        from basic_pitch import inference, note_creation  # noqa: F401
        from basic_pitch.constants import AUDIO_N_SAMPLES, AUDIO_SAMPLE_RATE, FFT_HOP  # noqa: F401


def _gpu_peak_mb(device: str) -> int | None:
    if device != "cuda":
        return None
    import torch

    return int(torch.cuda.max_memory_reserved() // 2**20)


def _notes(args: argparse.Namespace, params: dict) -> tuple[list, str, int | None]:
    """The notes of the whole stem, the device they were made on, and the
    General MIDI program the engine heard (None: the stem's own)."""

    def on_pct(pct: int) -> None:
        emit("progress", pct=pct)

    if args.engine == "ymt3":
        from app.pipeline import ymt3

        device = pick_device(args.device, args.gpu_min_free_mb)
        # Said before anything can fail on it, as for piano_hr.
        emit("progress", pct=2, device=device)
        model = ymt3.load_weights(ymt3.build_model(), args.checkpoint, device)
        segs = ymt3.segments(ymt3.read_audio(args.audio))
        preds = ymt3.model_output(model, segs, device, args.batch, on_pct)
        events, program = ymt3.stem_notes(ymt3.decode(model, preds, len(segs)), args.stem)
        return events, device, program
    if args.engine == "piano_hr":
        from app.pipeline import piano_hr

        device = pick_device(args.device, args.gpu_min_free_mb)
        # Said before anything can fail on it: the parent runs a failure on
        # the GPU again on the CPU, and one already on the CPU it does not.
        emit("progress", pct=2, device=device)
        model = piano_hr.load_model(args.checkpoint, device)
        audio = piano_hr.read_audio(args.audio)
        output = piano_hr.model_output(model, audio, device, args.batch, on_pct)
        return piano_hr.output_notes(output, params), device, None
    model = load_model(args.model)
    output = model_output(model, args.audio, on_pct)
    return output_notes(output, params), "cpu", None


def transcribe(args: argparse.Namespace) -> int:
    seconds = audio_seconds(args.audio)
    if seconds is None:
        emit("error", code="no_audio")
        return 3
    if seconds > args.max_seconds:
        emit("error", code="too_long")
        return 3
    params = json.loads(args.params)
    plan = json.loads(args.tempo)
    if args.engine != "basic_pitch" and (args.checkpoint is None or not args.checkpoint.is_file()):
        # The parent downloads and checks it before it starts this process.
        print(f"{args.engine} needs --checkpoint, an existing file", file=sys.stderr)
        emit("error", code="engine")
        return 4

    try:
        _import_engine(args.engine, engine_paths(args.engine, args))
    except Exception as exc:
        print(f"engine import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        # "import": every run would fail the same way (midi.availability).
        emit("error", code="engine", phase="import")
        return 4

    from app.pipeline.midi_file import write_midi

    emit("progress", pct=1)
    try:
        notes, device, program = _notes(args, params)
    except Exception as exc:
        print(f"engine failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        emit("error", code="engine")
        return 4
    emit("progress", pct=95)

    try:
        count = write_midi(notes, args.out, args.stem, plan, program)
    except OSError as exc:
        print(f"could not write the file: {exc}", file=sys.stderr)
        emit("error", code=_write_code(exc))
        return 5
    peak = _gpu_peak_mb(device)
    extra = {"gpu_peak_mb": peak} if peak is not None else {}
    emit("done", notes=count, seconds_audio=round(seconds, 3), device=device, **extra)
    return 0


def main(argv: list[str] | None = None) -> int:
    _claim_stdout()
    _block_network()
    # Hard exit when the server is gone: nobody would collect the file.
    arm_parent_watchdog()
    args = _parser().parse_args(argv)
    if not args.audio.is_file():
        emit("error", code="no_audio")
        return 3
    if args.stem not in MIDI_ENGINE_STEMS[args.engine]:
        # piano_hr knows the 88 keys and nothing else; Basic Pitch hears no
        # drums; YourMT3+ is not used for the vocals (config.MIDI_ENGINES).
        emit("error", code="engine")
        return 4
    _install_engine(args.vendor, engine_paths(args.engine, args), args.engine)
    return transcribe(args)


if __name__ == "__main__":
    sys.exit(main())
