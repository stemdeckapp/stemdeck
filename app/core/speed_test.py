"""The hardware speed test behind Settings, Separation: what it measures, what
it recommends, and when a saved result still describes this machine.

The static recommendation (stem_models.recommended) guesses from the device
and the GPU's memory. The speed test replaces the guess with a measurement on
the user's own machine: htdemucs_6s on a short generated clip, once per
device, in a one-shot subprocess (app/pipeline/speed_test_worker.py), plus a
write test of the jobs folder. Models that are not downloaded are never
downloaded: their speed is estimated from the measured 6s.

What lives here:

- the constants of the test (clip, budget, disk sizes, time limits),
- the hardware fingerprint a saved result is tied to, and staleness,
- the estimates and the recommendation, as pure functions,
- the check that turns a saved result (settings.json, which a person can
  edit) back into a trusted dict,
- the disk write test and the subprocess runner for one device.

Nothing here imports torch: the torch version comes from package metadata,
read the same way by the server and the worker, so the two always agree. The
state machine and the routes are app/api/speed_test.py.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import math
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.core import config, device_reason, stem_models

logger = logging.getLogger("stemdeck.speed_test")

VERSION = 1

# The generated clip: 12 s of stereo 44.1 kHz harmonic tones plus noise, the
# same every run (a fixed seed). Demucs and RoFormer do the same work on any
# audio of a given length, so the content does not change the timing.
CLIP_SECONDS = 12.0
CLIP_RATE = 44_100
CLIP_SEED = 20261123

# The model every device is measured with. Every install that separates at
# all has it, and the registry's numbers for the others are relative to it.
MEASURED_MODEL = "htdemucs_6s"
KIM = "kim_mel_roformer"

# The whole plan must separate a 4-minute song in 3 minutes (real-time factor
# 0.75), or a cheaper plan is recommended.
SONG_MINUTES = 4.0
BUDGET_MINUTES = 3.0

# The real-time factors are for a whole song, as the registry's are, so the
# measured clip time is scaled to SONG_MINUTES of audio (song_rtf below).
SONG_SECONDS = SONG_MINUTES * 60

# How Demucs cuts audio (demucs/apply.py, apply_model with split=True, read in
# demucs 4.0.1): segments of model.segment seconds (7.8 s for htdemucs_6s and
# each htdemucs_ft model, the checkpoint's kwargs), one starting every
# int((1 - overlap) * segment_length) samples, overlap 0.25 as the separation
# worker passes it. Every segment is padded to the full length before the
# model sees it (TensorChunk.padded, HTDemucs.forward pads to the training
# length), so every segment costs the same. shifts=1 (the worker's Standard
# quality) first pads the input by up to 0.5 s at a random offset.
HTDEMUCS_SEGMENT = 7.8
DEMUCS_OVERLAP = 0.25
_DEMUCS_MAX_SHIFT_SEC = 0.5

# A Demucs model other than the 6s is measured for real only when it is
# downloaded and the warm-up plus the two timed runs are expected to take at
# most this long (estimated from the measured 6s). htdemucs_ft on a GPU takes
# a few seconds; on a CPU about a minute, so there it stays an estimate.
REAL_MEASURE_MAX_SEC = float(config.SPEED_TEST_REAL_MEASURE_MAX_SEC)
TIMED_RUNS = 2

# The disk test: one file of this size, synced, in the jobs folder
# (app/core/config.py has the meaning of each).
_MIB = 1024 * 1024
DISK_TEST_BYTES = config.SPEED_TEST_DISK_MIB * _MIB
DISK_CHUNK_BYTES = 4 * _MIB
DISK_MIN_FREE_BYTES = config.SPEED_TEST_DISK_MIN_FREE_MIB * _MIB
DISK_SLOW_MB_S = float(config.SPEED_TEST_DISK_SLOW_MB_S)
DISK_TIMEOUT_SEC = float(config.TIMEOUT_SPEED_TEST_DISK)
DISK_TEMP_PREFIX = ".stemdeck-speedtest-"
DISK_TEMP_SUFFIX = ".tmp"
_DISK_NAME_TRIES = 3
# The only names the stale-file sweep removes: exactly what _new_temp_name
# makes. A temp file younger than this may still be in use (a disk test that
# timed out keeps writing until its write returns).
_DISK_TEMP_RE = re.compile(r"^\.stemdeck-speedtest-[0-9a-f]{16}\.tmp$")
DISK_TEMP_STALE_SEC = 600

# One device's subprocess: ended after this long in all, or after this long
# with no line from it; given SPEED_TEST_KILL_WAIT_SEC to exit before it and
# everything it started is killed.
DEVICE_TIMEOUT_SEC = float(config.TIMEOUT_SPEED_TEST_DEVICE)
DEVICE_STALL_SEC = float(config.TIMEOUT_SPEED_TEST_STALL)
KILL_WAIT_SEC = float(config.SPEED_TEST_KILL_WAIT_SEC)
_REAP_WAIT_SEC = 10

# The order devices are measured in, and every device the test knows.
DEVICE_ORDER: tuple[str, ...] = ("cpu", "cuda", "mps")
# On a tie between GPUs, CUDA first.
_TIE_ORDER: tuple[str, ...] = ("cuda", "mps", "cpu")
# The CPU is recommended over a measured GPU only when the GPU is at least
# this much slower: two timings of 12 s vary by a few percent, and a GPU
# leaves the CPU free for the rest of the app.
GPU_MARGIN = 1.10

# The worker's protocol, one line each on its stdout. A line longer than this
# is not the worker's and is dropped.
STEP_MARK = "@@STEP@@"
PCT_MARK = "@@PCT@@"
PARTIAL_MARK = "@@PARTIAL@@"
RESULT_MARK = "@@RESULT@@"
MAX_LINE_CHARS = 64 * 1024

Reason = Literal[
    "fits",
    "over-budget",
    "gpu-slower-than-cpu",
    "disk-slow",
    "no-gpu",
    "gpu-failed",
    "cpu-over-budget",
]
REASONS: tuple[str, ...] = (
    "fits",
    "over-budget",
    "gpu-slower-than-cpu",
    "disk-slow",
    "no-gpu",
    "gpu-failed",
    "cpu-over-budget",
)

# Why one device could not be measured. The worker sends one of these; any
# other word reads as "failed".
DEVICE_ERRORS: tuple[str, ...] = (
    "torch-missing",
    "model-missing",
    "device-unavailable",
    "timeout",
    "stalled",
    "failed",
)
# Why the disk test was skipped.
DISK_SKIPS: tuple[str, ...] = ("low-space", "unavailable", "name-taken", "failed", "timeout")
# What GET /api/speed-test says a failed run failed of (error_code).
RUN_ERRORS: tuple[str, ...] = ("torch-missing", "model-missing", "failed", "timeout", "stalled")

# The fixed sentences the API answers with. Never a path, never an exception.
ERROR_TORCH = "PyTorch is not installed or cannot load, so the benchmark cannot run."
ERROR_MODEL = (
    "The separation model is not downloaded yet. Separate one song, then run the benchmark again."
)
ERROR_FAILED = "The benchmark could not finish. The log has the details."
ERROR_TIMEOUT = "The benchmark took too long and was stopped. The log has the details."


def run_error(codes: list[str]) -> tuple[str, str]:
    """The (error_code, fixed sentence) of a run in which no device could be
    measured, from each device's code."""
    unique = set(codes)
    if unique == {"torch-missing"}:
        return "torch-missing", ERROR_TORCH
    if "model-missing" in unique:
        return "model-missing", ERROR_MODEL
    if unique == {"timeout"}:
        return "timeout", ERROR_TIMEOUT
    if unique == {"stalled"}:
        return "stalled", ERROR_TIMEOUT
    return "failed", ERROR_FAILED


