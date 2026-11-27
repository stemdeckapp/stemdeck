"""YourMT3+ as a MIDI engine, everything but the model itself: the weights
file format (app/core/tensor_file.py), a pinned file's checks
(app/core/pinned_file.py), the checkpoint's conversion without running its
pickle (app/pipeline/ymt3.py, ymt3_convert.py), which notes a stem's file
keeps, and the drums' file. test_midi_api.py runs a whole ymt3 run through
the routes; test_ymt3_vendor.py builds the model.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import types

import pytest
import torch

from app.core import tensor_file
from app.core.pinned_file import PinnedFile, sha256_of
from app.pipeline import midi_file, ymt3, ymt3_convert

# -- the weights file


def _tensors():
    return {
        "b.weight": torch.arange(6, dtype=torch.float32).reshape(2, 3) / 7,
        "a.bias": torch.tensor([1.5, -2.0], dtype=torch.float32),
        "steps": torch.tensor([3, 4, 5], dtype=torch.int64),
        "empty": torch.zeros((0, 4), dtype=torch.float32),
    }


def test_the_weights_file_reads_back_exactly(tmp_path):
    path = tmp_path / "w.safetensors"
    tensor_file.write(path, _tensors(), {"format": "pt"})
    back, meta = tensor_file.read(path)
    assert meta == {"format": "pt"}
    assert set(back) == set(_tensors())
    for name, t in _tensors().items():
        assert back[name].dtype == t.dtype and torch.equal(back[name], t), name


def test_the_same_tensors_always_give_the_same_bytes(tmp_path):
    """Sorted names, a fixed header: the SHA-256 a run checks can be pinned."""
    first, second = tmp_path / "1.safetensors", tmp_path / "2.safetensors"
    tensor_file.write(first, _tensors())
    shuffled = dict(reversed(list(_tensors().items())))
    tensor_file.write(second, shuffled)
    assert first.read_bytes() == second.read_bytes()
    data = first.read_bytes()
    (length,) = struct.unpack("<Q", data[:8])
    assert length % 8 == 0
    header = json.loads(data[8 : 8 + length])
    assert list(header) == sorted(header)


def test_the_file_is_the_safetensors_format(tmp_path):
    """The layout the format's own specification gives: what one float32
    tensor's file must be, byte for byte."""
    path = tmp_path / "w.safetensors"
    tensor_file.write(path, {"x": torch.tensor([1.0, 2.0])})
    raw = b'{"x":{"data_offsets":[0,8],"dtype":"F32","shape":[2]}}'
    raw += b" " * (-len(raw) % 8)
    assert path.read_bytes() == struct.pack("<Q", len(raw)) + raw + struct.pack("<2f", 1.0, 2.0)


@pytest.mark.parametrize(
    "header, body",
    [
        (b"not json", b""),
        (b'{"x":{"dtype":"F64","shape":[1],"data_offsets":[0,8]}}', b"\0" * 8),
        (b'{"x":{"dtype":"F32","shape":[2],"data_offsets":[0,4]}}', b"\0" * 4),
        (b'{"x":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}', b"\0" * 8),
        (
            b'{"x":{"dtype":"F32","shape":[1],"data_offsets":[0,4]},'
            b'"y":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}',
            b"\0" * 4,
        ),
        (b'{"x":{"dtype":"F32","shape":[-1],"data_offsets":[0,4]}}', b"\0" * 4),
        (b'{"__metadata__":{"a":1}}', b""),
        (b"[1, 2]", b""),
    ],
)
def test_a_file_that_breaks_the_format_is_refused(tmp_path, header, body):
    path = tmp_path / "bad.safetensors"
    path.write_bytes(struct.pack("<Q", len(header)) + header + body)
    with pytest.raises(tensor_file.TensorFileError):
        tensor_file.read(path)


def test_a_header_longer_than_the_file_is_refused(tmp_path):
    path = tmp_path / "bad.safetensors"
    path.write_bytes(struct.pack("<Q", 10_000) + b"{}")
    with pytest.raises(tensor_file.TensorFileError):
        tensor_file.read(path)


# -- a pinned file


def test_a_pinned_file_is_checked_by_size_then_sha256(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "MODELS_DIR", tmp_path)
    body = b"the pinned bytes"
    pinned = PinnedFile(None, hashlib.sha256(body).hexdigest(), len(body), "x/model.bin")
    assert pinned.path() == tmp_path / "x" / "model.bin"
    assert pinned.state() == "not_downloaded" and not pinned.ok()
    pinned.path().parent.mkdir()
    pinned.path().write_bytes(b"short")
    assert pinned.state() == "partial" and not pinned.ok()
    assert pinned.remove_bad() and not pinned.path().exists()
    pinned.path().write_bytes(b"X" * len(body))
    # The right size, other bytes: not the file.
    assert pinned.state() == "downloaded" and not pinned.ok()
    pinned.path().write_bytes(body)
    assert pinned.ok() and not pinned.remove_bad() and pinned.path().exists()
    assert sha256_of(pinned.path()) == pinned.sha256


