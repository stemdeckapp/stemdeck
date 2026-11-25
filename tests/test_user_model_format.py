"""The checks a user model's two files pass before anything runs them
(app/core/user_model_format.py): the format sniff, the zip and safetensors
guards, the safetensors reader, the YAML loader, the config whitelist and the
parameter count. Pure: no network, and torch only where a test says so."""

from __future__ import annotations

import importlib.util
import inspect
import json
import struct
import zipfile

import numpy as np
import pytest

from app.core import user_model_format as fmt
from app.core.config import MAX_USER_MODEL_PARAMS
from app.pipeline.mel_roformer import MODEL_KWARGS

HAS_TORCH = importlib.util.find_spec("torch") is not None
HAS_SEPARATOR = HAS_TORCH and importlib.util.find_spec("audio_separator") is not None
KIM_PARAMS = 228_202_852  # a real build of the class with Kim's kwargs


def _kim_model() -> dict:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in MODEL_KWARGS.items()}


def _kim_doc(**model_changes) -> dict:
    model = {**_kim_model(), **model_changes}
    return {
        "audio": {"chunk_size": 352_800, "num_channels": 2, "sample_rate": 44_100},
        "model": model,
        "training": {"instruments": ["vocals", "other"], "target_instrument": "vocals"},
        "inference": {"num_overlap": 2, "batch_size": 2},
    }


def _bs_model(**changes) -> dict:
    bands = [2] * 24 + [4] * 12 + [12] * 8 + [24] * 8 + [48] * 8 + [128, 129]
    bands[-1] += 1025 - sum(bands)
    return {"dim": 32, "depth": 1, "stereo": True, "freqs_per_bands": bands, **changes}


def _refused(code: str, fn, *args, **kwargs) -> None:
    with pytest.raises(fmt.Refused) as info:
        fn(*args, **kwargs)
    assert info.value.code == code


# ── safetensors files built by hand ──


def _st_bytes(tensors: dict[str, tuple[str, list[int], bytes]], *, header: dict | None = None):
    offset = 0
    head: dict = {}
    data = b""
    for name, (dtype, shape, raw) in tensors.items():
        head[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + len(raw)]}
        offset += len(raw)
        data += raw
    if header is not None:
        head = header
    blob = json.dumps(head).encode()
    return struct.pack("<Q", len(blob)) + blob + data


def _write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _bf16(values: np.ndarray) -> bytes:
    as_int = values.astype(np.float32).view(np.uint32)
    return (as_int >> 16).astype(np.uint16).tobytes()


# ── the format sniff ──


def test_a_zip_torch_checkpoint_is_torch(tmp_path):
    path = tmp_path / "m.bin"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("archive/data.pkl", b"\x80\x02}q\x00.")
        zf.writestr("archive/data/0", b"\x00" * 16)
    assert fmt.sniff_format(path) == "torch"


def test_a_safetensors_file_is_safetensors(tmp_path):
    raw = np.arange(4, dtype=np.float32).tobytes()
    path = _write(tmp_path, "m.bin", _st_bytes({"a.weight": ("F32", [4], raw)}))
    assert fmt.sniff_format(path) == "safetensors"


@pytest.mark.parametrize(
    "data",
    [
        b"\x80\x02}q\x00(X\x01\x00\x00\x00aq\x01K\x01u.",  # a legacy pickle
        b"\x08\x07\x12\x07pytorch\x1a\x05" + b"\x00" * 64,  # ONNX
        b"GGUF\x03\x00\x00\x00" + b"\x00" * 64,
        b"<!doctype html><html><body>Sign in</body></html>",
    ],
)
def test_anything_else_is_bad_format(tmp_path, data):
    _refused("bad_format", fmt.sniff_format, _write(tmp_path, "m.bin", data))


def test_an_empty_file_is_empty(tmp_path):
    _refused("empty", fmt.sniff_format, _write(tmp_path, "m.bin", b""))


# ── the zip guard ──


def _zip(tmp_path, names: list[str]):
    path = tmp_path / "m.ckpt"
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return path


@pytest.mark.parametrize(
    "names",
    [
        ["/abs/data.pkl"],
        ["archive/../data.pkl"],
        ["archive/data/0"],  # no data.pkl
        ["data.pkl"],  # not in a folder
        ["archive/sub/data.pkl"],
        ["c:/archive/data.pkl"],
    ],
)
def test_a_zip_that_is_not_a_plain_checkpoint_is_refused(tmp_path, names):
    _refused("bad_format", fmt.check_zip, _zip(tmp_path, names))


