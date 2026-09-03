#!/usr/bin/env python3
"""Ship yt-dlp with only the extractors StemDeck can actually reach.

yt-dlp bundles ~940 site extractors. StemDeck accepts YouTube and SoundCloud
and nothing else -- `validate_youtube_url` in app/pipeline/download.py rejects
every other host before yt-dlp is ever called -- so the rest are unreachable
code that we nonetheless copy onto every user's disk.

That is mostly a nuisance, except for the part that is not: several dozen of
them are adult sites, named as such. `pornhub.py`, `xhamster.py`,
`spankbang.py`, `chaturbate.py` and friends sit in the install directory, and
`lazy_extractors.py` is a single 15,000-line file listing every one of those
domains as a URL regex. A music tool has no business putting that on someone's
computer, and "it is dormant" is not an answer to a user who found it, or to a
corporate scanner that indexed it.

So the packaging scripts run this after installing dependencies and before the
post-strip import check, which then doubles as the proof the prune was safe.

WHAT IS KEPT, AND WHY IT IS NOT THE OBVIOUS LIST

  youtube, soundcloud    what StemDeck downloads
  generic                the loader imports GenericIE *by name* as the final
                         fallback, so it is not optional
  common, commonprotocols, unsupported
                         base classes and the "this site is not supported" path
  <discovered>           modules that the REST of yt_dlp imports from outside
                         extractor/. Today that is openload (YoutubeDL.py),
                         adobepass (yt_dlp/__init__.py) and afreecatv, whose
                         helper `downloader/soop.py` imports. A hand-written
                         list would have missed afreecatv, and `import yt_dlp`
                         would fail outright.

Everything reachable from those, transitively, comes along.

HOW THE REGISTRY IS REBUILT

`extractor/extractors.py` prefers `lazy_extractors.py` and falls back to
`_extractors.py` on ImportError. lazy_extractors.py has to go regardless: it is
where all the domain strings live, which is the whole point. Removing it puts
the loader on its own documented fallback path, so `_extractors.py` is rewritten
to import just what remains. No patching of yt-dlp's logic.

Idempotent: running it twice is a no-op.

Usage:
    python scripts/prune_ytdlp_extractors.py <site-packages> [--no-verify]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

# Extractors StemDeck itself reaches, plus the ones the loader hard-requires.
# Anything else in the keep set is discovered, never assumed.
SEEDS = frozenset(
    {
        "youtube",
        "soundcloud",
        "generic",
        "common",
        "commonprotocols",
        "unsupported",
    }
)

# Files that are the registry itself rather than an extractor.
REGISTRY = frozenset({"__init__.py", "extractors.py", "_extractors.py"})

# The classes _extractors.py must expose. Kept explicit so that a yt-dlp release
# renaming one fails here, loudly, instead of at a user's first download.
EXPORTS = {
    "soundcloud": (
        "SoundcloudIE",
        "SoundcloudPlaylistIE",
        "SoundcloudSetIE",
        "SoundcloudUserIE",
    ),
    "youtube": ("YoutubeIE", "YoutubePlaylistIE", "YoutubeTabIE"),
    "generic": ("GenericIE",),
}

_SIBLING_IMPORT = re.compile(r"^\s*from \.(\w+)", re.M)
_CROSS_IMPORT = re.compile(r"^\s*from \.{1,2}extractor\.(\w+) import", re.M)


def discover_external_seeds(pkg: pathlib.Path) -> set[str]:
    """Extractor modules that the rest of yt_dlp imports by name.

    These are invisible from inside extractor/ and are exactly the ones a
    hand-maintained list gets wrong, so they are read out of the source every
    time rather than written down once.
    """
    found: set[str] = set()
    extractor_dir = pkg / "extractor"
    for path in pkg.rglob("*.py"):
        if extractor_dir in path.parents or path.parent == extractor_dir:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(_CROSS_IMPORT.findall(text))
    return found


def _files_for(root: pathlib.Path, name: str) -> list[pathlib.Path]:
    """Every source file of one extractor, module or package."""
    module = root / f"{name}.py"
    if module.is_file():
        return [module]
    package = root / name
    return sorted(package.rglob("*.py")) if package.is_dir() else []


def resolve_keep_set(root: pathlib.Path, seeds: set[str]) -> set[str]:
    """Close the seed set over sibling imports."""
    keep: set[str] = set()
    queue = list(seeds)
    while queue:
        name = queue.pop()
        if name in keep:
            continue
        files = _files_for(root, name)
        if not files:
            # `from .utils import ...` and friends resolve outside extractor/.
            continue
        keep.add(name)
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            queue.extend(_SIBLING_IMPORT.findall(text))
    return keep


def render_registry(keep: set[str]) -> str:
    lines = [
        "# flake8: noqa: F401",
        "# Generated by scripts/prune_ytdlp_extractors.py at package time.",
        "#",
        "# StemDeck rejects every host but YouTube and SoundCloud before yt-dlp is",
        "# called, so the ~930 other extractors were unreachable. They are not",
        "# shipped, which also keeps several dozen adult-site modules and their",
        "# URL regexes off the user's disk.",
        "",
    ]
    for module, names in EXPORTS.items():
        if module not in keep:
            continue
        if len(names) == 1:
            lines.append(f"from .{module} import {names[0]}")
        else:
            lines.append(f"from .{module} import (")
            lines.extend(f"    {n}," for n in names)
            lines.append(")")
    return "\n".join(lines) + "\n"


def verify(site_packages: pathlib.Path) -> None:
    """Prove the pruned tree still loads and still matches the two hosts.

    Run in a subprocess so it exercises a cold import of what will ship, rather
    than whatever this process already has in sys.modules.
    """
    code = (
        "import pathlib, sys\n"
        "import yt_dlp\n"
        # Prove we are checking the tree that was just pruned. Without this the
        # check happily passes against some other yt-dlp on sys.path and reports
        # a green result for a bundle nobody looked at.
        "want = pathlib.Path(sys.argv[1]).resolve()\n"
        "got = pathlib.Path(yt_dlp.__file__).resolve()\n"
        "assert want in got.parents, f'verified the wrong yt_dlp: {got}'\n"
        "from yt_dlp.extractor import get_info_extractor, gen_extractor_classes\n"
        "names = sorted(c.IE_NAME for c in gen_extractor_classes())\n"
        "assert get_info_extractor('Youtube').suitable("
        "'https://www.youtube.com/watch?v=dQw4w9WgXcQ'), 'YouTube URL no longer matches'\n"
        "assert get_info_extractor('Youtube').suitable("
        "'https://youtu.be/dQw4w9WgXcQ'), 'youtu.be URL no longer matches'\n"
        "assert get_info_extractor('Soundcloud').suitable("
        "'https://soundcloud.com/artist/track'), 'SoundCloud URL no longer matches'\n"
        "print('  verified:', ', '.join(names))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(site_packages)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout + result.stderr)
        raise SystemExit("pruned yt-dlp failed verification; refusing to ship it")
    sys.stdout.write(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site_packages", type=pathlib.Path)
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the post-prune import check (the caller runs its own)",
    )
    args = parser.parse_args()

    pkg = args.site_packages / "yt_dlp"
    root = pkg / "extractor"
    if not root.is_dir():
        raise SystemExit(f"no yt_dlp extractor directory under {args.site_packages}")

    seeds = set(SEEDS) | discover_external_seeds(pkg)
    keep = resolve_keep_set(root, seeds)

    missing = sorted(set(EXPORTS) - keep)
    if missing:
        raise SystemExit(f"required extractors missing from yt-dlp: {missing}")

    removed = 0
    for path in sorted(root.iterdir()):
        if path.name in REGISTRY:
            continue
        if path.name == "__pycache__":
            shutil.rmtree(path)
            continue
        stem = path.stem if path.suffix == ".py" else path.name
        if stem in keep:
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        removed += 1

    lazy = root / "lazy_extractors.py"
    if lazy.exists():
        lazy.unlink()

    (root / "_extractors.py").write_text(render_registry(keep), encoding="utf-8")
    shutil.rmtree(root / "__pycache__", ignore_errors=True)

    print(f"==> Pruned yt-dlp extractors: removed {removed}, kept {len(keep)}")
    print(f"  kept: {', '.join(sorted(keep))}")
    if not args.no_verify:
        verify(args.site_packages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
