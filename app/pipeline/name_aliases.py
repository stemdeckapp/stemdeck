"""The other names an artist goes by, and names reduced for comparing.

LRCLIB files a song under whatever name its uploader used: 周杰倫, 周杰伦 or
Jay Chou; 아이유 or IU; 宇多田ヒカル or Hikaru Utada. A track known by one of
them has its lyrics under another just as often, and the lyrics lookup keeps
a version only when its artist is one the track is known by
(lyrics_lookup.belongs_to). So the names are taken from where they are
recorded as one artist's: the MusicBrainz artist the track was identified as
(its aliases) and the Wikidata item of its band (its labels and aliases).

Never guessed from the names themselves: no transliteration, no romanisation.
Two names count as one artist's only when one of those sources says so. What
each source says is taken with care, since both also list what is only
loosely a name:

* MusicBrainz: the artist's name, its primary alias in each language, and
  its other aliases that are distinctive (see ``distinctive``). Never a
  search hint (misspellings, mojibake) nor a legal name that is not primary
  (the name on a passport, which other people share).
* Wikidata: every label in the languages StemDeck speaks (and the Chinese
  variants), and the aliases that are distinctive.

MusicBrainz answers are kept in its cache (musicbrainz.py) with the same
MBID, one request for the artist and its links both, and take their turn from
the shared rate limiter. Wikidata answers are kept in memory for the process.
What is sent: an MBID, or a Wikidata item id. Nothing here raises: a source
that cannot be reached gives no names.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

from app.core.models import MBID_RE
from app.pipeline import artist_lookup, musicbrainz
from app.pipeline.artist_lookup import _QID_RE, artist_name_key
from app.pipeline.zh_variants import to_simplified

logger = logging.getLogger("stemdeck.lyrics")

# How many of a recording's credited artists are asked about: a duet's two.
_MAX_ARTISTS = 2
# A name that is not the artist's own or primary counts only when it weighs
# at least this much (name_weight) and, written only in Latin or another
# script with spaces, is at least two words: "Jay", "Hikki" and "VI" name too
# many people, "Jay Chou" and 周杰伦 do not.
_MIN_WEIGHT = 4
_MIN_WORDS = 2
# Wikidata labels and aliases are read in these languages: the ten StemDeck
# speaks, and the Chinese variants and Cantonese, whose names differ.
_WIKIDATA_LANGUAGES = "en|pl|ja|ko|zh|zh-hans|zh-hant|zh-cn|zh-tw|zh-hk|yue|de|fr|es|pt|pt-br|id"
_WIKIDATA_KEEP_SEC = 24 * 3600
_WIKIDATA_KEEP_MAX = 256
_NAME_MAX_CHARS = 300


# ── names reduced for comparing ──


# Latin letters Unicode does not build from a base letter and an accent, as a
# name typed without them has them: "Podsiadlo" for Podsiadło, "Grossstadt"
# for Großstadt. The accents that are marks go in artist_name_key.
_LATIN_LETTERS = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ð": "d",
        "Ð": "D",
        "ß": "ss",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "ı": "i",
        "þ": "th",
        "Þ": "TH",
    }
)


def fold(text: Any) -> str:
    """``text`` as it is compared: compatibility forms unified (full-width
    Latin, half-width kana, the ideographs Unicode keeps twice), traditional
    Chinese folded to simplified (zh_variants.py), and the Latin letters
    above written plain. fold() in static/js/lyricsLookup.js is its twin."""
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_LATIN_LETTERS)
    return to_simplified(text)


def name_key(name: Any) -> str:
    """artist_name_key of the folded name: 周杰倫 and 周杰伦 are one key."""
    return artist_name_key(fold(name))


def _han(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or (0x20000 <= cp <= 0x3FFFF)
    )


def _cjk(ch: str) -> bool:
    """A Chinese, Japanese or Korean letter: an ideograph, kana or Hangul."""
    cp = ord(ch)
    return (
        _han(ch)
        or 0x3040 <= cp <= 0x30FF
        or 0x31F0 <= cp <= 0x31FF
        or 0xFF66 <= cp <= 0xFF9F
        or 0x1100 <= cp <= 0x11FF
        or 0x3130 <= cp <= 0x318F
        or 0xAC00 <= cp <= 0xD7AF
    )


def name_weight(key: str) -> int:
    """How much of a name ``key`` (name_key) is, in Latin letters: an
    ideograph counts two, since a Chinese or Japanese name of two or three of
    them is a whole name where two or three Latin letters are an initial or a
    stray "DJ". Hangul counts by its letters, which a key has already split
    each syllable into; marks count nothing."""
    return sum(2 if _han(ch) else 0 if unicodedata.category(ch)[0] == "M" else 1 for ch in key)


def has_cjk(text: str) -> bool:
    return any(_cjk(ch) for ch in text)


def distinctive(name: Any) -> bool:
    """Whether a name says who it is without its source's word for it: long
    enough (name_weight), and more than one word when it is written in a
    script with spaces."""
    text = str(name or "").strip()
    if name_weight(name_key(text)) < _MIN_WEIGHT:
        return False
    return has_cjk(text) or len(text.split()) >= _MIN_WORDS


# ── what each source says ──


def _add(names: list[str], name: Any) -> None:
    if isinstance(name, str) and name.strip() and name.strip() not in names:
        names.append(name.strip()[:_NAME_MAX_CHARS])


def musicbrainz_names(artist: Any) -> list[str]:
    """The names a MusicBrainz artist (with ``inc=aliases``) goes by, best
    first: its own, its primary aliases, then the other distinctive ones."""
    if not isinstance(artist, dict):
        return []
    names: list[str] = []
    _add(names, artist.get("name"))
    aliases = [a for a in artist.get("aliases") or [] if isinstance(a, dict)]
    usable = [a for a in aliases if a.get("type") != "Search hint"]
    for alias in usable:
        if alias.get("primary") is True:
            _add(names, alias.get("name"))
    for alias in usable:
        if alias.get("type") != "Legal name" and distinctive(alias.get("name")):
            _add(names, alias.get("name"))
    return names


def wikidata_names(entity: Any) -> list[str]:
    """The names a Wikidata item goes by: its labels, then its distinctive
    aliases."""
    if not isinstance(entity, dict):
        return []
    names: list[str] = []
    labels = entity.get("labels")
    for label in labels.values() if isinstance(labels, dict) else []:
        if isinstance(label, dict):
            _add(names, label.get("value"))
    aliases = entity.get("aliases")
    for group in aliases.values() if isinstance(aliases, dict) else []:
        for alias in group if isinstance(group, list) else []:
            if isinstance(alias, dict) and distinctive(alias.get("value")):
                _add(names, alias.get("value"))
    return names


def _musicbrainz_artist(mbid: str, *, cancelled: Callable[[], bool] = lambda: False) -> Any:
    """The artist with its aliases and url relationships, from the cache when
    it holds them. The answer replaces a cached one without aliases, which
    artist_wikidata_id reads the same way. Raises when MusicBrainz cannot be
    reached."""
    kept = musicbrainz.cache_get("artist", mbid)
    if isinstance(kept, dict) and "aliases" in kept:
        return kept
    # Looked up on the module, so the tests' stand-in for the network holds.
    data = musicbrainz._fetch_json(
        f"artist/{mbid}", {"inc": "aliases+url-rels"}, cancelled=cancelled
    )
    if isinstance(data, dict):
        musicbrainz.cache_put("artist", mbid, data)
    return data


_wikidata_kept: dict[str, tuple[float, list[str]]] = {}
_wikidata_lock = threading.Lock()


def _wikidata_item_names(qid: str) -> list[str]:
    """wikidata_names for item ``qid``, kept for a day. Raises when Wikidata
    cannot be reached."""
    with _wikidata_lock:
        kept = _wikidata_kept.get(qid)
    if kept and time.time() - kept[0] < _WIKIDATA_KEEP_SEC:
        return kept[1]
    answer = artist_lookup._fetch_json(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|aliases",
            "languages": _WIKIDATA_LANGUAGES,
        }
    )
    entities = answer.get("entities") if isinstance(answer, dict) else None
    names = wikidata_names(entities.get(qid) if isinstance(entities, dict) else None)
    with _wikidata_lock:
        if len(_wikidata_kept) >= _WIKIDATA_KEEP_MAX:
            _wikidata_kept.clear()
        _wikidata_kept[qid] = (time.time(), names)
    return names


def artist_aliases(
    mbids: Iterable[str],
    qid: str = "",
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> list[str]:
    """Every name the artist is known by: the MusicBrainz artists ``mbids``
    (a recording's credit) and the Wikidata item ``qid`` (its band), best
    first, each once. [] when neither is known or reachable. Never raises."""
    names: list[str] = []
    for mbid in [m for m in mbids if isinstance(m, str) and MBID_RE.match(m)][:_MAX_ARTISTS]:
        if cancelled():
            return names
        try:
            for name in musicbrainz_names(_musicbrainz_artist(mbid, cancelled=cancelled)):
                _add(names, name)
        except Exception:
            logger.info("artist aliases from MusicBrainz failed", exc_info=True)
    if isinstance(qid, str) and _QID_RE.match(qid) and not cancelled():
        try:
            for name in _wikidata_item_names(qid):
                _add(names, name)
        except Exception:
            logger.info("artist names from Wikidata failed", exc_info=True)
    return names