def test_a_zip_with_too_many_names_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "MAX_ZIP_ENTRIES", 3)
    path = _zip(tmp_path, ["archive/data.pkl", "archive/data/0", "archive/data/1", "a/b"])
    _refused("bad_format", fmt.check_zip, path)


def test_a_zip_too_big_unpacked_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "MAX_ZIP_TOTAL", 10)
    path = tmp_path / "m.ckpt"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("archive/data.pkl", b"\x00" * 100)
    _refused("bad_format", fmt.check_zip, path)


# ── the safetensors guard ──


def test_a_header_longer_than_the_file_is_refused(tmp_path):
    data = struct.pack("<Q", 10_000) + b"{}"
    _refused("bad_format", fmt.read_safetensors_header, _write(tmp_path, "m", data))


def test_a_ten_terabyte_header_claim_is_refused_before_any_read(tmp_path):
    data = struct.pack("<Q", 10 * 1024**4) + b"{" + b" " * 32
    _refused("bad_format", fmt.read_safetensors_header, _write(tmp_path, "m", data))


def _header(**entries) -> dict:
    return dict(entries)


@pytest.mark.parametrize(
    "header",
    [
        # overlapping spans
        {
            "a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
            "b": {"dtype": "F32", "shape": [2], "data_offsets": [4, 12]},
        },
        # a gap
        {"a": {"dtype": "F32", "shape": [1], "data_offsets": [4, 8]}},
        # a span that is not the shape's size
        {"a": {"dtype": "F32", "shape": [3], "data_offsets": [0, 8]}},
        # a dtype the reader does not take
        {"a": {"dtype": "I64", "shape": [1], "data_offsets": [0, 8]}},
        # a name with characters outside the whitelist
        {"a/../b": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]}},
        # a negative or boolean dimension
        {"a": {"dtype": "F32", "shape": [-2], "data_offsets": [0, 8]}},
        {"a": {"dtype": "F32", "shape": [True, 2], "data_offsets": [0, 8]}},
        # no tensors at all
        {},
    ],
)
def test_a_bad_safetensors_header_is_refused(tmp_path, header):
    data = _st_bytes({}, header=header) + b"\x00" * 8
    _refused("bad_format", fmt.read_safetensors_header, _write(tmp_path, "m", data))


def test_data_past_the_last_tensor_is_refused(tmp_path):
    raw = np.zeros(2, dtype=np.float32).tobytes()
    data = _st_bytes({"a": ("F32", [2], raw)}) + b"\x00"
    _refused("bad_format", fmt.read_safetensors_header, _write(tmp_path, "m", data))


def test_too_many_tensors_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(fmt, "MAX_TENSORS", 2)
    raw = np.zeros(1, dtype=np.float32).tobytes()
    tensors = {f"t{i}": ("F32", [1], raw) for i in range(3)}
    _refused("bad_format", fmt.read_safetensors_header, _write(tmp_path, "m", _st_bytes(tensors)))


def test_metadata_is_allowed_and_not_a_tensor(tmp_path):
    raw = np.zeros(2, dtype=np.float32).tobytes()
    header = {
        "__metadata__": {"format": "pt"},
        "a": {"dtype": "F32", "shape": [2], "data_offsets": [0, 8]},
    }
    path = _write(tmp_path, "m", _st_bytes({}, header=header) + raw)
    tensors, start = fmt.read_safetensors_header(path)
    assert set(tensors) == {"a"} and start == path.stat().st_size - 8


@pytest.mark.skipif(not HAS_TORCH, reason="torch is not installed")
def test_the_reader_round_trips_f32_f16_and_bf16(tmp_path):
    values = np.array([[0.5, -1.25, 3.0], [1e-3, 0.0, -7.5]], dtype=np.float32)
    path = _write(
        tmp_path,
        "m.safetensors",
        _st_bytes(
            {
                "f32": ("F32", [2, 3], values.tobytes()),
                "f16": ("F16", [2, 3], values.astype(np.float16).tobytes()),
                "bf16": ("BF16", [2, 3], _bf16(values)),
                "empty": ("F32", [0, 3], b""),
            }
        ),
    )
    out = fmt.load_safetensors(path)
    import torch

    assert all(t.dtype == torch.float32 for t in out.values())
    np.testing.assert_array_equal(out["f32"].numpy(), values)
    np.testing.assert_array_equal(out["f16"].numpy(), values.astype(np.float16).astype(np.float32))
    bf16 = (values.view(np.uint32) & 0xFFFF0000).view(np.float32)
    np.testing.assert_array_equal(out["bf16"].numpy(), bf16)
    assert tuple(out["empty"].shape) == (0, 3)


