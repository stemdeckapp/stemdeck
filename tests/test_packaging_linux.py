"""The Linux desktop-entry assets staged into the portable tarball (#360).

These are plain data files with no code path to exercise them until the
installer lands (#361), so the things worth pinning are the ones that fail
silently at the user's end: a launcher that will not start, or an icon the
packaging script cannot find.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "packaging" / "linux" / "stemdeck.desktop.in"
ICON = ROOT / "desktop" / "src-tauri" / "icons" / "icon.png"
MAKE_PORTABLE = ROOT / "scripts" / "linux" / "make-portable.sh"


def _git_mode(relative: str) -> str | None:
    """The file mode git has recorded for `relative`, or None if it cannot say.

    None covers both "git is not installed" and "this is not a working tree",
    which are the same thing as far as the caller is concerned: fall back.
    """
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "ls-files", "-s", "--", relative],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split(maxsplit=1)[0]


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


def test_make_portable_stages_the_desktop_assets():
    script = MAKE_PORTABLE.read_text(encoding="utf-8")
    assert "packaging/stemdeck.png" in script
    assert "packaging/stemdeck.desktop.in" in script


def test_make_portable_stages_the_installer():
    """Without this the installer is not in the tarball, and the README tells
    users to run a file that is not there."""
    script = MAKE_PORTABLE.read_text(encoding="utf-8")
    assert "packaging/linux/install.sh" in script
    assert 'chmod +x "$STAGE/install.sh"' in script


def test_the_installer_exists_and_is_executable():
    """The bit that reaches the user is the one git records, not the one on disk.

    Asking the filesystem is wrong on Windows, where NTFS carries no execute
    bit at all and `core.filemode` is false, so a perfectly good `100755` file
    reads back as `0o666` and this failed for everyone developing there. It is
    also the wrong question: the tarball is built from what git has, so a file
    committed without the bit would ship unrunnable even if the author had
    chmod-ed their own copy.

    So read git's index. The filesystem is kept only as a fallback for a
    checkout that is not a git working tree at all, such as an unpacked sdist.
    """
    installer = ROOT / "packaging" / "linux" / "install.sh"
    assert installer.is_file()

    mode = _git_mode("packaging/linux/install.sh")
    if mode is None:
        assert installer.stat().st_mode & 0o111, "install.sh must be executable"
        return
    assert mode == "100755", (
        f"install.sh is recorded as {mode}; it ships unrunnable. "
        "Fix with: git update-index --chmod=+x packaging/linux/install.sh"
    )


def test_readme_documents_the_installer():
    readme = (ROOT / "packaging" / "linux" / "README-LINUX.txt").read_text(encoding="utf-8")
    assert "./install.sh" in readme
    assert "--uninstall" in readme
