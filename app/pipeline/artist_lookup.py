"""The band a job's artist tag names, found on Wikidata while the job runs (#699).

The page used to do this itself, once a finished track was opened, so the
artist box, the Lyrics tab and the now-playing card only learned the band a
moment after the track was open, and only while the page was. Done here, it
runs beside separation, which takes minutes, and the band is on the job the
moment the import is done.

This is a port of the page's own lookup (static/js/artistLookup.js), kept to
the same rule: the first search hit that has a MusicBrainz artist id is the
band, and it is kept only when its name is the tag's name once case, Latin
accents, punctuation and spacing are set aside. Wikidata's search forgives
spelling, so a near miss still finds some band, and saved with nobody looking
the name has to agree.

What is sent is the tag's artist name and nothing else about the track.

Everything here is best-effort. No tag, no match, no connection or a slow
answer all leave the band unset, and the page then looks for it itself when
the track is opened.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from app.core.config import ARTIST_LOOKUP_MAX_BYTES, ARTIST_LOOKUP_USER_AGENT, TIMEOUT_ARTIST_LOOKUP
from app.core.models import Job, _set, clean_artist

logger = logging.getLogger("stemdeck.artist")

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Having a MusicBrainz artist ID is what separates the band from the album
# named after it, a cinema that shares its name, or a disambiguation page.
MUSICBRAINZ_ARTIST_ID = "P434"
_QID_RE = re.compile(r"^Q\d+$")
_SEARCH_LIMIT = "10"

# "Artist feat. Someone", "Artist (ft. Someone)": no band is called that.
_FEATURING_RE = re.compile(r"\s+[(\[]?(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)

FetchJson = Callable[[dict[str, str]], Any]


def artist_name_key(name: Any) -> str:
    """A name reduced to what tells it apart: no case, Latin accents,
    punctuation, symbols or spaces. artistNameKey in artistLookup.js.

    Only the Latin combining marks are dropped: the marks in a Japanese or
    Korean name are part of it, and folding them could make two names one.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not 0x300 <= ord(ch) <= 0x36F).lower()
    return "".join(
        ch for ch in text if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S")
    )


def tagged_artist_name(tag: Any) -> str:
    """The name to look up from an artist tag, less any featured artist."""
    return _FEATURING_RE.split(str(tag or ""), maxsplit=1)[0].strip()


def artist_matches_name(artist: dict[str, str] | None, name: str) -> bool:
    """Whether a looked-up band's name, or its English name, is the tag's."""
    want = artist_name_key(name)
    if not want or not artist:
        return False
    return any(
        artist_name_key(candidate) == want
        for candidate in (artist.get("name"), artist.get("englishName"))
    )


def pick_artist(entities: Any, ranked_ids: list[str]) -> dict[str, Any] | None:
    """The first search hit, in the order the search ranked them, that is an
    artist, or None. ``entities`` is wbgetentities' ``entities`` object."""
    if not isinstance(entities, dict):
        return None
    for qid in ranked_ids:
        entity = entities.get(qid)
        if not isinstance(entity, dict):
            continue
        claims = entity.get("claims")
        if isinstance(claims, dict) and claims.get(MUSICBRAINZ_ARTIST_ID):
            return entity
    return None


def search_language(name: str) -> str:
    """The Wikidata language to search a name in.

    The page searched in the language the app was in. The server has no such
    thing, so the name's own script stands in for it: a name written in kana,
    Hangul, Han, Cyrillic or Greek is searched among the labels in that
    script's language, where Wikidata keeps it, and anything else in English.
    """
    for ch in name:
        if 0x3040 <= ord(ch) <= 0x30FF:
            return "ja"
    for ch in name:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            return "ko"
        if 0x4E00 <= code <= 0x9FFF:
            return "zh"
        if 0x0400 <= code <= 0x04FF:
            return "ru"
        if 0x0370 <= code <= 0x03FF:
            return "el"
    return "en"


def _ssl_context() -> ssl.SSLContext:
    # certifi where it is installed, as for the model downloads in
    # separate.py: a packaged Python on macOS may have no CA store of its own.
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except (ModuleNotFoundError, OSError):
        return ssl.create_default_context()