# ── the YAML loader ──

_KIM_YAML = b"""
audio:
  chunk_size: 352800
  num_channels: 2
  sample_rate: 44100
model:
  dim: 384
  depth: 6
  stereo: true
  num_stems: 1
  time_transformer_depth: 1
  freq_transformer_depth: 1
  num_bands: 60
  dim_head: 64
  heads: 8
  attn_dropout: 0
  ff_dropout: 0
  flash_attn: true
  dim_freqs_in: 1025
  sample_rate: 44100
  stft_n_fft: 2048
  stft_hop_length: 441
  stft_win_length: 2048
  stft_normalized: false
  mask_estimator_depth: 2
  multi_stft_resolution_loss_weight: 1.0
  multi_stft_resolutions_window_sizes: !!python/tuple
  - 4096
  - 2048
  - 1024
  - 512
  - 256
  multi_stft_hop_size: 147
  multi_stft_normalized: false
training:
  instruments:
  - vocals
  - other
  target_instrument: vocals
inference:
  batch_size: 2
  num_overlap: 2
"""


def test_a_real_config_with_a_python_tuple_parses_and_cleans():
    doc = fmt.parse_config(_KIM_YAML)
    assert doc["model"]["multi_stft_resolutions_window_sizes"] == [4096, 2048, 1024, 512, 256]
    cleaned = fmt.clean_config(doc, ["vocals"])
    assert cleaned.arch == "mel_band_roformer"
    assert cleaned.outputs == {"vocals": 0}
    assert cleaned.audio == {"chunk": 352_800, "overlap": 2, "batch": 2}
    assert cleaned.params == KIM_PARAMS
    # Training-only keys never reach the constructor.
    assert "attn_dropout" not in cleaned.kwargs
    assert "multi_stft_resolutions_window_sizes" not in cleaned.kwargs


def test_the_tuple_tag_is_added_to_a_subclass_only():
    import yaml

    fmt.parse_config(_KIM_YAML)
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("a: !!python/tuple [1, 2]")


def test_an_executable_tag_is_refused_and_nothing_runs(tmp_path):
    canary = tmp_path / "ran"
    data = f"model: !!python/object/apply:os.mkdir [{json.dumps(str(canary))}]\n".encode()
    _refused("bad_config", fmt.parse_config, data)
    assert not canary.exists()


@pytest.mark.parametrize(
    "data",
    [
        b"a: &x [1, 2]\nb: *x\n",  # anchor and alias
        b"a: &x 1\n",  # an anchor alone
        b"a: !!binary aGVsbG8=\n",
        b"a: !!set {x}\n",
        b"a: !custom 1\n",
        b"[1, 2]\n",  # not a mapping
        b"a: [" + b"[" * 13 + b"1" + b"]" * 13 + b"]\n",  # too deep
        b"a: 1\x00\n",
        b"a: \xff\xfe\n",  # not UTF-8
    ],
)
def test_a_dangerous_or_odd_yaml_is_refused(data):
    _refused("bad_config", fmt.parse_config, data)


def test_too_many_events_are_refused(monkeypatch):
    monkeypatch.setattr(fmt, "MAX_YAML_EVENTS", 20)
    _refused("bad_config", fmt.parse_config, ("a: [" + ", ".join(["1"] * 30) + "]\n").encode())


def test_a_config_over_the_limit_or_empty_is_refused():
    _refused("too_large", fmt.parse_config, b"a: 1\n" * 60_000)
    _refused("empty", fmt.parse_config, b"  \n")


