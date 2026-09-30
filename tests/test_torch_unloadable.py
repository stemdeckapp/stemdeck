"""A torch that is installed but cannot load must not take the server down (#730).

On Windows, a CUDA DLL left behind by a half-reverted CUDA install makes torch's
own DLL loader raise OSError (WinError 127) from `import torch` (#723). The
device probe caught only ImportError, so with the device on "auto" the backend
died at startup, and with it forced to CPU every /api/settings call was a 500.

These install an import hook that makes `import torch` raise exactly that, so
they run whether or not a real torch is present.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys

import pytest
from fastapi.testclient import TestClient

from app.core import config

REASON = "[WinError 127] The specified procedure could not be found. Error loading c10_cuda.dll"


class _BrokenTorchLoader(importlib.abc.Loader):
    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise OSError(REASON)


class _BrokenTorchFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "torch":
            return importlib.machinery.ModuleSpec(name, _BrokenTorchLoader())
        return None


@pytest.fixture
def broken_torch(monkeypatch):
    for name in [m for m in sys.modules if m == "torch" or m.startswith("torch.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_BrokenTorchFinder(), *sys.meta_path])
    monkeypatch.setattr(config, "_torch_load_error", None)
    yield


def test_the_probe_falls_back_to_cpu(broken_torch):
    assert config.available_torch_devices() == ["cpu"]
    assert config.detect_torch_device() == "cpu"


def test_the_probe_says_why(broken_torch):
    config.available_torch_devices()
    assert config.torch_load_error() == f"OSError: {REASON}"


def test_the_reason_is_logged_once_per_cause(broken_torch, caplog):
    with caplog.at_level("ERROR", logger="stemdeck.config"):
        config.available_torch_devices()
        config.available_torch_devices()
    assert len([r for r in caplog.records if "could not be loaded" in r.getMessage()]) == 1


def test_settings_still_answer_and_carry_the_error(broken_torch):
    from app.main import app

    with TestClient(app) as c:
        resp = c.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["demucs_devices_available"] == ["cpu"]
    assert body["torch_error"] == f"OSError: {REASON}"


def test_a_working_torch_reports_no_error(monkeypatch):
    # The healthy path: whatever this machine has, a torch that imports (or is
    # simply absent) leaves no error behind.
    monkeypatch.setattr(config, "_torch_load_error", "OSError: stale")
    try:
        import torch  # noqa: F401
    except ImportError:
        monkeypatch.setattr(config, "_torch_load_error", None)
    config.available_torch_devices()
    assert config.torch_load_error() is None
