"""Why separation runs on the CPU on a machine that has an NVIDIA GPU (#247).

Settings used to show only the resolved device, so a CPU-only torch, a CUDA
torch that cannot start and a CUDA install that failed in setup all read as
"currently: cpu". This derives one code from the facts the backend can see
(nvidia-smi, the torch build, whether CUDA starts) and the outcome the desktop
shell's setup persisted in config.json, which knows what it installed.

A code is given only when the facts agree on it. A machine with no NVIDIA GPU
gets none: the CPU is the expected device there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.config import (
    DATA_DIR,
    NVIDIA_SMI_TTL_SEC,
    TIMEOUT_NVIDIA_SMI,
    available_torch_devices,
    torch_load_error,
)

logger = logging.getLogger("stemdeck.device_reason")

# Written by the desktop shell's GPU setup step (persist_torch_device in
# desktop/src-tauri/src/main.rs). Absent on a source, Docker or Unraid run.
_config_path = DATA_DIR / "config.json"

# Setup outcomes that explain a CPU torch on a machine with an NVIDIA GPU. A
# failed restore of the CPU torch adds "-cpu-restore-failed"; the cause is the
# part before it, and a torch that then cannot load has its own message.
_SETUP_REASONS = frozenset(
    {
        "cuda-install-failed",
        "cuda-verify-failed",
        "cuda-unsupported-gpu",
        "no-gpu-detected",
        "cpu-only-package",
    }
)
# Every value the shell writes. Anything else in config.json is reported as
# "other", so the diagnostics line only ever carries a known word.
_SETUP_VALUES = _SETUP_REASONS | {"verified", "mps", "mps-unavailable"}

# Drivers before 616 print "CUDA Version: 12.6"; 616.64 prints
# "CUDA UMD Version: 13.4" in the same header. "N/A" is no version. The twin
# of parse_cuda_version in desktop/src-tauri/src/main.rs: keep the two one
# rule, and both tests on tests/fixtures/nvidia_smi_headers.json (#1012).
_CUDA_HEADER = re.compile(r"CUDA (?:UMD )?Version:\s*([0-9]+\.[0-9]+)")

_lock = threading.Lock()
# Held while nvidia-smi runs, so requests that arrive meanwhile wait for its
# answer instead of each starting their own.
_probe_lock = threading.Lock()
_gpu: dict[str, str | None] | None = None
_gpu_at: float | None = None


def reset_state() -> None:
    """Forget the cached nvidia-smi answer (tests)."""
    global _gpu, _gpu_at
    with _lock:
        _gpu, _gpu_at = None, None


def _nvidia_smi_path() -> str | None:
    found = shutil.which("nvidia-smi")
    if found or sys.platform != "win32":
        return found
    # The desktop shell starts this backend with a PATH that may not hold
    # System32, and DCH drivers can ship nvidia-smi only in the DriverStore.
    # The same places nvidia_smi_exe in the shell looks (#247).
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    if (system32 / "nvidia-smi.exe").is_file():
        return str(system32 / "nvidia-smi.exe")
    try:
        hits = list((system32 / "DriverStore" / "FileRepository").glob("nv*/nvidia-smi.exe"))
        return str(max(hits, key=lambda p: p.stat().st_mtime)) if hits else None
    except OSError as exc:
        logger.warning("could not search the DriverStore for nvidia-smi: %s", exc)
        return None


def _run_smi(smi: str, *args: str) -> str | None:
    try:
        out = subprocess.run(  # noqa: S603 -- fixed args, the driver's own tool
            [smi, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_NVIDIA_SMI,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("nvidia-smi %s failed: %s", " ".join(args), exc)
        return None
    return out.stdout if out.returncode == 0 else None


def _query_nvidia_gpu() -> dict[str, str | None] | None:
    smi = _nvidia_smi_path()
    if smi is None:
        return None
    listing = _run_smi(smi, "--query-gpu=name,driver_version", "--format=csv,noheader")
    if not listing or not listing.strip():
        return None
    # One line per GPU; the first is the one torch would use as cuda:0.
    name, _, driver = listing.strip().splitlines()[0].partition(",")
    header = _run_smi(smi) or ""
    m = _CUDA_HEADER.search(header)
    return {
        "name": name.strip(),
        "driver": driver.strip() or None,
        "cuda": m.group(1) if m else None,
    }


def _fresh() -> bool:
    return _gpu_at is not None and time.monotonic() - _gpu_at < NVIDIA_SMI_TTL_SEC


def refresh_nvidia_gpu() -> dict[str, str | None] | None:
    """Ask nvidia-smi now unless an answer is fresh, and keep it. Started at
    server start (app/main.py lifespan), so Settings never waits for the first
    answer; a GPU waking up can take seconds."""
    global _gpu, _gpu_at
    with _probe_lock:
        with _lock:
            if _fresh():
                return _gpu
        gpu = _query_nvidia_gpu()
        with _lock:
            _gpu, _gpu_at = gpu, time.monotonic()
        return gpu


def nvidia_gpu() -> dict[str, str | None] | None:
    """{"name", "driver", "cuda" (the newest CUDA the driver runs)} of the first
    NVIDIA GPU, or None when there is none or nvidia-smi cannot say.

    Waits only for the very first answer. An expired one is still returned,
    and a new one is asked for in the background."""
    with _lock:
        known, fresh, gpu = _gpu_at is not None, _fresh(), _gpu
    if not known:
        return refresh_nvidia_gpu()
    if not fresh and not _probe_lock.locked():
        threading.Thread(target=refresh_nvidia_gpu, name="nvidia-smi", daemon=True).start()
    return gpu


def _torch_build() -> tuple[str, str | None, str | None] | None:
    """(torch version, CUDA version it was built for, ROCm (HIP) version it
    was built for), or None when torch cannot be imported. A CPU build has
    neither."""
    try:
        import torch
    except Exception:  # noqa: BLE001 -- the device probe reports why (#730)
        return None
    return str(torch.__version__), torch.version.cuda, torch.version.hip


def _setup_reason() -> str | None:
    try:
        reason = json.loads(_config_path.read_text(encoding="utf-8")).get("torchDeviceReason")
    except (OSError, ValueError, AttributeError):
        return None
    return reason if isinstance(reason, str) else None


def _major(version: str | None) -> int | None:
    head = (version or "").split(".")[0]
    return int(head) if head.isdigit() else None


def cpu_reason() -> str | None:
    """Why the GPU is not used, as one code, or None when there is nothing to
    explain (the GPU is in use, or the machine has none)."""
    devices = available_torch_devices()
    if torch_load_error() is not None:
        return None
    setup = _setup_reason()
    if setup == "mps-unavailable" and "mps" not in devices:
        return "mps-unavailable"
    if "cuda" in devices:
        return None
    build = _torch_build()
    gpu = nvidia_gpu()
    if build is None or gpu is None:
        return None
    _version, torch_cuda, torch_hip = build
    if torch_hip is not None:
        # The AMD build (#825): none of the NVIDIA sentences describes it.
        return None
    if torch_cuda is None:
        # Setup's outcome describes the CPU torch it left in place, so it is
        # trusted only while that is still the torch installed. With a GPU
        # visible here, "no-gpu-detected" is setup's own miss (its probe found
        # no nvidia-smi), which is why it installed no GPU support (#247).
        cause = (setup or "").removesuffix("-cpu-restore-failed")
        return cause if cause in _SETUP_REASONS else "cpu-torch-build"
    driver_major, torch_major = _major(gpu["cuda"]), _major(torch_cuda)
    if driver_major is not None and torch_major is not None and driver_major < torch_major:
        return "driver-too-old"
    return "cuda-unavailable"


def device_diagnostics(why: str | None) -> str:
    """The facts behind `why` (what cpu_reason() returned) on one line, for a
    bug report."""
    gpu = nvidia_gpu() or {}
    version, torch_cuda, _hip = _torch_build() or (None, None, None)
    setup = _setup_reason()
    if setup is not None and setup.removesuffix("-cpu-restore-failed") not in _SETUP_VALUES:
        setup = "other"
    facts = {
        "gpu": gpu.get("name"),
        "driver": gpu.get("driver"),
        "driver_cuda": gpu.get("cuda"),
        "torch": version,
        "torch_cuda": torch_cuda,
        "cuda_available": "yes" if "cuda" in available_torch_devices() else "no",
        "setup": setup,
        "why": why,
    }
    return ", ".join(f"{key}={value or 'none'}" for key, value in facts.items())