def test_a_missing_yaml_package_reads_as_unavailable(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    _refused("unavailable", fmt.parse_config, b"a: 1\n")


# ── the whitelist ──


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"dim": 8}, "bad_config"),
        ({"dim": 2048}, "bad_config"),
        ({"depth": 0}, "bad_config"),
        ({"depth": 25}, "bad_config"),
        ({"heads": 33}, "bad_config"),
        ({"dim_head": 7}, "bad_config"),
        ({"dim_head": 63}, "bad_config"),  # odd: the rotary embedding needs it even
        ({"num_stems": 7}, "bad_config"),
        ({"num_bands": 4}, "bad_config"),
        ({"stft_n_fft": 3000}, "bad_config"),
        ({"stft_hop_length": 32}, "bad_config"),
        ({"stft_win_length": 4096}, "bad_config"),  # over n_fft
        ({"mask_estimator_depth": 5}, "bad_config"),
        ({"time_transformer_depth": 9}, "bad_config"),
        ({"flash_attn": 1}, "bad_config"),
        ({"dim": True}, "bad_config"),
        ({"dim": 384.0}, "bad_config"),
        ({"sage_attention": True}, "bad_config"),
        ({"use_torch_checkpoint": True}, "bad_config"),
        ({"attn_dropout": 0.9}, "bad_config"),
        ({"stereo": False}, "unsupported_audio"),
        ({"sample_rate": 48_000}, "unsupported_audio"),
        ({"freqs_per_bands": [512, 513]}, "unsupported_arch"),  # both arch keys
        ({"linear_attention": True}, "bad_config"),  # unknown key
        ({"dimm": 384}, "bad_config"),  # a typo is refused, never ignored
    ],
)
def test_a_model_block_out_of_range_is_refused(change, code):
    _refused(code, fmt.clean_config, _kim_doc(**change), ["vocals"])


def test_neither_arch_key_is_unsupported():
    model = _kim_model()
    del model["num_bands"]
    doc = {**_kim_doc(), "model": model}
    _refused("unsupported_arch", fmt.clean_config, doc, ["vocals"])


def test_a_missing_stereo_reads_as_mono_and_is_refused():
    model = _kim_model()
    del model["stereo"]
    _refused("unsupported_audio", fmt.clean_config, {**_kim_doc(), "model": model}, ["vocals"])


@pytest.mark.parametrize(
    ("audio", "code"),
    [
        ({"chunk_size": 352_800, "sample_rate": 48_000}, "unsupported_audio"),
        ({"chunk_size": 352_800, "num_channels": 1}, "unsupported_audio"),
        ({"sample_rate": 44_100}, "bad_config"),  # no chunk size
        ({"chunk_size": 1000}, "bad_config"),
        ({"chunk_size": 2_000_000}, "bad_config"),
        ("not a block", "bad_config"),
    ],
)
def test_an_audio_block_out_of_range_is_refused(audio, code):
    _refused(code, fmt.clean_config, {**_kim_doc(), "audio": audio}, ["vocals"])


def test_inference_defaults_and_ranges():
    doc = {**_kim_doc(), "inference": None}
    assert fmt.clean_config(doc, ["vocals"]).audio == {"chunk": 352_800, "overlap": 2, "batch": 1}
    _refused("bad_config", fmt.clean_config, {**doc, "inference": {"num_overlap": 9}}, ["vocals"])
    _refused("bad_config", fmt.clean_config, {**doc, "inference": {"batch_size": 5}}, ["vocals"])


def test_a_bs_config_needs_bands_that_add_up_to_the_stft():
    doc = {"audio": {"chunk_size": 485_100}, "model": _bs_model(), "training": None}
    cleaned = fmt.clean_config(doc, ["vocals"])
    assert cleaned.arch == "bs_roformer"
    bad = _bs_model()
    bad["freqs_per_bands"][0] += 1
    _refused("bad_config", fmt.clean_config, {**doc, "model": bad}, ["vocals"])
    _refused(
        "bad_config",
        fmt.clean_config,
        {**doc, "model": _bs_model(freqs_per_bands=[1025])},
        ["vocals"],
    )


def test_a_bs_config_with_a_sample_rate_drops_it():
    doc = {"audio": {"chunk_size": 485_100}, "model": _bs_model(sample_rate=44_100)}
    assert "sample_rate" not in fmt.clean_config(doc, ["vocals"]).kwargs


def test_linear_attention_is_refused():
    doc = {"audio": {"chunk_size": 485_100}, "model": _bs_model(linear_transformer_depth=1)}
    _refused("bad_config", fmt.clean_config, doc, ["vocals"])


# ── the outputs ──


def _multi(instruments, num_stems, **training) -> dict:
    return {
        "audio": {"chunk_size": 485_100},
        "model": _bs_model(num_stems=num_stems),
        "training": {"instruments": instruments, **training},
    }


def test_instruments_map_each_declared_stem_to_its_output():
    doc = _multi(["bass", "drums", "other", "vocals", "guitar", "piano"], 6)
    cleaned = fmt.clean_config(doc, ["drums", "piano", "vocals"])
    assert cleaned.outputs == {"drums": 1, "piano": 5, "vocals": 3}


