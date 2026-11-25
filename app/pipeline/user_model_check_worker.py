"""The test run of a user model (My models), one model per process.

`python -m app.pipeline.user_model_check_worker <model folder> <stem,stem>`,
started by app/core/user_model_check.py. Separate from the persistent
separation worker on purpose: whatever this model does, it ends only this
process, on the CPU, and the separation worker's state is never touched.

What it does, in this order, refusing at the first failure:
1. reads the folder's model file and config.yaml again, with the same checks
   the server made (app/core/user_model_format.py), on the same bytes;
2. requires torch 2.6 or newer, read from its package metadata before torch
   is imported (weights_only loading is only trusted from there);
3. builds the class on the "meta" device, which allocates nothing, and takes
   the exact names and shapes of the weights it expects;
4. compares them with the file's: a safetensors file from its header, before
   any weight is read; a torch checkpoint loaded with
   torch.load(weights_only=True), which refuses anything but tensors and
   plain containers. Names are compared exactly, no prefix stripped and
   nothing renamed. A checkpoint that differs only by the attention gates
   (to_gates) is refused as "gates_missing", never adapted;
5. refuses any weight that is not finite, builds the model on the CPU and
   loads the weights strictly;
6. runs it once on a deterministic synthetic clip of
   config.USER_MODEL_CHECK_CLIP_SEC seconds, and checks the output's shape,
   length and finiteness. Its time gives the CPU real-time factor.

Protocol, one line each on stdout (the parent merges stderr in and keeps the
rest as a log tail, never shown):
  "@@STEP@@<read|build|run>"
  "@@PCT@@<0..100>"
  "@@RESULT@@<json>"  last: {"ok": true, "params", "cpu_rtf", "torch",
                      "separator", "keys"} or {"ok": false, "code": <code>}
The code is one of user_model_format.REASONS; an exception's own text goes to
stderr only. STEMDECK_PARENT_PID arms the parent-death watchdog. On Linux and
macOS the address space is capped at config.USER_MODEL_CHECK_MEMORY_BYTES.
"""

from __future__ import annotations

import json
import math
import sys
import time
import traceback
from pathlib import Path

from app.core import user_model_format as fmt
from app.core import user_models
from app.core.config import (
    MAX_USER_MODEL_CONFIG_BYTES,
    MAX_USER_MODEL_PARAMS,
    USER_MODEL_CHECK_CLIP_SEC,
    USER_MODEL_CHECK_MEMORY_BYTES,
)
from app.core.process import arm_parent_watchdog
from app.core.stem_models import SELECTABLE_STEMS
from app.core.user_model_check import PCT_MARK, RESULT_MARK, STEP_MARK

MIN_TORCH = (2, 6)
_GATE_SUFFIXES = (".to_gates.weight", ".to_gates.bias")
SAMPLE_RATE = fmt.SAMPLE_RATE
MODEL_NAMES = user_models.MODEL_FILES
CONFIG_NAME = user_models.CONFIG_FILE


def _emit(mark: str, value: object) -> None:
    sys.stdout.write(f"{mark}{value}\n")
    sys.stdout.flush()


def _result(**fields: object) -> None:
    _emit(RESULT_MARK, json.dumps(fields))


def _limit_memory() -> None:
    try:
        import resource
    except ImportError:
        return  # Windows: the parameter limit is the guard
    try:
        resource.setrlimit(
            resource.RLIMIT_AS, (USER_MODEL_CHECK_MEMORY_BYTES, USER_MODEL_CHECK_MEMORY_BYTES)
        )
    except (ValueError, OSError):
        sys.stderr.write("could not limit the test run's memory\n")


def _version_tuple(text: str) -> tuple[int, int]:
    parts = text.split("+", 1)[0].split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return (0, 0)


def _package_version(name: str) -> str | None:
    from importlib import metadata

    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def compare_keys(expected: dict[str, tuple], got: dict[str, tuple]) -> str | None:
    """None when ``got`` has exactly the expected names and shapes; otherwise
    "gates_missing" when the only difference is absent attention gates, else
    "key_mismatch"."""
    missing = set(expected) - set(got)
    unexpected = set(got) - set(expected)
    if missing or unexpected:
        if not unexpected and missing and all(k.endswith(_GATE_SUFFIXES) for k in missing):
            return "gates_missing"
        return "key_mismatch"
    for name, shape in expected.items():
        if tuple(got[name]) != tuple(shape):
            return "key_mismatch"
    return None


def unwrap_state(obj: object) -> dict:
    """A checkpoint's state dict: the object itself, or the dict under
    "state_dict" or "model" one level down. Every value must be a tensor."""
    import torch

    if isinstance(obj, dict) and not all(isinstance(v, torch.Tensor) for v in obj.values()):
        for key in ("state_dict", "model"):
            inner = obj.get(key)
            if isinstance(inner, dict):
                obj = inner
                break
    if not isinstance(obj, dict) or not obj:
        raise fmt.Refused("key_mismatch")
    if not all(isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in obj.items()):
        raise fmt.Refused("key_mismatch")
    return obj


def _model_file(folder: Path) -> tuple[Path, str]:
    found = [
        (folder / name, kind) for kind, name in MODEL_NAMES.items() if (folder / name).is_file()
    ]
    if len(found) != 1:
        raise fmt.Refused("load_failed")
    return found[0]


def _classes():
    try:
        from audio_separator.separator.uvr_lib_v5.roformer.bs_roformer import BSRoformer
        from audio_separator.separator.uvr_lib_v5.roformer.mel_band_roformer import MelBandRoformer
    except Exception as exc:  # noqa: BLE001 -- a package that cannot load is a result
        raise fmt.Refused("unavailable") from exc
    return {"mel_band_roformer": MelBandRoformer, "bs_roformer": BSRoformer}


