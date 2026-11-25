"""A user model's test run (app/core/user_model_check.py and
app/pipeline/user_model_check_worker.py).

The parent side runs against stub workers: the protocol, the answer's
re-check, and the watchdog that ends a run on cancel, timeout or silence and
always reaps it. Then the real worker runs on tiny RoFormers built here, in
a temporary folder (skipped without torch and audio-separator): a model that
fits passes, a key or a gate missing, a NaN weight and a checkpoint that
would run code are each refused with their own code, and the code never
runs."""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from app.core import user_model_check as checker
from app.core import user_model_format as fmt
from app.pipeline import user_model_check_worker as worker

HAS_SEPARATOR = (
    importlib.util.find_spec("torch") is not None
    and importlib.util.find_spec("audio_separator") is not None
)


# ── the protocol ──


@pytest.mark.parametrize(
    ("line", "parsed"),
    [
        ("@@STEP@@read\n", ("step", "read")),
        ("@@STEP@@format_c\n", None),
        ("@@PCT@@40", ("pct", 40)),
        ("@@PCT@@400", ("pct", 100)),
        ("@@PCT@@²", None),
        ("@@PCT@@-1", None),
        ('@@RESULT@@{"ok": true}', ("result", {"ok": True})),
        ("@@RESULT@@[1]", None),
        ("@@RESULT@@{", None),
        ("Traceback (most recent call last):", None),
        pytest.param("@@RESULT@@" + "x" * (70 * 1024), None, id="over-the-line-limit"),
    ],
)
def test_parse_line(line, parsed):
    assert checker.parse_line(line) == parsed


GOOD = {
    "ok": True,
    "params": 1000,
    "cpu_rtf": 0.25,
    "torch": "2.6.0",
    "separator": "0.44.5",
    "keys": 12,
}


@pytest.mark.parametrize(
    ("raw", "result", "code"),
    [
        (GOOD, {k: v for k, v in GOOD.items() if k != "ok"}, ""),
        (
            {**GOOD, "cpu_rtf": float("nan")},
            {**{k: v for k, v in GOOD.items() if k != "ok"}, "cpu_rtf": None},
            "",
        ),
        ({**GOOD, "params": 10**12}, None, "load_failed"),
        ({**GOOD, "params": True}, None, "load_failed"),
        ({**GOOD, "keys": 0}, None, "load_failed"),
        ({"ok": False, "code": "key_mismatch"}, None, "key_mismatch"),
        ({"ok": False, "code": "rm -rf /"}, None, "load_failed"),
        ({"ok": "yes"}, None, "load_failed"),
        ([], None, "load_failed"),
    ],
)
def test_clean_result(raw, result, code):
    assert checker.clean_result(raw) == (result, code)


def test_an_odd_version_string_is_dropped():
    cleaned, _ = checker.clean_result({**GOOD, "torch": "2.6\n<script>", "separator": "x" * 100})
    assert cleaned["torch"] is None and cleaned["separator"] is None


# ── the parent against stub workers ──


def _stub(monkeypatch, body: str) -> None:
    script = textwrap.dedent(body)
    monkeypatch.setattr(checker, "worker_cmd", lambda folder, stems: [sys.executable, "-c", script])


def _run(tmp_path, **kwargs):
    steps: list[str] = []
    pcts: list[int] = []
    procs: list[object] = []
    cancel = kwargs.pop("cancel", threading.Event())
    run = checker.run_check(
        tmp_path,
        ["vocals"],
        on_step=steps.append,
        on_pct=pcts.append,
        cancel=cancel,
        register=procs.append,
        **kwargs,
    )
    return run, steps, pcts, procs


def test_a_passing_run(tmp_path, monkeypatch):
    _stub(
        monkeypatch,
        f"""
        import json, sys
        print("@@STEP@@read"); print("@@PCT@@50"); print("noise from a library")
        print("@@RESULT@@" + json.dumps({GOOD!r}))
        """,
    )
    run, steps, pcts, procs = _run(tmp_path)
    assert run.code is None and run.result["params"] == 1000
    assert steps == ["read"] and pcts == [50]
    assert run.tail == ["noise from a library"]
    assert procs[-1] is None  # deregistered


def test_a_refusal_passes_its_code(tmp_path, monkeypatch):
    _stub(monkeypatch, 'print(\'@@RESULT@@{"ok": false, "code": "gates_missing"}\')')
    run, *_ = _run(tmp_path)
    assert run.result is None and run.code == "gates_missing"


