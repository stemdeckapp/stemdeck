"""A job's lyrics, found on LRCLIB while it separates, kept as lyrics.json.

The Lyrics tab used to find them itself, in the browser, the first time it was
opened on a track. Done here, beside separation the way the band is
(artist_lookup.py), a track has its lyrics the moment the import is done, on
every device that opens it.

What is looked up, best source first: the recording the job was identified as
(``job.identity``), else the file's own tags, else the band saved on the job
with the song's name cleaned out of the title. Lyrics the file itself carried
win over anything LRCLIB has, and need no request at all.

The LRCLIB cascade, stopping at the first answer kept:

(a) /api/get with artist, track, album and length: LRCLIB's exact match;
(b) a search by artist and track, ranked by length (the page's rankMatches);
    when that finds nothing, the album as the artist, since LRCLIB files a
    cast or soundtrack recording under the show ("Popular" by "Wicked");
(c) a search by the track's name alone.

When the track's length is known, each step first looks for a version within
LYRICS_SAME_RECORDING_SEC of it, whose timing is the track's ("timing":
"exact"). (c) is never asked without a length, since a name alone can be
anybody's song. When no version is that length but LRCLIB has the song by
artist (or show) and title, its best version is kept all the same, since a
database's words beat a transcription's: "timing": "unverified", and once the
vocals stem exists lyrics_align.py moves its stamps onto the track and marks
it "shifted" when the stem shows clearly by how much.

Every version the searches found is kept beside the answer as ``others``, so
the tab can offer them without asking LRCLIB again. When nothing was kept at
all, they go in LYRICS_CANDIDATES_FILE instead, with when LRCLIB was asked:
the tab offers them to pick from, a transcription (transcribe.py) can be made,
and the tag backfill does not ask LRCLIB again for LYRICS_NOT_FOUND_RETRY_SEC.

What is sent is the artist, song, album or show names and the track's length,
nothing else. Everything here is best-effort: no connection, no match or a
slow answer leave the job without lyrics.json, and the tab then looks for them
itself as it always has.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path
from typing import Any

from app.core.config import (
    LRCLIB_API,
    LRCLIB_USER_AGENT,
    LYRICS_CANDIDATES_FILE,
    LYRICS_FILE,
    LYRICS_LOOKUP_BUDGET_SEC,
    LYRICS_LOOKUP_MAX_BYTES,
    LYRICS_NOT_FOUND_RETRY_SEC,
    LYRICS_OTHERS_MAX,
    LYRICS_SAME_RECORDING_SEC,
    TIMEOUT_LYRICS_LOOKUP,
    TITLE_MIN_COVERAGE,
)
from app.core.models import Job, _set, clean_identity
from app.pipeline.artist_lookup import _ssl_context
from app.pipeline.lyrics_align import align_lyrics
from app.pipeline.title_parse import TitleReading, coverage, resolvable, song_key, work_hint

logger = logging.getLogger("stemdeck.lyrics")

LYRICS_SOURCES = frozenset(("lrclib", "file", "whisper"))
# "exact": the track's own timing (a version its length, the file's own, a
# transcription); "unverified": a version of another length, as LRCLIB has it;
# "shifted": that, moved onto the track by lyrics_align.py.
LYRICS_TIMINGS = frozenset(("exact", "shifted", "unverified"))

# How many album names are tried as the artist before giving up on (b).
_ALBUM_ARTIST_TRIES = 2
# A name, album or title longer than this is not one.
_NAME_MAX_CHARS = 300
# Lyrics longer than this are not lyrics. A whole LRCLIB answer is capped by
# LYRICS_LOOKUP_MAX_BYTES; this caps each text kept from it.
_TEXT_MAX_CHARS = 100_000

FetchJson = Callable[[str, dict[str, str]], Any]
# What the job was identified as, when that is still being found beside the
# lookup: IdentifyLookup.wait_identity with a timeout.
IdentitySource = Callable[[], "dict[str, Any] | None"]


# ── ranking: rankMatches in static/js/lyricsLookup.js ──


def _number(value: Any) -> float:
    """A JSON number as a finite float, or 0 (JS ``Number(x) || 0``)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip() or 0)
        except ValueError:
            return 0.0
    else:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def normalise(row: Any) -> dict[str, Any]:
    """One LRCLIB row as a version of lyrics.json, without ``others``."""
    row = row if isinstance(row, dict) else {}
    lrclib_id = int(_number(row.get("id")))
    return {
        "v": 1,
        "source": "lrclib",
        "track": _text(row.get("trackName"), _NAME_MAX_CHARS),
        "artist": _text(row.get("artistName"), _NAME_MAX_CHARS),
        "album": _text(row.get("albumName"), _NAME_MAX_CHARS),
        "duration": _number(row.get("duration")),
        "synced": _text(row.get("syncedLyrics"), _TEXT_MAX_CHARS),
        "plain": _text(row.get("plainLyrics"), _TEXT_MAX_CHARS),
        "instrumental": bool(row.get("instrumental")),
        "lrclib_id": lrclib_id if lrclib_id > 0 else None,
    }


