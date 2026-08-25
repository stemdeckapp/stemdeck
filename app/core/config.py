import json
import os
import re
import sys
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def available_torch_devices() -> list[str]:
    """Compute devices this machine can actually use, best-first. CPU is always
    present; cuda/mps depend on the hardware + installed torch build. The
    Settings UI uses this to disable options that aren't available/detected so
    a user can't pick an impossible device."""
    devices: list[str] = []
    try:
        import torch

        if torch.cuda.is_available():
            devices.append("cuda")
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            devices.append("mps")
    except ImportError:
        pass
    devices.append("cpu")
    return devices


def detect_torch_device() -> str:
    """Best available Torch device for Demucs by hardware probe: cuda > mps >
    cpu. Apple Silicon needs the explicit MPS check -- demucs's CLI default is
    "cuda if available else cpu" and macOS has no CUDA, leaving the integrated
    GPU idle and processing 3-5x slower than necessary.

    User-facing device selection lives in app.core.settings (demucs_device,
    default "auto" -> this probe); the STEMDECK_DEMUCS_DEVICE env var seeds
    that setting's default so env-based deployments keep working."""
    return available_torch_devices()[0]


ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = ROOT / "static"
STEM_NAMES: tuple[str, ...] = ("vocals", "drums", "bass", "guitar", "piano", "other")
# Produced only when a job opts into the lead/backing vocal split (best-effort,
# additive -- see app/pipeline/vocal_split.py). Kept out of STEM_NAMES itself:
# selected_stems defaults, the "Original" complement-track math, and the
# GET /api/config contract all assume "the 6 Demucs stems" and must not change
# just because a job happened to request this extra pass.
EXTRA_STEM_NAMES: tuple[str, ...] = ("lead_vocals", "backing_vocals")
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Runtime knobs -- env-backed so Docker / desktop packaging / local dev can
# tune without a code edit. STEMDECK_DATA_DIR is the portable app root for
# mutable runtime data; when unset, dev behavior remains the repo-local jobs/
# folder.
PORTABLE_DATA_DIR_ENABLED = bool(os.environ.get("STEMDECK_DATA_DIR", "").strip())
DATA_DIR = _env_path("STEMDECK_DATA_DIR", ROOT)


