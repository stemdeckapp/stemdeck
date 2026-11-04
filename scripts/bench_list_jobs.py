"""Count and time the filesystem calls behind GET /api/jobs (#976).

Builds a synthetic library of finished jobs in a folder of its own, then calls
the list route repeatedly while counting every stat, directory listing, path
resolve and open the process makes. With --latency-ms each of those calls also
sleeps first, which stands in for the round trip an SMB or NAS mount adds. That
number is synthetic: it says how many round trips the list costs, not what one
round trip costs on a given network.

    uv run python scripts/bench_list_jobs.py --songs 300
    uv run python scripts/bench_list_jobs.py --songs 300 --latency-ms 2

The list trusts a kept answer only for a folder changed longer ago than
LIBRARY_RACY_WINDOW_NS, so a library built a moment ago would never show the
saving. The script sets the song and stems folders' times back an hour after
building them, as for a library that has sat a while.

Where it may write. The library folder must be new or one this script made (it
leaves a marker file), and it must be inside this repository. For a real
network number, --allow-outside-repo accepts a folder elsewhere, such as an
empty share on a NAS. Either way it refuses a folder that holds a registry.json
or sits anywhere under a folder that does, or under a folder that looks like a
StemDeck library (song folders with 12-hex names holding metadata.json), so
it cannot be pointed into a real library. App data and cache always go under
the repository's data/ folder. It prints the resolved folders and asks
nothing.
"""

from __future__ import annotations

import argparse
import builtins
import json
import ntpath
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = ".bench_list_jobs"
STEMS = ("vocals", "drums", "bass", "other", "guitar", "piano")
_SONG_DIR_RE = re.compile(r"^[0-9a-f]{12}$")
_AGED_BY_S = 3600


def _looks_like_library(folder: Path) -> bool:
    """registry.json, or a song folder (12-hex name holding metadata.json)."""
    if (folder / "registry.json").exists():
        return True
    try:
        children = list(folder.iterdir())
    except OSError:
        return False
    return any(
        _SONG_DIR_RE.match(child.name) and (child / "metadata.json").is_file() for child in children
    )


def check_library(library: Path, root: Path, *, allow_outside_repo: bool) -> str | None:
    """Why this folder must not be used, or None when it may be."""
    library = library.resolve()
    root = root.resolve()
    if not allow_outside_repo and not library.is_relative_to(root):
        return f"{library} is outside the repository (pass --allow-outside-repo for a NAS run)"
    if library.exists():
        if not (library / MARKER).is_file():
            return f"{library} already exists and is not a library this script made"
        if (library / "registry.json").exists():
            return f"{library} holds a registry.json"
    for ancestor in library.parents:
        if _looks_like_library(ancestor):
            return f"{library} is inside {ancestor}, which looks like a StemDeck library"
    return None


def data_dir_for(root: Path) -> Path:
    """App data and cache for the run, always under the repository's data/."""
    data = (root / "data" / "bench-list-jobs").resolve()
    if not data.is_relative_to(root.resolve()):
        raise SystemExit(f"refusing data folder {data}: outside the repository")
    return data


def _prepare_folder(library: Path) -> None:
    if library.exists():
        return
    library.mkdir(parents=True)
    (library / MARKER).write_text("synthetic library for bench_list_jobs.py", encoding="utf-8")


def _build(library: Path, songs: int) -> list:
    """Jobs in three kinds, in proportion to a library that has been in use a
    while: links, uploads that kept their source, and uploads finished before
    sources were kept (source_format unknown, nothing on disk to find)."""
    from app.core.models import Job

    aged = time.time() - _AGED_BY_S
    jobs = []
    for n in range(songs):
        job_id = f"{n:012x}"
        job_dir = library / job_id
        stems = job_dir / "stems"
        kind = n % 10
        if not stems.is_dir():
            stems.mkdir(parents=True)
            for name in STEMS:
                (stems / f"{name}.wav").write_bytes(b"RIFF0000WAVE")
            (job_dir / "metadata.json").write_text(json.dumps({"title": f"Song {n}"}))
            if kind == 8:
                (job_dir / "source.mp3").write_bytes(b"ID3")
        os.utime(stems, (aged, aged))
        os.utime(job_dir, (aged, aged))
        if kind == 8:
            source_url, source_format = f"local:Song {n}", "mp3"
        elif kind == 9:
            source_url, source_format = f"local:Song {n}", None
        else:
            source_url, source_format = f"https://www.youtube.com/watch?v={n:011d}", None
        jobs.append(
            Job(
                id=job_id,
                status="done",
                title=f"Song {n}",
                source_url=source_url,
                source_format=source_format,
                created_at=float(n),
            )
        )
    return jobs


