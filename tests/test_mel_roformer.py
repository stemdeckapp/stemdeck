"""The Mel-Band RoFormer vocal engine (app/pipeline/mel_roformer.py) without
its checkpoint: chunked overlap-add inference with stand-in models, the ROCm
attention fix, the checkpoint's download and healing, and a fingerprint of
the architecture audio-separator builds against the real checkpoint's."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from app.pipeline import mel_roformer  # noqa: E402

CHUNK = mel_roformer.CHUNK


class _Linear(torch.nn.Module):
    """A stand-in vocal model: ``fn`` applied to each chunk of the batch."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.calls = 0

    def forward(self, batch):
        assert batch.ndim == 3 and batch.shape[1:] == (2, CHUNK)
        self.calls += 1
        return self.fn(batch)


def _mix(n: int, seed: int = 1) -> torch.Tensor:
    g = torch.Generator().manual_seed(seed)
    return torch.rand(2, n, generator=g) * 1.6 - 0.8


@pytest.mark.parametrize("n", [1, 1000, CHUNK, CHUNK + 1, int(2.3 * CHUNK)])
def test_a_model_that_returns_its_input_gives_the_mix_back(n):
    mix = _mix(n)
    out = mel_roformer.separate_vocals(_Linear(lambda x: x), mix, "cpu")
    assert out.shape == mix.shape and out.dtype == torch.float32
    torch.testing.assert_close(out, mix, atol=1e-6, rtol=0)


def test_every_real_sample_sits_under_a_fully_open_window():
    """No seam: for any model, each sample of the song has at least one chunk
    whose window is fully open there, so no sample is made of fades alone."""
    for n in (1, 1000, CHUNK - 1, CHUNK, 3 * CHUNK + 17):
        pad, padded, starts = mel_roformer.chunk_starts(n)
        assert starts[-1] + CHUNK == padded
        assert pad >= mel_roformer.FADE and padded - pad - n >= mel_roformer.FADE
        opened = torch.zeros(padded, dtype=torch.bool)
        fade = mel_roformer.FADE
        for s in starts:
            opened[s + fade : s + CHUNK - fade] = True
        assert bool(opened[pad : pad + n].all()), n


def test_the_engine_is_linear():
    smooth = torch.tensor([[[0.25, 0.5, 0.25]]]).repeat(2, 1, 1)

    def fir(x):  # a linear filter, different at every sample
        return torch.nn.functional.conv1d(x, smooth, padding=1, groups=2) * 0.7

    model = _Linear(fir)
    a, b = _mix(int(1.7 * CHUNK), 2), _mix(int(1.7 * CHUNK), 3)
    left = mel_roformer.separate_vocals(model, 0.3 * a - 1.2 * b, "cpu")
    right = 0.3 * mel_roformer.separate_vocals(model, a, "cpu") - 1.2 * (
        mel_roformer.separate_vocals(model, b, "cpu")
    )
    torch.testing.assert_close(left, right, atol=1e-5, rtol=0)


def test_a_model_that_returns_a_few_samples_short_still_covers_the_song():
    mix = _mix(int(2.5 * CHUNK))
    out = mel_roformer.separate_vocals(_Linear(lambda x: x[..., :-7]), mix, "cpu")
    torch.testing.assert_close(out, mix, atol=1e-6, rtol=0)


def test_a_model_with_a_stems_axis_is_read_as_its_first_stem():
    mix = _mix(1000)
    out = mel_roformer.separate_vocals(_Linear(lambda x: (x * 0.5)[:, None]), mix, "cpu")
    torch.testing.assert_close(out, mix * 0.5, atol=1e-6, rtol=0)


