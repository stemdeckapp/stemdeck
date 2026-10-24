from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

import soundfile as sf
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from app.core.config import (
    CACHE_DIR,
    CLICK_STEM_NAME,
    EXTRA_STEM_NAMES,
    JOB_ID_RE,
    JOBS_DIR,
    STEM_NAMES,
    TAKE_ID_RE,
    TIMEOUT_FFMPEG,
    TRACK_ID_RE,
    ffmpeg_executable,
)
from app.core.registry import get as registry_get
from app.core.settings import get_export_sample_rate
from app.core.stem_files import stem_ext, stem_media_type, stem_path
from app.core.takes import take_dir
from app.pipeline.audio_encoders import unavailable_detail
from app.pipeline.audio_stats import vocal_envelope
from app.pipeline.click_render import (
    CLICK_TO_STEREO,
    count_in_beats,
    render_click_wav,
    render_count_in_wav,
)
from app.pipeline.click_render import cache_key as click_cache_key
from app.pipeline.click_sounds import CLICK_SOUND_IDS, DEFAULT_CLICK_SOUND
from app.pipeline.collect import fill_missing_peaks
from app.pipeline.fx_filters import (
    FX_PARAM_MAX_CHARS,
    FxError,
    FxRequest,
    check_section_budget,
    parse_fx_param,
    slot_filter,
    slot_section_count,
)
from app.pipeline.mastering import (
    MASTER_PARAM_MAX_CHARS,
    Loudness,
    MasterError,
    MasterPlan,
    MasterSettings,
    MeasureError,
    comp_ratio,
    effective_ceiling,
    measure_file,
    parse_master_param,
    plan_chain,
    plan_graph,
    run_plan,
    trim_filter,
)
from app.pipeline.take_render import (
    AAC_ENCODE_ARGS,
    AAC_ENCODE_ARGS_ALT,
    EXPORT_LIMITER_LOSSY,
    export_limiter,
)

logger = logging.getLogger("stemdeck.api")

router = APIRouter(tags=["stems"])

# Stem files served by this endpoint: the 6 demucs stems + two
# pipeline-produced extras, plus the on-demand lead/backing vocal split
# (#275, EXTRA_STEM_NAMES) when a job has requested it. "original" is the
# re-encoded source song (added when the user picked a strict subset), "mix"
# is the ffmpeg amix of the user's selected stems.
_ALLOWED_NAMES = frozenset(STEM_NAMES) | frozenset(EXTRA_STEM_NAMES) | {"original", "mix"}

# Lanes the dynamic mixdown may sum: the 6 stems plus "original" (the complement
# track shown when the user picked a subset) plus lead/backing vocals when a
# job has split them. "mix" is excluded -- it is the static pre-render this
# endpoint replaces. Gains are linear; the studio caps a lane at 2.0, so this
# generous bound just rejects abusive values.
_MIXDOWN_NAMES = frozenset(STEM_NAMES) | frozenset(EXTRA_STEM_NAMES) | {"original"}
# A recorded take is a lane too, named by its id (^t[0-9a-f]{8}$, never a stem
# name) and resolved to takes/<take_id>/audio.wav, which is already in the
# stems' format, aligned to song 0 and the song's length: export applies no
# offset and no resample to it.
_MIXDOWN_MAX_GAIN = 4.0
# lead_vocals/backing_vocals are a decomposition of vocals, not an independent
# signal -- summing vocals alongside either would double-count the vocal
# energy in the mix (#275).
_VOCAL_DECOMPOSITION_NAMES = frozenset(EXTRA_STEM_NAMES)

# Output encoders by container/extension, shared by the dynamic mixdown and the
# stems zip. WAV is lossless PCM, FLAC is lossless compressed, MP3 is VBR ~190 kbps,
# OGG is Vorbis VBR q6 (~192 kbps) — the quality tier matching the MP3 setting.
_ENCODE_ARGS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-q:a", "2"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
}
MIXDOWN_CODECS = {ext: [*args, "-f", ext] for ext, args in _ENCODE_ARGS.items()}
# Mixdowns whose encoder adds peak of its own, so they end in the true-peak
# limiter (EXPORT_LIMITER_LOSSY) rather than the bit-transparent one.
_LOSSY_MIXDOWN_EXTS = frozenset({"mp3", "ogg"})

# Containers whose muxer finishes the file by seeking back to a header it wrote
# earlier, which is impossible on a pipe (#458):
#
#   wav   RIFF and data chunk sizes, left as the 0xFFFFFFFF placeholder, so the
#         file claims ~4 GB of audio and strict hardware refuses it
#   flac  STREAMINFO total samples and the MD5 signature, left as zeros
#   mp3   the Xing/Info frame, which ffmpeg simply omits rather than writing a
#         wrong one -- and these are VBR (-q:a 2), so without it there is no
#         duration and no seek table
#
# ogg is absent on purpose: a granule position rides on every page, so nothing
# is patched afterwards. mp4 is handled separately by get_video_mixdown, which
# already muxes fragmented (frag_keyframe+empty_moov) for exactly this reason.
_SEEKABLE_OUTPUT_EXTS = frozenset({"wav", "flac", "mp3"})
MIXDOWN_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
}

# Mixdown render cache (#290): identical render params re-run the full ffmpeg
# graph on every request today. On a shared server, repeat downloads of the
# same mix (a common case -- re-downloading, or several listeners pulling the
# same export) burn CPU for no reason since the output is a pure function of
# the inputs. Bounded so a churny cache (many one-off region trims) can't
# grow unboundedly on a long-running server.
_MIXDOWN_CACHE_DIR = CACHE_DIR / "mixdown"
_CLICK_CACHE_DIR = CACHE_DIR / "click"

# The stems zip's click entry (#805) shares the zip's namespace with the stems
# and the takes. Takes are named "<label>_<take id>" or "<take id>", never a bare
# word, so only a stem could collide: refuse to start rather than overwrite one.
if CLICK_STEM_NAME in (*STEM_NAMES, *EXTRA_STEM_NAMES, "original", "mix"):
    raise RuntimeError("CLICK_STEM_NAME collides with a stem name")

CLICK_SOUND_QUERY_DESCRIPTION = (
    "Click sound preset id (static/js/clickSounds.js). Defaults to the classic click, "
    "which is what a client that predates presets sends."
)


def check_click_sound(sound: str) -> str:
    """The click sound id, validated against the preset whitelist (#805).

    The id is only ever used as a key into the preset table and a cache-key
    suffix, never in a path or a command, but it is still refused outright
    rather than defaulted: a client asking for a sound it cannot have should
    hear about it, not get the classic click without a word."""
    if sound not in CLICK_SOUND_IDS:
        raise HTTPException(status_code=422, detail="unknown click sound")
    return sound


_MIXDOWN_CACHE_MAX_FILES = 20
_MIXDOWN_CACHE_MAX_BYTES = 500 * 1024 * 1024  # 500 MB


# Folded into every cache key. Bump it whenever a render's *output* changes for
# inputs that are otherwise identical, so entries written by an older build can
# never be served by a newer one.
#
# "2": everything cached before #458 was streamed through a pipe and carries an
# unpatched container header. Those files are wrong, the key that produced them
# is still reachable, and _prune_mixdown_cache evicts by age rather than
# validity -- so without this a user who exported before upgrading would be
# handed the same broken file back for the same parameters indefinitely.
#
# "3": every mixdown now ends in the export limiter (EXPORT_LIMITER), so a mix
# that summed above -1 dBFS renders differently from the cached clipped file.
#
# "4": MP3 and OGG mixdowns now end in the true-peak limiter at -1.5 dBTP
# (EXPORT_LIMITER_LOSSY) instead of the -1 dBFS sample-peak one.
#
# "5": exports take the studio EQ (fx=), and its filters are part of the key.
#
# "6": exports take the master (master=): loudness target, glue compressor and
# true-peak limiter; the master chain with its measured gain is in the key.
_RENDER_CACHE_VERSION = "6"

# Measured masters (app/pipeline/mastering.py), one small JSON per mix and
# master setting, so the export, the region export, the "render and listen"
# excerpt and the loudness readout share one measurement of the whole song.
# The key holds every input of the measurement; bump the version when the
# plan's maths change.
_MASTER_PLAN_DIR = CACHE_DIR / "master"
_MASTER_PLAN_MAX_FILES = 200
_MASTER_PLAN_VERSION = "1"
# At most two songs measured at once on this server; one mix is never
# measured twice at the same time (the export and the readout often ask
# together).
_master_plan_slots = asyncio.Semaphore(2)
_master_plan_locks: dict[str, asyncio.Lock] = {}
# A mastered lossy export is decoded and measured; over the ceiling, it is
# rendered again with its output trimmed (_render_master_verified).
LOSSY_RETRIES = 3
LOSSY_RETRY_MARGIN_DB = 0.1
# How often a loudness request checks that its client is still there.
_DISCONNECT_POLL_SEC = 0.5


def _mixdown_cache_key(
    job_id: str,
    ext: str,
    names: list[str],
    gains: list[float],
    start: float | None,
    end: float | None,
    click_lane: tuple[Path, float] | None = None,
    pitches: list[int] | None = None,
    lane_versions: list[str] | None = None,
    fx_fragment: str = "",
    master_fragment: str = "",
    lanes_fragment: str = "",
) -> str:
    """Every render input is in the key, so a different mixer state, region,
    or a Settings change (export sample rate) misses cleanly -- never a stale
    hit. Stems are immutable once a job reaches "done", so job_id alone pins
    the underlying audio; gains are formatted to match the %.6f precision
    already used in the ffmpeg volume filter, so cosmetically-different but
    ffmpeg-equivalent gain strings (e.g. "1" vs "1.0") share a cache entry.

    `lanes_fragment` (the mode's lanes, which decide what Original holds) is
    appended only when given, so every key made before lanes= is unchanged."""
    parts = [
        _RENDER_CACHE_VERSION,
        job_id,
        ext,
        ",".join(names),
        ",".join(f"{g:.6f}" for g in gains),
        "" if start is None else f"{start:.6f}",
        "" if end is None else f"{end:.6f}",
        str(get_export_sample_rate()),
        # The click's own cache key already encodes the grid, rate and
        # accent mode, so its filename plus gain fully identifies it. ":u":
        # the click now goes to stereo at unity (#833), so a mix with a click
        # cached at the old -3 dB misses; a mix without one keeps its key.
        "" if click_lane is None else f"{click_lane[0].name}:{click_lane[1]:.6f}:u",
        # Appended only when something is actually transposed, so every
        # export made before #592 keeps the exact key it already had and
        # the cache survives the change.
        "" if not (pitches and any(pitches)) else "p" + ",".join(str(p) for p in pitches),
        # A take is not immutable like a stem: a nudge re-renders its
        # audio.wav under the same name. Its size and mtime pin the file
        # the render read, so a nudged take misses the cache.
        ",".join(lane_versions or []),
        # The EQ as the exact filter strings the render runs (RenderFx),
        # empty when no EQ changes the sound.
        fx_fragment,
        # The master chain with its measured gain, empty when off.
        master_fragment,
    ]
    if lanes_fragment:
        parts.append(lanes_fragment)
    raw = "|".join(parts)
    # Cache key, not a security context -- usedforsecurity=False documents
    # that for both readers and static analysis (bandit flags bare sha1()).
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def _prune_mixdown_cache(cache_dir: Path, keep: Path | None = None) -> None:
    """Evict oldest-first once either the file-count or total-size budget is
    exceeded. Best-effort: a failed prune just means the cache grows past
    budget until the next successful render, not a broken export.

    `keep` is never evicted, and exists because a render is added to the cache
    and then pruned before it is served. A single render larger than the whole
    budget put the directory over on its own, so the loop deleted it, newest
    and only entry though it was, and the caller handed a path that no longer
    existed to FileResponse. A 60-minute WAV crosses the 500 MB budget at about
    49.5 minutes, well inside the 60 StemDeck accepts (#482).

    Its size still counts toward the total, so an oversized entry evicts
    everything else and then stops, leaving the cache one file over budget
    until the next render clears it. That is the intended trade: a rendered
    file the user is waiting on outranks the budget.
    """
    try:
        entries = sorted(
            (
                p
                for p in cache_dir.iterdir()
                if p.is_file() and not p.name.startswith(".") and p != keep
            ),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in entries)
        if keep is not None and keep.is_file():
            total += keep.stat().st_size
    except OSError:
        return
    while entries and (len(entries) > _MIXDOWN_CACHE_MAX_FILES or total > _MIXDOWN_CACHE_MAX_BYTES):
        oldest = entries.pop(0)
        try:
            total -= oldest.stat().st_size
            oldest.unlink()
        except OSError:
            logger.debug("mixdown cache prune: could not remove %s", oldest, exc_info=True)


