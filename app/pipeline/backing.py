"""Backing tracks: a song without one of its stems, mixed on demand.

Streamer Mode lets a musician play one part themselves ("I play: Bass") over
the rest of the song. Demucs always leaves all six stems on disk
(stems/<name>.wav, STEM_NAMES), whatever the user selected at import, so the
backing is the sum of every stem file but the removed one: no separation runs
again.

The file is kept in the data cache, `<CACHE_ROOT>/<job id>/backing_without_
<stem>.wav`, with a small `.json` beside it naming the stem files it was mixed
from (size and mtime). An existing file whose sources are unchanged is never
rebuilt; one whose stems changed (a re-split) is. One build per (job, file) at
a time: a second caller waits for the first and then finds the file. A file is
only ever a sum of the stems, so the cache's budget may drop it (_prune_cache):
the next request builds it again.

Voice is the vocals stem. vocals.wav holds lead and backing vocals together
and is never deleted by the on-demand lead/backing split (#275), so removing
it removes both; the split files are never mixed in. A job whose vocals.wav
is missing but whose split exists falls back to the two split files for the
"everything but X" sum, and leaves both out when Voice is removed.

The mix sums the stems as make_original_track does (amix normalize=0: each
at its own level), in float, and is written as 16-bit PCM at the stems' rate.
Demucs stems sum back to about the original song, so leaving one out stays at
or below the song's level; should the sum still pass full scale (a stem that
was cancelling another), the whole file is scaled down to just under it
rather than clipped.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from app.core.config import CACHE_DIR, EXTRA_STEM_NAMES, JOB_ID_RE, STEM_NAMES
from app.core.stem_files import stem_exists, stem_path

logger = logging.getLogger("stemdeck.backing")

# The stems a backing can leave out: the six Demucs stems.
REMOVABLE = STEM_NAMES
_PEAK_CEILING = 0.999
_BLOCK = 1 << 16

# Every job's backing and rest files, one folder per job. 1 GiB holds about
# 24 sums of a four-minute song (42 MB each at 44.1 kHz stereo), and one sum
# of a 60-minute song (635 MB).
CACHE_ROOT = CACHE_DIR / "backing"
CACHE_MAX_BYTES = 1 << 30
# A file served this recently may still be streaming (the audio engine reads
# it in Range windows, one request each): the budget never evicts it.
_IN_USE_SEC = 60.0
_BACKING_PREFIX = "backing_without_"

# One per (job id, file name), dropped with the job (forget_job).
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()
_prune_guard = threading.Lock()


class BackingError(RuntimeError):
    """The backing could not be made (stems missing or unreadable)."""


def in_cache(path: Path) -> bool:
    return path.resolve().is_relative_to(CACHE_ROOT.resolve())


def backing_path(job_id: str, remove: str) -> Path:
    return CACHE_ROOT / job_id / f"{_BACKING_PREFIX}{remove}.wav"


def backing_inputs(stems_dir: Path, remove: str) -> list[Path]:
    """The stem files the backing without `remove` is the sum of."""
    if remove not in REMOVABLE:
        raise ValueError(f"unknown stem {remove!r}")
    names = [n for n in STEM_NAMES if n != remove]
    # WAV or MP3, whichever the song keeps (#783); libsndfile reads both.
    paths = [stem_path(stems_dir, n) for n in names]
    present = [p for p in paths if p.is_file()]
    vocals_gone = not stem_exists(stems_dir, "vocals")
    if remove != "vocals" and vocals_gone:
        # No vocals.wav but a finished split: its two halves stand in for it.
        split = [stem_path(stems_dir, n) for n in EXTRA_STEM_NAMES]
        if all(p.is_file() for p in split):
            present += split
    return present


def _signature(paths: list[Path]) -> dict[str, list[int]]:
    out = {}
    for p in paths:
        st = p.stat()
        out[p.name] = [st.st_size, st.st_mtime_ns]
    return out


def _fresh(out: Path, meta: Path, paths: list[Path]) -> bool:
    if not out.is_file() or not meta.is_file():
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("sources") == _signature(paths)
    except (OSError, ValueError):
        return False


def _lock(job_id: str, name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault((job_id, name), threading.Lock())


def forget_job(job_id: str) -> None:
    """A deleted job's sums and locks go with it. A lock still held (a build
    or a request in flight) stays, and a file that build finishes stays until
    the budget evicts it."""
    if not JOB_ID_RE.match(job_id):
        return
    with _locks_guard:
        for key, lock in list(_locks.items()):
            if key[0] == job_id and not lock.locked():
                del _locks[key]
    shutil.rmtree(CACHE_ROOT / job_id, ignore_errors=True)


def _mix(paths: list[Path], out: Path) -> float:
    """Sum `paths` into `out` (16-bit PCM). Returns the gain applied (1.0
    unless the sum passed full scale). Two passes over the files, a block at
    a time: the peak, then the write."""
    infos = [sf.info(str(p)) for p in paths]
    rate = infos[0].samplerate
    channels = max(i.channels for i in infos)
    if any(i.samplerate != rate for i in infos):
        raise BackingError("stems at different sample rates")
    frames = max(i.frames for i in infos)

    def blocks():
        handles = [sf.SoundFile(str(p)) for p in paths]
        try:
            done = 0
            while done < frames:
                n = min(_BLOCK, frames - done)
                acc = np.zeros((n, channels), dtype=np.float64)
                for h in handles:
                    data = h.read(n, dtype="float64", always_2d=True)
                    if data.shape[1] == 1 and channels > 1:
                        data = np.repeat(data, channels, axis=1)
                    acc[: data.shape[0]] += data
                yield acc
                done += n
        finally:
            for h in handles:
                h.close()

    peak = 0.0
    for acc in blocks():
        peak = max(peak, float(np.max(np.abs(acc))) if acc.size else 0.0)
    gain = 1.0 if peak <= _PEAK_CEILING else _PEAK_CEILING / peak
    with sf.SoundFile(
        str(out), "w", samplerate=rate, channels=channels, subtype="PCM_16", format="WAV"
    ) as w:
        for acc in blocks():
            w.write(acc * gain if gain != 1.0 else acc)
    return gain


def ensure_backing(job_id: str, job_dir: Path, remove: str) -> tuple[Path, bool]:
    """The backing without `remove` for the job in `job_dir`, built when it is
    missing or stale. Returns (path, built). Blocking; one build per (job,
    stem) at a time. Raises ValueError for an unknown stem, BackingError when
    there is nothing to mix."""
    stems_dir = job_dir / "stems"
    paths = backing_inputs(stems_dir, remove)
    if not paths:
        raise BackingError("no stems to mix")
    out = backing_path(job_id, remove)
    built = _ensure_sum(job_id, job_dir, out, paths, {"removed": remove})
    return out, built


def _ensure_sum(job_id: str, job_dir: Path, out: Path, paths: list[Path], meta_extra: dict) -> bool:
    """Build `out` as the sum of `paths` unless it is fresh, then hold the
    cache to its budget. Returns whether it was built. One build per file at a
    time: a second caller waits for the first and then finds the file. A
    fresh file is touched, so the budget counts it as just used."""
    meta = out.with_suffix(".json")
    with _lock(job_id, out.name):
        if _fresh(out, meta, paths):
            try:
                os.utime(out)
            except OSError:
                logger.debug("backing: could not touch %s", out.name, exc_info=True)
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp.wav")
        try:
            gain = _mix(paths, tmp)
            os.replace(tmp, out)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackingError("the backing could not be mixed") from exc
        finally:
            tmp.unlink(missing_ok=True)
        meta.write_text(
            json.dumps({**meta_extra, "sources": _signature(paths), "gain": gain}),
            encoding="utf-8",
        )
        if gain != 1.0:
            logger.info("[%s] %s scaled by %.4f to stay unclipped", job_id, out.name, gain)
    _drop_from_song_folder(job_dir)
    _prune_cache(keep=out)
    return True


def _drop_from_song_folder(job_dir: Path) -> None:
    """Builds before the cache kept these sums in the song folder. Once the
    job has a sum in the cache, those copies are only disk space."""
    for pattern in (f"{_BACKING_PREFIX}*", f"{REST_PREFIX}*"):
        for old in job_dir.glob(pattern):
            try:
                old.unlink()
            except OSError:
                logger.debug("backing: old %s still in use", old.name, exc_info=True)


def _prune_cache(keep: Path) -> None:
    """Hold every job's sums under CACHE_MAX_BYTES, least recently used first,
    after a build. Never evicted: `keep` (just built, about to be served, even
    on a volume whose clock disagrees with ours), a file used in the last
    _IN_USE_SEC, and a file whose lock a build or a request holds. A file
    Windows refuses to delete (still being read) is left for the next prune;
    so is what stays over budget."""
    with _prune_guard:
        entries = []
        for p in CACHE_ROOT.glob("*/*.wav"):
            # Only the sums this module wrote: not a build in progress (".")
            # and nothing else a misplaced STEMDECK_CACHE_DIR may hold.
            if not JOB_ID_RE.match(p.parent.name) or not p.name.startswith(
                (_BACKING_PREFIX, REST_PREFIX)
            ):
                continue
            try:
                st = p.stat()
            except OSError:
                continue  # evicted or deleted since the scan
            entries.append((st.st_mtime, st.st_size, p))
        total = sum(size for _, size, _ in entries)
        entries.sort()
        in_use = time.time() - _IN_USE_SEC
        for mtime, size, p in entries:
            if total <= CACHE_MAX_BYTES:
                break
            if p == keep or mtime > in_use:
                continue
            lock = _lock(p.parent.name, p.name)
            if not lock.acquire(blocking=False):
                continue
            try:
                # Asked for since the scan: it counts as in use now.
                if p.stat().st_mtime > in_use:
                    continue
                p.unlink()
                total -= size
                p.with_suffix(".json").unlink(missing_ok=True)
            except OSError:
                logger.debug("backing prune: %s still in use", p.name, exc_info=True)
            finally:
                lock.release()


# ── The rest of the song, for any set of stems ──
#
# A studio mode (Practice, Advanced, Karaoke) chooses which stems are lanes of
# their own; its Original is the sum of every other stem. `rest_<set>.wav` is
# that sum for one set (base stems, sorted, joined by "-"), with the same
# signature, lock, peak guard and cache budget as the backing. The engine plays
# Original from the stem files themselves; this file is for what needs one file
# (the streaming engine, the lane's download, a full-decode waveform).

REST_PREFIX = "rest_"
_SET_SEP = "-"


def parse_rest_set(raw: str) -> tuple[str, ...]:
    """The stems of a rest set as spelled in its URL and file name: base stems
    joined by "-", sorted, each once, 1 to 6 of them. Anything else raises
    ValueError, so one set has exactly one spelling and one file."""
    names = tuple(raw.split(_SET_SEP)) if raw else ()
    if not 1 <= len(names) <= len(STEM_NAMES):
        raise ValueError("a rest set holds 1 to 6 stems")
    if any(n not in STEM_NAMES for n in names):
        raise ValueError("unknown stem in the set")
    if len(set(names)) != len(names) or list(names) != sorted(names):
        raise ValueError("a rest set is sorted and names each stem once")
    return names


def rest_name(names: tuple[str, ...] | list[str]) -> str:
    """The canonical spelling of a set of stems."""
    return _SET_SEP.join(sorted(set(names)))


def rest_path(job_id: str, names: tuple[str, ...]) -> Path:
    return CACHE_ROOT / job_id / f"{REST_PREFIX}{rest_name(names)}.wav"


def rest_inputs(stems_dir: Path, names: tuple[str, ...]) -> list[Path]:
    """The stem files the rest of `names` sums. Every named stem must be on
    disk (a missing one raises BackingError: the sum would be another song),
    except Voice, whose lead/backing halves stand in for a missing vocals.wav
    as they do for the backing. The split halves are never added otherwise:
    vocals.wav already holds them."""
    paths: list[Path] = []
    for name in names:
        p = stem_path(stems_dir, name)
        if p.is_file():
            paths.append(p)
            continue
        split = [stem_path(stems_dir, n) for n in EXTRA_STEM_NAMES]
        if name == "vocals" and all(s.is_file() for s in split):
            paths += split
            continue
        raise BackingError(f"stem {name} is missing")
    return paths


def ensure_rest(job_id: str, job_dir: Path, names: tuple[str, ...]) -> tuple[Path, bool]:
    """The sum of the stems `names` for the job in `job_dir`, built when it is
    missing or stale. Returns (path, built). Blocking. Raises ValueError for a
    bad set and BackingError when a stem is missing or the mix fails."""
    names = parse_rest_set(rest_name(names))
    paths = rest_inputs(job_dir / "stems", names)
    out = rest_path(job_id, names)
    built = _ensure_sum(job_id, job_dir, out, paths, {"names": list(names)})
    return out, built
