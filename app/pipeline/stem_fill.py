"""Separate only the stems a song does not have yet (#804).

Extract on a song that is already in the library never separates it over
again: the stems it has are kept as they are, byte for byte, and only a stem it
is missing is made, with the same model and quality the song was separated
with, so the new stem adds up with the old ones to the original. A stem that
appears on disk while this runs is never replaced either.

The run works on a stand-in Job with the song's id, so the song itself stays
"done" with its diagnostics (device, timings, progress) untouched while it
plays; only its stem list, presence and peaks gain the new stems. Everything
the separation writes goes into a scratch folder inside the song's folder,
removed afterwards whatever happens.

Blocking: the caller runs it with asyncio.to_thread under _pipeline_lock.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from app.core.config import DEMUCS_MODEL, STEM_NAMES, TIMEOUT_FFMPEG, ffmpeg_executable
from app.core.models import Job
from app.core.stem_files import stem_exists, stem_url
from app.pipeline.cancel import run_registered
from app.pipeline.collect import merge_stem_peaks, presence_for_split
from app.pipeline.download import download
from app.pipeline.separate import separate
from app.pipeline.stem_storage import store_split_stems

logger = logging.getLogger("stemdeck.stem_fill")

# The quality a song separated before it was recorded is filled with: the
# default, which is what every song had before "best" was offered.
FALLBACK_QUALITY = "standard"


class FillRefused(Exception):
    """The song cannot be filled as asked. `code` is a fixed word the API
    turns into a safe message: "no_source", "model"."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def missing_stems(stems_dir: Path, wanted: list[str]) -> list[str]:
    """The base stems in `wanted` with no file in `stems_dir`, in order."""
    return [n for n in STEM_NAMES if n in wanted and not stem_exists(stems_dir, n)]


def _as_wav(stand_in: Job, source: Path, work: Path) -> Path:
    """`source` itself when it is a WAV, else a 44.1 kHz 16-bit WAV of it in
    `work`. The song's own source is only read, never moved or deleted."""
    if source.suffix.lower() == ".wav":
        return source
    dest = work / "source.wav"
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ar",
        "44100",
        "-ac",
        "2",
        "-sample_fmt",
        "s16",
        "-y",
        str(dest),
    ]
    result = run_registered(stand_in, cmd, TIMEOUT_FFMPEG)
    if result.returncode != 0 or not dest.is_file():
        raise RuntimeError("could not prepare the source for separation")
    return dest


def _place(src: Path, dest: Path) -> bool:
    """Move a new stem into place unless a file is already there. The move is
    a rename inside the song's folder, so the stem appears whole or not at
    all. Returns whether it was placed."""
    if not src.is_file() or dest.exists():
        return False
    tmp = dest.with_name(f".{dest.stem}.{uuid.uuid4().hex}.fill.wav")
    try:
        shutil.move(str(src), tmp)
        if dest.exists():
            return False
        os.replace(tmp, dest)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def fill_stems(job: Job, job_dir: Path, wanted: list[str], source: Path | None) -> list[str]:
    """Separate the stems of `wanted` the song has no file for and add them.

    `source`: the upload kept beside the song, or None for a link, which is
    downloaded again. Returns the names added (empty when none was missing).
    Raises FillRefused when the song cannot be filled the way it was
    separated, and any separation error as it is."""
    stems_dir = job_dir / "stems"
    missing = missing_stems(stems_dir, wanted)
    if not missing:
        return []
    model = job.separation_model or DEMUCS_MODEL
    if model != DEMUCS_MODEL:
        # A different model's stems would not add up with these.
        raise FillRefused("model")
    url = None if source is not None else (job.source_url or "")
    if source is None and (not url or url.startswith("local:")):
        raise FillRefused("no_source")

    stand_in = Job(id=job.id, source_url=job.source_url, title=job.title)
    work = job_dir / f".fill-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    added: list[str] = []
    try:
        src = download(stand_in, url, work) if source is None else source
        src = _as_wav(stand_in, src, work)
        quality = job.separation_quality or FALLBACK_QUALITY
        stems_root = separate(stand_in, src, work, quality=quality)
        for name in missing:
            if _place(stems_root / f"{name}.wav", stems_dir / f"{name}.wav"):
                added.append(name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not added:
        return []

    # The peaks are read from the new WAVs first, with one stem whose presence
    # is known so the new ones land on the same scale; then they are kept the
    # way the song keeps its stems (#783). Neither raises.
    reference = next((n for n in (job.stem_presence or {}) if stem_exists(stems_dir, n)), None)
    scan = [*([reference] if reference and reference not in added else []), *added]
    rms_values = merge_stem_peaks(stems_dir, scan)
    store_split_stems(job, stems_dir, added)
    if reference:
        extra = presence_for_split(rms_values, job.stem_presence, reference=reference)
        if extra:
            job.stem_presence = {**(job.stem_presence or {}), **extra}
    known = {s.get("name") for s in job.stems}
    for name in added:
        if name not in known:
            job.stems.append({"name": name, "url": stem_url(job.id, stems_dir, name)})
    logger.info("[%s] filled missing stems: %s", job.id, ", ".join(added))
    return added
