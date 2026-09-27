"""What a track's title says it is, when nothing else does.

A YouTube upload of a cast recording or a film song usually carries no music
metadata at all: yt-dlp's artist, track and album are empty, and the channel
("WickedVEVO", "Atlantic Records", a fan's) names no artist. The title is all
there is, and titles follow a handful of patterns:

* "Artist - Song", and as often "Song - Artist";
* 'Song (From "Work" Original Broadway Cast Recording/2003)', "Song (from Work)";
* "Song - Work The Musical", "Song - Work Soundtrack", "Song | Work", "Work: Song";
* "Song   Name, Name": a performer list after a run of spaces;
* "Cast of Work", "Work Cast", "Work Original Broadway Cast" as the artist;

wrapped in video noise: "(Official Audio)", "[Lyric Video]", "HD", "4K",
"Remastered", a year after a slash.

parse_title reads a title every way it can be read, most likely first, as
TitleReading: the song, who performs it, and the work (musical, film, series)
it is from, when the title names one. Which reading is right is decided
elsewhere (musicbrainz.search_by_title, lyrics_lookup.identify_on_lrclib), by
whether a recording of that song, that length, credits those names. This
module is pure: no requests, no state.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# At most this many readings of one title are offered: each one costs a
# MusicBrainz search at one request a second.
MAX_READINGS = 3
# A title longer than this is not one.
_TITLE_MAX_CHARS = 300

# Hyphen, figure dash, en dash, em dash, horizontal bar. Built from code
# points so no dash character has to be typed here.
_DASHES = "-" + "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015))
# Double quotes of every kind. Single quotes are left alone: they are
# apostrophes as often as not ("Don't Stop Me Now").
_QUOTES = '"' + "".join(chr(c) for c in (0x201C, 0x201D, 0x201E, 0x201F, 0x00AB, 0x00BB))
# Stands for a run of two or more spaces, which some uploads put between the
# song and a performer list, so it survives the bracket removal that leaves
# spaces of its own. A control character: never in a real title.
_GAP = chr(0x1F)

_BRACKET_RE = re.compile(r"[(\[{]([^()\[\]{}]*)[)\]}]")
_GAP_RE = re.compile("[ \t" + chr(0xA0) + "]{2,}")
_SPACES_RE = re.compile(r"\s+")
# Between artist and song: a dash with space either side, or a pipe.
_DASH_SPLIT_RE = re.compile(rf"\s+[{re.escape(_DASHES)}]+\s+")
_PIPE_SPLIT_RE = re.compile(r"\s*\|\s*")
_COLON_SPLIT_RE = re.compile(r"\s*:\s+")
_GAP_SPLIT_RE = re.compile(rf"\s*{_GAP}\s*")

_FROM_RE = re.compile(r"^\s*from\s+(.+)$", re.IGNORECASE)
_QUOTED_RE = re.compile(rf"[{_QUOTES}]([^{_QUOTES}]+)[{_QUOTES}]")
_FEAT_RE = re.compile(r"^\s*(?:ft\.?|feat\.?|featuring|with)\s", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# Where a work's name ends inside 'from Work Original Broadway Cast ...'.
_QUALIFIER_START_RE = re.compile(
    rf"\s*/|\s+[{re.escape(_DASHES)}]\s+|\s*:\s|\s+(?=(?:the\s+)?(?:original|official|motion\s+picture"
    r"|soundtrack|o\.?s\.?t\b|broadway|west\s+end|musical|movie|film|cast|score|television|tv\b"
    r"|series|version|edition|deluxe)\b)",
    re.IGNORECASE,
)

# Video noise, bracketed or trailing. Anything bracketed that is not a work,
# a kind or a featured artist is dropped from the song anyway; these are the
# words that also go when they trail the title unbracketed.
_TRAILING_NOISE_RE = re.compile(
    r"(?:^|\s+)(?:official\s+(?:music\s+)?(?:video|audio|lyric\s+video|visuali[sz]er)"
    r"|(?:lyric|lyrics|music)\s+video|lyrics|official\s+audio|audio|hd|hq|4k|8k"
    r"|remaster(?:ed)?(?:\s+\d{4})?)\s*$",
    re.IGNORECASE,
)
_YEAR_SUFFIX_RE = re.compile(r"\s*/\s*(?:19|20)\d{2}\s*$")

# What an album, a bracket or an artist says the work is. A film's words
# first: an "Original Motion Picture Cast Recording" is a film's.
_FILM_RE = re.compile(r"motion\s+picture|\bfilm\b|\bmovie\b", re.IGNORECASE)
_MUSICAL_RE = re.compile(
    r"broadway|west\s+end|\bmusical\b|\bstage\b|cast\s+recording|original\s+(?:\w+\s+)?cast",
    re.IGNORECASE,
)
_TV_RE = re.compile(r"television|\btv\b|\bseries\b", re.IGNORECASE)
_SOUNDTRACK_RE = re.compile(r"soundtrack|\bo\.?s\.?t\b|\bscore\b", re.IGNORECASE)

_CAST_WORDS = (
    r"(?:(?:original|broadway|london|west\s+end|motion\s+picture|movie|film|revival|studio)\s+)*"
)
# "Cast of Hamilton", "Original Broadway Cast of Hamilton".
_CAST_OF_RE = re.compile(
    rf"^(?:the\s+)?{_CAST_WORDS}cast\s+of\s+(?P<work>.+)$",
    re.IGNORECASE,
)
# "The Greatest Showman Cast", "Wicked Original Broadway Cast (Recording)".
_WORK_CAST_RE = re.compile(
    rf"^(?P<work>.+?)\s+{_CAST_WORDS}cast(?:\s+recording)?$",
    re.IGNORECASE,
)
# "Wicked The Musical", "Wicked: The Musical", "Wicked Musical".
_WORK_MUSICAL_RE = re.compile(
    rf"^(?P<work>.+?)\s*[:{re.escape(_DASHES)}]?\s+(?:the\s+)?musical$", re.IGNORECASE
)
# "Frozen Soundtrack", "Frozen OST", "Frozen Original Motion Picture Soundtrack".
_WORK_SOUNDTRACK_RE = re.compile(
    r"^(?P<work>.+?)\s*:?\s+(?:the\s+)?(?:original\s+)?(?:motion\s+picture\s+)?"
    r"(?:soundtrack|o\.?s\.?t\.?)$",
    re.IGNORECASE,
)
# The last name in a list: "Keala Settle & The Greatest Showman" is the show.
_LIST_SPLIT_RE = re.compile(r"\s*(?:&|,|\+|\band\b)\s*", re.IGNORECASE)

# Words that say nothing about which recording a title names: joins, and the
# qualifiers every cast recording and soundtrack shares. Left out of the words
# a candidate recording has to account for (extra_words).
_STOPWORDS = frozenset(
    re.split(
        r"\s+",
        "the a an of and feat ft featuring with vs x by from original broadway london west end"
        " cast recording motion picture soundtrack ost film movie musical ensemble revival"
        " version edition deluxe official",
    )
)


@dataclass(frozen=True)
class TitleReading:
    """One way to read a title: the song, who performs it, and the work it is
    from. ``artist`` and ``work`` may be empty, and are then unknown."""

    song: str
    artist: str = ""
    work: str = ""
    # "musical", "film" or "tv": what the title says the work is.
    kind: str | None = None
    # The year the title gives the recording ("/2003").
    year: int | None = None


# ── words ──


def words(text: Any) -> frozenset[str]:
    """The words of ``text`` that tell one recording from another: no case,
    no Latin accents, no apostrophes or punctuation, and none of _STOPWORDS."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    norm = "".join(ch for ch in norm if not 0x300 <= ord(ch) <= 0x36F).casefold()
    norm = norm.replace("'", "").replace(chr(0x2019), "")
    return frozenset(w for w in re.split(r"[\W_]+", norm) if w and w not in _STOPWORDS)


