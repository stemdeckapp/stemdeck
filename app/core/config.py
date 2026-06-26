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


def _detect_device() -> str:
    """Pick best available Torch device for Demucs. Override via
    STEMDECK_DEMUCS_DEVICE env var ('cuda' | 'mps' | 'cpu'). Apple Silicon
    silently falls back to CPU otherwise -- demucs's CLI default is
    "cuda if available else cpu" and macOS has no CUDA, leaving the
    integrated GPU idle and processing 3-5x slower than necessary."""
    forced = os.environ.get("STEMDECK_DEMUCS_DEVICE", "").strip().lower()
    if forced in ("cuda", "mps", "cpu"):
        return forced
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = ROOT / "static"
STEM_NAMES: tuple[str, ...] = ("vocals", "drums", "bass", "guitar", "piano", "other")
JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# Runtime knobs -- env-backed so Docker / desktop packaging / local dev can
# tune without a code edit. STEMDECK_DATA_DIR is the portable app root for
# mutable runtime data; when unset, dev behavior remains the repo-local jobs/
# folder.
PORTABLE_DATA_DIR_ENABLED = bool(os.environ.get("STEMDECK_DATA_DIR", "").strip())
DATA_DIR = _env_path("STEMDECK_DATA_DIR", ROOT)
JOBS_DIR = _env_path(
    "STEMDECK_JOBS_DIR",
    (DATA_DIR / "jobs") if PORTABLE_DATA_DIR_ENABLED else (ROOT / "jobs"),
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
DEMUCS_MODEL = os.environ.get("STEMDECK_DEMUCS_MODEL", "htdemucs_6s").strip() or "htdemucs_6s"
DEMUCS_DEVICE = _detect_device()
MAX_DURATION_SEC = max(60, _env_int("STEMDECK_MAX_DURATION_SEC", 1200))  # 20 min default
JOB_TTL_SECONDS = max(300, _env_int("STEMDECK_JOB_TTL_SECONDS", 24 * 3600))  # 24 h default
MAX_PENDING_JOBS = max(1, min(50, _env_int("STEMDECK_MAX_PENDING_JOBS", 3)))
TIMEOUT_FFMPEG = _env_int("STEMDECK_TIMEOUT_FFMPEG", 300)
TIMEOUT_ANALYZE = _env_int("STEMDECK_TIMEOUT_ANALYZE", 120)
TIMEOUT_DEMUCS_STALL = _env_int("STEMDECK_TIMEOUT_DEMUCS_STALL", 1800)
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