class _Counter:
    """Wraps the calls pathlib and the list route reach the disk through."""

    def __init__(self, latency_s: float) -> None:
        self.latency_s = latency_s
        self.counts: dict[str, int] = {}
        self._saved: list[tuple[object, str, object]] = []

    def _wrap(self, owner: object, attr: str, label: str) -> None:
        original = getattr(owner, attr)

        def wrapper(*args, **kwargs):
            self.counts[label] = self.counts.get(label, 0) + 1
            if self.latency_s:
                time.sleep(self.latency_s)
            return original(*args, **kwargs)

        self._saved.append((owner, attr, original))
        setattr(owner, attr, wrapper)

    def __enter__(self) -> _Counter:
        self._wrap(os, "stat", "stat")
        self._wrap(os, "lstat", "lstat")
        self._wrap(os, "scandir", "scandir")
        self._wrap(os, "listdir", "listdir")
        self._wrap(builtins, "open", "open")
        if os.name == "nt":
            # Path.resolve on Windows opens a handle to ask for the final path.
            self._wrap(ntpath, "_getfinalpathname", "resolve")
        return self

    def __exit__(self, *exc: object) -> None:
        for owner, attr, original in reversed(self._saved):
            setattr(owner, attr, original)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=ROOT / "data" / "f49" / "jobs")
    parser.add_argument("--songs", type=int, default=300)
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--allow-outside-repo",
        action="store_true",
        help="accept a library folder outside the repository, for a NAS run",
    )
    args = parser.parse_args()

    library = args.library.resolve()
    refusal = check_library(library, ROOT, allow_outside_repo=args.allow_outside_repo)
    if refusal:
        sys.exit(f"refusing: {refusal}")
    data = data_dir_for(ROOT)
    _prepare_folder(library)
    print(f"library folder: {library}")
    print(f"app data folder: {data}")
    os.environ["STEMDECK_JOBS_DIR"] = str(library)
    os.environ["STEMDECK_DATA_DIR"] = str(data)
    os.environ["STEMDECK_CACHE_DIR"] = str(data / "cache")
    sys.path.insert(0, str(ROOT))

    from app.api import jobs as jobs_api
    from app.core import config, registry

    if Path(config.JOBS_DIR).resolve() != library:
        sys.exit(f"jobs folder resolved to {config.JOBS_DIR}, expected {library}")
    print(f"jobs folder: {config.JOBS_DIR}")

    for job in _build(library, args.songs):
        registry.register(job)

    latency_s = args.latency_ms / 1000
    rows = []
    for call in range(1, args.repeats + 1):
        with _Counter(latency_s) as counter:
            start = time.perf_counter()
            listed = jobs_api.list_jobs()
            elapsed = time.perf_counter() - start
        assert len(listed) == args.songs, len(listed)
        rows.append((call, elapsed, dict(counter.counts)))

    print(f"{args.songs} songs, injected latency {args.latency_ms} ms per call (synthetic)")
    for call, elapsed, counts in rows:
        total = sum(counts.values())
        detail = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(
            f"call {call}: {elapsed * 1000:8.1f} ms  {total:5d} fs calls "
            f"({total / args.songs:.2f} per song)  {detail}"
        )
    warm = [elapsed for _call, elapsed, _counts in rows[1:]] or [rows[0][1]]
    print(f"median of calls 2-{len(rows)}: {statistics.median(warm) * 1000:.1f} ms")


if __name__ == "__main__":
    main()
