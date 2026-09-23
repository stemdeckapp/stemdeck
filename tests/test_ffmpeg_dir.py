"""Which FFmpeg directory the backend hands to PATH and to yt-dlp (#651).

Setup can reject the FFmpeg in the data directory and settle on another -- an
Intel pair on Apple Silicon (#637), a build missing an encoder -- and it tells
the backend through STEMDECK_FFMPEG / STEMDECK_FFPROBE. Separation honoured
that. PATH and yt-dlp were given FFMPEG_DIR instead, which in a desktop install
is always data/ffmpeg, so every YouTube import ran the binary setup had thrown
out, and the error named a binary the app had supposedly stopped using.

These build real files in a temporary data directory and reload the config
module under the environment the desktop shell would give it, because what is
under test is exactly what the backend concludes from that environment.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path

import pytest

EXE = ".exe" if sys.platform.startswith("win") else ""
ENV = ("STEMDECK_DATA_DIR", "STEMDECK_FFMPEG", "STEMDECK_FFPROBE", "STEMDECK_FFMPEG_DIR")


def _pair(directory: Path) -> tuple[Path, Path]:
    """An ffmpeg and an ffprobe, side by side, as a real install has them."""
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = directory / f"ffmpeg{EXE}"
    ffprobe = directory / f"ffprobe{EXE}"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    return ffmpeg, ffprobe


@pytest.fixture
def load(monkeypatch):
    """Reload config as the backend would start under the given environment."""
    import app.core.config as config

    def _load(data_dir: Path, **env: Path | None):
        monkeypatch.setenv("STEMDECK_DATA_DIR", str(data_dir))
        for name in ENV[1:]:
            value = env.get(name.removeprefix("STEMDECK_").lower())
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, str(value))
        return importlib.reload(config)

    yield _load
    monkeypatch.undo()
    importlib.reload(config)


@pytest.fixture
def config_log():
    """What config logged. Attached to its own logger rather than through
    caplog, because the app's logging setup may stop propagation upward."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("stemdeck.config")
    handler = Keep(level=logging.INFO)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _ydl_location(config) -> str | None:
    # Imported here: download.py holds the function it imported from config,
    # and a reload of config re-executes the same module dict, so it sees the
    # reloaded values.
    from app.pipeline.download import _base_ydl_opts

    return _base_ydl_opts(["youtube"]).get("ffmpeg_location")


# ── the bug ──────────────────────────────────────────────────────────────────


def test_a_rejected_data_dir_ffmpeg_is_not_the_directory_used(load, tmp_path):
    data = tmp_path / "data"
    _pair(data / "ffmpeg")  # the pair setup rejected
    ffmpeg, ffprobe = _pair(tmp_path / "system")  # the one it verified

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)

    assert config.ffmpeg_dir() == (tmp_path / "system").resolve()


def test_path_gets_the_verified_directory_and_never_the_rejected_one(load, monkeypatch, tmp_path):
    data = tmp_path / "data"
    _pair(data / "ffmpeg")
    ffmpeg, ffprobe = _pair(tmp_path / "system")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)
    config.configure_portable_environment()

    entries = [Path(p) for p in os.environ["PATH"].split(os.pathsep)]
    assert entries[0] == (tmp_path / "system").resolve()
    assert (data / "ffmpeg").resolve() not in [p.resolve() for p in entries]


def test_yt_dlp_is_given_the_verified_directory(load, tmp_path):
    data = tmp_path / "data"
    _pair(data / "ffmpeg")
    ffmpeg, ffprobe = _pair(tmp_path / "system")

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)

    assert _ydl_location(config) == str((tmp_path / "system").resolve())


def test_skipping_the_data_dir_copy_is_logged(load, config_log, monkeypatch, tmp_path):
    # The failure was silent; the next report should find this in the log.
    data = tmp_path / "data"
    _pair(data / "ffmpeg")
    ffmpeg, ffprobe = _pair(tmp_path / "system")
    monkeypatch.setenv("PATH", "")

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)
    config.configure_portable_environment()

    messages = [r.getMessage() for r in config_log]
    assert any(m.startswith("Not using") and "ffmpeg" in m for m in messages), messages


# ── the nested layout (#248) ─────────────────────────────────────────────────


def test_the_nested_bin_layout_hands_over_bin(load, config_log, monkeypatch, tmp_path):
    # yt-dlp was given data/ffmpeg here, which holds no binary, and it runs
    # dir/ffmpeg literally without falling back to PATH.
    data = tmp_path / "data"
    ffmpeg, ffprobe = _pair(data / "ffmpeg" / "bin")
    monkeypatch.setenv("PATH", "")

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)
    config.configure_portable_environment()

    assert config.ffmpeg_dir() == (data / "ffmpeg" / "bin").resolve()
    assert _ydl_location(config) == str((data / "ffmpeg" / "bin").resolve())
    # The same install, nested: nothing is being skipped.
    assert not any(r.getMessage().startswith("Not using") for r in config_log)


# ── what has to keep working ─────────────────────────────────────────────────


def test_a_verified_data_dir_ffmpeg_is_still_used(load, tmp_path):
    data = tmp_path / "data"
    ffmpeg, ffprobe = _pair(data / "ffmpeg")

    config = load(data, ffmpeg=ffmpeg, ffprobe=ffprobe)

    assert config.ffmpeg_dir() == (data / "ffmpeg").resolve()
    assert _ydl_location(config) == str((data / "ffmpeg").resolve())


def test_docker_and_source_runs_leave_it_to_path(load, monkeypatch, tmp_path):
    # No FFmpeg of our own and no override: PATH is already right there.
    monkeypatch.setenv("PATH", str(tmp_path / "usr-bin"))

    config = load(tmp_path / "data")
    config.configure_portable_environment()

    assert config.ffmpeg_dir() is None
    assert os.environ["PATH"] == str(tmp_path / "usr-bin")
    assert _ydl_location(config) is None


def test_a_hand_set_ffmpeg_dir_is_still_honoured(load, tmp_path):
    # Source runs point STEMDECK_FFMPEG_DIR at a bundled build when FFmpeg is
    # not on PATH, with no STEMDECK_FFMPEG. The binary defaults into it.
    bundled = tmp_path / "bundled-ffmpeg"
    _pair(bundled)

    config = load(tmp_path / "data", ffmpeg_dir=bundled)

    assert config.ffmpeg_dir() == bundled.resolve()
    assert _ydl_location(config) == str(bundled.resolve())


def test_a_split_pair_is_left_to_path(load, monkeypatch, tmp_path):
    # yt-dlp takes one directory and looks for both binaries in it, so there
    # is no directory to give it. PATH decides, where the desktop shell has
    # already put the verified FFmpeg's directory first.
    ffmpeg, _ = _pair(tmp_path / "a")
    _, ffprobe = _pair(tmp_path / "b")
    monkeypatch.setenv("PATH", str(tmp_path / "shell-path"))

    config = load(tmp_path / "data", ffmpeg=ffmpeg, ffprobe=ffprobe)
    config.configure_portable_environment()

    assert config.ffmpeg_dir() is None
    assert os.environ["PATH"] == str(tmp_path / "shell-path")
    assert _ydl_location(config) is None