@pytest.mark.parametrize(
    "fields",
    [
        {"url": "http://example.com/x"},
        {"url": "https://example.com/a%20b"},
        {"sha256": "ABC"},
        {"size": 0},
        {"relpath": "../x"},
        {"relpath": "a//b"},
        {"relpath": "C:/x"},
        {"relpath": "a\\b"},
    ],
)
def test_a_pinned_file_refuses_what_could_escape_or_lie(fields):
    good = {"url": None, "sha256": "a" * 64, "size": 1, "relpath": "a/b"}
    with pytest.raises(ValueError):
        PinnedFile(**{**good, **fields})


# -- the checkpoint, read without running its pickle

RAN: list = []


def _checkpoint(path, extra_global: str | None = None) -> dict:
    """A Lightning-shaped checkpoint whose hyper_parameters hold objects of
    the classes CHECKPOINT_GLOBALS names, each of which would record that it
    ran if it were ever built or given its state; plus, when asked, one more
    class the list does not name."""

    class Tracer:
        def __init__(self, *a, **k):
            RAN.append("init")

        def __setstate__(self, state):
            RAN.append("setstate")

        def __reduce__(self):
            return (type(self), ())

    added = []
    for name in (*ymt3.CHECKPOINT_GLOBALS[5:], *([extra_global] if extra_global else [])):
        module_name, cls_name = name.rsplit(".", 1)
        parts = module_name.split(".")
        for depth in range(1, len(parts) + 1):
            prefix = ".".join(parts[:depth])
            if prefix not in sys.modules:
                package = types.ModuleType(prefix)
                package.__path__ = []
                sys.modules[prefix] = package
                added.append(prefix)
        module = sys.modules[module_name]
        cls = type(cls_name, (Tracer,), {"__module__": module_name, "__qualname__": cls_name})
        setattr(module, cls_name, cls)
    state = {"model.w": torch.ones(2, 2), "model.pitchshift.k": torch.zeros(1)}
    objects = [getattr(sys.modules[n.rsplit(".", 1)[0]], n.rsplit(".", 1)[1])() for n in
               (*ymt3.CHECKPOINT_GLOBALS[5:], *([extra_global] if extra_global else []))]  # fmt: skip
    try:
        torch.save(
            {
                "state_dict": state,
                "optimizer_states": [{"m": torch.ones(3)}],
                "hyper_parameters": {"task_manager": objects},
            },
            path,
        )
    finally:
        for name in added:
            del sys.modules[name]
    RAN.clear()
    return state


def test_the_checkpoint_gives_its_tensors_and_runs_nothing(tmp_path):
    path = tmp_path / "last.ckpt"
    state = _checkpoint(path)
    tensors = ymt3.checkpoint_tensors(path)
    # The model's tensors only: the pitch-shift layer and the optimizer stay.
    assert list(tensors) == ["model.w"] and torch.equal(tensors["model.w"], state["model.w"])
    assert RAN == []
    # None of the classes it names had to be importable.
    assert not any(n.startswith("utils.") for n in sys.modules)


def test_a_checkpoint_with_any_other_object_is_refused(tmp_path):
    path = tmp_path / "last.ckpt"
    _checkpoint(path, extra_global="utils.evil.Payload")
    with pytest.raises(Exception, match="(?i)weights only|unsupported global"):
        ymt3.checkpoint_tensors(path)
    assert RAN == []


def test_the_conversion_puts_the_file_in_place_only_when_it_is_the_pinned_one(tmp_path):
    ckpt = tmp_path / "last.ckpt"
    _checkpoint(ckpt)
    events: list = []

    def emit(event, **fields):
        events.append({"event": event, **fields})

    probe = tmp_path / "probe.safetensors"
    ymt3.convert_checkpoint(ckpt, probe)
    sha, size = sha256_of(probe), probe.stat().st_size
    out = tmp_path / "models" / "ymt3" / "w.safetensors"
    assert ymt3_convert.convert(ckpt, out, sha, size, emit) == 0
    assert out.read_bytes() == probe.read_bytes() and events[-1] == {"event": "done", "tensors": 1}
    # Another pin: what it made is not that file, so nothing is left.
    out.unlink()
    events.clear()
    assert ymt3_convert.convert(ckpt, out, "0" * 64, size, emit) == 3
    assert events[-1] == {"event": "error", "code": "convert"}
    assert not out.exists() and list(out.parent.iterdir()) == []


def test_a_file_that_is_not_a_checkpoint_does_not_convert(tmp_path):
    ckpt = tmp_path / "last.ckpt"
    ckpt.write_bytes(b"not a checkpoint at all")
    events: list = []
    out = tmp_path / "w.safetensors"
    code = ymt3_convert.convert(ckpt, out, "0" * 64, 1, lambda e, **f: events.append((e, f)))
    assert code == 3 and events[-1] == ("error", {"code": "convert"}) and not out.exists()