def _fetch_json(params: dict[str, str]) -> Any:
    """One Wikidata API request. Raises on any failure."""
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    request = urllib.request.Request(
        url, headers={"User-Agent": ARTIST_LOOKUP_USER_AGENT, "Accept": "application/json"}
    )
    # A fixed https URL with only its query built here, never a URL from a
    # request or a tag, which is what B310 exists to catch.
    with urllib.request.urlopen(  # nosec B310
        request, timeout=TIMEOUT_ARTIST_LOOKUP, context=_ssl_context()
    ) as response:
        body = response.read(ARTIST_LOOKUP_MAX_BYTES + 1)
    if len(body) > ARTIST_LOOKUP_MAX_BYTES:
        raise ValueError("Wikidata answer too large")
    return json.loads(body)


def _label(entity: dict[str, Any], *codes: str) -> str:
    """The entity's label in the first of ``codes`` it has one in, or ""."""
    labels = entity.get("labels")
    if not isinstance(labels, dict):
        return ""
    for code in codes:
        label = labels.get(code)
        if isinstance(label, dict) and isinstance(label.get("value"), str):
            return label["value"]
    return ""


# The languages a name's labels are read in, by the language it is searched
# in. A name in Chinese characters alone may be Chinese in either script
# (鄧麗君, 邓丽君) or Japanese (米津玄師): its label is looked for in all of
# them, since Wikidata keeps a separate one for each.
_LABEL_LANGUAGES = {
    "zh": ("zh", "zh-hant", "zh-hans", "zh-tw", "zh-hk", "zh-cn", "ja"),
}


def _languages(lang: str) -> list[str]:
    return [*_LABEL_LANGUAGES.get(lang, (lang,)), "en"] if lang != "en" else ["en"]


def _names_in(entity: dict[str, Any], codes: list[str]) -> list[tuple[str, bool]]:
    """(name, is_label) for every label and alias the entity has in
    ``codes``, labels first."""
    found: list[tuple[str, bool]] = []
    for field, is_label in (("labels", True), ("aliases", False)):
        values = entity.get(field)
        if not isinstance(values, dict):
            continue
        for code in codes:
            entries = values.get(code)
            for entry in entries if isinstance(entries, list) else [entries]:
                if isinstance(entry, dict) and isinstance(entry.get("value"), str):
                    found.append((entry["value"], is_label))
    return found


def _display_name(entity: dict[str, Any], lang: str, name: str) -> str:
    """What to call a band found for ``name``: its label in the script the
    name was written in when one matches it ("邓丽君" searched finds the
    simplified label, not the traditional 鄧麗君), else its label in
    ``lang``'s languages, else English."""
    codes = _languages(lang)
    want = artist_name_key(name)
    for label, is_label in _names_in(entity, codes):
        if is_label and want and artist_name_key(label) == want:
            return label
    return _label(entity, *codes)


def _goes_by(entity: dict[str, Any], lang: str, name: str) -> bool:
    """Whether any label or alias of the entity in ``lang``'s languages, or
    English, is ``name`` once case, accents and punctuation are set aside."""
    want = artist_name_key(name)
    return bool(want) and any(
        artist_name_key(value) == want for value, _ in _names_in(entity, _languages(lang))
    )