def test_instrument_names_are_matched_in_lower_case():
    doc = _multi(["Vocals", "Instrumental"], 2)
    assert fmt.clean_config(doc, ["vocals"]).outputs == {"vocals": 0}


@pytest.mark.parametrize(
    ("doc", "stems"),
    [
        (_multi(["male", "female"], 2), ["vocals"]),
        (_multi(["vocals", "instrumental"], 2), ["vocals", "drums"]),
        ({**_kim_doc(), "training": {"target_instrument": "instrumental"}}, ["vocals"]),
        ({**_kim_doc(), "training": None}, ["vocals", "drums"]),  # two stems, one output
    ],
)
def test_a_declared_stem_the_model_does_not_make_is_refused(doc, stems):
    _refused("stem_not_in_model", fmt.clean_config, doc, stems)


def test_a_one_output_model_without_instruments_takes_the_declared_stem():
    doc = {**_kim_doc(), "training": {}}
    assert fmt.clean_config(doc, ["drums"]).outputs == {"drums": 0}


def test_instruments_that_do_not_match_the_outputs_are_refused():
    _refused("bad_config", fmt.clean_config, _multi(["vocals", "other"], 3), ["vocals"])
    doc = _multi(["vocals"], 2, target_instrument="vocals")
    _refused("bad_config", fmt.clean_config, doc, ["vocals"])
    _refused("bad_config", fmt.clean_config, _multi(["vo<cals>"], 1), ["vocals"])


def test_no_stems_or_repeated_stems_are_refused():
    _refused("stem_not_in_model", fmt.clean_config, _kim_doc(), [])
    _refused("stem_not_in_model", fmt.clean_config, _kim_doc(), ["vocals", "vocals"])


# ── the parameter count ──


def test_the_count_is_kims_exact_count():
    arch, kwargs = fmt.clean_model(_kim_model())
    assert fmt.estimate_params(arch, kwargs) == KIM_PARAMS


def test_a_model_over_the_parameter_limit_is_refused_before_it_is_built():
    doc = _kim_doc(dim=1024, depth=24, heads=16, dim_head=64)
    arch, kwargs = fmt.clean_model(doc["model"])
    assert fmt.estimate_params(arch, kwargs) > MAX_USER_MODEL_PARAMS
    _refused("too_big_model", fmt.clean_config, doc, ["vocals"])


_COUNT_CASES = [
    {
        "dim": 64,
        "depth": 2,
        "stereo": True,
        "num_stems": 2,
        "num_bands": 32,
        "stft_hop_length": 441,
        "mask_estimator_depth": 2,
        "time_transformer_depth": 1,
        "freq_transformer_depth": 1,
        "dim_head": 32,
        "heads": 4,
    },
    {"dim": 32, "depth": 1, "stereo": True, "num_bands": 60, "stft_n_fft": 4096},
    _bs_model(num_stems=3, mlp_expansion_factor=2, mask_estimator_depth=2, dim=48, depth=2),
    _bs_model(mask_estimator_depth=3),
]


@pytest.mark.skipif(not HAS_SEPARATOR, reason="audio-separator is not installed")
@pytest.mark.parametrize("model", _COUNT_CASES)
def test_the_count_equals_a_real_build(model):
    import torch
    from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import BSRoformer
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    arch, kwargs = fmt.clean_model(model)
    cls = MelBandRoformer if arch == "mel_band_roformer" else BSRoformer
    with torch.device("meta"):
        built = cls(**fmt.constructor_kwargs(arch, kwargs))
    assert fmt.estimate_params(arch, kwargs) == sum(p.numel() for p in built.parameters())


@pytest.mark.skipif(not HAS_SEPARATOR, reason="audio-separator is not installed")
def test_the_defaults_are_the_classes_own():
    from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import BSRoformer
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    for arch, cls in (("mel_band_roformer", MelBandRoformer), ("bs_roformer", BSRoformer)):
        params = inspect.signature(cls.__init__).parameters
        for key, value in fmt.DEFAULTS[arch].items():
            assert params[key].default == value, (arch, key)