def rank_matches(rows: Any, duration: float = 0) -> list[dict[str, Any]]:
    """Best first, for a track ``duration`` seconds long (0 when unknown):
    closest in length, then synced over plain among versions within
    LYRICS_SAME_RECORDING_SEC of each other, then whatever LRCLIB ranked
    first. Rows with no id, and with neither kind of lyrics and no
    instrumental flag, are dropped."""
    return _rank(
        [
            m
            for m in map(normalise, rows if isinstance(rows, list) else [])
            if m["lrclib_id"] and (m["synced"] or m["plain"] or m["instrumental"])
        ],
        duration,
    )


def _rank(matches: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    """rank_matches' order, over versions already normalised."""

    def off(m: dict[str, Any]) -> float:
        return abs(m["duration"] - duration) if duration and m["duration"] else 0.0

    def compare(a: tuple[int, dict[str, Any]], b: tuple[int, dict[str, Any]]) -> float:
        da, db = off(a[1]), off(b[1])
        if abs(da - db) > LYRICS_SAME_RECORDING_SEC:
            return da - db
        if bool(a[1]["synced"]) != bool(b[1]["synced"]):
            return -1 if a[1]["synced"] else 1
        return (da - db) or (a[0] - b[0])

    return [m for _, m in sorted(enumerate(matches), key=cmp_to_key(compare))]


def same_recording(match: dict[str, Any], duration: float) -> bool:
    """Whether a version is the length of a track ``duration`` seconds long."""
    return (
        duration > 0
        and match["duration"] > 0
        and abs(match["duration"] - duration) <= LYRICS_SAME_RECORDING_SEC
    )


# ── the song's name: songFromTitle in static/js/lyricsLookup.js ──

# Unicode's dash punctuation, which titles use between artist and song. Built
# from the category rather than typed out, as \p{Pd} is in the page.
_DASHES = "".join(chr(c) for c in range(0x11000) if unicodedata.category(chr(c)) == "Pd")
_DASH = f"[{re.escape(_DASHES)}:|]"
# A letter: \p{L} in the page's expressions.
_LETTER = r"[^\W\d_]"
_TITLE_NOISE = re.compile(
    r"[(\[][^)\]]*(?<!" + _LETTER + r")"
    r"(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|remaster(?:ed)?|m/?v|explicit"
    r"|clean|full album)"
    r"(?!" + _LETTER + r")[^)\]]*[)\]]",
    re.IGNORECASE,
)
_FEATURING = re.compile(
    r"\s+(?:ft\.?|feat\.?|featuring)\s[^" + re.escape(_DASHES) + r"(\[]*", re.IGNORECASE
)


def _edge(ch: str) -> bool:
    return ch.isspace() or ch in _DASHES or ch in ":|"


def song_from_title(title: Any, artist: Any = "") -> str:
    """The song's name from a track title, for searching lyrics: the artist
    taken off either end, bracketed video noise and featured artists dropped,
    quotes removed. ``artist`` may be empty, and then only the noise goes."""
    song = _TITLE_NOISE.sub(" ", str(title or ""))
    # The featured artist, up to the next dash or bracket, before the artist
    # is looked for, which it would otherwise stand between.
    song = _FEATURING.sub(" ", song)
    name = str(artist or "").strip()
    if name:
        literal = re.escape(name)
        song = re.sub(rf"^\s*{literal}\s*{_DASH}\s*", "", song, count=1, flags=re.IGNORECASE)
        song = re.sub(rf"\s*{_DASH}\s*{literal}\s*$", "", song, count=1, flags=re.IGNORECASE)
    song = "".join(ch for ch in song if ch != '"' and unicodedata.category(ch) not in ("Pi", "Pf"))
    song = re.sub(r"\s+", " ", song)
    start, end = 0, len(song)
    while start < end and _edge(song[start]):
        start += 1
    while end > start and _edge(song[end - 1]):
        end -= 1
    return song[start:end].strip()


_LRC_STAMP = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]", re.MULTILINE)


