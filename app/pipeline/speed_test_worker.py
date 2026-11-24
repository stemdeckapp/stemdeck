"""One device's part of the speed test (Settings, Separation).

Run as its own process, once per device:
`python -m app.pipeline.speed_test_worker <cpu|cuda|mps>`. Separate from the
persistent separation worker (demucs_worker.py) on purpose: a failure here,
CUDA's included, ends only this process, and the separation worker's state is
never touched.

What it does: generates a deterministic 12 s stereo clip in memory, loads
htdemucs_6s the way the separation worker does (demucs.pretrained.get_model),
runs it once to warm up (model transfer, CUDA start, kernel selection; the time
is discarded), then times TIMED_RUNS more and reports the mean as the real-time
factor of a whole song (speed_test.song_rtf: Demucs runs whole segments, and a
short clip is mostly padding). Another Demucs model is measured the same way only when it is already
downloaded and expected to be quick (speed_test.cheap_to_measure); every other
model is estimated by the parent. Kim's Mel-Band RoFormer is always estimated
here: its loader belongs to the separation engine, not to this test.

Nothing is downloaded: a missing 6s checkpoint is reported as "model-missing",
and any download torch.hub would start raises instead. Nothing is written: the
clip and the output stay in memory.

Protocol, one line each on stdout (the parent merges stderr into it and keeps
the other lines as a log tail):
  "@@STEP@@<device>"   once, at the start
  "@@PCT@@<0..100>"    progress of this device
  "@@PARTIAL@@<json>"  once the 6s is measured, the same shape as the result
  "@@RESULT@@<json>"   last: {"device", "torch", "models": {id: {"rtf",
                       "peak_mb"}}} or {"device", "torch", "error": <code>}
STEMDECK_PARENT_PID arms the parent-death watchdog (app/core/process.py).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager

import numpy as np

from app.core import speed_test as st
from app.core.process import arm_parent_watchdog

_MIB = 1024 * 1024


def synthetic_clip(
    seconds: float = st.CLIP_SECONDS, rate: int = st.CLIP_RATE, seed: int = st.CLIP_SEED
) -> np.ndarray:
    """A (2, n) float32 clip: a few harmonic tones per channel plus a little
    noise, the same samples for the same arguments."""
    n = int(round(seconds * rate))
    t = np.arange(n, dtype=np.float64) / rate
    rng = np.random.default_rng(seed)
    channels = []
    for base in (110.0, 164.81):
        tone = np.zeros(n, dtype=np.float64)
        for k in range(1, 7):
            tone += np.sin(2 * np.pi * base * k * t + rng.uniform(0, 2 * np.pi)) / k
        tone += 0.1 * rng.standard_normal(n)
        channels.append(tone)
    clip = np.stack(channels)
    clip *= 0.5 / np.max(np.abs(clip))
    return clip.astype(np.float32)


def time_runs(
    run: Callable[[], None], clock: Callable[[], float] = time.perf_counter, runs: int = 2
) -> list[float]:
    """Run once untimed (the warm-up, discarded), then ``runs`` times timed.
    Returns the timed durations in seconds."""
    run()
    times: list[float] = []
    for _ in range(runs):
        start = clock()
        run()
        times.append(clock() - start)
    return times


def segment_of(model) -> float:
    """The segment length Demucs cuts the audio into for ``model``, in
    seconds: its own ``segment``, or its first sub-model's for a bag of models
    (htdemucs_ft, whose models share one). HTDEMUCS_SEGMENT if neither says."""
    for candidate in (model, *getattr(model, "models", ())):
        segment = getattr(candidate, "segment", None)
        if segment is not None:
            try:
                value = float(segment)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return st.HTDEMUCS_SEGMENT


def rtf_from_times(times: list[float], segment: float) -> float:
    """The song real-time factor from the timed runs on the clip (see
    speed_test.song_rtf: a run costs a whole number of segments)."""
    return st.song_rtf(sum(times) / len(times), segment)


def peak_rss_mb() -> int | None:
    """This process's peak resident memory in MiB, where the platform says it
    without an extra package (POSIX getrusage); None elsewhere (Windows)."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, KiB on Linux.
    return int(peak // _MIB) if sys.platform == "darwin" else int(peak // 1024)


def _emit(mark: str, value: object) -> None:
    sys.stdout.write(f"{mark}{value}\n")
    sys.stdout.flush()


def _result(device: str, torch_version: str | None, **fields: object) -> None:
    _emit(st.RESULT_MARK, json.dumps({"device": device, "torch": torch_version, **fields}))


class _DownloadRefused(RuntimeError):
    pass


@contextmanager
def _no_downloads(torch):
    """Inside the block, any torch.hub download raises: the speed test never
    fetches a model."""

    def refuse(*_args, **_kwargs):
        raise _DownloadRefused("the speed test downloads nothing")

    original = torch.hub.download_url_to_file
    torch.hub.download_url_to_file = refuse
    try:
        yield
    finally:
        torch.hub.download_url_to_file = original


def _measure(torch, model_id: str, clip: np.ndarray, device: str, pct: tuple[int, int]) -> dict:
    """One model's measured speed on ``device``: {"rtf", "peak_mb"}."""
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    start_pct, end_pct = pct
    model = get_model(model_id)
    model.eval()
    _emit(st.PCT_MARK, start_pct + (end_pct - start_pct) // 4)

    wav = torch.from_numpy(clip)
    # The separation worker's own normalisation (demucs_worker._run_one_job).
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / ref.std()
    batch = wav[None]

    def sync() -> None:
        if device == "cuda":
            torch.cuda.synchronize()
        elif device == "mps":
            torch.mps.synchronize()

    step = [0]

    def run() -> None:
        with torch.inference_mode():
            apply_model(
                model,
                batch,
                device=device,
                shifts=1,
                split=True,
                overlap=st.DEMUCS_OVERLAP,
                progress=False,
                num_workers=0,
                segment=None,
            )
        sync()
        step[0] += 1
        span = end_pct - start_pct
        _emit(st.PCT_MARK, start_pct + span // 4 + (3 * span // 4) * step[0] // (1 + st.TIMED_RUNS))

    if device == "cuda":
        torch.cuda.reset_peak_memory_stats()
    times = time_runs(run, runs=st.TIMED_RUNS)
    if device == "cuda":
        peak: int | None = int(torch.cuda.max_memory_allocated() // _MIB)
    elif device == "cpu":
        peak = peak_rss_mb()
    else:
        peak = None
    # Off the GPU before the next model loads; the process ends soon after.
    model.cpu()
    if device == "cuda":
        torch.cuda.empty_cache()
    return {"rtf": rtf_from_times(times, segment_of(model)), "peak_mb": peak}


def _log_failure(device: str, model_id: str, exc: BaseException) -> None:
    """On stderr, which the parent keeps as the log tail; never in the result."""
    sys.stderr.write(f"speed test: {model_id} on {device} failed: {type(exc).__name__}: {exc}\n")
    sys.stderr.flush()


def _device_usable(torch, device: str) -> bool:
    if device == "cpu":
        return True
    if device == "cuda":
        return bool(torch.cuda.is_available())
    mps = getattr(torch.backends, "mps", None)
    return device == "mps" and mps is not None and bool(mps.is_available())


def main(argv: list[str]) -> int:
    device = argv[1] if len(argv) > 1 else ""
    if device not in st.DEVICE_ORDER:
        _result(device, None, error="device-unavailable")
        return 0
    arm_parent_watchdog()
    _emit(st.STEP_MARK, device)
    _emit(st.PCT_MARK, 0)
    version = st.torch_version()
    try:
        import torch

        from app.core import stem_models
    except Exception:  # noqa: BLE001 -- a torch that cannot load is a result, not a crash
        _result(device, version, error="torch-missing")
        return 0
    _emit(st.PCT_MARK, 10)
    if not _device_usable(torch, device):
        _result(device, version, error="device-unavailable")
        return 0
    if not stem_models.model_downloaded(st.MEASURED_MODEL):
        _result(device, version, error="model-missing")
        return 0

    clip = synthetic_clip()
    extra = [
        m.id
        for m in stem_models.MODELS.values()
        if m.engine == "demucs" and m.id != st.MEASURED_MODEL
    ]
    with _no_downloads(torch):
        try:
            models = {st.MEASURED_MODEL: _measure(torch, st.MEASURED_MODEL, clip, device, (10, 70))}
        except _DownloadRefused:
            _result(device, version, error="model-missing")
            return 0
        except Exception as exc:  # noqa: BLE001 -- reported as this device failing
            _log_failure(device, st.MEASURED_MODEL, exc)
            _result(device, version, error="failed")
            return 0
        # Kept by the parent if the run ends while another model is measured.
        _emit(
            st.PARTIAL_MARK,
            json.dumps({"device": device, "torch": version, "models": dict(models)}),
        )
        factor = st.machine_factor(models[st.MEASURED_MODEL]["rtf"], device)
        for model_id in extra:
            if not (
                stem_models.model_downloaded(model_id)
                and st.cheap_to_measure(model_id, device, factor)
            ):
                continue
            try:
                models[model_id] = _measure(torch, model_id, clip, device, (70, 95))
            except Exception as exc:  # noqa: BLE001 -- that model stays an estimate
                _log_failure(device, model_id, exc)
    _emit(st.PCT_MARK, 100)
    _result(device, version, models=models)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
