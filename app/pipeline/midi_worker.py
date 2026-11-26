"""Isolated Basic Pitch worker: one stem in, one .mid out.

Run as ``python -m app.pipeline.midi_worker --audio <stem> --out <file.mid>
--stem piano|bass --params <json> --tempo <json> --max-seconds <n>``. A fresh
process per request, like the lyrics transcription worker: a run is a few
seconds of CPU, asked for by hand, so nothing stays resident between them.

Protocol: the real stdout carries JSON lines only, one object each:

    {"event": "progress", "pct": 0..100}
    {"event": "done", "notes": n, "seconds_audio": s}
    {"event": "error", "code": "<code>"}   (and "phase": "import" when the
                                            engine itself failed to import)

and nothing else. Basic Pitch prints to stdout, logs three warnings at import
and its dependencies may write from C, so before anything third-party is
imported the process moves its stdout file descriptor aside for the protocol
and points descriptor 1 at stderr: whatever else writes "to stdout", Python
or native, lands in stderr, which the parent keeps only as a diagnostic tail.

The engine is the vendored copy in app/_vendor/transcribe, put first on
sys.path, and always its ONNX model by explicit path: TensorFlow, CoreML and
TFLite are blocked from importing, so an install that happens to have one
cannot change which model runs. The worker never needs the network, and
refuses it: every socket connect and name lookup raises.

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


# Model runtimes Basic Pitch tries before ONNX when they import. None in
# sys.modules makes an import of them raise ImportError at once.
_BLOCKED_RUNTIMES = ("tensorflow", "coremltools", "tflite_runtime")


def _install_engine(vendor_dir: Path) -> None:
    import logging

    for name in _BLOCKED_RUNTIMES:
        sys.modules[name] = None  # type: ignore[assignment]
    path = str(vendor_dir)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
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
    parser.add_argument("--stem", choices=("piano", "bass"), required=True)
    parser.add_argument("--params", required=True)
    parser.add_argument("--tempo", required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--vendor", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
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

    try:
        import numpy  # noqa: F401
        from basic_pitch import inference, note_creation  # noqa: F401
        from basic_pitch.constants import AUDIO_N_SAMPLES, AUDIO_SAMPLE_RATE, FFT_HOP  # noqa: F401
    except Exception as exc:
        print(f"engine import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        # "import": every run would fail the same way (midi.availability).
        emit("error", code="engine", phase="import")
        return 4

    from app.pipeline.midi_file import write_midi

    emit("progress", pct=1)
    try:
        model = load_model(args.model)
        output = model_output(model, args.audio, lambda pct: emit("progress", pct=pct))
        notes = output_notes(output, params)
    except Exception as exc:
        print(f"engine failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        emit("error", code="engine")
        return 4
    emit("progress", pct=95)

    try:
        count = write_midi(notes, args.out, args.stem, plan)
    except OSError as exc:
        print(f"could not write the file: {exc}", file=sys.stderr)
        emit("error", code=_write_code(exc))
        return 5
    emit("done", notes=count, seconds_audio=round(seconds, 3))
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
    _install_engine(args.vendor)
    return transcribe(args)


if __name__ == "__main__":
    sys.exit(main())
