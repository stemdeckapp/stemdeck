"""Why separation runs on the CPU on a machine with an NVIDIA GPU (#247).

Settings used to say only "currently: cpu". A missing GPU, a CPU-only torch, a
CUDA torch that cannot start and a failed CUDA install all read the same, and
the reason lived only in the desktop shell's setup log. These pin the one code
the backend now derives from the facts it can see, and the Settings payload
that carries it.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.core import device_reason

RTX = {"name": "NVIDIA GeForce RTX 3080", "driver": "616.64", "cuda": "13.4"}

# The header of driver 616.64, verbatim: it labels the CUDA version
# "CUDA UMD Version", not "CUDA Version" as older drivers do.
HEADER_616 = (
    "Wed Nov  4 05:13:22 2026\n"
    "+-----------------------------------------------------------------------------------------+\n"
    "| NVIDIA-SMI 616.64                 KMD Version: 616.64        CUDA UMD Version: 13.4     |\n"
)
HEADER_OLD = "| NVIDIA-SMI 560.94       Driver Version: 560.94       CUDA Version: 12.6     |\n"


@pytest.fixture
def facts(monkeypatch, tmp_path):
    """Set what the machine looks like: devices torch can use, the torch build,
    the NVIDIA GPU nvidia-smi reports, and the shell's persisted setup reason."""
    monkeypatch.setattr(device_reason, "_config_path", tmp_path / "config.json")
    monkeypatch.setattr(device_reason, "torch_load_error", lambda: None)

    def set_facts(devices, torch_build, gpu, setup_reason=None):
        monkeypatch.setattr(device_reason, "available_torch_devices", lambda: devices)
        monkeypatch.setattr(device_reason, "_torch_build", lambda: torch_build)
        monkeypatch.setattr(device_reason, "nvidia_gpu", lambda: gpu)
        if setup_reason is not None:
            (tmp_path / "config.json").write_text(
                json.dumps({"torchDevice": "cpu", "torchDeviceReason": setup_reason}),
                encoding="utf-8",
            )

    return set_facts


def test_no_reason_while_cuda_is_in_use(facts):
    facts(["cuda", "cpu"], ("2.6.0+cu124", "12.4"), RTX, setup_reason="cuda-verify-failed")
    assert device_reason.cpu_reason() is None


def test_no_reason_on_a_machine_without_an_nvidia_gpu(facts):
    # CPU is the expected device there: nothing to explain, even when the
    # shell's setup recorded that it found no GPU.
    facts(["cpu"], ("2.6.0+cpu", None), None, setup_reason="no-gpu-detected")
    assert device_reason.cpu_reason() is None


def test_cpu_torch_build_with_an_nvidia_gpu(facts):
    facts(["cpu"], ("2.6.0+cpu", None), RTX)
    assert device_reason.cpu_reason() == "cpu-torch-build"


def test_cuda_torch_that_cannot_start(facts):
    # For example CUDA_VISIBLE_DEVICES=-1, or a driver that refuses the GPU.
    facts(["cpu"], ("2.6.0+cu124", "12.4"), RTX)
    assert device_reason.cpu_reason() == "cuda-unavailable"


def test_driver_older_than_the_torch_cuda_major(facts):
    facts(["cpu"], ("2.6.0+cu124", "12.4"), {**RTX, "driver": "472.12", "cuda": "11.4"})
    assert device_reason.cpu_reason() == "driver-too-old"


def test_same_cuda_major_is_not_called_too_old(facts):
    # CUDA 12 is minor-version compatible: a 12.2 driver runs a cu124 build,
    # so an older minor alone is no evidence and the plain code is shown.
    facts(["cpu"], ("2.6.0+cu124", "12.4"), {**RTX, "cuda": "12.2"})
    assert device_reason.cpu_reason() == "cuda-unavailable"


@pytest.mark.parametrize(
    ("setup_reason", "code"),
    [
        ("cuda-install-failed", "cuda-install-failed"),
        ("cuda-verify-failed", "cuda-verify-failed"),
        ("cuda-unsupported-gpu", "cuda-unsupported-gpu"),
        ("cuda-verify-failed-cpu-restore-failed", "cuda-verify-failed"),
        ("cuda-install-failed-cpu-restore-failed", "cuda-install-failed"),
        ("no-gpu-detected", "no-gpu-detected"),
        ("cpu-only-package", "cpu-only-package"),
    ],
)
def test_the_shell_setup_outcome_wins_over_the_torch_build(facts, setup_reason, code):
    # Setup knows the install outcome the backend cannot see: after a failed
    # CUDA swap the CPU torch is back, which alone would read cpu-torch-build.
    facts(["cpu"], ("2.6.0+cpu", None), RTX, setup_reason=setup_reason)
    assert device_reason.cpu_reason() == code


def test_a_stale_setup_failure_is_ignored_once_torch_has_cuda(facts):
    # The setup reason describes the CPU torch it left behind. With a CUDA
    # build installed since, the facts of that build decide.
    facts(["cpu"], ("2.6.0+cu124", "12.4"), RTX, setup_reason="cuda-install-failed")
    assert device_reason.cpu_reason() == "cuda-unavailable"