# ── the hardware fingerprint ──


@lru_cache(maxsize=1)
def cpu_name() -> str:
    """The CPU's marketing name, read without starting anything heavy. Cached:
    it cannot change while the process runs."""
    name = ""
    try:
        if sys.platform == "win32":
            import winreg

            key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                name = str(winreg.QueryValueEx(handle, "ProcessorNameString")[0])
        elif sys.platform == "darwin":
            out = subprocess.run(  # noqa: S603 -- fixed args, the system's own tool
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            name = out.stdout if out.returncode == 0 else ""
        else:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                if key.strip() in ("model name", "Hardware", "Processor") and value.strip():
                    name = value
                    break
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.info("could not read the CPU name: %s", exc)
    name = " ".join(name.split()) or platform.processor() or platform.machine() or "unknown"
    return name[:200]


def torch_version() -> str | None:
    """The installed torch's version, from its package metadata: no import.
    The worker reads it the same way, so a saved result and the machine agree
    on it exactly."""
    try:
        return importlib.metadata.version("torch")
    except Exception:  # noqa: BLE001 -- not installed, or metadata unreadable
        return None


def measure_devices(available: list[str]) -> list[str]:
    """The devices the test measures, in DEVICE_ORDER: the CPU always, then
    each GPU the app itself can use (an available_torch_devices() result)."""
    return ["cpu"] + [d for d in DEVICE_ORDER[1:] if d in available]


def _available_devices() -> list[str]:
    """The app's own device probe, the one Settings and every job use."""
    return config.available_torch_devices()


def current_hw(torch: str | None = None) -> dict:
    """This machine's fingerprint. ``torch`` is the version the worker
    reported; None reads it here, from the same package metadata."""
    gpu = device_reason.nvidia_gpu() or {}
    name = gpu.get("name")
    memory = gpu.get("memory_mb")
    return {
        "cpu": cpu_name(),
        "cores": os.cpu_count() or 0,
        "gpu": name if isinstance(name, str) and name else None,
        "vram_mb": memory if isinstance(memory, int) and not isinstance(memory, bool) else None,
        "devices": measure_devices(_available_devices()),
        "torch": torch if torch is not None else torch_version(),
    }


_FINGERPRINT_KEYS: tuple[str, ...] = ("cpu", "cores", "gpu", "vram_mb", "devices", "torch")


def is_stale(hw: Mapping[str, object], current: Mapping[str, object]) -> bool:
    """Whether a result measured on ``hw`` no longer describes ``current``:
    another CPU or GPU, more or less GPU memory, another torch, or a device
    that came or went."""
    return any(hw.get(key) != current.get(key) for key in _FINGERPRINT_KEYS)


# ── estimates and the recommendation (pure) ──


def registry_rtf(model_id: str, device: str) -> float:
    """The registry's real-time factor of ``model_id`` on ``device``: its CPU
    number on the CPU, its GPU number on any GPU."""
    model = stem_models.MODELS[model_id]
    if device == "cpu" or model.gpu_rtf is None:
        return model.cpu_rtf
    return model.gpu_rtf


def machine_factor(measured_rtf: float, device: str) -> float:
    """How much slower (above 1) or faster this machine is than the registry's
    numbers, from the measured 6s."""
    return measured_rtf / registry_rtf(MEASURED_MODEL, device)


def demucs_chunks(
    seconds: float,
    segment: float = HTDEMUCS_SEGMENT,
    rate: int = CLIP_RATE,
    overlap: float = DEMUCS_OVERLAP,
) -> float:
    """How many segments Demucs runs the model on for ``seconds`` of audio:
    one per stride, ``ceil(length / stride)``, the length of apply_model's
    ``range(0, length, stride)``. With shifts=1 the input is up to 0.5 s
    longer, at a random offset; the mean of the two ends is returned (for the
    clip and the song below they are the same number)."""
    segment_length = int(rate * segment)
    stride = int((1 - overlap) * segment_length)
    length = int(round(seconds * rate))
    shortest = math.ceil(length / stride)
    longest = math.ceil((length + int(_DEMUCS_MAX_SHIFT_SEC * rate)) / stride)
    return (shortest + longest) / 2


def song_rtf(run_seconds: float, segment: float = HTDEMUCS_SEGMENT) -> float:
    """The real-time factor of a SONG_MINUTES song, from the seconds one run
    on the CLIP_SECONDS clip took.

    Every segment costs the same, so a run costs (segments x one segment).
    The 12 s clip is 3 segments of 7.8 s (23.4 s of model input for 12 s of
    audio) and a 240 s song is 42 (327.6 s for 240 s): dividing the clip's
    time by 12 s would make every speed about 1.43 times too slow."""
    per_segment = run_seconds / demucs_chunks(CLIP_SECONDS, segment)
    return per_segment * demucs_chunks(SONG_SECONDS, segment) / SONG_SECONDS


def clip_run_seconds(rtf: float, segment: float = HTDEMUCS_SEGMENT) -> float:
    """How long one run on the clip takes at a song real-time factor ``rtf``:
    song_rtf() the other way round."""
    return (
        rtf
        * SONG_SECONDS
        / demucs_chunks(SONG_SECONDS, segment)
        * demucs_chunks(CLIP_SECONDS, segment)
    )


def cheap_to_measure(model_id: str, device: str, factor: float) -> bool:
    """Whether measuring ``model_id`` for real (a warm-up and the timed runs)
    is expected to take at most REAL_MEASURE_MAX_SEC on this machine."""
    one_run = clip_run_seconds(registry_rtf(model_id, device) * factor)
    return one_run * (1 + TIMED_RUNS) <= REAL_MEASURE_MAX_SEC


def device_table(device: str, measured: Mapping[str, Mapping[str, object]]) -> dict:
    """Every registry model's speed on ``device``: the measured ones as
    measured, the rest estimated from the measured 6s. ``measured`` maps a
    model id to {"rtf", "peak_mb"} and must hold the 6s."""
    six = measured[MEASURED_MODEL]
    factor = machine_factor(float(six["rtf"]), device)
    table: dict[str, dict] = {}
    for model_id in stem_models.MODELS:
        got = measured.get(model_id)
        if got is not None:
            table[model_id] = {
                "rtf": round(float(got["rtf"]), 4),
                "peak_mb": got.get("peak_mb"),
                "measured": True,
            }
        else:
            table[model_id] = {
                "rtf": round(registry_rtf(model_id, device) * factor, 4),
                "measured": False,
                "estimated_from": MEASURED_MODEL,
            }
    return table


@dataclass(frozen=True)
class Plan:
    """One way to separate a song: the vocal model and its total real-time
    factor (every pass added up)."""

    vocals: str
    rtf: float

    @property
    def minutes(self) -> float:
        return self.rtf * SONG_MINUTES

    @property
    def fits(self) -> bool:
        return self.minutes <= BUDGET_MINUTES + 1e-9

    def stems(self) -> dict[str, str]:
        return {
            stem: (self.vocals if stem == "vocals" else MEASURED_MODEL)
            for stem in stem_models.SELECTABLE_STEMS
        }


def plans(table: Mapping[str, Mapping[str, object]], kim_ok: bool) -> list[Plan]:
    """The plans on one device, most preferred first: Kim for the vocals and
    the 6s for the rest (two passes), when Kim may run there; then the 6s
    alone, which exists everywhere."""
    six = float(table[MEASURED_MODEL]["rtf"])
    out: list[Plan] = []
    if kim_ok and KIM in table:
        out.append(Plan(KIM, float(table[KIM]["rtf"]) + six))
    out.append(Plan(MEASURED_MODEL, six))
    return out


def choose_plan(table: Mapping[str, Mapping[str, object]], kim_ok: bool) -> Plan:
    """The most preferred plan that fits the budget, else the fastest one."""
    options = plans(table, kim_ok)
    for plan in options:
        if plan.fits:
            return plan
    return min(options, key=lambda p: p.rtf)


def safe_stems() -> dict[str, str]:
    """The plan every install that separates at all can run: the 6s alone."""
    return Plan(MEASURED_MODEL, 0.0).stems()


def recommend(
    devices: Mapping[str, Mapping[str, Mapping[str, object]]],
    disk: Mapping[str, object],
    kim_devices: frozenset[str] | set[str],
    failed: Mapping[str, str] | None = None,
) -> dict:
    """The device and the model per stem to use, from the measured tables.

    The fastest GPU measured (by the 6s), unless the CPU beats it by
    GPU_MARGIN or more, then the CPU. On that device, the most preferred plan
    that separates a 4-minute song within BUDGET_MINUTES, else the fastest
    plan. ``kim_devices`` are the devices Kim may run on here (available,
    recommended there, enough GPU memory): the same rule as the static
    recommendation, so the test can only make the plan cheaper, never pick a
    model the static rule rules out. ``failed`` holds the devices that could
    not be measured, with why.

    ``reason`` is one code, the first that applies:
    "gpu-failed" (the plan is on the CPU and a GPU failed to be measured),
    "cpu-over-budget" (Kim cannot run there and even the 6s alone is over
    the budget), "no-gpu" (only the CPU was measured), "gpu-slower-than-cpu",
    "over-budget" (the preferred plan, or every plan, is over the budget),
    else "fits". A slow disk changes no plan: it is always in ``reasons``, and
    is ``reason`` when the plan reason is "fits", the one with nothing to say."""
    failed = failed or {}
    measured = [d for d in DEVICE_ORDER if d in devices and MEASURED_MODEL in devices[d]]
    if not measured:
        raise ValueError("no device was measured")

    def rtf(device: str) -> float:
        return float(devices[device][MEASURED_MODEL]["rtf"])

    gpus = [d for d in measured if d != "cpu"]
    best_gpu = min(gpus, key=lambda d: (rtf(d), _TIE_ORDER.index(d))) if gpus else None
    if best_gpu is None or ("cpu" in measured and rtf(best_gpu) >= GPU_MARGIN * rtf("cpu")):
        device = "cpu"
    else:
        device = best_gpu

    options = plans(devices[device], device in kim_devices)
    plan = choose_plan(devices[device], device in kim_devices)

    if device == "cpu" and any(d != "cpu" for d in failed):
        reason: str = "gpu-failed"
    elif len(options) == 1 and not plan.fits:
        reason = "cpu-over-budget"
    elif not gpus:
        reason = "no-gpu"
    elif device == "cpu":
        reason = "gpu-slower-than-cpu"
    elif plan != options[0] or not plan.fits:
        reason = "over-budget"
    else:
        reason = "fits"
    reasons = [reason]
    if disk.get("slow") is True:
        reasons.append("disk-slow")
    return {
        "device": device,
        "stems": plan.stems(),
        "estimate_minutes_4min": round(plan.minutes, 2),
        "budget_minutes": BUDGET_MINUTES,
        "reason": "disk-slow" if reason == "fits" and len(reasons) > 1 else reason,
        "reasons": reasons,
    }


def measured_plan(
    result: Mapping[str, object] | None, device: str, kim_ok: bool
) -> dict[str, str] | None:
    """The model per stem the saved test recommends for a job on ``device``,
    or None when there is no fresh result for it. ``result`` is a public
    result (sanitize() plus "stale"). Worked out again from the measured
    table, so a device the user chose over the recommended one gets the plan
    its own numbers support, and ``kim_ok`` is today's answer, not the one at
    test time. A device the test could not measure (a CUDA run that timed out)
    gets the 6s alone until the test is run again: whatever stopped the test
    there may stop a long Kim pass too."""
    if not result or result.get("stale") is not False:
        return None
    tables = result.get("devices")
    if isinstance(tables, Mapping) and device in tables:
        return choose_plan(tables[device], kim_ok).stems()
    failed = result.get("failed")
    if isinstance(failed, Mapping) and device in failed:
        return safe_stems()
    return None


# ── a saved result, checked ──
#
# Every value is type-checked before it is compared or looked up: a hand-
# edited settings.json can hold a list where a string was, and a list used as
# a dict key raises TypeError. sanitize() never raises.


def _finite(value: object, low: float = 0.0, high: float = 1e6) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:  # an int beyond a float's range
        return None
    return value if math.isfinite(value) and low < value <= high else None


def _int_or_none(value: object, high: int = 10_000_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= high else None


def _short_str(value: object, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _one_of(value: object, allowed: tuple[str, ...]) -> str | None:
    """``value`` when it is a string in ``allowed``, else None."""
    return value if isinstance(value, str) and value in allowed else None


def _clean_hw(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    devices = raw.get("devices")
    if not isinstance(devices, list) or not all(_one_of(d, DEVICE_ORDER) for d in devices):
        return None
    cpu = _short_str(raw.get("cpu"))
    cores = _int_or_none(raw.get("cores"), 100_000)
    if cpu is None or cores is None:
        return None
    return {
        "cpu": cpu,
        "cores": cores,
        "gpu": _short_str(raw.get("gpu")),
        "vram_mb": _int_or_none(raw.get("vram_mb")),
        "devices": [d for d in DEVICE_ORDER if d in devices],
        "torch": _short_str(raw.get("torch"), 64),
    }


def _clean_disk(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    raw_skipped = raw.get("skipped")
    skipped = _one_of(raw_skipped, DISK_SKIPS)
    if raw_skipped is not None and skipped is None:
        return None
    speed = _finite(raw.get("write_mb_s"), 0.0, 1e7)
    if skipped is None and speed is None:
        return None
    return {
        "write_mb_s": None if skipped is not None or speed is None else round(speed, 1),
        "skipped": skipped,
        "slow": skipped is None and speed is not None and speed < DISK_SLOW_MB_S,
    }


def _clean_table(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    six = raw.get(MEASURED_MODEL)
    if not isinstance(six, Mapping) or six.get("measured") is not True:
        return None
    table: dict[str, dict] = {}
    for model_id in stem_models.MODELS:
        entry = raw.get(model_id)
        if not isinstance(entry, Mapping):
            return None
        rtf = _finite(entry.get("rtf"), 0.0, 1000.0)
        if rtf is None:
            return None
        measured = entry.get("measured")
        if measured is True:
            table[model_id] = {
                "rtf": rtf,
                "peak_mb": _int_or_none(entry.get("peak_mb")),
                "measured": True,
            }
        elif measured is False:
            table[model_id] = {"rtf": rtf, "measured": False, "estimated_from": MEASURED_MODEL}
        else:
            return None
    return table


def _clean_recommendation(raw: object, devices: Mapping[str, object]) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    device = _one_of(raw.get("device"), DEVICE_ORDER)
    stems = raw.get("stems")
    if device is None or device not in devices or not isinstance(stems, Mapping):
        return None
    clean_stems: dict[str, str] = {}
    for stem in stem_models.SELECTABLE_STEMS:
        model_id = _one_of(stems.get(stem), stem_models.CHOICES[stem])
        if model_id is None:
            return None
        clean_stems[stem] = model_id
    estimate = _finite(raw.get("estimate_minutes_4min"), 0.0, 1e5)
    reason = _one_of(raw.get("reason"), REASONS)
    reasons = raw.get("reasons", [reason])
    if estimate is None or reason is None:
        return None
    if not isinstance(reasons, list) or not all(_one_of(r, REASONS) for r in reasons):
        return None
    return {
        "device": device,
        "stems": clean_stems,
        "estimate_minutes_4min": round(estimate, 2),
        "budget_minutes": BUDGET_MINUTES,
        "reason": reason,
        "reasons": list(dict.fromkeys(reasons)),
    }


def sanitize(raw: object) -> dict | None:
    """``raw`` (a saved result) rebuilt from known keys and checked values, or
    None when it is not one. settings.json can be edited by hand, and a result
    that does not check must read as "never run", not crash Settings or steer
    a job. Never raises."""
    try:
        return _sanitize(raw)
    except Exception:  # noqa: BLE001 -- a value no check above foresaw
        logger.warning("the saved speed test does not check; reading it as never run")
        return None


def _sanitize(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    version = raw.get("version")
    if type(version) is not int or version != VERSION:
        return None
    ts = _finite(raw.get("ts"), 0.0, 1e11)
    hw = _clean_hw(raw.get("hw"))
    disk = _clean_disk(raw.get("disk"))
    raw_devices = raw.get("devices")
    if ts is None or hw is None or disk is None or not isinstance(raw_devices, Mapping):
        return None
    devices: dict[str, dict] = {}
    for device in DEVICE_ORDER:
        if device in raw_devices:
            table = _clean_table(raw_devices[device])
            if table is None:
                return None
            devices[device] = table
    if not devices:
        return None
    recommendation = _clean_recommendation(raw.get("recommendation"), devices)
    if recommendation is None:
        return None
    raw_failed = raw.get("failed", {})
    failed: dict[str, str] = {}
    if isinstance(raw_failed, Mapping):
        for device in DEVICE_ORDER:
            code = _one_of(raw_failed.get(device), DEVICE_ERRORS)
            if code is not None and device not in devices:
                failed[device] = code
    return {
        "version": VERSION,
        "ts": int(ts),
        "hw": hw,
        "disk": disk,
        "devices": devices,
        "failed": failed,
        "recommendation": recommendation,
    }


def public(raw: object, current: Mapping[str, object] | None = None) -> dict | None:
    """A saved result as the API serves it: checked, with "stale" set against
    this machine's fingerprint (``current``, else read now)."""
    result = sanitize(raw)
    if result is None:
        return None
    current = current if current is not None else current_hw()
    return {**result, "stale": is_stale(result["hw"], current)}


def build_result(
    *,
    hw: Mapping[str, object],
    disk: Mapping[str, object],
    measured: Mapping[str, Mapping[str, Mapping[str, object]]],
    failed: Mapping[str, str],
    kim_devices: frozenset[str] | set[str],
    ts: float | None = None,
) -> dict:
    """The result to save, from what the run measured. ``measured`` maps each
    device that succeeded to its {model id: {"rtf", "peak_mb"}}."""
    devices = {d: device_table(d, measured[d]) for d in DEVICE_ORDER if d in measured}
    result = {
        "version": VERSION,
        "ts": int(ts if ts is not None else time.time()),
        "hw": dict(hw),
        "disk": dict(disk),
        "devices": devices,
        "failed": {d: failed[d] for d in DEVICE_ORDER if d in failed},
        "recommendation": recommend(devices, disk, kim_devices, failed),
    }
    clean = sanitize(result)
    if clean is None:
        raise ValueError("the measured result does not check")
    return clean


# ── the disk test ──


def _new_temp_name() -> str:
    return f"{DISK_TEMP_PREFIX}{secrets.token_hex(8)}{DISK_TEMP_SUFFIX}"


def _free_bytes(folder: Path) -> int:
    return shutil.disk_usage(folder).free


def _write_chunk(fd: int, data: memoryview) -> int:
    """One os.write; a seam, so tests can slow it down or make it hang."""
    return os.write(fd, data)


def measure_disk(
    folder: Path,
    cancel: threading.Event | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    """Write DISK_TEST_BYTES to a new file in ``folder``, synced, and time it.

    The file has a name nothing else uses and is created exclusively, so an
    existing file is never opened, let alone overwritten; it is removed in
    ``finally``, whatever happens. Skipped, with the reason, when the folder
    has under DISK_MIN_FREE_BYTES free. Only the write is reported: a read
    straight after it comes from the page cache and measures memory.
    ``cancel`` stops it between chunks. Blocking: see measure_disk_bounded."""
    try:
        if not folder.is_dir():
            return _disk_skipped("unavailable")
        if _free_bytes(folder) < DISK_MIN_FREE_BYTES:
            return _disk_skipped("low-space")
    except OSError as exc:
        logger.warning("speed test: could not check the jobs folder: %s", exc)
        return _disk_skipped("unavailable")

    path: Path | None = None
    fd: int | None = None
    for _ in range(_DISK_NAME_TRIES):
        candidate = folder / _new_temp_name()
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            logger.warning("speed test: could not create the disk test file: %s", exc)
            return _disk_skipped("unavailable")
        path = candidate
        break
    if path is None or fd is None:
        return _disk_skipped("name-taken")

    chunk = os.urandom(min(DISK_CHUNK_BYTES, DISK_TEST_BYTES))
    try:
        start = clock()
        written = 0
        while written < DISK_TEST_BYTES:
            if cancel is not None and cancel.is_set():
                return _disk_skipped("failed")
            view = memoryview(chunk)[: min(len(chunk), DISK_TEST_BYTES - written)]
            while view:
                n = _write_chunk(fd, view)
                view = view[n:]
                written += n
        os.fsync(fd)
        seconds = clock() - start
        os.close(fd)
        fd = None
    except OSError as exc:
        logger.warning("speed test: the disk test write failed: %s", exc)
        return _disk_skipped("failed")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.error("speed test: could not remove %s: %s", path, exc)
    speed = (DISK_TEST_BYTES / _MIB) / max(seconds, 1e-6)
    return {"write_mb_s": round(speed, 1), "skipped": None, "slow": speed < DISK_SLOW_MB_S}


def measure_disk_bounded(
    folder: Path,
    cancel: threading.Event,
    timeout: float | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    """measure_disk in its own thread, given up on after ``timeout`` seconds
    (DISK_TIMEOUT_SEC) and reported as skipped "timeout".

    A write to a dead network share can block for minutes, and the caller
    holds the pipeline lock: every import would wait with it. A thread given
    up on is told to stop at its next chunk and removes its file in its own
    ``finally`` once its write returns; the startup sweep removes the file if
    it never does. On ``cancel`` it is waited for a few seconds, so its file
    is gone before the run says "cancelled"."""
    limit = DISK_TIMEOUT_SEC if timeout is None else timeout
    stop = threading.Event()
    box: dict[str, dict] = {}

    def work() -> None:
        try:
            box["result"] = measure_disk(folder, stop, clock)
        except Exception:
            logger.exception("speed test: the disk test failed")
            box["result"] = _disk_skipped("failed")

    worker = threading.Thread(target=work, name="speed-test-disk", daemon=True)
    worker.start()
    deadline = time.monotonic() + limit
    while worker.is_alive():
        if cancel.is_set():
            stop.set()
            worker.join(timeout=KILL_WAIT_SEC)
            return _disk_skipped("failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stop.set()
            logger.warning("speed test: the disk test took over %s s; given up on", limit)
            return _disk_skipped("timeout")
        worker.join(timeout=min(0.1, remaining))
    return box.get("result", _disk_skipped("failed"))


def _disk_skipped(reason: str) -> dict:
    return {"write_mb_s": None, "skipped": reason, "slow": False}


def sweep_stale_temp_files(folder: Path, now: float | None = None) -> int:
    """Remove disk test files a killed process left in ``folder`` (the jobs
    folder). Only what this module itself makes: a regular file, not a link,
    directly in the folder, named exactly as _new_temp_name names it, no larger
    than the test writes, and untouched for DISK_TEMP_STALE_SEC. Returns how
    many were removed. Run at server start and before every test."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for entry in entries:
        if not _DISK_TEMP_RE.match(entry.name):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > DISK_TEST_BYTES:
                continue
            if now - info.st_mtime < DISK_TEMP_STALE_SEC:
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError as exc:
            logger.warning("speed test: could not remove a stale test file: %s", exc)
    if removed:
        logger.info("speed test: removed %d stale disk test file(s)", removed)
    return removed


# ── one device's subprocess ──


def parse_line(line: str) -> tuple[str, object] | None:
    """One line of the worker's stdout as ("step", device), ("pct", 0..100),
    ("partial", dict) or ("result", dict); None for anything else (a
    library's own output, a line over MAX_LINE_CHARS, a value that does not
    parse). Never raises."""
    if len(line) > MAX_LINE_CHARS:
        return None
    line = line.strip()
    if line.startswith(STEP_MARK):
        step = line[len(STEP_MARK) :]
        return ("step", step) if step in DEVICE_ORDER else None
    if line.startswith(PCT_MARK):
        raw = line[len(PCT_MARK) :]
        # ASCII digits only: "²".isdigit() is True and int("²") raises.
        if not (raw.isascii() and raw.isdecimal()) or len(raw) > 3:
            return None
        try:
            return ("pct", max(0, min(100, int(raw))))
        except ValueError:
            return None
    for mark, kind in ((PARTIAL_MARK, "partial"), (RESULT_MARK, "result")):
        if line.startswith(mark):
            try:
                value = json.loads(line[len(mark) :])
            except (ValueError, RecursionError):
                return None
            return (kind, value) if isinstance(value, dict) else None
    return None


def check_worker_result(device: str, raw: object) -> tuple[dict | None, str | None, str | None]:
    """The worker's result for ``device`` as (measured models, error code,
    torch version). Measured models map a model id to {"rtf", "peak_mb"};
    only registry Demucs models, with a finite positive speed, are kept, and
    the 6s must be among them or the device counts as failed."""
    if not isinstance(raw, Mapping) or raw.get("device") != device:
        return None, "failed", None
    torch = _short_str(raw.get("torch"), 64)
    error = raw.get("error")
    if error is not None:
        return None, error if error in DEVICE_ERRORS else "failed", torch
    models = raw.get("models")
    if not isinstance(models, Mapping):
        return None, "failed", torch
    measured: dict[str, dict] = {}
    for model_id, entry in models.items():
        if model_id not in stem_models.MODELS or not isinstance(entry, Mapping):
            continue
        rtf = _finite(entry.get("rtf"), 0.0, 1000.0)
        if rtf is None:
            continue
        measured[model_id] = {"rtf": rtf, "peak_mb": _int_or_none(entry.get("peak_mb"))}
    if MEASURED_MODEL not in measured:
        return None, "failed", torch
    return measured, None, torch


def worker_cmd(device: str) -> list[str]:
    """The worker invocation for one device. A seam, so tests run a stub."""
    return [sys.executable, "-m", "app.pipeline.speed_test_worker", device]


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    # The worker exits when this process is gone (app/core/process.py), so a
    # hard kill of the server cannot leave it holding the GPU.
    env["STEMDECK_PARENT_PID"] = str(os.getpid())
    env["PYTHONIOENCODING"] = "utf-8:replace"
    return env


def _taskkill() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.join(root, "System32", "taskkill.exe")


def signal_tree(proc: subprocess.Popen, *, hard: bool) -> None:
    """Stop ``proc`` and every process it started.

    POSIX: the worker runs in its own session (start_new_session), so its
    process group is the whole tree: SIGTERM, or SIGKILL when ``hard``.
    Windows has no gentle stop for a console child (terminate() is already
    TerminateProcess), so both are taskkill /T /F while the worker still
    lives, which is when its children can still be found, then kill() in
    case taskkill could not run. A child that keeps the stdout pipe open is
    what would leave the reader blocked after the worker itself is gone."""
    if os.name == "nt":
        if proc.poll() is None:
            try:
                subprocess.run(  # noqa: S603 -- fixed args, the system's own tool
                    [_taskkill(), "/T", "/F", "/PID", str(proc.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_REAP_WAIT_SEC,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("speed test: taskkill failed: %s", exc)
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError as exc:
        logger.warning("speed test: could not signal the worker group: %s", exc)


def stop_process(proc: subprocess.Popen) -> None:
    """Stop the tree, wait KILL_WAIT_SEC, kill it, reap; bounded."""
    if proc.poll() is None:
        signal_tree(proc, hard=False)
        try:
            proc.wait(timeout=KILL_WAIT_SEC)
        except subprocess.TimeoutExpired:
            pass
    # Its children too, even when the worker itself has exited.
    signal_tree(proc, hard=True)
    try:
        proc.wait(timeout=_REAP_WAIT_SEC)
    except subprocess.TimeoutExpired:
        logger.error("speed test worker %s did not exit after kill", proc.pid)


@dataclass
class DeviceRun:
    """What one device's subprocess answered: its result, or why not.
    ``partial`` is set when only the 6s measurement survived (the run ended
    while it measured another model); the rest is then estimated."""

    measured: dict | None
    error: str | None
    torch: str | None
    tail: list[str]
    partial: bool = False


def run_device(
    device: str,
    *,
    on_step: Callable[[str], None],
    on_pct: Callable[[int], None],
    cancel: threading.Event,
    register: Callable[[subprocess.Popen | None], None],
    timeout: float | None = None,
    stall: float | None = None,
    grace: float | None = None,
) -> DeviceRun:
    """Measure one device in its own subprocess, so a CUDA failure cannot take
    the CPU measurement with it.

    Ended on cancel, after ``timeout`` seconds in all and after ``stall``
    seconds of silence: first asked to stop, then, if its output has not
    ended ``grace`` seconds later, killed with every process it started, so
    the read below always returns and the pipeline lock is released. Always
    reaped. The worker's @@PARTIAL@@ line (the 6s measured) is kept when the
    run ends before its @@RESULT@@."""
    timeout = DEVICE_TIMEOUT_SEC if timeout is None else timeout
    stall = DEVICE_STALL_SEC if stall is None else stall
    grace = KILL_WAIT_SEC if grace is None else grace
    proc = subprocess.Popen(  # noqa: S603 -- fixed args: our interpreter and module
        worker_cmd(device),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_worker_env(),
        start_new_session=os.name != "nt",
    )
    register(proc)
    started = time.monotonic()
    last_line = [started]
    ended_by: list[str] = []
    done = threading.Event()

    def watchdog() -> None:
        while not done.wait(timeout=0.25):
            now = time.monotonic()
            if cancel.is_set():
                why = "cancelled"
            elif now - started > timeout:
                why = "timeout"
            elif now - last_line[0] > stall:
                why = "stalled"
            else:
                continue
            ended_by.append(why)
            if why != "cancelled":
                logger.warning("speed test on %s: %s, stopping the worker", device, why)
            signal_tree(proc, hard=False)
            if not done.wait(timeout=grace):
                logger.warning("speed test on %s: still running; killing it", device)
                signal_tree(proc, hard=True)
                if not done.wait(timeout=_REAP_WAIT_SEC):
                    logger.error("speed test on %s: the worker's output never ended", device)
            return

    guard = threading.Thread(target=watchdog, name=f"speed-test-{device}", daemon=True)
    guard.start()
    result: object = None
    partial: object = None
    tail: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            last_line[0] = time.monotonic()
            parsed = parse_line(line)
            if parsed is None:
                text = line.strip()
                if text:
                    tail.append(text[:500])
                    del tail[:-40]
                continue
            kind, value = parsed
            if kind == "step":
                on_step(str(value))
            elif kind == "pct":
                on_pct(int(value))  # type: ignore[call-overload]
            elif kind == "partial":
                partial = value
            else:
                result = value
    finally:
        done.set()
        stop_process(proc)
        guard.join(timeout=2)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass
        register(None)

    if ended_by and ended_by[0] == "cancelled":
        return DeviceRun(None, "failed", None, tail)
    timed_out = bool(ended_by) and ended_by[0] in ("timeout", "stalled")
    if result is None or timed_out:
        if partial is not None:
            measured, _error, torch = check_worker_result(device, partial)
            if measured is not None:
                logger.warning(
                    "speed test on %s: ended before its result (%s); keeping the 6s timing",
                    device,
                    ended_by[0] if ended_by else "no result",
                )
                return DeviceRun(measured, None, torch, tail, partial=True)
        return DeviceRun(None, ended_by[0] if timed_out else "failed", None, tail)
    measured, error, torch = check_worker_result(device, result)
    return DeviceRun(measured, error, torch, tail)
