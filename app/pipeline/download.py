from __future__ import annotations

import logging
import re
import time
import urllib.parse
from pathlib import Path

from yt_dlp import YoutubeDL

from app.core.config import FFMPEG_DIR, bundled_js_runtime, js_solver_available
from app.core.models import Job, JobCancelled, _set
from app.core.settings import get_cookies_file, get_max_duration_sec, get_video_max_height

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


# yt-dlp's default socket timeout is 20 s but only applies where it plumbs the
# option through; set it explicitly on every YoutubeDL we build so a stalled
# TCP connection can never hang a job indefinitely (#279).
_SOCKET_TIMEOUT_SEC = 30


def _with_retries(job: Job, fn, *, what: str):
    """Run `fn` with the shared transient-network retry policy (#279).

    Retries _MAX_RETRIES times with backoff on retriable errors; re-raises
    immediately on non-retriable ones. A cancel arriving mid-attempt is
    surfaced as JobCancelled. Shared by the metadata probe and the download
    itself so both survive the same network blips."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if job.cancel_requested:
                raise JobCancelled() from exc
            if attempt < _MAX_RETRIES and _is_retriable(exc):
                wait = _RETRY_BACKOFF[attempt]
                logger.warning(
                    "[%s] %s attempt %d/%d failed (%s), retrying in %ds",
                    job.id,
                    what,
                    attempt + 1,
                    _MAX_RETRIES,
                    exc,
                    wait,
                )
                _set(job, stage=f"Network error — retrying ({attempt + 1}/{_MAX_RETRIES})...")
                time.sleep(wait)
            else:
                raise


# YouTube refusing to serve us at all, as opposed to a video being unavailable.
# Cookies are the only remedy in-tree: yt-dlp ships no PO token generator.
_BOT_CHECK = ("sign in to confirm", "http error 429", "too many requests")

# What a missing challenge solver looks like once cookies ARE in play.
_NEEDS_SOLVER = (
    "requested format is not available",
    "only images are available",
    "n challenge solving failed",
)


def _is_bot_check(exc: Exception) -> bool:
    low = str(exc).lower()
    return any(s in low for s in _BOT_CHECK)


def _with_cookie_fallback(job: Job, fn, *, what: str) -> tuple[object, bool]:
    """Run `fn(use_cookies)` without cookies first, with them only if YouTube
    turned us away (#432).

    Cookies are not a better way to fetch: supplying them makes yt-dlp skip
    every client that does not support them, which removes the unauthenticated
    fallback clients that resolve formats today without any JS challenge
    solver. Applying them to every request would therefore break imports that
    currently work, in order to fix imports for the smaller group whose IP
    YouTube has flagged.

    Trying without them first means the setting cannot make anything worse: by
    the time cookies are used, the path they would have displaced has already
    failed. Same shape as separate()'s GPU->CPU retry -- the fallback runs only
    once the primary path is known to be dead.

    Returns (result, used_cookies). The caller passes that flag into any later
    request for the same URL, so one job never re-derives the answer.
    """

    def attempt(use_cookies: bool):
        return _with_retries(job, lambda: fn(use_cookies), what=what)

    try:
        return attempt(False), False
    except Exception as exc:
        if job.cancel_requested or not _is_bot_check(exc) or get_cookies_file() is None:
            raise
        logger.info("[%s] %s hit YouTube's bot check; retrying with cookies", job.id, what)
        _set(job, stage="Retrying with cookies...")
        try:
            return attempt(True), True
        except Exception as retry_exc:
            # The cookies cleared the bot check and the job then died for want
            # of a challenge solver. Say that, rather than leaving the user to
            # infer it from "Requested format is not available" (#432).
            low = str(retry_exc).lower()
            if any(p in low for p in _NEEDS_SOLVER) and not js_solver_available():
                raise RuntimeError(
                    "Cookies cleared YouTube's bot check, but no JavaScript runtime is "
                    "available to solve YouTube's format challenge, so no audio format "
                    "could be resolved. Clearing the cookies path in Settings restores "
                    "the fallback that does not need one."
                ) from retry_exc
            raise


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = frozenset(
    (
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        # The "privacy-enhanced mode" embed domain -- same site, same extractor,
        # shows up in copy-pasted embed/share code rather than the address bar.
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
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


# Expanding a playlist needs the tab/playlist extractors, which the single-video
# allowlist above deliberately excludes. Entries are regexes matched with
# re.fullmatch against IE_NAME.lower(), so "youtube" alone never resolves to
# "youtube:tab" -- hence a second, separate list rather than widening the first.
# "generic" stays out of both: that exclusion is the whole point of #173.
_ALLOWED_PLAYLIST_EXTRACTORS = [
    "youtube:tab",
    "youtube:playlist",
    "youtube",
    "soundcloud:set",
    "soundcloud",
]


def _base_ydl_opts(extractors: list[str], *, use_cookies: bool = False) -> dict:
    """Options every YoutubeDL built in this module must share (#435).

    There are four call sites -- playlist expansion, the metadata probe, the
    audio fetch and the MP4 video fetch -- and they used to build their options
    independently. They had already drifted (`ffmpeg_location` set in one of
    four, `noplaylist` in three), and the drift is invisible: a fix applied to
    the audio fetch silently misses the probe that runs before it and the video
    fetch that runs after. `_download_video_track` swallows every exception and
    falls back to audio-only, so a missing option there costs the user their
    MP4 export with nothing surfaced anywhere.

    `allowed_extractors` is a required argument rather than a default because
    it is the SSRF boundary (#173) and the two valid values are genuinely
    different; a caller must state which one it means.
    """
    opts: dict = {
        "quiet": True,
        "allowed_extractors": extractors,
        "socket_timeout": _SOCKET_TIMEOUT_SEC,
    }
    # Portable builds have no ffmpeg on PATH; needed wherever a DASH stream
    # might be remuxed. Inert for the metadata-only calls.
    if FFMPEG_DIR.is_dir():
        opts["ffmpeg_location"] = str(FFMPEG_DIR)
    # YouTube's n-challenge solver needs a JS runtime. Absent outside portable
    # builds, where yt-dlp resolves its own from PATH instead (#432).
    if (runtime := bundled_js_runtime()) is not None:
        name, exe = runtime
        opts["js_runtimes"] = {name: {"path": str(exe)}}
    # Only on an explicit retry, never on the first attempt. See
    # _with_cookie_fallback for why (#432).
    if use_cookies and (cookies := get_cookies_file()) is not None:
        opts["cookiefile"] = cookies
    return opts


# YouTube list ids. RD-prefixed ones are algorithmic radio: effectively endless
# and different for every viewer, so there is no meaningful set to import.
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_SOUNDCLOUD_SET_RE = re.compile(r"^/[^/]+/sets/[^/]+/?$")


class InvalidYouTubeURL(ValueError):
    """Raised at the API boundary for URLs we won't hand to yt-dlp."""


class InvalidPlaylistURL(ValueError):
    """Raised for URLs that are not a playlist we are willing to expand."""


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
      * `watch?v=X&list=...` -> `watch?v=X` (drop the playlist context,
        regardless of what other tracking/context params ride along --
        `si=`, `t=`, `app=desktop`, etc.)
      * `?list=RD<videoId>&start_radio=1` -> `watch?v=<videoId>` (Radio
        playlists embed the seed in the list ID; YouTube refuses to view the
        playlist directly with "This playlist type is unviewable.")
      * `youtu.be/<videoId>` -> `watch?v=<videoId>`
      * `youtube.com/shorts/<videoId>` -> `watch?v=<videoId>`
      * `youtube.com/live/<videoId>` -> `watch?v=<videoId>` (premieres and
        creator livestreams keep this URL once they end and become a normal
        VOD -- common for concert/DJ-set recordings)
      * `youtube-nocookie.com/...` -> the same forms on `youtube.com`
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
    if host not in ("youtube.com", "youtu.be", "youtube-nocookie.com"):
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

    if host in ("youtube.com", "youtube-nocookie.com"):
        for path_prefix in ("/shorts/", "/live/", "/embed/"):
            if parsed.path.startswith(path_prefix):
                vid = parsed.path[len(path_prefix) :].lstrip("/").split("/")[0]
                if _VIDEO_ID_RE.match(vid):
                    return f"https://www.youtube.com/watch?v={vid}"
                break

    return url


def validate_playlist_url(url: str) -> str:
    """Accept only a playlist we are willing to expand.

    The SSRF boundary is unchanged: the host must still be one of the same
    allowlisted hosts as a single track. This adds the playlist-shaped checks on
    top, so a bare watch URL or a user's profile page is rejected before yt-dlp
    ever sees it.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidPlaylistURL("URL is required")
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        raise InvalidPlaylistURL(f"could not parse URL: {e}") from e
    if parsed.scheme not in ("http", "https"):
        raise InvalidPlaylistURL("URL must use http or https")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise InvalidPlaylistURL(f"unsupported host: {host or '(empty)'}")

    if host in _SOUNDCLOUD_HOSTS:
        if not _SOUNDCLOUD_SET_RE.match(parsed.path or ""):
            raise InvalidPlaylistURL("not a SoundCloud playlist URL")
        return url

    list_id = urllib.parse.parse_qs(parsed.query or "").get("list", [""])[0]
    if not list_id:
        raise InvalidPlaylistURL("URL has no playlist id")
    if not _PLAYLIST_ID_RE.match(list_id):
        raise InvalidPlaylistURL("invalid playlist id")
    if list_id.upper().startswith("RD"):
        raise InvalidPlaylistURL("radio playlists cannot be imported")
    return f"https://www.youtube.com/playlist?list={list_id}"


def is_playlist_url(url: str) -> bool:
    """Cheap check for the UI: would validate_playlist_url accept this?"""
    try:
        validate_playlist_url(url)
    except InvalidPlaylistURL:
        return False
    return True


def expand_playlist(url: str, limit: int) -> dict:
    """List a playlist's entries without downloading anything.

    Flat extraction, so this is one request rather than one per video. Every
    entry URL is put back through validate_youtube_url before it is returned:
    entries are attacker-influenced data as far as this process is concerned,
    and nothing that failed that check may ever reach the pipeline.
    """
    playlist_url = validate_playlist_url(url)
    ydl_opts = {
        **_base_ydl_opts(_ALLOWED_PLAYLIST_EXTRACTORS),
        "noprogress": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
        # One past the cap, so a playlist longer than the cap can be reported as
        # truncated rather than silently looking like it ends there.
        "playlistend": max(1, limit) + 1,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False) or {}

    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    truncated = len(entries) > limit
    entries = entries[:limit]
    items: list[dict] = []
    unavailable = 0
    for entry in entries:
        raw = entry.get("url") or entry.get("webpage_url") or ""
        try:
            normalized = validate_youtube_url(raw)
        except InvalidYouTubeURL:
            # Deleted, private or region-blocked entries come back as
            # placeholders with no usable URL. Count them so the dialog can say
            # so, and drop them.
            unavailable += 1
            continue
        items.append(
            {
                "url": normalized,
                "title": entry.get("title") or "",
                "duration": entry.get("duration"),
                "thumbnail": entry.get("thumbnail"),
            }
        )

    return {
        "playlist_title": (info.get("title") or "Playlist").strip() or "Playlist",
        "playlist_url": playlist_url,
        "items": items,
        "unavailable": unavailable,
        "truncated": truncated,
    }


def _download_video_track(job: Job, url: str, job_dir: Path, *, use_cookies: bool = False) -> None:
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
        **_base_ydl_opts(_ALLOWED_EXTRACTORS, use_cookies=use_cookies),
        "format": (
            f"bestvideo[height<={max_height}][vcodec^=avc1]"
            f"/bestvideo[height<={max_height}][ext=mp4]"
        ),
        "outtmpl": str(job_dir / "video.%(ext)s"),
        "noprogress": True,
        "noplaylist": True,
        "progress_hooks": [vhook],
    }

    _set(job, stage="Fetching video...")
    # Distinguishes "this video has no MP4 stream to offer" from "the fetch
    # broke", which has_video alone cannot (#436). Only the second is worth
    # telling the user about.
    failed = False
    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except JobCancelled:
        raise
    except Exception as exc:
        if job.cancel_requested:
            raise JobCancelled() from exc
        failed = True
        logger.warning("[%s] video track unavailable (audio-only): %s", job.id, exc)

    video = job_dir / "video.mp4"
    if video.is_file() and video.stat().st_size > 0:
        job.has_video = True
        job.video_status = "ok"
    else:
        job.video_status = "failed" if failed else "unavailable"
        # Drop any partial/non-mp4 leftover so the export endpoint sees nothing.
        for f in job_dir.glob("video.*"):
            f.unlink(missing_ok=True)


def download(job: Job, url: str, job_dir: Path) -> Path:
    url = normalize_youtube_url(url)
    logger.info("[%s] download starting: %s", job.id, url)
    _set(job, status="downloading", progress=0.0, stage="Processing...")

    # Fetch metadata first (no download) so we can reject videos that are
    # too long before wasting bandwidth and disk. Runs under the same retry
    # policy as the download itself -- a transient blip on this first request
    # used to fail the whole job immediately (#279).
    def _probe(use_cookies: bool) -> dict:
        opts = {**_base_ydl_opts(_ALLOWED_EXTRACTORS, use_cookies=use_cookies), "noplaylist": True}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}

    # The bot check lands on this first request, so this is where the cookie
    # fallback is decided. Whether it engaged is remembered below so the fetch
    # does not have to rediscover it.
    probed, needs_cookies = _with_cookie_fallback(job, _probe, what="metadata probe")
    meta: dict = probed if isinstance(probed, dict) else {}
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
    def _fetch(use_cookies: bool) -> dict:
        ydl_opts = {
            **_base_ydl_opts(_ALLOWED_EXTRACTORS, use_cookies=use_cookies),
            "format": "bestaudio/best",
            "outtmpl": str(job_dir / "source.%(ext)s"),
            "noprogress": True,
            "noplaylist": True,
            "progress_hooks": [hook],
        }
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True) or {}

    # If the probe already needed cookies, this request will too -- go straight
    # there rather than spending another round trip proving it again.
    if needs_cookies:
        info: dict = _with_retries(job, lambda: _fetch(True), what="download")
    else:
        info, needs_cookies = _with_cookie_fallback(job, _fetch, what="download")

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
        _download_video_track(job, url, job_dir, use_cookies=needs_cookies)

    candidates = sorted(job_dir.glob("source.*"))
    if not candidates:
        raise RuntimeError("yt-dlp finished but no source file was produced")
    logger.info("[%s] download complete: %s", job.id, candidates[0].name)
    return candidates[0]