def test_a_verified_setup_does_not_hide_a_cuda_that_stopped_working(facts):
    facts(["cpu"], ("2.6.0+cu124", "12.4"), RTX, setup_reason="verified")
    assert device_reason.cpu_reason() == "cuda-unavailable"


def test_mps_unavailable_on_a_mac(facts):
    facts(["cpu"], ("2.6.0", None), None, setup_reason="mps-unavailable")
    assert device_reason.cpu_reason() == "mps-unavailable"


def test_no_reason_when_torch_is_absent(facts):
    facts(["cpu"], None, RTX)
    assert device_reason.cpu_reason() is None


def test_no_reason_when_torch_cannot_load(facts, monkeypatch):
    # Separation cannot run on any device then, and Settings already says so
    # in place of the device line (#730). A second sentence would contradict it.
    facts(["cpu"], ("2.6.0+cu124", "12.4"), RTX)
    monkeypatch.setattr(device_reason, "torch_load_error", lambda: "OSError: WinError 127")
    assert device_reason.cpu_reason() is None


def test_an_unreadable_config_is_no_setup_reason(facts, tmp_path):
    facts(["cpu"], ("2.6.0+cpu", None), RTX)
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    assert device_reason.cpu_reason() == "cpu-torch-build"


def test_diagnostics_carry_the_facts(facts):
    facts(["cpu"], ("2.6.0+cpu", None), RTX, setup_reason="no-gpu-detected")
    assert device_reason.device_diagnostics() == (
        "gpu=NVIDIA GeForce RTX 3080, driver=616.64, driver_cuda=13.4, "
        "torch=2.6.0+cpu, torch_cuda=none, cuda_available=no, "
        "setup=no-gpu-detected, why=no-gpu-detected"
    )


def test_diagnostics_without_a_gpu_or_torch(facts):
    facts(["cpu"], None, None)
    assert device_reason.device_diagnostics() == (
        "gpu=none, driver=none, driver_cuda=none, torch=none, torch_cuda=none, "
        "cuda_available=no, setup=none, why=none"
    )


# The nvidia-smi probe itself.


def _fake_smi(monkeypatch, header, query="NVIDIA GeForce RTX 3080, 616.64\n", code=0):
    calls = []

    def run(args, **_kw):
        calls.append(args)
        out = query if any(a.startswith("--query-gpu") for a in args) else header
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")

    monkeypatch.setattr(device_reason, "_nvidia_smi_path", lambda: "nvidia-smi")
    monkeypatch.setattr(device_reason.subprocess, "run", run)
    device_reason.reset_state()
    return calls


def test_the_616_header_is_read(monkeypatch):
    _fake_smi(monkeypatch, HEADER_616)
    assert device_reason.nvidia_gpu() == RTX


def test_the_older_header_is_read(monkeypatch):
    _fake_smi(monkeypatch, HEADER_OLD, query="NVIDIA GeForce RTX 4060, 560.94\n")
    assert device_reason.nvidia_gpu() == {
        "name": "NVIDIA GeForce RTX 4060",
        "driver": "560.94",
        "cuda": "12.6",
    }


def test_nvidia_smi_is_asked_once_within_the_ttl(monkeypatch):
    calls = _fake_smi(monkeypatch, HEADER_616)
    for _ in range(5):
        device_reason.nvidia_gpu()
    assert len(calls) == 2  # the query and the header, once


def test_nvidia_smi_is_asked_again_after_the_ttl(monkeypatch):
    calls = _fake_smi(monkeypatch, HEADER_616)
    clock = [1000.0]
    monkeypatch.setattr(device_reason.time, "monotonic", lambda: clock[0])
    device_reason.nvidia_gpu()
    clock[0] += device_reason.NVIDIA_SMI_TTL_SEC + 1
    device_reason.nvidia_gpu()
    assert len(calls) == 4


def test_a_failing_nvidia_smi_is_no_gpu(monkeypatch):
    _fake_smi(monkeypatch, "", query="", code=9)
    assert device_reason.nvidia_gpu() is None


def test_a_hung_nvidia_smi_is_no_gpu(monkeypatch):
    def run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout"))

    monkeypatch.setattr(device_reason, "_nvidia_smi_path", lambda: "nvidia-smi")
    monkeypatch.setattr(device_reason.subprocess, "run", run)
    device_reason.reset_state()
    assert device_reason.nvidia_gpu() is None


def test_no_nvidia_smi_is_no_gpu(monkeypatch):
    monkeypatch.setattr(device_reason, "_nvidia_smi_path", lambda: None)
    device_reason.reset_state()
    assert device_reason.nvidia_gpu() is None


# What Settings receives.


def test_settings_carry_the_reason_and_diagnostics(facts):
    from app.main import app

    facts(["cpu"], ("2.6.0+cpu", None), RTX)
    with TestClient(app) as c:
        body = c.get("/api/settings").json()
    assert body["demucs_device_why"] == "cpu-torch-build"
    assert body["device_diagnostics"].startswith("gpu=NVIDIA GeForce RTX 3080, driver=616.64")


def test_settings_carry_no_reason_while_cuda_is_in_use(facts):
    from app.main import app

    facts(["cuda", "cpu"], ("2.6.0+cu124", "12.4"), RTX)
    with TestClient(app) as c:
        body = c.get("/api/settings").json()
    assert body["demucs_device_why"] is None