@pytest.mark.skipif(importlib.util.find_spec("librosa") is None, reason="librosa is missing")
@pytest.mark.parametrize(("n_fft", "bands"), [(2048, 60), (4096, 128), (2048, 8), (256, 32)])
def test_the_mel_bands_match_librosa(n_fft, bands):
    import librosa

    weights = librosa.filters.mel(sr=44_100, n_fft=n_fft, n_mels=bands)
    weights[0][0] = 1.0
    weights[-1, -1] = 1.0
    covered = weights > 0
    expected = [int(v) for v in covered.sum(axis=1)]
    if not covered.any(axis=0).all():
        expected.append(0)
    eff = {"sample_rate": 44_100, "stft_n_fft": n_fft, "num_bands": bands}
    assert fmt.mel_band_sizes(eff) == expected


# ── a stored record ──


def test_a_stored_record_passes_and_an_edited_one_does_not():
    cleaned = fmt.clean_config(_kim_doc(), ["vocals"])
    fmt.check_stored(cleaned.arch, cleaned.kwargs, cleaned.audio, cleaned.outputs, ["vocals"])
    edits = [
        ("bs_roformer", cleaned.kwargs, cleaned.audio, cleaned.outputs),
        (cleaned.arch, {**cleaned.kwargs, "dim": 4096}, cleaned.audio, cleaned.outputs),
        (cleaned.arch, {**cleaned.kwargs, "attn_dropout": 0}, cleaned.audio, cleaned.outputs),
        (cleaned.arch, cleaned.kwargs, {**cleaned.audio, "chunk": 1}, cleaned.outputs),
        (cleaned.arch, cleaned.kwargs, cleaned.audio, {"vocals": 3}),
        (cleaned.arch, cleaned.kwargs, cleaned.audio, {"drums": 0}),
    ]
    for arch, kwargs, audio, outputs in edits:
        with pytest.raises(fmt.Refused):
            fmt.check_stored(arch, kwargs, audio, outputs, ["vocals"])


def test_constructor_kwargs_make_the_band_tuple():
    arch, kwargs = fmt.clean_model(_bs_model())
    assert isinstance(fmt.constructor_kwargs(arch, kwargs)["freqs_per_bands"], tuple)
    assert isinstance(kwargs["freqs_per_bands"], list)  # the stored copy stays JSON


# ── display text ──


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [
        ("  My  model ", 60, "My model"),
        ("a\x00b\nc\u200bd", 60, "a b c d"),
        ("<b>MIT</b>", 80, "MIT"),
        ("Cafe\u0301", 60, "Caf\u00e9"),
        ("x" * 100, 60, "x" * 60),
        ("   ", 60, None),
        (12, 60, None),
    ],
)
def test_display_text(value, limit, expected):
    assert fmt.display_text(value, limit) == expected


def test_an_unknown_code_becomes_bad_format():
    assert fmt.Refused("no_such_code").code == "bad_format"


def test_build_model_honours_the_mask_estimator_factor():
    """audio-separator drops mlp_expansion_factor; build_model passes it on."""
    pytest.importorskip("audio_separator")
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    kwargs = {
        "dim": 32,
        "depth": 1,
        "num_stems": 1,
        "time_transformer_depth": 1,
        "freq_transformer_depth": 1,
        "num_bands": 8,
        "dim_head": 8,
        "heads": 2,
        "mask_estimator_depth": 2,
        "mlp_expansion_factor": 1,
        "flash_attn": False,
    }
    model = fmt.build_model(MelBandRoformer, "mel_band_roformer", kwargs)
    key = "mask_estimators.0.to_freqs.0.0.0.weight"
    assert tuple(model.state_dict()[key].shape) == (32, 32)  # dim * 1, not dim * 4
    default = fmt.build_model(
        MelBandRoformer, "mel_band_roformer", {**kwargs, "mlp_expansion_factor": 4}
    )
    assert default.state_dict()[key].shape[0] == 128
    guitar = {
        "dim": 256, "depth": 4, "stereo": True, "num_stems": 1, "time_transformer_depth": 1,
        "freq_transformer_depth": 1, "num_bands": 60, "dim_head": 64, "heads": 8,
        "flash_attn": True, "dim_freqs_in": 1025, "sample_rate": 44100, "stft_n_fft": 2048,
        "stft_hop_length": 441, "stft_win_length": 2048, "stft_normalized": False,
        "mask_estimator_depth": 2, "mlp_expansion_factor": 1,
    }  # fmt: skip
    # becruily/mel-band-roformer-guitar: its checkpoint holds 22,435,332 values,
    # 192 of them the rotary frequency tables that are not parameters.
    assert fmt.estimate_params("mel_band_roformer", guitar) == 22_435_332 - 192
