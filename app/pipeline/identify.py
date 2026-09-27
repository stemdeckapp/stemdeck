"""Which recording a job is, found while it separates.

Best source first, stopping at the first answer:

1. AcoustID, when the user has set a key in Settings: an audio fingerprint of
   the first FINGERPRINT_LENGTH_SEC seconds, made with FFmpeg's chromaprint
   muxer (or fpcalc where this FFmpeg has none, see fpcalc.py), matched
   against AcoustID's database. A match scoring at least
   ACOUSTID_MIN_SCORE names a MusicBrainz recording, which MusicBrainz then
   describes (credited title and artists, the album, its type).
2. A MusicBrainz recording search by the tags' artist and title (or the song's
   name cleaned out of the video title), kept only when it is unmistakable:
   see musicbrainz.search_recording.
3. The title, read every way it can be (title_parse.py: "Artist - Song",
   'Song (From "Work")', "Song   Performers", "Work Cast - Song"...), each
   reading searched for on MusicBrainz and kept only when a recording that
   length credits the title's other words (musicbrainz.search_by_title); then
   the artist a reading names, looked up by name and alias in any script, and
   the song among that artist's recordings, a music video's longer length
   allowed for (identify_by_artist). When MusicBrainz has none, LRCLIB is
   asked the same way, and its artist and album name the track (source
   "lrclib"). This is all a YouTube upload with no music metadata has, and a
   second opinion when the tags named something MusicBrainz does not know.
4. The tags alone, as source "tags", when they name both an artist and a song.

The answer is ``job.identity`` (clean_identity's shape). The band a job's
artist is (``job.artist``) is then found through it: the recording's first
credited artist's MusicBrainz id, that artist's Wikidata link, and the
Wikidata item naming the same MusicBrainz artist back. Without an identity
with artist ids, the band is searched for by name as before (artist_lookup),
then by the names the tag or the title give, on MusicBrainz (find_band_for):
a compilation matches no recording, and its artist box still shows the band.

What is sent: to AcoustID a fingerprint (not audio) and the track's length;
to MusicBrainz, Wikidata and LRCLIB, ids, names from the track (artist, song,
album or show) and its length. Everything here is best-effort
and never raises into the pipeline: no key, no chromaprint in this FFmpeg and
no fpcalc, no connection or no match all leave the job with less, never failed.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import (
    ACOUSTID_LOOKUP_URL,
    ACOUSTID_MIN_SCORE,
    FINGERPRINT_LENGTH_SEC,
    IDENTIFY_MAX_BYTES,
    TIMEOUT_FINGERPRINT,
    TIMEOUT_IDENTIFY_REQUEST,
    TITLE_ARTIST_SEARCHES,
    TITLE_LRCLIB_SEARCHES,
    TITLE_MUSICBRAINZ_SEARCHES,
    ffmpeg_executable,
)
from app.core.models import MBID_RE, Job, _set, clean_identity
from app.core.settings import get_acoustid_api_key
from app.pipeline import fpcalc, musicbrainz, ratelimit, title_parse
from app.pipeline.artist_lookup import (
    _ssl_context,
    find_band,
    lookup_band_by_id,
    tagged_artist_name,
)
from app.pipeline.work_lookup import find_work, might_have_work

logger = logging.getLogger("stemdeck.identify")

# fpcalc's default algorithm. Chromaprint numbers its algorithms from 0 in
# its API (TEST1..TEST5) and FFmpeg passes the number straight through, so the
# muxer's 1 is CHROMAPRINT_ALGORITHM_TEST2, the one every fingerprint in
# AcoustID's database was made with. Its fingerprints start "AQ".
CHROMAPRINT_ALGORITHM = "1"
# A compressed, base64 fingerprint, as fpcalc prints and AcoustID takes it.
# URL-safe alphabet, no padding; two minutes come to a few kilobytes.
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_\-+/]{16,65536}$")
# FFmpeg builds without --enable-chromaprint (see the report in the PR):
# "Requested output format 'chromaprint' is not known" or, in older builds,
# "Unknown output format".
_NO_MUXER_RE = re.compile(r"output format.*chromaprint|chromaprint.*not known", re.IGNORECASE)

# FFmpeg executables found to have no chromaprint muxer, so a machine without
# one does not spawn a doomed process for every import.
_NO_CHROMAPRINT: set[str] = set()
# The fingerprint processes running now, by job id, so the pipeline can make
# sure none still has the source open before it deletes it (release_source).
_RUNNING: dict[str, tuple[subprocess.Popen, threading.Event]] = {}
_RUNNING_LOCK = threading.Lock()


# ── the fingerprint ──


def fingerprint_command(ffmpeg: str, audio: list[Path], length: int) -> list[str]:
    """The FFmpeg command that prints the fingerprint of the first ``length``
    seconds of ``audio``: one file, or several played together (the stems of
    a track whose source is gone, which add back up to the mix)."""
    return decode_command(ffmpeg, audio, length) + [
        "-f",
        "chromaprint",
        "-algorithm",
        CHROMAPRINT_ALGORITHM,
        "-fp_format",
        "base64",
        "-",
    ]


def decode_command(ffmpeg: str, audio: list[Path], length: int) -> list[str]:
    """fingerprint_command up to its output: the first ``length`` seconds of
    ``audio``, summed when there are several. Also what feeds fpcalc stems,
    which it cannot mix itself."""
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    for path in audio:
        # An input option, so FFmpeg stops reading after ``length`` seconds
        # instead of decoding the whole file and throwing the rest away.
        cmd += ["-t", str(length), "-i", str(path)]
    if len(audio) > 1:
        # normalize=0: the stems are parts of one mix, so they are summed,
        # not averaged. Chromaprint is level-independent either way.
        inputs = "".join(f"[{i}:a]" for i in range(len(audio)))
        cmd += [
            "-filter_complex",
            f"{inputs}amix=inputs={len(audio)}:normalize=0[mix]",
            "-map",
            "[mix]",
        ]
    else:
        cmd += ["-vn"]
    return cmd


def parse_fingerprint(stdout: bytes | str | None) -> str | None:
    """The fingerprint FFmpeg printed, or None when it printed none."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("ascii", errors="replace")
    text = (stdout or "").strip()
    return text if _FINGERPRINT_RE.match(text) else None


