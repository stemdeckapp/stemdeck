"""Environment resolution, the parent watchdog loop, and the SSE tails.

The last cluster of reachable gaps. Four unrelated areas, grouped because each
is a handful of lines rather than a file's worth.

The config helpers decide where the library lives and which compute devices the
Settings panel offers, and they all fail towards a safe default -- which is
exactly the kind of code that is never exercised until the day it matters.
_stored_jobs_dir in particular refuses a folder that is not currently mounted,
because mkdir-ing a mount point on macOS can stop the real drive mounting under
its own name later.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# environment parsing
# --------------------------------------------------------------------------


def test_an_integer_setting_falls_back_when_the_environment_holds_junk(monkeypatch):
    """These are read at import time. Raising here would stop the app starting
    over a typo in a compose file."""
    from app.core import config

    monkeypatch.setenv("STEMDECK_TEST_INT", "not-a-number")
    assert config._env_int("STEMDECK_TEST_INT", 42) == 42

    monkeypatch.setenv("STEMDECK_TEST_INT", "7")
    assert config._env_int("STEMDECK_TEST_INT", 42) == 7

    monkeypatch.setenv("STEMDECK_TEST_INT", "   ")
    assert config._env_int("STEMDECK_TEST_INT", 42) == 42

    monkeypatch.delenv("STEMDECK_TEST_INT", raising=False)
    assert config._env_int("STEMDECK_TEST_INT", 42) == 42


def test_a_path_setting_is_expanded_and_resolved(monkeypatch, tmp_path):
    from app.core import config

    monkeypatch.setenv("STEMDECK_TEST_PATH", str(tmp_path))
    assert config._env_path("STEMDECK_TEST_PATH", Path("/default")) == tmp_path.resolve()

    monkeypatch.setenv("STEMDECK_TEST_PATH", "")
    assert config._env_path("STEMDECK_TEST_PATH", Path("/default")) == Path("/default")


def test_an_optional_path_that_is_unset_means_the_feature_is_off(monkeypatch):
    from app.core import config

    monkeypatch.delenv("STEMDECK_TEST_OPT", raising=False)
    assert config._env_path_opt("STEMDECK_TEST_OPT") is None


# --------------------------------------------------------------------------
# available_torch_devices
# --------------------------------------------------------------------------


def test_the_cpu_is_always_offered():
    """Settings greys out what the machine cannot do, so this list must never
    come back empty -- there would be nothing left to select."""
    from app.core.config import available_torch_devices

    assert "cpu" in available_torch_devices()


def test_a_cuda_machine_offers_cuda_first(monkeypatch):
    """Best-first: the panel's default is whatever leads this list."""
    import types

    from app.core.config import available_torch_devices

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: True)
    torch.backends = types.SimpleNamespace(mps=None)
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert available_torch_devices() == ["cuda", "cpu"]


def test_an_apple_silicon_machine_offers_mps(monkeypatch):
    import types

    from app.core.config import available_torch_devices

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert available_torch_devices() == ["mps", "cpu"]


def test_a_torch_build_with_no_mps_attribute_does_not_crash(monkeypatch):
    """Older torch builds have no torch.backends.mps at all."""
    import types

    from app.core.config import available_torch_devices

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert available_torch_devices() == ["cpu"]


def test_a_machine_with_no_torch_still_offers_the_cpu(monkeypatch):
    """Docker's CPU-only image and a source checkout mid-install both hit this."""
    from app.core.config import available_torch_devices

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _no_torch(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_torch)

    assert available_torch_devices() == ["cpu"]


