"""What a source says about itself: artist, title, album, and lyrics (#699).

Read once at import and kept on the job as ``audio_tags``, for the Lyrics tab
and the artist box. The library otherwise records only a title, and a title
names the song as often as the band, so a file's own tags are the best answer
there is to "who is this and what are the words".

Two sources, one shape:

* an upload's container tags, read with ffprobe before the upload is deleted
  (the stems carry no tags, and the source goes once the pipeline is done);
* a link's metadata from yt-dlp, which fills artist, track and album for
  releases YouTube knows as music, and otherwise the channel, when the video
  itself corroborates that the channel is the artist's.

Everything here is optional. A probe that fails, times out or finds nothing
gives ``None`` and the import goes ahead exactly as before.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from itertools import groupby
from pathlib import Path
from typing import Any

from app.core.config import (
    AUDIO_TAG_LEGACY_CODEPAGES,
    AUDIO_TAG_LYRICS_MAX_CHARS,
    AUDIO_TAG_MAX_CHARS,
    AUDIO_TAG_MOJIBAKE_MIN_WORD,
    TIMEOUT_PROBE_TAGS,
    ffprobe_executable,
)

logger = logging.getLogger(__name__)

# ffprobe reports an MP3's lyrics frame as "lyrics-<language>" ("lyrics-eng"),
# a FLAC's as "LYRICS" or "UNSYNCEDLYRICS", an M4A's as "lyrics". Keys are
# compared lower-cased.
_LYRICS_KEYS = ("lyrics", "unsyncedlyrics", "syncedlyrics")


def _is_control(ch: str) -> bool:
    """C0 and C1 control characters, and lone surrogates (raw bytes that
    were not UTF-8, which nothing downstream could encode)."""
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF


def _clean(value: Any, limit: int) -> str:
    """A tag value as plain text: control characters other than line breaks
    dropped, trimmed, and cut at ``limit``. Tags are written by anyone, and
    this text ends up in the page."""
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch in "\n\t" or not _is_control(ch))
    return text.replace("\r\n", "\n").strip()[:limit]


def _first(tags: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = _clean(tags.get(key), AUDIO_TAG_MAX_CHARS)
        if value:
            return value
    return ""


# ── Tags written in a Windows codepage ──
#
# An MP3's ID3v1 tag has no encoding at all, and an ID3v2 frame marked
# ISO-8859-1 is as often filled with whatever the tagger's Windows used:
# cp1250 across Central Europe, cp1251 for Cyrillic. Read as Latin-1, as
# ffprobe does, "zapomniałem" comes out "zapomnia³em", "Żuraw" "¯uraw", and
# "Кино" "Êèíî". A tag like that finds nothing on MusicBrainz or LRCLIB and
# looks broken on the page.
#
# Re-reading is only ever a choice between readings of the same bytes, and
# only taken when the Latin-1 one is implausible and a codepage's is clearly
# less so. Correct Latin-1 ("Björk", "Sigur Rós", "Mañana", "Ágætis byrjun")
# scores zero and is never touched.

# Beside a letter these are ordinary punctuation, in Latin-1 and in every
# codepage tried: an apostrophe, quotes, dashes.
_BESIDE_LETTER_OK = frozenset(
    "\N{NO-BREAK SPACE}\N{SOFT HYPHEN}\N{COPYRIGHT SIGN}\N{REGISTERED SIGN}"
    "\N{DEGREE SIGN}\N{MIDDLE DOT}\N{ACUTE ACCENT}\N{EURO SIGN}\N{TRADE MARK SIGN}"
    "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}"
    "\N{SINGLE LEFT-POINTING ANGLE QUOTATION MARK}\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
    "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}\N{SINGLE LOW-9 QUOTATION MARK}"
    "\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}\N{DOUBLE LOW-9 QUOTATION MARK}"
    "\N{EN DASH}\N{EM DASH}\N{HORIZONTAL ELLIPSIS}\N{BULLET}"
)
# Spanish opens a question or exclamation with these, right before a letter.
_OPENING_MARKS = frozenset("\N{INVERTED QUESTION MARK}\N{INVERTED EXCLAMATION MARK}")


def _script(ch: str) -> str:
    code = ord(ch)
    if 0x0400 <= code <= 0x052F:
        return "cyrillic"
    if code < 0x0250 or 0x1E00 <= code <= 0x1EFF:
        return "latin"
    return "other"


def _implausibility(text: str) -> int:
    """How much ``text`` looks like bytes read in the wrong codepage. Zero for
    anything a person would have typed.

    Three signs, each worth the same:

    * a C1 control character, which no one types, and which is what Latin-1
      makes of cp1250's "ś", "ź", "Š", "ž" or cp1252's quotes;
    * a symbol against a letter, "zapomnia³em", "¯uraw", "mo¿e" (cp1250's
      "ł", "Ż", "ż"), except punctuation that belongs there;
    * a word that mixes Latin and Cyrillic letters, or is at least
      AUDIO_TAG_MOJIBAKE_MIN_WORD letters long without a single one from
      ASCII, "Êèíî", "Ãðóïïà" (cp1251's "Кино", "Группа").
    """
    score = 0
    last = len(text) - 1
    for i, ch in enumerate(text):
        if 0x80 <= ord(ch) <= 0x9F:
            score += 1
            continue
        if ch.isascii() or ch.isalpha() or ch.isspace() or ch in _BESIDE_LETTER_OK:
            continue
        before = i > 0 and text[i - 1].isalpha()
        after = i < last and text[i + 1].isalpha()
        if ch in _OPENING_MARKS and not before:
            continue
        if before or after:
            score += 1
    for is_letter, run in groupby(text, str.isalpha):
        if not is_letter:
            continue
        word = "".join(run)
        scripts = {_script(ch) for ch in word}
        mixed = len(scripts) > 1
        all_accents = (
            scripts == {"latin"}
            and len(word) >= AUDIO_TAG_MOJIBAKE_MIN_WORD
            and not any(ch.isascii() for ch in word)
        )
        if mixed or all_accents:
            score += 1
    return score


def _tagger_bytes(value: str) -> tuple[bytes, bool] | None:
    """The bytes the tagger wrote, when ``value`` could be a misreading of
    them, and whether they came straight from the file.

    ffprobe passes a field it cannot read as UTF-8 through as raw bytes (an
    ID3v1 tag's, see probe_tags), which arrive as lone surrogates. Anything
    else outside ASCII that fits Latin-1 may have been decoded as Latin-1.
    Text beyond Latin-1 came from a Unicode frame and is what it says.
    """
    if value.isascii():
        return None
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        try:
            return value.encode("utf-8", "surrogateescape"), True
        except UnicodeEncodeError:
            return None
    try:
        return value.encode("latin-1"), False
    except UnicodeEncodeError:
        return None


def _as_utf8(raw: bytes, from_file: bool) -> str | None:
    """``raw`` read as UTF-8 ("Å¼" is "ż", "Ã©" is "é"), when it is.

    Strict, so it is only ever taken when every byte agrees, which Latin-1
    text practically never does by accident. Bytes straight from an ID3v1
    field may end in half a character, since the field is cut at 30 bytes.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if not from_file or exc.reason != "unexpected end of data":
            return None
        text = raw[: exc.start].decode("utf-8", "strict")
    if text.isascii() or any(_is_control(ch) for ch in text if ch not in "\n\r\t"):
        return None
    return text


def _repair_encoding(tags: dict[str, str]) -> dict[str, str]:
    """``tags`` with values misread from UTF-8 or a Windows codepage read again.

    UTF-8 is decided value by value, since it checks itself. A codepage is
    decided for the file as a whole: one tagger wrote every field, so a title
    that gives the codepage away ("Zapomnia³em") settles an artist that
    cannot on its own ("Piêæ" for "Pięć"). The codepage must make the file's
    fields, together, strictly more plausible than Latin-1 does; two
    codepages that are equally good and disagree settle nothing. And it is
    only applied to a field it makes no less plausible.
    """
    repaired = dict(tags)
    legacy: dict[str, tuple[bytes, int]] = {}
    for key, value in tags.items():
        found = _tagger_bytes(value)
        if found is None:
            continue
        raw, from_file = found
        utf8 = _as_utf8(raw, from_file)
        if utf8 is not None:
            repaired[key] = utf8
            continue
        # What a reader that follows the ID3 spec would have shown.
        latin = raw.decode("latin-1")
        repaired[key] = latin
        legacy[key] = (raw, _implausibility(latin))
    before = sum(score for _, score in legacy.values())
    if not before:
        return repaired
    readings: list[tuple[int, dict[str, tuple[str, int]]]] = []
    for codepage in AUDIO_TAG_LEGACY_CODEPAGES:
        try:
            decoded = {key: raw.decode(codepage) for key, (raw, _) in legacy.items()}
        except UnicodeDecodeError:
            # A byte the codepage has no character for: not this codepage.
            continue
        scored = {key: (text, _implausibility(text)) for key, text in decoded.items()}
        readings.append((sum(score for _, score in scored.values()), scored))
    if not readings:
        return repaired
    best_score, best = min(readings, key=lambda reading: reading[0])
    if best_score >= before:
        return repaired
    texts = {key: text for key, (text, _) in best.items()}
    for score, scored in readings:
        if score == best_score and {key: text for key, (text, _) in scored.items()} != texts:
            logger.info("tags could be read in more than one codepage; left as they are")
            return repaired
    for key, (text, score) in best.items():
        if score <= legacy[key][1]:
            repaired[key] = text
    return repaired


def tags_from_probe(raw: dict[str, Any] | None) -> dict[str, str] | None:
    """Artist, title, album and lyrics from ffprobe's ``format.tags``, or None
    when the file names none of them."""
    if not isinstance(raw, dict):
        return None
    tags = {str(k).lower(): v for k, v in raw.items() if isinstance(v, str)}
    tags = _repair_encoding(tags)
    lyrics = ""
    for key, value in tags.items():
        if key in _LYRICS_KEYS or key.startswith("lyrics-") or key.startswith("lyrics "):
            lyrics = _clean(value, AUDIO_TAG_LYRICS_MAX_CHARS)
            if lyrics:
                break
    found = {
        "artist": _first(tags, "artist", "album_artist", "albumartist", "performer"),
        "title": _first(tags, "title"),
        "album": _first(tags, "album"),
        "lyrics": lyrics,
    }
    found = {k: v for k, v in found.items() if v}
    return found or None


# YouTube Music's auto-generated channels are named "<Artist> - Topic", and
# only ever carry that artist's releases.
_TOPIC_SUFFIX = " - Topic"
# "Artist - Title", with the separators people actually type: a hyphen, an en
# or em dash or a pipe with space either side, a colon followed by one, and the
# ones East Asian titles use: "_", "/" or "~" after a space, their full-width
# forms with or without one ("YOASOBI／群青"), or a dash touching the title
# ("周杰倫 -晴天"). A title
# that goes straight on to the song in marks counts too:
# "周杰倫 Jay Chou【晴天 Sunny Day】", "YOASOBI「夜に駆ける」".
_TITLE_SEPARATOR_RE = re.compile(
    r"\s+[-\N{EN DASH}\N{EM DASH}|_/~]\s*|\s*:\s+"
    r"|\s*[\N{FULLWIDTH SOLIDUS}\N{FULLWIDTH VERTICAL LINE}\N{FULLWIDTH HYPHEN-MINUS}"
    r"\N{FULLWIDTH COLON}]\s*"
    r"|\s*(?=[\N{LEFT BLACK LENTICULAR BRACKET}\N{LEFT CORNER BRACKET}"
    r"\N{LEFT WHITE CORNER BRACKET}\N{LEFT DOUBLE ANGLE BRACKET}\N{LEFT ANGLE BRACKET}"
    r"\N{LEFT WHITE LENTICULAR BRACKET}])"
)
# What an artist's own channel tends to append to their name: "NirvanaVEVO",
# "Nirvana Official", "King Gnu official YouTube channel", "公式チャンネル"
# (official channel), "官方頻道". Compared on the squashed key, so spacing is
# irrelevant.
_CHANNEL_SUFFIXES = (
    "vevo",
    "official",
    "channel",
    "youtube",
    "officiel",
    "oficial",
    "チャンネル",
    "公式",
    "頻道",
    "频道",
    "官方",
)


def _name_key(text: str) -> str:
    """A name reduced for comparison: no case, accents, punctuation or
    spacing, and traditional Chinese as simplified (title_parse.name_key), so
    a title's "Dawid Podsiadlo" is the channel's "Dawid Podsiadło"."""
    # Imported here: title_parse reaches the network layer's key function
    # lazily, and this module stays light to import.
    from app.pipeline.title_parse import name_key

    return name_key(text)


def _channel_keys(channel: str) -> list[str]:
    """The channel's key, then each shorter one its suffixes come off to:
    "Roxy Music" stays "roxymusic" too, however many suffixes there are."""
    key = _name_key(channel)
    keys = [key]
    stripped = True
    while stripped:
        stripped = False
        for suffix in _CHANNEL_SUFFIXES:
            suffix_key = _name_key(suffix)
            if key.endswith(suffix_key) and key != suffix_key:
                key = key.removesuffix(suffix_key)
                keys.append(key)
                stripped = True
    return keys


def _artist_from_channel(info: dict[str, Any]) -> tuple[str, str] | None:
    """(artist, title) from the channel, only when something corroborates it.

    A bare channel name is not evidence: labels, compilers and fans upload
    other people's songs all the time. It counts when the channel is YouTube's
    auto-generated "Artist - Topic", or when the video's own title starts with
    the channel's name ("Nirvana - Lithium (Official Music Video)" on the
    NirvanaVEVO channel). The title keeps whatever noise follows; the page
    cleans that up.
    """
    video_title = _clean(info.get("title"), AUDIO_TAG_MAX_CHARS)
    channels: list[str] = []
    for field in ("channel", "uploader"):
        name = _clean(info.get(field), AUDIO_TAG_MAX_CHARS)
        if name and name not in channels:
            channels.append(name)
    for channel in channels:
        if channel.endswith(_TOPIC_SUFFIX):
            artist = channel.removesuffix(_TOPIC_SUFFIX).strip()
            if artist:
                return artist, video_title
            continue
        keys = [k for k in _channel_keys(channel) if k]
        if not keys:
            continue
        for match in _TITLE_SEPARATOR_RE.finditer(video_title):
            prefix = video_title[: match.start()].strip()
            if prefix and _name_key(prefix) in keys:
                rest = video_title[match.end() :].strip()
                if rest:
                    # The channel's own spelling when it is the name itself
                    # (a title typed without its accents), else the title's
                    # ("Nirvana", not "NirvanaVEVO").
                    return (channel if _name_key(prefix) == keys[0] else prefix), rest
                break
    return None


def tags_from_ytdlp(info: dict[str, Any] | None) -> dict[str, str] | None:
    """Artist, track and album from yt-dlp's info, or None.

    The music fields first, and they always win. Only when they name no
    artist does the channel count, and then only when corroborated (see
    _artist_from_channel): the channel alone is as often a label ("Roadrunner
    Records") or a fan's upload, and a wrong artist is worse than none because
    it gets searched for.
    """
    if not isinstance(info, dict):
        return None
    artists = info.get("artists")
    artist = _clean(info.get("artist"), AUDIO_TAG_MAX_CHARS) or (
        _clean(artists[0], AUDIO_TAG_MAX_CHARS) if isinstance(artists, list) and artists else ""
    )
    title = _clean(info.get("track"), AUDIO_TAG_MAX_CHARS)
    if not artist and (corroborated := _artist_from_channel(info)) is not None:
        artist = corroborated[0]
        title = title or corroborated[1]
    found = {
        "artist": artist,
        "title": title,
        "album": _clean(info.get("album"), AUDIO_TAG_MAX_CHARS),
    }
    found = {k: v for k, v in found.items() if v}
    # An album alone says nothing useful about who or what this is.
    if not found.get("artist") and not found.get("title"):
        return None
    return found


def probe_tags(path: Path) -> dict[str, str] | None:
    """Read an upload's tags. Never raises: tags are not worth failing an
    upload over, so every failure is logged and gives None."""
    try:
        result = subprocess.run(
            [
                ffprobe_executable(),
                "-v",
                "quiet",
                "-print_format",
                # A field that is not UTF-8 (an ID3v1 tag, in whatever
                # codepage its tagger used) as its bytes, rather than
                # replacement characters: _repair_encoding reads them again.
                "json=string_validation=ignore",
                "-show_entries",
                "format_tags",
                str(path),
            ],
            capture_output=True,
            # The same reason as _probe_duration in api/jobs.py: tags are
            # arbitrary text, and the Windows locale codec would choke on it.
            # Bytes that are not UTF-8 come through as lone surrogates.
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            timeout=TIMEOUT_PROBE_TAGS,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not read tags from %s", path.name, exc_info=True)
        return None
    if result.returncode != 0:
        logger.info("ffprobe found no readable tags in %s", path.name)
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        logger.info("ffprobe tag output for %s was not JSON", path.name)
        return None
    fmt = data.get("format") if isinstance(data, dict) else None
    return tags_from_probe(fmt.get("tags") if isinstance(fmt, dict) else None)
