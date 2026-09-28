"""A band's profile from Discogs, for bands Wikipedia has no article on.

Small and underground bands from anywhere have a Discogs page long before
they have a Wikipedia article: a profile, members, releases and their own
links. This asks Discogs for them, and only when the user has set a Discogs
personal access token in Settings (settings.get_discogs_token). Without one,
nothing here is ever called.

Finding the right artist follows the rule the rest of the lookups keep: no
weak guesses. A name alone is never enough, since Discogs has many artists
called NIHIL ("Nihil", "Nihil (2)", "Nihil (5)"), and a song's title can be
a band's name too. An artist is taken only when one of these says so:

* the MusicBrainz artist the track was identified as links to it (its url
  relationship to Discogs, from the cached MusicBrainz lookup);
* the Wikidata item of the band saved on the track names it (P1953);
* a Discogs release credits that exact artist name (case, accents and
  scripts aside, and Discogs' "(5)" set aside as disambiguation) with the
  track's song on its tracklist, or as the album the track is from. Two
  different artists doing so is no answer at all.

The token goes in the Authorization header only: never in a URL, never in a
log line, never in an answer. Every request takes its turn from
ratelimit.DISCOGS, is abandoned after TIMEOUT_DISCOGS_REQUEST, reads at most
DISCOGS_MAX_BYTES, and is kept on disk for DISCOGS_CACHE_TTL_SEC, so opening
the same band again asks Discogs nothing. Nothing here raises into its
callers: a failure is "nothing found".

What is sent: the track's artist name, its song and album titles, and
Discogs ids.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import (
    DISCOGS_API,
    DISCOGS_CACHE_DIR,
    DISCOGS_CACHE_TTL_SEC,
    DISCOGS_MAX_BYTES,
    DISCOGS_REQUEST_BUDGET_SEC,
    DISCOGS_RETRIES,
    DISCOGS_RETRY_BACKOFF_SEC,
    DISCOGS_RETRY_MAX_WAIT_SEC,
    MUSICBRAINZ_USER_AGENT,
    TIMEOUT_DISCOGS_REQUEST,
)
from app.core.models import MBID_RE
from app.pipeline import artist_lookup, name_aliases, ratelimit
from app.pipeline.artist_lookup import _QID_RE, _ssl_context, tagged_artist_name

logger = logging.getLogger("stemdeck.artist")

# Replaced by tests with a temporary directory.
CACHE_DIR: Path = DISCOGS_CACHE_DIR

Cancelled = Callable[[], bool]


def _never() -> bool:
    return False


# ── the client ──


def _send(request: urllib.request.Request) -> bytes:
    """One request, as it goes out. Raises on any failure; an HTTP error
    status raises urllib.error.HTTPError. Replaced by tests."""
    # A fixed https base with a path and query built here from ids and names,
    # never a URL from a request or a tag, which is what B310 exists to catch.
    with urllib.request.urlopen(  # nosec B310
        request, timeout=TIMEOUT_DISCOGS_REQUEST, context=_ssl_context()
    ) as response:
        return response.read(DISCOGS_MAX_BYTES + 1)


# Replaced by tests, so a retry costs no real time.
_sleep = time.sleep


def _retry_wait(err: urllib.error.HTTPError, attempt: int) -> float:
    """How long to wait before asking again: what Retry-After says when it is
    a number of seconds, else the backoff for this attempt, capped."""
    backoff = DISCOGS_RETRY_BACKOFF_SEC * 2**attempt
    try:
        asked = float((err.headers or {}).get("Retry-After") or "")
    except (TypeError, ValueError):
        asked = backoff
    return max(0.0, min(asked, DISCOGS_RETRY_MAX_WAIT_SEC))


class _Refused(Exception):
    """Discogs answered, and the answer was no (a 401, a 404)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def _fetch(path: str, params: dict[str, str], token: str, cancelled: Cancelled) -> Any:
    """One Discogs request, after waiting for its turn. Raises on any failure.
    A 429 or a 502/503 is asked again, each time on a turn of its own, up to
    DISCOGS_RETRIES times and never past DISCOGS_REQUEST_BUDGET_SEC."""
    query = urllib.parse.urlencode(params)
    url = f"{DISCOGS_API}/{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(
        url,
        headers={
            # The token lives here and nowhere else: not in the URL, which a
            # proxy or an exception could log.
            "Authorization": f"Discogs token={token}",
            "User-Agent": MUSICBRAINZ_USER_AGENT,
            "Accept": "application/vnd.discogs.v2.discogs+json",
        },
    )
    deadline = time.monotonic() + DISCOGS_REQUEST_BUDGET_SEC
    for attempt in range(DISCOGS_RETRIES + 1):
        ratelimit.DISCOGS.wait(cancelled=cancelled)
        try:
            body = _send(request)
            break
        except urllib.error.HTTPError as err:
            if err.code not in (429, 502, 503):
                raise _Refused(err.code) from None
            if attempt == DISCOGS_RETRIES:
                raise
            left = _retry_wait(err, attempt)
            if time.monotonic() + left > deadline:
                raise
            # Slept in short steps, so a cancelled lookup stops promptly.
            while left > 0:
                if cancelled():
                    raise ratelimit.RateLimited("cancelled while waiting") from None
                step = min(left, 0.25)
                _sleep(step)
                left -= step
    if len(body) > DISCOGS_MAX_BYTES:
        raise ValueError("Discogs answer too large")
    return json.loads(body)


