"""The work a soundtrack or cast recording is from: the musical, film or series.

A song from a musical has no band to show. "Popular" is credited to Kristin
Chenoweth, but what someone opening it wants to read about is Wicked. So when
the recording a job is identified as (identify.py) came out on a soundtrack, or
its album says it is one ("Wicked (Original Broadway Cast Recording)", "The
Wizard of Oz (Original Motion Picture Soundtrack)"), the work is looked for on
Wikidata, best source first:

1. The release group's own links on MusicBrainz. A soundtrack's Wikidata link
   is sometimes the film itself (The Wizard of Oz links both the album and the
   1939 film). More often it is the album, and the work is the item that names
   that album as its soundtrack release (P406). An IMDb link names the film.
2. A Wikidata search for the album's name with the qualifier taken off
   ("Wicked", "The Wizard of Oz"), kept only when the name is the hit's label
   or alias exactly, once case, accents and punctuation are set aside. When
   no album says so, the show the track's own title names ('Dancing Through
   Life (From "Wicked" Original Broadway Cast Recording/2003)'), which also
   gives the kind and the year when the album does not (title_hint).

Either way, an item is kept only when it is a kind of work a song can be from:
a musical, a film, a television series or a stage work, directly or through
Wikidata's "subclass of" (WORK_CLASS_DEPTH steps up). The novel, the video game
and the album that share the musical's name are not. Several that are (the
2003 musical and the 2024 film are both "Wicked") are told apart by what the
album says it is (a "Broadway Cast Recording" is the musical) and by year: a
work first shown well after the recording came out cannot be what it is from.

What is sent: a release group's MBID to MusicBrainz, and to Wikidata the
album's name or ids found on MusicBrainz. Everything here is best-effort:
find_work never raises, and no answer leaves the job without a work.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any

from app.core.config import WORK_CLASS_DEPTH, WORK_YEAR_SLACK
from app.core.models import clean_work
from app.pipeline import artist_lookup, musicbrainz
from app.pipeline.artist_lookup import FetchJson, artist_name_key, search_language
from app.pipeline.title_parse import TitleReading, work_hint

logger = logging.getLogger("stemdeck.work")

INSTANCE_OF = "P31"
SUBCLASS_OF = "P279"
SOUNDTRACK_RELEASE = "P406"
IMDB_ID = "P345"
PUBLICATION_DATE = "P577"
FIRST_PERFORMANCE = "P1191"
START_TIME = "P580"

# The classes a work a song comes from is an instance of, and the kind each
# one makes it. The ones met most often are listed so that no walk up the
# class tree is needed for them; anything else is walked up from.
WORK_CLASSES: dict[str, str] = {
    "Q11424": "film",
    "Q24869": "film",  # feature film
    "Q202866": "film",  # animated film
    "Q842256": "film",  # musical film
    "Q24862": "film",  # short film
    "Q506240": "film",  # television film
    "Q24856": "film",  # film series
    "Q5398426": "tv",  # television series
    "Q15416": "tv",  # television program
    "Q7724161": "tv",  # television serial
    "Q1259759": "tv",  # miniseries
    "Q581714": "tv",  # animated series
    "Q117467246": "tv",  # animated television series
    "Q63952888": "tv",  # anime television series
    "Q2743": "musical",  # musical play
    "Q58483083": "musical",  # dramatico-musical work
    "Q7777570": "other",  # theatrical production
    "Q25379": "other",  # play
    "Q1344": "other",  # opera
}
# When an item reaches more than one kind: a musical film is a film.
_KIND_ORDER = ("film", "tv", "musical", "other")
# What shares a work's name and is never it. Checked on the item's own
# classes, so an item typed as one of these is not walked up from at all.
NOT_WORKS = frozenset(
    {
        "Q7889",  # video game
        "Q7725634",  # literary work
        "Q8261",  # novel
        "Q482994",  # album
        "Q4176708",  # soundtrack album
        "Q134556",  # single
        "Q7366",  # song
    }
)

_QID_RE = re.compile(r"^Q\d{1,12}$")
_IMDB_RE = re.compile(r"^(tt\d{7,10})/?$")
_IMDB_URL_RE = re.compile(r"^https?://(?:www\.)?imdb\.com/title/(tt\d{7,10})/?$")
_WIKIDATA_URL_RE = re.compile(r"^https?://(?:www\.)?wikidata\.org/wiki/(Q\d{1,12})$")
_YEAR_RE = re.compile(r"^[+]?(\d{4})")
# Links followed from one release group, at most. A soundtrack links one
# album item and sometimes the film; more is a group linking every reissue.
_MAX_LINKS = 3
_SEARCH_LIMIT = "10"

# What an album's name says when it is a soundtrack or a cast recording.
_WORK_WORDS_RE = re.compile(
    r"soundtrack|\bost\b|\bscore\b|\bcast\b|motion picture|\bmusical\b|broadway|west end"
    r"|\bfilm\b|\bmovie\b|television|\btv\b|\bseries\b",
    re.IGNORECASE,
)
_BRACKETED_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]")
# "Wicked: The Soundtrack", "Les Misérables - Original London Cast": the part
# after the colon or dash, when it is the qualifier. Hyphen, en dash, em dash.
_DASHES = "-" + chr(0x2013) + chr(0x2014)
_TAIL_SPLIT_RE = re.compile(rf"\s*:\s+|\s+[{_DASHES}]\s+")
# "Music from the Motion Picture Grease", "Original Soundtrack from Frozen".
_LEADING_RE = re.compile(
    r"^(?:(?:the\s+)?original\s+)?(?:music|songs|soundtrack|score)\s+(?:from|of|to)\s+"
    r"(?:and\s+inspired\s+by\s+)?(?:the\s+)?(?:original\s+)?"
    r"(?:broadway\s+musical|motion\s+picture|film|movie|musical|television\s+series|series)\s+",
    re.IGNORECASE,
)
# "Frozen Original Soundtrack", "Frozen OST".
_TRAILING_RE = re.compile(
    r"\s+(?:(?:original\s+)?(?:motion\s+picture\s+)?(?:soundtrack|score)|ost|o\.s\.t\.)$",
    re.IGNORECASE,
)
_HINTS = (
    ("musical", re.compile(r"broadway|west end|\bcast\b|\bmusical\b|\bstage\b", re.IGNORECASE)),
    ("film", re.compile(r"motion picture|\bfilm\b|\bmovie\b", re.IGNORECASE)),
    ("tv", re.compile(r"television|\btv\b|\bseries\b", re.IGNORECASE)),
    # Last: a stage show's album is a cast recording, so a plain "soundtrack"
    # that names no cast, series or show ("Wicked: The Soundtrack") is a film's.
    ("film", re.compile(r"soundtrack|\bost\b|\bscore\b", re.IGNORECASE)),
)

# Each class's "subclass of", as Wikidata answered it, for the life of the
# process: classes are few and do not change between two imports.
_CLASS_PARENTS: dict[str, list[str]] = {}
_CLASS_LOCK = threading.Lock()


# ── what the album says ──


def strip_album(album: Any) -> str:
    """An album's name with the soundtrack or cast recording qualifier taken
    off: "Wicked (Original Broadway Cast Recording)" is "Wicked". A name with
    no qualifier comes back as it was."""
    text = str(album or "").strip()
    text = _BRACKETED_RE.sub(
        lambda m: "" if _WORK_WORDS_RE.search(m.group()) else m.group(), text
    ).strip()
    parts = _TAIL_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and _WORK_WORDS_RE.search(parts[1]):
        text = parts[0]
    text = _LEADING_RE.sub("", text)
    text = _TRAILING_RE.sub("", text)
    return text.strip(" \t:" + _DASHES)


def kind_hint(album: Any) -> str | None:
    """The kind of work an album's name says it is from, or None."""
    text = str(album or "")
    for kind, pattern in _HINTS:
        if pattern.search(text):
            return kind
    return None