def extra_words(reading: TitleReading) -> frozenset[str]:
    """The words beyond the song a recording must account for: the artist's
    and the work's."""
    return words(reading.artist) | words(reading.work)


def coverage(reading: TitleReading, *texts: Any) -> float:
    """How many of the reading's extra words ``texts`` contain, 0..1. Zero
    when the reading has none."""
    want = extra_words(reading)
    if not want:
        return 0.0
    have: set[str] = set()
    for text in texts:
        have |= words(text)
    return len(want & have) / len(want)


def song_key(text: Any) -> str:
    """A song's name reduced for comparison: brackets gone, then no case,
    accents, punctuation or spaces (artist_lookup.artist_name_key)."""
    # Imported here: artist_lookup pulls in the network layer, and this
    # module stays importable without it.
    from app.pipeline.artist_lookup import artist_name_key

    return artist_name_key(_BRACKET_RE.sub(" ", str(text or "")))


def resolvable(reading: TitleReading) -> bool:
    """Whether a reading names more than a song: a song alone can be anybody's."""
    return bool(reading.song) and bool(extra_words(reading))


# ── what a piece of the title says ──


def kind_of(text: Any) -> str | None:
    """The kind of work ``text`` says a recording is from, or None."""
    text = str(text or "")
    if _FILM_RE.search(text):
        return "film"
    if _MUSICAL_RE.search(text):
        return "musical"
    if _TV_RE.search(text):
        return "tv"
    if _SOUNDTRACK_RE.search(text):
        return "film"
    return None


