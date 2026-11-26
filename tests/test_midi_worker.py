"""The note worker (app/pipeline/midi_worker.py) as a subprocess.

Real inference on short synthetic tones (the ONNX model is vendored, so
nothing is downloaded), then the protocol with a stand-in engine that does
everything a badly behaved library can: prints to stdout from Python and from
the file descriptor, logs at import and tries the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.pipeline import midi as midi_run
from app.pipeline import midi_file as mf

ROOT = Path(__file__).resolve().parent.parent
SR = 44100


def _wav(path: Path, y: np.ndarray) -> Path:
    sf.write(str(path), y.astype("float32"), SR)
    return path


def _tone(freq: float, seconds: float, partials: int = 1, decay: float = 0.0) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    y = sum((0.6**k) * np.sin(2 * np.pi * freq * (k + 1) * t) for k in range(partials))
    y = y * np.exp(-t * decay) * np.minimum(1, t / 0.005)
    return 0.4 * y / np.abs(y).max()


def _env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        STEMDECK_JOBS_DIR=str(tmp_path / "jobs"),
        STEMDECK_DATA_DIR=str(tmp_path / "data"),
        STEMDECK_CACHE_DIR=str(tmp_path / "cache"),
        PYTHONIOENCODING="utf-8",
    )
    return env


def _run(cmd: list[str], tmp_path: Path, timeout: float = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT, env=_env(tmp_path)
    )


def _events(stdout: str) -> list[dict]:
    """Every stdout line, each of which must be a protocol event."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    assert all(e["event"] in ("progress", "done", "error") for e in events)
    return events


def _pitches(path: Path) -> list[tuple[int, int, int]]:
    """(pitch, velocity, length in ticks) per note, read with the vendored mido."""
    sys.path.insert(0, str(mf.VENDOR_DIR))
    try:
        import mido
    finally:
        sys.path.remove(str(mf.VENDOR_DIR))
    held, out, now = {}, [], 0
    for m in mido.MidiFile(str(path)).tracks[0]:
        now += m.time
        if m.type == "note_on" and m.velocity:
            held[m.note] = (now, m.velocity)
        elif m.type == "note_off":
            start, vel = held.pop(m.note)
            out.append((m.note, vel, now - start))
    return out


def _cmd(audio: Path, out: Path, stem: str, **over) -> list[str]:
    cmd = midi_run._spawn_worker_cmd(
        audio, out, stem, midi_run.run_params(stem, "balanced"), mf.default_tempo()
    )
    for flag, value in over.items():
        cmd[cmd.index("--" + flag.replace("_", "-")) + 1] = str(value)
    return cmd