def _is_recorded_lane(name: str) -> bool:
    """A take id or a recording track id: lanes resolved to a take's file."""
    return bool(TAKE_ID_RE.match(name) or TRACK_ID_RE.match(name))


def _lane_take_id(job_id: str, name: str) -> str:
    """The take a recorded lane plays: a take id is itself, a track id is its
    active take. A track with no take yet is a 422, like an unknown name."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    if TRACK_ID_RE.match(name):
        track = next((t for t in job.tracks if t.get("id") == name), None)
        if track is None:
            raise HTTPException(status_code=422, detail="unknown stem requested")
        if not track.get("active_take_id"):
            raise HTTPException(status_code=422, detail="track has no take")
        return str(track["active_take_id"])
    return name


def _take_lane_path(job_id: str, name: str) -> Path:
    """A take or track lane's audio.wav, with the same guards as a stem. Only
    a finished take (one listed in job.takes) is a lane; anything else is the
    same 422 as an unknown stem name."""
    name = _lane_take_id(job_id, name)
    job = registry_get(job_id)
    if not TAKE_ID_RE.match(name) or not any(t.get("id") == name for t in job.takes):
        raise HTTPException(status_code=422, detail="unknown stem requested")
    folder = take_dir(job_id, name, JOBS_DIR)
    if folder is None:
        raise HTTPException(status_code=404, detail="stem not found")
    path = (folder / "audio.wav").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")
    return path


def _take_versions(job_id: str, names: list[str]) -> list[str]:
    """Cache-key fragments for the recorded lanes in `names`, the only lanes
    whose file can change in place. A track's fragment names its active take,
    so switching the active take misses the cache."""
    out: list[str] = []
    for name in names:
        if not _is_recorded_lane(name):
            continue
        take_id = _lane_take_id(job_id, name)
        st = _take_lane_path(job_id, name).stat()
        out.append(f"{name}={take_id}@{st.st_size}:{st.st_mtime_ns}")
    return out


def _validate_stem_path(job_id: str, name: str):
    """Shared guard: validate job_id, name, job state, and path. Returns resolved Path."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if name not in _ALLOWED_NAMES:
        raise HTTPException(status_code=404, detail="unknown stem")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    # WAV or MP3, whichever this song keeps (#783).
    path = stem_path(JOBS_DIR / job_id / "stems", name).resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="stem not found")
    return path


class _ClickLane(NamedTuple):
    """The extra click/count-in audio input handed to the ffmpeg graph.

    `lead_in` is the seconds of count-in baked into the front of the WAV (0 for
    a plain click). `count_in` marks the file as living in *output* coordinates
    -- already region-trimmed and lead-in-prefixed -- so the graph must not
    `-ss` it and must delay the stems by `lead_in` to match.
    """

    path: Path
    gain: float
    lead_in: float
    count_in: bool


def _parse_groups(raw: str, accent_mode: int) -> list[int] | None:
    """Parse a "3+2+2" grouping, or None to use the default.

    Rejected wholesale rather than repaired, and never a 422: a grouping that
    does not fit the bar would put accents on beats the user never asked for,
    and an export is better served with the default grouping than refused. The
    same rule the browser applies in transport.js::_applyGrouping, so a URL a
    user cannot produce from the UI degrades to what the UI would have sent.

    Only meaningful with an explicit meter -- under Auto each detected bar is
    grouped by its own length, so one grouping cannot be assumed to fit.
    """
    if not raw or accent_mode <= 0:
        return None
    parts = [int(p) for p in re.findall(r"\d+", raw)[:32]]
    if not parts or any(p < 1 for p in parts) or sum(parts) != accent_mode:
        return None
    return parts


def _click_lane(
    job_id: str,
    enabled: bool,
    multiplier: float,
    accent_mode: int,
    gain: float,
    count_in_bars: int = 0,
    start: float | None = None,
    end: float | None = None,
    groups: list[int] | None = None,
    sound: str = DEFAULT_CLICK_SOUND,
) -> _ClickLane | None:
    """Render (or reuse) the click / count-in track for this job as an extra
    ffmpeg input, or None when both are off or the job has no beat grid.

    The click is synthesised in the browser during playback and never reaches
    the server, so an export can only include it by rendering an equivalent WAV
    here. A plain click spans the whole track and is lined up by the region trim
    (`-ss` before every input); a count-in is baked in output coordinates
    instead (see render_count_in_wav) and the caller delays the stems to match.
    """
    if not enabled and count_in_bars <= 0:
        return None
    grid = _read_beat_grid(job_id)
    if grid is None:
        return None
    beats = grid.get("beats") or []
    if not beats:
        return None
    bars = grid.get("bars") or []
    duration = float(grid.get("duration") or 0.0)
    sample_rate = get_export_sample_rate()
    g = max(0.0, min(4.0, gain))

    key = click_cache_key(
        job_id,
        beats,
        bars,
        duration,
        sample_rate,
        multiplier,
        accent_mode,
        count_in_bars=count_in_bars,
        include_click=enabled,
        start=start,
        end=end,
        groups=groups,
        sound=sound,
    )
    path = _CLICK_CACHE_DIR / f"{key}.wav"

    if count_in_bars > 0:
        # lead_in is a pure function of the grid; recompute it even on a cache
        # hit so the caller can delay the stems without re-reading the WAV.
        lead_in, _ = count_in_beats(
            beats, bars, count_in_bars, multiplier, accent_mode, start=start or 0.0, groups=groups
        )
        if not path.is_file():
            try:
                rendered = render_count_in_wav(
                    path,
                    beats,
                    bars,
                    duration,
                    sample_rate=sample_rate,
                    multiplier=multiplier,
                    accent_mode=accent_mode,
                    count_in_bars=count_in_bars,
                    include_click=enabled,
                    start=start or 0.0,
                    end=end,
                    groups=groups,
                    sound=sound,
                )
            except Exception:
                logger.exception("count-in render failed for %s", job_id)
                return None
            if rendered is None:
                return None
        # keep=path or a render larger than the cache budget evicts itself the
        # instant it is written, and ffmpeg is then handed a missing -i (#512).
        # Same reason the mixdown path passes keep= (#482).
        _prune_mixdown_cache(_CLICK_CACHE_DIR, keep=path)
        return _ClickLane(path, g, lead_in, True)

    if not path.is_file():
        try:
            rendered = render_click_wav(
                path,
                beats,
                bars,
                duration,
                sample_rate=sample_rate,
                multiplier=multiplier,
                accent_mode=accent_mode,
                groups=groups,
                sound=sound,
            )
        except Exception:
            logger.exception("click render failed for %s", job_id)
            return None
        if rendered is None:
            return None
    _prune_mixdown_cache(_CLICK_CACHE_DIR, keep=path)
    return _ClickLane(path, g, 0.0, False)


# A trim range is only ever meaningful inside the track. Without a ceiling,
# `end` is an unbounded float that reaches
# np.zeros(int(round(duration * sample_rate))) in the click renderer -- so
# ?start=0&end=20000&count_in=1 asks for a 7 GB allocation, and a larger value
# raises MemoryError inside a blanket except and silently drops the click
# (#512). The job's own duration is the honest ceiling; MAX_TRIM_SECONDS is the
# backstop for a job whose duration was never recorded.
MAX_TRIM_SECONDS = 6 * 60 * 60


def _validate_trim_range(job_id: str, start: float | None, end: float | None) -> None:
    """Reject a trim range that is not inside the track."""
    if start is None or end is None:
        return
    job = registry_get(job_id)
    duration = getattr(job, "duration_sec", None) if job is not None else None
    ceiling = float(duration) if duration else float(MAX_TRIM_SECONDS)
    # A little slack over the recorded duration: it comes from ffprobe and can
    # sit a hair under the decoded length, and the UI legitimately asks for the
    # very end of a track.
    if end > ceiling + 1.0:
        raise HTTPException(
            status_code=422,
            detail="end is beyond the end of the track",
        )


