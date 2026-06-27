from __future__ import annotations

import logging
import re
import time
import urllib.parse
from pathlib import Path

from yt_dlp import YoutubeDL

from app.core.config import FFMPEG_DIR
from app.core.models import Job, JobCancelled, _set
from app.core.settings import get_max_duration_sec, get_video_max_height

logger = logging.getLogger("stemdeck.download")

_MAX_RETRIES = 3
_RETRY_BACKOFF = (2, 4, 8)  # seconds between attempts

# Errors worth retrying — transient network blips.
_RETRIABLE = (
    "connection reset",
    "ssl",
    "timed out",
    "network is unreachable",
    "temporary failure",
    "unable to download",
    "read timed out",
    "remotedisconnected",
    "broken pipe",
    "connection refused",
)

# Errors that will never succeed on retry — reject immediately.
_NON_RETRIABLE = (
    "private video",
    "video unavailable",
    "has been removed",
    "http error 404",
    "http error 403",
    "not available in your country",
    "age-restricted",
)


def _is_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(s in msg for s in _NON_RETRIABLE):
        return False
    return any(s in msg for s in _RETRIABLE)


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = frozenset(
    (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    )
)
# Note: on.soundcloud.com (the share shortener) is intentionally excluded — it
# redirects to arbitrary targets, which is an SSRF vector once handed to yt-dlp
# (#173). Users must paste the full soundcloud.com URL.
_SOUNDCLOUD_HOSTS = frozenset(("soundcloud.com", "www.soundcloud.com"))
_ALLOWED_HOSTS = _YOUTUBE_HOSTS | _SOUNDCLOUD_HOSTS

# Restrict yt-dlp to the extractors we actually support. Crucially this excludes
# the "generic" extractor, so even a URL that slips past host validation cannot
# make yt-dlp fetch an arbitrary host/redirect target (#173).
_ALLOWED_EXTRACTORS = ["youtube", "soundcloud"]


class InvalidYouTubeURL(ValueError):
    """Raised at the API boundary for URLs we won't hand to yt-dlp."""


def validate_youtube_url(url: str) -> str:
    """Reject anything that isn't an http(s) URL on a known supported host.
    YouTube URLs are normalized to single-video form; SoundCloud URLs are
    passed through as-is. Gives callers a clean 422 instead of a yt-dlp
    extractor stack trace."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidYouTubeURL("URL is required")
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        raise InvalidYouTubeURL(f"could not parse URL: {e}") from e
    if parsed.scheme not in ("http", "https"):
        raise InvalidYouTubeURL("URL must use http or https")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidYouTubeURL(f"unsupported host: {host or '(empty)'}")

    if host in _SOUNDCLOUD_HOSTS:
        return url

    normalized = normalize_youtube_url(url)
    # normalize_youtube_url returns the original on playlist-only URLs with
    # no derivable seed video. We always expect the canonical watch?v=... form.
    if not normalized.startswith("https://www.youtube.com/watch?v="):
        raise InvalidYouTubeURL("could not extract a video ID from URL")
    return normalized


def normalize_youtube_url(url: str) -> str:
    """Coerce a YouTube URL to a single-video form so yt-dlp doesn't end up in
    the playlist extractor. Pass non-YouTube URLs through unchanged.

    Cases handled:
      * `watch?v=X&list=...` -> `watch?v=X` (drop the playlist context)
      * `?list=RD<videoId>&start_radio=1` -> `watch?v=<videoId>` (Radio
        playlists embed the seed in the list ID; YouTube refuses to view the
        playlist directly with "This playlist type is unviewable.")
      * `youtu.be/<videoId>` -> `watch?v=<videoId>`
    Everything else (PL/OL/algorithmic playlists with no derivable seed) is
    left alone -- yt-dlp will surface its own error.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return url
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m.", "music."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if host not in ("youtube.com", "youtu.be"):
        return url

    qs = urllib.parse.parse_qs(parsed.query)
    if (vid := (qs.get("v") or [None])[0]) and _VIDEO_ID_RE.match(vid):
        return f"https://www.youtube.com/watch?v={vid}"

    if (
        (lst := (qs.get("list") or [None])[0])
        and lst.startswith("RD")
        and _VIDEO_ID_RE.match(lst[2:13])
    ):
        return f"https://www.youtube.com/watch?v={lst[2:13]}"

    if host == "youtu.be":
        vid = parsed.path.lstrip("/")
        if _VIDEO_ID_RE.match(vid):
            return f"https://www.youtube.com/watch?v={vid}"

    return url