def test_a_worker_that_dies_without_a_result_is_load_failed(tmp_path, monkeypatch):
    _stub(monkeypatch, "import sys; print('Killed'); sys.exit(9)")
    run, *_ = _run(tmp_path)
    assert run.code == "load_failed" and run.tail == ["Killed"]


def _alive(pid: int) -> bool:
    from app.core.process import process_exists

    return process_exists(pid)


def test_a_run_over_the_time_limit_is_stopped_and_reaped(tmp_path, monkeypatch):
    _stub(
        monkeypatch,
        """
        import time
        while True:
            print("@@PCT@@1", flush=True)
            time.sleep(0.1)
        """,
    )
    started = time.monotonic()
    run, _steps, _pcts, procs = _run(tmp_path, timeout=1.0, stall=30, grace=1.0)
    assert run.code == "timeout"
    assert time.monotonic() - started < 20
    proc = procs[0]
    assert isinstance(proc, subprocess.Popen) and proc.poll() is not None


def test_a_silent_run_is_stopped(tmp_path, monkeypatch):
    _stub(monkeypatch, "import time; time.sleep(60)")
    run, *_ = _run(tmp_path, timeout=60, stall=1.0, grace=1.0)
    assert run.code == "timeout"


def test_cancel_stops_the_run(tmp_path, monkeypatch):
    _stub(monkeypatch, "import time; print('@@STEP@@build', flush=True); time.sleep(60)")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    run, _steps, _pcts, procs = _run(tmp_path, cancel=cancel, grace=1.0)
    assert run.code == "interrupted"
    assert procs[0].poll() is not None


def test_the_worker_command_is_fixed(tmp_path):
    cmd = checker.worker_cmd(tmp_path, ["vocals", "drums"])
    assert cmd == [
        sys.executable,
        "-m",
        "app.pipeline.user_model_check_worker",
        str(tmp_path),
        "vocals,drums",
    ]


def test_the_worker_env_hides_the_gpu_and_arms_the_watchdog():
    env = checker._worker_env()
    assert env["CUDA_VISIBLE_DEVICES"] == "-1" and env["STEMDECK_PARENT_PID"] == str(os.getpid())


# ── the worker's own pieces ──


def test_compare_keys():
    expected = {"a.weight": (2, 2), "l.to_gates.weight": (1, 2), "l.to_gates.bias": (1,)}
    assert worker.compare_keys(expected, dict(expected)) is None
    no_gates = {"a.weight": (2, 2)}
    assert worker.compare_keys(expected, no_gates) == "gates_missing"
    assert worker.compare_keys(expected, {**expected, "extra": (1,)}) == "key_mismatch"
    assert worker.compare_keys(expected, {**expected, "a.weight": (2, 3)}) == "key_mismatch"
    assert worker.compare_keys(expected, {"a.weight": (2, 2), "l.to_gates.bias": (1,)}) == (
        "gates_missing"
    )
    assert worker.compare_keys({"a": (1,), "b": (1,)}, {"a": (1,)}) == "key_mismatch"


def test_bad_arguments_are_refused_before_anything_loads(capsys):
    assert worker.main(["w"]) == 0
    assert json.loads(capsys.readouterr().out.split("@@RESULT@@")[1])["code"] == "load_failed"
    assert worker.main(["w", "x", "vocals,flute"]) == 0
    assert json.loads(capsys.readouterr().out.split("@@RESULT@@")[1])["code"] == (
        "stem_not_in_model"
    )


def test_version_tuple():
    assert worker._version_tuple("2.6.0+cpu") == (2, 6)
    assert worker._version_tuple("2.10.1") == (2, 10)
    assert worker._version_tuple("nonsense") == (0, 0)


# ── the real worker on tiny models ──

TINY = {
    "audio": {"chunk_size": 44_100, "num_channels": 2, "sample_rate": 44_100},
    "model": {
        "dim": 16,
        "depth": 1,
        "stereo": True,
        "num_stems": 2,
        "num_bands": 8,
        "stft_n_fft": 256,
        "stft_win_length": 256,
        "stft_hop_length": 64,
        "heads": 1,
        "dim_head": 8,
        "time_transformer_depth": 1,
        "freq_transformer_depth": 1,
        "mask_estimator_depth": 1,
        "flash_attn": True,
    },
    "training": {"instruments": ["vocals", "drums"]},
}