def test_the_weights_state_follows_the_files(tmp_path):
    """conftest's _isolate_ymt3 puts MODELS_DIR in tmp_path: a checkpoint
    waiting for its conversion is a partial model; the weights file is it."""
    assert ymt3.model_state() == "not_downloaded"
    ymt3.checkpoint_path().parent.mkdir(parents=True)
    ymt3.checkpoint_path().write_bytes(b"x")
    assert ymt3.model_state() == "partial"
    ymt3.checkpoint_path().unlink()
    with open(ymt3.weights_path(), "wb") as f:
        f.truncate(ymt3.WEIGHTS.size)
    assert ymt3.model_state() == "downloaded"
    # The right size is not the right file.
    assert not ymt3.weights_ok()


# -- which notes a stem's file keeps


def _note(program, is_drum=False, pitch=60, onset=1.0):
    return (onset, onset + 0.5, pitch, 1, program, is_drum)


NOTES = [
    _note(0),  # Acoustic Grand Piano
    _note(4),  # Electric Piano 1
    _note(26),  # Electric Guitar (jazz)
    _note(29),
    _note(29),
    _note(33),  # Electric Bass (finger)
    _note(48),  # String Ensemble 1
    _note(56),  # Trumpet
    _note(100),  # the model's singing voice
    _note(128, True, 36),  # a kick
    _note(128, True, 38),  # a snare
]


@pytest.mark.parametrize(
    "stem, programs, file_program",
    [
        ("piano", [0, 4], 0),
        ("guitar", [26, 29, 29], 29),
        ("bass", [33], 33),
        ("other", [48, 56], 48),
        ("vocals", [100], 100),
    ],
)
def test_a_stem_keeps_its_own_instrument_class(stem, programs, file_program):
    events, program = ymt3.stem_notes(NOTES, stem)
    kept = [n[4] for n in NOTES if not n[5] and ymt3.keeps(stem, n[4], False)]
    assert kept == programs and len(events) == len(programs)
    assert program == file_program
    assert all(e[3] == ymt3.YMT3_NOTE_AMPLITUDE and e[4] is None for e in events)


def test_the_drums_keep_the_hits_only():
    events, program = ymt3.stem_notes(NOTES, "drums")
    assert [e[2] for e in events] == [36, 38] and program is None


def test_no_note_of_the_class_is_an_empty_file_with_the_stems_own_program():
    assert ymt3.stem_notes(NOTES[:2], "guitar") == ([], None)


def test_the_segments_are_slice_padded_arrays_arithmetic():
    """utils/audio.py slice_padded_array(x, L, L): ceil(n / L) segments, the
    last padded with zeros, no overlap."""
    import numpy as np

    n = ymt3.SEGMENT_SAMPLES * 2 + 5
    audio = np.arange(n, dtype=np.float32)[None, :]
    segs = ymt3.segments(audio)
    assert segs.shape == (3, 1, ymt3.SEGMENT_SAMPLES) and segs.dtype == np.float32
    assert segs[1, 0, 0] == ymt3.SEGMENT_SAMPLES and segs[2, 0, 4] == n - 1
    assert not segs[2, 0, 5:].any()
    assert ymt3.segments(audio[:, : ymt3.SEGMENT_SAMPLES]).shape[0] == 1
    assert ymt3.segments(audio[:, :10]).shape[0] == 1
    with pytest.raises(RuntimeError):
        ymt3.segments(audio[:, :0])


# -- the drums' file


@pytest.fixture
def mido():
    """The vendored mido, on sys.path for this test only (as
    test_midi_file.py has it)."""
    path = str(midi_file.VENDOR_DIR)
    sys.path.insert(0, path)
    try:
        import mido as module

        yield module
    finally:
        sys.path.remove(path)


def test_the_drums_are_general_midi_percussion_on_channel_10(mido):
    notes = [(0.5, 0.51, 36, 0.8, None), (1.0, 1.01, 42, 0.8, None)]
    midi = midi_file.build_midi(notes, "drums", midi_file.default_tempo())
    track = midi.tracks[0]
    names = [m.name for m in track if m.type == "track_name"]
    assert names == ["Drums"]
    assert not [m for m in track if m.type == "program_change"]
    channels = {m.channel for m in track if m.type in ("note_on", "note_off")}
    assert channels == {9}
    buf = io.BytesIO()
    midi.save(file=buf)
    assert mido.MidiFile(file=io.BytesIO(buf.getvalue())).tracks[0]


def test_an_engine_named_program_is_the_files_program(mido):
    notes = [(0.5, 1.0, 60, 0.8, None)]
    for program, expected in ((40, 40), (None, 27), (128, 27), (True, 27)):
        midi = midi_file.build_midi(notes, "guitar", midi_file.default_tempo(), program)
        changes = [m for m in midi.tracks[0] if m.type == "program_change"]
        assert [(m.program, m.channel) for m in changes] == [(expected, 0)], program
