from __future__ import annotations

import logging
import re
import time
import urllib.parse
from pathlib import Path

from yt_dlp import YoutubeDL

from app.core.config import MAX_DURATION_SEC
from app.core.models import Job, JobCancelled

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
_ALLOWED_HOSTS = frozenset(
    (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    )
)


class InvalidYouTubeURL(ValueError):
    """Raised at the API boundary for URLs we won't hand to yt-dlp."""


def validate_youtube_url(url: str) -> str:
    """Reject anything that isn't an http(s) URL on a known YouTube host, then
    return the normalized single-video form. Keeps StemDeck from acting as a
    generic URL fetcher and gives callers a clean 422 instead of a yt-dlp
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


def _set(job: Job, **fields: object) -> None:
    """Mutate Job fields. Polling SSE picks the change up automatically."""
    for k, v in fields.items():
        if k == "stage":
            job.stage_message = v  # type: ignore[assignment]
        else:
            setattr(job, k, v)


def download(job: Job, url: str, job_dir: Path) -> Path:
    url = normalize_youtube_url(url)
    logger.info("[%s] download starting: %s", job.id, url)
    _set(job, status="downloading", progress=0.0, stage="Processing...")

    # Fetch metadata first (no download) so we can reject videos that are
    # too long before wasting bandwidth and disk.
    with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
        meta = ydl.extract_info(url, download=False) or {}
    duration = meta.get("duration") or 0
    if duration > MAX_DURATION_SEC:
        mins = MAX_DURATION_SEC // 60
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

    # No postprocessors -- Demucs reads the raw audio container (webm/m4a/opus/...)
    # directly via torchaudio + ffmpeg. Skipping the WAV transcode saves the slowest
    # part of the download pipeline and a lot of disk.
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "source.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
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

    candidates = sorted(job_dir.glob("source.*"))
    if not candidates:
        raise RuntimeError("yt-dlp finished but no source file was produced")
    logger.info("[%s] download complete: %s", job.id, candidates[0].name)
    return candidates[0]
