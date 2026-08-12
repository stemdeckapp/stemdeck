"""The Linux desktop-entry assets staged into the portable tarball (#360).

These are plain data files with no code path to exercise them until the
installer lands (#361), so the things worth pinning are the ones that fail
silently at the user's end: a launcher that will not start, or an icon the
packaging script cannot find.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "packaging" / "linux" / "stemdeck.desktop.in"
ICON = ROOT / "desktop" / "src-tauri" / "icons" / "icon.png"
MAKE_PORTABLE = ROOT / "scripts" / "linux" / "make-portable.sh"


def _entries() -> dict[str, str]:
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Desktop Entry]", "the group header must come first"
    return dict(line.split("=", 1) for line in lines[1:] if line and not line.startswith("#"))


def test_exec_is_quoted_so_a_path_with_spaces_still_launches():
    """The freedesktop spec splits Exec on whitespace.

    The reference installer in #342 emitted an unquoted Exec, so installing to
    a directory such as ~/My Apps produced an entry that tried to run a binary
    called ".../My". Quoting is the whole fix, and it is invisible until
    someone picks a custom path.
    """
    exec_line = _entries()["Exec"].replace("@EXEC@", "/home/u/My Apps/StemDeck-Linux-x64/StemDeck")
    assert shlex.split(exec_line) == ["/home/u/My Apps/StemDeck-Linux-x64/StemDeck"]


def test_placeholders_are_present_for_the_installer_to_substitute():
    entries = _entries()
    assert "@EXEC@" in entries["Exec"]
    assert entries["Icon"] == "@ICON@"


@pytest.mark.parametrize("key", ["Type", "Name", "Exec", "Icon", "Categories"])
def test_required_keys_are_present(key):
    assert key in _entries()


def test_type_and_categories_are_registered_values():
    entries = _entries()
    assert entries["Type"] == "Application"
    cats = [c for c in entries["Categories"].split(";") if c]
    # A registered main category is required; Audio and Music are additional
    # ones that only carry meaning alongside it.
    assert "AudioVideo" in cats
    assert "Multimedia" not in cats, "Multimedia is not a registered category"
    assert entries["Categories"].endswith(";"), "the list must be semicolon-terminated"


def test_the_icon_the_packaging_script_copies_exists():
    """make-portable.sh copies this by path. A move would break the Linux build
    at package time, long after the change that caused it."""
    assert ICON.is_file()


def test_make_portable_stages_both_assets():
    script = MAKE_PORTABLE.read_text(encoding="utf-8")
    assert "packaging/stemdeck.png" in script
    assert "packaging/stemdeck.desktop.in" in script