# ── the cache ──


def _cache_path(path: str, params: dict[str, str]) -> Path:
    # A hash of our own request, so no name from a tag ever reaches a path.
    # The token is not part of the request asked for, so it is not in it.
    key = path + "?" + urllib.parse.urlencode(sorted(params.items()))
    return CACHE_DIR / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"


def _cache_get(path: str, params: dict[str, str]) -> Any:
    try:
        kept = json.loads(_cache_path(path, params).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, dict) or not isinstance(kept.get("at"), (int, float)):
        return None
    if time.time() - kept["at"] > DISCOGS_CACHE_TTL_SEC:
        return None
    return kept.get("data")


def _cache_put(path: str, params: dict[str, str], data: Any) -> None:
    """Best-effort: a cache that cannot be written only means asking again."""
    target = _cache_path(path, params)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps({"at": time.time(), "data": data}), encoding="utf-8")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        logger.info("could not cache a Discogs answer", exc_info=True)


@dataclass
class _Session:
    """One lookup's token and cancel check, and whether any request failed
    (so a "nothing found" that was really "could not ask" is not kept)."""

    token: str
    cancelled: Cancelled = _never
    failed: bool = False
    asked: list[str] = field(default_factory=list)

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        """The answer to one request, from the cache when it is there, or
        None on any failure. Never raises."""
        params = dict(params or {})
        kept = _cache_get(path, params)
        if isinstance(kept, dict):
            return kept
        if self.cancelled():
            self.failed = True
            return None
        self.asked.append(path)
        try:
            data = _fetch(path, params, self.token, self.cancelled)
        except _Refused as err:
            # A 404 is an answer: that release or artist is not there.
            if err.status != 404:
                self.failed = True
                logger.info("Discogs refused %s (HTTP %s)", path.split("/")[0], err.status)
            return None
        except Exception as err:
            self.failed = True
            # The type only: nothing about the request is worth logging more.
            logger.info("Discogs request failed (%s)", type(err).__name__)
            return None
        if not isinstance(data, dict):
            return None
        _cache_put(path, params, data)
        return data


# ── names ──


# Discogs tells artists who share a name apart with a number: "Nihil (5)".
_NUMBER_SUFFIX_RE = re.compile(r"\s*\(\d{1,4}\)\s*$")
_BRACKETS_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")


