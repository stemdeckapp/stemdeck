"""Separate one stem of a finished song again, with the model the person
chose for it, and undo that (POST .../reextract, .../reextract/undo).

The stem is replaced in place, in the format the song keeps its stems in
(WAV or MP3), and the file it replaces is kept as that stem's undo copy until
the person keeps the new one (POST .../reextract/keep) or re-extracts that
stem again. One undo copy per stem, in <job>/stem-undo/<stem>-<hex>/.

The separation always runs on the song's original audio: the upload kept
beside it, or its link fetched again, as a fill does (stem_fill.py). Never on
the stems it has. The run is a plan of its own (run_plan): the chosen model
for the stem, and as few other passes as a plan can have, keeping the
RoFormer passes of the song's own plan when the chosen model is a Demucs one,
because a Demucs pass hears the mix minus those stems in a full separation
too. Only the chosen stem is taken from it. A model the person added that
cannot run is stood in for by HT Demucs 6 stems on the same device, as for an
import, and the song records it (stem_model_fallbacks).

"other" is the residual, so the six stems still add up to the song: with
delta = new X - old X, "other" becomes old other - delta, and the backing
track ("original") and the selected mix ("mix") gain delta for X and lose it
for "other" when only one of the two is in them. The other stems are never
touched, so the levels a person is mixing stay as they are, with one
exception required for the sum to hold without clipping: when the new X or
the new "other" would peak over the ceiling every separation writes under
(stem_plan.CLAMP_CEILING), every stem file of the song is turned down by the
same factor, the shared-scale rule of a separation. Job.stem_gain keeps the
product of those factors, so an undo copy made before is put back at the
level the song has now.

The new X is put on the scale of the song's stems first. The worker divides
every stem by a scale of its own run (stem_plan.shared_scale), which is not
recorded, so the ratio of the two runs' scales is measured: the sum of the
new run's stems is the mix over its scale, the sum of the song's stems the
mix over theirs, and a least-squares fit of one onto the other gives the
ratio. The same fit says whether the audio fetched again is still the song's:
a link that serves another cut or master now is refused (source_changed)
rather than leaving the old stem's sound in "other".

Every file is computed in a work folder inside the song's folder, encoded and
checked like the storage stage does (stem_storage.verify_mp3) when the song
keeps MP3s, put on the disk, and only then renamed into place, one rename per
file, so no stem is ever half-written. A failure or a cancel before the
renames leaves the song as it was.

Not done again: the beat grid (also when drums change: the beats are the
song's, and the grid the person may have edited and practised with must not
move under them; beats.json is also served immutable), the key, the sections
and the loudness (the sum is the same song). Lead + Backing is made from the
vocals, so a vocals re-extract moves the pair into the undo copy and the song
can be split again; its undo brings the pair back.

Blocking: the caller runs it with asyncio.to_thread, holding _pipeline_lock.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import stem_models
from app.core.config import DEMUCS_MODEL, EXTRA_STEM_NAMES, STEM_NAMES
from app.core.models import Job, JobCancelled, _set
from app.core.online_sources import OnlineSourcesOff
from app.core.registry import refresh_stem_urls, stem_revision
from app.core.settings import fsync_dir
from app.core.stem_files import stem_exists, stem_path, stem_url
from app.core.stem_models import SELECTABLE_STEMS
from app.pipeline import stem_plan
from app.pipeline.cancel import check_cancel
from app.pipeline.collect import merge_stem_peaks, presence_for_split
from app.pipeline.download import download
from app.pipeline.separate import separate
from app.pipeline.stem_fill import FALLBACK_QUALITY, _as_wav
from app.pipeline.stem_storage import _correlation, _encode, _fsync, verify_mp3

logger = logging.getLogger("stemdeck.stem_reextract")

# The folder of a song that holds its undo copies.
UNDO_DIRNAME = "stem-undo"
# One undo copy's folder and file, as a record names them. Checked on every
# read: a record is a file on disk.
_UNDO_DIR_RE = re.compile(r"^(vocals|drums|bass|guitar|piano)-[0-9a-f]{8}\Z")
_UNDO_FILE_RE = re.compile(
    r"^(vocals|drums|bass|guitar|piano|lead_vocals|backing_vocals)\.(wav|mp3)\Z"
)

# Frames read at a time: about 3 s of audio, a few MB per file.
_BLOCK = 1 << 17
# int16 full scale, and the ceiling a separation writes every stem under.
_FULL = 32768.0
_LIMIT = math.floor(stem_plan.CLAMP_CEILING * _FULL)
# The audio fetched again is the song's when the song's stems are this close
# to the new run's (residual energy over the song's, after the scale fit).
# The same audio gives about 1e-7 (16-bit rounding), stems kept as 128 kbps
# MP3 about 1e-2; another master or cut gives 0.3 and more.
_MAX_RESIDUAL = 0.1
# And the loudest block lines up at lag 0 (stem_storage's alignment check,
# stricter: a one-sample shift leaves the old stem audible in "other").
_ALIGN_MAX_LAG = 4096
_ALIGN_MIN_CORR = 0.5
_ALIGN_MIN_RATIO = 0.999
# The scales of two runs are either the same (both 1, nothing clipped) or
# differ by more than this; within it the ratio is taken as exactly 1 so a
# WAV song's arithmetic stays exact.
_RATIO_SNAP = 1e-3
# The new run may be this many frames longer or shorter than the song (a link
# fetched again): the stem is cut or padded with silence to the song.
_MAX_LENGTH_DIFF = 44100

PHASES = ("waiting", "downloading", "separating", "finishing")

# The stand-in of each run on now, by song id, so POST .../cancel reaches it.
_STAND_INS: dict[str, Job] = {}
_STAND_INS_LOCK = threading.Lock()


class ReextractRefused(Exception):
    """The stem cannot be re-extracted or undone as asked. `code` is a fixed
    word the API answers with: see docs/api.md."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- What may run