def _tiny_state():
    import torch
    from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer

    torch.manual_seed(0)
    arch, kwargs = fmt.clean_model(TINY["model"])
    return MelBandRoformer(**fmt.constructor_kwargs(arch, kwargs)).state_dict()


def _folder(tmp_path, state=None, *, kind="torch", raw: bytes | None = None):
    import torch
    import yaml

    folder = tmp_path / "user_0123456789ab"
    folder.mkdir()
    (folder / "config.yaml").write_text(yaml.safe_dump(TINY))
    path = folder / ("model.ckpt" if kind == "torch" else "model.safetensors")
    if raw is not None:
        path.write_bytes(raw)
    elif kind == "torch":
        torch.save(state, path)
    else:
        path.write_bytes(_safetensors(state))
    return folder


def _safetensors(state) -> bytes:
    header: dict = {}
    data = b""
    for name, tensor in state.items():
        raw = tensor.detach().float().contiguous().numpy().tobytes()
        header[name] = {
            "dtype": "F32",
            "shape": list(tensor.shape),
            "data_offsets": [len(data), len(data) + len(raw)],
        }
        data += raw
    blob = json.dumps(header).encode()
    return struct.pack("<Q", len(blob)) + blob + data


def _real(tmp_path, folder, stems=("vocals",)):
    return checker.run_check(
        folder,
        list(stems),
        on_step=lambda s: None,
        on_pct=lambda p: None,
        cancel=threading.Event(),
        register=lambda p: None,
    )


needs_separator = pytest.mark.skipif(not HAS_SEPARATOR, reason="audio-separator is not installed")


@needs_separator
def test_a_tiny_checkpoint_passes(tmp_path):
    run = _real(tmp_path, _folder(tmp_path, _tiny_state()), stems=("vocals", "drums"))
    assert run.code is None, run.tail
    assert run.result["params"] == fmt.estimate_params(*fmt.clean_model(TINY["model"]))
    assert run.result["cpu_rtf"] > 0


@needs_separator
def test_a_tiny_safetensors_file_passes(tmp_path):
    run = _real(tmp_path, _folder(tmp_path, _tiny_state(), kind="safetensors"))
    assert run.code is None, run.tail


@needs_separator
def test_a_renamed_key_is_key_mismatch(tmp_path):
    state = _tiny_state()
    name = next(iter(state))
    state["prefix." + name] = state.pop(name)
    assert _real(tmp_path, _folder(tmp_path, state)).code == "key_mismatch"


@needs_separator
def test_a_wrapped_state_dict_is_unwrapped_once(tmp_path):
    folder = _folder(tmp_path, {"state_dict": _tiny_state(), "epoch": 3})
    assert _real(tmp_path, folder).code is None


@needs_separator
def test_missing_gates_are_named_not_adapted(tmp_path):
    state = {k: v for k, v in _tiny_state().items() if ".to_gates." not in k}
    folder = _folder(tmp_path, state, kind="safetensors")
    assert _real(tmp_path, folder).code == "gates_missing"


@needs_separator
def test_a_nan_weight_is_non_finite(tmp_path):
    state = _tiny_state()
    name = next(k for k, v in state.items() if v.is_floating_point() and v.numel() > 1)
    state[name][0] = float("nan")
    assert _real(tmp_path, _folder(tmp_path, state)).code == "non_finite"


class _Planted:
    """A pickle that would make a folder when loaded unsafely."""

    def __init__(self, path: str) -> None:
        self.path = path

    def __reduce__(self):
        return (os.mkdir, (self.path,))


@needs_separator
def test_a_checkpoint_that_would_run_code_is_refused_and_nothing_runs(tmp_path):
    canary = tmp_path / "ran"
    state = dict(_tiny_state())
    state["planted"] = _Planted(str(canary))
    run = _real(tmp_path, _folder(tmp_path, state))
    assert run.code == "load_failed"
    assert not canary.exists()


@needs_separator
def test_a_declared_stem_the_model_lacks_is_refused_in_the_worker_too(tmp_path):
    run = _real(tmp_path, _folder(tmp_path, _tiny_state()), stems=("bass",))
    assert run.code == "stem_not_in_model"


@needs_separator
def test_a_file_whose_name_lies_about_its_format_is_refused(tmp_path):
    raw = _safetensors(_tiny_state())
    assert _real(tmp_path, _folder(tmp_path, raw=raw)).code == "bad_format"