def display_name(name: Any) -> str:
    """An artist's name as Discogs gives it, less its number: "Nihil (5)" is
    Nihil. A trailing "*" (a name variation on a credit) goes too."""
    text = str(name or "").strip().rstrip("*").strip()
    return _NUMBER_SUFFIX_RE.sub("", text).strip()


def name_key(name: Any) -> str:
    """A Discogs artist name reduced for comparing, number set aside
    (name_aliases.name_key: case, accents, punctuation, Chinese scripts)."""
    return name_aliases.name_key(display_name(name))


def title_key(title: Any) -> str:
    """A song or album title reduced for comparing, brackets set aside
    ("Barro (Remastered)" is Barro)."""
    text = str(title or "")
    return name_aliases.name_key(_BRACKETS_RE.sub("", text)) or name_aliases.name_key(text)


# ── finding the artist ──


# Releases a search found that are checked, at most: each is one request.
_RELEASES_CHECKED = 4
_SEARCH_PER_PAGE = "25"
_DISCOGS_ARTIST_URL_RE = re.compile(
    r"^https?://(?:www\.)?discogs\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?artist/(\d{1,12})(?:[-/?#].*)?$",
    re.IGNORECASE,
)
_DISCOGS_ID_RE = re.compile(r"^[1-9]\d{0,11}$")
_WIKIDATA_DISCOGS_ARTIST = "P1953"


def _artist_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and _DISCOGS_ID_RE.match(value):
        return int(value)
    return None


def artist_id_from_musicbrainz(mbids: list[str], *, cancelled: Cancelled = _never) -> int | None:
    """The Discogs artist the first of ``mbids`` that links to one does, by
    its MusicBrainz url relationships, from the MusicBrainz cache when it is
    there (one request a second otherwise, as every MusicBrainz caller)."""
    for mbid in [m for m in mbids if isinstance(m, str) and MBID_RE.match(m)][:2]:
        if cancelled():
            return None
        try:
            artist = name_aliases._musicbrainz_artist(mbid, cancelled=cancelled)
        except Exception:
            logger.info("MusicBrainz artist for Discogs failed", exc_info=True)
            continue
        relations = artist.get("relations") if isinstance(artist, dict) else None
        for relation in relations if isinstance(relations, list) else []:
            if not isinstance(relation, dict) or relation.get("type") != "discogs":
                continue
            url = relation.get("url")
            resource = url.get("resource") if isinstance(url, dict) else None
            match = _DISCOGS_ARTIST_URL_RE.match(resource) if isinstance(resource, str) else None
            if match:
                return int(match.group(1))
    return None