def _read_beat_grid(job_id: str) -> dict | None:
    """The grid an export should click to: the user's edits when present, the
    detected grid otherwise. Mirrors GET /api/jobs/{id}/beats."""
    stems = (JOBS_DIR / job_id / "stems").resolve()
    if not stems.is_relative_to(JOBS_DIR.resolve()):
        return None
    computed = stems / "beats.json"
    if not computed.is_file():
        return None
    try:
        grid = json.loads(computed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    user = stems / "beats.user.json"
    if user.is_file():
        try:
            edits = json.loads(user.read_text(encoding="utf-8"))
            if isinstance(edits.get("beats"), list) and edits["beats"]:
                grid["beats"] = edits["beats"]
                grid["bars"] = edits.get("bars") or []
        except (OSError, json.JSONDecodeError):
            # Fall back to the detected grid rather than failing the export,
            # but say so: silently ignoring this is how a user ends up asking
            # why their grid edits disappeared.
            logger.warning("ignoring unreadable beat edits for %s", job_id)
    return grid


# Transpose on export (#592). The studio shifts pitch in the browser with a
# SoundTouch AudioWorklet; the server has no such stage, so an export came out
# in the original key however the mixer was set.
#
# PITCH_MIN/MAX mirror static/js/pitchBus.js. Keep them in step: a UI that can
# ask for a shift the export rejects is worse than one that cannot ask.
PITCH_MIN = -6
PITCH_MAX = 6

# Probed once per process; the ffmpeg binary cannot change mid-run. None until
# the first export needs to know.
_rubberband_cached: bool | None = None


def _rubberband_available() -> bool:
    """Whether this ffmpeg can pitch-shift.

    librubberband is GPL and plenty of builds omit it -- the Homebrew ffmpeg on
    a dev machine does, while the bundled macOS build does not -- so this is
    probed rather than assumed. Any probe problem reads as "no": an export that
    silently used a different algorithm would be worse than one that says it
    cannot transpose.

    rubberband specifically, not asetrate/atempo. Measured on the bundled build:
    rubberband holds duration to the microsecond across -6..+6 and lands within
    1 Hz of the target, and a lane whose length moved would desync the mix.
    """
    global _rubberband_cached
    if _rubberband_cached is None:
        _rubberband_cached = False
        try:
            probe = subprocess.run(
                [ffmpeg_executable(), "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            _rubberband_cached = bool(re.search(r"\brubberband\b", probe.stdout))
        except (OSError, subprocess.SubprocessError):
            # Treated as "no pitch support", which is the safe answer, but the
            # two cases are not the same: a build genuinely without the filter
            # and an ffmpeg that would not run at all both surface to the user
            # as "this build cannot transpose". Log which one it was, or that
            # message is the only thing anybody has to go on.
            logger.debug(
                "rubberband probe failed; treating transpose as unavailable", exc_info=True
            )
    return _rubberband_cached


def effective_pitch(name: str, semitones: int) -> int:
    """The shift a lane actually gets, mirroring effectivePitch in
    static/js/pitchBus.js.

    Drums are never transposed, whatever the request says: resampling a snare
    does not move it to another key, it makes it a different drum. The client
    refuses this too, so honouring a drums pitch here would export something the
    studio will not play.
    """
    if name == "drums":
        return 0
    return max(PITCH_MIN, min(PITCH_MAX, int(semitones)))


def _parse_pitches(pitches: str, names: list[str]) -> list[int]:
    """Parse semitone shifts parallel to `names`, or all zeros when absent.

    Empty means "no transpose", which is what every client that predates #592
    sends by omitting the parameter -- so an old caller keeps its exact
    behaviour rather than needing to opt out.
    """
    if not pitches:
        return [0] * len(names)
    raw = [p for p in pitches.split(",") if p != ""]
    if len(raw) != len(names):
        raise HTTPException(status_code=422, detail="pitches must be the same length as stems")
    try:
        parsed = [int(p) for p in raw]
    except ValueError:
        raise HTTPException(status_code=422, detail="pitches must be whole semitones") from None
    if any(p < PITCH_MIN or p > PITCH_MAX for p in parsed):
        raise HTTPException(
            status_code=422, detail=f"pitch out of range ({PITCH_MIN}..{PITCH_MAX})"
        )
    return [effective_pitch(n, p) for n, p in zip(names, parsed, strict=True)]


def _pitch_filter(semitones: int) -> str:
    """The rubberband stage for a lane, or "" when it is not being shifted.

    Trailing comma so it composes into the existing per-lane chain. Pitch is a
    frequency ratio, hence the twelfth root of two.
    """
    if semitones == 0:
        return ""
    return f"rubberband=pitch={2 ** (semitones / 12):.9f},"


def _parse_lane_gains(stems: str, gains: str) -> tuple[list[str], list[float]]:
    """Parse and validate parallel comma-separated lane names and linear gains.
    Shared by the audio mixdown and the MP4 video mux. Raises HTTPException
    on malformed input, unknown lanes, or out-of-range gains."""
    names = [s for s in stems.split(",") if s]
    raw_gains = [g for g in gains.split(",") if g]
    if not names or len(names) != len(raw_gains):
        raise HTTPException(
            status_code=422, detail="stems and gains must be non-empty and equal length"
        )
    try:
        parsed_gains = [float(g) for g in raw_gains]
    except ValueError:
        raise HTTPException(status_code=422, detail="gains must be numbers") from None
    if any(g < 0 or g > _MIXDOWN_MAX_GAIN for g in parsed_gains):
        raise HTTPException(status_code=422, detail="gain out of range")
    if not all(n in _MIXDOWN_NAMES or _is_recorded_lane(n) for n in names):
        raise HTTPException(status_code=422, detail="unknown stem requested")
    if "vocals" in names and _VOCAL_DECOMPOSITION_NAMES & set(names):
        raise HTTPException(
            status_code=422,
            detail="vocals cannot be combined with lead_vocals/backing_vocals (same signal)",
        )
    return names, parsed_gains


# The longest lanes= value: the six base stem names and their commas.
LANES_PARAM_MAX_CHARS = 64
LANES_QUERY_DESCRIPTION = (
    "The base stems that are lanes of their own in the mode exported, muted or "
    "not, comma-separated (may be empty). With it, Original is rebuilt as every "
    "other stem, as playback builds it. Absent: Original is the job's own file."
)


def parse_lanes_param(lanes: str | None) -> frozenset[str] | None:
    """The `lanes=` value: None when absent (behaviour exactly as before it
    existed), else the set of base stems with a lane of their own. Unknown or
    repeated names are refused, never ignored: a dropped name would put its
    stem into Original and the export would carry it twice."""
    # Not a string: absent, or the Query default of a route called directly
    # as a function (tests do), which is absent too.
    if not isinstance(lanes, str):
        return None
    names = [n for n in lanes.split(",") if n]
    if len(names) != len(set(names)) or any(n not in STEM_NAMES for n in names):
        raise HTTPException(status_code=422, detail="lanes: unknown or repeated stem")
    return frozenset(names)


class _RenderLane(NamedTuple):
    """One ffmpeg input in the mixdown graph: a file, its gain, its shift, and
    the mixer lane it belongs to (whose EQ it gets; "" for the click)."""

    path: Path
    gain: float
    pitch: int
    name: str = ""


def parse_fx_query(fx: str) -> FxRequest:
    """The fx= parameter, validated, or a 422 with a safe message."""
    try:
        return parse_fx_param(fx)
    except FxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _input_rate(path: Path) -> int:
    """The sample rate an input file is decoded at: the rate the EQ is
    designed for, because the filter runs on the file's own samples (before
    any resample to the export rate). Stems and takes are 44.1 kHz; reading the
    header keeps an older job at another rate right."""
    try:
        return int(sf.info(str(path)).samplerate)
    except Exception:
        logger.debug("could not read the sample rate of %s; assuming 44100", path, exc_info=True)
        return 44100


class RenderFx(NamedTuple):
    """The EQ of one render: per input a filter prefix ("" or "biquad=...,"),
    the master prefix, and the cache key fragment."""

    lanes: list[str]
    master: str
    fragment: str


def build_render_fx(
    lane_names: list[str], lane_paths: list[Path], fx_req: FxRequest, extra_sections: int = 0
) -> RenderFx:
    """Filter prefixes for each input (by its lane name) and for the mix.

    An input whose lane has no EQ gets "" and the graph is exactly the one
    built before the EQ existed. The master EQ is designed at the first
    input's rate, which is the rate amix runs at."""
    prefixes: list[str] = []
    sections = extra_sections
    for name, path in zip(lane_names, lane_paths, strict=True):
        effects = fx_req.lanes.get(name, []) if name else []
        chain = slot_filter(effects, _input_rate(path)) if effects else ""
        sections += slot_section_count(effects)
        prefixes.append(f"{chain}," if chain else "")
    master_chain = ""
    if fx_req.master and lane_paths:
        master_chain = slot_filter(fx_req.master, _input_rate(lane_paths[0]))
        sections += slot_section_count(fx_req.master)
    try:
        check_section_budget(sections)
    except FxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    master = f"{master_chain}," if master_chain else ""
    fragment = ""
    if master or any(prefixes):
        fragment = "fx:" + "|".join(prefixes) + "#" + master
    return RenderFx(prefixes, master, fragment)


def parse_master_query(master: str) -> MasterSettings | None:
    """The master= parameter, validated, or a 422 with a safe message."""
    try:
        return parse_master_param(master)
    except MasterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _read_master_plan(path: Path) -> MasterPlan | None:
    try:
        return MasterPlan.from_json(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        logger.debug("unreadable master plan %s", path, exc_info=True)
        return None


def _write_master_plan(path: Path, plan: MasterPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(plan.to_json()), encoding="utf-8")
    os.replace(tmp, path)
    try:
        entries = sorted(
            (p for p in path.parent.iterdir() if p.is_file() and not p.name.startswith(".")),
            key=lambda p: p.stat().st_mtime,
        )
        for old in entries[: max(0, len(entries) - _MASTER_PLAN_MAX_FILES)]:
            if old != path:
                old.unlink(missing_ok=True)
    except OSError:
        logger.debug("master plan prune failed", exc_info=True)


async def master_plan(
    job_id: str,
    lane_paths: list[Path],
    lane_chains: list[str],
    master_eq: str,
    settings: MasterSettings,
    lossy: bool,
    versions: list[str],
) -> tuple[MasterPlan, int]:
    """The measured master of the whole song for these lanes, and the rate the
    master chain runs at (the first input's, which amix runs at).

    ``lane_chains[i]`` is input i's chain as the export runs it (EQ, pitch
    shift, gain), ``master_eq`` the master EQ prefix, ``versions`` the take
    fragments that pin files which can change in place. Cached on disk; a
    mix measured once is never measured again, and never twice at once."""
    rate = await asyncio.to_thread(_input_rate, lane_paths[0])
    ceiling = effective_ceiling(settings, lossy)
    inputs: list[str] = []
    for p in lane_paths:
        inputs += ["-i", str(p)]
    graph = plan_graph(lane_chains, master_eq)
    raw = "|".join(
        [
            _MASTER_PLAN_VERSION,
            job_id,
            ";".join(str(p) for p in lane_paths),
            graph,
            ",".join(versions),
            settings.fragment(),
            f"{ceiling:.1f}",
            str(rate),
        ]
    )
    key = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()
    path = _MASTER_PLAN_DIR / f"{key}.json"
    cached = await asyncio.to_thread(_read_master_plan, path)
    if cached is not None:
        return cached, rate
    lock = _master_plan_locks.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            cached = await asyncio.to_thread(_read_master_plan, path)
            if cached is not None:
                return cached, rate
            async with _master_plan_slots:
                try:
                    plan = await run_plan(inputs, graph, settings, ceiling, rate)
                except MeasureError as exc:
                    logger.warning("master measurement failed [job=%s]: %s", job_id, exc)
                    raise HTTPException(
                        status_code=500, detail="loudness measurement failed"
                    ) from None
            await asyncio.to_thread(_write_master_plan, path, plan)
            return plan, rate
    finally:
        if not lock.locked():
            _master_plan_locks.pop(key, None)


def master_headers(plan: MasterPlan, out: Loudness | None = None) -> dict[str, str]:
    """The measured loudness before and after the master, for API clients.
    ``out`` is the decoded file's own measurement (lossy exports), otherwise
    the plan's (lossless output is the plan's signal, quantised)."""

    def fmt(v: float | None) -> str:
        return "none" if v is None else f"{v:.2f}"

    post = out or plan.post
    return {
        "X-StemDeck-Loudness-Pre": f"i={fmt(plan.pre.i)};tp={fmt(plan.pre.tp)}",
        "X-StemDeck-Loudness-Post": f"i={fmt(post.i)};tp={fmt(post.tp)}",
        "X-StemDeck-Master-Gain": f"{plan.gain_db:.2f}",
    }


def _output_note_path(cache_key: str) -> Path:
    return _MASTER_PLAN_DIR / f"out-{cache_key}.json"


def _read_master_output(cache_key: str) -> Loudness | None:
    try:
        data = json.loads(_output_note_path(cache_key).read_text(encoding="utf-8"))
        return Loudness(**data)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        logger.debug("unreadable master output note %s", cache_key, exc_info=True)
        return None


def _write_master_output(cache_key: str, out: Loudness) -> None:
    path = _output_note_path(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(asdict(out)), encoding="utf-8")
    os.replace(tmp, path)


async def _render_master_verified(
    build_cmd: Callable[[float, bool], list[str]],
    suffix: str,
    context: str,
    ceiling: float,
    cache_path: Path | None,
    cache_key: str | None,
    has_alt_encoder: bool = False,
) -> tuple[Path, Loudness]:
    """Render a mastered lossy export, decode it and measure its true peak.

    The master limits the signal at the lossy ceiling, but an encoder adds
    peak of its own: up to 1.0 dB for MP3 and OGG on dense limited material,
    and the AAC encoder sometimes sends a single block several dB over (task F
    measured +3.6 dBFS on one song at a -1.6 dB limit, not monotonic in the
    input level). So the file is checked as a player would decode it, and when
    it is over the user's ceiling the master's output is trimmed by the
    overshoot plus LOSSY_RETRY_MARGIN_DB and the file rendered again, up to
    LOSSY_RETRIES times. With ``has_alt_encoder`` (AAC) the first retry keeps
    the level and switches to the alternative encoder settings instead: an AAC
    glitch is one block, and trimming the whole song by it cost up to 4.9 LU
    (measured), where the fast coder stayed within 0.8 dB of its input on
    every song tried. Returns the file (the cache entry when ``cache_path``
    is given, otherwise a temp file the caller removes) and its measurement."""
    trim = 0.0
    alt = False
    path: Path | None = None
    out = Loudness(None, None)
    for attempt in range(LOSSY_RETRIES + 1):
        path = await _render_to_file(build_cmd(trim, alt), suffix, context=context)
        try:
            out = await measure_file(str(path))
        except MeasureError as exc:
            path.unlink(missing_ok=True)
            logger.warning("master output measurement failed [%s]: %s", context, exc)
            raise HTTPException(status_code=500, detail="export failed") from None
        if out.tp is None or out.tp <= ceiling:
            break
        if attempt == LOSSY_RETRIES:
            logger.warning(
                "mastered export still %.2f dBTP over its ceiling after %d retries [%s]",
                out.tp - ceiling,
                LOSSY_RETRIES,
                context,
            )
            break
        if has_alt_encoder and not alt:
            alt = True
        else:
            trim += out.tp - ceiling + LOSSY_RETRY_MARGIN_DB
        path.unlink(missing_ok=True)
    assert path is not None
    if cache_path is not None and cache_key is not None:
        os.replace(path, cache_path)
        _prune_mixdown_cache(cache_path.parent, keep=cache_path)
        await asyncio.to_thread(_write_master_output, cache_key, out)
        path = cache_path
    return path, out


def master_plan_view(plan: MasterPlan) -> dict:
    """The plan as the studio reads it (the readout and the live preview)."""
    view = plan.to_json()
    view["comp_ratio"] = comp_ratio(plan.comp) if plan.comp_threshold_db is not None else None
    return view


def _lane_chain(eq: str, pitch: int, gain: float) -> str:
    """One input's chain as every export runs it: EQ, pitch shift, gain."""
    return f"{eq}{_pitch_filter(pitch)}volume={gain:.6f}"


def _expand_render_lanes(
    job_id: str,
    names: list[str],
    gains: list[float],
    pitches: list[int],
    remove: frozenset[str] = frozenset(),
    lanes: frozenset[str] | None = None,
) -> tuple[list[_RenderLane], list[str]]:
    """Lanes to render, with "original" rebuilt from its parts when it is
    transposed. Returns the lanes and the names of any lane left unpitched.

    "original" is the complement lane: whatever the user did not select, already
    summed into one file -- which means it usually contains drums. Shifting that
    file would resample the drums, so playback never does it. buildPlaybackStems
    in static/js/playbackStems.js rebuilds the lane from the component stems
    instead, routing drums around the pitch stage, and this is the same move on
    the export side.

    When a component is missing -- an older job that kept only the rendered mix
    -- the lane cannot be taken apart, so it stays at its original key. That is
    deliberately the same conservative choice playback makes: melodic content
    stays in the old key rather than the drums being resampled.

    `remove` (Streamer Mode's "I play": the stems the musician plays) is left
    out: a lane of that name is dropped, and "original" is always rebuilt
    from its components without it, as playback does (playbackStems.js), so
    the export carries exactly the backing that was heard. A job that cannot
    be taken apart then gets a 422 rather than the removed part back.

    `lanes` (the mode's own lanes: every base stem shown as a lane, muted or
    not) makes Original exactly what playback plays: every base stem that is
    neither a lane nor removed. Without it Original is the complement of the
    *requested* names, and the client leaves muted and solo-silenced lanes out
    of the request, so a transposed export with a muted lane put that stem
    back inside Original (tests/test_mode_stems.py). With it, a mode whose
    lanes differ from the job's selection never exports a stem twice: at key 0
    the job's original.wav is used only when it holds exactly that set.
    """
    if lanes is not None:
        return _expand_mode_lanes(job_id, names, gains, pitches, remove, lanes)
    lanes_out: list[_RenderLane] = []
    unpitched: list[str] = []
    selected = set(names)
    # A completed lead/backing split stands in for the base vocals source, so
    # vocals is not also part of the complement (mirrors buildPlaybackStems).
    if {"lead_vocals", "backing_vocals"} <= selected:
        selected.add("vocals")

    for name, gain, pitch in zip(names, gains, pitches, strict=True):
        # Validate first, always. Whether a lane exists is a property of the
        # job, not of the request: without this an "original" lane the job
        # never produced would 404 untransposed and be synthesised from
        # components when transposed, so the same URL was valid or not
        # depending on the pitch attached to it.
        if _is_recorded_lane(name):
            lanes_out.append(_RenderLane(_take_lane_path(job_id, name), gain, pitch, name))
            continue
        if name in remove:
            continue
        path = _validate_stem_path(job_id, name)
        if name != "original" or (pitch == 0 and not remove):
            lanes_out.append(_RenderLane(path, gain, pitch, name))
            continue

        components = [n for n in STEM_NAMES if n not in selected and n not in remove]
        if not components:
            # Everything the lane held is removed or has a lane of its own.
            continue
        # Deliberately not `path`: that name holds the validated "original.wav"
        # this lane falls back to, and reusing it here left the fallback
        # pointing at whichever component was tried last.
        paths: list[Path] = []
        for comp in components:
            comp_path = stem_path(JOBS_DIR / job_id / "stems", comp).resolve()
            if not comp_path.is_file() or not comp_path.is_relative_to(JOBS_DIR.resolve()):
                paths = []
                break
            paths.append(comp_path)

        if not paths:
            if remove:
                raise HTTPException(
                    status_code=422, detail="this song's stems cannot be mixed without one part"
                )
            lanes_out.append(_RenderLane(path, gain, 0, name))
            unpitched.append(name)
            continue

        # Each component carries the lane's own gain; amix sums them back to
        # what the single file would have been. effective_pitch keeps drums at 0.
        for comp, path in zip(components, paths, strict=True):
            lanes_out.append(_RenderLane(path, gain, effective_pitch(comp, pitch), name))

    return lanes_out, unpitched


def _job_original_set(job_id: str) -> frozenset[str] | None:
    """The stems the job's own original.wav holds (make_original_track: every
    base stem not selected), or None when it has no such file."""
    job = registry_get(job_id)
    path = stem_path(JOBS_DIR / job_id / "stems", "original").resolve()
    if job is None or not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        return None
    selected = set(job.selected_stems)
    return frozenset(n for n in STEM_NAMES if n not in selected)


def _expand_mode_lanes(
    job_id: str,
    names: list[str],
    gains: list[float],
    pitches: list[int],
    remove: frozenset[str],
    lanes: frozenset[str],
) -> tuple[list[_RenderLane], list[str]]:
    """_expand_render_lanes for a request that names its mode's lanes.

    Original = every base stem that is neither in `lanes` nor removed, always,
    whatever was muted. A base stem requested as a lane must be in `lanes`:
    otherwise it would also be inside Original and play twice."""
    laned = set(lanes)
    if {"lead_vocals", "backing_vocals"} & set(names):
        # The split halves are the Voice lane: vocals is not in Original.
        laned.add("vocals")
    for name in names:
        if name in STEM_NAMES and name not in laned and name not in remove:
            raise HTTPException(status_code=422, detail="lanes: a requested stem is not a lane")
    components = [n for n in STEM_NAMES if n not in laned and n not in remove]
    own_set = _job_original_set(job_id)

    out: list[_RenderLane] = []
    unpitched: list[str] = []
    for name, gain, pitch in zip(names, gains, pitches, strict=True):
        if _is_recorded_lane(name):
            out.append(_RenderLane(_take_lane_path(job_id, name), gain, pitch, name))
            continue
        if name in remove:
            continue
        if name != "original":
            out.append(_RenderLane(_validate_stem_path(job_id, name), gain, pitch, name))
            continue
        # The job id and state, as _validate_stem_path checks them; the file
        # itself may not exist (a song extracted with all six stems).
        if not JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=404, detail="job not found")
        job = registry_get(job_id)
        if job is None or job.status != "done":
            raise HTTPException(status_code=404, detail="job not ready")
        if not components:
            continue
        if pitch == 0 and own_set is not None and own_set == frozenset(components):
            # The job's own file holds exactly this set: the export made
            # before lanes= existed, byte for byte.
            out.append(_RenderLane(_validate_stem_path(job_id, "original"), gain, 0, name))
            continue
        paths: list[Path] = []
        for comp in components:
            comp_path = stem_path(JOBS_DIR / job_id / "stems", comp).resolve()
            if not comp_path.is_file() or not comp_path.is_relative_to(JOBS_DIR.resolve()):
                paths = []
                break
            paths.append(comp_path)
        if not paths:
            if own_set is not None and own_set == frozenset(components):
                # An older job whose parts are gone: its file, unpitched,
                # the conservative choice playback makes.
                path = _validate_stem_path(job_id, "original")
                out.append(_RenderLane(path, gain, 0, name))
                unpitched.append(name)
                continue
            raise HTTPException(
                status_code=422, detail="this song's stems cannot be mixed for these lanes"
            )
        for comp, path in zip(components, paths, strict=True):
            out.append(_RenderLane(path, gain, effective_pitch(comp, pitch), name))
    return out, unpitched


async def _drain_stderr(stream: asyncio.StreamReader, sink: deque[str]) -> None:
    """Collect ffmpeg stderr lines into a bounded deque. Draining is mandatory
    once stderr is a pipe -- an undrained full pipe would deadlock ffmpeg."""
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace").rstrip())


async def _stream_ffmpeg(cmd: list[str], context: str = "", cache_path: Path | None = None):
    """Yield ffmpeg stdout in 64 KB chunks; kill process on client disconnect.

    stderr is captured (bounded tail) and logged at WARNING when ffmpeg exits
    non-zero (#280): the HTTP status is already committed mid-stream, so a
    failed render reaches the client as a truncated file -- the log entry is
    the only place the failure can surface. Kills we initiated (client
    disconnect) are expected and not logged as failures.

    When `cache_path` is given, every chunk is also teed to a per-request
    temp file alongside it (#290). A clean finish atomically renames the temp
    file into place as the cache entry and prunes the cache to its budget;
    any failure or client disconnect removes the temp file instead -- a
    render the client didn't get in full never becomes a cache hit for the
    next request."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: deque[str] = deque(maxlen=30)
    drain_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_tail))
    # Whether stdout reached EOF. proc.returncode stays None until wait()
    # reaps the child even after it exited, so EOF -- not returncode -- is
    # what distinguishes "ffmpeg finished on its own" from "client
    # disconnected mid-stream and we killed it".
    finished = False

    tmp_path: Path | None = None
    tmp_file = None
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_file = open(tmp_path, "wb")  # noqa: SIM115 -- closed in finally, not a context manager here
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                finished = True
                break
            if tmp_file is not None:
                tmp_file.write(chunk)
            yield chunk
    finally:
        if tmp_file is not None:
            tmp_file.close()
        if not finished and proc.returncode is None:
            proc.kill()
        await proc.wait()
        try:
            await asyncio.wait_for(drain_task, timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            drain_task.cancel()
        if finished and proc.returncode != 0:
            logger.warning(
                "stream ffmpeg exit %s [%s]: %s",
                proc.returncode,
                context,
                " | ".join(list(stderr_tail)[-8:]) or "(no stderr)",
            )
        if tmp_path is not None:
            if finished and proc.returncode == 0:
                os.replace(tmp_path, cache_path)
                _prune_mixdown_cache(cache_path.parent, keep=cache_path)
            else:
                tmp_path.unlink(missing_ok=True)


async def _render_to_file(
    cmd: list[str], suffix: str, context: str = "", cache_path: Path | None = None
) -> Path:
    """Run ffmpeg to completion against a real file and return the path.

    The sibling of _stream_ffmpeg, for the containers in _SEEKABLE_OUTPUT_EXTS
    whose muxer finishes the file by seeking back to a header it wrote earlier
    (#458). A pipe cannot seek, so those headers were never patched and every
    exported WAV claimed roughly 4 GB of audio.

    Returning a path rather than yielding chunks is what lets the caller answer
    with FileResponse, and that is worth more than the extra buffering costs.
    A streamed render commits HTTP 200 before ffmpeg has exited, so a failure
    reaches the client as a truncated file that only the server log records;
    here the exit code is known while the response is still ours to choose, so
    a failed render is an honest 500. It also brings Content-Length and range
    requests, which chunked encoding cannot offer at all.

    `cmd` must not carry an output path -- this appends one. With `cache_path`
    the temp file is created beside it and a clean render is renamed into place
    as the cache entry (#290); the caller then serves the cache entry and must
    not delete it. Without one the caller owns the returned temp file and is
    responsible for removing it once the response has been sent.
    """
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp{suffix}")
    else:
        _MIXDOWN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _MIXDOWN_CACHE_DIR / f".render.{uuid.uuid4().hex}.tmp{suffix}"
    # Both names start with a dot, which is what keeps an in-flight render out
    # of _prune_mixdown_cache's eviction list and out of the cache-hit path.

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        "-y",
        str(tmp_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr_tail: deque[str] = deque(maxlen=30)
    drain_task = asyncio.create_task(_drain_stderr(proc.stderr, stderr_tail))
    ok = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=TIMEOUT_FFMPEG)
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
        try:
            await asyncio.wait_for(drain_task, timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            drain_task.cancel()
        if proc.returncode != 0:
            logger.warning(
                "render ffmpeg exit %s [%s]: %s",
                proc.returncode,
                context,
                " | ".join(list(stderr_tail)[-8:]) or "(no stderr)",
            )
            raise HTTPException(status_code=500, detail="export failed")
        ok = True
    finally:
        # A cancelled request (client gone) unwinds through here too, so the
        # process is never left running and the temp file is never orphaned.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not ok:
            tmp_path.unlink(missing_ok=True)

    if cache_path is not None:
        os.replace(tmp_path, cache_path)
        # Exempt from its own prune: this is the file about to be served.
        _prune_mixdown_cache(cache_path.parent, keep=cache_path)
        return cache_path
    return tmp_path


def _unlink_later(path: Path) -> None:
    """Drop a rendered temp file once its response has been sent.

    Only ever attached to an uncached render. A cached one returns the cache
    entry itself, and deleting that would throw away the render the cache
    exists to keep."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("could not remove rendered export %s", path, exc_info=True)


async def _ensure_cached_mp3(src: Path) -> Path:
    """Transcode `src` (a stem WAV) to a sibling `<name>.mp3`, cached on disk.
    Re-encoding a full song on every request is the slow part of loading a track
    on mobile (≈3s/stem × 6 in parallel); caching makes repeat loads instant.
    Written atomically (temp + rename) so concurrent fetches can't serve a
    partial file."""
    dest = src.with_suffix(".mp3")
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-q:a",
        "2",  # VBR ~190 kbps
        "-f",
        "mp3",
        str(tmp),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT_FFMPEG)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=504, detail="mp3 transcode timed out") from None
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        logger.warning(
            "mp3 transcode failed for %s: %s", src.name, (stderr or b"").decode("utf-8", "replace")
        )
        raise HTTPException(status_code=500, detail="mp3 transcode failed")
    os.replace(tmp, dest)
    return dest


# One build at a time per job: the catalog and the player can both ask for
# the peaks of a song that has none in the same moment.
_peaks_build_locks: dict[str, asyncio.Lock] = {}


async def _build_missing_peaks(job_id: str, job_takes: list[dict]) -> None:
    """Build peaks.json for a finished job that has none (see
    fill_missing_peaks). Failures leave the job as it was: the studio then
    decodes the stems in the browser, as it always did for such a job."""
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(JOBS_DIR.resolve()):
        return
    lock = _peaks_build_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        if (stems_dir / "peaks.json").is_file():
            return
        sources: dict[str, Path] = {}
        for take in job_takes:
            folder = take_dir(job_id, str(take.get("id") or ""))
            if folder is not None:
                sources[take["id"]] = folder / "audio.wav"
        try:
            await asyncio.to_thread(fill_missing_peaks, stems_dir, sources)
        except Exception:
            logger.exception("could not build peaks for %s", job_id)
    _peaks_build_locks.pop(job_id, None)


async def _peaks_response(job_id: str, request: Request) -> Response:
    """Pre-computed waveform peaks for every stem, revalidated on each use.

    Not cacheable forever, because it is not fixed once a job is done. The
    on-demand splits add stems to a finished job and rewrite this file through
    merge_stem_peaks. It used to be served `immutable`, which a browser takes
    literally, so after a lead/backing split the new stems played under the
    waveform from before it, and neither a reload nor a hard reload could fix
    it (#639).

    `no-cache` keeps a copy but asks before using it. The ETag makes asking
    cheap: an unchanged file is a 304 with no body, which FileResponse does not
    do by itself.

    A finished job with no peaks.json (older or hand-made) has it built on
    this first request, about a second for a three minute song, so it opens
    on the streaming engine like any other. 404 only when that fails.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / "peaks.json").resolve()
    if not path.is_file():
        await _build_missing_peaks(job_id, list(job.takes or []))
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="peaks not found")
    st = path.stat()
    # Changes whenever the file is rewritten: the rewrite is a new file moved
    # into place, so its mtime moves even when the size happens not to.
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    offered = request.headers.get("if-none-match", "")
    # A list, possibly weak-prefixed, or "*". Weak comparison is the right one
    # for If-None-Match (RFC 9110 13.1.2).
    if offered.strip() == "*" or etag in {
        tag.strip().removeprefix("W/") for tag in offered.split(",")
    }:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="application/json", headers=headers)


@router.get("/jobs/{job_id}/peaks")
async def get_peaks(job_id: str, request: Request) -> Response:
    """Waveform peaks for all stems. The URL the frontend uses.

    A separate URL from stems/peaks.json on purpose, the way /jobs/{id}/beats
    sits beside stems/beats.json. Browsers that already hold peaks.json under
    the old `immutable` header will keep serving that copy for up to a year
    without asking, whatever the server says now. Only a different URL reaches
    them, and this one is known before the job's state is, so the peaks fetch
    can still start in parallel with it.
    """
    return await _peaks_response(job_id, request)


@router.get("/jobs/{job_id}/stems/peaks.json")
async def get_stem_peaks(job_id: str, request: Request) -> Response:
    """Waveform peaks, at the URL they were first served from.

    Kept for anything that still asks here. It no longer claims to be
    immutable, since that was never true once splits could rewrite it.
    """
    return await _peaks_response(job_id, request)


# The vocals stem's level every 40ms: fine enough to find the gap between two
# sung words (a breath is 150ms or more), coarse enough that a ten minute track
# is about 60 KB. peaks.json cannot serve instead: it is a fixed 3000 points
# per track, 100ms apart on a five minute song and 200ms on a ten minute one.
_VOCAL_ENVELOPE_HOP_SEC = 0.04
_VOCAL_ENVELOPE_FILE = "vocal_envelope.json"


def _write_vocal_envelope(src: Path, dest: Path) -> None:
    hop, levels = vocal_envelope(src, _VOCAL_ENVELOPE_HOP_SEC)
    # A unique temp name: two first requests for the same track may compute at
    # once, and each moves a whole file into place.
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps({"hop": hop, "db": levels}), encoding="utf-8")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


@router.get("/jobs/{job_id}/vocal-envelope")
async def get_vocal_envelope(job_id: str) -> Response:
    """How loud the vocals stem is over time, for the lyrics wipe (#699):
    `{"hop": seconds, "db": [whole dBFS per hop, -90 to 0]}`.

    Lyrics from LRCLIB mostly stamp only when each line starts, so the tab
    uses this to tell where in a line the singer is actually singing.

    Computed the first time a track asks and kept beside its stems, so every
    job has it, including those separated before it existed. Recomputed if
    vocals.wav is newer than what was kept. Deliberately outside
    _pipeline_lock: it is one streamed read of one stem, under a second for a
    five minute track, and waiting behind a whole separation would leave the
    lyrics on the rough estimate for minutes.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    src = stem_path(stems_dir, "vocals")
    dest = stems_dir / _VOCAL_ENVELOPE_FILE
    jobs_root = JOBS_DIR.resolve()
    if not src.is_file() or not src.is_relative_to(jobs_root) or not dest.is_relative_to(jobs_root):
        raise HTTPException(status_code=404, detail="no vocals")
    try:
        if not dest.is_file() or dest.stat().st_mtime_ns < src.stat().st_mtime_ns:
            await asyncio.to_thread(_write_vocal_envelope, src, dest)
    except Exception:
        logger.exception("could not compute the vocal envelope for %s", job_id)
        raise HTTPException(status_code=500, detail="vocal envelope unavailable") from None
    return FileResponse(dest, media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.get("/jobs/{job_id}/stems/beats.json")
async def get_beat_grid(job_id: str) -> Response:
    """Return the pre-computed beat grid driving the click track.

    404s for jobs separated before this stage existed; the UI treats that as
    "no click track available" rather than an error.
    """
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    path = (JOBS_DIR / job_id / "stems" / "beats.json").resolve()
    if not path.is_file() or not path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="beat grid not found")
    return FileResponse(
        path,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


def _stem_download_name(job_id: str, name: str, ext: str, suffix: str = "") -> str:
    """Filename offered for a single stem, prefixed with the song (#336).

    Content-Disposition wins over an <a download> attribute for same-origin
    requests, so the server has to carry the name for the browser to honour it.
    """
    job = registry_get(job_id)
    slug = _title_slug(job.title if job else None)
    stem = f"{name}{suffix}"
    return f"{slug}_{stem}.{ext}" if slug else f"{stem}.{ext}"


@router.api_route("/jobs/{job_id}/stems/{name}.wav", methods=["GET", "HEAD"], response_model=None)
async def get_stem(
    job_id: str,
    name: str,
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
) -> FileResponse | StreamingResponse:
    """A stem, as the song stores it: WAV, or MP3 for a song stored as MP3
    (#783), with its own type and file name whatever this URL says. Optional
    ?start=&end= trims to a time region, always rendered as WAV."""
    path = _validate_stem_path(job_id, name)

    if start is None and end is None:
        return FileResponse(
            path,
            media_type=stem_media_type(path),
            filename=_stem_download_name(job_id, name, stem_ext(path)),
        )

    if start is None or end is None or start >= end:
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )
    _validate_trim_range(job_id, start, end)

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-i",
        str(path),
        "-t",
        str(end - start),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
    ]
    rendered = await _render_to_file(cmd, ".wav", context=f"stem-region job={job_id} stem={name}")
    return FileResponse(
        rendered,
        media_type="audio/wav",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_stem_download_name(job_id, name, "wav", "_region")}"'
            )
        },
        background=BackgroundTask(_unlink_later, rendered),
    )