def check_model(stem: str, model_id: str) -> None:
    """Raise ReextractRefused unless ``model_id`` may separate ``stem`` now:
    "stem_not_selectable", "model_not_allowed", "model_removed" or
    "model_unavailable"."""
    if stem not in SELECTABLE_STEMS:
        raise ReextractRefused("stem_not_selectable")
    if stem_models.is_user_model(model_id):
        entry = stem_models.user_model_entry(model_id)
        if entry is None:
            raise ReextractRefused("model_removed")
        declared = entry.get("stems")
        if stem_models.deployment_pinned() or not (
            isinstance(declared, (list, tuple)) and stem in declared
        ):
            raise ReextractRefused("model_not_allowed")
        if not stem_models.user_model_usable(model_id):
            raise ReextractRefused("model_unavailable")
        return
    if model_id != stem_models.BASE and model_id not in stem_models.CHOICES.get(stem, ()):
        raise ReextractRefused("model_not_allowed")
    if model_id in stem_models.MODELS and not stem_models.model_available(model_id):
        raise ReextractRefused("model_unavailable")


def _makes(model_id: str, stem: str) -> bool:
    return model_id == stem_models.BASE or model_id in stem_models.choices_for(stem)


def recorded_plan(job: Job) -> dict[str, str]:
    """The plan the song's stems were made with now, every selectable stem
    named. A song separated before plans existed, or one whose record does
    not read as a plan, was made with the base model alone."""
    base = job.separation_model or DEMUCS_MODEL
    try:
        return stem_plan.check_plan(job.stem_models, base=base, allow_gone=True)
    except ValueError:
        return stem_plan.base_plan(base)


def run_plan(recorded: dict[str, str], stem: str, model_id: str) -> dict[str, str]:
    """The plan the separation runs to make ``stem`` with ``model_id``.

    Every selectable stem must be named (the worker's check_plan), so the
    other stems go to ``model_id`` when it makes them and to the base model
    otherwise: at most two passes. A Demucs model hears the rest, the mix
    minus the RoFormer stems of the plan, in a full separation; the song's own
    RoFormer passes that can still run are kept for that, so the stem is the
    one an import with this choice would have made."""
    demucs = stem_models.engine_of(model_id) == "demucs"
    plan: dict[str, str] = {}
    for s in SELECTABLE_STEMS:
        if s == stem:
            plan[s] = model_id
            continue
        kept = recorded.get(s)
        if (
            demucs
            and isinstance(kept, str)
            and stem_models.engine_of(kept) != "demucs"
            and kept in stem_models.choices_for(s)
            and stem_models.model_available(kept)
        ):
            plan[s] = kept
        elif _makes(model_id, s):
            plan[s] = model_id
        else:
            plan[s] = stem_models.BASE
    return stem_plan.check_plan(plan, base=stem_models.BASE)


def base_stems_on_disk(stems_dir: Path) -> list[str]:
    return [n for n in STEM_NAMES if stem_exists(stems_dir, n)]


def check_stems(stems_dir: Path, stem: str) -> None:
    """ReextractRefused("stems_missing") unless the song has the stem and
    "other", the two files every re-extract rewrites."""
    if not stem_exists(stems_dir, stem) or not stem_exists(stems_dir, "other"):
        raise ReextractRefused("stems_missing")


# -- Running and cancelling


def cancel(job_id: str) -> bool:
    """Ask the running re-extract or undo of `job_id` to stop. True when one
    was running."""
    with _STAND_INS_LOCK:
        stand_in = _STAND_INS.get(job_id)
        if stand_in is None:
            return False
        stand_in.cancel_requested = True
        return True