def lookup_band(
    name: str,
    *,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The band called ``name`` on Wikidata, as {"id", "name", "englishName"},
    or None when nothing there is an artist by exactly that name. Raises when
    Wikidata cannot be reached or answers nonsense."""
    search = name.strip()
    if not search or cancelled():
        return None
    # Resolved here rather than as the default, so a test can stand in for
    # the network by replacing _fetch_json.
    fetch_json = fetch_json or _fetch_json
    lang = search_language(search)
    found = fetch_json(
        {
            "action": "wbsearchentities",
            "search": search,
            "language": lang,
            "uselang": lang,
            "type": "item",
            "limit": _SEARCH_LIMIT,
        }
    )
    hits = found.get("search") if isinstance(found, dict) else None
    ids = [
        hit["id"]
        for hit in (hits if isinstance(hits, list) else [])
        if isinstance(hit, dict) and isinstance(hit.get("id"), str) and _QID_RE.match(hit["id"])
    ]
    if not ids or cancelled():
        return None
    answer = fetch_json(
        {
            "action": "wbgetentities",
            "ids": "|".join(ids),
            # Aliases too, outside English: a band written in another script
            # is often known by a name its label is not (IU is 아이유).
            "props": "claims|labels" if lang == "en" else "claims|labels|aliases",
            "languages": "|".join(_languages(lang)),
            "languagefallback": "1",
        }
    )
    entity = pick_artist(answer.get("entities") if isinstance(answer, dict) else None, ids)
    if entity is None:
        return None
    band = clean_artist(
        {
            "id": entity.get("id"),
            "name": _display_name(entity, lang, search) or search,
            # Kept beside the name: LRCLIB lists artists by the name they
            # release under, which the English label nearly always is.
            "englishName": _label(entity, "en"),
        }
    )
    if not band:
        return None
    if artist_matches_name(band, search) or (lang != "en" and _goes_by(entity, lang, search)):
        return band
    return None


def _claim_values(entity: dict[str, Any], prop: str) -> set[str]:
    """The plain string values of one of the entity's claims."""
    claims = entity.get("claims")
    statements = claims.get(prop) if isinstance(claims, dict) else None
    values = set()
    for statement in statements if isinstance(statements, list) else []:
        snak = statement.get("mainsnak") if isinstance(statement, dict) else None
        datavalue = snak.get("datavalue") if isinstance(snak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if isinstance(value, str):
            values.add(value.lower())
    return values


def lookup_band_by_id(
    qid: str,
    musicbrainz_id: str,
    *,
    name: str = "",
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """The band Wikidata item ``qid`` is, as {"id", "name", "englishName"}.

    For a band reached from a MusicBrainz artist (its url relationship to
    Wikidata), not by searching a name, so there is no name to agree with:
    the rule instead is that the item names that same MusicBrainz artist back
    (P434). An item that does not is a stale or wrong link, and is not kept.
    ``name`` only picks the language the label is read in. Raises when
    Wikidata cannot be reached."""
    if not _QID_RE.match(qid or "") or not musicbrainz_id or cancelled():
        return None
    fetch_json = fetch_json or _fetch_json
    lang = search_language(name) if name else "en"
    answer = fetch_json(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "claims|labels",
            "languages": "|".join(_languages(lang)),
            "languagefallback": "1",
        }
    )
    entities = answer.get("entities") if isinstance(answer, dict) else None
    entity = entities.get(qid) if isinstance(entities, dict) else None
    if not isinstance(entity, dict):
        return None
    if musicbrainz_id.lower() not in _claim_values(entity, MUSICBRAINZ_ARTIST_ID):
        return None
    return clean_artist(
        {
            "id": entity.get("id"),
            "name": _display_name(entity, lang, name) or name,
            "englishName": _label(entity, "en"),
        }
    )


def find_band(
    artist_tag: Any,
    *,
    fetch_json: FetchJson | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, str] | None:
    """lookup_band for an artist tag, featured artists left off. Never raises:
    any failure is logged and gives None."""
    name = tagged_artist_name(artist_tag)
    if not name:
        return None
    try:
        return lookup_band(name, fetch_json=fetch_json, cancelled=cancelled)
    except Exception:
        # Offline, a timeout, Wikidata down or answering something else. The
        # page tries again itself when the track is opened.
        logger.info("band lookup failed", exc_info=True)
        return None


class BandLookup:
    """One job's band lookup, in a thread of its own, so separation never
    waits for it.

    The thread never touches the job. It leaves its answer here, and finish()
    puts it on the job from the pipeline's own thread, once the pipeline has
    got that far without being cancelled. A job cancelled or failed while the
    lookup is out is therefore never written to, and an answer that arrives
    after the job has finished goes nowhere.
    """

    def __init__(self, job: Job, artist_tag: str) -> None:
        self._result: dict[str, str] | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(job, artist_tag),
            name=f"artist-{job.id}",
            daemon=True,
        )

    def _run(self, job: Job, artist_tag: str) -> None:
        # find_band never raises, so there is nothing for the thread to lose.
        self._result = find_band(artist_tag, cancelled=lambda: job.cancel_requested)

    @classmethod
    def start(cls, job: Job) -> BandLookup | None:
        """Start looking, or None when there is nothing to look for: no artist
        tag, or a band already known (a re-split inherits its source's)."""
        tags = job.audio_tags or {}
        if job.artist or not tagged_artist_name(tags.get("artist")):
            return None
        lookup = cls(job, tags["artist"])
        lookup._thread.start()
        return lookup

    def finish(self, job: Job, timeout: float) -> None:
        """Wait up to ``timeout`` seconds for the answer, and keep it on the
        job if there is one and the job was not cancelled meanwhile."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.info("[%s] band lookup still out after %ss; finishing without", job.id, timeout)
            return
        if job.cancel_requested or self._result is None:
            return
        _set(job, artist=self._result)