@router.get("/jobs/{job_id}/stems/{name}.mp3")
async def get_stem_mp3(
    job_id: str,
    name: str,
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
) -> Response:
    """Stem as MP3 (VBR ~190 kbps). Full stems are cached to disk; ?start=&end=
    streams a freshly-trimmed region (uncached)."""
    path = _validate_stem_path(job_id, name)

    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )
    _validate_trim_range(job_id, start, end)

    # Full-stem requests (no trim) are cached to disk so repeat loads — the
    # common case for the mobile player — are instant instead of re-encoding.
    if start is None:
        cached = await _ensure_cached_mp3(path)
        return FileResponse(
            cached,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{_stem_download_name(job_id, name, "mp3")}"'
                ),
                # Stems are immutable once a job is done — let the phone cache
                # them so a re-load is instant and offline-friendly.
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    pre_seek = ["-ss", str(start)] if start is not None else []
    post_seek = ["-t", str(end - start)] if start is not None else []

    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        *pre_seek,
        "-i",
        str(path),
        *post_seek,
        "-q:a",
        "2",  # VBR ~190 kbps
        "-f",
        "mp3",
    ]
    # Only the trimmed branch reaches here; the untrimmed one returned above.
    filename = _stem_download_name(job_id, name, "mp3", "_region")
    # Rendered rather than streamed for the same reason as the WAV region
    # above: on a pipe ffmpeg omits the Xing frame entirely, and these are VBR,
    # so the result has no duration and no seek table (#458).
    rendered = await _render_to_file(cmd, ".mp3", context=f"stem-mp3 job={job_id} stem={name}")
    return FileResponse(
        rendered,
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=BackgroundTask(_unlink_later, rendered),
    )