def start_state(stem: str, model_id: str | None, action: str) -> dict[str, Any]:
    return {
        "stem": stem,
        "model": model_id,
        "action": action,
        "state": "running",
        "phase": "waiting",
        "progress": 0.0,
        "code": None,
        "used_model": None,
        "fallback": None,
        "gain_db": None,
        "started_at": time.time(),
        "finished_at": None,
    }


def _update(job: Job, **fields: Any) -> None:
    """A new dict every time, so a reader holding the old one never sees it
    change, and the version bump the event stream watches."""
    _set(job, stem_reextract={**(job.stem_reextract or {}), **fields})


def _finish(job: Job, state: str, code: str | None = None, **fields: Any) -> None:
    _update(
        job,
        state=state,
        phase=None,
        code=code,
        finished_at=time.time(),
        **({"progress": 1.0} if state == "done" else {}),
        **fields,
    )


class _Mirror:
    """Copies the stand-in's progress onto the song's run record while the
    separation runs, so the page sees it through the song's own state and
    event stream. The stand-in keeps the song's own progress untouched."""

    def __init__(self, job: Job, stand_in: Job, has_download: bool) -> None:
        self.job = job
        self.stand_in = stand_in
        self.has_download = has_download
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _Mirror:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        seen = -1
        while not self.stop.wait(0.25):
            stand_in = self.stand_in
            if stand_in.version == seen:
                continue
            seen = stand_in.version
            if stand_in.status == "downloading":
                phase, progress = "downloading", 0.1 * stand_in.progress
            elif stand_in.status == "separating":
                start = 0.1 if self.has_download else 0.0
                phase, progress = "separating", start + (0.9 - start) * stand_in.progress
            else:
                continue
            _update(self.job, phase=phase, progress=round(max(0.0, min(1.0, progress)), 4))


def _new_stand_in(job: Job) -> Job:
    with _STAND_INS_LOCK:
        stand_in = Job(id=job.id, source_url=job.source_url, title=job.title)
        _STAND_INS[job.id] = stand_in
    return stand_in


def _drop_stand_in(job: Job) -> None:
    with _STAND_INS_LOCK:
        _STAND_INS.pop(job.id, None)


def _work_dir(job_dir: Path) -> Path:
    # The fill's prefix: the startup sweep removes what a killed run left.
    work = job_dir / f".fill-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    return work