def has_synced_lines(text: str) -> bool:
    """Whether ``text`` is LRC, with at least one timed line (parseLrc)."""
    return bool(_LRC_STAMP.search(text or ""))


# ── what to look up ──


@dataclass(frozen=True)
class LyricsQuery:
    artist: str
    track: str
    album: str
    duration: float
    # Lyrics the file itself carried, which win over any lookup.
    embedded: str = ""
    # Other names the recording may be filed under on LRCLIB: the show a cast
    # or soundtrack recording belongs to.
    album_artists: tuple[str, ...] = ()


def _name(value: Any) -> str:
    return value.strip()[:_NAME_MAX_CHARS] if isinstance(value, str) else ""


def _album_artists(album: str, work: Any, artist: str, title_work: str = "") -> tuple[str, ...]:
    """Names to try as the artist when the artist finds nothing: the work the
    recording belongs to, when known, or the show the track's title names
    ('Dancing Through Life (From "Wicked" ...)'), and the album, less what an
    album title adds to the show's name ("Wicked: The Soundtrack", "Wicked
    (Original Broadway Cast Recording)", "Wicked - Original Broadway Cast")."""
    names: list[str] = []
    if isinstance(work, dict):
        names += [_name(work.get("englishName")), _name(work.get("name"))]
    names.append(_name(title_work))
    if album:
        short = re.sub(r"\s*[(\[].*?[)\]]\s*", " ", album)
        short = re.split(rf"\s*:\s*|\s+{_DASH}\s+", short, maxsplit=1)[0].strip()
        names += [short, album]
    seen = {artist.casefold()} if artist else set()
    out = []
    for name in names:
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    return tuple(out[:_ALBUM_ARTIST_TRIES])


def build_query(
    job: Job,
    *,
    audio_tags: dict[str, str] | None = None,
    band: dict[str, str] | None = None,
    identity: dict[str, Any] | None = None,
) -> LyricsQuery | None:
    """What the job is known to be, best source first: its identity, its
    tags, or the band saved on it with the song's name from the title.
    ``audio_tags``, ``band`` and ``identity`` stand in for the job's own when
    given (they can be known before they are on the job). None when nothing
    is known well enough to look up and the file carried no lyrics."""
    tags = audio_tags if audio_tags is not None else (job.audio_tags or {})
    band = band or job.artist or {}
    identity = identity or getattr(job, "identity", None)
    identity = identity if isinstance(identity, dict) else {}
    band_name = _name(band.get("englishName")) or _name(band.get("name"))
    duration = _number(job.duration_sec) or _number(identity.get("duration"))
    embedded = tags.get("lyrics") if isinstance(tags.get("lyrics"), str) else ""

    if _name(identity.get("title")):
        artist = _name(identity.get("artist")) or _name(tags.get("artist")) or band_name
        track = _name(identity["title"])
        album = _name(identity.get("album")) or _name(tags.get("album"))
    elif _name(tags.get("title")):
        artist = _name(tags.get("artist")) or band_name
        track = song_from_title(tags["title"], artist) or _name(tags["title"])
        album = _name(tags.get("album"))
    else:
        artist = band_name
        track = song_from_title(job.title, artist) if artist else ""
        album = ""

    if not track and not embedded:
        return None
    hint = work_hint(job.title, track) if track else None
    return LyricsQuery(
        artist=artist,
        track=track or _name(job.title),
        album=album,
        duration=duration,
        embedded=embedded,
        album_artists=_album_artists(
            album, getattr(job, "work", None), artist, hint.work if hint else ""
        ),
    )


