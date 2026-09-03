"""The packaging step that keeps unreachable yt-dlp extractors off users' disks.

This runs against the real installed yt-dlp, copied into a temp tree, because
the thing most likely to break it is a yt-dlp upgrade rather than a change here.
The cross-package imports it has to discover have already moved once between
releases, and when they move again the failure is `import yt_dlp` raising
ModuleNotFoundError inside a shipped bundle -- after packaging, on a user's
machine, at the first download. These tests are the earlier warning.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "prune_ytdlp_extractors.py"

yt_dlp = pytest.importorskip("yt_dlp", reason="yt-dlp is what this prunes")
YTDLP_DIR = pathlib.Path(yt_dlp.__file__).parent


@pytest.fixture(scope="module")
def pruned(tmp_path_factory) -> pathlib.Path:
    """A site-packages tree holding a pruned copy of the installed yt-dlp."""
    site = tmp_path_factory.mktemp("site-packages")
    shutil.copytree(YTDLP_DIR, site / "yt_dlp")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(site), "--no-verify"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return site


def _run_in(site: pathlib.Path, code: str) -> subprocess.CompletedProcess:
    """Run code with the pruned tree ahead of the installed one on sys.path.

    Inheriting the environment rather than replacing it: a bare env breaks
    asyncio on Windows, which yt_dlp imports on the way in, and the failure
    looks exactly like a broken prune.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(site)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )


def test_the_pruned_tree_still_imports(pruned: pathlib.Path) -> None:
    # The whole risk of this step: something outside extractor/ imports an
    # extractor by name, and deleting it breaks the package at import time
    # rather than at use time. `downloader/soop.py` does exactly that today.
    proc = _run_in(
        pruned,
        "import pathlib, yt_dlp\n"
        "assert pathlib.Path(yt_dlp.__file__).parent.parent.name.startswith("
        "'site-packages'), yt_dlp.__file__\n"
        "print('ok')\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_youtube_and_soundcloud_still_match(pruned: pathlib.Path) -> None:
    proc = _run_in(
        pruned,
        "from yt_dlp.extractor import get_info_extractor\n"
        "assert get_info_extractor('Youtube').suitable("
        "'https://www.youtube.com/watch?v=dQw4w9WgXcQ')\n"
        "assert get_info_extractor('Youtube').suitable('https://youtu.be/dQw4w9WgXcQ')\n"
        "assert get_info_extractor('Soundcloud').suitable("
        "'https://soundcloud.com/artist/track')\n"
        "print('ok')\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_registry_holds_only_what_ships(pruned: pathlib.Path) -> None:
    proc = _run_in(
        pruned,
        "from yt_dlp.extractor import gen_extractor_classes\n"
        "print(','.join(sorted(c.IE_NAME for c in gen_extractor_classes())))\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    names = proc.stdout.strip().split(",")
    assert "youtube" in names
    assert "soundcloud" in names
    # GenericIE is the loader's hard-wired final fallback, not an oversight.
    assert "generic" in names
    assert len(names) < 20, f"far more survived the prune than expected: {names}"


def test_the_adult_extractors_are_gone(pruned: pathlib.Path) -> None:
    """The reason this step exists.

    Not a size optimisation: these are files a user can find in the install
    directory, and a scanner can index, in a tool for splitting music.
    """
    root = pruned / "yt_dlp" / "extractor"
    remaining = {p.stem for p in root.iterdir()} | {p.name for p in root.iterdir()}
    for name in ("pornhub", "xhamster", "xnxx", "spankbang", "chaturbate", "redtube"):
        assert name not in remaining, f"{name} survived the prune"

    # And the file that lists every one of those domains as a URL regex, which
    # is the bulk of the exposure and is easy to forget because it is generated.
    assert not (root / "lazy_extractors.py").exists()

    # Nothing anywhere in the shipped tree should still name them.
    haystack = " ".join(p.name for p in root.rglob("*"))
    assert "porn" not in haystack.lower()


def test_it_removes_essentially_everything(pruned: pathlib.Path) -> None:
    before = len([p for p in (YTDLP_DIR / "extractor").iterdir() if p.name != "__pycache__"])
    after = len([p for p in (pruned / "yt_dlp" / "extractor").iterdir() if p.name != "__pycache__"])
    assert before > 500, "yt-dlp got much smaller; re-check what this step assumes"
    assert after < 20, f"prune kept {after} entries, expected a handful"


def test_running_it_twice_changes_nothing(pruned: pathlib.Path, tmp_path) -> None:
    """Packaging scripts get re-run, and a rebuild must not be a special case."""
    site = tmp_path / "again"
    site.mkdir()
    shutil.copytree(pruned / "yt_dlp", site / "yt_dlp")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(site), "--no-verify"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    # __pycache__ is ignored: importing the tree in the tests above recreates
    # it, and it is not part of what the prune decides.
    def entries(root):
        return sorted(p.name for p in root.iterdir() if p.name != "__pycache__")

    assert entries(pruned / "yt_dlp" / "extractor") == entries(site / "yt_dlp" / "extractor")


def test_it_refuses_rather_than_ships_a_broken_tree(tmp_path) -> None:
    """A yt-dlp that no longer has these extractors must stop the build.

    Silently shipping a bundle whose only download path is missing would look
    like a successful release and fail at the user's first URL.
    """
    site = tmp_path / "broken"
    (site / "yt_dlp" / "extractor").mkdir(parents=True)
    (site / "yt_dlp" / "__init__.py").write_text("", encoding="utf-8")
    (site / "yt_dlp" / "extractor" / "__init__.py").write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(site), "--no-verify"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "required extractors missing" in (result.stdout + result.stderr)
