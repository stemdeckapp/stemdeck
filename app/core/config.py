import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("stemdeck.config")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def _env_path_opt(name: str) -> Path | None:
    """A path setting with no default: absent means the feature is off."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


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


def _packaged_data_dir() -> Path | None:
    """The data folder of a desktop package, found without being told.

    The desktop shell always passes STEMDECK_DATA_DIR, so this only matters
    when the backend is started some other way: by hand, from a terminal, out
    of the package the shell would normally launch. That used to resolve
    DATA_DIR to `backend/` itself, so the backend read a settings.json that did
    not exist and ignored every choice the user had made in the app. Silent,
    and it made a directly-run backend a different application with the same
    files (#459).

    Keyed on the layout every package shares -- `<package>/backend/app` here,
    `<package>/data` beside it -- rather than on a marker file, because only
    the Windows package writes one. A source checkout has ROOT named for the
    repo and Docker has WORKDIR /app, so neither matches and neither changes.
    """
    if ROOT.name != "backend":
        return None
    candidate = ROOT.parent / "data"
    return candidate if candidate.is_dir() else None


# Runtime knobs -- env-backed so Docker / desktop packaging / local dev can
# tune without a code edit. STEMDECK_DATA_DIR is the portable app root for
# mutable runtime data; when unset, a package finds its own (above) and a plain
# dev checkout stays on the repo-local jobs/ folder.
_PACKAGED_DATA_DIR = _packaged_data_dir()
PORTABLE_DATA_DIR_ENABLED = bool(
    os.environ.get("STEMDECK_DATA_DIR", "").strip() or _PACKAGED_DATA_DIR
)
DATA_DIR = _env_path("STEMDECK_DATA_DIR", _PACKAGED_DATA_DIR or ROOT)


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

# ── TLS, for server deployments ───────────────────────────────────────────────
#
# AudioWorklet -- and so transpose -- is a [SecureContext] API, which browsers
# grant to https:// and localhost and to nothing else. A phone on
# http://<lan-ip> therefore cannot have it, and there is no workaround worth
# shipping: driving the same DSP from a ScriptProcessorNode was measured
# dropping ~5% of the audio, audibly, because that node type is lossy on its
# own regardless of buffer size.
#
# Terminating TLS here costs nothing. uvicorn uses the stdlib `ssl` module, so
# no package is added, uv.lock does not change, and the desktop in-app updater
# is unaffected (see .claude/rules/desktop-update-gate.md). Generating a
# certificate would need a dependency and would hand every client a full-page
# browser warning, so StemDeck never does that: bring your own, from a reverse
# proxy, Tailscale Serve, or mkcert.
SSL_CERTFILE = _env_path_opt("STEMDECK_SSL_CERT")
SSL_KEYFILE = _env_path_opt("STEMDECK_SSL_KEY")
# The desktop app runs two listeners, not one: plain http on loopback for its
# own webview, and https on the LAN for phones. It needs both because the two
# have incompatible requirements -- the webview cannot be shown a certificate
# warning it has no way to click through, and a phone cannot have transpose
# without a secure origin. When this is set, app.main starts the second
# listener alongside the first; when it is not, there is only ever one server
# and TLS (if configured at all) belongs to whoever launched uvicorn.
HTTPS_PORT = _env_int("STEMDECK_HTTPS_PORT", 0) or None
FFMPEG_BIN = _env_path(
    "STEMDECK_FFMPEG",
    FFMPEG_DIR / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"),
)
FFPROBE_BIN = _env_path(
    "STEMDECK_FFPROBE",
    FFMPEG_DIR / ("ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"),
)
# Chromaprint's fpcalc, for fingerprinting when the FFmpeg in use has no
# chromaprint muxer (app/pipeline/fpcalc.py). The desktop shell downloads it
# beside FFmpeg on macOS and Linux; elsewhere it is looked for beside the FFmpeg
# in use, then on PATH.
FPCALC_BIN = _env_path(
    "STEMDECK_FPCALC",
    FFMPEG_DIR / ("fpcalc.exe" if sys.platform.startswith("win") else "fpcalc"),
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


def _js_runtime_dirs() -> tuple[Path, ...]:
    """Where a bundled JS runtime can live, in priority order.

    Two layouts, because the packages are not built the same way. Windows and
    Linux stage a whole tree and put the binary in `data/jsruntime`, which is
    JS_RUNTIME_DIR's default. macOS downloads a runtime pack and keeps its own
    data directory in ~/Library/Application Support, so the binary rides in the
    pack next to `backend/` instead.

    Checking both here rather than setting an env var from the desktop shell
    keeps this a Python-side fact that can be tested, instead of a contract
    split across two languages that only breaks on one platform.
    """
    dirs = [JS_RUNTIME_DIR]
    # app/core/config.py -> app/core -> app -> backend/ (or the repo root).
    backend_root = Path(__file__).resolve().parents[2]
    dirs.append(backend_root / "jsruntime")
    dirs.append(backend_root.parent / "jsruntime")
    seen: set[Path] = set()
    return tuple(d for d in dirs if not (d in seen or seen.add(d)))  # type: ignore[func-returns-value]


def bundled_js_runtime() -> tuple[str, Path] | None:
    """The JS runtime shipped with this install, as (yt-dlp name, path).

    None when nothing is bundled, which is the normal case outside a packaged
    build -- yt-dlp then falls back to its own PATH lookup, which is how Docker
    finds the deno it ships. Never raises: a missing or unreadable directory
    just means "not bundled".
    """
    suffix = ".exe" if sys.platform.startswith("win") else ""
    for directory in _js_runtime_dirs():
        try:
            if not directory.is_dir():
                continue
            for name, stem in _JS_RUNTIME_BINARIES:
                exe = directory / f"{stem}{suffix}"
                if exe.is_file():
                    return name, exe
        except OSError:
            continue
    return None


def js_solver_available() -> bool:
    """Whether anything on this machine could run YouTube's challenge solver.

    Used to explain a failure, never to gate one: yt-dlp does its own runtime
    discovery and this is only a best-effort mirror of it. A false positive
    just means the user gets the generic error instead of the specific one.
    """
    if bundled_js_runtime() is not None:
        return True
    return any(shutil.which(exe) for _, exe in _JS_RUNTIME_BINARIES)


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
# Automatic functional-section analysis. Inference stays on CPU because the
# persistent Demucs worker deliberately keeps its model resident on the chosen
# accelerator between jobs; loading a second model beside it would make VRAM
# use depend on GPU size and the preceding job. The ensemble name remains
# configurable for deployments evaluating a different compatible checkpoint.
SECTION_MODEL = os.environ.get("STEMDECK_SECTION_MODEL", "harmonix-all").strip() or "harmonix-all"
TIMEOUT_SECTIONS = max(60, _env_int("STEMDECK_TIMEOUT_SECTIONS", 30 * 60))
TIMEOUT_SECTIONS_STALL = max(30, _env_int("STEMDECK_TIMEOUT_SECTIONS_STALL", 120))
# Conservative evidence gates for the section refiner. The first real-song
# diagnostic found that the upstream decoder emitted 13 Come As You Are spans
# but our equal-label merge hid six boundaries. It also found a suppressed
# 0.059 activation inside the intro with only 0.18 embedding novelty. Requiring
# 0.05 activation and 0.35 novelty preserves strong independent evidence
# without turning each instrumental entrance into a new functional section.
SECTION_REFINEMENT_GRID_MIN_CONFIDENCE = 70
SECTION_REFINEMENT_BEAT_SNAP_SECONDS = 0.12
SECTION_REFINEMENT_MIN_ACTIVATION = 0.05
SECTION_REFINEMENT_MIN_NOVELTY = 0.35
SECTION_REFINEMENT_NOVELTY_WINDOW_SECONDS = 8.0
SECTION_REFINEMENT_MIN_SEGMENT_SECONDS = 6.0
# The test track's disputed 49.85-65.89 span had a 0.045 Verse/Chorus margin,
# while the surrounding accepted semantic spans were at least 0.10. A weak
# tie becomes neutral Part rather than a confidently wrong functional label.
SECTION_REFINEMENT_MIN_LABEL_MARGIN = 0.08
SECTION_REFINEMENT_RECURRENCE_SIMILARITY = 0.85
SECTION_REFINEMENT_RECURRENCE_LABEL_MARGIN = 0.25
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

# Reading an upload's tags (artist, title, album, lyrics) with ffprobe for the
# Lyrics tab and the artist box (#699). Tags are a nicety, so a probe that
# takes longer than this is abandoned and the upload goes ahead without them.
TIMEOUT_PROBE_TAGS = _env_int("STEMDECK_TIMEOUT_PROBE_TAGS", 15)
# Looking up a finished link's tags after the fact, for tracks imported before
# tags were read: one yt-dlp metadata request (two if YouTube's bot check sends
# it back for cookies), no download. Past this the lookup answers "nothing
# found" rather than keep the page waiting.
TIMEOUT_FETCH_TAGS = _env_int("STEMDECK_TIMEOUT_FETCH_TAGS", 45)
# Caps on what a tag may carry into the job record, which is rewritten whole
# on every save: a name longer than this is not a name, and a lyrics tag
# longer than this is not lyrics.
AUDIO_TAG_MAX_CHARS = 300
AUDIO_TAG_LYRICS_MAX_CHARS = 20000

# Finding the band a job's artist tag names, on Wikidata, while the job is
# separated (#699), so the artist box and the Lyrics tab have it the moment the
# import is done. Two requests, each abandoned after this many seconds. The
# lookup runs beside separation, which takes far longer, so it costs an import
# no time unless both requests are slow and the separation was fast.
TIMEOUT_ARTIST_LOOKUP = _env_int("STEMDECK_TIMEOUT_ARTIST_LOOKUP", 8)
# How long a finished pipeline waits for a lookup still in flight before it
# lets the job finish without a band. Kept short: the page finds the band
# itself when it opens a track that has none, so the wait buys little.
ARTIST_LOOKUP_GRACE_SEC = _env_int("STEMDECK_ARTIST_LOOKUP_GRACE_SEC", 2)
# Wikimedia asks every client to name itself and a way to reach its maker.
ARTIST_LOOKUP_USER_AGENT = (
    "StemDeck (https://github.com/stemdeckapp/stemdeck; self-hosted stem separation)"
)
# A search and one entity fetch for its hits come to well under this; an
# answer bigger than it is not one worth parsing.
ARTIST_LOOKUP_MAX_BYTES = 8 * 1024 * 1024

# Which recording a job is, found while it separates (app/pipeline/identify.py).
#
# AcoustID: an audio fingerprint of the first FINGERPRINT_LENGTH_SEC seconds
# (fpcalc's default length), made with ffmpeg's chromaprint muxer (or fpcalc,
# FPCALC_BIN, where that FFmpeg has none), looked up with the user's own API
# key. Only runs when the user set one in Settings. TIMEOUT_FINGERPRINT bounds
# either one.
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
FINGERPRINT_LENGTH_SEC = 120
# Decoding two minutes of audio for the fingerprint takes about a second; past
# this the fingerprint is abandoned and identification goes on without it.
TIMEOUT_FINGERPRINT = _env_int("STEMDECK_TIMEOUT_FINGERPRINT", 30)
# AcoustID's score is how closely the fingerprints agree, 0..1. The same
# recording scores well above 0.9; a different mix or a live cut of the song
# lands far lower. Kept high because the answer is saved with nobody looking.
ACOUSTID_MIN_SCORE = 0.8
# AcoustID allows three requests a second per client.
ACOUSTID_MIN_INTERVAL_SEC = 0.34
# MusicBrainz: one request a second per client, and every client names itself
# with a way to reach its maker, or it is throttled harder.
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_MIN_INTERVAL_SEC = 1.0


def _musicbrainz_user_agent() -> str:
    try:
        from app._version import version

        release = str(version).split("+", 1)[0] or "dev"
    except Exception:
        release = "dev"
    return f"StemDeck/{release} ( https://github.com/stemdeckapp/stemdeck )"


MUSICBRAINZ_USER_AGENT = _musicbrainz_user_agent()
# A request that would wait longer than this for its turn under the rate
# limit is not made at all: a queue that long means something else is
# hammering the service, and identification is best-effort.
RATE_LIMIT_MAX_WAIT_SEC = 15
# A search by name is kept only when MusicBrainz scores it at least this
# (0..100) and its length is within IDENTIFY_DURATION_TOLERANCE_SEC of the
# track's: a name alone can be anybody's song, the length says which.
MUSICBRAINZ_SEARCH_MIN_SCORE = 90
IDENTIFY_DURATION_TOLERANCE_SEC = 4
# A track its tags do not name (a YouTube upload with no music metadata) is
# looked for by its title (app/pipeline/title_parse.py): up to this many
# readings of it on MusicBrainz, then, when none is confident, on LRCLIB.
TITLE_MUSICBRAINZ_SEARCHES = 3
TITLE_LRCLIB_SEARCHES = 2
# A recording found that way is kept only when its credit and release titles
# (or LRCLIB's artist and album) contain at least this share of the title's
# words beyond the song's name: the performers, the show.
TITLE_MIN_COVERAGE = 0.5
# Each AcoustID or MusicBrainz request is abandoned after this many seconds.
TIMEOUT_IDENTIFY_REQUEST = _env_int("STEMDECK_TIMEOUT_IDENTIFY_REQUEST", 8)
# How long a finished pipeline waits for identification still in flight, as
# ARTIST_LOOKUP_GRACE_SEC does for the band. Separation takes minutes, so the
# answer is nearly always waiting already.
IDENTIFY_GRACE_SEC = _env_int("STEMDECK_IDENTIFY_GRACE_SEC", 2)
# POST /api/jobs/{id}/audio-tags identifies an older track before it answers;
# past this it answers with what it has.
TIMEOUT_IDENTIFY_BACKFILL = _env_int("STEMDECK_TIMEOUT_IDENTIFY_BACKFILL", 60)
# A lookup answer bigger than this is not one worth parsing.
IDENTIFY_MAX_BYTES = 4 * 1024 * 1024
# MusicBrainz answers kept on disk, one file per recording or artist MBID, so
# re-identifying a track (a re-split, a backfill) asks nothing twice.
MUSICBRAINZ_CACHE_DIR = CACHE_DIR / "musicbrainz"
MUSICBRAINZ_CACHE_TTL_SEC = 30 * 24 * 3600
# The work behind a soundtrack or cast recording (app/pipeline/work_lookup.py):
# the musical, film or series a job's song is from, found on Wikidata beside
# the band. Past this many seconds the tag backfill answers without it.
TIMEOUT_WORK_BACKFILL = _env_int("STEMDECK_TIMEOUT_WORK_BACKFILL", 30)
# How far up Wikidata's "subclass of" a work's class is followed to find a
# kind of work it is. Three reaches "animated television series" from
# "anime television series"; further only finds classes too broad to mean it.
WORK_CLASS_DEPTH = 3
# A cast recording comes out months after the show opens, and a soundtrack
# can come out a little before its film. A work first shown more than this
# many years after the recording came out cannot be what it is from.
WORK_YEAR_SLACK = 1

# A job's lyrics, found on LRCLIB (lrclib.net) while it separates and kept
# beside its stems as LYRICS_FILE (app/pipeline/lyrics_lookup.py). LRCLIB asks
# clients to name themselves and link their homepage.
LRCLIB_API = "https://lrclib.net/api"
LRCLIB_USER_AGENT = (
    "StemDeck (https://github.com/stemdeckapp/stemdeck; self-hosted stem separation)"
)
LYRICS_FILE = "lyrics.json"
# When LRCLIB was asked and no version was kept: the versions it had, for the
# tab to offer, and when it was asked. The tag backfill does not ask again for
# LYRICS_NOT_FOUND_RETRY_SEC; lyrics.json from any source settles it for good.
LYRICS_CANDIDATES_FILE = "lyrics_candidates.json"
LYRICS_NOT_FOUND_RETRY_SEC = _env_int("STEMDECK_LYRICS_NOT_FOUND_RETRY_DAYS", 30) * 24 * 3600
# Each request is abandoned after this many seconds; /api/get can be slow when
# LRCLIB has to look further afield for an exact match.
TIMEOUT_LYRICS_LOOKUP = _env_int("STEMDECK_TIMEOUT_LYRICS_LOOKUP", 10)
# No further request is started this many seconds into a lookup. The cascade
# is at most five requests, and all of them run beside separation.
LYRICS_LOOKUP_BUDGET_SEC = _env_int("STEMDECK_LYRICS_LOOKUP_BUDGET_SEC", 30)
# How long a finished pipeline waits for a lookup still in flight. Short for
# the same reason as the band's: the tab looks for lyrics itself when a track
# has none.
LYRICS_LOOKUP_GRACE_SEC = _env_int("STEMDECK_LYRICS_LOOKUP_GRACE_SEC", 2)
# How long the lookup waits for the identification running beside it (a
# fingerprint, then MusicBrainz at one request a second) before it goes by the
# tags instead. Both run beside separation, so the wait costs an import nothing.
LYRICS_IDENTITY_WAIT_SEC = _env_int("STEMDECK_LYRICS_IDENTITY_WAIT_SEC", 60)
# A search answers at most 20 versions of a few kilobytes each.
LYRICS_LOOKUP_MAX_BYTES = 8 * 1024 * 1024
# LRCLIB lengths come from real releases, so the same recording lands within a
# second or two; a live cut or a radio edit does not. The page's
# SAME_RECORDING_SEC and SAME_LENGTH_SEC (static/js/lyrics*.js).
LYRICS_SAME_RECORDING_SEC = 3
# Other versions kept beside the one chosen, for the tab to offer.
LYRICS_OTHERS_MAX = 11
# Moving the timing of lyrics from a version of another length onto the track
# (app/pipeline/lyrics_align.py): the shifts tried, how near a line's start the
# voice's rise is looked for, and how clear the best shift must be: this many
# standard deviations above all shifts, this many times the best shift a
# second or more away, and this share of the lines on a rise of the voice.
# Measured on six library tracks' vocals stems against their LRCLIB versions:
# the same recording scored z 5.1 to 7.3, 1.24 to 1.48 and 66 to 94% of lines
# (two songs of even, repeating lines scored 1.06 and stay unverified); a
# different performance of the song z 2.7, 1.03, 55%; scattered synthetic
# singing at most z 4.9, 1.33, 35%.
LYRICS_ALIGN_MAX_OFFSET_SEC = 60
LYRICS_ALIGN_TOLERANCE_SEC = 0.2
LYRICS_ALIGN_MIN_LINES = 6
LYRICS_ALIGN_MIN_Z = 4.5
LYRICS_ALIGN_MIN_RATIO = 1.15
LYRICS_ALIGN_MIN_HITS = 0.6

# Lyrics transcribed from the vocals stem with Whisper when no lookup found
# any (app/pipeline/transcribe.py). Whether it runs is the transcribe_lyrics
# setting. Weights download on first use into <TORCH_HOME>/whisper, beside the
# Demucs checkpoints, and are verified by Whisper's own SHA-256 check.
#
# large-v3-turbo on a GPU: large-v3's encoder with a 4-layer decoder, 1.6 GB.
# small on a CPU, or on a GPU without room for turbo: 0.5 GB.
TRANSCRIBE_MODEL_GPU = os.environ.get("STEMDECK_WHISPER_MODEL_GPU", "").strip() or "turbo"
TRANSCRIBE_MODEL_CPU = os.environ.get("STEMDECK_WHISPER_MODEL_CPU", "").strip() or "small"
# Free VRAM the GPU model needs, checked in the worker before loading, since
# the Demucs worker keeps its own allocation resident on the same card.
# Measured on an RTX 3080 (Nirvana "Lithium", 255 s): turbo held in fp16
# peaked at 2172 MB reserved by torch, 2463 MB on the card with the CUDA context.
TRANSCRIBE_GPU_MIN_FREE_MB = _env_int("STEMDECK_WHISPER_GPU_MIN_FREE_MB", 3072)
# Below this, even the small model is not tried on the GPU and runs on the CPU.
TRANSCRIBE_SMALL_GPU_MIN_FREE_MB = _env_int("STEMDECK_WHISPER_SMALL_GPU_MIN_FREE_MB", 1536)
# The whole stage, including a first-run model download on a slow connection.
TIMEOUT_TRANSCRIBE = max(60, _env_int("STEMDECK_TIMEOUT_TRANSCRIBE", 30 * 60))
# No output for this long is a hang. Whisper reports progress once per 30 s
# window of audio, which the small model on a CPU finishes well inside it.
TIMEOUT_TRANSCRIBE_STALL = max(30, _env_int("STEMDECK_TIMEOUT_TRANSCRIBE_STALL", 300))
# Vocals quieter than this, as stem_presence (0-100, relative to the loudest
# stem), are not worth transcribing: Whisper invents words over silence. A stem
# that is absent from a track measured 0-2 across six library tracks.
TRANSCRIBE_MIN_VOCAL_PRESENCE = 3
# Skip silent stretches longer than this around a suspected hallucination
# (Whisper's hallucination_silence_threshold). An isolated vocal has long
# instrumental gaps, which is where Whisper makes up text.
TRANSCRIBE_HALLUCINATION_SILENCE_SEC = 2.0
# Lines are Whisper's segments, broken further at a pause at least this long
# between two words, and at a comma or a pause once a line is this many
# characters long. Nothing is ever longer than the hard cap.
TRANSCRIBE_LINE_BREAK_GAP_SEC = 0.8
TRANSCRIBE_LINE_SOFT_CHARS = 40
TRANSCRIBE_LINE_HARD_CHARS = 64
# A gap between two words at least this long is written as an end stamp, so
# the karaoke wipe waits through it instead of stretching the first word.
TRANSCRIBE_WORD_GAP_SEC = 0.3
# A silence at least this long between two lines starts a new stanza (a blank
# line) in the plain text.
TRANSCRIBE_STANZA_GAP_SEC = 4.0

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


def ffmpeg_dir() -> Path | None:
    """The directory holding the FFmpeg this process actually runs, or None.

    PATH and yt-dlp both need a directory, and they used to be given
    FFMPEG_DIR: in a desktop install always data/ffmpeg, whatever setup had
    concluded about what lives there. Setup can reject that copy and settle on
    another -- an Intel pair on Apple Silicon (#637), a build missing an
    encoder -- and tell the backend through STEMDECK_FFMPEG/STEMDECK_FFPROBE,
    which ffmpeg_executable() honours. PATH and yt-dlp never got the message,
    so separation used the verified binary while every YouTube import ran the
    rejected one (#651).

    So the directory is derived from the binary, which makes it one answer
    everywhere. It also covers data/ffmpeg/bin/, the nested layout setup
    accepts (#248): yt-dlp was handed data/ffmpeg there, which holds no binary
    at all, and yt-dlp does not fall back to PATH from a directory it was
    given.

    None when there is no single directory to hand over: no binaries (Docker,
    or a source checkout with FFmpeg on PATH, where PATH is already right), or
    ffmpeg and ffprobe in different places. yt-dlp takes one directory and
    looks for both in it, so a split pair is left to PATH, where the desktop
    shell has already put the verified directory first.

    A hand-set STEMDECK_FFMPEG_DIR is still honoured: FFMPEG_BIN defaults to a
    file inside it.

    Evaluated per call, not bound at import, so nothing has to agree with a
    value captured before the environment was final.
    """
    if not (FFMPEG_BIN.is_file() and FFPROBE_BIN.is_file()):
        return None
    directory = FFMPEG_BIN.resolve().parent
    if FFPROBE_BIN.resolve().parent != directory:
        return None
    return directory


def configure_portable_environment() -> None:
    """Keep generated caches inside the portable data folder when requested.

    This is intentionally best-effort. It only sets variables that are still
    unset, so explicit caller/env choices win.
    """
    # FFmpeg first on PATH for anything that runs a bare `ffmpeg`, Demucs
    # decoding a compressed source among them. See ffmpeg_dir() for why this is
    # the verified binary's directory and not FFMPEG_DIR.
    directory = ffmpeg_dir()
    if directory is not None:
        path = os.environ.get("PATH", "")
        ffmpeg_path = str(directory)
        if ffmpeg_path not in path.split(os.pathsep):
            os.environ["PATH"] = ffmpeg_path + (os.pathsep + path if path else "")
        logger.info("FFmpeg directory: %s", directory)
    else:
        logger.info("FFmpeg directory: none of our own, resolving from PATH")
    # Said out loud because the failure it prevents was silent: a data-directory
    # FFmpeg that is not the one in use is the copy setup rejected, and the
    # error a user sees if it is ever run names that binary (#651). Not said
    # for data/ffmpeg/bin/, which is the same install in its nested layout.
    if FFMPEG_DIR.is_dir() and (
        directory is None or not directory.is_relative_to(FFMPEG_DIR.resolve())
    ):
        logger.info(
            "Not using %s: the FFmpeg in use is %s",
            FFMPEG_DIR,
            directory or "whatever PATH resolves",
        )

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