def _is_soundtrack(identity: dict[str, Any] | None) -> bool:
    types = (identity or {}).get("secondary_types") or []
    return any(isinstance(t, str) and t.lower() == "soundtrack" for t in types)


def title_hint(identity: dict[str, Any] | None, title: Any) -> TitleReading | None:
    """The work the track's title names ('Dancing Through Life (From "Wicked"
    Original Broadway Cast Recording/2003)', "The Greatest Showman Cast - This
    Is Me"), read the way that makes the identified song the song. None when
    it names none, or when MusicBrainz placed the recording on a release that
    is no soundtrack (a studio album): then a work in the title is noise."""
    identity = identity if isinstance(identity, dict) else {}
    if (
        identity.get("source") in ("acoustid", "musicbrainz")
        and identity.get("release_group_mbid")
        and not identity.get("secondary_types")
    ):
        return None
    return work_hint(title, identity.get("title"))


def work_query(
    identity: dict[str, Any] | None, tags: dict[str, str] | None, title: Any = None
) -> tuple[str, str | None, bool] | None:
    """(name to look for, kind hint, whether the release group is the
    soundtrack) for a job, or None when nothing says it is from a work.

    The release group being a soundtrack says so, and so does an album name
    carrying a qualifier: the identity's (the release group's title) or the
    file's own album tag, which may name the soundtrack when the recording
    was identified as a compilation it is also on. Last, the track's title
    naming the show (title_hint), which is all a YouTube upload of a cast
    recording has; it also says the kind when the album does not."""
    identity = identity if isinstance(identity, dict) else {}
    tags = tags if isinstance(tags, dict) else {}
    albums = [
        a.strip()
        for a in (identity.get("album"), tags.get("album"))
        if isinstance(a, str) and a.strip()
    ]
    from_title = title_hint(identity, title) if title else None
    hint = next((h for h in (kind_hint(a) for a in albums) if h), None) or (
        from_title.kind if from_title else None
    )
    if _is_soundtrack(identity) and identity.get("album"):
        name = strip_album(identity["album"])
        return (name, hint, True) if name else None
    for album in albums:
        name = strip_album(album)
        if name and name != album:
            return name, hint, False
    if from_title:
        return from_title.work, hint, False
    return None