# --------------------------------------------------------------------------
# _stored_jobs_dir
# --------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _write_settings(data_dir: Path, payload):
    (data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_stored_library_folder_is_honoured(data_dir, tmp_path):
    from app.core.config import _stored_jobs_dir

    library = tmp_path / "library"
    library.mkdir()
    _write_settings(data_dir, {"jobs_dir": str(library)})

    assert _stored_jobs_dir() == library


def test_a_folder_that_is_not_mounted_is_not_honoured(data_dir, tmp_path):
    """It existed when the user picked it, so a missing one means the disk is
    not plugged in. ensure_runtime_dirs would otherwise mkdir the mount point,
    which on macOS can stop the real drive mounting under its own name."""
    from app.core.config import _stored_jobs_dir

    _write_settings(data_dir, {"jobs_dir": str(tmp_path / "on-an-unplugged-disk")})

    assert _stored_jobs_dir() is None


def test_a_stored_path_pointing_at_a_file_is_not_honoured(data_dir, tmp_path):
    from app.core.config import _stored_jobs_dir

    target = tmp_path / "not-a-folder"
    target.write_text("x")
    _write_settings(data_dir, {"jobs_dir": str(target)})

    assert _stored_jobs_dir() is None


@pytest.mark.parametrize(
    "payload, why",
    [
        ({}, "no key at all"),
        ({"jobs_dir": ""}, "an empty string"),
        ({"jobs_dir": "   "}, "whitespace"),
        ({"jobs_dir": 42}, "not a string"),
        ({"jobs_dir": None}, "null"),
        ([1, 2, 3], "settings that are not an object"),
    ],
)
def test_an_unusable_stored_folder_falls_back_to_the_default(data_dir, payload, why):
    """A library that quietly moves is far worse than one that ignores a corrupt
    preference."""
    from app.core.config import _stored_jobs_dir

    _write_settings(data_dir, payload)

    assert _stored_jobs_dir() is None, why


def test_a_settings_file_that_is_absent_or_corrupt_falls_back(data_dir):
    from app.core.config import _stored_jobs_dir

    assert _stored_jobs_dir() is None  # nothing written yet

    (data_dir / "settings.json").write_text("{truncated", encoding="utf-8")
    assert _stored_jobs_dir() is None


# --------------------------------------------------------------------------
# portable environment
# --------------------------------------------------------------------------


def test_a_bundled_ffmpeg_is_put_on_the_path(monkeypatch, tmp_path):
    from app.core import config

    ffmpeg_dir = tmp_path / "ffmpeg"
    ffmpeg_dir.mkdir()
    monkeypatch.setattr(config, "FFMPEG_DIR", ffmpeg_dir)
    monkeypatch.setenv("PATH", "/usr/bin")

    config.configure_portable_environment()

    assert str(ffmpeg_dir) in os.environ["PATH"].split(os.pathsep)


def test_the_bundled_ffmpeg_is_not_added_twice(monkeypatch, tmp_path):
    """The lifespan can run this again on a reload; a PATH that grows every
    time is how an environment ends up megabytes long."""
    from app.core import config

    ffmpeg_dir = tmp_path / "ffmpeg"
    ffmpeg_dir.mkdir()
    monkeypatch.setattr(config, "FFMPEG_DIR", ffmpeg_dir)
    monkeypatch.setenv("PATH", "/usr/bin")

    config.configure_portable_environment()
    config.configure_portable_environment()

    assert os.environ["PATH"].split(os.pathsep).count(str(ffmpeg_dir)) == 1


def test_portable_mode_keeps_model_caches_inside_the_data_folder(monkeypatch, tmp_path):
    """Otherwise a "portable" install scatters gigabytes of checkpoints through
    the user's home directory."""
    from app.core import config

    monkeypatch.setattr(config, "FFMPEG_DIR", tmp_path / "absent")
    monkeypatch.setattr(config, "PORTABLE_DATA_DIR_ENABLED", True)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("TORCH_HOME", raising=False)

    config.configure_portable_environment()

    assert os.environ["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert os.environ["TORCH_HOME"] == str(tmp_path / "models" / "torch")


def test_an_explicit_cache_location_is_not_overridden(monkeypatch, tmp_path):
    """It only sets what is still unset, so a caller's own choice wins."""
    from app.core import config

    monkeypatch.setattr(config, "FFMPEG_DIR", tmp_path / "absent")
    monkeypatch.setattr(config, "PORTABLE_DATA_DIR_ENABLED", True)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("XDG_CACHE_HOME", "/somewhere/chosen")

    config.configure_portable_environment()

    assert os.environ["XDG_CACHE_HOME"] == "/somewhere/chosen"


def test_a_js_runtime_on_the_path_is_reported(monkeypatch):
    """Used only to explain a YouTube challenge failure, never to gate one."""
    import shutil

    from app.core import config

    monkeypatch.setattr(config, "bundled_js_runtime", lambda: None)
    monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/" + exe)

    assert config.js_solver_available() is True

    monkeypatch.setattr(shutil, "which", lambda exe: None)
    assert config.js_solver_available() is False


# --------------------------------------------------------------------------
# the parent-death watchdog loop
# --------------------------------------------------------------------------


def test_the_watchdog_exits_the_worker_once_the_parent_is_gone(monkeypatch, capsys):
    """os._exit rather than sys.exit: this runs on a daemon thread, where
    raising SystemExit would not interrupt inference running in C code."""
    from app.core import process

    monkeypatch.setattr(process, "process_exists", lambda pid: False)
    monkeypatch.setattr(process, "_PARENT_POLL_SECONDS", 0)

    exited: list[int] = []

    def _exit(code):
        exited.append(code)
        raise SystemExit(code)  # stand in for os._exit so the loop stops here

    monkeypatch.setattr(os, "_exit", _exit)

    with pytest.raises(SystemExit):
        process._watch_parent(4242)

    assert exited == [1]
    # The parent's stderr reader is watching for this marker.
    assert "@@ERROR@@parent process exited" in capsys.readouterr().err


def test_the_watchdog_keeps_waiting_while_the_parent_lives(monkeypatch):
    from app.core import process

    seen = {"n": 0}

    def _alive(pid):
        seen["n"] += 1
        if seen["n"] > 3:
            raise KeyboardInterrupt  # break out of the infinite loop
        return True

    monkeypatch.setattr(process, "process_exists", _alive)
    monkeypatch.setattr(process, "_PARENT_POLL_SECONDS", 0)
    monkeypatch.setattr(os, "_exit", lambda code: pytest.fail("exited while parent was alive"))

    with pytest.raises(KeyboardInterrupt):
        process._watch_parent(4242)

    assert seen["n"] == 4


# --------------------------------------------------------------------------
# the SSE endpoints
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_streaming_an_unknown_job_is_a_404(client):
    """The browser reconnects to an EventSource forever; a stream that opened
    for a deleted job would never close and never send anything."""
    r = client.get("/api/jobs/abcdefabcdef/events")

    assert r.status_code == 404


def test_the_queue_stream_rejects_an_unknown_job(client):
    r = client.get("/api/queue/abcdefabcdef/events")

    assert r.status_code in (404, 405)