def _stored_jobs_dir() -> Path | None:
    """The stems location the user picked in Settings, if any.

    Read straight out of settings.json rather than through app.core.settings,
    which imports this module -- and it has to happen here because JOBS_DIR is
    bound at import time across the app. Any problem reading it falls through to
    the default: a library that quietly moves is far worse than one that ignores
    a corrupt preference.
    """
    try:
        raw = json.loads((DATA_DIR / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = raw.get("jobs_dir") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value).expanduser()
    # Only honour a folder that is actually there. It existed when the user
    # picked it, so a missing one means the disk holding it is not mounted --
    # and ensure_runtime_dirs would otherwise happily mkdir it, which on macOS
    # creates a real directory at the mount point on the boot disk and can stop
    # the drive mounting under its own name later. Falling back to the default
    # leaves the library findable again as soon as the disk is plugged in.
    return path if path.is_dir() else None


# Where extracted stems live. Precedence, most explicit first:
#   1. STEMDECK_JOBS_DIR         -- a deployment that pinned it (Docker, Unraid,
#                                   CI, tests). Wins over everything: a mounted
#                                   volume is not the user's to relocate.
#   2. settings.json             -- what the user chose in Settings (#354).
#                                   Desktop only; the endpoint that writes it
#                                   refuses to run anywhere else.
#   3. STEMDECK_DEFAULT_JOBS_DIR -- the desktop shell's default
#                                   (~/Documents/StemDeck/jobs). Passed as a
#                                   default rather than a pin, so 2 can win.
#   4. DATA_DIR/jobs             -- portable default
#   5. <repo>/jobs               -- plain dev checkout
JOBS_DIR = _env_path(
    "STEMDECK_JOBS_DIR",
    _stored_jobs_dir()
    or _env_path(
        "STEMDECK_DEFAULT_JOBS_DIR",
        (DATA_DIR / "jobs") if PORTABLE_DATA_DIR_ENABLED else (ROOT / "jobs"),
    ),
)
CACHE_DIR = _env_path("STEMDECK_CACHE_DIR", DATA_DIR / "cache")
DOWNLOADS_DIR = _env_path("STEMDECK_DOWNLOADS_DIR", DATA_DIR / "downloads")
MODELS_DIR = _env_path("STEMDECK_MODELS_DIR", DATA_DIR / "models")
LOGS_DIR = _env_path("STEMDECK_LOGS_DIR", DATA_DIR / "logs")
FFMPEG_DIR = _env_path("STEMDECK_FFMPEG_DIR", DATA_DIR / "ffmpeg")
FFMPEG_BIN = _env_path(
    "STEMDECK_FFMPEG",
    FFMPEG_DIR / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"),
)
FFPROBE_BIN = _env_path(
    "STEMDECK_FFPROBE",
    FFMPEG_DIR / ("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"),
)
# JavaScript runtime for yt-dlp's YouTube challenge solver (#432). Portable
# builds drop a binary here because nothing is on PATH in a portable install;
# Docker ships deno on PATH and source checkouts have whatever the developer
# installed, so both leave this directory absent and yt-dlp resolves its own.
JS_RUNTIME_DIR = _env_path("STEMDECK_JS_RUNTIME_DIR", DATA_DIR / "jsruntime")

# Ordered by yt-dlp's own JS challenge provider preference (deno 1000 >
# node 900 > quickjs 850), so a build that ships more than one still gets the
# solver yt-dlp would have picked itself.
_JS_RUNTIME_BINARIES = (("deno", "deno"), ("node", "node"), ("quickjs", "qjs"))


def bundled_js_runtime() -> tuple[str, Path] | None:
    """The JS runtime shipped with this install, as (yt-dlp name, path).

    None when nothing is bundled, which is the normal case outside a portable
    build -- yt-dlp then falls back to its own PATH lookup. Never raises: a
    missing or unreadable directory just means "not bundled".
    """
    try:
        if not JS_RUNTIME_DIR.is_dir():
            return None
        suffix = ".exe" if sys.platform.startswith("win") else ""
        for name, stem in _JS_RUNTIME_BINARIES:
            exe = JS_RUNTIME_DIR / f"{stem}{suffix}"
            if exe.is_file():
                return name, exe
    except OSError:
        return None
    return None


DEMUCS_MODEL = os.environ.get("STEMDECK_DEMUCS_MODEL", "htdemucs_6s").strip() or "htdemucs_6s"
MAX_DURATION_SEC = max(60, _env_int("STEMDECK_MAX_DURATION_SEC", 1200))  # 20 min default
JOB_TTL_SECONDS = max(300, _env_int("STEMDECK_JOB_TTL_SECONDS", 24 * 3600))  # 24 h default
# TTL for quarantined failed-job dirs (jobs/failed/<id>, kept for diagnostics).
# Swept unconditionally -- even deployments with a persistent library must not
# accumulate failure evidence forever.
FAILED_TTL_SECONDS = max(3600, _env_int("STEMDECK_FAILED_TTL_SECONDS", 7 * 24 * 3600))  # 7 d
# Depth of the import queue: jobs waiting for their turn, not counting the one
# running. Counted separately by kind, because the two cost wildly different
# things. A queued upload holds its source file on disk for the whole wait, so
# 20 of them is already an 8 GB worst case. A queued URL holds nothing at all --
# it downloads when its turn comes -- so the only real cost is a registry
# record, and a 50-track playlist should not have to be imported in batches.
MAX_PENDING_UPLOAD_JOBS = max(1, min(200, _env_int("STEMDECK_MAX_PENDING_JOBS", 20)))
MAX_PENDING_URL_JOBS = max(1, min(500, _env_int("STEMDECK_MAX_PENDING_URL_JOBS", 200)))
# Ceiling on how much of a playlist one import may expand to. Enforced twice:
# as yt-dlp's playlistend so nothing beyond it is ever fetched, and again after
# normalization. Unrelated to MAX_PENDING_JOBS, which bounds the queue itself --
# a playlist larger than the queue has room for fills what it can and says so.
PLAYLIST_MAX_ITEMS = max(1, min(200, _env_int("STEMDECK_PLAYLIST_MAX_ITEMS", 50)))
TIMEOUT_FFMPEG = _env_int("STEMDECK_TIMEOUT_FFMPEG", 300)
TIMEOUT_ANALYZE = _env_int("STEMDECK_TIMEOUT_ANALYZE", 120)
TIMEOUT_DEMUCS_STALL = _env_int("STEMDECK_TIMEOUT_DEMUCS_STALL", 1800)
# On-demand lead/backing vocal split (#275). UVR-MDX-NET Karaoke 2 is an
# officially-distributed UVR-project model (MIT + credit-to-UVR per the
# audio-separator README) -- the default. STEMDECK_KARAOKE_MODEL lets a
# deployment swap the checkpoint (e.g. to a roformer model) without a code
# change; see docs/models.md for the license audit behind this default.
VOCAL_SPLIT_MODEL = os.environ.get("STEMDECK_KARAOKE_MODEL", "").strip() or "UVR_MDXNET_KARA_2.onnx"
# A first run downloads the checkpoint (hundreds of MB); generous default so a
# slow connection isn't mistaken for a stall.
TIMEOUT_VOCAL_SPLIT = _env_int("STEMDECK_TIMEOUT_VOCAL_SPLIT", 1800)
# Beat-grid stage decodes the whole drums stem (not the 180 s analyze window),
# so it gets its own, larger budget.
TIMEOUT_BEATGRID = _env_int("STEMDECK_TIMEOUT_BEATGRID", 300)

# Beat-grid analysis parameters. 22050 Hz is plenty for onset detection (the
# percussive energy that matters lives well under 11 kHz) and keeps the decode
# cheap; hop 512 gives ~23 ms grid resolution, the librosa default pairing.
# Which beat detector the grid stage uses.
#   "auto"    -- the neural model when it is importable and its weights are
#                available, otherwise librosa. The shipping default.
#   "model"   -- require the model; fail the stage rather than silently
#                degrading. For diagnosing packaging problems.
#   "librosa" -- force the classic tracker. Kept because it needs no model
#                download and no network.
#
# The model is not a nicety. librosa's `beat_track` carries a lognormal tempo
# prior centred on 120 BPM, so a 180 BPM punk track resolves to 90 and the
# click plays half-time -- measured on Green Day "Welcome To Paradise"
# (detected 90.0, true ~180) and reproduced on synthetic punk at 176 (detected
# 117.5). No confidence metric catches it: a half-time grid puts a real drum
# hit under every beat and scores 94%.
BEAT_DETECTOR = os.environ.get("STEMDECK_BEAT_DETECTOR", "").strip().lower() or "auto"
if BEAT_DETECTOR not in ("auto", "model", "librosa"):
    BEAT_DETECTOR = "auto"
# beat_this checkpoint name; resolved through torch.hub, so it lands under
# TORCH_HOME (which configure_portable_environment points at MODELS_DIR).
BEAT_MODEL_CHECKPOINT = os.environ.get("STEMDECK_BEAT_MODEL", "").strip() or "final0"

BEATGRID_SR = 22050
BEATGRID_HOP = 512
# Interior gaps: the model only emits beats where it hears them, so a section
# with no drums leaves a hole -- 5.26 s on the Welcome To Paradise breakdown,
# where the drums stem RMS drops to 0.008 against 0.098 for the whole stem. A
# click that stops for five seconds reads as a bug, so gaps that are a clean
# multiple of the local period are subdivided at that period. Both endpoints
# are real detected beats, so the filled beats cannot drift out of the section.
# How far the implied period (gap / inserted-beat count) may sit from the local
# period. Judged per inserted beat, not across the whole gap. 0.15 fills the
# 5.26 s Welcome To Paradise hole (residual 3.1%) while refusing a 1.54x
# interval, which at 0.35 was being split into two beats 23% too fast -- audible
# as a stumble rather than a repair.
BEATGRID_GAP_FILL_MAX_RESIDUAL = 0.15
BEATGRID_GAP_LOCAL_WINDOW = 8  # intervals either side used for the local period
# Gap filling, the grid-consistency pass and edge extension all assume a locally
# regular pulse. On genuinely irregular material they do harm: on Dance of
# Eternity they inserted 85 beats and "corrected" 269 of 1059, wrecking a grid
# the detector had tracked adequately.
#
# The gate is interquartile range over median interval, NOT the coefficient of
# variation. A single drum-free section inflates cv enormously while the pulse
# either side is perfectly steady -- Welcome To Paradise measures cv 0.535 and
# IQR/median 0.054. Measured: 0.054 (regular punk with one 5 s hole), 0.025
# (steady rock), 0.531 (108 time-signature changes). A 10x separation, so the
# threshold sits comfortably in the middle.
BEATGRID_MAX_IRREGULARITY = 0.20
# Phase check. A tracker can land the right tempo on the wrong half of the beat,
# putting every click in a gap between hits -- measured on a bare repeated-kick
# pattern where the whole grid sat in silence and refinement rejected all 45
# beats for having no transient to snap to. Shifting the grid by half a beat is
# accepted only when it improves onset support by this factor, so a grid that is
# already correct (where off-beat hi-hats give the wrong phase *some* support)
# is never flipped.
BEATGRID_PHASE_FLIP_RATIO = 2.0
# Beat times out of librosa land on the 512-hop grid, i.e. quantised to ~23 ms.
# A click carrying that error flams audibly against the drums, so beats are
# refined against a much finer onset envelope (~2.9 ms) and then parabolically
# interpolated to sub-frame precision. The search window is deliberately under
# half a coarse hop: wide enough to correct quantisation, far too narrow to
# drag a beat onto a neighbouring sixteenth.
BEATGRID_REFINE_HOP = 32
# Temporal precision is set by the analysis *window*, not the hop: onset_strength
# defaults to a 2048-sample (93 ms) FFT, which smears a transient far too much to
# localise it. 512 samples (23 ms) with 64 mel bands measured a 0.19 ms standard
# deviation against synthetic transients, versus 4.8 ms at the default. Dropping
# n_mels alongside n_fft avoids empty mel filters at this resolution.
BEATGRID_REFINE_NFFT = 512
BEATGRID_REFINE_MELS = 64
# Must comfortably exceed half the coarse hop (~11.6 ms) or the true peak falls
# outside the search window and refinement makes things *worse* than it found
# them -- measured 10.9 ms worst-case error at a 20 ms window versus 2.0 ms at
# 30 ms, because the coarse quantisation phase beats against the beat interval
# and periodically pushes the true peak past the edge. 30 ms stays under a
# sixteenth note (50 ms) even at the 300 BPM ceiling, so it can never snap to a
# neighbouring subdivision. Widening beyond 30 ms measured identically, so this
# is the knee, not a guess.
BEATGRID_REFINE_WINDOW = 0.030  # seconds, floor for the search window
# Both the search window and the move limit scale with the beat interval, because
# what they have to correct is detector error, and that is a fraction of a beat
# rather than a fixed number of milliseconds. Fixed 30 ms/29 ms values were sized
# for librosa's 23 ms hop and left a 46 ms model error uncorrectable at 90 BPM.
# Ceilings keep the window under a sixteenth note at any plausible tempo.
BEATGRID_REFINE_WINDOW_OF_BEAT = 0.15
BEATGRID_REFINE_WINDOW_MAX = 0.100
BEATGRID_REFINE_MOVE_OF_BEAT = 0.10
BEATGRID_REFINE_MOVE_MIN = 0.029
BEATGRID_REFINE_MOVE_MAX = 0.070
# Peak-picking threshold for the onset list the grid editor snaps to, as a
# multiple of this percentile of the onset envelope. Tuned against an
# independent spectral-flux detector on two real tracks: this pair lands within
# 1.01-1.13x of its onset count, while a lower bar detects 3-5x too many and
# would let a dragged beat snap to noise. The percentile must stay high --
# the envelope is mostly zeros, so the 65th percentile is literally 0.
BEATGRID_ONSET_PERCENTILE = 90
BEATGRID_ONSET_DELTA_MULT = 1.5
# Hard cap on onsets shipped to the client, dropped weakest-first.
BEATGRID_MAX_ONSETS = 6000
# The same bound has to apply to *interpolated* beats, not just snapped ones,
# and as a fraction of a beat rather than of the hop. Beats in a drum-free
# section have no peak to snap to, so they are positioned by interpolation over
# beat index -- and without a clamp that interpolation happily redistributes a
# 5 s silent gap, which measured a 4001 ms displacement on one beat of Welcome
# To Paradise (138x the 29 ms snap limit). Refinement sharpens where a beat
# sits; it must never decide which pulse a beat belongs to.
BEATGRID_REFINE_MAX_INTERP_FRAC = 0.25
# An onset peak must clear this percentile of the whole envelope to count as a
# real hit. Below it, the beat is treated as unplayed and its position is
# interpolated from its neighbours instead of snapped to noise.
BEATGRID_REFINE_PERCENTILE = 70
# A beat counts as "confirmed" when a detected onset lands within this fraction
# of the median beat interval. 0.15 of a 500 ms beat is 75 ms -- wide enough to
# absorb human feel and hop quantisation, tight enough that a half-time or
# double-time grid error fails the check.
BEATGRID_ONSET_TOL_FRAC = 0.15
BEATGRID_ONSET_TOL_MAX = 0.07  # absolute ceiling on the above, seconds
# Max height for the MP4 video stream pulled from YouTube (issue #219).
# Capped to keep downloads reasonable; 1080p of a full song is large.
VIDEO_MAX_HEIGHT = max(144, _env_int("STEMDECK_VIDEO_MAX_HEIGHT", 720))


def ffmpeg_executable() -> str:
    """Return the preferred FFmpeg executable.

    In portable mode, setup places FFmpeg under DATA_DIR/ffmpeg. Prefer that
    binary when present; otherwise fall back to PATH so local dev and Docker
    keep working exactly as before.
    """
    return str(FFMPEG_BIN) if FFMPEG_BIN.is_file() else "ffmpeg"


def ffprobe_executable() -> str:
    """Return the preferred ffprobe executable (same bundled dir as ffmpeg)."""
    return str(FFPROBE_BIN) if FFPROBE_BIN.is_file() else "ffprobe"


def configure_portable_environment() -> None:
    """Keep generated caches inside the portable data folder when requested.

    This is intentionally best-effort. It only sets variables that are still
    unset, so explicit caller/env choices win.
    """
    if FFMPEG_DIR.is_dir():
        path = os.environ.get("PATH", "")
        ffmpeg_path = str(FFMPEG_DIR)
        if ffmpeg_path not in path.split(os.pathsep):
            os.environ["PATH"] = ffmpeg_path + (os.pathsep + path if path else "")

    if PORTABLE_DATA_DIR_ENABLED:
        os.environ.setdefault("XDG_CACHE_HOME", str(CACHE_DIR))
        os.environ.setdefault("TORCH_HOME", str(MODELS_DIR / "torch"))


def ensure_runtime_dirs() -> None:
    paths = (
        (JOBS_DIR, CACHE_DIR, DOWNLOADS_DIR, MODELS_DIR, LOGS_DIR)
        if PORTABLE_DATA_DIR_ENABLED
        else (JOBS_DIR,)
    )
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
