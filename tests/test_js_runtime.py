"""Finding the bundled JS runtime, on every packaging layout (#438).

yt-dlp ships the challenge solver as a Python package (the yt-dlp-ejs
dependency) but needs a JavaScript engine to execute it. The three packages do
not put that engine in the same place:

  Windows / Linux   a staged tree, binary in data/jsruntime
  macOS             a downloaded runtime pack, binary next to backend/
  Docker            deno on PATH, nothing bundled

Getting this wrong is silent. bundled_js_runtime() returns None, yt-dlp falls
back to the client that skips the challenge, and imports keep working until
that client goes away.
"""

from __future__ import annotations

import sys

import pytest

from app.core import config as cfg


def _touch(directory, stem):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / (f"{stem}.exe" if sys.platform.startswith("win") else stem)
    exe.write_bytes(b"#!/bin/sh\n")
    return exe


def test_nothing_bundled_is_not_an_error(tmp_path, monkeypatch):
    """Docker and source checkouts bundle nothing and resolve deno from PATH.
    None is the correct answer, not a failure."""
    monkeypatch.setattr(cfg, "JS_RUNTIME_DIR", tmp_path / "absent")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (tmp_path / "absent",))
    assert cfg.bundled_js_runtime() is None


def test_the_staged_layout_is_found(tmp_path, monkeypatch):
    """Windows and Linux: data/jsruntime, which is JS_RUNTIME_DIR's default."""
    exe = _touch(tmp_path / "data" / "jsruntime", "qjs")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (tmp_path / "data" / "jsruntime",))
    assert cfg.bundled_js_runtime() == ("quickjs", exe)


def test_the_runtime_pack_layout_is_found(tmp_path, monkeypatch):
    """macOS: beside backend/, because the app's data directory is user-owned
    and a binary there would have to be installed rather than shipped."""
    exe = _touch(tmp_path / "runtime" / "jsruntime", "qjs")
    monkeypatch.setattr(
        cfg, "_js_runtime_dirs", lambda: (tmp_path / "absent", tmp_path / "runtime" / "jsruntime")
    )
    assert cfg.bundled_js_runtime() == ("quickjs", exe)


def test_the_real_search_path_covers_the_packaged_layout():
    """The list itself, not a monkeypatched stand-in.

    In a package this file is backend/app/core/config.py, so parents[2] is
    backend/ and backend/jsruntime is where all three build scripts put the
    binary. A refactor that changes that index leaves every desktop package
    silently without an engine, and silently is the whole problem: yt-dlp just
    falls back to the client that skips the challenge.
    """
    from pathlib import Path

    dirs = cfg._js_runtime_dirs()
    assert cfg.JS_RUNTIME_DIR in dirs, "the env-var override must still win"
    package_root = Path(cfg.__file__).resolve().parents[2]
    assert package_root / "jsruntime" in dirs, (
        "backend/jsruntime is not searched; the build scripts put the engine there"
    )
    assert len(set(dirs)) == len(dirs), "duplicate directories mean a wasted stat per lookup"


def test_the_build_scripts_agree_with_the_lookup():
    """A cross-language contract: three shell/PowerShell scripts choose where
    the engine lands, and Python decides where to look. Nothing else checks
    that those two facts still match, and a mismatch only shows up as YouTube
    imports quietly degrading on one platform.
    """
    from pathlib import Path

    repo = Path(cfg.__file__).resolve().parents[2]
    scripts = {
        "windows": repo / "scripts" / "windows" / "make-portable.ps1",
        "linux": repo / "scripts" / "linux" / "make-portable.sh",
        "macos": repo / "scripts" / "macos" / "make-runtime-pack.sh",
    }
    for name, path in scripts.items():
        if not path.is_file():
            continue  # running from an installed package, not the repo
        text = path.read_text(encoding="utf-8")
        assert "jsruntime" in text, f"{name} no longer installs a JS runtime"
        assert "BackendDir" in text or "BACKEND_DIR" in text, (
            f"{name} puts the engine outside backend/, where the updater cannot reach it"
        )
        assert "sha256" in text.lower(), f"{name} fetches a binary without verifying it"


def test_ytdlp_preference_order_is_honoured(tmp_path, monkeypatch):
    """deno 1000 > node 900 > quickjs 850 in yt-dlp's own provider ranking. A
    build shipping more than one should get the solver yt-dlp would pick."""
    directory = tmp_path / "jsruntime"
    _touch(directory, "qjs")
    deno = _touch(directory, "deno")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (directory,))
    assert cfg.bundled_js_runtime() == ("deno", deno)


def test_an_unreadable_directory_does_not_raise(tmp_path, monkeypatch):
    """Never raises: this runs inside every YoutubeDL construction, and a
    permissions problem must degrade to 'not bundled', not fail an import."""

    class Boom:
        def is_dir(self):
            raise OSError("permission denied")

    good = _touch(tmp_path / "ok", "qjs")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (Boom(), tmp_path / "ok"))
    assert cfg.bundled_js_runtime() == ("quickjs", good)


def test_solver_availability_reports_a_bundled_runtime(tmp_path, monkeypatch):
    directory = tmp_path / "jsruntime"
    _touch(directory, "qjs")
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (directory,))
    assert cfg.js_solver_available() is True


def test_the_ejs_solver_resolves_from_the_vendored_copy():
    """The engine is useless without the script. yt-dlp resolves this package
    before any remote source, which is what keeps --remote-components off, and
    it must come from app/_vendor rather than site-packages: a dependency would
    change uv.lock, shift runtimeId, and stand the desktop updater down."""
    import yt_dlp.dependencies
    import yt_dlp_ejs

    assert yt_dlp.dependencies.yt_dlp_ejs is not None, (
        "the solver is missing; yt-dlp would fall back to fetching it at runtime"
    )
    assert "_vendor" in yt_dlp_ejs.__file__.replace("\\", "/"), (
        f"resolved from {yt_dlp_ejs.__file__}, not the vendored copy"
    )


def test_the_vendored_payload_is_complete():
    """The Python shim alone is not the solver. Without the .js files yt-dlp
    finds the package, believes it has a solver, and fails at solve time."""
    from pathlib import Path

    import app

    vendor = Path(app.__file__).resolve().parent / "_vendor" / "yt_dlp_ejs"
    scripts = list((vendor / "yt" / "solver").glob("*.js"))
    assert scripts, "no solver scripts vendored"
    assert all(p.stat().st_size > 0 for p in scripts)


def test_the_vendor_path_insert_is_idempotent():
    """app/__init__ runs on every import of the package. Re-importing must not
    keep growing sys.path."""
    import importlib
    import sys

    import app

    before = sys.path.count(
        str(__import__("pathlib").Path(app.__file__).resolve().parent / "_vendor")
    )
    importlib.reload(app)
    after = sys.path.count(
        str(__import__("pathlib").Path(app.__file__).resolve().parent / "_vendor")
    )
    assert after == before == 1


@pytest.mark.parametrize("stem", ["deno", "node", "qjs"])
def test_every_supported_runtime_is_recognised(tmp_path, monkeypatch, stem):
    directory = tmp_path / stem
    exe = _touch(directory, stem)
    monkeypatch.setattr(cfg, "_js_runtime_dirs", lambda: (directory,))
    found = cfg.bundled_js_runtime()
    assert found is not None and found[1] == exe