def _year(text: str) -> int | None:
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def _unquote(text: str) -> str:
    return text.strip().strip(_QUOTES).strip()


def _from_work(text: str) -> tuple[str, str | None, int | None]:
    """(work, kind, year) from what follows "from" in a bracket."""
    quoted = _QUOTED_RE.search(text)
    if quoted:
        work = quoted.group(1).strip()
        rest = text[: quoted.start()] + " " + text[quoted.end() :]
    else:
        work = _QUALIFIER_START_RE.split(text, maxsplit=1)[0].strip()
        rest = text[len(work) :]
    if not words(work):
        work = ""
    return work, kind_of(rest), _year(rest)


def cast_work(artist: str) -> str:
    """The work an artist credit names as a cast ("The Greatest Showman Cast",
    "Cast of Hamilton"), or ""."""
    artist = artist.strip()
    match = _CAST_OF_RE.match(artist) or _WORK_CAST_RE.match(artist)
    if not match:
        return ""
    work = _LIST_SPLIT_RE.split(match.group("work"))[-1].strip()
    return work if words(work) else ""


def _named_work(part: str) -> tuple[str, str | None]:
    """(work, kind) when a whole part of the title is a work's name with its
    qualifier ("Wicked The Musical", "Frozen Soundtrack"), else ("", None)."""
    for pattern, kind in ((_WORK_MUSICAL_RE, "musical"), (_WORK_SOUNDTRACK_RE, "film")):
        match = pattern.match(part)
        if match and words(match.group("work")):
            return match.group("work").strip(" :" + _DASHES), kind
    return "", None


def _clean_part(part: str) -> str:
    part = _SPACES_RE.sub(" ", part).strip()
    for _ in range(4):
        before = part
        part = _YEAR_SUFFIX_RE.sub("", part)
        part = _TRAILING_NOISE_RE.sub("", part)
        part = _unquote(part).strip(" " + _DASHES + ":|")
        if part == before:
            break
    return part


# ── the readings ──