def might_have_work(
    identity: dict[str, Any] | None, tags: dict[str, str] | None, title: Any = None
) -> bool:
    """Whether find_work has anything to go on."""
    return work_query(identity, tags, title) is not None


# ── Wikidata ──


def _claim_ids(entity: dict[str, Any], prop: str) -> list[str]:
    """The item ids of an entity's claims for ``prop``, deprecated ones left out."""
    claims = entity.get("claims")
    statements = claims.get(prop) if isinstance(claims, dict) else None
    ids = []
    for statement in statements if isinstance(statements, list) else []:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        snak = statement.get("mainsnak")
        datavalue = snak.get("datavalue") if isinstance(snak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        qid = value.get("id") if isinstance(value, dict) else None
        if isinstance(qid, str) and _QID_RE.match(qid) and qid not in ids:
            ids.append(qid)
    return ids


def work_year(entity: dict[str, Any]) -> int | None:
    """The year a work first came out: published, first performed or first
    aired, whichever is earliest. None when Wikidata has none of them."""
    years = []
    claims = entity.get("claims") if isinstance(entity.get("claims"), dict) else {}
    for prop in (PUBLICATION_DATE, FIRST_PERFORMANCE, START_TIME):
        for statement in claims.get(prop) or []:
            snak = statement.get("mainsnak") if isinstance(statement, dict) else None
            datavalue = snak.get("datavalue") if isinstance(snak, dict) else None
            value = datavalue.get("value") if isinstance(datavalue, dict) else None
            time = value.get("time") if isinstance(value, dict) else None
            match = _YEAR_RE.match(time) if isinstance(time, str) else None
            if match:
                years.append(int(match.group(1)))
    return min(years) if years else None


def _entities(ids: list[str], lang: str, fetch_json: FetchJson) -> dict[str, Any]:
    ids = [i for i in dict.fromkeys(ids) if _QID_RE.match(i)][:50]
    if not ids:
        return {}
    answer = fetch_json(
        {
            "action": "wbgetentities",
            "ids": "|".join(ids),
            "props": "claims|labels",
            "languages": "en" if lang == "en" else f"{lang}|en",
            "languagefallback": "1",
        }
    )
    entities = answer.get("entities") if isinstance(answer, dict) else None
    return entities if isinstance(entities, dict) else {}


def _class_parents(ids: list[str], fetch_json: FetchJson) -> dict[str, list[str]]:
    """Each class's "subclass of", from the cache or one request for all the
    classes it has not seen."""
    with _CLASS_LOCK:
        missing = [i for i in ids if i not in _CLASS_PARENTS]
    if missing:
        answer = fetch_json(
            {"action": "wbgetentities", "ids": "|".join(missing[:50]), "props": "claims"}
        )
        entities = answer.get("entities") if isinstance(answer, dict) else None
        found = {
            qid: _claim_ids(entity, SUBCLASS_OF)
            for qid, entity in (entities if isinstance(entities, dict) else {}).items()
            if isinstance(entity, dict)
        }
        with _CLASS_LOCK:
            for qid in missing[:50]:
                _CLASS_PARENTS[qid] = found.get(qid, [])
    with _CLASS_LOCK:
        return {i: _CLASS_PARENTS.get(i, []) for i in ids}


def _best_kind(classes: list[str]) -> str | None:
    kinds = {WORK_CLASSES[c] for c in classes if c in WORK_CLASSES}
    return next((k for k in _KIND_ORDER if k in kinds), None)


def work_kind(
    entity: dict[str, Any],
    fetch_json: FetchJson,
    cancelled: Callable[[], bool] = lambda: False,
) -> str | None:
    """The kind of work ``entity`` is ("musical", "film", "tv", "other"), or
    None when it is not one a song can be from.

    Its own classes first. Then, unless one of them is a thing that is never a
    work (NOT_WORKS), up through "subclass of", one request per step, up to
    WORK_CLASS_DEPTH steps; the nearest step with a known class decides."""
    classes = _claim_ids(entity, INSTANCE_OF)
    kind = _best_kind(classes)
    if kind or not classes or NOT_WORKS.intersection(classes):
        return kind
    seen = set(classes)
    frontier = classes
    for _ in range(WORK_CLASS_DEPTH):
        if cancelled() or not frontier:
            return None
        parents = _class_parents(frontier[:50], fetch_json)
        step = [p for ps in parents.values() for p in ps if p not in seen]
        kind = _best_kind(step)
        if kind:
            return kind
        seen.update(step)
        frontier = list(dict.fromkeys(step))
    return None


def _label(entity: dict[str, Any], *codes: str) -> str:
    labels = entity.get("labels")
    for code in codes:
        label = labels.get(code) if isinstance(labels, dict) else None
        if isinstance(label, dict) and isinstance(label.get("value"), str):
            return label["value"]
    return ""


def _work(entity: dict[str, Any], kind: str, lang: str, name: str = "") -> dict[str, str] | None:
    return clean_work(
        {
            "id": entity.get("id"),
            "kind": kind,
            "name": _label(entity, lang, "en") or name,
            "englishName": _label(entity, "en"),
        }
    )


def _items_with(prop: str, value: str, fetch_json: FetchJson) -> list[str]:
    """The items that have a ``prop`` statement of ``value``, by Wikidata's
    search. ``value`` is a checked item id or IMDb id, never free text."""
    answer = fetch_json(
        {
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:{prop}={value}",
            "srnamespace": "0",
            "srlimit": "5",
        }
    )
    query = answer.get("query") if isinstance(answer, dict) else None
    hits = query.get("search") if isinstance(query, dict) else None
    return [
        hit["title"]
        for hit in (hits if isinstance(hits, list) else [])
        if isinstance(hit, dict)
        and isinstance(hit.get("title"), str)
        and _QID_RE.match(hit["title"])
    ]


def release_group_links(group: Any) -> tuple[list[str], list[str]]:
    """(Wikidata item ids, IMDb title ids) a MusicBrainz release group links."""
    qids: list[str] = []
    imdb: list[str] = []
    relations = group.get("relations") if isinstance(group, dict) else None
    for relation in relations if isinstance(relations, list) else []:
        url = relation.get("url") if isinstance(relation, dict) else None
        resource = url.get("resource") if isinstance(url, dict) else None
        if not isinstance(resource, str):
            continue
        wikidata = _WIKIDATA_URL_RE.match(resource)
        film = _IMDB_URL_RE.match(resource)
        if wikidata and wikidata.group(1) not in qids:
            qids.append(wikidata.group(1))
        elif film and film.group(1) not in imdb:
            imdb.append(film.group(1))
    return qids[:_MAX_LINKS], imdb[:_MAX_LINKS]


def _first_work(
    ids: list[str], lang: str, fetch_json: FetchJson, cancelled: Callable[[], bool]
) -> dict[str, str] | None:
    """The first of ``ids``, in order, that is a work: one typed as a known
    kind of work outright first, so no class tree is walked when one is."""
    if not ids or cancelled():
        return None
    entities = _entities(ids, lang, fetch_json)
    found = [entities[qid] for qid in ids if isinstance(entities.get(qid), dict)]
    for entity in found:
        kind = _best_kind(_claim_ids(entity, INSTANCE_OF))
        if kind:
            return _work(entity, kind, lang)
    for entity in found:
        if cancelled():
            return None
        kind = work_kind(entity, fetch_json, cancelled)
        if kind:
            return _work(entity, kind, lang)
    return None


def work_from_links(
    qids: list[str],
    imdb: list[str],
    lang: str,
    fetch_json: FetchJson,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The work a release group's links name: a linked item that is one; else
    the item naming a linked album as its soundtrack release; else the item
    with a linked IMDb id."""
    work = _first_work(qids, lang, fetch_json, cancelled)
    if work:
        return work
    found: list[str] = []
    for qid in qids:
        if cancelled():
            return None
        found += [i for i in _items_with(SOUNDTRACK_RELEASE, qid, fetch_json) if i not in qids]
    for title in imdb:
        if cancelled():
            return None
        if _IMDB_RE.match(title):
            found += _items_with(IMDB_ID, title, fetch_json)
    return _first_work(list(dict.fromkeys(found)), lang, fetch_json, cancelled)


def search_work(
    name: str,
    hint: str | None,
    year: int | None,
    fetch_json: FetchJson,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The work called ``name`` on Wikidata, or None.

    Only hits whose label or alias is ``name`` exactly (artist_name_key) are
    considered, and only those that are works (work_kind). Of those, a work
    of the kind ``hint`` says beats one that is not, and a work first shown
    more than WORK_YEAR_SLACK years after ``year`` is left out; otherwise the
    search's own order, which puts the famous one first."""
    want = artist_name_key(name)
    if not want or cancelled():
        return None
    lang = search_language(name)
    found = fetch_json(
        {
            "action": "wbsearchentities",
            "search": name,
            "language": lang,
            "uselang": lang,
            "type": "item",
            "limit": _SEARCH_LIMIT,
        }
    )
    hits = found.get("search") if isinstance(found, dict) else None
    ids = []
    for hit in hits if isinstance(hits, list) else []:
        if not isinstance(hit, dict) or not _QID_RE.match(str(hit.get("id") or "")):
            continue
        match = hit.get("match") if isinstance(hit.get("match"), dict) else {}
        if want in (artist_name_key(hit.get("label")), artist_name_key(match.get("text"))):
            ids.append(hit["id"])
    if not ids or cancelled():
        return None
    entities = _entities(ids, lang, fetch_json)
    candidates = []
    for rank, qid in enumerate(ids):
        entity = entities.get(qid)
        if not isinstance(entity, dict) or cancelled():
            continue
        born = work_year(entity)
        if year and born and born > year + WORK_YEAR_SLACK:
            continue
        kind = work_kind(entity, fetch_json, cancelled)
        if kind:
            candidates.append(((bool(hint) and kind != hint, rank), entity, kind))
    if not candidates:
        return None
    _, entity, kind = min(candidates, key=lambda c: c[0])
    return _work(entity, kind, lang, name)


def _release_year(group: Any) -> int | None:
    date = group.get("first-release-date") if isinstance(group, dict) else None
    match = _YEAR_RE.match(date) if isinstance(date, str) else None
    return int(match.group(1)) if match else None


def lookup_work(
    identity: dict[str, Any] | None,
    tags: dict[str, str] | None,
    *,
    title: Any = None,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The work a job's song is from (see the module), or None. Raises when
    Wikidata cannot be reached; a MusicBrainz failure only skips its links.

    ``title``, the track's own, can name the show and its kind and year
    (title_hint). When the release group's links name a work of another kind
    than that (a film where the title says Broadway cast), the name search is
    asked too, and a work of the kind the title says wins."""
    query = work_query(identity, tags, title)
    if query is None or cancelled():
        return None
    name, hint, is_soundtrack = query
    from_title = title_hint(identity, title) if title else None
    # Resolved here, so a test can stand in for the network by replacing
    # artist_lookup._fetch_json, as conftest does for every test.
    fetch_json = fetch_json or artist_lookup._fetch_json
    lang = search_language(name)
    year = None
    linked = None
    mbid = (identity or {}).get("release_group_mbid")
    if is_soundtrack and mbid:
        try:
            group = musicbrainz.lookup_release_group(mbid, cancelled=cancelled)
        except Exception:
            logger.info("release group lookup failed", exc_info=True)
            group = None
        if cancelled():
            return None
        year = _release_year(group)
        qids, imdb = release_group_links(group)
        linked = work_from_links(qids, imdb, lang, fetch_json, cancelled)
        if linked and not (hint and linked["kind"] != hint):
            return linked
    if cancelled():
        return None
    year = year or (from_title.year if from_title else None)
    found = search_work(name, hint, year, fetch_json, cancelled)
    if found and (linked is None or found["kind"] == hint):
        return found
    return linked or found


def find_work(
    identity: dict[str, Any] | None,
    tags: dict[str, str] | None,
    *,
    title: Any = None,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """lookup_work, never raising: any failure is logged and gives None."""
    try:
        return lookup_work(identity, tags, title=title, fetch_json=fetch_json, cancelled=cancelled)
    except Exception:
        logger.info("work lookup failed", exc_info=True)
        return None