def release_source(job_id: str, timeout: float = 5.0) -> None:
    """Make sure no fingerprint of ``job_id`` still has its source open.

    Called by the pipeline before it deletes a link's source: on Windows a
    file another process has open cannot be deleted, and that failure would
    fail the job. A fingerprint takes about a second and the separation
    before this minutes, so there is nearly always nothing to do; one still
    running is stopped, and identification goes on without it."""
    with _RUNNING_LOCK:
        entry = _RUNNING.get(job_id)
    if entry is None:
        return
    proc, done = entry
    if done.is_set():
        return
    try:
        proc.kill()
    except OSError:
        pass
    done.wait(timeout)


def fingerprint(
    audio: list[Path],
    *,
    job_id: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> str | None:
    """The chromaprint fingerprint of ``audio``, or None. Never raises.

    Made by FFmpeg's chromaprint muxer, or by fpcalc once this FFmpeg is known
    to have none (fpcalc.py). Neither: None, and the track is named by its
    tags."""
    audio = [p for p in audio if p.is_file()]
    if not audio or cancelled():
        return None
    ffmpeg = ffmpeg_executable()
    if ffmpeg in _NO_CHROMAPRINT:
        return _fingerprint_with_fpcalc(ffmpeg, audio, job_id=job_id, cancelled=cancelled)
    cmd = fingerprint_command(ffmpeg, audio, FINGERPRINT_LENGTH_SEC)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        logger.info("could not start FFmpeg for a fingerprint", exc_info=True)
        return None
    output = _wait(proc, job_id=job_id, cancelled=cancelled)
    if output is None:
        return None
    stdout, stderr = output
    if proc.returncode != 0:
        message = (stderr or b"").decode("utf-8", errors="replace")
        if _NO_MUXER_RE.search(message):
            _NO_CHROMAPRINT.add(ffmpeg)
            logger.warning("this FFmpeg has no chromaprint muxer; fingerprinting with fpcalc")
            return _fingerprint_with_fpcalc(ffmpeg, audio, job_id=job_id, cancelled=cancelled)
        logger.info("fingerprint failed (exit %s): %s", proc.returncode, message[-300:])
        return None
    return parse_fingerprint(stdout)


def _fingerprint_with_fpcalc(
    ffmpeg: str,
    audio: list[Path],
    *,
    job_id: str,
    cancelled: Callable[[], bool],
) -> str | None:
    """fingerprint() by fpcalc, for an FFmpeg without the muxer. One file is
    read by fpcalc itself; stems are summed by FFmpeg (decode_command, which
    needs no muxer) and piped to fpcalc as WAV. Never raises."""
    exe = fpcalc.fpcalc_executable()
    if exe is None:
        logger.info("no fpcalc either; tracks are identified by their tags only")
        return None
    upstream: subprocess.Popen | None = None
    try:
        if len(audio) == 1:
            source = str(audio[0].absolute())
            stdin: Any = subprocess.DEVNULL
        else:
            upstream = subprocess.Popen(
                decode_command(ffmpeg, audio, FINGERPRINT_LENGTH_SEC) + ["-f", "wav", "-"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            source, stdin = "-", upstream.stdout
        try:
            proc = subprocess.Popen(
                fpcalc.fpcalc_command(exe, source, FINGERPRINT_LENGTH_SEC),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        finally:
            # fpcalc holds its own copy; with none left here, FFmpeg sees a
            # closed pipe, not a stall, if fpcalc goes first.
            if upstream is not None and upstream.stdout is not None:
                upstream.stdout.close()
    except OSError:
        if upstream is not None:
            upstream.kill()
            upstream.wait()
        logger.info("could not start fpcalc for a fingerprint", exc_info=True)
        return None
    output = _wait(proc, job_id=job_id, cancelled=cancelled, upstream=upstream)
    if output is None:
        return None
    stdout, stderr = output
    if proc.returncode != 0:
        message = (stderr or b"").decode("utf-8", errors="replace")
        logger.info("fpcalc failed (exit %s): %s", proc.returncode, message[-300:])
        return None
    return parse_fingerprint(fpcalc.parse_fpcalc_output(stdout))


def _wait(
    proc: subprocess.Popen,
    *,
    job_id: str,
    cancelled: Callable[[], bool],
    upstream: subprocess.Popen | None = None,
) -> tuple[bytes, bytes] | None:
    """(stdout, stderr) of a fingerprint process once it exits, or None when
    it was abandoned: cancelled, or still running after TIMEOUT_FINGERPRINT.
    Registered for release_source meanwhile. ``upstream`` (FFmpeg feeding
    fpcalc) is stopped with it, and reaped before release_source is told the
    audio is closed."""
    done = threading.Event()
    if job_id:
        with _RUNNING_LOCK:
            _RUNNING[job_id] = (proc, done)
    try:
        # Not registered with set_proc: that slot is the separation's, which
        # runs at the same time. Cancel is polled here instead.
        deadline = time.monotonic() + TIMEOUT_FINGERPRINT
        while True:
            try:
                return proc.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                if cancelled() or time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    logger.info("fingerprint abandoned (cancelled or too slow)")
                    return None
    finally:
        if upstream is not None:
            # Done with its output either way. Normally it has already exited
            # (-t bounds it); after a kill of fpcalc it would only die of the
            # closed pipe a moment later.
            if upstream.poll() is None:
                upstream.kill()
            upstream.wait()
        done.set()
        if job_id:
            with _RUNNING_LOCK:
                if _RUNNING.get(job_id, (None,))[0] is proc:
                    _RUNNING.pop(job_id, None)


# ── AcoustID ──


class AcoustIDRefused(OSError):
    """A lookup AcoustID refused, with its error code: 4 is a key it does not
    know, 3 a fingerprint it cannot read."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(message)
        self.code = code


# AcoustID's error code for a key it does not accept.
_ACOUSTID_BAD_KEY = 4


def _acoustid_request(form: dict[str, str]) -> Any:
    """One AcoustID lookup, after waiting for its turn. Raises on any failure.

    POSTed rather than put in a URL: the fingerprint is kilobytes long, and
    the key then never appears in a URL that something might log. An error
    AcoustID explains (a bad key, say) is raised with its message, which
    never contains the key."""
    ratelimit.ACOUSTID.wait()
    body = urllib.parse.urlencode(form).encode("ascii")
    request = urllib.request.Request(
        ACOUSTID_LOOKUP_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": musicbrainz.MUSICBRAINZ_USER_AGENT,
        },
    )
    try:
        # A fixed https URL, never one from a request or a tag (B310).
        with urllib.request.urlopen(  # nosec B310
            request, timeout=TIMEOUT_IDENTIFY_REQUEST, context=_ssl_context()
        ) as response:
            answer = response.read(IDENTIFY_MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        # AcoustID answers a refused lookup with 400 and a JSON reason.
        try:
            error = json.loads(err.read(64 * 1024)).get("error", {})
            reason, code = error.get("message"), error.get("code")
        except Exception:
            reason, code = None, None
        raise AcoustIDRefused(
            code if isinstance(code, int) else None,
            f"AcoustID refused the lookup ({err.code}): {reason or 'no reason'}",
        ) from None
    if len(answer) > IDENTIFY_MAX_BYTES:
        raise ValueError("AcoustID answer too large")
    return json.loads(answer)


def acoustid_key_works(key: str) -> bool | None:
    """Whether AcoustID accepts ``key`` for lookups: True or False, or None
    when it cannot be asked (offline, AcoustID down), and the key is then
    kept on trust.

    Asked with a fingerprint that is not one: a key AcoustID knows is refused
    for the fingerprint, one it does not for the key. The key AcoustID's site
    shows on a user's profile is for submitting fingerprints, not for looking
    them up, and is the one people paste; this is what tells them."""
    form = {"client": key, "duration": "30", "fingerprint": "AQAA", "format": "json"}
    try:
        answer = _acoustid_request(form)
    except AcoustIDRefused as err:
        return err.code != _ACOUSTID_BAD_KEY
    except Exception:
        logger.info("AcoustID could not be asked whether the key works", exc_info=True)
        return None
    error = answer.get("error") if isinstance(answer, dict) else None
    if isinstance(error, dict):
        return error.get("code") != _ACOUSTID_BAD_KEY
    return True


def best_acoustid_match(
    answer: Any, duration: float | None, min_score: float = ACOUSTID_MIN_SCORE
) -> tuple[float, dict[str, Any]] | None:
    """(score, recording) for the best AcoustID result scoring at least
    ``min_score`` that is linked to a MusicBrainz recording, or None.

    A fingerprint can match several recordings (the same audio released
    twice, merged duplicates): the one named in full whose length is nearest
    the track's is taken."""
    if not isinstance(answer, dict) or answer.get("status") != "ok":
        return None
    results = answer.get("results")
    ranked = sorted(
        (
            r
            for r in (results if isinstance(results, list) else [])
            if isinstance(r, dict) and isinstance(r.get("score"), (int, float))
        ),
        key=lambda r: -r["score"],
    )
    for result in ranked:
        score = float(result["score"])
        if score < min_score:
            return None
        recordings = [
            rec
            for rec in (result.get("recordings") or [])
            if isinstance(rec, dict) and isinstance(rec.get("id"), str) and MBID_RE.match(rec["id"])
        ]
        if not recordings:
            continue

        def rank(rec: dict[str, Any]) -> tuple:
            named = bool(rec.get("title")) and bool(rec.get("artists"))
            length = rec.get("duration")
            off = (
                abs(float(length) - duration)
                if duration and isinstance(length, (int, float)) and not isinstance(length, bool)
                else 0.0
            )
            return (not named, off)

        return score, min(recordings, key=rank)
    return None


def _recording_from_acoustid(rec: dict[str, Any]) -> dict[str, Any]:
    """AcoustID's own description of a recording, in MusicBrainz's shape, for
    when MusicBrainz cannot be asked."""
    credit = []
    for artist in rec.get("artists") or []:
        if isinstance(artist, dict) and isinstance(artist.get("name"), str):
            credit.append(
                {
                    "name": artist["name"],
                    "joinphrase": artist.get("joinphrase") or "",
                    "artist": {"id": artist.get("id"), "name": artist["name"]},
                }
            )
    releases = []
    for group in rec.get("releasegroups") or []:
        if isinstance(group, dict):
            releases.append(
                {
                    "release-group": {
                        "id": group.get("id"),
                        "title": group.get("title"),
                        "primary-type": group.get("type"),
                        "secondary-types": group.get("secondarytypes") or [],
                    }
                }
            )
    duration = rec.get("duration")
    return {
        "id": rec.get("id"),
        "title": rec.get("title"),
        "length": duration * 1000 if isinstance(duration, (int, float)) else None,
        "artist-credit": credit,
        "releases": releases,
    }


def identify_by_fingerprint(
    audio: list[Path],
    duration: float | None,
    api_key: str,
    *,
    job_id: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The identity AcoustID gives ``audio``, or None. Raises on a failed
    request; fingerprinting itself never raises."""
    if not api_key or not duration or duration <= 0:
        return None
    print_ = fingerprint(audio, job_id=job_id, cancelled=cancelled)
    if not print_ or cancelled():
        return None
    answer = _acoustid_request(
        {
            "client": api_key,
            "format": "json",
            "meta": "recordings releasegroups compress",
            "duration": str(round(duration)),
            "fingerprint": print_,
        }
    )
    match = best_acoustid_match(answer, duration)
    if match is None:
        logger.info("[%s] AcoustID knows no recording for this fingerprint", job_id)
        return None
    score, rec = match
    identity = None
    if not cancelled():
        try:
            recording = musicbrainz.lookup_recording(rec["id"])
            identity = musicbrainz.identity_from_recording(
                recording, source="acoustid", score=score, fallback_duration=duration
            )
        except Exception:
            logger.info("[%s] MusicBrainz lookup failed; using AcoustID's", job_id, exc_info=True)
    return identity or musicbrainz.identity_from_recording(
        _recording_from_acoustid(rec), source="acoustid", score=score, fallback_duration=duration
    )


# ── the title ──


def identify_by_title(
    *,
    tags: dict[str, str] | None,
    title: str | None,
    duration: float | None,
    job_id: str = "",
    lrclib: bool = True,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The identity a track's title (and its tags' title) names, or None.

    Each reading of the title (title_parse.readings_for) is searched for on
    MusicBrainz, best first, up to TITLE_MUSICBRAINZ_SEARCHES; the first
    confident answer wins (musicbrainz.best_title_match). When MusicBrainz has
    none, or cannot be reached, LRCLIB is asked as an independent check, up
    to TITLE_LRCLIB_SEARCHES readings (lyrics_lookup.identify_on_lrclib),
    unless ``lrclib`` is False: tags naming artist and song already give the
    lyrics lookup its names. Nothing without the track's length: a name alone
    can be anybody's song. Never raises."""
    # Imported here: lyrics_lookup is the newer module and may come to
    # depend on this one.
    from app.pipeline.lyrics_lookup import identify_on_lrclib

    if not duration or duration <= 0:
        return None
    readings = title_parse.readings_for(tags, title)
    reachable = True
    for reading in readings[:TITLE_MUSICBRAINZ_SEARCHES]:
        if cancelled():
            return None
        try:
            identity = musicbrainz.search_by_title(reading, duration, cancelled=cancelled)
        except Exception:
            # Down, or the rate limit's queue too long: no point asking again.
            logger.info("[%s] MusicBrainz title search failed", job_id, exc_info=True)
            reachable = False
            break
        if identity:
            return identity
    if reachable and not cancelled():
        try:
            identity = identify_by_artist(readings, title, duration, cancelled=cancelled)
        except Exception:
            logger.info("[%s] MusicBrainz artist search failed", job_id, exc_info=True)
            identity = None
        if identity:
            return identity
    for reading in readings[:TITLE_LRCLIB_SEARCHES] if lrclib else []:
        if cancelled():
            return None
        try:
            identity = identify_on_lrclib(reading, duration, cancelled=cancelled)
        except Exception:
            logger.info("[%s] LRCLIB title search failed", job_id, exc_info=True)
            break
        if identity:
            return identity
    return None


def identify_by_artist(
    readings: list[title_parse.TitleReading],
    title: str | None,
    duration: float,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The identity found by looking the artist up first, or None.

    For when no recording credits the title's words as they are written: the
    title names 周杰倫 as "Jay Chou", 鄧麗君 as "邓丽君", or its video is longer
    than the album cut. Each reading's artist is searched for by name and
    alias (musicbrainz.search_artists), then the song among that artist's
    recordings (musicbrainz.search_by_artist), for up to
    TITLE_ARTIST_SEARCHES readings. A title with nothing else to read in it,
    no separator at all ("美空ひばり 川の流れのように"), is cut every way it can
    be, and each cut that names an artist is tried, the longest name first.
    Raises when MusicBrainz cannot be reached."""
    tried: set[tuple[str, ...]] = set()
    for reading in readings:
        if len(tried) >= TITLE_ARTIST_SEARCHES or cancelled():
            break
        names = title_parse.artist_names(reading)
        key = tuple(title_parse.name_key(n) for n in names)
        if not names or key in tried:
            continue
        tried.add(key)
        artists = musicbrainz.search_artists(names, cancelled=cancelled)
        ids = [a["id"] for a in artists]
        if ids:
            identity = musicbrainz.search_by_artist(
                title_parse.song_names(reading), ids, duration, cancelled=cancelled
            )
            if identity:
                return identity
    if readings:
        return None
    cuts = title_parse.name_splits(title)
    if not cuts or cancelled():
        return None
    artists = musicbrainz.search_artists([a for a, _ in cuts], cancelled=cancelled)
    by_name: dict[str, list[str]] = {}
    for artist in artists:
        by_name.setdefault(title_parse.name_key(artist["matched"]), []).append(artist["id"])
    songs = {title_parse.name_key(a): s for a, s in cuts}
    for name in sorted(by_name, key=len, reverse=True)[:TITLE_ARTIST_SEARCHES]:
        if cancelled():
            return None
        identity = musicbrainz.search_by_artist(
            [songs[name]], by_name[name], duration, cancelled=cancelled
        )
        if identity:
            return identity
    return None


def can_identify_title(tags: dict[str, str] | None, title: str | None) -> bool:
    """Whether identify_by_title has a reading to go on: one naming more than
    a song, or a title an artist's name could be cut from; or, for the band
    alone, a compilation's title (band_from_title)."""
    return (
        bool(title_parse.readings_for(tags, title))
        or bool(title_parse.name_splits(title))
        or title_parse.title_names(title)[1]
    )


# ── the whole answer ──


def identify(
    *,
    tags: dict[str, str] | None,
    title: str | None,
    duration: float | None,
    audio: list[Path],
    api_key: str | None,
    job_id: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The identity of a track, best source first (see the module), or None
    when nothing names it, with the other titles its song goes by
    (title_aliases). Never raises."""
    identity = _identify(
        tags=tags,
        title=title,
        duration=duration,
        audio=audio,
        api_key=api_key,
        job_id=job_id,
        cancelled=cancelled,
    )
    if identity is None or cancelled():
        return identity
    aliases = title_aliases(identity, tags, title, cancelled=cancelled)
    return clean_identity({**identity, "title_aliases": aliases}) if aliases else identity


def title_aliases(
    identity: dict[str, Any],
    tags: dict[str, str] | None,
    title: str | None,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[str]:
    """The other titles an identified song goes by, for the lyrics lookup:
    LRCLIB files 좋은 날 as "Good Day" as often as not. The upload's own
    ("IU _ Good Day (좋은 날)": whichever of its readings names the song the
    identity does), then, for a song in Chinese, Japanese or Korean, where
    they matter, its MusicBrainz work's title and aliases. Never raises."""
    song = title_parse.song_key(identity.get("title"))
    found: list[str] = []
    for reading in [*title_parse.readings_for(tags, title), *title_parse.parse_title(title)]:
        names = title_parse.song_names(reading)
        if song in {title_parse.song_key(n) for n in names}:
            found += names
    recording = identity.get("recording_mbid")
    if recording and title_parse.is_east_asian(str(identity.get("title") or "")):
        try:
            found += musicbrainz.work_titles(recording, cancelled=cancelled)
        except Exception:
            logger.info("work titles lookup failed", exc_info=True)
    aliases: list[str] = []
    keys = {song}
    for name in found:
        key = title_parse.song_key(name)
        if key and key not in keys:
            keys.add(key)
            aliases.append(name.strip())
    return aliases


def _identify(
    *,
    tags: dict[str, str] | None,
    title: str | None,
    duration: float | None,
    audio: list[Path],
    api_key: str | None,
    job_id: str = "",
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    # Imported here: lyrics_lookup is the newer module and may come to
    # depend on this one.
    from app.pipeline.lyrics_lookup import song_from_title

    tags = tags or {}
    if api_key and audio:
        try:
            identity = identify_by_fingerprint(
                audio, duration, api_key, job_id=job_id, cancelled=cancelled
            )
            if identity:
                return identity
        except Exception:
            logger.info("[%s] AcoustID lookup failed", job_id, exc_info=True)
    if cancelled():
        return None
    artist = tagged_artist_name(tags.get("artist"))
    song = song_from_title(tags.get("title"), artist) or (tags.get("title") or "").strip()
    if song and artist:
        # A song in marks or among East Asian noise ("「群青」Official Music
        # Video", a channel's title less its name): the title's own reading.
        readings = title_parse.readings_for({"artist": artist, "title": song})
        if readings and title_parse.name_key(readings[0].artist) in {
            title_parse.name_key(n) for n in title_parse.name_alternatives(artist)
        }:
            song = readings[0].song
    if not song and artist:
        song = song_from_title(title, artist)
    if artist and song:
        try:
            identity = musicbrainz.search_recording(artist, song, duration, cancelled=cancelled)
            if identity:
                return identity
        except Exception:
            logger.info("[%s] MusicBrainz search failed", job_id, exc_info=True)
    # The title: all a YouTube upload of a cast recording has, and a second
    # opinion when the tags named something MusicBrainz does not know.
    if cancelled():
        return None
    identity = identify_by_title(
        tags=tags,
        title=title,
        duration=duration,
        job_id=job_id,
        lrclib=not (artist and song),
        cancelled=cancelled,
    )
    if identity or not artist or not song:
        return identity
    return clean_identity(
        {
            "source": "tags",
            # Nothing checked it: the file says so, and that is all.
            "score": 0.0,
            "title": song,
            "artist": tags.get("artist") or artist,
            "album": tags.get("album"),
            "duration": duration,
        }
    )


def find_band_for(
    identity: dict[str, Any] | None,
    artist_tag: Any,
    *,
    title: str | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The band for a track, or None. Never raises.

    Through the identity's first credited artist (MusicBrainz artist -> its
    Wikidata link -> the item naming it back), else by searching Wikidata for
    the tag's artist name, or the identity's, with the name search's own
    strict rule. When that finds nothing, or there is no name at all but a
    ``title`` (a compilation, a live cut, a fan's upload of another length:
    nothing a recording search could match), the names the tag or the title
    give ("鄧麗君 Teresa Teng テレサ・テン", "邓丽君经典金曲 - Teresa Teng's
    classic songs") are looked up on MusicBrainz by name and alias, and the
    one artist they name, when it is only one, is the band."""
    if identity and identity.get("source") != "tags" and identity.get("artist_mbids"):
        band = _band_by_artist_id(
            identity["artist_mbids"][0],
            tagged_artist_name(identity.get("artist")),
            cancelled=cancelled,
        )
        if band:
            return band
    if cancelled():
        return None
    name = tagged_artist_name(artist_tag) or tagged_artist_name((identity or {}).get("artist"))
    if name:
        band = find_band(name, cancelled=cancelled)
        if band:
            return band
        return _band_by_other_names(name, cancelled=cancelled)
    return band_from_title(title, cancelled=cancelled) if title else None


def _band_by_other_names(name: str, *, cancelled: Callable[[], bool]) -> dict[str, str] | None:
    """The band an artist tag or credit names by one of its other names
    ("鄧麗君 Teresa Teng テレサ・テン" is three), found on MusicBrainz by name
    and alias when only one artist is that likely, else on Wikidata by each
    of the first two. Never raises."""
    names = title_parse.name_alternatives(name)
    if len(names) < 2 or cancelled():
        return None
    try:
        artists = musicbrainz.search_artists(names, cancelled=cancelled)
    except Exception:
        logger.info("band lookup by MusicBrainz name failed", exc_info=True)
        artists = []
    artist = _only_artist(artists)
    if artist:
        band = _band_by_artist_id(artist["id"], artist["name"], cancelled=cancelled)
        if band:
            return band
    # Wikidata by each other name, as the tag's own name was: its label may
    # be in either script ("周杰倫 Jay Chou" is neither label whole).
    for other in names[1:3]:
        if cancelled():
            return None
        band = find_band(other, cancelled=cancelled)
        if band:
            return band
    return None


def band_from_title(
    title: str | None, *, cancelled: Callable[[], bool] = lambda: False
) -> dict[str, str] | None:
    """The band a title names with nothing else to go by (no recording
    found, no artist tag), or None. Never raises.

    Only on evidence a title cannot give by chance, since a wrong band is
    worse than none ("Metropolis - Part I" once made Metropolis the band):

    * two names in different scripts that MusicBrainz knows, exactly (by
      name, sort name or alias, never a near miss), as one artist and no
      other: "邓丽君经典金曲 - Teresa Teng's classic songs", "周杰倫 Jay Chou";
    * or a compilation's title (its words taken off: "经典金曲", "Greatest
      Hits") every part of which names that one artist and nobody else.

    That artist's Wikidata item, which must name the same MusicBrainz artist
    back (and so be a musician or a band), is the band, as for a recording."""
    groups, compilation = title_parse.title_names(title)
    names = [n for group in groups for n in group]
    if not names or cancelled():
        return None
    try:
        artists = musicbrainz.search_artists(names, cancelled=cancelled)
    except Exception:
        logger.info("band lookup by MusicBrainz name failed", exc_info=True)
        return None
    ids_by_name: dict[str, set[str]] = {}
    for artist in artists:
        for matched in artist["matches"]:
            ids_by_name.setdefault(title_parse.name_key(matched), set()).add(artist["id"])

    def ids(name: str) -> set[str]:
        return ids_by_name.get(title_parse.name_key(name), set())

    chosen: set[str] = set()
    # Two scripts: each artist id with the scripts of the names naming it.
    scripts: dict[str, set[str]] = {}
    for name in names:
        for mbid in ids(name):
            scripts.setdefault(mbid, set()).add(title_parse.script_of(name))
    chosen = {mbid for mbid, seen in scripts.items() if len(seen) > 1}
    if not chosen and compilation:
        # Every part names the artist: the one id all of them share.
        per_part = [set().union(*(ids(n) for n in group)) for group in groups]
        if all(per_part):
            chosen = set.intersection(*per_part)
    if len(chosen) != 1 or cancelled():
        return None
    mbid = chosen.pop()
    name = next(a["name"] for a in artists if a["id"] == mbid)
    return _band_by_artist_id(mbid, name, cancelled=cancelled)


def _band_by_artist_id(
    mbid: str, name: str, *, cancelled: Callable[[], bool]
) -> dict[str, str] | None:
    """The band a MusicBrainz artist is on Wikidata, or None. Never raises."""
    try:
        qid = musicbrainz.artist_wikidata_id(mbid)
        if qid and not cancelled():
            return lookup_band_by_id(qid, mbid, name=name, cancelled=cancelled)
    except Exception:
        logger.info("band lookup by MusicBrainz id failed", exc_info=True)
    return None


# Two artists matching the same name this close in MusicBrainz's score are
# as likely as each other: neither is taken for the band without a recording
# to say which ("BTS" the group scores 100, the next "BTS" 68).
_ARTIST_SCORE_MARGIN = 10


def _only_artist(artists: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one artist a name search found for the first name that matched
    any, or None when none did or two are as likely."""
    if not artists:
        return None
    first = artists[0]
    same = [a for a in artists if a["matched"] == first["matched"]]
    if len(same) > 1 and same[0]["score"] - same[1]["score"] < _ARTIST_SCORE_MARGIN:
        return None
    return first


def can_identify(
    tags: dict[str, str] | None,
    api_key: str | None,
    audio: list[Path],
    title: str | None = None,
) -> bool:
    """Whether identify() has anything to go on: a key and audio to
    fingerprint, an artist tag, or a title naming more than a song."""
    return (
        bool(api_key and audio)
        or bool(tagged_artist_name((tags or {}).get("artist")))
        or can_identify_title(tags, title)
    )


def identify_and_find_band(
    job: Job,
    audio: list[Path],
    *,
    tags: dict[str, str] | None,
    want_band: bool,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """(identity, band) for a job, blocking: the job's identity when it has
    one, else a fresh one; the band only when ``want_band``. Reads the job,
    never writes it. For the tag backfill. Never raises."""
    identity = job.identity
    if identity is None and can_identify(tags, get_acoustid_api_key(), audio, job.title):
        identity = identify(
            tags=tags,
            title=job.title,
            duration=job.duration_sec,
            audio=audio,
            api_key=get_acoustid_api_key(),
            job_id=job.id,
            cancelled=cancelled,
        )
    band = None
    if want_band and not cancelled():
        band = find_band_for(
            identity, (tags or {}).get("artist"), title=job.title, cancelled=cancelled
        )
    return identity, band


class IdentifyLookup:
    """One job's identification and band lookup, in a thread of its own, so
    separation never waits for it. BandLookup's contract, which this replaces
    in the pipeline: the thread never touches the job, and finish() writes the
    answer from the pipeline's own thread once the pipeline has got that far
    uncancelled. A job cancelled or failed meanwhile is never written to.

    The identity is published the moment it is known (wait_identity), before
    the band is looked up through it, for anything else running beside
    separation that needs it (the lyrics lookup). Then the band, then the work
    a soundtrack or cast recording is from (work_lookup.py), each kept by
    finish() once its own step is done.
    """

    def __init__(
        self,
        job: Job,
        audio: list[Path],
        api_key: str | None,
        want_band: bool,
        want_work: bool = False,
    ) -> None:
        self._identity: dict[str, Any] | None = None
        self._band: dict[str, str] | None = None
        self._work: dict[str, str] | None = None
        self._identity_ready = threading.Event()
        self._band_done = threading.Event()
        self._work_done = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(job, list(audio), api_key, want_band, want_work),
            name=f"identify-{job.id}",
            daemon=True,
        )

    def _run(
        self, job: Job, audio: list[Path], api_key: str | None, want_band: bool, want_work: bool
    ) -> None:
        tags = dict(job.audio_tags or {})
        cancelled = lambda: job.cancel_requested  # noqa: E731
        try:
            identity = job.identity or identify(
                tags=tags,
                title=job.title,
                duration=job.duration_sec,
                audio=audio,
                api_key=api_key,
                job_id=job.id,
                cancelled=cancelled,
            )
        except Exception:
            # identify() never raises; this is for a future change that does.
            logger.info("[%s] identification failed", job.id, exc_info=True)
            identity = job.identity
        self._identity = identity
        self._identity_ready.set()
        if want_band and not cancelled():
            self._band = find_band_for(
                identity, tags.get("artist"), title=job.title, cancelled=cancelled
            )
        self._band_done.set()
        if want_work and not cancelled():
            self._work = find_work(identity, tags, title=job.title, cancelled=cancelled)
        self._work_done.set()

    @classmethod
    def start(cls, job: Job, audio: list[Path]) -> IdentifyLookup | None:
        """Start, or None when there is nothing to find: the identity and the
        band already known (a re-split inherits both), or nothing to go on
        (no key to fingerprint with, no artist tag and no title naming more
        than a song)."""
        api_key = get_acoustid_api_key()
        tags = job.audio_tags or {}
        want_identity = job.identity is None and can_identify(tags, api_key, audio, job.title)
        has_name = bool(
            tagged_artist_name(tags.get("artist")) or (job.identity or {}).get("artist")
        )
        want_band = job.artist is None and (want_identity or has_name)
        # The work behind a soundtrack: known only once the identity is, so
        # asked for whenever an identity is being found, or one already known
        # (or the album tag) says the recording is a soundtrack.
        want_work = job.work is None and (
            want_identity or might_have_work(job.identity, tags, job.title)
        )
        if not want_identity and not want_band and not want_work:
            return None
        lookup = cls(job, audio if api_key else [], api_key, want_band, want_work)
        lookup._thread.start()
        return lookup

    def wait_identity(self, timeout: float) -> dict[str, Any] | None:
        """The identity, waiting up to ``timeout`` seconds for it: None when
        there is none or it is not known yet. Safe from any thread."""
        self._identity_ready.wait(timeout)
        return self._identity

    def finish(self, job: Job, timeout: float) -> None:
        """Wait up to ``timeout`` seconds, then keep what was found on the job,
        unless the job was cancelled meanwhile. An identity found in time is
        kept even when the band behind it is still out, and a band even when
        the work is."""
        self._thread.join(timeout)
        alive = self._thread.is_alive()
        if alive:
            logger.info("[%s] identification still out after %ss; finishing", job.id, timeout)
        if job.cancel_requested:
            return
        fields: dict[str, object] = {}
        if self._identity_ready.is_set() and self._identity and job.identity is None:
            fields["identity"] = self._identity
        if self._band_done.is_set() and self._band and job.artist is None:
            fields["artist"] = self._band
        if self._work_done.is_set() and self._work and job.work is None:
            fields["work"] = self._work
        if fields:
            _set(job, **fields)