def reextract_stem(job: Job, job_dir: Path, stem: str, model_id: str, source: Path | None) -> None:
    """Separate ``stem`` again with ``model_id`` and swap it in. ``source``:
    the upload kept beside the song, or None to fetch its link again. Never
    raises: the outcome is in job.stem_reextract."""
    stems_dir = job_dir / "stems"
    if not job.stem_reextract_running():
        _set(job, stem_reextract=start_state(stem, model_id, "reextract"))
    stand_in = _new_stand_in(job)
    work: Path | None = None
    try:
        check_model(stem, model_id)
        check_stems(stems_dir, stem)
        url = None if source is not None else (job.source_url or "")
        if source is None and (not url or url.startswith("local:")):
            raise ReextractRefused("no_source")
        recorded = recorded_plan(job)
        plan = run_plan(recorded, stem, model_id)
        quality = job.separation_quality or FALLBACK_QUALITY
        check_cancel(stand_in)
        work = _work_dir(job_dir)
        with _Mirror(job, stand_in, has_download=source is None):
            src = download(stand_in, url, work) if source is None else source
            src = _as_wav(stand_in, src, work)
            check_cancel(stand_in)
            stems_root = separate(stand_in, src, work, quality=quality, plan=plan, stand_in=True)
        check_cancel(stand_in)
        _update(job, phase="finishing", progress=0.9)
        used = (stand_in.stem_models or {}).get(stem, model_id)
        fallback = (stand_in.stem_model_fallbacks or {}).get(stem)
        new_x = stems_root / f"{stem}.wav"
        if not new_x.is_file():
            raise RuntimeError("the separation made no file for the stem")
        old = base_stems_on_disk(stems_dir)
        new = [n for n in STEM_NAMES if (stems_root / f"{n}.wav").is_file()]
        if old != new:
            # A song missing a base stem cannot be checked against the mix.
            raise ReextractRefused("stems_missing")
        ratio = fit_scale(
            [stem_path(stems_dir, n) for n in old], [stems_root / f"{n}.wav" for n in new]
        )
        entry = {
            "model": (job.stem_models or recorded).get(stem, recorded[stem]),
            "fallback": (job.stem_model_fallbacks or {}).get(stem),
            "saved_at": time.time(),
        }
        result = _apply(
            job,
            job_dir,
            stand_in,
            work,
            stem,
            NewStem(new_x, ratio),
            save_undo=entry,
        )
        _record_model(job, stem, used, fallback)
        logger.info(
            "[%s] re-extracted %s with %s%s, scale ratio %.5f, gain %.5f",
            job.id,
            stem,
            used,
            f" (stood in for {model_id})" if used != model_id else "",
            ratio,
            result.gain,
        )
        _finish(
            job,
            "done",
            used_model=used,
            fallback=dict(fallback) if isinstance(fallback, dict) else None,
            gain_db=_gain_db(result.gain),
        )
    except ReextractRefused as e:
        logger.info("[%s] re-extract of %s refused: %s", job.id, stem, e.code)
        _finish(job, "error", e.code)
    except OnlineSourcesOff:
        _finish(job, "error", "online_sources_off")
    except JobCancelled:
        _finish(job, "cancelled")
    except Exception:
        if stand_in.cancel_requested:
            _finish(job, "cancelled")
        else:
            logger.exception("[%s] re-extract of %s failed", job.id, stem)
            _finish(job, "error", "failed")
    finally:
        _drop_stand_in(job)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def undo_stem(job: Job, job_dir: Path, stem: str) -> None:
    """Put the undo copy of ``stem`` back and rebuild "other" around it.
    Raises ReextractRefused("no_undo") before anything is written, any write
    error as it is (the song is then as it was); the run record says the
    outcome either way."""
    if not job.stem_reextract_running():
        record = (job.stem_undo or {}).get(stem)
        model = record.get("model") if isinstance(record, dict) else None
        _set(job, stem_reextract=start_state(stem, model, "undo"))
    stand_in = _new_stand_in(job)
    work: Path | None = None
    try:
        entry = undo_entry(job, job_dir, stem)
        if entry is None:
            raise ReextractRefused("no_undo")
        check_stems(job_dir / "stems", stem)
        work = _work_dir(job_dir)
        _update(job, phase="finishing", progress=0.5)
        saved_gain = _gain_of(entry.record.get("gain"))
        factor = _gain_of(job.stem_gain) / saved_gain
        if abs(factor - 1.0) < 1e-9:
            factor = 1.0
        result = _apply(
            job, job_dir, stand_in, work, stem, NewStem(entry.file, factor), restore=entry
        )
        model = entry.record.get("model")
        fallback = entry.record.get("fallback")
        _record_model(job, stem, model if isinstance(model, str) else None, fallback)
        logger.info("[%s] undid the re-extract of %s", job.id, stem)
        _finish(job, "done", used_model=model, gain_db=_gain_db(result.gain))
    except ReextractRefused as e:
        _finish(job, "error", e.code)
        raise
    except Exception:
        logger.exception("[%s] undo of the re-extract of %s failed", job.id, stem)
        _finish(job, "error", "failed")
        raise
    finally:
        _drop_stand_in(job)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def keep_stem(job: Job, job_dir: Path, stem: str) -> None:
    """Drop the undo copy of ``stem``: the re-extracted one stays."""
    entry = undo_entry(job, job_dir, stem)
    if entry is None:
        raise ReextractRefused("no_undo")
    _forget_undo(job, job_dir, stem)


def _gain_db(gain: float) -> float | None:
    return None if gain == 1.0 else round(20 * math.log10(gain), 2)