def test_progress_rises_to_100():
    seen: list[int] = []
    model = _Linear(lambda x: x)
    mel_roformer.separate_vocals(model, _mix(int(4.2 * CHUNK)), "cpu", seen.append)
    assert seen[-1] == 100
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    _, _, starts = mel_roformer.chunk_starts(int(4.2 * CHUNK))
    assert model.calls == -(-len(starts) // mel_roformer.BATCH)


class _Attention(torch.nn.Module):
    """Calls scaled_dot_product_attention with a transposed view of v, the
    layout that ROCm gets wrong."""

    def forward(self, batch):
        import torch.nn.functional as F

        q = torch.ones(1, 1, 4, 4)
        v = torch.ones(1, 1, 4, 4).transpose(-1, -2)
        F.scaled_dot_product_attention(q, q, v)
        return batch


@pytest.mark.parametrize("rocm", [True, False])
def test_attention_gets_contiguous_tensors_on_rocm_only(monkeypatch, rocm):
    import torch.nn.functional as F

    seen: list[bool] = []
    real = F.scaled_dot_product_attention

    def recording(q, k, v, *args, **kwargs):
        seen.append(v.is_contiguous())
        return real(q, k, v, *args, **kwargs)

    monkeypatch.setattr(F, "scaled_dot_product_attention", recording)
    monkeypatch.setattr(mel_roformer, "is_rocm", lambda device: rocm)

    mel_roformer.separate_vocals(_Attention(), _mix(100), "cpu")

    assert seen == [rocm]
    # Put back after the pass, whatever it did.
    assert F.scaled_dot_product_attention is recording


def test_rocm_is_a_cuda_device_on_a_hip_build(monkeypatch):
    monkeypatch.setattr(torch.version, "hip", "6.4.0", raising=False)
    assert mel_roformer.is_rocm("cuda") is True
    assert mel_roformer.is_rocm("cpu") is False
    assert mel_roformer.is_rocm("mps") is False
    monkeypatch.setattr(torch.version, "hip", None, raising=False)
    assert mel_roformer.is_rocm("cuda") is False


@pytest.mark.filterwarnings("ignore:User provided device_type of .cuda.:UserWarning")
def test_fp16_autocast_is_for_a_gpu_only():
    assert type(mel_roformer.autocast("cpu")).__name__ == "nullcontext"
    assert type(mel_roformer.autocast("mps")).__name__ == "nullcontext"
    cast = mel_roformer.autocast("cuda")
    assert isinstance(cast, torch.autocast) and cast.fast_dtype == torch.float16


# -- The checkpoint on disk


def _model_file(tmp_path: Path, size: int = 5) -> SimpleNamespace:
    return SimpleNamespace(url="https://example.invalid/m.ckpt", sha256="0" * 64, size=size)


def test_a_checkpoint_of_the_right_size_is_used_as_it_is(tmp_path, monkeypatch):
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    path.parent.mkdir()
    path.write_bytes(b"12345")

    def no_download(*a, **k):
        raise AssertionError("a complete file must not be downloaded again")

    monkeypatch.setattr("app.core.model_download.fetch_verified_file", no_download)
    got = mel_roformer.ensure_checkpoint(
        read_timeout=1, attempts=1, model_file=_model_file(tmp_path), path=path
    )
    assert got == path


@pytest.mark.parametrize("present", [b"", b"123", None])
def test_a_missing_or_short_checkpoint_is_downloaded_and_said(
    tmp_path, monkeypatch, capsys, present
):
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    if present is not None:
        path.parent.mkdir()
        path.write_bytes(present)
    calls = []

    def fake_fetch(url, dst, sha256, size, *, read_timeout, attempts):
        calls.append((url, dst, size, read_timeout, attempts))
        Path(dst).write_bytes(b"12345")

    monkeypatch.setattr("app.core.model_download.fetch_verified_file", fake_fetch)
    spec = _model_file(tmp_path)
    mel_roformer.ensure_checkpoint(read_timeout=7, attempts=2, model_file=spec, path=path)

    assert calls == [(spec.url, str(path), 5, 7, 2)]
    # torch.hub's own words, which the worker's parent reads as the download.
    assert f'Downloading: "{spec.url}" to {path}' in capsys.readouterr().err


class _Fits(torch.nn.Module):
    """A stand-in MelBandRoformer that takes the stand-in checkpoint."""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.zeros(3))