def _download_video_track(job: Job, url: str, job_dir: Path) -> None:
    """Best-effort: download a video-only H.264/MP4 stream to video.mp4 for the
    MP4 export (issue #219). The audio source is downloaded separately as
    usual; this is a second, additive fetch so the audio pipeline is untouched.

    Video-only MP4 needs no ffmpeg merge, so this can't break an audio-only job:
    any failure (no progressive MP4 video, network error, unsupported codec) is
    logged and swallowed, leaving has_video False. A cancel mid-download raises
    JobCancelled, which the runner treats like any other cancellation.

    Capped at VIDEO_MAX_HEIGHT to keep downloads reasonable -- a full song at
    1080p is large, and the MP4 export doesn't need it."""

    def vhook(d: dict) -> None:
        if job.cancel_requested:
            raise JobCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                p = float(d.get("downloaded_bytes", 0)) / float(total)
                _set(job, stage=f"Fetching video {int(p * 100)}%")

    # Prefer H.264 (avc1) so the exported MP4 plays everywhere -- YouTube also
    # serves AV1/VP9 in mp4 containers, which many players (Safari/iOS, older
    # devices) can't decode. Fall back to any <=cap mp4 only if no avc1 exists.
    max_height = get_video_max_height()
    ydl_opts = {
        "format": (
            f"bestvideo[height<={max_height}][vcodec^=avc1]"
            f"/bestvideo[height<={max_height}][ext=mp4]"
        ),
        "outtmpl": str(job_dir / "video.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "allowed_extractors": _ALLOWED_EXTRACTORS,
        "progress_hooks": [vhook],
    }
    # Point yt-dlp at the bundled ffmpeg in case a DASH stream needs remuxing;
    # in portable builds ffmpeg is not on PATH.
    if FFMPEG_DIR.is_dir():
        ydl_opts["ffmpeg_location"] = str(FFMPEG_DIR)

    _set(job, stage="Fetching video...")
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except JobCancelled:
        raise
    except Exception as exc:
        if job.cancel_requested:
            raise JobCancelled() from exc
        logger.warning("[%s] video track unavailable (audio-only): %s", job.id, exc)

    video = job_dir / "video.mp4"
    if video.is_file() and video.stat().st_size > 0:
        job.has_video = True
    else:
        # Drop any partial/non-mp4 leftover so the export endpoint sees nothing.
        for f in job_dir.glob("video.*"):
            f.unlink(missing_ok=True)


def download(job: Job, url: str, job_dir: Path) -> Path:
    url = normalize_youtube_url(url)
    logger.info("[%s] download starting: %s", job.id, url)
    _set(job, status="downloading", progress=0.0, stage="Processing...")

    # Fetch metadata first (no download) so we can reject videos that are
    # too long before wasting bandwidth and disk.
    with YoutubeDL(
        {"quiet": True, "noplaylist": True, "allowed_extractors": _ALLOWED_EXTRACTORS}
    ) as ydl:
        meta = ydl.extract_info(url, download=False) or {}
    duration = meta.get("duration") or 0
    max_duration = get_max_duration_sec()
    if duration > max_duration:
        mins = max_duration // 60
        raise RuntimeError(f"Video is {int(duration // 60)} min -- limit is {mins} min")

    def hook(d: dict) -> None:
        # yt-dlp calls this on each chunk; raising here aborts the download.
        # The runner unwraps yt-dlp's DownloadError and routes to JobCancelled.
        if job.cancel_requested:
            raise JobCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                p = float(d.get("downloaded_bytes", 0)) / float(total)
                _set(job, progress=p, stage=f"Downloading {int(p * 100)}%")
        elif d.get("status") == "finished":
            _set(job, progress=1.0, stage="Download complete")

    # YouTube jobs additionally fetch the real video stream (below) for the
    # MP4 export (issue #219). SoundCloud is audio-only and excluded.
    is_youtube = url.startswith("https://www.youtube.com/")

    # No postprocessors -- Demucs reads the raw audio container (webm/m4a/opus/...)
    # directly via torchaudio + ffmpeg. Skipping the WAV transcode saves the slowest
    # part of the download pipeline and a lot of disk.
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
        "allowed_extractors": _ALLOWED_EXTRACTORS,
        "progress_hooks": [hook],
    }
    info: dict = {}
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True) or {}
            break
        except Exception as exc:
            if job.cancel_requested:
                raise JobCancelled() from exc
            if attempt < _MAX_RETRIES and _is_retriable(exc):
                wait = _RETRY_BACKOFF[attempt]
                logger.warning(
                    "[%s] download attempt %d/%d failed (%s), retrying in %ds",
                    job.id,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                _set(job, stage=f"Network error — retrying ({attempt + 1}/{_MAX_RETRIES})...")
                time.sleep(wait)
            else:
                raise

    _set(
        job,
        title=info.get("title") or meta.get("title"),
        duration_sec=info.get("duration") or duration,
        thumbnail=info.get("thumbnail") or meta.get("thumbnail"),
    )

    raw_tags = [
        t.strip().lower()
        for t in (info.get("tags") or []) + (info.get("categories") or [])
        if isinstance(t, str) and t.strip()
    ]
    seen: set[str] = set()
    deduped = [t for t in raw_tags if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
    _set(job, tags=deduped[:8] or None)

    # Best-effort: fetch the real video stream for the MP4 export.
    # Non-fatal -- on any failure the job proceeds audio-only.
    if is_youtube:
        _download_video_track(job, url, job_dir)

    candidates = sorted(job_dir.glob("source.*"))
    if not candidates:
        raise RuntimeError("yt-dlp finished but no source file was produced")
    logger.info("[%s] download complete: %s", job.id, candidates[0].name)
    return candidates[0]