# ── the lookup ──


def _fetch_json(endpoint: str, params: dict[str, str]) -> Any:
    """One LRCLIB request to /api/<endpoint>. None for a 404, which is
    LRCLIB's "no such track"; raises on any other failure."""
    url = f"{LRCLIB_API}/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": LRCLIB_USER_AGENT, "Accept": "application/json"}
    )
    try:
        # A fixed https URL with only its query built here, never a URL from
        # a request or a tag, which is what B310 exists to catch.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=TIMEOUT_LYRICS_LOOKUP, context=_ssl_context()
        ) as response:
            body = response.read(LYRICS_LOOKUP_MAX_BYTES + 1)
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    if len(body) > LYRICS_LOOKUP_MAX_BYTES:
        raise ValueError("LRCLIB answer too large")
    return json.loads(body)


def identify_on_lrclib(
    reading: TitleReading,
    duration: float | None,
    *,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """An identity (source "lrclib") for a track MusicBrainz could not place,
    from one reading of its title, or None.

    One search for the reading's words. A version counts only when its track
    name is the song's, its length is within LYRICS_SAME_RECORDING_SEC of the
    track's, and its artist and album contain at least TITLE_MIN_COVERAGE of
    the reading's words beyond the song; the most words covered, then the
    nearest length, then synced lyrics, wins. Its artist and album then name
    the track, for the band, the work and the lyrics to be looked up by.
    Raises when LRCLIB cannot be reached."""
    want = song_key(reading.song)
    if not want or not resolvable(reading) or not duration or duration <= 0 or cancelled():
        return None
    fetch_json = fetch_json or _fetch_json
    terms = " ".join(dict.fromkeys(t for t in (reading.song, reading.artist, reading.work) if t))
    kept = []
    for match in rank_matches(fetch_json("search", {"q": terms[:_NAME_MAX_CHARS]}), duration):
        if not same_recording(match, duration) or song_key(match["track"]) != want:
            continue
        covered = round(coverage(reading, match["artist"], match["album"]), 3)
        if covered < TITLE_MIN_COVERAGE:
            continue
        rank = (-covered, abs(match["duration"] - duration), not match["synced"])
        kept.append((rank, covered, match))
    if not kept:
        return None
    _, covered, match = min(kept, key=lambda item: item[0])
    return clean_identity(
        {
            "source": "lrclib",
            "score": covered,
            "title": match["track"],
            "artist": match["artist"],
            "album": match["album"] or None,
            "duration": match["duration"],
        }
    )


class _Stop(Exception):
    """Cancelled, or out of time: ask nothing more."""


@dataclass(frozen=True)
class LookupAnswer:
    """What a lookup that ran to its end found: ``lyrics`` to keep as
    lyrics.json, or None when no version qualified, and then ``others``, the
    versions LRCLIB has (possibly none), to offer instead."""

    lyrics: dict[str, Any] | None
    others: list[dict[str, Any]]


def _from_file(query: LyricsQuery, fallback_title: str) -> dict[str, Any]:
    synced = query.embedded if has_synced_lines(query.embedded) else ""
    return {
        "v": 1,
        "source": "file",
        "track": query.track or fallback_title,
        "artist": query.artist,
        "album": query.album,
        "duration": query.duration,
        "synced": synced,
        "plain": "" if synced else query.embedded,
        "instrumental": False,
        "timing": "exact",
        "others": [],
        "lrclib_id": None,
    }


def lookup_lyrics(
    query: LyricsQuery,
    *,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    budget: float | None = None,
    fallback_title: str = "",
) -> LookupAnswer | None:
    """What LRCLIB has for ``query``. None when nothing could be asked, or
    the lookup was cancelled or ran out of time before it kept anything, so
    that it is asked again later. Raises when LRCLIB cannot be reached or
    answers nonsense.

    No request is started once ``cancelled()`` or ``budget`` seconds have
    passed; running out of time keeps what was found."""
    if query.embedded:
        return LookupAnswer(_from_file(query, fallback_title), [])
    if not query.track:
        return None
    # Resolved here rather than as the default, so a test can stand in for
    # the network by replacing _fetch_json.
    fetch_json = fetch_json or _fetch_json
    deadline = time.monotonic() + (LYRICS_LOOKUP_BUDGET_SEC if budget is None else budget)
    duration = query.duration
    found: list[dict[str, Any]] = []
    # The song by artist (or show) and title, whatever its length.
    song: list[dict[str, Any]] = []
    chosen: dict[str, Any] | None = None

    def ask(endpoint: str, params: dict[str, str]) -> Any:
        if cancelled() or time.monotonic() > deadline:
            raise _Stop
        return fetch_json(endpoint, params)

    def search(params: dict[str, str]) -> list[dict[str, Any]]:
        rows = rank_matches(ask("search", params), duration)
        found.extend(rows)
        return rows

    def held(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The versions the length of this track, or all when it is unknown."""
        return [m for m in rows if same_recording(m, duration)] if duration else rows

    try:
        if query.artist:
            # (a) The exact match. A failure here is not the end: the search
            # below asks a different question of a different index.
            params = {"artist_name": query.artist, "track_name": query.track}
            if query.album:
                params["album_name"] = query.album
            if duration:
                params["duration"] = str(round(duration))
            try:
                exact = rank_matches([ask("get", params)], duration)
            except urllib.error.HTTPError:
                logger.info("LRCLIB exact match failed", exc_info=True)
                exact = []
            song.extend(exact)
            exact = held(exact)
            chosen = exact[0] if exact else None
            # (b) The artist's versions of the song, which are also the
            # versions offered beside an exact match.
            rows = search({"artist_name": query.artist, "track_name": query.track})
            song.extend(rows)
            rows = held(rows)
            chosen = chosen or (rows[0] if rows else None)
        if chosen is None and not song:
            # (b) again, under the show's name.
            for name in query.album_artists:
                rows = search({"artist_name": name, "track_name": query.track})
                song.extend(rows)
                rows = held(rows)
                if rows:
                    chosen = rows[0]
                    break
                if song:
                    break
        if chosen is None and duration:
            # (c) The name alone.
            rows = held(search({"q": query.track}))
            chosen = rows[0] if rows else None
    except _Stop:
        if cancelled() or (chosen is None and not song):
            return None
        logger.info("lyrics lookup ran out of time; keeping what was found")

    timing = "exact"
    if chosen is None and song:
        # No version the track's length, but the song itself: its words, with
        # timing to be moved onto the track once there is a vocals stem. A
        # synced version, when there is one, since only that can be moved.
        ranked = _rank(song, duration)
        chosen = next((m for m in ranked if m["synced"]), ranked[0])
        timing = "unverified" if chosen["synced"] else "exact"

    others: list[dict[str, Any]] = []
    seen = {chosen["lrclib_id"]} if chosen else set()
    for match in _rank(found, duration):
        if match["lrclib_id"] not in seen:
            seen.add(match["lrclib_id"])
            others.append(match)
    others = others[:LYRICS_OTHERS_MAX]
    if chosen is None:
        return LookupAnswer(None, others)
    return LookupAnswer({**chosen, "timing": timing, "others": others}, [])


def find_lyrics(
    query: LyricsQuery | None,
    *,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    budget: float | None = None,
    fallback_title: str = "",
) -> LookupAnswer | None:
    """lookup_lyrics that never raises: any failure is logged and gives None."""
    if query is None:
        return None
    try:
        return lookup_lyrics(
            query,
            fetch_json=fetch_json,
            cancelled=cancelled,
            budget=budget,
            fallback_title=fallback_title,
        )
    except Exception:
        # Offline, a timeout, LRCLIB down or answering something else. The
        # tab looks for the lyrics itself when it is opened.
        logger.info("lyrics lookup failed", exc_info=True)
        return None


# ── lyrics.json ──


# A short BCP-47 style language code: "en", "yue", "pt-BR".
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


def _clean_version(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("source") not in LYRICS_SOURCES:
        return None
    lrclib_id = value.get("lrclib_id")
    if not (lrclib_id is None or (type(lrclib_id) is int and lrclib_id > 0)):
        return None
    duration = value.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    if not math.isfinite(duration) or duration < 0:
        return None
    version = {
        "v": 1,
        "source": value["source"],
        "track": _text(value.get("track"), _NAME_MAX_CHARS),
        "artist": _text(value.get("artist"), _NAME_MAX_CHARS),
        "album": _text(value.get("album"), _NAME_MAX_CHARS),
        "duration": float(duration),
        "synced": _text(value.get("synced"), _TEXT_MAX_CHARS),
        "plain": _text(value.get("plain"), _TEXT_MAX_CHARS),
        "instrumental": value.get("instrumental") is True,
        "lrclib_id": lrclib_id,
    }
    if not (version["synced"] or version["plain"] or version["instrumental"]):
        return None
    # The language a transcription detected (transcribe.py), kept only when
    # it looks like a language code; absent for every other source.
    language = value.get("language")
    if isinstance(language, str) and _LANGUAGE_RE.match(language):
        version["language"] = language
    return version


def clean_lyrics(value: Any) -> dict[str, Any] | None:
    """lyrics.json as read back from disk, or None when it is not one. A
    damaged or hand-edited file must not put anything malformed in front of
    the page."""
    if not isinstance(value, dict) or value.get("v") != 1:
        return None
    entry = _clean_version(value)
    if entry is None:
        return None
    # Missing on lyrics written before timing was recorded, and on anything
    # that has no reason to doubt its own (a transcription).
    timing = value.get("timing", "exact")
    if timing not in LYRICS_TIMINGS:
        return None
    entry["timing"] = timing
    others = value.get("others")
    cleaned = [_clean_version(o) for o in (others if isinstance(others, list) else [])]
    entry["others"] = [o for o in cleaned if o is not None][:LYRICS_OTHERS_MAX]
    return entry


def lyrics_path(job_dir: Path) -> Path:
    return job_dir / LYRICS_FILE


def candidates_path(job_dir: Path) -> Path:
    return job_dir / LYRICS_CANDIDATES_FILE


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` whole or not at all. Raises OSError."""
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_lyrics(job: Job, job_dir: Path, entry: dict[str, Any]) -> bool:
    """Keep ``entry`` as the job's lyrics.json, whole or not at all, and mark
    the job as having lyrics. False, logged, when it could not be written.

    LRCLIB lyrics carry their own others, so they replace the candidates
    file. A transcription leaves it: its versions are still worth offering."""
    entry = clean_lyrics(entry)
    if entry is None:
        return False
    try:
        _write_json_atomic(lyrics_path(job_dir), entry)
        if entry["source"] == "lrclib":
            candidates_path(job_dir).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not write lyrics for job %s", job.id, exc_info=True)
        return False
    _set(job, has_lyrics=True)
    return True


def write_candidates(job: Job, job_dir: Path, others: list[dict[str, Any]]) -> bool:
    """Record that LRCLIB was asked and nothing was kept: when, and the
    versions it had, to offer. False, logged, when it could not be written."""
    cleaned = [v for v in (_clean_version(o) for o in others) if v is not None]
    data = {"v": 1, "searched_at": time.time(), "others": cleaned[:LYRICS_OTHERS_MAX]}
    try:
        _write_json_atomic(candidates_path(job_dir), data)
    except OSError:
        logger.warning("could not write lyrics candidates for job %s", job.id, exc_info=True)
        return False
    return True


def read_candidates(job_dir: Path) -> dict[str, Any] | None:
    """The candidates file, cleaned: {"searched_at", "others"}, or None."""
    try:
        data = json.loads(candidates_path(job_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    searched_at = data.get("searched_at")
    if isinstance(searched_at, bool) or not isinstance(searched_at, (int, float)):
        return None
    if not math.isfinite(searched_at):
        return None
    others = data.get("others")
    cleaned = [_clean_version(o) for o in (others if isinstance(others, list) else [])]
    return {
        "searched_at": float(searched_at),
        "others": [o for o in cleaned if o is not None][:LYRICS_OTHERS_MAX],
    }


def lyrics_settled(job_dir: Path, now: float | None = None) -> bool:
    """Whether there is nothing to ask LRCLIB for this job: it has lyrics, or
    LRCLIB was asked less than LYRICS_NOT_FOUND_RETRY_SEC ago and had none."""
    if lyrics_path(job_dir).is_file():
        return True
    found = read_candidates(job_dir)
    now = time.time() if now is None else now
    return found is not None and now - found["searched_at"] < LYRICS_NOT_FOUND_RETRY_SEC


def keep_answer(job: Job, job_dir: Path, answer: LookupAnswer) -> bool:
    """Keep a lookup's answer: the lyrics, or the candidates and when they
    were asked for. True when lyrics were written (has_lyrics changed).

    Lyrics of unverified timing are then moved onto the track, which needs
    the vocals stem: both callers (LyricsLookup.finish and the tag backfill)
    run once separation is done."""
    if answer.lyrics is None:
        write_candidates(job, job_dir, answer.others)
        return False
    if not write_lyrics(job, job_dir, answer.lyrics):
        return False
    align_lyrics(job, job_dir)
    return True


def copy_lyrics(src_dir: Path, dest_dir: Path) -> bool:
    """Copy a job's lyrics.json and candidates file to another job of the
    same recording (a re-split). True when lyrics.json was copied."""
    copied = False
    for path in (lyrics_path(src_dir), candidates_path(src_dir)):
        if not path.is_file():
            continue
        try:
            shutil.copy2(path, dest_dir / path.name)
        except OSError:
            logger.warning("could not copy %s to %s", path.name, dest_dir.name, exc_info=True)
            continue
        copied = copied or path.name == LYRICS_FILE
    return copied


def read_lyrics(job_dir: Path) -> dict[str, Any] | None:
    """The job's lyrics.json, cleaned, or None when it has none."""
    try:
        return clean_lyrics(json.loads(lyrics_path(job_dir).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


class LyricsLookup:
    """One job's lyrics lookup, in a thread of its own, so separation never
    waits for it. The same contract as BandLookup: the thread never touches
    the job or its directory, and finish() writes the answer from the
    pipeline's own thread, once the pipeline has got that far uncancelled.
    """

    def __init__(self, job: Job, identity: IdentitySource | None) -> None:
        self._result: LookupAnswer | None = None
        self._thread = threading.Thread(
            target=self._run, args=(job, identity), name=f"lyrics-{job.id}", daemon=True
        )

    def _run(self, job: Job, identity: IdentitySource | None) -> None:
        try:
            # The identity is still being found beside this (identify.py), and
            # is only put on the job once the pipeline is done: it is asked of
            # the identification itself.
            query = build_query(job, identity=identity() if identity else None)
        except Exception:
            logger.info("[%s] lyrics query failed", job.id, exc_info=True)
            return
        # find_lyrics never raises.
        self._result = find_lyrics(
            query, cancelled=lambda: job.cancel_requested, fallback_title=job.title or ""
        )

    @classmethod
    def start(
        cls, job: Job, job_dir: Path, *, identity: IdentitySource | None = None
    ) -> LyricsLookup | None:
        """Start looking, or None when there is nothing to look for, or the
        job already has lyrics or had none on LRCLIB lately (a re-split has
        its source's). ``identity``, when given, is called in the thread for
        what the job was identified as, and may wait for it."""
        if lyrics_settled(job_dir):
            return None
        if identity is None and build_query(job) is None:
            return None
        lookup = cls(job, identity)
        lookup._thread.start()
        return lookup

    def finish(self, job: Job, job_dir: Path, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the answer, and keep it
        (keep_answer) if there is one and the job was not cancelled meanwhile."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.info(
                "[%s] lyrics lookup still out after %ss; finishing without", job.id, timeout
            )
            return
        if job.cancel_requested or self._result is None:
            return
        keep_answer(job, job_dir, self._result)