@pytest.mark.parametrize(
    "name,tone,stem,pitch",
    [
        # A 3 s sine at 220 Hz is A3, MIDI 57.
        ("sine220", lambda: _tone(220, 3), "piano", 57),
        # A 2 s piano-like A440 (six partials, decaying) is A4, MIDI 69.
        ("piano440", lambda: _tone(440, 2, partials=6, decay=1.5), "piano", 69),
    ],
)
def test_real_inference_finds_the_pitch(tmp_path, name, tone, stem, pitch):
    silence = np.zeros(SR // 2)
    audio = _wav(tmp_path / f"{name}.wav", np.concatenate([silence, tone(), silence]))
    out = tmp_path / f"{name}.mid"
    result = _run(_cmd(audio, out, stem), tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    events = _events(result.stdout)
    assert events[-1]["event"] == "done"
    pcts = [e["pct"] for e in events if e["event"] == "progress"]
    assert pcts == sorted(pcts) and pcts[-1] <= 100
    notes = _pitches(out)
    assert events[-1]["notes"] == len(notes) >= 1
    # The tone's own pitch is there and is the strongest, longest note; a
    # faint overtone may be heard as well.
    loudest = max(notes, key=lambda n: (n[2], n[1]))
    assert loudest[0] == pitch, notes


def test_a_bass_line_comes_out_one_note_at_a_time(tmp_path):
    """A1 then D2, each with strong partials: the bass preset's one voice
    (midi_notes.one_voice) keeps each fundamental and never two notes at once,
    where Basic Pitch alone also hears the octave above."""
    silence = np.zeros(SR // 2)
    line = np.concatenate(
        [silence, _tone(55.0, 1.5, partials=6, decay=0.5), _tone(73.42, 1.5, partials=6, decay=0.5)]
    )
    audio = _wav(tmp_path / "line.wav", np.concatenate([line, silence]))
    out = tmp_path / "line.mid"
    result = _run(_cmd(audio, out, "bass"), tmp_path)
    assert result.returncode == 0, result.stderr[-3000:]
    notes = mf.read_notes(out)["notes"]
    assert [n[2] for n in notes] == [33, 38], notes
    assert all(a[1] <= b[0] for a, b in zip(notes, notes[1:], strict=False))


def test_too_long_is_refused_before_anything_is_decoded_or_imported(tmp_path):
    marker = tmp_path / "engine-imported"
    fake = _fake_engine(tmp_path, marker=marker)
    audio = _wav(tmp_path / "three.wav", _tone(220, 3))
    result = _run(_cmd(audio, tmp_path / "x.mid", "bass", max_seconds=2, vendor=fake), tmp_path)
    assert result.returncode != 0
    assert _events(result.stdout) == [{"event": "error", "code": "too_long"}]
    assert not marker.exists()
    assert not (tmp_path / "x.mid").exists()


def test_unreadable_audio_is_no_audio(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all")
    result = _run(_cmd(bad, tmp_path / "x.mid", "piano"), tmp_path)
    assert _events(result.stdout) == [{"event": "error", "code": "no_audio"}]
    result = _run(_cmd(tmp_path / "missing.wav", tmp_path / "x.mid", "piano"), tmp_path)
    assert _events(result.stdout) == [{"event": "error", "code": "no_audio"}]


def _fake_engine(tmp_path: Path, *, marker: Path | None = None, fail: str = "") -> Path:
    """A stand-in basic_pitch with the real vendored mido beside it. It
    prints to stdout every way it can, logs at import like the real one, and
    records whether it could reach the network."""
    root = tmp_path / "fake_vendor"
    if root.exists():
        shutil.rmtree(root)
    pkg = root / "basic_pitch"
    pkg.mkdir(parents=True)
    shutil.copytree(mf.VENDOR_DIR / "mido", root / "mido")
    net = tmp_path / "network.txt"
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            f"""
            import logging, os, sys
            {"open(" + repr(str(marker)) + ", 'w').close()" if marker else ""}
            {"raise ImportError('broken engine')" if fail == "import" else ""}
            logging.warning("Tensorflow is not installed.")
            print("stray print at import")
            sys.__stdout__.write("stray write to the original stdout\\n")
            sys.__stdout__.flush()
            os.write(1, b"stray bytes on descriptor 1\\n")
            import socket
            results = []
            for attempt in (
                lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
                lambda: socket.socket().connect(("127.0.0.1", 9)),
                lambda: socket.getaddrinfo("example.com", 80),
            ):
                try:
                    attempt()
                    results.append("connected")
                except OSError as exc:
                    results.append("blocked" if "no network access" in str(exc) else repr(exc))
            open({str(net)!r}, "w").write(",".join(results))
            """
        ),
        encoding="utf-8",
    )
    (pkg / "constants.py").write_text(
        "AUDIO_N_SAMPLES = 43844\nAUDIO_SAMPLE_RATE = 22050\nFFT_HOP = 256\n", encoding="utf-8"
    )
    (pkg / "inference.py").write_text(
        textwrap.dedent(
            """
            import enum
            import numpy as np

            class Model:
                class MODEL_TYPES(enum.Enum):
                    ONNX = 1

                def __init__(self, path):
                    print("loading", path)
                    self.model_type = Model.MODEL_TYPES.ONNX
                    self.calls = 0

                def predict(self, x):
                    # Window 0 holds MIDI 60 from frame 10 to 20, window 1
                    # MIDI 64 from its frame 10 to 20, window 2 nothing.
                    print("predicting")
                    note = np.zeros((1, 40, 88))
                    onset = np.zeros((1, 40, 88))
                    if self.calls < 2:
                        pitch = (60, 64)[self.calls] - 21
                        note[0, 10:20, pitch] = 0.9
                        onset[0, 10, pitch] = 0.9
                    self.calls += 1
                    return {"note": note, "onset": onset, "contour": np.zeros((1, 40, 264))}

            def get_audio_input(path, overlap_len, hop_size):
                # Three windows' worth of samples: (n + overlap / 2) / hop = 3.
                for _ in range(3):
                    yield np.zeros((1, 43844, 1), dtype=np.float32), {}, 104652

            def unwrap_output(output, length, frames):
                return output.reshape(-1, output.shape[-1])
            """
        ),
        encoding="utf-8",
    )
    (pkg / "note_creation.py").write_text(
        textwrap.dedent(
            f"""
            import numpy as np

            def constrain_frequency(onsets, frames, max_freq, min_freq):
                return onsets, frames

            def get_infered_onsets(onsets, frames):
                print("making notes")
                {"raise RuntimeError('engine blew up')" if fail == "run" else ""}
                return onsets

            def model_frames_to_time(n_frames):
                return np.arange(n_frames) * 0.05
            """
        ),
        encoding="utf-8",
    )
    return root


def test_stdout_carries_json_lines_only_and_the_network_is_refused(tmp_path):
    fake = _fake_engine(tmp_path)
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    out = tmp_path / "a.mid"
    result = _run(_cmd(audio, out, "piano", vendor=fake), tmp_path, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    events = _events(result.stdout)  # fails on any line that is not one
    assert events[-1] == {"event": "done", "notes": 2, "seconds_audio": 1.0, "device": "cpu"}
    assert [e["pct"] for e in events if e["event"] == "progress"] == [1, 5, 33, 61, 90, 95]
    # Everything the engine printed went to stderr; its import warning did not.
    for stray in ("stray print", "stray write", "stray bytes", "loading", "making notes"):
        assert stray in result.stderr
    assert "Tensorflow is not installed" not in result.stderr
    assert (tmp_path / "network.txt").read_text() == "blocked,blocked,blocked"
    assert [n[0] for n in _pitches(out)] == [60, 64]


@pytest.mark.parametrize("fail,phase", [("import", {"phase": "import"}), ("run", {})])
def test_an_engine_failure_is_one_error_line(tmp_path, fail, phase):
    fake = _fake_engine(tmp_path, fail=fail)
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    result = _run(_cmd(audio, tmp_path / "a.mid", "piano", vendor=fake), tmp_path, timeout=120)
    assert result.returncode != 0
    assert _events(result.stdout)[-1] == {"event": "error", "code": "engine", **phase}
    assert not (tmp_path / "a.mid").exists()


def test_a_write_failure_is_io(tmp_path):
    fake = _fake_engine(tmp_path)
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    # A folder where the file should go: the open fails.
    out = tmp_path / "taken.mid"
    out.mkdir()
    result = _run(_cmd(audio, out, "piano", vendor=fake), tmp_path, timeout=120)
    assert _events(result.stdout)[-1] == {"event": "error", "code": "io"}


# -- the second piano engine, piano_hr (app/pipeline/piano_hr.py)


def _fake_piano_hr(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in piano_transcription_inference and a checkpoint for it, made
    with the real torch: the worker's own load (weights_only, strict), its
    batches and its post-processing call run as they do for the real model.
    It misbehaves as the BP stand-in does, and tries matplotlib and os.system."""
    import torch

    root = tmp_path / "fake_piano_vendor"
    pkg = root / "piano_transcription_inference"
    pkg.mkdir(parents=True)
    probe = tmp_path / "piano-probe.txt"
    (pkg / "__init__.py").write_text(
        textwrap.dedent(
            f"""
            import os, socket, sys
            print("stray print at import")
            os.write(1, b"stray bytes on descriptor 1\\n")
            results = []
            for attempt in (
                lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
                lambda: os.system("echo reached"),
                lambda: __import__("matplotlib"),
            ):
                try:
                    attempt()
                    results.append("reached")
                except ImportError:
                    results.append("blocked")
                except OSError as exc:
                    results.append("blocked" if "no network access" in str(exc) else repr(exc))
            open({str(probe)!r}, "w").write(",".join(results))
            """
        ),
        encoding="utf-8",
    )
    (pkg / "models.py").write_text(
        textwrap.dedent(
            """
            import torch

            class Note_pedal(torch.nn.Module):
                def __init__(self, frames_per_second, classes_num):
                    super().__init__()
                    self.note_model = torch.nn.Linear(1, 1)
                    self.pedal_model = torch.nn.Linear(1, 1)

                def forward(self, x):
                    print("batch of", x.shape[0])
                    n = x.shape[0]
                    # 9 frames a segment: 8 after the last is dropped, in quarters.
                    zeros = torch.zeros(n, 9, 88)
                    return {"frame_output": zeros, "reg_onset_output": zeros + 0.5,
                            "pedal_frame_output": torch.zeros(n, 9, 1)}
            """
        ),
        encoding="utf-8",
    )
    (pkg / "utilities.py").write_text(
        textwrap.dedent(
            """
            class RegressionPostProcessor:
                def __init__(self, frames_per_second, classes_num, onset_threshold,
                             offset_threshold, frame_threshold, pedal_offset_threshold):
                    print("thresholds", onset_threshold, offset_threshold, frame_threshold)

                def output_dict_to_midi_events(self, output):
                    assert output["reg_onset_output"].shape[1] == 88
                    output["added"] = True
                    notes = [
                        {"onset_time": 0.5, "offset_time": 0.9, "midi_note": 60, "velocity": 100},
                        {"onset_time": 1.0, "offset_time": 1.2, "midi_note": 64, "velocity": 64},
                    ]
                    return notes, [{"onset_time": 0.8, "offset_time": 1.5}]
            """
        ),
        encoding="utf-8",
    )
    model = torch.nn.Module()
    model.note_model = torch.nn.Linear(1, 1)
    model.pedal_model = torch.nn.Linear(1, 1)
    checkpoint = tmp_path / "fake.pth"
    torch.save(
        {
            "model": {
                "note_model": model.note_model.state_dict(),
                "pedal_model": model.pedal_model.state_dict(),
            }
        },
        checkpoint,
    )
    return root, checkpoint


def _hr_cmd(audio: Path, out: Path, vendor: Path, checkpoint: Path, **over) -> list[str]:
    stem = over.pop("stem", "piano")
    cmd = midi_run._spawn_worker_cmd(
        audio,
        out,
        stem,
        midi_run.run_params("piano", "balanced", "piano_hr"),
        mf.default_tempo(),
        "piano_hr",
        over.pop("device", "cpu"),
    )
    cmd[cmd.index("--piano-vendor") + 1] = str(vendor)
    cmd[cmd.index("--checkpoint") + 1] = str(checkpoint)
    for flag, value in over.items():
        cmd[cmd.index("--" + flag.replace("_", "-")) + 1] = str(value)
    return cmd


def test_piano_hr_runs_in_batches_with_stdout_claimed_and_the_network_refused(tmp_path):
    vendor, checkpoint = _fake_piano_hr(tmp_path)
    # 31 s at 16 kHz: four 10 s segments 5 s apart after padding to 40 s,
    # wait seven, run two at a time: four batches.
    audio = _wav(tmp_path / "a.wav", _tone(220, 31))
    out = tmp_path / "a.mid"
    result = _run(_hr_cmd(audio, out, vendor, checkpoint, batch=2), tmp_path, timeout=180)
    assert result.returncode == 0, result.stderr[-3000:]
    events = _events(result.stdout)
    assert events[-1] == {"event": "done", "notes": 2, "seconds_audio": 31.0, "device": "cpu"}
    pcts = [e["pct"] for e in events if e["event"] == "progress"]
    assert pcts == sorted(pcts) and pcts[0] == 1 and pcts[-1] == 95
    assert result.stderr.count("batch of 2") == 3 and result.stderr.count("batch of 1") == 1
    assert "stray print" in result.stderr and "stray bytes" in result.stderr
    p = midi_run.run_params("piano", "balanced", "piano_hr")
    thresholds = f"{p['onset_threshold']} {p['offset_threshold']} {p['frame_threshold']}"
    assert f"thresholds {thresholds}" in result.stderr
    # The socket, a shell command and matplotlib are all refused.
    assert (tmp_path / "piano-probe.txt").read_text() == "blocked,blocked,blocked"
    # Both notes held by the pedal to 1.5 s; velocity from the engine's 0..127.
    notes = mf.read_notes(out)["notes"]
    assert [(n[0], n[1], n[2]) for n in notes] == [(0.5, 1.5, 60), (1.0, 1.5, 64)]
    assert [n[3] for n in notes] == [mf.velocity(100 / 127), mf.velocity(64 / 127)]


def test_piano_hr_refuses_the_bass_and_a_missing_checkpoint(tmp_path):
    vendor, checkpoint = _fake_piano_hr(tmp_path)
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    for cmd in (
        _hr_cmd(audio, tmp_path / "a.mid", vendor, checkpoint, stem="bass"),
        _hr_cmd(audio, tmp_path / "a.mid", vendor, tmp_path / "missing.pth"),
    ):
        result = _run(cmd, tmp_path, timeout=120)
        assert result.returncode != 0
        assert _events(result.stdout)[-1] == {"event": "error", "code": "engine"}
    assert not (tmp_path / "a.mid").exists()
    # A file that is not a checkpoint of this model fails the load, not the import.
    (tmp_path / "junk.pth").write_bytes(b"not a checkpoint")
    result = _run(_hr_cmd(audio, tmp_path / "a.mid", vendor, tmp_path / "junk.pth"), tmp_path)
    assert _events(result.stdout)[-1] == {"event": "error", "code": "engine"}


def test_piano_hr_without_its_package_is_an_import_failure(tmp_path):
    _vendor, checkpoint = _fake_piano_hr(tmp_path)
    empty = tmp_path / "empty_vendor"
    empty.mkdir()
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    result = _run(_hr_cmd(audio, tmp_path / "a.mid", empty, checkpoint), tmp_path, timeout=120)
    assert _events(result.stdout)[-1] == {"event": "error", "code": "engine", "phase": "import"}


def test_segments_overlap_by_half_and_join_on_their_middle_halves():
    """piano_hr.enframe and deframe, PianoTranscription's own steps: 10 s
    segments 5 s apart, then the first three quarters of the first segment,
    the middle half of each other one and the last three quarters of the
    last, each segment's final frame dropped first."""
    from app.pipeline import piano_hr

    x = np.arange(40, dtype=float)[None, :]
    segments = piano_hr.enframe(x, 10)
    assert segments.shape == (7, 10)
    assert [int(s[0]) for s in segments] == [0, 5, 10, 15, 20, 25, 30]
    with pytest.raises(AssertionError):
        piano_hr.enframe(np.zeros((1, 15)), 10)
    # Frame f of segment i holds the time it stands for: i * 4 + f, at 9
    # frames a segment (8 after the drop) and half of 8 apart.
    out = np.array([[[i * 4 + f] for f in range(9)] for i in range(7)], dtype=float)
    joined = piano_hr.deframe(out)
    assert joined[:, 0].tolist() == list(range(32))
    one = np.ones((1, 9, 88))
    assert piano_hr.deframe(one).shape == (9, 88)


def test_the_device_is_the_gpu_only_with_room_on_it(monkeypatch):
    import torch

    from app.pipeline import midi_worker

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda: (3000 * 2**20, 10_000 * 2**20))
    assert midi_worker.pick_device("cuda", 2048) == "cuda"
    assert midi_worker.pick_device("cpu", 2048) == "cpu"
    assert midi_worker.pick_device("cuda", 4096) == "cpu"

    def unreadable():
        raise RuntimeError("CUDA error: unknown error")

    monkeypatch.setattr(torch.cuda, "mem_get_info", unreadable)
    assert midi_worker.pick_device("cuda", 2048) == "cpu"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert midi_worker.pick_device("cuda", 0) == "cpu"


def _real_piano_hr_checkpoint() -> Path | None:
    """The real checkpoint, when this machine has it and it is the pinned
    file: named by STEMDECK_TEST_PIANO_HR_CHECKPOINT, or the benchmark's copy."""
    from app.pipeline import piano_hr

    named = os.environ.get("STEMDECK_TEST_PIANO_HR_CHECKPOINT")
    candidates = [Path(named)] if named else []
    candidates += [
        ROOT / "data" / "midi-bench" / "app-data" / "data" / "models" / "piano_hr"
        / "CRNN_note_F1_0.9677_pedal_F1_0.9186.pth",
        ROOT / "data" / "midi-bench" / "engines" / "pti-ckpt" / "note_F1_0.9677.pth",
    ]  # fmt: skip
    for path in candidates:
        if path.is_file() and piano_hr.checkpoint_ok(path):
            return path
    return None


def test_real_piano_hr_inference_finds_the_pitch(tmp_path):
    """The real model, on the CPU, on a piano-like A440 (six partials,
    decaying): A4, MIDI 69. Skipped where the 172 MB checkpoint is not here."""
    from app.pipeline import piano_hr

    checkpoint = _real_piano_hr_checkpoint()
    if checkpoint is None:
        pytest.skip("the piano_hr checkpoint is not on this machine")
    silence = np.zeros(SR // 2)
    audio = _wav(
        tmp_path / "a440.wav",
        np.concatenate([silence, _tone(440, 2, partials=6, decay=1.5), silence]),
    )
    out = tmp_path / "a440.mid"
    result = _run(_hr_cmd(audio, out, piano_hr.VENDOR_DIR, checkpoint), tmp_path, timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    events = _events(result.stdout)
    assert events[-1]["event"] == "done" and events[-1]["device"] == "cpu"
    notes = _pitches(out)
    loudest = max(notes, key=lambda n: (n[2], n[1]))
    assert loudest[0] == 69, notes


# -- the third engine (ymt3)


def _ymt3_cmd(audio: Path, out: Path, stem: str, weights: Path, device: str = "cpu") -> list[str]:
    cmd = midi_run._spawn_worker_cmd(
        audio,
        out,
        stem,
        midi_run.run_params(stem, "balanced", "ymt3"),
        mf.default_tempo(),
        "ymt3",
        device,
    )
    cmd[cmd.index("--checkpoint") + 1] = str(weights)
    return cmd


def test_each_engine_refuses_a_stem_it_is_not_offered_for(tmp_path):
    """Basic Pitch hears no drums; YourMT3+ is not offered for the vocals;
    piano_hr knows only the piano. Refused before any engine is imported."""
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    weights = tmp_path / "w.safetensors"
    weights.write_bytes(b"x")
    drums = midi_run._spawn_worker_cmd(audio, tmp_path / "a.mid", "drums", {}, mf.default_tempo())
    for cmd in (drums, _ymt3_cmd(audio, tmp_path / "a.mid", "vocals", weights)):
        result = _run(cmd, tmp_path, timeout=120)
        assert result.returncode == 4
        assert _events(result.stdout) == [{"event": "error", "code": "engine"}]
    assert not (tmp_path / "a.mid").exists()


def test_ymt3_refuses_missing_weights_and_a_file_that_is_not_them(tmp_path):
    audio = _wav(tmp_path / "a.wav", _tone(220, 1))
    result = _run(_ymt3_cmd(audio, tmp_path / "a.mid", "bass", tmp_path / "none"), tmp_path)
    assert _events(result.stdout)[-1] == {"event": "error", "code": "engine"}
    # A file that is not a weights file fails the load, not the import.
    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"\x10\x00\x00\x00\x00\x00\x00\x00{}              ")
    result = _run(_ymt3_cmd(audio, tmp_path / "a.mid", "bass", junk), tmp_path, timeout=300)
    assert _events(result.stdout)[-1] == {"event": "error", "code": "engine"}, result.stderr[-2000:]
    assert not (tmp_path / "a.mid").exists()


def test_a_module_from_outside_the_vendored_folders_is_refused(tmp_path, monkeypatch):
    """The ymt3 engine's top-level names (model, utils, config, transformers)
    are generic: one installed elsewhere would run in its place without a
    word, so the worker checks where each came from."""
    import types

    from app.pipeline import midi_worker

    vendored = tmp_path / "vendored"
    (vendored / "model").mkdir(parents=True)
    inside = types.ModuleType("model")
    inside.__file__ = str(vendored / "model" / "__init__.py")
    monkeypatch.setitem(sys.modules, "model", inside)
    midi_worker._from_vendored(("model",), [vendored])
    outside = types.ModuleType("model.thing")
    outside.__file__ = str(tmp_path / "elsewhere" / "thing.py")
    monkeypatch.setitem(sys.modules, "model.thing", outside)
    with pytest.raises(ImportError, match="did not come from the vendored engine"):
        midi_worker._from_vendored(("model",), [vendored])


def _real_ymt3_weights() -> Path | None:
    """The real weights file, when this machine has it and it is the pinned
    file: named by STEMDECK_TEST_YMT3_WEIGHTS, or the benchmark's copy."""
    from app.core.config import YMT3_WEIGHTS_RELPATH
    from app.pipeline import ymt3

    named = os.environ.get("STEMDECK_TEST_YMT3_WEIGHTS")
    candidates = [Path(named)] if named else []
    candidates.append(ROOT / "data" / "ymt3-bench-models" / Path(YMT3_WEIGHTS_RELPATH))
    for path in candidates:
        if path.is_file() and ymt3.WEIGHTS.ok(path):
            return path
    return None


def test_real_ymt3_inference_matches_the_engine_comparison(tmp_path):
    """The real model, on the CPU, through the worker, on 20 s of a piano
    separated from a Slakh song: every note in its file is one the engine
    comparison's own run of YourMT3+ found there (data/midi-bench, the same
    notes to the millisecond, class filter and all). Skipped where the
    188 MB weights or the benchmark's audio are not on this machine."""
    weights = _real_ymt3_weights()
    bench = ROOT / "data" / "midi-bench" / "eng"
    clip = bench / "crops" / "slakh-test" / "Track01880" / "piano.wav"
    ref = bench / "out" / "ymt3" / "slakh__Track01880__piano__sep.json"
    if weights is None or not clip.is_file() or not ref.is_file():
        pytest.skip("the ymt3 weights or the benchmark's audio are not on this machine")
    y, sr = sf.read(str(clip), dtype="float32", always_2d=True)
    # A whole number of the model's 2.048 s segments, so they cut alike.
    seconds = 10 * 32767 / 16000
    part = _wav(tmp_path / "piano.wav", y[: int(seconds * sr)].mean(axis=1))
    assert sr == SR
    out = tmp_path / "piano.mid"
    result = _run(_ymt3_cmd(part, out, "piano", weights), tmp_path, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    events = _events(result.stdout)
    assert events[-1]["event"] == "done" and events[-1]["device"] == "cpu"
    notes = mf.read_notes(out)["notes"]
    expected = [
        n for n in json.loads(ref.read_text(encoding="utf-8"))["notes"]
        if not n[5] and 0 <= n[4] < 8 and n[0] < seconds - 0.1
    ]  # fmt: skip
    assert notes and len(notes) <= len(expected)
    for start, _end, pitch, _vel in notes:
        assert any(p == pitch and abs(s - start) < 0.003 for s, _e, p, *_ in expected), start
