"""The Basic Pitch worker (app/pipeline/midi_worker.py) as a subprocess.

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
    assert events[-1] == {"event": "done", "notes": 2, "seconds_audio": 1.0}
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