def _mixdown_download_name(job_id: str, ext: str, is_region: bool) -> str:
    """Filename offered for a mixdown, prefixed with the song like the stems
    (#336). Mirrors the names the frontend builds, because Content-Disposition
    overrides an <a download> attribute and the two must not disagree."""
    job = registry_get(job_id)
    slug = _title_slug(job.title if job else None)
    kind = "region" if is_region else "exported_mix"
    return f"{slug}_{kind}.{ext}" if slug else f"{kind}.{ext}"


@router.get("/jobs/{job_id}/mixdown.{ext}", response_model=None)
async def get_mixdown(
    job_id: str,
    ext: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    pitches: str = Query(
        default="",
        max_length=128,
        description=(
            "Comma-separated semitone shifts parallel to stems (-6..6). "
            "Empty means no transpose, which is what a client that predates this sends."
        ),
    ),
    start: float | None = Query(default=None, ge=0, description="Trim start in seconds"),
    end: float | None = Query(default=None, gt=0, description="Trim end in seconds"),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
    click_groups: str = Query(
        default="",
        max_length=64,
        description='Beat grouping for an explicit meter, e.g. "3+2+2". Blank uses the default.',
    ),
    click_sound: str = Query(
        default=DEFAULT_CLICK_SOUND, max_length=32, description=CLICK_SOUND_QUERY_DESCRIPTION
    ),
    count_in: int = Query(
        # Ceiling matches MAX_COUNT_IN_BARS in static/js/state.js, the longest
        # count-in the UI can ask for (#587). The lead-in it prepends is
        # count_in * beats_per_bar * beat_interval seconds of extra render, and
        # beats_per_bar is itself capped at 32, so this bounds the allocation
        # the same way `end` is bounded by _validate_trim_range.
        default=0,
        ge=0,
        le=4,
        description="Count-in bars before the audio (0 off)",
    ),
    fx: str = Query(
        default="",
        max_length=FX_PARAM_MAX_CHARS,
        description=(
            "The studio EQ: base64url JSON {v: 1, lanes: {name: [eq]}, master: [eq]}. "
            "Empty means no EQ. See app/pipeline/fx_filters.py."
        ),
    ),
    master: str = Query(
        default="",
        max_length=MASTER_PARAM_MAX_CHARS,
        description=(
            "The master: base64url JSON {v: 1, target: LUFS or null, ceiling: dBTP, "
            "comp: 0..1}. Empty means no master. See app/pipeline/mastering.py."
        ),
    ),
    lanes: str | None = Query(
        default=None, max_length=LANES_PARAM_MAX_CHARS, description=LANES_QUERY_DESCRIPTION
    ),
) -> FileResponse | StreamingResponse:
    """Render a mixdown of the given lanes at the given gains, streamed as WAV,
    MP3, FLAC, or OGG. Mirrors the studio mixer (per-stem volume, mute, solo) so the
    exported file matches what is heard. The master fader is intentionally not
    applied -- it is a monitoring level, not part of the mix. Optional ?start=&end=
    trims to a loop region.

    `count_in` prepends N bars of click before the audio (issue #269): the stems
    are delayed and the count-in is baked into the click WAV, so the exported
    file leads in like a drummer's count. It works with or without the running
    click track (`click`), so a clean backing track can still carry a count-in.

    Identical params (including start/end and the current export sample rate)
    hit a render cache instead of re-running ffmpeg (#290) -- a cheap win on a
    shared server where the same export gets re-downloaded."""
    if ext not in ("wav", "mp3", "flac", "ogg"):
        raise HTTPException(status_code=404, detail="not found")

    names, parsed_gains = _parse_lane_gains(stems, gains)
    parsed_pitches = _parse_pitches(pitches, names)
    fx_req = parse_fx_query(fx)
    master_settings = parse_master_query(master)
    lane_set = parse_lanes_param(lanes)
    check_click_sound(click_sound)
    if (start is None) != (end is None) or (start is not None and start >= end):
        raise HTTPException(
            status_code=422,
            detail="start and end are both required and start must be less than end",
        )
    _validate_trim_range(job_id, start, end)

    # Refuse rather than export in the wrong key. A build without librubberband
    # cannot pitch-shift at all, and quietly returning the original key would
    # look like the bug this feature exists to fix (#592).
    if any(parsed_pitches) and not await asyncio.to_thread(_rubberband_available):
        raise HTTPException(
            status_code=422,
            detail="this ffmpeg build cannot transpose (no rubberband filter)",
        )

    # Validates job_id (404), job done (404), and path traversal (404) per
    # stem -- deliberately before the cache lookup below, so a deleted or
    # not-yet-done job 404s the same way it always has instead of serving a
    # stale cache entry from before the job was removed.
    render_lanes, unpitched = _expand_render_lanes(
        job_id, names, parsed_gains, parsed_pitches, lanes=lane_set
    )
    # Like the transpose check, for an encoder this ffmpeg lacks: libvorbis is
    # optional (#824) and the UI disables OGG without it. A stale page or a
    # direct call gets this instead of an empty streamed file. After the job
    # checks, so an unknown job still answers 404.
    unavailable = await asyncio.to_thread(unavailable_detail, ext)
    if unavailable:
        raise HTTPException(status_code=422, detail=unavailable)
    paths = [lane.path for lane in render_lanes]
    # Each lane's EQ as biquad filters with the live engine's coefficients;
    # "" for a lane without EQ, so its graph is unchanged.
    render_fx = await asyncio.to_thread(
        build_render_fx, [lane.name for lane in render_lanes], paths, fx_req
    )

    # The master: measured on the whole song (so a region gets the full
    # master's gain), then its chain replaces the export limiter.
    lossy = ext in _LOSSY_MIXDOWN_EXTS
    plan: MasterPlan | None = None
    limiter = export_limiter(lossy=lossy)
    master_fragment = ""
    if master_settings is not None:
        plan, rate_in = await master_plan(
            job_id,
            paths,
            [
                _lane_chain(render_fx.lanes[i], lane.pitch, lane.gain)
                for i, lane in enumerate(render_lanes)
            ],
            render_fx.master,
            master_settings,
            lossy,
            _take_versions(job_id, names),
        )
        limiter = plan_chain(plan, rate_in)
        master_fragment = "m:" + limiter

    media_type = MIXDOWN_MEDIA_TYPES[ext]
    click_lane = await asyncio.to_thread(
        _click_lane,
        job_id,
        click,
        click_mult,
        click_accent,
        click_gain,
        count_in_bars=count_in,
        start=start,
        end=end,
        groups=_parse_groups(click_groups, click_accent),
        sound=click_sound,
    )
    cache_key = _mixdown_cache_key(
        job_id,
        ext,
        names,
        parsed_gains,
        start,
        end,
        click_lane,
        parsed_pitches,
        _take_versions(job_id, names),
        render_fx.fragment,
        master_fragment,
        lanes_fragment="" if lane_set is None else "l:" + ",".join(sorted(lane_set)),
    )
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{_mixdown_download_name(job_id, ext, start is not None)}"'
        ),
        **(master_headers(plan) if plan is not None else {}),
    }
    cache_path = _MIXDOWN_CACHE_DIR / f"{cache_key}.{ext}"
    if cache_path.is_file():
        if plan is not None and lossy:
            out = await asyncio.to_thread(_read_master_output, cache_key)
            if out is not None:
                headers.update(master_headers(plan, out))
        return FileResponse(cache_path, media_type=media_type, headers=headers)

    pre_seek = ["-ss", str(start)] if start is not None else []

    # A count-in shifts the whole timeline: the stems are delayed by the lead-in
    # and the click WAV already carries it, in output coordinates, so it is not
    # `-ss`-trimmed like a plain click. Everything else is generic over the input
    # list. lead_in is 0 for a plain click, collapsing this to the old graph.
    lead_in = click_lane.lead_in if click_lane else 0.0
    count_in_mode = bool(click_lane and click_lane.count_in)
    delay_ms = int(round(lead_in * 1000))
    # Output length is the region plus the lead-in prepended in front of it.
    post_seek = ["-t", str(lead_in + (end - start))] if start is not None else []

    stem_count = len(paths)
    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for p in paths:
        cmd += [*pre_seek, "-i", str(p)]
    if click_lane is not None:
        # The count-in WAV is already in output coordinates; a plain click is
        # full-length and lines up under the same -ss as the stems.
        click_pre = [] if count_in_mode else pre_seek
        cmd += [*click_pre, "-i", str(click_lane.path)]

    # Delay each stem by the lead-in (silent front-padding), apply its gain, then
    # sum with amix (normalize=0 keeps levels faithful, matching collect.py). The
    # click lane is never delayed -- its lead-in is baked in. A single audible
    # lane skips amix (a 1-input amix is a no-op).
    #
    # Then the export limiter, always. Six stems at unity summed to +0.9 dBFS
    # on a real song and the encoder clamped 77 samples to full scale. For WAV
    # and FLAC the limiter holds the sample peak at -1 dBFS and is
    # bit-transparent below it, so an export that never reached the ceiling is
    # unchanged (EXPORT_LIMITER). MP3 and OGG get the true-peak limiter at
    # -1.5 dBTP, because their encoders add peak of their own
    # (EXPORT_LIMITER_LOSSY). With a master, its chain (compressor, gain,
    # true-peak limiter) takes the limiter's place (set above).
    filters = []
    for i, lane in enumerate(render_lanes):
        delay = f"adelay={delay_ms}:all=1," if delay_ms > 0 else ""
        # Shift before the delay: rubberband would otherwise chew through the
        # silent lead-in padding for nothing. EQ before the shift, because the
        # live engine's EQ sits before its pitch stage too (between the lane
        # gain and the pitch bus): a boost at 1 kHz on a lane moved up two
        # semitones is heard at 1.12 kHz, and the export must agree.
        eq = render_fx.lanes[i]
        filters.append(f"[{i}:a]{eq}{_pitch_filter(lane.pitch)}{delay}volume={lane.gain:.6f}[a{i}]")
    if click_lane is not None:
        # The mono click to stereo at unity, as the app plays it (#833).
        filters.append(
            f"[{stem_count}:a]volume={click_lane.gain:.6f},{CLICK_TO_STEREO}[a{stem_count}]"
        )
    n = stem_count + (1 if click_lane is not None else 0)
    # Master EQ on the sum (click included, as live: the click joins upstream
    # of the master slot), then the limiter last.
    master = render_fx.master
    out_label = "[mix]"
    codec = MIXDOWN_CODECS[ext]
    # Resample to the user's chosen export rate (default 44.1 kHz = the stem rate,
    # so a no-op unless changed). Applies to every audio container -- some hardware
    # samplers reject anything but a specific rate.
    rate = ["-ar", str(get_export_sample_rate())]

    def build_cmd(trim_db: float = 0.0, _alt: bool = False) -> list[str]:
        # trim_db > 0 only on a mastered lossy export the encoder pushed over
        # the ceiling (_render_master_verified).
        end_chain = f"{master}{limiter}{trim_filter(trim_db)}[mix]"
        if n > 1:
            labels = "".join(f"[a{i}]" for i in range(n))
            mix = f"{labels}amix=inputs={n}:normalize=0,{end_chain}"
        else:
            mix = f"[a0]{end_chain}"
        return [
            *cmd,
            "-filter_complex",
            ";".join([*filters, mix]),
            "-map",
            out_label,
            *post_seek,
            *codec,
            *rate,
        ]

    context = f"mixdown job={job_id} ext={ext} stems={stems}"
    if plan is not None and master_settings is not None and lossy:
        rendered, out = await _render_master_verified(
            build_cmd, f".{ext}", context, master_settings.ceiling, cache_path, cache_key
        )
        headers.update(master_headers(plan, out))
        return FileResponse(rendered, media_type=media_type, headers=headers)
    cmd = build_cmd()
    if ext in _SEEKABLE_OUTPUT_EXTS:
        # Rendered to a file so the muxer can seek back and finish its header
        # (#458). It lands in the cache, so nothing is deleted afterwards --
        # this is the same file a later identical request will be served from
        # by the cache-hit branch above.
        rendered = await _render_to_file(cmd, f".{ext}", context=context, cache_path=cache_path)
        return FileResponse(rendered, media_type=media_type, headers=headers)
    # ogg needs no back-patching, so it keeps streaming and the client sees
    # bytes as soon as ffmpeg produces them.
    return StreamingResponse(
        _stream_ffmpeg([*cmd, "pipe:1"], context=context, cache_path=cache_path),
        media_type=media_type,
        headers=headers,
    )