def check(folder: Path, stems: list[str]) -> dict:
    """The whole test run; returns the result's fields. Raises fmt.Refused."""
    _emit(STEP_MARK, "read")
    _emit(PCT_MARK, 0)
    path, kind = _model_file(folder)
    if fmt.sniff_format(path) != kind:
        raise fmt.Refused("bad_format")
    config_path = folder / CONFIG_NAME
    try:
        if config_path.stat().st_size > MAX_USER_MODEL_CONFIG_BYTES:
            raise fmt.Refused("too_large")
        raw = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise fmt.Refused("config_missing") from exc
    except OSError as exc:
        raise fmt.Refused("load_failed") from exc
    cleaned = fmt.clean_config(fmt.parse_config(raw), stems)

    torch_version = _package_version("torch")
    if torch_version is None:
        raise fmt.Refused("torch_missing")
    if _version_tuple(torch_version) < MIN_TORCH:
        raise fmt.Refused("torch_too_old")
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 -- a torch that cannot load is a result
        raise fmt.Refused("torch_missing") from exc
    cls = _classes()[cleaned.arch]
    _emit(PCT_MARK, 10)

    _emit(STEP_MARK, "build")
    try:
        with torch.device("meta"):
            skeleton = fmt.build_model(cls, cleaned.arch, cleaned.kwargs)
    except Exception as exc:  # noqa: BLE001 -- the whitelist let through what the class refuses
        raise fmt.Refused("load_failed") from exc
    expected = {k: tuple(v.shape) for k, v in skeleton.state_dict().items()}
    params = sum(p.numel() for p in skeleton.parameters())
    del skeleton
    if params > MAX_USER_MODEL_PARAMS:
        raise fmt.Refused("too_big_model")
    _emit(PCT_MARK, 20)

    if kind == "safetensors":
        header, _start = fmt.read_safetensors_header(path)
        mismatch = compare_keys(expected, {k: v.shape for k, v in header.items()})
        if mismatch:
            raise fmt.Refused(mismatch)
        state = fmt.load_safetensors(path)
    else:
        try:
            loaded = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
        except Exception as exc:  # noqa: BLE001 -- refused by the safe unpickler, or torn
            raise fmt.Refused("load_failed") from exc
        state = unwrap_state(loaded)
        mismatch = compare_keys(expected, {k: tuple(v.shape) for k, v in state.items()})
        if mismatch:
            raise fmt.Refused(mismatch)
    _emit(PCT_MARK, 40)
    for tensor in state.values():
        if tensor.is_floating_point() and not bool(torch.isfinite(tensor).all()):
            raise fmt.Refused("non_finite")
    try:
        model = fmt.build_model(cls, cleaned.arch, cleaned.kwargs)
        model.load_state_dict(state, strict=True)
    except RuntimeError as exc:
        raise fmt.Refused("key_mismatch") from exc
    except Exception as exc:  # noqa: BLE001
        raise fmt.Refused("load_failed") from exc
    del state
    model.eval()
    _emit(PCT_MARK, 60)

    _emit(STEP_MARK, "run")
    from app.pipeline.speed_test_worker import synthetic_clip

    hop = int(cleaned.kwargs.get("stft_hop_length", fmt.DEFAULTS[cleaned.arch]["stft_hop_length"]))
    n = hop * max(1, round(USER_MODEL_CHECK_CLIP_SEC * SAMPLE_RATE / hop))
    clip = synthetic_clip(seconds=n / SAMPLE_RATE + 0.01)[:, :n]
    x = torch.from_numpy(clip.copy()).unsqueeze(0)
    try:
        with torch.no_grad():
            start = time.perf_counter()
            y = model(x)
            elapsed = time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001 -- MemoryError included
        raise fmt.Refused("forward_failed") from exc
    num_stems = cleaned.kwargs["num_stems"]
    if not isinstance(y, torch.Tensor):
        raise fmt.Refused("forward_failed")
    if y.ndim == 3 and num_stems == 1:
        y = y.unsqueeze(1)
    if y.ndim != 4 or tuple(y.shape[:3]) != (1, num_stems, 2):
        raise fmt.Refused("forward_failed")
    if abs(int(y.shape[-1]) - n) > hop:
        raise fmt.Refused("forward_failed")
    if not bool(torch.isfinite(y).all()):
        raise fmt.Refused("non_finite")
    _emit(PCT_MARK, 100)
    rtf = elapsed / (n / SAMPLE_RATE)
    return {
        "ok": True,
        "params": int(params),
        "cpu_rtf": rtf if math.isfinite(rtf) else None,
        "torch": torch_version,
        "separator": _package_version("audio-separator"),
        "keys": len(expected),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        _result(ok=False, code="load_failed")
        return 0
    folder = Path(argv[1])
    stems = [s for s in argv[2].split(",") if s]
    if not stems or any(s not in SELECTABLE_STEMS for s in stems) or len(set(stems)) != len(stems):
        _result(ok=False, code="stem_not_in_model")
        return 0
    arm_parent_watchdog()
    _limit_memory()
    try:
        fields = check(folder, stems)
    except fmt.Refused as exc:
        if exc.__cause__ is not None:
            traceback.print_exception(exc.__cause__, file=sys.stderr)
        _result(ok=False, code=exc.code)
        return 0
    except Exception:  # noqa: BLE001 -- said as a code; the text goes to the log tail
        traceback.print_exc(file=sys.stderr)
        _result(ok=False, code="load_failed")
        return 0
    _result(**fields)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