def _good_checkpoint(path: Path) -> SimpleNamespace:
    """A real torch checkpoint at ``path``, and its pin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"w": torch.ones(3)}, path)
    data = path.read_bytes()
    return SimpleNamespace(
        url="https://example.invalid/m.ckpt",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


@pytest.fixture()
def fetches(monkeypatch):
    """Every download load() asks for, which writes ``good`` bytes."""
    calls: list[str] = []
    good: dict[str, bytes] = {}

    def fake_fetch(url, dst, sha256, size, *, read_timeout, attempts):
        calls.append(dst)
        Path(dst).write_bytes(good["bytes"])

    monkeypatch.setattr("app.core.model_download.fetch_verified_file", fake_fetch)
    monkeypatch.setattr(mel_roformer, "build_model", _Fits)
    return calls, good


def test_an_intact_checkpoint_that_cannot_be_read_is_kept_and_not_fetched_again(
    tmp_path, monkeypatch, fetches
):
    """913 MB fetched again would read the same way, on every job, twice with
    the CPU retry. The file matches its pin: it is kept, and the error says so."""
    calls, _ = fetches
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    spec = _good_checkpoint(path)

    def cannot_read(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(torch, "load", cannot_read)

    with pytest.raises(RuntimeError) as exc_info:
        mel_roformer.load(read_timeout=1, attempts=1, model_file=spec, path=path)

    assert calls == []
    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == spec.sha256
    message = str(exc_info.value)
    assert str(path) in message and "is intact" in message and "was kept" in message


def test_a_damaged_checkpoint_of_the_right_size_is_fetched_again_once(tmp_path, fetches):
    calls, good = fetches
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    spec = _good_checkpoint(path)
    good["bytes"] = path.read_bytes()
    path.write_bytes(b"\0" * spec.size)  # the right size, not the right bytes

    model = mel_roformer.load(read_timeout=1, attempts=1, model_file=spec, path=path)

    assert calls == [str(path)]
    torch.testing.assert_close(model.w.detach(), torch.ones(3))


def test_a_checkpoint_that_does_not_fit_the_class_is_kept_and_said(tmp_path, monkeypatch, fetches):
    """A strict-load misfit is audio-separator's class, not the file."""
    calls, _ = fetches
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    spec = _good_checkpoint(path)

    class _Other(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.renamed = torch.nn.Parameter(torch.zeros(3))

    monkeypatch.setattr(mel_roformer, "build_model", _Other)

    with pytest.raises(RuntimeError) as exc_info:
        mel_roformer.load(read_timeout=1, attempts=1, model_file=spec, path=path)

    assert calls == [] and path.is_file()
    assert "does not fit the MelBandRoformer" in str(exc_info.value)
    assert "was kept" in str(exc_info.value)


def test_a_model_that_cannot_be_built_never_touches_the_file(tmp_path, monkeypatch, fetches):
    calls, _ = fetches
    path = tmp_path / "roformer" / "MelBandRoformer.ckpt"
    spec = _good_checkpoint(path)

    def changed_constructor():
        raise TypeError("__init__() got an unexpected keyword argument 'dim'")

    monkeypatch.setattr(mel_roformer, "build_model", changed_constructor)

    with pytest.raises(TypeError):
        mel_roformer.load(read_timeout=1, attempts=1, model_file=spec, path=path)

    assert calls == [] and path.is_file()


def test_the_checkpoint_is_loaded_with_weights_only_and_strict(tmp_path, monkeypatch):
    seen = {}

    class _Model:
        def load_state_dict(self, state, strict):
            seen["strict"] = strict
            seen["state"] = state

        def eval(self):
            return self

    def fake_torch_load(path, map_location, weights_only):
        seen["weights_only"] = weights_only
        seen["map_location"] = map_location
        return {"w": 1}

    monkeypatch.setattr(torch, "load", fake_torch_load)
    monkeypatch.setattr(mel_roformer, "build_model", _Model)
    mel_roformer.load_weights(tmp_path / "x.ckpt")
    assert seen == {"weights_only": True, "map_location": "cpu", "strict": True, "state": {"w": 1}}


# -- The architecture against the real checkpoint

# The SHA-256 of the checkpoint's state dict as sorted [name, shape] pairs, in
# compact JSON. Read from the pinned checkpoint itself (revision
# ac9b0614ab3cd7f77219e18ba494dfd93956c348) on 2026-11-23 without
# downloading it: HTTP Range requests for the zip directory and data.pkl
# only, unpickled with a whitelist that builds no real object. 684 tensors,
# 228_203_172 values (913 MB in float32, the file's size).
CHECKPOINT_FINGERPRINT = "753dca2007ff0e3935b5943cc61555f5b8390775e6d9b2625deea4c27603a699"
CHECKPOINT_TENSORS = 684
CHECKPOINT_VALUES = 228_203_172


def _shapes_on_meta() -> dict[str, list[int]]:
    """MelBandRoformer(**MODEL_KWARGS)'s state dict shapes, every parameter on
    the meta device: nothing is allocated for the weights. (Building the
    whole module under torch.device("meta") does not work: its constructor
    reads mel filter values with .item().)"""
    real = torch.nn.Module.register_parameter

    def on_meta(self, name, param):
        if param is not None:
            param = torch.nn.Parameter(param.to("meta"), requires_grad=param.requires_grad)
        real(self, name, param)

    torch.nn.Module.register_parameter = on_meta
    try:
        model = mel_roformer.build_model()
    finally:
        torch.nn.Module.register_parameter = real
    return {k: list(v.shape) for k, v in model.state_dict().items()}


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_audio_separators_mel_band_roformer_builds_the_checkpoints_exact_shape():
    """If audio-separator moves or changes MelBandRoformer, or a
    hyper-parameter here is wrong, the checkpoint would not load (strict) or,
    worse, load into a different network. This fails first, in CI."""
    pytest.importorskip("audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer")
    import math

    shapes = _shapes_on_meta()
    canon = json.dumps(sorted([k, v] for k, v in shapes.items()), separators=(",", ":"))
    assert len(shapes) == CHECKPOINT_TENSORS
    assert sum(math.prod(v) for v in shapes.values()) == CHECKPOINT_VALUES
    assert hashlib.sha256(canon.encode()).hexdigest() == CHECKPOINT_FINGERPRINT


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
def test_the_stft_settings_return_exactly_one_chunk():
    """The hop and the window leave no trace in the weights, and a wrong one
    would be absorbed by the short-return tolerance. With the config's STFT
    settings and tiny everything else, a forward pass on one chunk returns
    exactly one chunk."""
    module = pytest.importorskip("audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer")
    tiny = {
        **mel_roformer.MODEL_KWARGS,
        "dim": 8,
        "depth": 1,
        "heads": 1,
        "dim_head": 8,
        "mask_estimator_depth": 1,
        "flash_attn": False,
    }
    model = module.MelBandRoformer(**tiny).eval()
    with torch.no_grad():
        out = model(torch.zeros(1, 2, CHUNK))
    assert out.shape[-1] == CHUNK


def test_a_chunk_of_another_length_is_said_once(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(mel_roformer, "_length_noted", [])
    with caplog.at_level(logging.WARNING, logger="stemdeck.mel_roformer"):
        mel_roformer.separate_vocals(_Linear(lambda x: x[..., :-7]), _mix(int(2.5 * CHUNK)), "cpu")
        mel_roformer.separate_vocals(_Linear(lambda x: x[..., :-7]), _mix(1000), "cpu")
    said = [r.message for r in caplog.records if "returned" in r.message]
    assert said == [f"the vocal model returned {CHUNK - 7} samples for a chunk of {CHUNK}"]


# -- The engine for models the person added (My models)

USER_ID = "user_0123456789ab"


def _spec(outputs, *, chunk=8000, overlap=2, batch=2, num_stems=3) -> mel_roformer.RoformerSpec:
    return mel_roformer.RoformerSpec(
        arch="mel_band_roformer",
        kwargs={},
        chunk=chunk,
        overlap=overlap,
        batch=batch,
        outputs=outputs,
        num_stems=num_stems,
    )


class _PerIndex(torch.nn.Module):
    """A stand-in multi-stem model: output i is the input times (i + 1) / 10,
    so a swapped index shows."""

    def __init__(self, num_stems: int, chunk: int):
        super().__init__()
        self.num_stems = num_stems
        self.chunk = chunk

    def forward(self, batch):
        assert batch.ndim == 3 and batch.shape[1:] == (2, self.chunk)
        return torch.stack([batch * (i + 1) / 10 for i in range(self.num_stems)], dim=1)


def test_each_declared_stem_comes_from_its_own_output_index():
    mix = _mix(30_000)
    spec = _spec({"drums": 2, "bass": 0})
    out = mel_roformer.separate(_PerIndex(3, 8000), mix, "cpu", spec)
    assert set(out) == {"drums", "bass"}
    torch.testing.assert_close(out["drums"], mix * 0.3, atol=1e-6, rtol=0)
    torch.testing.assert_close(out["bass"], mix * 0.1, atol=1e-6, rtol=0)


def test_only_the_stems_asked_for_are_kept():
    spec = _spec({"drums": 2, "bass": 0, "vocals": 1})
    out = mel_roformer.separate(_PerIndex(3, 8000), _mix(9000), "cpu", spec, stems=("bass",))
    assert set(out) == {"bass"}
    with pytest.raises(ValueError):
        mel_roformer.separate(_PerIndex(3, 8000), _mix(9000), "cpu", spec, stems=("piano",))


@pytest.mark.parametrize("overlap", [1, 2, 3, 4, 8])
@pytest.mark.parametrize("n", [1, 7999, 8000, 23_456])
def test_any_overlap_gives_an_identity_model_the_mix_back(overlap, n):
    """overlap 1 is run as 2: with one chunk over a sample, the song's ends
    would sit under a fade alone."""
    spec = _spec({"vocals": 0}, overlap=overlap, num_stems=1)
    mix = _mix(n)
    out = mel_roformer.separate(_Identity(8000), mix, "cpu", spec)["vocals"]
    torch.testing.assert_close(out, mix, atol=1e-6, rtol=0)


class _Identity(torch.nn.Module):
    def __init__(self, chunk: int):
        super().__init__()
        self.chunk = chunk
        self.calls = 0

    def forward(self, batch):
        assert batch.shape[1:] == (2, self.chunk)
        self.calls += 1
        return batch.clone()


@pytest.mark.parametrize("overlap", [2, 3, 5, 8])
def test_every_real_sample_sits_under_a_fully_open_window_at_any_overlap(overlap):
    chunk = 8000
    step = chunk // overlap
    fade = chunk // 10
    for n in (1, 1000, chunk - 1, chunk, 3 * chunk + 17):
        pad, padded, starts = mel_roformer.chunk_starts(n, chunk, step)
        assert starts[-1] + chunk == padded
        opened = torch.zeros(padded, dtype=torch.bool)
        for s in starts:
            opened[s + fade : s + chunk - fade] = True
        assert bool(opened[pad : pad + n].all()), (overlap, n)


def test_the_batch_is_the_specs():
    model = _Identity(8000)
    spec = _spec({"vocals": 0}, batch=3, num_stems=1)
    mel_roformer.separate(model, _mix(50_000), "cpu", spec)
    _, _, starts = mel_roformer.chunk_starts(50_000, 8000, 4000)
    assert model.calls == -(-len(starts) // 3)


def test_a_one_output_model_cannot_give_a_second_index():
    spec = _spec({"drums": 1}, num_stems=2)
    with pytest.raises(ValueError, match="unexpected shape"):
        mel_roformer.separate(_Identity(8000), _mix(9000), "cpu", spec)


def test_kim_keeps_its_own_settings():
    spec = mel_roformer.KIM_SPEC
    assert (spec.chunk, spec.batch, dict(spec.outputs)) == (
        CHUNK,
        mel_roformer.BATCH,
        {"vocals": 0},
    )
    assert spec.chunk // spec.overlap == mel_roformer.STEP
    assert spec.kwargs is mel_roformer.MODEL_KWARGS


# -- Loading a model the person added


class _Small(torch.nn.Module):
    """A stand-in RoFormer with one gated attention layer."""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.zeros(3))
        self.layers = torch.nn.ModuleList([torch.nn.Module()])
        self.layers[0].to_gates = torch.nn.Linear(2, 2)


def _small_state(**over) -> dict:
    state = {k: torch.full_like(v, 0.5) for k, v in _Small().state_dict().items()}
    state.update(over)
    return state


def _user_file(path: Path, fmt: str = "torch") -> mel_roformer.UserFile:
    data = path.read_bytes()
    return mel_roformer.UserFile(
        model_id=USER_ID,
        path=path,
        format=fmt,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        mtime_ns=path.stat().st_mtime_ns,
    )


@pytest.fixture
def small(monkeypatch):
    """build_user_model makes a _Small; every file is checked afresh."""
    monkeypatch.setattr(mel_roformer, "build_user_model", lambda spec: _Small())
    monkeypatch.setattr(mel_roformer, "_verified", set())
    monkeypatch.setattr(mel_roformer, "_torch_new_enough", lambda: True)


def _load(path: Path, fmt: str = "torch"):
    return mel_roformer.load_user(_spec({"vocals": 0}, num_stems=1), _user_file(path, fmt))


def _code(path: Path, fmt: str = "torch") -> str:
    with pytest.raises(mel_roformer.UserModelError) as exc_info:
        _load(path, fmt)
    return exc_info.value.code


@pytest.mark.usefixtures("small")
def test_a_checkpoint_that_fits_exactly_loads_strictly_and_with_weights_only(tmp_path, monkeypatch):
    path = tmp_path / "model.ckpt"
    torch.save(_small_state(), path)
    seen = {}
    real = torch.load

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(torch, "load", spy)
    model = _load(path)
    assert seen["weights_only"] is True and seen["map_location"] == "cpu"
    assert not model.training
    torch.testing.assert_close(model.w.detach(), torch.full((3,), 0.5))


@pytest.mark.usefixtures("small")
@pytest.mark.parametrize("key", ["state_dict", "model"])
def test_one_level_of_wrapping_is_taken_off(tmp_path, key):
    path = tmp_path / "model.ckpt"
    torch.save({key: _small_state(), "epoch": 3}, path)
    assert _load(path).w.shape == (3,)


@pytest.mark.usefixtures("small")
def test_a_file_with_more_or_fewer_keys_is_a_key_mismatch(tmp_path):
    path = tmp_path / "model.ckpt"
    torch.save(_small_state(extra=torch.zeros(1)), path)
    assert _code(path) == "key_mismatch"
    state = _small_state()
    del state["w"]
    torch.save(state, path)
    assert _code(path) == "key_mismatch"
    # Nothing is stripped: a "module." prefix is another name.
    torch.save({f"module.{k}": v for k, v in _small_state().items()}, path)
    assert _code(path) == "key_mismatch"


@pytest.mark.usefixtures("small")
def test_a_shape_that_differs_is_a_key_mismatch(tmp_path):
    path = tmp_path / "model.ckpt"
    torch.save(_small_state(w=torch.zeros(4)), path)
    assert _code(path) == "key_mismatch"


@pytest.mark.usefixtures("small")
def test_a_file_without_the_attention_gates_is_named_and_not_adapted(tmp_path):
    path = tmp_path / "model.ckpt"
    state = {k: v for k, v in _small_state().items() if ".to_gates." not in k}
    torch.save(state, path)
    with pytest.raises(mel_roformer.GatesMissing) as exc_info:
        _load(path)
    assert exc_info.value.code == "gates_missing"
    assert "to_gates" in str(exc_info.value)


@pytest.mark.usefixtures("small")
def test_a_weight_that_is_not_a_number_is_refused(tmp_path):
    path = tmp_path / "model.ckpt"
    torch.save(_small_state(w=torch.tensor([0.0, float("nan"), 1.0])), path)
    assert _code(path) == "non_finite"


@pytest.mark.usefixtures("small")
def test_a_file_that_holds_more_than_tensors_does_not_load(tmp_path):
    path = tmp_path / "model.ckpt"
    torch.save({"w": [1, 2, 3]}, path)
    assert _code(path) == "load_failed"


class _Planted:
    """A pickle that would create a file if anything ran it."""

    def __init__(self, target: Path):
        self.target = str(target)

    def __reduce__(self):
        return (open, (self.target, "w"))


@pytest.mark.usefixtures("small")
def test_a_checkpoint_that_runs_code_is_refused_and_nothing_runs(tmp_path):
    canary = tmp_path / "canary.txt"
    path = tmp_path / "model.ckpt"
    torch.save({"w": torch.zeros(3), "x": _Planted(canary)}, path)
    assert not canary.exists()
    assert _code(path) == "load_failed"
    assert not canary.exists()


@pytest.mark.usefixtures("small")
def test_a_file_changed_since_it_was_checked_is_never_read(tmp_path, monkeypatch):
    path = tmp_path / "model.ckpt"
    torch.save(_small_state(), path)
    file = _user_file(path)
    read: list = []
    monkeypatch.setattr(mel_roformer, "read_user_state", lambda f: read.append(f) or {})
    spec = _spec({"vocals": 0}, num_stems=1)

    for changed in (
        mel_roformer.UserFile(**{**file.__dict__, "size": file.size + 1}),
        mel_roformer.UserFile(**{**file.__dict__, "mtime_ns": file.mtime_ns + 1}),
        mel_roformer.UserFile(**{**file.__dict__, "sha256": "0" * 64}),
        mel_roformer.UserFile(**{**file.__dict__, "path": tmp_path / "gone.ckpt"}),
    ):
        with pytest.raises(mel_roformer.UserModelError) as exc_info:
            mel_roformer.load_user(spec, changed)
        assert exc_info.value.code == "changed"
    assert read == []


@pytest.mark.usefixtures("small")
def test_the_sha256_is_checked_once_per_worker(tmp_path, monkeypatch):
    from app.core import model_download

    path = tmp_path / "model.ckpt"
    torch.save(_small_state(), path)
    hashed: list = []
    real = model_download.file_matches

    def spy(*args):
        hashed.append(args)
        return real(*args)

    monkeypatch.setattr(model_download, "file_matches", spy)
    _load(path)
    _load(path)
    assert len(hashed) == 1


def test_a_torch_older_than_2_6_never_reads_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mel_roformer, "_torch_new_enough", lambda: False)
    monkeypatch.setattr(
        mel_roformer, "check_user_file", lambda f: pytest.fail("the file was looked at")
    )
    path = tmp_path / "model.ckpt"
    path.write_bytes(b"PK")
    assert _code(path) == "torch_too_old"


def test_the_torch_version_is_read_from_its_package(monkeypatch):
    import importlib.metadata as md

    for version, ok in (("2.6.0", True), ("2.6.0+cu124", True), ("2.10.1", True), ("2.2.2", False)):
        monkeypatch.setattr(md, "version", lambda name, v=version: v)
        assert mel_roformer._torch_new_enough() is ok, version


def _write_safetensors(path: Path, state: dict) -> None:
    """A safetensors file by hand: u64 little-endian header length, a JSON
    header of dtype, shape and data offsets, then the bytes."""
    import struct

    header, blobs, offset = {}, [], 0
    for name, tensor in state.items():
        data = tensor.contiguous().numpy().tobytes()
        header[name] = {
            "dtype": "F32",
            "shape": list(tensor.shape),
            "data_offsets": [offset, offset + len(data)],
        }
        blobs.append(data)
        offset += len(data)
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"".join(blobs))