@router.get("/jobs/{job_id}/mixdown/loudness")
async def get_mixdown_loudness(
    request: Request,
    job_id: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    pitches: str = Query(default="", max_length=128, description="As for mixdown"),
    fx: str = Query(default="", max_length=FX_PARAM_MAX_CHARS, description="As for mixdown"),
    master: str = Query(..., max_length=MASTER_PARAM_MAX_CHARS, description="As for mixdown"),
    lossy: bool = Query(default=False, description="Plan for a lossy export's ceiling"),
    lanes: str | None = Query(
        default=None, max_length=LANES_PARAM_MAX_CHARS, description=LANES_QUERY_DESCRIPTION
    ),
) -> dict:
    """The measured master of this mix (the same plan the export renders):
    loudness before, after the compressor and after the master, the gain and
    the compressor threshold. The studio's Master readout and its live
    preview read it. The whole song is always measured, so there is no
    start/end; a mix measured once is answered from the cache.

    The studio asks again every time the mix settles and drops the older
    request, so a measurement whose client has gone away is stopped (its
    ffmpeg killed) rather than left to delay the one that is wanted."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    names, parsed_gains = _parse_lane_gains(stems, gains)
    parsed_pitches = _parse_pitches(pitches, names)
    fx_req = parse_fx_query(fx)
    settings = parse_master_query(master)
    if settings is None:
        raise HTTPException(status_code=422, detail="master: required")
    lane_set = parse_lanes_param(lanes)
    if any(parsed_pitches) and not await asyncio.to_thread(_rubberband_available):
        raise HTTPException(
            status_code=422,
            detail="this ffmpeg build cannot transpose (no rubberband filter)",
        )
    render_lanes, _unpitched = _expand_render_lanes(
        job_id, names, parsed_gains, parsed_pitches, lanes=lane_set
    )
    paths = [lane.path for lane in render_lanes]
    render_fx = await asyncio.to_thread(
        build_render_fx, [lane.name for lane in render_lanes], paths, fx_req
    )
    task = asyncio.create_task(
        master_plan(
            job_id,
            paths,
            [
                _lane_chain(render_fx.lanes[i], lane.pitch, lane.gain)
                for i, lane in enumerate(render_lanes)
            ],
            render_fx.master,
            settings,
            lossy,
            _take_versions(job_id, names),
        )
    )
    while not task.done():
        await asyncio.wait({task}, timeout=_DISCONNECT_POLL_SEC)
        if not task.done() and await request.is_disconnected():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Nobody is listening; the status is for the log.
            raise HTTPException(status_code=499, detail="client went away")
    plan, _rate = task.result()
    return master_plan_view(plan)


def _title_slug(title: str | None) -> str:
    """Sanitize a song title into a filename-safe slug (matches the frontend).

    Returns "" for a title that survives sanitizing as empty, so callers can
    decide whether to substitute a fallback or drop the prefix entirely. The
    output is restricted to [A-Za-z0-9_], which also makes it safe to use as a
    ZIP member name -- no separators, no traversal.
    """
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", title or "")
    return re.sub(r"_{2,}", "_", safe).strip("_")[:80].strip("_")


def _safe_title(title: str | None) -> str:
    """Slug for a whole-archive filename, where an empty title still needs a name."""
    return _title_slug(title) or "stems"


@router.get("/jobs/{job_id}/video.mp4", response_model=None)
async def get_video_mixdown(
    job_id: str,
    stems: str = Query(..., description="Comma-separated lane names to sum"),
    gains: str = Query(..., description="Comma-separated linear gains, parallel to stems"),
    pitches: str = Query(
        default="",
        max_length=128,
        description="Comma-separated semitone shifts parallel to stems (-6..6). Empty = none.",
    ),
    click: bool = Query(default=False, description="Mix the click track into the export"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
    click_sound: str = Query(
        default=DEFAULT_CLICK_SOUND, max_length=32, description=CLICK_SOUND_QUERY_DESCRIPTION
    ),
    fx: str = Query(
        default="",
        max_length=FX_PARAM_MAX_CHARS,
        description="The studio EQ, as for mixdown. Empty means no EQ.",
    ),
    master: str = Query(
        default="",
        max_length=MASTER_PARAM_MAX_CHARS,
        description="The master, as for mixdown. Empty means no master.",
    ),
    lanes: str | None = Query(
        default=None, max_length=LANES_PARAM_MAX_CHARS, description=LANES_QUERY_DESCRIPTION
    ),
) -> StreamingResponse:
    """Mux a fresh audio mixdown of the current mixer state with the job's preserved
    video into an MP4 (issue #219). Mirrors get_mixdown's audio graph (encoded
    as AAC) and stream-copies video.mp4 -- the silent video kept from an .mp4 upload
    or the real video stream downloaded for a YouTube job. 404 when the job has no
    video (SoundCloud / plain audio uploads).

    Streamed as fragmented MP4 (frag_keyframe+empty_moov) since the output pipe is
    not seekable -- +faststart would require a seekable file. The full song is
    exported; no region trim, to avoid A/V drift from stream-copy seeking."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")

    video_path = (JOBS_DIR / job_id / "video.mp4").resolve()
    if not video_path.is_file() or not video_path.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(status_code=404, detail="no video track for this job")

    names, parsed_gains = _parse_lane_gains(stems, gains)
    parsed_pitches = _parse_pitches(pitches, names)
    fx_req = parse_fx_query(fx)
    master_settings = parse_master_query(master)
    # Same refusal as the audio export: a video whose audio came back in the
    # original key would be the same complaint reported again (#592).
    if any(parsed_pitches) and not await asyncio.to_thread(_rubberband_available):
        raise HTTPException(
            status_code=422,
            detail="this ffmpeg build cannot transpose (no rubberband filter)",
        )
    # Validates job_id (404), job done (404), and path traversal (404) per stem,
    # and rebuilds a transposed "original" lane from its parts.
    render_lanes, _unpitched = _expand_render_lanes(
        job_id, names, parsed_gains, parsed_pitches, lanes=parse_lanes_param(lanes)
    )
    paths = [lane.path for lane in render_lanes]

    # Click is one more audio input. It must be appended before the video input
    # so the audio indices the filter graph references stay contiguous from 0.
    click_lane = await asyncio.to_thread(
        _click_lane,
        job_id,
        click,
        click_mult,
        click_accent,
        click_gain,
        sound=check_click_sound(click_sound),
    )
    if click_lane is not None:
        paths = [*paths, click_lane[0]]
        render_lanes = [*render_lanes, _RenderLane(click_lane[0], click_lane[1], 0, "")]
    render_fx = await asyncio.to_thread(
        build_render_fx, [lane.name for lane in render_lanes], paths, fx_req
    )
    # The master, measured on the music lanes (the click is the last input and
    # is left out of the measurement), replaces the lossy export limiter.
    limiter = EXPORT_LIMITER_LOSSY
    plan: MasterPlan | None = None
    if master_settings is not None:
        music = len(render_lanes) - (1 if click_lane is not None else 0)
        plan, rate_in = await master_plan(
            job_id,
            paths[:music],
            [
                _lane_chain(render_fx.lanes[i], lane.pitch, lane.gain)
                for i, lane in enumerate(render_lanes[:music])
            ],
            render_fx.master,
            master_settings,
            True,
            _take_versions(job_id, names),
        )
        limiter = plan_chain(plan, rate_in)

    cmd: list[str] = [ffmpeg_executable(), "-nostdin", "-loglevel", "error"]
    for p in paths:
        cmd += ["-i", str(p)]
    cmd += ["-i", str(video_path)]
    video_idx = len(paths)
    # Per-lane gain then amix (normalize=0 keeps levels faithful). A single audible
    # lane skips amix (a 1-input amix is a no-op), matching get_mixdown.
    # EQ first in each lane, as in get_mixdown; the click lane has none, and
    # goes to stereo at unity as in get_mixdown (#833).
    click_idx = len(render_lanes) - 1 if click_lane is not None else -1
    filters = [
        f"[{i}:a]{render_fx.lanes[i]}{_pitch_filter(lane.pitch)}volume={lane.gain:.6f}"
        f"{',' + CLICK_TO_STEREO if i == click_idx else ''}[a{i}]"
        for i, lane in enumerate(render_lanes)
    ]
    n = len(paths)
    # Same limiter as an MP3 or OGG mixdown: this is the same mix, encoded
    # lossy (AAC), so it must not clip where the audio export no longer does.
    # The master EQ goes before it.
    master = render_fx.master
    out_label = "[mix]"

    def build_cmd(trim_db: float = 0.0, alt: bool = False) -> list[str]:
        end_chain = f"{master}{limiter}{trim_filter(trim_db)}[mix]"
        if n > 1:
            labels = "".join(f"[a{i}]" for i in range(n))
            mix = f"{labels}amix=inputs={n}:normalize=0,{end_chain}"
        else:
            mix = f"[a0]{end_chain}"
        return [
            *cmd,
            "-filter_complex",
            ";".join([*filters, mix]),
            "-map",
            out_label,
            "-map",
            f"{video_idx}:v",
            "-c:v",
            "copy",
            *(AAC_ENCODE_ARGS_ALT if alt else AAC_ENCODE_ARGS),
            "-shortest",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-f",
            "mp4",
        ]

    filename = f"{_safe_title(job.title)}_video.mp4"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    context = f"video-mux job={job_id} stems={stems}"
    if plan is not None and master_settings is not None:
        # A mastered video is rendered to a file first, so its decoded true
        # peak can be checked against the ceiling (_render_master_verified),
        # then sent and removed.
        rendered, out = await _render_master_verified(
            build_cmd, ".mp4", context, master_settings.ceiling, None, None, True
        )
        headers.update(master_headers(plan, out))
        return FileResponse(
            rendered,
            media_type="video/mp4",
            headers=headers,
            background=BackgroundTask(_unlink_later, rendered),
        )
    return StreamingResponse(
        _stream_ffmpeg([*build_cmd(), "pipe:1"], context=context),
        media_type="video/mp4",
        headers=headers,
    )