def _gain_of(value: object) -> float:
    """A recorded gain, or 1.0 for none or a damaged one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    return float(value) if 0 < value <= 1 and math.isfinite(value) else 1.0


def _record_model(job: Job, stem: str, model_id: str | None, fallback: object) -> None:
    plan = dict(job.stem_models or recorded_plan(job))
    if model_id:
        plan[stem] = model_id
    fallbacks = dict(job.stem_model_fallbacks or {})
    fallbacks.pop(stem, None)
    if isinstance(fallback, dict):
        fallbacks[stem] = dict(fallback)
    _set(job, stem_models=plan, stem_model_fallbacks=fallbacks or None)


# -- Undo copies


@dataclass(frozen=True)
class UndoEntry:
    record: dict[str, Any]
    folder: Path
    file: Path
    extras: tuple[Path, ...]


def undo_entry(job: Job, job_dir: Path, stem: str) -> UndoEntry | None:
    """The undo copy of ``stem`` when its record and its file are both
    there, every name in the record checked, else None."""
    undo = job.stem_undo if isinstance(job.stem_undo, dict) else {}
    record = undo.get(stem)
    if stem not in SELECTABLE_STEMS or not isinstance(record, dict):
        return None
    folder_name, file_name = record.get("dir"), record.get("file")
    if not (isinstance(folder_name, str) and _UNDO_DIR_RE.match(folder_name)):
        return None
    if not (isinstance(file_name, str) and _UNDO_FILE_RE.match(file_name)):
        return None
    if file_name.rsplit(".", 1)[0] != stem or not folder_name.startswith(f"{stem}-"):
        return None
    root = (job_dir / UNDO_DIRNAME).resolve()
    folder = (root / folder_name).resolve()
    path = (folder / file_name).resolve()
    if not folder.is_relative_to(root) or not path.is_relative_to(folder) or not path.is_file():
        return None
    extras: list[Path] = []
    raw = record.get("extras")
    for name in raw if isinstance(raw, list) else []:
        if not (isinstance(name, str) and _UNDO_FILE_RE.match(name)):
            return None
        if name.rsplit(".", 1)[0] not in EXTRA_STEM_NAMES:
            return None
        extra = (folder / name).resolve()
        if not extra.is_relative_to(folder) or not extra.is_file():
            return None
        extras.append(extra)
    return UndoEntry(record=record, folder=folder, file=path, extras=tuple(extras))


def _forget_undo(job: Job, job_dir: Path, stem: str) -> None:
    undo = dict(job.stem_undo or {})
    record = undo.pop(stem, None)
    _set(job, stem_undo=undo or None)
    folder_name = record.get("dir") if isinstance(record, dict) else None
    if isinstance(folder_name, str) and _UNDO_DIR_RE.match(folder_name):
        root = (job_dir / UNDO_DIRNAME).resolve()
        folder = (root / folder_name).resolve()
        if folder.is_relative_to(root) and folder != root:
            shutil.rmtree(folder, ignore_errors=True)
    root = job_dir / UNDO_DIRNAME
    try:
        root.rmdir()  # only when empty
    except OSError:
        pass


def _keep_file(src: Path, dest: Path) -> None:
    """``dest`` holds ``src``'s bytes, on the disk, and ``src`` stays: a hard
    link, or a copy where the disk has none."""
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)
        _fsync(dest)


# -- The scale of the new run


def fit_scale(old: list[Path], new: list[Path]) -> float:
    """The factor that puts the new run's stems on the scale of the song's:
    the least-squares fit of the sum of ``new`` onto the sum of ``old``.
    ReextractRefused("source_changed") when the two sums are not the same
    audio (another length, a residual over _MAX_RESIDUAL, a lag)."""
    import numpy as np
    import soundfile as sf

    old_info = [sf.info(str(p)) for p in old]
    new_info = [sf.info(str(p)) for p in new]
    shapes = {(i.samplerate, i.channels) for i in (*old_info, *new_info)}
    if len(shapes) != 1:
        raise ReextractRefused("source_changed")
    old_frames, new_frames = old_info[0].frames, new_info[0].frames
    if abs(old_frames - new_frames) > _MAX_LENGTH_DIFF:
        raise ReextractRefused("source_changed")
    so = oo = nn = 0.0
    best_energy, block_a, block_b = -1.0, None, None
    for old_block, new_block in zip(_sum_blocks(old), _sum_blocks(new), strict=False):
        n = min(len(old_block), len(new_block))
        a, b = old_block[:n], new_block[:n]
        so += float(np.sum(a * b))
        oo += float(np.sum(a * a))
        nn += float(np.sum(b * b))
        mono_a, mono_b = a.sum(axis=1), b.sum(axis=1)
        energy = float(np.sum(mono_a * mono_a))
        if energy > best_energy:
            best_energy, block_a, block_b = energy, mono_a, mono_b
    if oo <= 0 or nn <= 0:
        return 1.0  # silence: nothing to fit, and nothing to hear
    ratio = so / nn
    residual = max(0.0, oo - 2 * ratio * so + ratio * ratio * nn) / oo
    if not (0.25 <= ratio <= 4.0) or residual > _MAX_RESIDUAL:
        logger.info("re-extract: ratio %.4f, residual %.4g: not the same audio", ratio, residual)
        raise ReextractRefused("source_changed")
    if block_a is not None and block_b is not None:
        zero, best, lag = _correlation(block_a, block_b, _ALIGN_MAX_LAG)
        if zero < _ALIGN_MIN_CORR or (lag != 0 and zero < _ALIGN_MIN_RATIO * best):
            logger.info("re-extract: the new run lines up at lag %d, not 0", lag)
            raise ReextractRefused("source_changed")
    return 1.0 if abs(ratio - 1.0) < _RATIO_SNAP else ratio


def _sum_blocks(paths: list[Path]) -> Iterator[Any]:
    """The sum of ``paths``, block by block, in int16 units (float64)."""
    import numpy as np
    import soundfile as sf

    readers = [sf.SoundFile(str(p)) for p in paths]
    try:
        while True:
            parts = [r.read(_BLOCK, dtype="float32", always_2d=True) for r in readers]
            n = max(len(p) for p in parts)
            if n == 0:
                return
            total = np.zeros((n, readers[0].channels), dtype=np.float64)
            for p in parts:
                total[: len(p)] += p
            yield total * _FULL
    finally:
        for r in readers:
            r.close()


# -- The swap


@dataclass(frozen=True)
class NewStem:
    """The new X: the file holding it, times ``factor``."""

    path: Path
    factor: float


@dataclass(frozen=True)
class Applied:
    gain: float
    changed: tuple[str, ...]


class _Reader:
    """One input file, read block by block in int16 units; silence after its
    end."""

    def __init__(self, path: Path, channels: int) -> None:
        import soundfile as sf

        self.file = sf.SoundFile(str(path))
        self.channels = channels

    def read(self, n: int) -> Any:
        import numpy as np

        got = self.file.read(n, dtype="float32", always_2d=True)
        out = np.zeros((n, self.channels), dtype=np.float64)
        width = min(self.channels, got.shape[1]) if got.size else 0
        if width:
            out[: len(got), :width] = got[:, :width]
        return out * _FULL

    def close(self) -> None:
        self.file.close()


def _composites(job: Job, stems_dir: Path) -> dict[str, frozenset[str]]:
    """The backing track and the selected mix the song has, each with the
    base stems summed into it (collect.make_original_track and
    make_selected_mix)."""
    selected = [s for s in job.selected_stems if s in STEM_NAMES] or list(STEM_NAMES)
    out: dict[str, frozenset[str]] = {}
    if stem_exists(stems_dir, "original"):
        out["original"] = frozenset(s for s in STEM_NAMES if s not in selected)
    if stem_exists(stems_dir, "mix"):
        out["mix"] = frozenset(selected)
    return out


def _peak(stems_dir: Path, stem: str, new: NewStem, frames: int, channels: int) -> float:
    """The largest sample of the new X and the new "other", before any gain."""
    import numpy as np

    x_old = _Reader(stem_path(stems_dir, stem), channels)
    other = _Reader(stem_path(stems_dir, "other"), channels)
    x_new = _Reader(new.path, channels)
    peak = 0.0
    try:
        done = 0
        while done < frames:
            n = min(_BLOCK, frames - done)
            xn = np.rint(x_new.read(n) * new.factor)
            o = other.read(n) - (xn - x_old.read(n))
            peak = max(peak, float(np.max(np.abs(xn))), float(np.max(np.abs(o))))
            done += n
    finally:
        for r in (x_old, other, x_new):
            r.close()
    if not math.isfinite(peak):
        raise RuntimeError("the new stem holds samples that are not numbers")
    return peak


def _apply(
    job: Job,
    job_dir: Path,
    stand_in: Job,
    work: Path,
    stem: str,
    new: NewStem,
    *,
    save_undo: dict[str, Any] | None = None,
    restore: UndoEntry | None = None,
) -> Applied:
    """Put the new ``stem`` in place with "other" (and the backing track and
    mix) rebuilt around it; see the module docstring. Exactly one of
    ``save_undo`` (a re-extract: the record of the undo copy to make of the
    stem it replaces) and ``restore`` (an undo: the copy being put back)."""
    import numpy as np
    import soundfile as sf

    stems_dir = job_dir / "stems"
    info = sf.info(str(stem_path(stems_dir, stem)))
    frames, channels, rate = info.frames, info.channels, info.samplerate
    composites = _composites(job, stems_dir)
    vocals = stem == "vocals"
    # Lead + Backing is the old vocals' split: it leaves with them, into the
    # undo copy, and an undo puts the saved pair back in place of the
    # current one.
    pair_now = [n for n in EXTRA_STEM_NAMES if stem_exists(stems_dir, n)] if vocals else []

    peak = _peak(stems_dir, stem, new, frames, channels)
    gain = 1.0 if peak <= _LIMIT else _LIMIT / (peak * (1.0 + stem_plan.CLAMP_MARGIN))
    exact = new.factor == 1.0 and gain == 1.0
    if gain == 1.0:
        names = [stem, "other"] + [
            c for c, parts in composites.items() if (stem in parts) != ("other" in parts)
        ]
        rescaled: list[str] = []
    else:
        rescaled = [
            n
            for n in (*STEM_NAMES, *EXTRA_STEM_NAMES)
            if stem_exists(stems_dir, n) and n not in (stem, "other")
        ]
        if vocals:
            rescaled = [n for n in rescaled if n not in EXTRA_STEM_NAMES]
        names = [stem, "other", *composites, *[n for n in rescaled if n not in composites]]
    restored_pair: list[tuple[str, Path]] = []
    if restore is not None:
        restored_pair = [(p.name.rsplit(".", 1)[0], p) for p in restore.extras]
    pair_factor = (new.factor if restore is not None else 1.0) * gain

    out_dir = work / "out"
    out_dir.mkdir()
    wavs: dict[str, Path] = {name: out_dir / f"{name}.wav" for name in names}
    # The saved pair is written too when it must change level; else its own
    # bytes go back.
    pair_written = pair_factor != 1.0
    if pair_written:
        for name, _src in restored_pair:
            wavs[name] = out_dir / f"{name}.wav"

    readers: dict[str, _Reader] = {}
    writers: dict[str, Any] = {}
    try:
        readers["@x_old"] = _Reader(stem_path(stems_dir, stem), channels)
        readers["@x_new"] = _Reader(new.path, channels)
        for name in names:
            if name != stem:
                readers[name] = _Reader(stem_path(stems_dir, name), channels)
        if pair_written:
            for name, src in restored_pair:
                readers[f"@pair:{name}"] = _Reader(src, channels)
        for name, path in wavs.items():
            writers[name] = sf.SoundFile(
                str(path), "w", samplerate=rate, channels=channels, subtype="PCM_16", format="WAV"
            )
        done = 0
        while done < frames:
            check_cancel(stand_in)
            n = min(_BLOCK, frames - done)
            x_new = np.rint(readers["@x_new"].read(n) * new.factor)
            delta = x_new - readers["@x_old"].read(n)
            for name in names:
                if name == stem:
                    block = x_new
                elif name == "other":
                    block = readers[name].read(n) - delta
                elif name in composites:
                    parts = composites[name]
                    block = readers[name].read(n)
                    block = block + delta * ((stem in parts) - ("other" in parts))
                else:
                    block = readers[name].read(n)
                writers[name].write(_int16(block * gain))
            if pair_written:
                for name, _src in restored_pair:
                    writers[name].write(_int16(readers[f"@pair:{name}"].read(n) * pair_factor))
            done += n
    finally:
        for r in readers.values():
            r.close()
        for w in writers.values():
            w.close()

    # Each file as the song keeps that stem: an MP3 song's files are encoded
    # and checked the way the storage stage does it, then all are on the disk.
    finals: dict[str, Path] = {}
    for name, wav in wavs.items():
        current = stem_path(stems_dir, name)
        mp3 = (
            current.suffix.lower() == ".mp3" or (not current.is_file() and job.stem_format == "mp3")
        ) and bool(job.stem_mp3_kbps)
        suffix = ".mp3" if mp3 else ".wav"
        if name == stem and exact and restore is not None and restore.file.suffix == suffix:
            # Back at the level it was saved at: its own bytes go back, never
            # a second encode of them.
            finals[name] = restore.file
        elif mp3:
            dest = out_dir / f"{name}.mp3"
            _encode(stand_in, wav, dest, int(job.stem_mp3_kbps or 0))
            verify_mp3(stand_in, wav, dest)
            _fsync(dest)
            finals[name] = dest
        else:
            _fsync(wav)
            finals[name] = wav
    check_cancel(stand_in)

    # The undo copy of the stem being replaced, made before anything moves.
    undo_folder: Path | None = None
    undo_record: dict[str, Any] | None = None
    if save_undo is not None:
        root = job_dir / UNDO_DIRNAME
        root.mkdir(exist_ok=True)
        undo_folder = root / f"{stem}-{uuid.uuid4().hex[:8]}"
        undo_folder.mkdir()
        current = stem_path(stems_dir, stem)
        _keep_file(current, undo_folder / current.name)
        undo_record = {
            **save_undo,
            "dir": undo_folder.name,
            "file": current.name,
            "gain": _gain_of(job.stem_gain),
            "extras": [stem_path(stems_dir, n).name for n in pair_now],
            "vocal_split": job.vocal_split if vocals else None,
        }
        fsync_dir(undo_folder)

    # The swap: one rename per file, each replacing the file of the same name.
    old_undo = (job.stem_undo or {}).get(stem) if save_undo is not None else None
    changed: list[str] = []
    removed: list[str] = []
    try:
        for name, final in finals.items():
            dest = stems_dir / f"{name}{final.suffix}"
            _replace(final, dest)
            changed.append(name)
            # The file it replaces under the other name: an MP3 song's old MP3
            # when the new file is a WAV, or a WAV song's download cache
            # (GET .../stems/<name>.mp3), which would otherwise outlive it.
            _drop_sibling(dest)
        if save_undo is not None and undo_folder is not None:
            for name in pair_now:
                current = stem_path(stems_dir, name)
                _replace(current, undo_folder / current.name)
                removed.append(name)
                _drop_sibling(current)
        elif restore is not None and vocals:
            # The current pair was split from the vocals going away.
            saved = {name for name, _ in restored_pair}
            for name in EXTRA_STEM_NAMES:
                if name in saved or not stem_exists(stems_dir, name):
                    continue
                for ext in (".wav", ".mp3"):
                    _unlink(stems_dir / f"{name}{ext}")
                removed.append(name)
            if not pair_written:
                for name, src in restored_pair:
                    dest = stems_dir / src.name
                    _replace(src, dest)
                    changed.append(name)
                    _drop_sibling(dest)
    except BaseException:
        if not changed and not removed and undo_folder is not None:
            # Nothing moved: the song is as it was, and so is its undo.
            shutil.rmtree(undo_folder, ignore_errors=True)
        elif changed or removed:
            # Some files moved: keep the record of what did, so the undo copy
            # stays reachable and the page loads the files on disk.
            logger.error("[%s] the swap of %s stopped part way: %s", job.id, stem, changed)
            _after_swap(job, stems_dir, stem, changed, removed, gain)
            if undo_record is not None:
                _set(job, stem_undo={**(job.stem_undo or {}), stem: undo_record})
        raise
    if vocals:
        # Made from the vocals, and kept by its time against the file: an
        # undo puts an older file back, which the time check would pass.
        _unlink(stems_dir / "vocal_envelope.json")
    fsync_dir(stems_dir)

    _after_swap(job, stems_dir, stem, changed, removed, gain)
    if vocals and save_undo is not None:
        _set(job, vocal_split="none", vocal_split_reason=None)
    elif vocals and restore is not None:
        has_pair = all(stem_exists(stems_dir, n) for n in EXTRA_STEM_NAMES)
        _set(job, vocal_split="done" if has_pair else "none", vocal_split_reason=None)
    if save_undo is not None and undo_record is not None:
        if isinstance(old_undo, dict):
            _forget_undo_folder(job_dir, old_undo)
        _set(job, stem_undo={**(job.stem_undo or {}), stem: undo_record})
    elif restore is not None:
        _forget_undo(job, job_dir, stem)
    return Applied(gain=gain, changed=tuple(changed))


# Windows refuses to replace or delete a file another handle has open: a
# stem the studio is streaming is opened for each range it asks for, briefly.
_HELD_RETRY_SEC = 10.0
_HELD_RETRY_STEP = 0.1


def _retry_held(action, *args) -> None:
    deadline = time.monotonic() + _HELD_RETRY_SEC
    while True:
        try:
            action(*args)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_HELD_RETRY_STEP)


def _replace(src: Path, dest: Path) -> None:
    _retry_held(os.replace, src, dest)


def _unlink(path: Path) -> None:
    _retry_held(lambda p: p.unlink(missing_ok=True), path)


def _drop_sibling(dest: Path) -> None:
    """Remove the file of the same stem under the other extension. Should it
    stay held past the retries, a WAV beside a cached MP3 is touched so the
    cache (which is kept while it is not older than its WAV) is made again."""
    sibling = dest.with_suffix(".wav" if dest.suffix == ".mp3" else ".mp3")
    try:
        _unlink(sibling)
    except PermissionError:
        logger.warning("could not remove %s; it is still in use", sibling.name)
        if dest.suffix == ".wav" and dest.is_file():
            os.utime(dest)


def _forget_undo_folder(job_dir: Path, record: dict[str, Any]) -> None:
    folder_name = record.get("dir")
    if isinstance(folder_name, str) and _UNDO_DIR_RE.match(folder_name):
        root = (job_dir / UNDO_DIRNAME).resolve()
        folder = (root / folder_name).resolve()
        if folder.is_relative_to(root) and folder != root:
            shutil.rmtree(folder, ignore_errors=True)


def _int16(block: Any) -> Any:
    import numpy as np

    return np.clip(np.rint(block), -_FULL, _FULL - 1).astype(np.int16)


def _after_swap(
    job: Job, stems_dir: Path, stem: str, changed: list[str], removed: list[str], gain: float
) -> None:
    """The song's record after the files moved: revisions and URLs, the
    stem list, the level, the peaks and the presence of what changed."""
    revisions = {k: v for k, v in (job.stem_revisions or {}).items() if isinstance(k, str)}
    for name in (*changed, *removed):
        revisions[name] = stem_revision(job, name) + 1
    stems = [s for s in job.stems if not (isinstance(s, dict) and s.get("name") in removed)]
    known = {s.get("name") for s in stems if isinstance(s, dict)}
    for name in changed:
        if name not in known and name in EXTRA_STEM_NAMES:
            stems.append({"name": name, "url": stem_url(job.id, stems_dir, name)})
    _set(
        job,
        stem_revisions=revisions,
        stems=stems,
        stem_gain=None if _gain_of(job.stem_gain) * gain == 1.0 else _gain_of(job.stem_gain) * gain,
    )
    refresh_stem_urls(job, stems_dir)
    presence = dict(job.stem_presence or {})
    for name in removed:
        presence.pop(name, None)
    reference = next(
        (
            n
            for n in STEM_NAMES
            if n not in (stem, "other") and n in presence and stem_exists(stems_dir, n)
        ),
        None,
    )
    scan = [*changed, *([reference] if reference and reference not in changed else [])]
    rms = merge_stem_peaks(stems_dir, scan, remove=removed or None)
    if reference:
        wanted = {stem, "other", *(n for n in changed if n in EXTRA_STEM_NAMES)}
        extra = presence_for_split(
            {k: v for k, v in rms.items() if k in wanted or k == reference},
            presence,
            reference=reference,
        )
        presence.update(extra)
    _set(job, stem_presence=presence or None)
