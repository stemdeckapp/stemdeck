#!/usr/bin/env python3
"""Refresh the vendored yt-dlp-ejs payload in app/_vendor.

Vendoring normally means remembering to update something, which is a bad trade.
This exists so it does not: run with no arguments to check whether PyPI has a
newer release, and with --apply to take it.

    python scripts/update_vendored_ejs.py            # check only, exit 1 if stale
    python scripts/update_vendored_ejs.py --apply    # fetch and replace

Why the package is vendored at all rather than declared as a dependency: the
desktop updater derives runtimeId from sha256(uv.lock) and stands down when it
changes, because it can replace backend/ but never python/. Adding a dependency
would have sent every existing desktop install to a manual reinstall. This is a
53 KB pure-Python payload that StemDeck never imports and yt-dlp discovers at
runtime, so it belongs in the app layer the updater does replace.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

PACKAGE = "yt-dlp-ejs"
VENDOR = Path(__file__).resolve().parents[1] / "app" / "_vendor"
PKG_DIR = VENDOR / "yt_dlp_ejs"
LICENSE_DEST = VENDOR / "LICENSE.yt-dlp-ejs"


def installed_version() -> str | None:
    version_file = PKG_DIR / "_version.py"
    if not version_file.is_file():
        return None
    # setuptools-scm's _version.py lists "__version__" in __all__ before it
    # assigns it, so match the assignment specifically rather than the name.
    match = re.search(
        r"""^__version__(?:\s*:\s*str)?\s*=.*?["']([^"']+)["']""",
        version_file.read_text(encoding="utf-8"),
        re.M,
    )
    return match.group(1) if match else None


def latest_release() -> tuple[str, str, str]:
    """(version, wheel url, sha256) for the newest release on PyPI."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{PACKAGE}/json", timeout=30) as fh:
        data = json.load(fh)
    version = data["info"]["version"]
    for entry in data["urls"]:
        if entry["filename"].endswith(".whl"):
            return version, entry["url"], entry["digests"]["sha256"]
    raise SystemExit(f"{PACKAGE} {version} publishes no wheel")


def apply(url: str, sha256: str) -> None:
    with urllib.request.urlopen(url, timeout=60) as fh:
        blob = fh.read()
    got = hashlib.sha256(blob).hexdigest()
    if got != sha256:
        raise SystemExit(f"checksum mismatch: expected {sha256}, got {got}")

    archive = zipfile.ZipFile(io.BytesIO(blob))
    if PKG_DIR.exists():
        shutil.rmtree(PKG_DIR)
    VENDOR.mkdir(parents=True, exist_ok=True)

    for name in archive.namelist():
        if name.startswith("yt_dlp_ejs/") and not name.endswith("/"):
            dest = VENDOR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(name))
        elif name.lower().endswith("licenses/license"):
            LICENSE_DEST.write_bytes(archive.read(name))

    # Bytecode from whoever ran this has no business in the repo or the package.
    for cache in PKG_DIR.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="fetch and replace the payload")
    args = parser.parse_args()

    have = installed_version()
    version, url, sha256 = latest_release()

    if have == version:
        print(f"{PACKAGE} {version} vendored, up to date")
        return 0

    if not args.apply:
        print(f"{PACKAGE}: vendored {have or '(none)'}, latest {version}")
        print("run with --apply to update")
        return 1

    apply(url, sha256)
    print(f"{PACKAGE}: {have or '(none)'} -> {version}")
    print(f"  sha256 {sha256}")
    print("  review the diff and commit app/_vendor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