def _arcname(prefix: str, name: str, ext: str) -> str:
    """ZIP member name for one stem. Prefixed with the song slug so the files stay
    identifiable once extracted into a project folder alongside other songs'
    stems (#336); unprefixed when the song has no usable title."""
    return f"{prefix}_{name}.{ext}" if prefix else f"{name}.{ext}"


def _build_stems_zip(
    sources: list[tuple[str, Path]], fmt: str, dest: Path, prefix: str = ""
) -> None:
    """Blocking: write the stems into a ZIP. A file already in `fmt` is stored
    as-is (a WAV in a WAV zip, and an MP3-stored stem in an MP3 zip, which is
    not encoded a second time); anything else is transcoded per stem via
    ffmpeg. ZIP_STORED throughout - audio doesn't meaningfully compress, and
    STORED keeps the build fast. Runs in a thread."""
    if all(p.suffix.lower() == f".{fmt}" for _name, p in sources):
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            for name, p in sources:
                zf.write(p, arcname=_arcname(prefix, name, fmt))
        return
    encode = _ENCODE_ARGS[fmt]
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for name, p in sources:
            if p.suffix.lower() == f".{fmt}":
                zf.write(p, arcname=_arcname(prefix, name, fmt))
                continue
            out = os.path.join(td, f"{name}.{fmt}")
            cmd = [
                ffmpeg_executable(),
                "-nostdin",
                "-loglevel",
                "error",
                "-i",
                str(p),
                *encode,
                "-f",
                fmt,
                out,
            ]
            proc = subprocess.run(  # noqa: S603 — list args, no shell, trusted ffmpeg
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=TIMEOUT_FFMPEG,
            )
            if proc.returncode != 0:
                tail = proc.stderr[-2000:].decode("utf-8", "replace")
                raise RuntimeError(f"ffmpeg failed for {name}: {tail}")
            zf.write(out, arcname=_arcname(prefix, name, fmt))


