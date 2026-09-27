"""MusicBrainz, asked by everything in StemDeck that needs it.

Five questions, all read-only:

* a recording by its MBID (what AcoustID matched a fingerprint to): its
  credited title and artists, and the release groups it came out on;
* a recording search by artist, title and length, for a track with no
  fingerprint match, kept only when it is unmistakable;
* a recording search by one reading of a track's title (title_parse.py), for
  a track its tags do not name, kept only when the recording's credit and
  release titles account for the title's other words;
* an artist's Wikidata item, from its url relationships, which is how the band
  a recording credits is found without searching for its name;
* a release group's url relationships (Wikidata, IMDb), which is how the film
  or musical a soundtrack is from is found (work_lookup.py).

Every request takes its turn from ratelimit.MUSICBRAINZ (one a second across
the whole process, as MusicBrainz asks) and names StemDeck in its User-Agent.
Lookups by MBID are kept on disk, one file per MBID, so the same recording or
artist is never asked for twice within MUSICBRAINZ_CACHE_TTL_SEC.

What is sent: an MBID, or names from the track: artist, song, album or show,
and its length. Nothing here
raises past its caller's except-clause: callers treat any failure as "not
found".
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.config import (
    IDENTIFY_DURATION_TOLERANCE_SEC,
    IDENTIFY_MAX_BYTES,
    MUSICBRAINZ_API,
    MUSICBRAINZ_CACHE_DIR,
    MUSICBRAINZ_CACHE_TTL_SEC,
    MUSICBRAINZ_SEARCH_MIN_SCORE,
    MUSICBRAINZ_USER_AGENT,
    TIMEOUT_IDENTIFY_REQUEST,
    TITLE_MIN_COVERAGE,
)
from app.core.models import MBID_RE, clean_identity
from app.pipeline import ratelimit, title_parse
from app.pipeline.artist_lookup import _ssl_context, artist_name_key
from app.pipeline.title_parse import TitleReading

logger = logging.getLogger("stemdeck.identify")

# Replaced by tests with a temporary directory.
CACHE_DIR: Path = MUSICBRAINZ_CACHE_DIR

_SEARCH_LIMIT = "25"
# Words of a title put in one search, at most: a performer list can be long.
_TITLE_TERMS = 12
_WIKIDATA_URL_RE = re.compile(r"^https?://(?:www\.)?wikidata\.org/wiki/(Q\d{1,12})$")
# What a release group is, best first, for choosing the album a recording is
# "from": the studio album over the single, and either over a compilation.
_PRIMARY_RANK = {"album": 0, "ep": 1, "single": 2}
_BRACKETS_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


def _fetch_json(path: str, params: dict[str, str]) -> Any:
    """One MusicBrainz request, after waiting for its turn. Raises on any
    failure, including a turn too far off (ratelimit.RateLimited).

    MusicBrainz answers 503 when it sheds load, even to a client keeping to
    its rate, so a 503 is asked once more, on the next turn."""
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    url = f"{MUSICBRAINZ_API}/{path}?{query}"
    request = urllib.request.Request(
        url, headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"}
    )
    for attempt in range(2):
        ratelimit.MUSICBRAINZ.wait()
        try:
            # A fixed https base with only a path of our own and a query built
            # here, never a URL from a request or a tag, which is what B310
            # exists to catch.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=TIMEOUT_IDENTIFY_REQUEST, context=_ssl_context()
            ) as response:
                body = response.read(IDENTIFY_MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as err:
            if err.code != 503 or attempt:
                raise
    if len(body) > IDENTIFY_MAX_BYTES:
        raise ValueError("MusicBrainz answer too large")
    return json.loads(body)


# ── the cache ──


def _cache_path(kind: str, mbid: str) -> Path | None:
    # Both halves are ours, and the MBID is checked, so the name can never
    # leave the cache directory.
    if kind not in ("recording", "artist", "release-group") or not MBID_RE.match(mbid):
        return None
    return CACHE_DIR / f"{kind}-{mbid}.json"


def cache_get(kind: str, mbid: str) -> Any:
    """What was kept for ``mbid``, or None when nothing fresh is."""
    path = _cache_path(kind, mbid)
    if path is None:
        return None
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, dict) or not isinstance(kept.get("at"), (int, float)):
        return None
    if time.time() - kept["at"] > MUSICBRAINZ_CACHE_TTL_SEC:
        return None
    return kept.get("data")


def cache_put(kind: str, mbid: str, data: Any) -> None:
    """Keep ``data`` for ``mbid``. Best-effort: a cache that cannot be
    written only means asking again next time."""
    path = _cache_path(kind, mbid)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps({"at": time.time(), "data": data}), encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        logger.info("could not cache MusicBrainz %s %s", kind, mbid, exc_info=True)


def _cached(kind: str, mbid: str, path: str, params: dict[str, str]) -> Any:
    kept = cache_get(kind, mbid)
    if kept is not None:
        return kept
    data = _fetch_json(path, params)
    if isinstance(data, dict):
        cache_put(kind, mbid, data)
    return data


# ── reading an answer ──


def credited_artist(credit: Any) -> str:
    """The artist credit as MusicBrainz prints it: each name and its join
    phrase ("Queen & David Bowie")."""
    if not isinstance(credit, list):
        return ""
    parts = []
    for entry in credit:
        if not isinstance(entry, dict):
            continue
        artist = entry.get("artist") if isinstance(entry.get("artist"), dict) else {}
        name = entry.get("name") or artist.get("name") or ""
        joinphrase = entry.get("joinphrase") or ""
        if isinstance(name, str) and isinstance(joinphrase, str):
            parts.append(name + joinphrase)
    return "".join(parts).strip()


def credited_artist_ids(credit: Any) -> list[str]:
    """The MBIDs of every artist in a credit, in credit order."""
    if not isinstance(credit, list):
        return []
    ids = []
    for entry in credit:
        artist = entry.get("artist") if isinstance(entry, dict) else None
        mbid = artist.get("id") if isinstance(artist, dict) else None
        if isinstance(mbid, str) and MBID_RE.match(mbid) and mbid not in ids:
            ids.append(mbid)
    return ids


def _secondary_types(group: Any) -> set[str]:
    types = group.get("secondary-types") if isinstance(group, dict) else None
    return {t.lower() for t in types if isinstance(t, str)} if isinstance(types, list) else set()


def _release_group_rank(release: dict[str, Any], prefer: frozenset[str] = frozenset()) -> tuple:
    group = release.get("release-group") if isinstance(release.get("release-group"), dict) else {}
    types = _secondary_types(group)
    primary = str(group.get("primary-type") or "").lower()
    date = release.get("date") or group.get("first-release-date") or ""
    return (
        # Named by the title the track came with ("Wicked", "The Greatest
        # Showman"): the cast album, not a compilation it is also on.
        bool(prefer)
        and not prefer <= title_parse.words(group.get("title") or release.get("title")),
        bool(types),
        "live" in types,
        "compilation" in types,
        _PRIMARY_RANK.get(primary, 3 if primary else 4),
        str(release.get("status") or "").lower() != "official",
        str(date) if date else "9999",
    )


def best_release_group(
    releases: Any, prefer: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """The release group a recording is best known from: a studio album over a
    single or EP, any of them over a soundtrack, a soundtrack over a
    compilation, anything over a live album, an official release over a
    bootleg, the earliest over a reissue. A recording that only ever came out
    on a soundtrack keeps the soundtrack, which is what tells the work lookup
    to look for the film or show. ``prefer``, the words of a work the track's
    title names, puts a release group whose title has them all first."""
    if not isinstance(releases, list):
        return None
    candidates = [
        r for r in releases if isinstance(r, dict) and isinstance(r.get("release-group"), dict)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda r: _release_group_rank(r, prefer))["release-group"]


_YEAR_RE = re.compile(r"^(\d{4})")


def _year(date: Any) -> int | None:
    match = _YEAR_RE.match(date) if isinstance(date, str) else None
    return int(match.group(1)) if match else None


def release_group_year(releases: Any, group: dict[str, Any]) -> int | None:
    """The year a release group first came out: its first-release-date, as a
    recording lookup carries it, else the earliest date among the recording's
    releases in that group, since a search answer leaves the group's date
    out. None when neither says."""
    year = _year(group.get("first-release-date"))
    group_id = group.get("id")
    if year or not group_id or not isinstance(releases, list):
        return year
    years = [
        _year(release.get("date"))
        for release in releases
        if isinstance(release, dict)
        and isinstance(release.get("release-group"), dict)
        and release["release-group"].get("id") == group_id
    ]
    return min((y for y in years if y), default=None)


def identity_from_recording(
    recording: Any,
    *,
    source: str,
    score: float,
    fallback_duration: float | None = None,
    prefer: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """An identity (clean_identity's shape) from a MusicBrainz recording, as a
    lookup or a search answers it, or None when it names nothing. ``prefer``
    as for best_release_group."""
    if not isinstance(recording, dict):
        return None
    credit = recording.get("artist-credit")
    group = best_release_group(recording.get("releases"), prefer) or {}
    length = recording.get("length")
    duration = (
        length / 1000
        if isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0
        else fallback_duration
    )
    secondary = group.get("secondary-types")
    return clean_identity(
        {
            "source": source,
            "score": score,
            "recording_mbid": recording.get("id"),
            "title": recording.get("title"),
            "artist": credited_artist(credit),
            "artist_mbids": credited_artist_ids(credit),
            "album": group.get("title"),
            "release_group_mbid": group.get("id"),
            "release_group_type": group.get("primary-type"),
            "secondary_types": secondary if isinstance(secondary, list) else [],
            "year": release_group_year(recording.get("releases"), group),
            "duration": duration,
        }
    )


# ── the questions ──


def lookup_recording(mbid: str) -> dict[str, Any] | None:
    """A recording by MBID, with its artist credit and its releases' release
    groups, from the cache when it is there. Raises when MusicBrainz cannot
    be reached."""
    if not MBID_RE.match(mbid or ""):
        return None
    data = _cached(
        "recording",
        mbid,
        f"recording/{mbid}",
        {"inc": "artist-credits+releases+release-groups"},
    )
    return data if isinstance(data, dict) else None


def artist_wikidata_id(mbid: str) -> str | None:
    """The Wikidata item a MusicBrainz artist links to, or None. Raises when
    MusicBrainz cannot be reached."""
    if not MBID_RE.match(mbid or ""):
        return None
    data = _cached("artist", mbid, f"artist/{mbid}", {"inc": "url-rels"})
    relations = data.get("relations") if isinstance(data, dict) else None
    for relation in relations if isinstance(relations, list) else []:
        if not isinstance(relation, dict) or relation.get("type") != "wikidata":
            continue
        url = relation.get("url")
        resource = url.get("resource") if isinstance(url, dict) else None
        match = _WIKIDATA_URL_RE.match(resource or "") if isinstance(resource, str) else None
        if match:
            return match.group(1)
    return None


def lookup_release_group(mbid: str) -> dict[str, Any] | None:
    """A release group by MBID, with its url relationships (Wikidata, IMDb),
    from the cache when it is there. For the work a soundtrack is from
    (work_lookup.py). Raises when MusicBrainz cannot be reached."""
    if not MBID_RE.match(mbid or ""):
        return None
    data = _cached("release-group", mbid, f"release-group/{mbid}", {"inc": "url-rels"})
    return data if isinstance(data, dict) else None


def _lucene_phrase(text: str) -> str:
    """``text`` as one quoted Lucene phrase: quotes and backslashes escaped,
    so a title cannot change the shape of the query."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _title_key(title: Any) -> str:
    return artist_name_key(_BRACKETS_RE.sub("", str(title or "")))


def search_recording(
    artist: str,
    title: str,
    duration: float | None,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The recording ``artist`` - ``title`` is, found by search, or None.

    Kept only when it is unmistakable: MusicBrainz scores it at least
    MUSICBRAINZ_SEARCH_MIN_SCORE, its credited artist and its title are the
    ones asked for once case, accents and punctuation are set aside, and its
    length is within IDENTIFY_DURATION_TOLERANCE_SEC of the track's. Without a
    length to check, nothing is kept: a name alone can be anybody's song.
    Raises when MusicBrainz cannot be reached."""
    artist, title = artist.strip(), title.strip()
    if not artist or not title or not duration or duration <= 0 or cancelled():
        return None
    # The length and an official release narrow it down in the query: a
    # popular song has hundreds of recordings under its name, most of them
    # bootleg live cuts, and the one the track is would not be among the
    # first page of them otherwise.
    low = max(0, round((duration - IDENTIFY_DURATION_TOLERANCE_SEC) * 1000))
    high = round((duration + IDENTIFY_DURATION_TOLERANCE_SEC) * 1000)
    data = _fetch_json(
        "recording",
        {
            "query": (
                f"recording:{_lucene_phrase(title)} AND artist:{_lucene_phrase(artist)}"
                f" AND dur:[{low} TO {high}] AND status:official"
            ),
            "limit": _SEARCH_LIMIT,
        },
    )
    hits = data.get("recordings") if isinstance(data, dict) else None
    want_artist = artist_name_key(artist)
    want_title = _title_key(title)
    kept: list[tuple[float, float, dict[str, Any]]] = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict):
            continue
        score = hit.get("score")
        length = hit.get("length")
        if not isinstance(score, (int, float)) or score < MUSICBRAINZ_SEARCH_MIN_SCORE:
            continue
        if not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
            continue
        off = abs(length / 1000 - duration)
        if off > IDENTIFY_DURATION_TOLERANCE_SEC:
            continue
        credit = hit.get("artist-credit")
        names = {artist_name_key(credited_artist(credit))}
        if isinstance(credit, list) and credit and isinstance(credit[0], dict):
            first = credit[0]
            names.add(artist_name_key(first.get("name")))
            if isinstance(first.get("artist"), dict):
                names.add(artist_name_key(first["artist"].get("name")))
        if want_artist not in names:
            continue
        if not want_title or want_title not in (
            _title_key(hit.get("title")),
            artist_name_key(hit.get("title")),
        ):
            continue
        if hit.get("video") is True:
            continue
        kept.append((_search_rank(hit, off, float(score)), float(score), hit))
    if not kept:
        return None
    _, score, hit = min(kept, key=lambda item: item[0])
    return identity_from_recording(hit, source="musicbrainz", score=score / 100)


_LIVE_RE = re.compile(r"\blive\b", re.IGNORECASE)


def is_live(hit: dict[str, Any]) -> bool:
    """Whether a recording is a live one: its disambiguation says so ("live,
    1986"), or every release group it came out on is a live album (the best
    one is, and best_release_group ranks live albums last)."""
    if _LIVE_RE.search(str(hit.get("disambiguation") or "")):
        return True
    return "live" in _secondary_types(best_release_group(hit.get("releases")))


def _search_rank(hit: dict[str, Any], off: float, score: float) -> tuple:
    """Which of several recordings of the same song a search found is the
    track, best first. A popular song has dozens with the same name and
    artist, all scored 100: the live cuts, the karaoke and the compilations
    are recordings of their own. So a live recording last, whatever else it
    has going for it ("Popular" from the cast album, not from Kristin
    Chenoweth's live "Coming Home"); then the studio recording first (no
    disambiguation, released on something that is not a compilation, live
    album or remix), then the one released most often (the original is on
    every reissue and best-of), then the nearest length, then the score."""
    releases = hit.get("releases")
    group = best_release_group(releases) or {}
    return (
        is_live(hit),
        bool(str(hit.get("disambiguation") or "").strip()),
        bool(group.get("secondary-types")) or not group,
        -len(releases) if isinstance(releases, list) else 0,
        off,
        -score,
    )


# ── a track named only by its title ──


def _length_off(hit: dict[str, Any], duration: float) -> float | None:
    """How far a hit's length is from the track's, or None when it has none."""
    length = hit.get("length")
    if not isinstance(length, (int, float)) or isinstance(length, bool) or length <= 0:
        return None
    return abs(length / 1000 - duration)


def _release_titles(hit: dict[str, Any]) -> list[str]:
    titles = []
    for release in hit.get("releases") or []:
        if not isinstance(release, dict):
            continue
        group = release.get("release-group")
        for title in (
            release.get("title"),
            group.get("title") if isinstance(group, dict) else None,
        ):
            if isinstance(title, str) and title not in titles:
                titles.append(title)
    return titles


def best_title_match(answer: Any, reading: TitleReading, duration: float) -> dict[str, Any] | None:
    """The recording a search for ``reading`` found that the track is, as an
    identity, or None when none is confidently it.

    A hit counts only when its title is the song's, its length is within
    IDENTIFY_DURATION_TOLERANCE_SEC of the track's (the Broadway cast's 457
    seconds, not the film's 587) and it is not a video; and it is kept only
    when its credit and its release titles contain at least
    TITLE_MIN_COVERAGE of the reading's words beyond the song (the performers
    the title lists, the show it names). Of those: the most words covered,
    then not live, then the credit naming the performers (Keala Settle, not
    Kesha's cover on "The Greatest Showman: Reimagined"), then a release named
    for the show (the cast album, not a compilation), then _search_rank."""
    want_title = _title_key(reading.song)
    want = title_parse.extra_words(reading)
    artist_words = title_parse.words(reading.artist)
    work_words = title_parse.words(reading.work)
    hits = answer.get("recordings") if isinstance(answer, dict) else None
    if not want_title or not want or not duration or duration <= 0:
        return None
    kept: list[tuple[tuple, float, dict[str, Any]]] = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict) or hit.get("video") is True:
            continue
        off = _length_off(hit, duration)
        if off is None or off > IDENTIFY_DURATION_TOLERANCE_SEC:
            continue
        if want_title not in (_title_key(hit.get("title")), artist_name_key(hit.get("title"))):
            continue
        credit = credited_artist(hit.get("artist-credit"))
        releases = _release_titles(hit)
        covered = round(title_parse.coverage(reading, credit, *releases), 3)
        if covered < TITLE_MIN_COVERAGE:
            continue
        credit_words = title_parse.words(credit)
        by_credit = bool(artist_words) and (
            len(artist_words & credit_words) / len(artist_words) >= TITLE_MIN_COVERAGE
        )
        named_release = bool(work_words) and any(
            work_words <= title_parse.words(title) for title in releases
        )
        score = hit.get("score")
        score = float(score) if isinstance(score, (int, float)) else 0.0
        rank = (-covered, is_live(hit), not by_credit, not named_release)
        kept.append((rank + _search_rank(hit, off, score), covered, hit))
    if not kept:
        return None
    _, covered, hit = min(kept, key=lambda item: item[0])
    return identity_from_recording(hit, source="musicbrainz", score=covered, prefer=work_words)


def search_by_title(
    reading: TitleReading,
    duration: float | None,
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any] | None:
    """The recording one reading of a track's title is, found by search, or
    None (see best_title_match). The query asks for the song by name, the
    track's length, an official release, and any of the reading's other words
    in the credit or a release title, so the recordings that could account
    for them come first. Raises when MusicBrainz cannot be reached."""
    want = sorted(title_parse.extra_words(reading))
    if not reading.song or not want or not duration or duration <= 0 or cancelled():
        return None
    low = max(0, round((duration - IDENTIFY_DURATION_TOLERANCE_SEC) * 1000))
    high = round((duration + IDENTIFY_DURATION_TOLERANCE_SEC) * 1000)
    # Each word its own quoted phrase: no word can change the query's shape.
    terms = " ".join(_lucene_phrase(w) for w in want[:_TITLE_TERMS])
    data = _fetch_json(
        "recording",
        {
            "query": (
                f"recording:{_lucene_phrase(reading.song)}"
                f" AND dur:[{low} TO {high}] AND status:official"
                f" AND (artist:({terms}) OR release:({terms}))"
            ),
            "limit": _SEARCH_LIMIT,
        },
    )
    return best_title_match(data, reading, duration)