@pytest.mark.usefixtures("small")
def test_a_safetensors_file_loads_through_the_own_reader(tmp_path):
    path = tmp_path / "model.safetensors"
    _write_safetensors(path, _small_state())
    model = _load(path, "safetensors")
    torch.testing.assert_close(model.w.detach(), torch.full((3,), 0.5))
    _write_safetensors(path, _small_state(extra=torch.zeros(2)))
    assert _code(path, "safetensors") == "key_mismatch"
    path.write_bytes(b"not a safetensors file at all")
    assert _code(path, "safetensors") == "load_failed"


def test_a_request_entry_that_is_not_a_model_is_refused():
    good = {
        "id": USER_ID,
        "path": "x/model.ckpt",
        "arch": "bs_roformer",
        "kwargs": {"num_stems": 2},
        "audio": {"chunk": 44100, "overlap": 2, "batch": 1},
        "outputs": {"drums": 1},
        "num_stems": 2,
        "sha256": "a" * 64,
        "size": 10,
        "mtime_ns": 5,
        "stems": ["drums"],
        "format": "torch",
    }
    spec, file, stems = mel_roformer.user_spec(USER_ID, good)
    assert (spec.arch, spec.chunk, dict(spec.outputs), spec.num_stems) == (
        "bs_roformer",
        44100,
        {"drums": 1},
        2,
    )
    assert (file.format, file.size, file.mtime_ns, stems) == ("torch", 10, 5, ("drums",))
    for bad in (
        {**good, "arch": "htdemucs"},
        {**good, "outputs": {"drums": 2}},  # index past the model's outputs
        {**good, "outputs": {"bass": 0}},  # not a declared stem
        {**good, "outputs": {}},  # a declared stem with no output
        {**good, "format": "onnx"},
        {**good, "sha256": "short"},
        {**good, "audio": {"chunk": True}},
        {**good, "size": 0},
        "not a mapping",
    ):
        with pytest.raises(mel_roformer.UserModelError) as exc_info:
            mel_roformer.user_spec(USER_ID, bad)
        assert exc_info.value.code == "load_failed"