class _ClickStem(NamedTuple):
    """What the stems zip's click entry is rendered from (#805): the same
    choices the click in a mix export is, so the two click identically."""

    grid: dict
    gain: float
    multiplier: float
    accent_mode: int
    groups: list[int] | None
    sound: str


def _render_click_stem(click: _ClickStem, reference: Path, fmt: str, workdir: Path) -> Path | None:
    """Blocking: the isolated click track for the stems zip, in `fmt`.

    Sample-aligned with the stems by construction: rendered from the beat grid
    in song time from sample 0 (the stems' own origin), at the reference
    stem's sample rate and channel count, and exactly its decoded length in
    frames. An MP3-stored stem decodes to the WAV's frame count (#783), which
    is what soundfile reports for it here.

    Gain: what the click contributes to a mix export, so the zip's files sum
    to that mix at unity stem gains: the click's export level (`click_gain`),
    and the mono click on both channels at unity (CLICK_TO_STEREO), as the
    mix graph does and as the app plays it (#833).

    The end of the chain is the export limiter for the format, as for every
    exported mix: bit-transparent below its -1 dBFS ceiling for WAV and FLAC
    (a click at the default 0.6 never reaches it), and the true-peak one for
    MP3 and OGG. apad/atrim pin the length after it: the lossy limiter's
    oversampling round trip may otherwise move the end by a few samples.

    Returns None when the grid has no beats."""
    beats = click.grid.get("beats") or []
    if not beats:
        return None
    info = sf.info(str(reference))
    frames = int(info.frames)
    rate = int(info.samplerate)
    channels = int(info.channels)
    raw = render_click_wav(
        workdir / "click_mono.wav",
        beats,
        click.grid.get("bars") or [],
        frames / rate,
        sample_rate=rate,
        multiplier=click.multiplier,
        accent_mode=click.accent_mode,
        groups=click.groups,
        sound=click.sound,
        frames=frames,
    )
    if raw is None:
        return None
    # To the stems' layout by the same conversion the mix export's graph
    # applies to the click (the mono click on both channels at unity, #833),
    # so the click in the zip is exactly the click in the mix.
    upmix = f"{CLICK_TO_STEREO}," if channels == 2 else ""
    chain = (
        f"volume={max(0.0, min(4.0, click.gain)):.6f},{upmix}"
        f"{export_limiter(lossy=fmt in _LOSSY_MIXDOWN_EXTS)},aresample={rate},"
        f"apad=whole_len={frames},atrim=end_sample={frames}"
    )
    out = workdir / f"{CLICK_STEM_NAME}.{fmt}"
    cmd = [
        ffmpeg_executable(),
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(raw),
        "-af",
        chain,
        *_ENCODE_ARGS[fmt],
        "-f",
        fmt,
        str(out),
    ]
    proc = subprocess.run(  # noqa: S603 - list args, no shell, trusted ffmpeg
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_FFMPEG,
    )
    if proc.returncode != 0:
        tail = proc.stderr[-2000:].decode("utf-8", "replace")
        raise RuntimeError(f"ffmpeg failed for the click stem: {tail}")
    return out


def _build_stems_zip_with_click(
    sources: list[tuple[str, Path]],
    fmt: str,
    dest: Path,
    prefix: str,
    click: _ClickStem | None,
) -> None:
    """Blocking: _build_stems_zip, plus the click entry when one is asked for.
    The first source is the length reference: every stem and take shares the
    song's frame count."""
    if click is None:
        _build_stems_zip(sources, fmt, dest, prefix)
        return
    with tempfile.TemporaryDirectory() as td:
        path = _render_click_stem(click, sources[0][1], fmt, Path(td))
        extra = [(CLICK_STEM_NAME, path)] if path is not None else []
        _build_stems_zip([*sources, *extra], fmt, dest, prefix)


@router.get("/jobs/{job_id}/stems/all.zip")
async def get_all_stems_zip(
    job_id: str,
    fmt: str = Query(default="wav", alias="format"),
    stems: str | None = Query(default=None, description="Comma-separated stems; default all"),
    click: bool = Query(default=False, description="Add the click track as its own file"),
    click_mult: float = Query(default=1.0, description="Click rate: 0.5, 1 or 2"),
    click_accent: int = Query(default=-1, ge=-1, le=32, description="-1 auto, 0 off, N per bar"),
    click_gain: float = Query(default=0.6, ge=0, le=4, description="Click level"),
    click_groups: str = Query(
        default="",
        max_length=64,
        description='Beat grouping for an explicit meter, e.g. "3+2+2". Blank uses the default.',
    ),
    click_sound: str = Query(
        default=DEFAULT_CLICK_SOUND, max_length=32, description=CLICK_SOUND_QUERY_DESCRIPTION
    ),
) -> FileResponse:
    """Bundle the requested stems into a single ZIP, named after the song.

    `stems` is the active subset selected in the DAW (whitelisted). When omitted,
    every available stem is included.

    `click` adds the click track as one more file, `<song>_click.<format>`
    (#805), rendered from the song's beat grid (the user's edits when there
    are any) with the same rate, accents, grouping, level and sound as the
    click in a mix export. It starts at the song's first sample like every
    stem, with the stems' length, rate and channels, so it lines up with them
    in any player. A count-in is not part of it: the stems in a zip are not
    delayed (an MP3 stem is stored without being re-encoded), so a count-in
    could only sit before the first sample, which no stem has. The count-in
    stays a mix-export option. Without `click` the zip is exactly as before."""
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    if fmt not in ("wav", "mp3", "flac", "ogg"):
        raise HTTPException(status_code=422, detail="format must be 'wav', 'mp3', 'flac', or 'ogg'")
    check_click_sound(click_sound)
    job = registry_get(job_id)
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="job not ready")
    # An encoder this ffmpeg lacks (libvorbis is optional, #824): refused
    # here rather than as a 500 after the first stem fails to encode.
    unavailable = await asyncio.to_thread(unavailable_detail, fmt)
    if unavailable:
        raise HTTPException(status_code=422, detail=unavailable)

    # Resolve the requested subset (whitelisted) or fall back to all stems.
    # Takes go in only when asked for by id: "all stems" means the separation.
    all_names = (*STEM_NAMES, *EXTRA_STEM_NAMES)
    take_names: list[str] = []
    if stems:
        requested = {s for s in stems.split(",") if s}
        take_names = sorted(n for n in requested if _is_recorded_lane(n))
        requested -= set(take_names)
        if not requested <= set(all_names):
            raise HTTPException(status_code=422, detail="unknown stem requested")
        if "vocals" in requested and _VOCAL_DECOMPOSITION_NAMES & requested:
            raise HTTPException(
                status_code=422,
                detail="vocals cannot be combined with lead_vocals/backing_vocals (same signal)",
            )
        wanted = [name for name in all_names if name in requested]
    else:
        wanted = list(all_names)

    jobs_root = JOBS_DIR.resolve()
    stems_dir = (JOBS_DIR / job_id / "stems").resolve()
    if not stems_dir.is_dir() or not stems_dir.is_relative_to(jobs_root):
        raise HTTPException(status_code=404, detail="stems not found")

    sources: list[tuple[str, Path]] = []
    for name in wanted:
        p = stem_path(stems_dir, name).resolve()
        if p.is_file() and p.is_relative_to(jobs_root):
            sources.append((name, p))
    labels = {t.get("id"): t.get("label") for t in job.takes}
    labels.update({t.get("id"): t.get("name") for t in job.tracks})
    for lane in take_names:
        path = _take_lane_path(job_id, lane)
        # Named for the label (take) or name (track) the user gave it, with
        # the id to keep two called "Take 1" apart. _title_slug keeps it to
        # [A-Za-z0-9_].
        label = _title_slug(labels.get(lane))
        sources.append((f"{label}_{lane}" if label else lane, path))
    if not sources:
        raise HTTPException(status_code=404, detail="no stems found")

    click_stem: _ClickStem | None = None
    if click:
        grid = await asyncio.to_thread(_read_beat_grid, job_id)
        if grid is not None:
            click_stem = _ClickStem(
                grid,
                click_gain,
                click_mult if click_mult in (0.5, 1.0, 2.0) else 1.0,
                click_accent,
                _parse_groups(click_groups, click_accent),
                click_sound,
            )

    fd, tmp = tempfile.mkstemp(prefix="stemdeck_zip_", suffix=".zip")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        await asyncio.to_thread(
            _build_stems_zip_with_click,
            sources,
            fmt,
            tmp_path,
            _title_slug(job.title),
            click_stem,
        )
    except Exception:
        tmp_path.unlink(missing_ok=True)
        logger.exception("failed to build stems zip for job %s", job_id)
        raise HTTPException(status_code=500, detail="failed to build archive") from None

    filename = f"{_safe_title(job.title)}_stems.zip"
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )
