from __future__ import annotations

import importlib
import os
from pathlib import Path


def test_data_dir_moves_default_runtime_dirs(monkeypatch, tmp_path: Path):
    import app.core.config as config

    original = config
    data_dir = tmp_path / "portable-data"
    monkeypatch.setenv("STEMDECK_DATA_DIR", str(data_dir))
    monkeypatch.delenv("STEMDECK_JOBS_DIR", raising=False)
    monkeypatch.delenv("STEMDECK_CACHE_DIR", raising=False)
    monkeypatch.delenv("STEMDECK_DOWNLOADS_DIR", raising=False)
    monkeypatch.delenv("STEMDECK_MODELS_DIR", raising=False)
    monkeypatch.delenv("STEMDECK_LOGS_DIR", raising=False)
    try:
        reloaded = importlib.reload(config)
        assert data_dir.resolve() == reloaded.DATA_DIR
        assert data_dir.resolve() / "jobs" == reloaded.JOBS_DIR
        assert data_dir.resolve() / "cache" == reloaded.CACHE_DIR
        assert data_dir.resolve() / "downloads" == reloaded.DOWNLOADS_DIR
        assert data_dir.resolve() / "models" == reloaded.MODELS_DIR
        assert data_dir.resolve() / "logs" == reloaded.LOGS_DIR
    finally:
        monkeypatch.delenv("STEMDECK_DATA_DIR", raising=False)
        importlib.reload(original)


def test_jobs_dir_override_wins_over_data_dir(monkeypatch, tmp_path: Path):
    import app.core.config as config

    original = config
    data_dir = tmp_path / "portable-data"
    jobs_dir = tmp_path / "custom-jobs"
    monkeypatch.setenv("STEMDECK_DATA_DIR", str(data_dir))
    monkeypatch.setenv("STEMDECK_JOBS_DIR", str(jobs_dir))
    try:
        reloaded = importlib.reload(config)
        assert data_dir.resolve() == reloaded.DATA_DIR
        assert jobs_dir.resolve() == reloaded.JOBS_DIR
    finally:
        monkeypatch.delenv("STEMDECK_DATA_DIR", raising=False)
        monkeypatch.delenv("STEMDECK_JOBS_DIR", raising=False)
        importlib.reload(original)


def test_ffmpeg_executable_prefers_portable_binary(monkeypatch, tmp_path: Path):
    import app.core.config as config

    original = config
    ffmpeg = tmp_path / "ffmpeg" / "ffmpeg"
    ffmpeg.parent.mkdir()
    ffmpeg.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("STEMDECK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STEMDECK_FFMPEG", str(ffmpeg))
    try:
        reloaded = importlib.reload(config)
        assert reloaded.ffmpeg_executable() == str(ffmpeg.resolve())
    finally:
        monkeypatch.delenv("STEMDECK_DATA_DIR", raising=False)
        monkeypatch.delenv("STEMDECK_FFMPEG", raising=False)
        importlib.reload(original)


def test_configure_portable_environment_leaves_dev_cache_env_alone(monkeypatch):
    import app.core.config as config

    original = config
    monkeypatch.delenv("STEMDECK_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("TORCH_HOME", raising=False)
    try:
        reloaded = importlib.reload(config)
        reloaded.configure_portable_environment()
        assert "XDG_CACHE_HOME" not in os.environ
        assert "TORCH_HOME" not in os.environ
    finally:
        monkeypatch.delenv("STEMDECK_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("TORCH_HOME", raising=False)
        importlib.reload(original)


# --- packaged data directory discovery (#459) --------------------------------
#
# The desktop shell always passes STEMDECK_DATA_DIR. These cover what happens
# when the backend is started some other way: by hand, from a terminal, out of
# the package the shell would normally launch. That used to resolve DATA_DIR to
# backend/ itself, so the backend read a settings.json that did not exist and
# quietly ignored every choice the user had made in the app.


def test_packaged_layout_finds_the_data_dir_beside_the_backend(tmp_path, monkeypatch):
    from app.core import config

    package = tmp_path / "StemDeck-Windows-x64"
    backend = package / "backend"
    data = package / "data"
    backend.mkdir(parents=True)
    data.mkdir()

    monkeypatch.setattr(config, "ROOT", backend)
    assert config._packaged_data_dir() == data


def test_a_source_checkout_is_not_a_package(tmp_path, monkeypatch):
    """ROOT is the repo root in a checkout and /app under Docker. Neither is
    named backend, so neither changes behaviour."""
    from app.core import config

    for name in ("stemdeck", "app"):
        root = tmp_path / name
        (root / "data").mkdir(parents=True)
        monkeypatch.setattr(config, "ROOT", root)
        assert config._packaged_data_dir() is None


def test_a_backend_without_a_data_sibling_is_not_a_package(tmp_path, monkeypatch):
    """Half a layout is not a layout. Returning a path that is not there would
    invent a data directory next to whatever the backend happened to sit in."""
    from app.core import config

    backend = tmp_path / "somewhere" / "backend"
    backend.mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", backend)
    assert config._packaged_data_dir() is None