# -- The real classes, tiny


def _tiny(arch: str) -> tuple[str, dict]:
    from app.core import user_model_format as fmt

    model = {"dim": 16, "depth": 1, "stereo": True, "num_stems": 2, "heads": 1, "dim_head": 8}
    model.update(
        {"num_bands": 8, "stft_hop_length": 441}
        if arch == "mel_band_roformer"
        else {"freqs_per_bands": [512, 513], "stft_hop_length": 441}
    )
    return fmt.clean_model({**model, "time_transformer_depth": 1, "freq_transformer_depth": 1})


@pytest.mark.filterwarnings("ignore::UserWarning")
@pytest.mark.filterwarnings("ignore::FutureWarning")
@pytest.mark.parametrize("arch", ["mel_band_roformer", "bs_roformer"])
def test_a_tiny_real_model_loads_and_separates_end_to_end(tmp_path, monkeypatch, arch):
    pytest.importorskip("audio_separator.separator.uvr_lib_v5.roformer.bs_roformer")
    monkeypatch.setattr(mel_roformer, "_verified", set())
    arch, kwargs = _tiny(arch)
    spec = mel_roformer.RoformerSpec(
        arch=arch,
        kwargs=kwargs,
        chunk=44_100,
        overlap=2,
        batch=1,
        outputs={"drums": 1, "bass": 0},
        num_stems=2,
    )
    path = tmp_path / "model.ckpt"
    torch.save(mel_roformer.build_user_model(spec).state_dict(), path)

    model = mel_roformer.load_user(spec, _user_file(path))
    mix = _mix(50_000)
    out = mel_roformer.separate(model, mix, "cpu", spec, stems=("drums",))

    assert set(out) == {"drums"}
    assert out["drums"].shape == mix.shape
    assert bool(torch.isfinite(out["drums"]).all())