def parse_title(title: Any) -> list[TitleReading]:
    """Every reading of ``title``, most likely first, at most MAX_READINGS,
    each with a song. Empty when the title names nothing."""
    text = str(title or "")[:_TITLE_MAX_CHARS]
    text = "".join(ch if ch >= " " else " " for ch in text)
    text = _GAP_RE.sub(f" {_GAP} ", text)
    found = {"work": "", "kind": None, "year": None}

    def bracket(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        source = _FROM_RE.match(inner)
        if source:
            work, kind, year = _from_work(source.group(1))
            found["work"] = found["work"] or work
            found["kind"] = found["kind"] or kind
            found["year"] = found["year"] or year
        elif not _FEAT_RE.match(inner):
            kind = kind_of(inner)
            if kind:
                found["kind"] = found["kind"] or kind
                found["year"] = found["year"] or _year(inner)
        # Whatever it was, it is not part of the song's name.
        return " "

    for _ in range(3):
        stripped = _BRACKET_RE.sub(bracket, text)
        if stripped == text:
            break
        text = stripped
    kind: str | None = found["kind"]  # type: ignore[assignment]
    year: int | None = found["year"]  # type: ignore[assignment]
    bracket_work: str = found["work"]  # type: ignore[assignment]

    parts, separator = _split(text)
    if not parts:
        return []
    readings: list[TitleReading] = []

    def add(song: str, others: list[str]) -> None:
        song = _clean_part(song)
        if not song or not words(song) and not song_key(song):
            return
        artist_parts: list[str] = []
        work = bracket_work
        reading_kind = kind
        for other in others:
            named, named_kind = _named_work(other)
            if named:
                work = work or named
                reading_kind = reading_kind or named_kind
                continue
            artist_parts.append(other)
            work = work or cast_work(other)
        artist = ", ".join(artist_parts)
        # A title that says it is a cast recording or a soundtrack, and names
        # no work of its own, names it beside the song, if anywhere.
        if not work and reading_kind and artist_parts:
            work = artist_parts[-1]
        reading = TitleReading(song=song, artist=artist, work=work, kind=reading_kind, year=year)
        if reading not in readings:
            readings.append(reading)

    if len(parts) == 1:
        raw = _SPACES_RE.sub(" ", text.replace(_GAP, " ")).strip()
        quoted = _QUOTED_RE.search(raw)
        outside = _clean_part(raw[: quoted.start()] + " " + raw[quoted.end() :]) if quoted else ""
        if quoted and outside:
            # 'Keala Settle "This Is Me"'.
            add(quoted.group(1), [outside])
        else:
            add(parts[0], [])
    elif len(parts) == 2:
        first, second = parts
        if separator in ("gap", "pipe"):
            # "Song   Name, Name": the song, then who sings it. "Song | Work".
            add(first, [second])
            add(second, [first])
        elif cast_work(first) or _named_work(first)[0]:
            add(second, [first])
        elif cast_work(second) or _named_work(second)[0]:
            add(first, [second])
        else:
            add(second, [first])
            add(first, [second])
    else:
        # "Artist - Song - Work": the middle first, then either end.
        order = [1, 0, len(parts) - 1]
        for index in dict.fromkeys(order):
            add(parts[index], [p for i, p in enumerate(parts) if i != index])
    return readings[:MAX_READINGS]


def _split(text: str) -> tuple[list[str], str]:
    """The parts of a title between its separators, and which separator it
    was: "dash", "pipe", "colon", "gap" (a run of spaces) or "" for none.
    Dashes first, then pipes; else a colon ("Work: Song", unless it is "Work:
    The Musical"); else a run of spaces."""
    text = text.strip()
    flat = text.replace(_GAP, " ")
    for pattern, name in ((_DASH_SPLIT_RE, "dash"), (_PIPE_SPLIT_RE, "pipe")):
        parts = [p for p in (_clean_part(p) for p in pattern.split(flat)) if p]
        if len(parts) > 1:
            return parts, name
    single = _clean_part(flat)
    if not _named_work(single)[0]:
        parts = [
            p for p in (_clean_part(p) for p in _COLON_SPLIT_RE.split(single, maxsplit=1)) if p
        ]
        if len(parts) > 1:
            return parts, "colon"
    parts = [p for p in (_clean_part(p) for p in _GAP_SPLIT_RE.split(text)) if p]
    if len(parts) > 1:
        return [parts[0], ", ".join(parts[1:])], "gap"
    return ([single] if single else []), ""


def readings_for(tags: dict[str, str] | None, *titles: Any) -> list[TitleReading]:
    """The readings worth resolving for a track: its tags' artist and title
    first, when both are there, then each title's readings, those that name
    more than a song (resolvable), duplicates dropped."""
    tags = tags if isinstance(tags, dict) else {}
    artist = str(tags.get("artist") or "").strip()
    tag_title = str(tags.get("title") or "").strip()
    found: list[TitleReading] = []
    if artist and tag_title:
        for reading in parse_title(tag_title):
            if not reading.artist or song_key(reading.artist) == song_key(artist):
                found.append(
                    TitleReading(
                        song=reading.song,
                        artist=artist,
                        work=reading.work or cast_work(artist),
                        kind=reading.kind,
                        year=reading.year,
                    )
                )
                break
    for title in (tag_title, *titles):
        found += parse_title(title)
    out: list[TitleReading] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    for reading in found:
        key = (song_key(reading.song), extra_words(reading))
        if resolvable(reading) and key not in seen:
            seen.add(key)
            out.append(reading)
    return out[:MAX_READINGS]


def work_hint(title: Any, song: Any = None) -> TitleReading | None:
    """The reading of ``title`` that names a work: the one whose song is
    ``song`` when that is given (the song the track was identified as), else
    the first. None when the title names no work."""
    want = song_key(song) if song else ""
    for reading in parse_title(title):
        if not reading.work:
            continue
        if not want or song_key(reading.song) == want:
            return reading
    return None