def artist_id_from_wikidata(qid: str, *, fetch_json: Any = None) -> int | None:
    """The Discogs artist the Wikidata item ``qid`` names (P1953), when it
    names exactly one. Never raises."""
    if not isinstance(qid, str) or not _QID_RE.match(qid):
        return None
    try:
        answer = (fetch_json or artist_lookup._fetch_json)(
            {"action": "wbgetentities", "ids": qid, "props": "claims"}
        )
    except Exception:
        logger.info("Wikidata item for Discogs failed", exc_info=True)
        return None
    entities = answer.get("entities") if isinstance(answer, dict) else None
    entity = entities.get(qid) if isinstance(entities, dict) else None
    claims = entity.get("claims") if isinstance(entity, dict) else None
    statements = claims.get(_WIKIDATA_DISCOGS_ARTIST) if isinstance(claims, dict) else None
    ids = set()
    for statement in statements if isinstance(statements, list) else []:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        snak = statement.get("mainsnak")
        datavalue = snak.get("datavalue") if isinstance(snak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        found = _artist_id(value)
        if found:
            ids.add(found)
    return ids.pop() if len(ids) == 1 else None


def _credited(artists: Any, want: str) -> set[int]:
    """The ids of the artists on a Discogs credit whose name, or the name
    variation they were credited as, is ``want`` (a name_key)."""
    found = set()
    for artist in artists if isinstance(artists, list) else []:
        if not isinstance(artist, dict):
            continue
        artist_id = _artist_id(artist.get("id"))
        if artist_id and want in {name_key(artist.get("name")), name_key(artist.get("anv"))}:
            found.add(artist_id)
    return found


def _tracks(tracklist: Any) -> list[dict[str, Any]]:
    """Every track on a tracklist, the sub-tracks of an index track too."""
    tracks = []
    for track in tracklist if isinstance(tracklist, list) else []:
        if not isinstance(track, dict):
            continue
        if track.get("type_") in (None, "", "track"):
            tracks.append(track)
        tracks.extend(_tracks(track.get("sub_tracks")))
    return tracks


def credits_on_release(
    release: Any, want_artist: str, *, song: str = "", album: str = ""
) -> set[int]:
    """The artists a release (GET /releases/{id}) credits by the name
    ``want_artist`` with the song ``song`` on its tracklist, or, with no
    song, as the album ``album``. Keys, both (name_key, title_key)."""
    if not isinstance(release, dict) or not want_artist:
        return set()
    if song:
        found: set[int] = set()
        for track in _tracks(release.get("tracklist")):
            if title_key(track.get("title")) != song:
                continue
            # A compilation credits each track's own artists.
            found |= _credited(track.get("artists") or release.get("artists"), want_artist)
        return found
    if album and title_key(release.get("title")) == album:
        return _credited(release.get("artists"), want_artist)
    return set()


def _hit_artist_key(hit: dict[str, Any]) -> str:
    """The artist part of a search hit's "Artist - Title" title, as a key."""
    title = hit.get("title")
    return name_key(title.split(" - ", 1)[0]) if isinstance(title, str) else ""


def artist_id_from_releases(
    session: _Session, name: str, *, song: str = "", album: str = ""
) -> int | None:
    """The Discogs artist called ``name`` that a release credits with the
    track's ``song`` (or, failing that, as its ``album``), found by searching
    releases by artist and track (or album), or None. None too when two
    different artists by that name both qualify: that is no answer."""
    want = name_key(name)
    if not want:
        return None
    for mode, value in (("song", song), ("album", album)):
        key = title_key(value)
        if not key:
            continue
        params = {"type": "release", "artist": name, "per_page": _SEARCH_PER_PAGE}
        params["track" if mode == "song" else "release_title"] = value
        answer = session.get("database/search", params)
        hits = answer.get("results") if isinstance(answer, dict) else None
        hits = [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []
        # The hits whose title names the artist first; one pressing of each
        # master, since every pressing credits the same artist.
        hits.sort(key=lambda h: _hit_artist_key(h) != want)
        seen_masters: set[int] = set()
        found: set[int] = set()
        checked = 0
        for hit in hits:
            release_id = _artist_id(hit.get("id"))
            master = _artist_id(hit.get("master_id"))
            if not release_id or (master and master in seen_masters):
                continue
            if master:
                seen_masters.add(master)
            if checked >= _RELEASES_CHECKED or session.cancelled():
                break
            checked += 1
            release = session.get(f"releases/{release_id}")
            found |= credits_on_release(
                release,
                want,
                song=key if mode == "song" else "",
                album=key if mode == "album" else "",
            )
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            logger.info("Discogs: %d artists share the name and the %s", len(found), mode)
            return None
    return None


# ── what is taken from the artist ──


_PROFILE_MAX_CHARS = 1400
_PROFILE_MAX_PARAGRAPHS = 8
_MEMBERS_MAX = 40
_GROUPS_MAX = 20
_RELEASES_MAX = 25
_URL_MAX_CHARS = 2048
_NAME_MAX_CHARS = 300
_ELLIPSIS = chr(0x2026)

# Discogs markup: [a=Name] and [l=Name] name an artist or a label, [a123],
# [r=123], [m123] point at one by id alone, [url=...]text[/url] is a link.
_TAG_NAMED_RE = re.compile(r"\[(?:a|l)=([^\]\[]{1,200})\]", re.IGNORECASE)
_TAG_BY_ID_RE = re.compile(r"\[(?:a|l|r|m|t)=?\d{1,12}\]", re.IGNORECASE)
_TAG_URL_RE = re.compile(r"\[url=[^\]]{0,2048}\]([\s\S]{0,2000}?)\[/url\]", re.IGNORECASE)
_TAG_BARE_URL_RE = re.compile(r"\[url\]([^\[]{0,2048})\[/url\]", re.IGNORECASE)
_TAG_STYLE_RE = re.compile(r"\[/?(?:b|i|u|s|img[^\]]{0,2048})\]", re.IGNORECASE)
_SPACES_RE = re.compile(r"[ \t\u00a0]+")


# Kept: it joins the parts of an emoji or of a letter in some scripts.
_ZWJ = chr(0x200D)


def _plain(text: str) -> str:
    """``text`` without control characters, bidi overrides or runs of spaces."""
    kept = "".join(
        ch
        for ch in text
        if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf") or ch == _ZWJ
    )
    return _SPACES_RE.sub(" ", kept)


def profile_paragraphs(profile: Any) -> list[str]:
    """A Discogs profile as plain paragraphs: its markup turned into the text
    it stands for ("[a=Nihil (5)]" is "Nihil"), references by id alone
    dropped, at most _PROFILE_MAX_CHARS in all, cut at a word."""
    if not isinstance(profile, str):
        return []
    text = profile.replace("\r\n", "\n").replace("\r", "\n")
    text = _TAG_URL_RE.sub(lambda m: m.group(1), text)
    text = _TAG_BARE_URL_RE.sub(lambda m: m.group(1), text)
    text = _TAG_NAMED_RE.sub(lambda m: display_name(m.group(1)), text)
    text = _TAG_BY_ID_RE.sub("", text)
    text = _TAG_STYLE_RE.sub("", text)
    paragraphs = []
    used = 0
    for raw in _plain(text).split("\n"):
        paragraph = re.sub(r"\s+([,.;:!?])", r"\1", raw).strip()
        if not paragraph:
            continue
        room = _PROFILE_MAX_CHARS - used
        if room <= 0 or len(paragraphs) >= _PROFILE_MAX_PARAGRAPHS:
            break
        if len(paragraph) > room:
            cut = paragraph[:room].rsplit(" ", 1)[0].rstrip(",;: ")
            paragraphs.append(cut + _ELLIPSIS)
            break
        paragraphs.append(paragraph)
        used += len(paragraph)
    return paragraphs


def _clean_name(name: Any) -> str:
    return _plain(display_name(name)).strip()[:_NAME_MAX_CHARS] if isinstance(name, str) else ""


# Where a URL on a Discogs artist goes, by host, in the order the box shows
# them. A host is the domain itself or any subdomain of it.
_LINK_HOSTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bandcamp", ("bandcamp.com",)),
    ("instagram", ("instagram.com",)),
    ("facebook", ("facebook.com", "fb.com")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("spotify", ("open.spotify.com",)),
    ("appleMusic", ("music.apple.com", "itunes.apple.com")),
)
_LINK_ORDER = ("website", *(kind for kind, _ in _LINK_HOSTS))
# Hosts that are somebody else's page about the band, not the band's own
# site: never taken for "Website".
_NOT_OWN_SITE = (
    "discogs.com",
    "wikipedia.org",
    "wikidata.org",
    "musicbrainz.org",
    "myspace.com",
    "twitter.com",
    "x.com",
    "soundcloud.com",
    "last.fm",
    "lastfm.de",
    "allmusic.com",
    "rateyourmusic.com",
    "metal-archives.com",
    "tiktok.com",
    "deezer.com",
    "tidal.com",
    "imdb.com",
    "linktr.ee",
    "reverbnation.com",
    "vk.com",
    "bandsintown.com",
    "songkick.com",
    "genius.com",
    "threads.net",
    "apple.com",
    "spotify.com",
    "amazon.com",
    "google.com",
    "archive.org",
)


def _on(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def safe_url(value: Any) -> str:
    """``value`` as a plain http(s) URL, or "": no spaces or control
    characters, a host straight after the scheme, no user:password@. A bare
    "www.band.com" is read as https."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if (
        not text
        or len(text) > _URL_MAX_CHARS
        or any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in text)
    ):
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*:", text, re.IGNORECASE):
        if not re.match(
            r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[/?#]|$)", text, re.I
        ):
            return ""
        text = "https://" + text
    if not re.match(r"^https?://[^/\\]", text, re.IGNORECASE):
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return text


def official_links(urls: Any) -> list[dict[str, str]]:
    """The band's own links among a Discogs artist's URLs, as [{kind, url}],
    one of each kind, in _LINK_ORDER: its site (the first URL on no known
    service), Bandcamp, Instagram, Facebook, YouTube, Spotify, Apple Music."""
    found: dict[str, str] = {}
    for value in urls if isinstance(urls, list) else []:
        url = safe_url(value)
        if not url:
            continue
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        kind = next((k for k, domains in _LINK_HOSTS if _on(host, domains)), "")
        if not kind and not _on(host, _NOT_OWN_SITE):
            kind = "website"
        if kind and kind not in found:
            found[kind] = url
    return [{"kind": kind, "url": found[kind]} for kind in _LINK_ORDER if kind in found]


def main_releases(answer: Any) -> list[dict[str, str]]:
    """An artist's own releases (GET /artists/{id}/releases, role "Main"), as
    [{"year", "title"}], oldest first: a master once, over any release of the
    same title, and at most _RELEASES_MAX."""
    items = answer.get("releases") if isinstance(answer, dict) else None
    kept: dict[str, tuple[int, int, str, str]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("role") != "Main":
            continue
        title = _plain(str(item.get("title") or "")).strip()[:_NAME_MAX_CHARS]
        key = title_key(title)
        if not key:
            continue
        year = item.get("year")
        year = year if isinstance(year, int) and not isinstance(year, bool) and year > 0 else 0
        rank = 0 if item.get("type") == "master" else 1
        entry = (rank, year or 9999, title, str(year) if year else "")
        if key not in kept or entry[:2] < kept[key][:2]:
            kept[key] = entry
    ordered = sorted(kept.values(), key=lambda e: (e[1], e[2].lower()))
    return [{"year": year, "title": title} for _, _, title, year in ordered[:_RELEASES_MAX]]


_ARTIST_PAGE_RE = re.compile(r"^https://www\.discogs\.com/(?:[a-z]{2}/)?artist/\d{1,12}[-\w%.()]*$")


def artist_profile(artist: Any, releases: Any) -> dict[str, Any] | None:
    """What the artist box shows from a Discogs artist (GET /artists/{id})
    and its releases, or None when it has no id or name. Images are left out:
    the box takes its photos from Wikimedia, and Discogs' images are theirs."""
    if not isinstance(artist, dict):
        return None
    artist_id = _artist_id(artist.get("id"))
    name = _clean_name(artist.get("name"))
    if not artist_id or not name:
        return None
    members: dict[str, list[str]] = {"current": [], "former": []}
    for member in artist.get("members") or []:
        if not isinstance(member, dict):
            continue
        who = _clean_name(member.get("name"))
        bucket = members["former" if member.get("active") is False else "current"]
        if who and who not in bucket and len(bucket) < _MEMBERS_MAX:
            bucket.append(who)
    groups = []
    for group in artist.get("groups") or []:
        who = _clean_name(group.get("name")) if isinstance(group, dict) else ""
        if who and who not in groups and len(groups) < _GROUPS_MAX:
            groups.append(who)
    page = artist.get("uri")
    if not isinstance(page, str) or not _ARTIST_PAGE_RE.match(page):
        page = f"https://www.discogs.com/artist/{artist_id}"
    real_name = _plain(str(artist.get("realname") or "")).strip()[:_NAME_MAX_CHARS]
    return {
        "id": artist_id,
        "name": name,
        "real_name": real_name if real_name and name_key(real_name) != name_key(name) else "",
        "profile": profile_paragraphs(artist.get("profile")),
        "members": members,
        "groups": groups,
        "links": official_links(artist.get("urls")),
        "releases": main_releases(releases),
        "url": page,
    }


# ── one track's artist ──


def _first(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def lookup_for_track(
    identity: dict[str, Any] | None,
    audio_tags: dict[str, str] | None,
    band: dict[str, str] | None,
    token: str,
    *,
    cancelled: Cancelled = _never,
) -> tuple[dict[str, Any] | None, bool]:
    """The Discogs profile of the artist a track is by, and whether the answer
    is settled (False when a request failed, so "nothing" may be "could not
    ask"). ``band`` is the Wikidata band saved on the track, if any. Never
    raises."""
    session = _Session(token=token, cancelled=cancelled)
    identity = identity or {}
    tags = audio_tags or {}
    try:
        artist_id = artist_id_from_musicbrainz(
            list(identity.get("artist_mbids") or []), cancelled=cancelled
        )
        if not artist_id and band and not cancelled():
            artist_id = artist_id_from_wikidata(str(band.get("id") or ""))
        if not artist_id and not cancelled():
            name = tagged_artist_name(_first(tags.get("artist"), identity.get("artist")))
            song = _first(identity.get("title"), tags.get("title"))
            album = _first(identity.get("album"), tags.get("album"))
            artist_id = artist_id_from_releases(session, name, song=song, album=album)
        if not artist_id or cancelled():
            return None, not session.failed and not cancelled()
        artist = session.get(f"artists/{artist_id}")
        if artist is None:
            return None, not session.failed
        releases = session.get(
            f"artists/{artist_id}/releases",
            {"sort": "year", "sort_order": "asc", "per_page": "100"},
        )
        return artist_profile(artist, releases), True
    except Exception:
        logger.warning("Discogs lookup failed", exc_info=True)
        return None, False


# Answers by what they were asked from, for the life of the process: a band
# found is kept a day (its pages are on disk for a month besides), "nothing"
# an hour, and a failure not at all.
_KEPT: dict[tuple, tuple[float, dict[str, Any] | None]] = {}
_KEPT_LOCK = threading.Lock()
_KEPT_MAX = 512
_KEEP_FOUND_SEC = 24 * 3600
_KEEP_NOTHING_SEC = 3600


def _ask_key(identity: Any, tags: Any, band: Any) -> tuple:
    identity = identity if isinstance(identity, dict) else {}
    tags = tags if isinstance(tags, dict) else {}
    band = band if isinstance(band, dict) else {}
    return (
        tuple(identity.get("artist_mbids") or ()),
        str(band.get("id") or ""),
        _first(tags.get("artist"), identity.get("artist")),
        _first(identity.get("title"), tags.get("title")),
        _first(identity.get("album"), tags.get("album")),
    )


def artist_for_track(
    identity: dict[str, Any] | None,
    audio_tags: dict[str, str] | None,
    band: dict[str, str] | None,
    token: str,
    *,
    cancelled: Cancelled = _never,
) -> dict[str, Any] | None:
    """lookup_for_track's profile, kept in memory as said above. Never raises."""
    if not token:
        return None
    key = _ask_key(identity, audio_tags, band)
    with _KEPT_LOCK:
        kept = _KEPT.get(key)
    if kept is not None:
        at, answer = kept
        if time.time() - at < (_KEEP_FOUND_SEC if answer else _KEEP_NOTHING_SEC):
            return answer
    answer, settled = lookup_for_track(identity, audio_tags, band, token, cancelled=cancelled)
    if answer is not None or settled:
        with _KEPT_LOCK:
            if len(_KEPT) >= _KEPT_MAX:
                _KEPT.clear()
            _KEPT[key] = (time.time(), answer)
    return answer


def forget() -> None:
    """Drop every answer kept in memory. For tests."""
    with _KEPT_LOCK:
        _KEPT.clear()
