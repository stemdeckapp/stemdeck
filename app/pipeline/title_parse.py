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

Titles from East Asia follow patterns of their own, and are read as carefully:

* the song in marks rather than after a dash: "YOASOBI「夜に駆ける」",
  "周杰倫 Jay Chou【晴天 Sunny Day】", "米津玄師 MV「Lemon」", "BTS 'Spring Day'";
* one name in two scripts: "IU(아이유)", "周杰倫 Jay Chou", "晴天 Sunny Day",
  each kept as an alternative, since MusicBrainz may know either;
* "_", "/", "~" and full-width punctuation ("／", "｜", "（）") as separators;
* noise in any language and bracket: "【MV】", "[MV]", "官方完整版", "高音質",
  "歌詞", "뮤직비디오", "公式". Never inside a name: "Official髭男dism" is a band.

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

from app.pipeline.zh_variants import to_simplified

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
# Private-use characters standing for structure found in a title: between two
# names of one thing ("IU(아이유)"), and either side of a song in marks
# ("「Lemon」"). Not whitespace, which the control characters are to Python,
# and removed from the title first, so never confused with a real character.
_ALT = chr(0xE000)
_MARK_OPEN = chr(0xE001)
_MARK_CLOSE = chr(0xE002)
_PRIVATE_USE_RE = re.compile("[" + chr(0xE000) + "-" + chr(0xF8FF) + "]")
# The marks East Asian titles put around a song's name, opening to closing:
# corner brackets, white corner brackets, double angle brackets, angle
# brackets, black lenticular brackets, white lenticular brackets.
_SONG_MARKS = {chr(o): chr(o + 1) for o in (0x300C, 0x300E, 0x300A, 0x3008, 0x3010, 0x3016)}
# The lenticular ones hold video noise as often as a song ("【MV】", "【公式】").
_LENTICULAR = (chr(0x3010), chr(0x3016))
# Tortoise-shell and white square brackets, read as round ones.
_ROUND_ALIASES = {chr(0x3014): "(", chr(0x3015): ")", chr(0x3018): "(", chr(0x3019): ")"}
# Where a song mark was, once found: its name between the two.
_MARKED_RE = re.compile(f"{_MARK_OPEN}([^{_MARK_OPEN}{_MARK_CLOSE}]*){_MARK_CLOSE}")
# 'Song' in single quotes, K-pop's way ("NewJeans 'Ditto'"): only a pair
# opening after a space and closing before one, at least two characters
# apart, so "Don't", "Guns N' Roses" and "Rock 'n' Roll" stay as they are.
# Straight or curly.
_SQ_OPEN = "'" + chr(0x2018)
_SQ_CLOSE = "'" + chr(0x2019)
_SQ_ANY = _SQ_OPEN + chr(0x2019)
_SINGLE_QUOTED_RE = re.compile(
    rf"(?:^|(?<=[\s({_MARK_OPEN}]))[{_SQ_OPEN}]([^{_SQ_ANY}\s][^{_SQ_ANY}]*[^{_SQ_ANY}\s])"
    rf"[{_SQ_CLOSE}](?=$|[\s){_MARK_CLOSE},.!?])"
)
# A Japanese subtitle between wave dashes: "夜に駆ける ~Racing Into The Night~".
_WAVE_RE = re.compile(r"~(\S[^~]*\S|\S)~")
# A hashtag ("#music", "#shorts") says nothing about the song.
_HASHTAG_RE = re.compile(r"(?:^|(?<=\s))#\S+")

_BRACKET_RE = re.compile(r"[(\[{]([^()\[\]{}]*)[)\]}]")
_ROUND_RE = re.compile(r"[(\[]([^()\[\]{}]*)[)\]]")
_GAP_RE = re.compile("[ \t" + chr(0xA0) + "]{2,}")
_SPACES_RE = re.compile(r"\s+")
# Between artist and song: a dash with space on at least one side
# ("鄧麗君 -月亮代表我的心"), a lone "_", "/", "//" or "~" with space either
# side ("IU _ Palette"), or a pipe.
_DASH_SPLIT_RE = re.compile(
    rf"\s+[{re.escape(_DASHES)}]+\s*|\s*[{re.escape(_DASHES)}]+\s+|\s+(?:_+|/{{1,2}}|~)\s+"
)
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

# What a bracket holds when it only describes the upload, in the languages the
# app speaks and the ones its users' music comes in. A bracket is noise when
# all its words are these (or numbers), and at least one is a marker; the
# fillers alone ("(The Night)") are not. Compared case-folded.
_NOISE_MARKERS = frozenset(
    re.split(
        r"\s+",
        "official officiel officielle oficial offizielles offizielle offiziell resmi"
        " video videos vidéo vídeo videoclip clip clipe musikvideo mv m/v pv"
        " audio lyric lyrics letra letras paroles testo tekst lirik legendado legendada"
        " subtitles subtitle subtitled subbed subs sub hd hq uhd 4k 8k 1080p 720p 60fps"
        " hifi lossless remaster remastered ver ver. version teaser trailer visualizer"
        " visualiser karaoke live cover",
    )
)
_NOISE_FILLERS = frozenset(
    re.split(
        r"\s+",
        "the a of with and in on sing along music musik full short tv size color colour"
        " coded han rom eng english special performance by",
    )
)
# The same in Chinese, Japanese and Korean, which put no spaces between words,
# so they are found anywhere in a bracket, longest first: "官方完整版" is
# official, complete version; "高音質" high quality; "歌詞" lyrics; "字幕"
# subtitles; "公式" official; "뮤직비디오" music video; "가사" lyrics; and what a
# song is to a film or an anime ("主題歌" theme song, "オープニング" opening).
_CJK_NOISE = re.split(
    r"\s+",
    "官方完整版 官方高畫質 官方高画质 官方版 官方 完整版 高畫質 高画質 高画质 高音質"
    " 高音质 高清 動態歌詞 动态歌词 歌詞付き 歌詞付 附歌詞 中文字幕 韓中字 中字 字幕 歌詞"
    " 歌词 繁中 簡中 简中 公式 音樂錄影帶 音乐录影带 首播 無損 无损 純享版 纯享版 音源"
    " ミュージックビデオ フルバージョン 歌ってみた 弾いてみた 翻唱 뮤직비디오 뮤비 한글자막"
    " 자막 가사 공식 主題歌 主题歌 主題曲 主题曲 插曲 片尾曲 片頭曲 片头曲 オープニング"
    " エンディング 挿入歌 劇場版",
)
_CJK_NOISE_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(_CJK_NOISE, key=len, reverse=True))
)
_NOISE_SPLIT_RE = re.compile(r"[\s/&+,:;!?|\-" + chr(0x30FB) + chr(0xB7) + "]+")
# Unbracketed noise trimmed off either end of a part, besides
# _TRAILING_NOISE_RE's: a word from this list, or one made only of _CJK_NOISE
# ("林俊傑 JJ Lin 官方完整版 MV"). Never "Official" at the start: that is
# Official髭男dism's (and OFFICIAL HIGE DANDISM's) own name.
_TRAILING_NOISE_WORDS = frozenset(("mv", "m/v", "pv", "official", "officiel", "oficial", "teaser"))
_LEADING_NOISE_WORDS = frozenset(("mv", "m/v", "pv"))

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
    # Other names the title gives the song and the artist, in another script:
    # "晴天 Sunny Day", "IU(아이유)". MusicBrainz may know either.
    song_alts: tuple[str, ...] = ()
    artist_alts: tuple[str, ...] = ()


# ── words ──


# Latin letters NFKD leaves whole, folded as the accents are, so a title
# typed without Polish or Nordic letters ("Dawid Podsiadlo") still names
# "Dawid Podsiadło": l with stroke, o with stroke, d with stroke, dotless
# i, ae, oe, thorn, eth.
_LATIN_FOLD = str.maketrans(
    {
        chr(0x142): "l",
        chr(0xF8): "o",
        chr(0x111): "d",
        chr(0x131): "i",
        chr(0xE6): "ae",
        chr(0x153): "oe",
        chr(0xFE): "th",
        chr(0xF0): "d",
    }
)


def _words(text: Any, *, simplified: bool) -> frozenset[str]:
    norm = unicodedata.normalize("NFKD", str(text or ""))
    # Composed again once the Latin accents are gone, so kana keep their
    # voicing marks and Hangul its syllables: "が" is one letter, not two.
    norm = unicodedata.normalize(
        "NFC", "".join(ch for ch in norm if not 0x300 <= ord(ch) <= 0x36F)
    ).casefold()
    norm = norm.translate(_LATIN_FOLD).replace("'", "").replace(chr(0x2019), "")
    if simplified:
        norm = to_simplified(norm)
    return frozenset(w for w in re.split(r"[\W_]+", norm) if w and w not in _STOPWORDS)


def words(text: Any) -> frozenset[str]:
    """The words of ``text`` that tell one recording from another: no case,
    no Latin accents, no apostrophes or punctuation, traditional Chinese as
    simplified (鄧麗君 is 邓丽君), and none of _STOPWORDS."""
    return _words(text, simplified=True)


def extra_words(reading: TitleReading) -> frozenset[str]:
    """The words beyond the song a recording must account for: the artist's
    (in every name the title gives it) and the work's."""
    found = words(reading.artist) | words(reading.work)
    for name in reading.artist_alts:
        found |= words(name)
    return found


def search_words(reading: TitleReading) -> frozenset[str]:
    """extra_words as a search asks for them: each as written as well as
    folded, since a search engine knows 周杰倫 and 周杰伦 as two words."""
    found = extra_words(reading)
    for text in (reading.artist, reading.work, *reading.artist_alts):
        found |= _words(text, simplified=False)
    return found


def coverage(reading: TitleReading, *texts: Any) -> float:
    """How many of the reading's extra words ``texts`` contain, 0..1, for the
    best of the artist's names: "周杰倫 Jay Chou" is covered by a credit to
    周杰倫 alone. Zero when the reading has none."""
    have: set[str] = set()
    for text in texts:
        have |= words(text)
    best = 0.0
    for artist in (reading.artist, *reading.artist_alts):
        want = words(artist) | words(reading.work)
        if want:
            best = max(best, len(want & have) / len(want))
    return best


def name_key(text: Any) -> str:
    """A name reduced for comparison (artist_lookup.artist_name_key), with
    the Latin letters NFKD keeps whole folded too, and traditional Chinese
    as simplified (zh_variants.py): 周杰倫 and 周杰伦 are one name."""
    # Imported here: artist_lookup pulls in the network layer, and this
    # module stays importable without it.
    from app.pipeline.artist_lookup import artist_name_key

    return to_simplified(artist_name_key(str(text or "")).translate(_LATIN_FOLD))


def song_key(text: Any) -> str:
    """A song's name reduced for comparison: brackets gone, then no case,
    accents, punctuation or spaces (name_key)."""
    return name_key(_BRACKET_RE.sub(" ", str(text or "")))


def song_names(reading: TitleReading) -> list[str]:
    """Every name the reading gives its song, the first one first."""
    return [n for n in (reading.song, *reading.song_alts) if n]


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


def _noise_words(text: str) -> list[str]:
    """The words of ``text``, case-folded, once its East Asian noise is out."""
    rest = _CJK_NOISE_RE.sub(" ", text).casefold().replace("m/v", "mv")
    return [w for w in _NOISE_SPLIT_RE.split(rest) if w]


def _is_noise(text: str) -> bool:
    """Whether a bracket holding ``text`` only describes the upload: "MV",
    "Official Music Video", "官方完整版", "HD", not "Sunny Day"."""
    marked = bool(_CJK_NOISE_RE.search(text))
    for word in _noise_words(text):
        if word in _NOISE_MARKERS:
            marked = True
        elif word not in _NOISE_FILLERS and not word.isdigit():
            return False
    return marked


def _has_noise(text: str) -> bool:
    """Whether ``text`` says anything about the upload at all ("Japanese
    ver.", "MV Full")."""
    return bool(_CJK_NOISE_RE.search(text)) or any(w in _NOISE_MARKERS for w in _noise_words(text))


def _noise_token(token: str, allowed: frozenset[str]) -> bool:
    """Whether one unbracketed word is noise: in ``allowed``, or made only of
    _CJK_NOISE and at most one word of ``allowed`` ("官方完整版", "公式MV")."""
    if token.casefold() in allowed:
        return True
    rest = _CJK_NOISE_RE.sub("", token)
    return rest != token and (not rest or rest.casefold() in allowed)


def _clean_part(part: str) -> str:
    part = _SPACES_RE.sub(" ", part).strip()
    for _ in range(8):
        before = part
        part = _YEAR_SUFFIX_RE.sub("", part)
        part = _TRAILING_NOISE_RE.sub("", part)
        tokens = part.split(" ")
        if _noise_token(tokens[-1], _TRAILING_NOISE_WORDS):
            part = " ".join(tokens[:-1])
        elif len(tokens) > 1 and _noise_token(tokens[0], _LEADING_NOISE_WORDS):
            part = " ".join(tokens[1:])
        part = _unquote(part).strip(" " + _DASHES + ":|_/~,;" + chr(0x3001) + chr(0x3002))
        if part == before:
            break
    return part


# ── scripts, and names in two of them ──


def _script(ch: str) -> str:
    """Which script a character is written in, as far as telling two names
    of one thing apart goes: "hangul", "hankana" (Chinese characters and
    Japanese kana together, since a Japanese name mixes them), "cyrillic",
    "greek", "latin", or "" for no letter at all."""
    code = ord(ch)
    if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return "hangul"
    if (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x3FFFF
        or code == 0x3005
    ):
        return "hankana"
    if not ch.isalpha():
        return ""
    if 0x0400 <= code <= 0x052F:
        return "cyrillic"
    if 0x0370 <= code <= 0x03FF:
        return "greek"
    return "latin"


def _token_script(token: str) -> str:
    """The one script a word is written in, "mixed" for more than one
    ("Official髭男dism"), "" for none."""
    found = {s for s in map(_script, token) if s}
    if len(found) == 1:
        return found.pop()
    return "mixed" if found else ""


def is_east_asian(text: str) -> bool:
    """Whether ``text`` has a Chinese character, kana or Hangul in it."""
    return any(_script(ch) in ("hangul", "hankana") for ch in text)


def _script_runs(text: str) -> list[str]:
    """``text`` cut where one whole word in one script is followed by one in
    another: "周杰倫 Jay Chou" is two names, "晴天 Sunny Day" two titles. A
    word mixing scripts ("Official髭男dism") is never cut. Empty when there is
    only one run."""
    runs: list[list[str]] = []
    current = ""
    for token in text.split():
        script = _token_script(token)
        pure = script not in ("", "mixed")
        if runs and pure and current and script != current:
            runs.append([token])
            current = script
        elif runs:
            runs[-1].append(token)
            current = current or (script if pure else "")
        else:
            runs.append([token])
            current = script if pure else ""
    if len(runs) < 2:
        return []
    return [" ".join(run) for run in runs]


def alternatives(text: Any) -> list[str]:
    """Every name one part of a title gives a thing, the first one first:
    "IU(아이유)" is IU and 아이유, "周杰倫 Jay Chou" 周杰倫 and Jay Chou,
    "Official髭男dism" only itself."""
    names: list[str] = []
    keys: set[str] = set()
    for piece in str(text or "").split(_ALT):
        piece = _clean_part(piece)
        for name in _script_runs(piece) or [piece]:
            name = _clean_part(name)
            key = song_key(name)
            if name and key and key not in keys:
                keys.add(key)
                names.append(name)
    return names


# A bracket after a name that is not another name for it: a featured artist,
# a producer, where the song is from.
_CREDIT_RE = re.compile(
    r"^\s*(?:ft\.?|feat\.?|featuring|with|prod\.?|produced\s+by|cover(?:ed)?\s+by|by)\s",
    re.IGNORECASE,
)


def _mark_alternatives(text: str) -> str:
    """Round and square brackets holding another name for what they follow,
    in another script ("IU(아이유)", "봄날 (Spring Day)"), turned into _ALT
    and that name, so it is kept as an alternative rather than dropped with
    the other brackets."""
    out: list[str] = []
    last = 0
    for match in _ROUND_RE.finditer(text):
        inner = match.group(1).strip()
        before = next((s for s in map(_script, reversed(text[: match.start()])) if s), "")
        after = next((s for s in map(_script, inner) if s), "")
        if (
            before
            and after
            and before != after
            and not _has_noise(inner)
            and not _CREDIT_RE.match(inner)
            and not _FROM_RE.match(inner)
            and not kind_of(inner)
        ):
            out.append(text[last : match.start()].rstrip() + _ALT + inner)
        else:
            out.append(text[last : match.end()])
        last = match.end()
    out.append(text[last:])
    return "".join(out)


def _song_mark_patterns() -> list[tuple[re.Pattern[str], bool]]:
    patterns = []
    for opening, closing in _SONG_MARKS.items():
        both = re.escape(opening + closing)
        pattern = re.compile(f"{re.escape(opening)}([^{both}]*){re.escape(closing)}")
        patterns.append((pattern, opening in _LENTICULAR))
    return patterns


_SONG_MARK_PATTERNS = _song_mark_patterns()


def _mark_songs(text: str) -> str:
    """Each song mark with its content: noise dropped ("【MV】", 「MV」), any
    other name kept between _MARK_OPEN and _MARK_CLOSE. A lenticular bracket
    with any noise in it at all ("【Japanese ver.】") is dropped too: those
    describe the upload far more often than they name a song."""

    def mark(match: re.Match[str], lenticular: bool) -> str:
        inner = match.group(1).strip()
        if not inner or (_has_noise(inner) if lenticular else _is_noise(inner)):
            return " "
        return f" {_MARK_OPEN}{inner}{_MARK_CLOSE} "

    for pattern, lenticular in _SONG_MARK_PATTERNS:
        text = pattern.sub(lambda m, lent=lenticular: mark(m, lent), text)
    for ch in (*_SONG_MARKS, *_SONG_MARKS.values()):
        text = text.replace(ch, " ")
    return text


def _normalize(title: Any) -> str:
    """``title`` with control and private-use characters gone, full-width
    ASCII and half-width katakana made ordinary (NFKC, for those blocks only:
    elsewhere it would change letters), and the ideographic space, the wave
    dash and the tortoise-shell brackets read as a space, "~" and "()"."""
    text = _PRIVATE_USE_RE.sub(" ", str(title or "")[:_TITLE_MAX_CHARS])
    out = []
    for ch in text:
        code = ord(ch)
        if ch < " ":
            out.append(" ")
        elif code in (0x3001, 0xFF0C):
            # The ideographic and full-width commas, which no space follows.
            out.append(", ")
        elif 0xFF01 <= code <= 0xFF5E or 0xFF61 <= code <= 0xFF9F or 0xFFE0 <= code <= 0xFFE6:
            out.append(unicodedata.normalize("NFKC", ch))
        elif code == 0x3000:
            out.append(" ")
        elif code == 0x301C:
            out.append("~")
        else:
            out.append(_ROUND_ALIASES.get(ch, ch))
    return unicodedata.normalize("NFC", "".join(out))


def _prepare(title: Any) -> tuple[str, dict[str, Any]]:
    """The title ready to split: normalised, song marks and alternative
    names marked, brackets read and dropped. And what the brackets said:
    the work, its kind, the year."""
    text = _normalize(title)
    text = _WAVE_RE.sub(r" (\1) ", _HASHTAG_RE.sub(" ", text))
    text = _GAP_RE.sub(f" {_GAP} ", text)
    text = _mark_songs(text)
    text = _mark_alternatives(text)
    text = _SINGLE_QUOTED_RE.sub(lambda m: chr(0x201C) + m.group(1) + chr(0x201D), text)
    found: dict[str, Any] = {"work": "", "kind": None, "year": None}

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
    return text, found


def _unmark(text: str) -> str:
    return text.replace(_MARK_OPEN, " ").replace(_MARK_CLOSE, " ")


# ── the readings ──


def parse_title(title: Any) -> list[TitleReading]:
    """Every reading of ``title``, most likely first, at most MAX_READINGS,
    each with a song. Empty when the title names nothing."""
    text, found = _prepare(title)
    kind: str | None = found["kind"]
    year: int | None = found["year"]
    bracket_work: str = found["work"]
    readings: list[TitleReading] = []

    def add(song: str, others: list[str]) -> None:
        names = alternatives(song) or [_clean_part(song.replace(_ALT, " "))]
        song = names[0]
        if not song or not words(song) and not song_key(song):
            return
        artist_parts: list[str] = []
        artist_alts: tuple[str, ...] = ()
        work = bracket_work
        reading_kind = kind
        for other in others:
            other_names = alternatives(other) or [_clean_part(other.replace(_ALT, " "))]
            primary = other_names[0]
            named, named_kind = _named_work(primary)
            if named:
                work = work or named
                reading_kind = reading_kind or named_kind
                continue
            if not primary:
                continue
            artist_parts.append(primary)
            artist_alts = tuple(other_names[1:])
            work = work or cast_work(primary)
        artist = ", ".join(artist_parts)
        if len(artist_parts) != 1:
            artist_alts = ()
        # A title that says it is a cast recording or a soundtrack, and names
        # no work of its own, names it beside the song, if anywhere.
        if not work and reading_kind and artist_parts:
            work = artist_parts[-1]
        reading = TitleReading(
            song=song,
            artist=artist,
            work=work,
            kind=reading_kind,
            year=year,
            song_alts=tuple(names[1:]),
            artist_alts=artist_alts,
        )
        if reading not in readings:
            readings.append(reading)

    # A song in marks ("YOASOBI「夜に駆ける」", "周杰倫【晴天】"): the marks say
    # which part is the song, and the rest, up to a separator, who sings it.
    # The first marked name, then the last (an anime's name can come first:
    # "『鬼滅の刃』主題歌 LiSA「紅蓮華」"), then the first the other way round
    # ("【周杰倫】晴天").
    marked = [m for m in _MARKED_RE.finditer(text) if song_key(m.group(1).replace(_ALT, " "))]
    if marked:
        reverse: tuple[str, str] | None = None
        for match in dict.fromkeys((marked[0], marked[-1])):
            # Any other marked name is left out: a work ("『鬼滅の刃』") or a
            # second song, not who sings this one.
            outside = _MARKED_RE.sub(" ", text[: match.start()] + " " + text[match.end() :])
            parts, _ = _split(outside)
            add(match.group(1), parts[:1])
            if reverse is None and parts:
                reverse = (parts[0], match.group(1))
        if reverse:
            add(reverse[0], [reverse[1]])
        return readings[:MAX_READINGS]

    parts, separator = _split(text)
    if not parts:
        return []
    if len(parts) == 1:
        raw = _SPACES_RE.sub(" ", text.replace(_GAP, " ")).strip()
        quoted = _QUOTED_RE.search(raw)
        outside = _clean_part(raw[: quoted.start()] + " " + raw[quoted.end() :]) if quoted else ""
        if quoted and outside:
            # 'Keala Settle "This Is Me"', "NewJeans (뉴진스) 'Ditto'".
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
    Dashes first (and "_", "/", "~", which East Asian titles use the same
    way), then pipes; else a colon ("Work: Song", unless it is "Work: The
    Musical"); else a run of spaces."""
    text = text.strip()
    flat = text.replace(_GAP, " ")
    for pattern, name in ((_DASH_SPLIT_RE, "dash"), (_PIPE_SPLIT_RE, "pipe")):
        pieces = pattern.split(flat)
        parts = [p for p in (_clean_part(p) for p in pieces) if p]
        if len(parts) > 1:
            return parts, name
        if len(pieces) > 1 and parts:
            # Everything beside one part was noise: "Artist -Official Video".
            flat = parts[0]
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
        # "鄧麗君 Teresa Teng テレサ・テン", a channel's name: each is a name.
        names = alternatives(artist) or [artist]
        keys = {song_key(n) for n in (artist, *names)}
        tag_readings = parse_title(tag_title)
        marked = _MARKED_RE.search(_prepare(tag_title)[0])
        for index, reading in enumerate(tag_readings):
            # A song in marks is the song whatever the rest of a tag's title
            # says: the artist is the tag's ("【告白氣球】MV花絮 Making of").
            in_marks = index == 0 and marked is not None
            if in_marks or not reading.artist or song_key(reading.artist) in keys:
                found.append(
                    TitleReading(
                        song=reading.song,
                        artist=names[0],
                        work=reading.work or cast_work(artist),
                        kind=reading.kind,
                        year=reading.year,
                        song_alts=reading.song_alts,
                        artist_alts=tuple(names[1:]),
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


# ── who a title says the artist is, for finding them by name ──

# What a compilation or a playlist adds to its artist's name: "邓丽君经典金曲"
# is Teresa Teng's classic hits, "Teresa Teng's classic songs" the same. In
# Chinese, Japanese and Korean, found anywhere, longest first ("20首" is
# twenty songs); elsewhere as whole words.
_CJK_COMPILATION = re.split(
    r"\s+",
    "经典金曲 經典金曲 经典老歌 經典老歌 经典歌曲 經典歌曲 经典 經典 金曲 精选集 精選集 精选 精選"
    " 合集 串烧 串燒 好听的歌 好聽的歌 好听 好聽 歌曲 名曲集 名曲 老歌 专辑 專輯 全集 大全 歌单"
    " 歌單 首歌 ベストアルバム ベスト メドレー 作業用BGM 作業用 메들리 노래모음 모음 히트곡 명곡"
    " 연속듣기 플레이리스트",
)
_CJK_COMPILATION_RE = re.compile(
    r"\d+\s*首|" + "|".join(re.escape(w) for w in sorted(_CJK_COMPILATION, key=len, reverse=True))
)
_COMPILATION_RE = re.compile(
    r"(?:['" + chr(0x2019) + r"]s\b)?\s*\b(?:the\s+)?(?:greatest\s+hits|best\s+of|best\s+songs?"
    r"|best\s+hits|classic\s+(?:songs?|hits)|golden\s+(?:hits|songs?)"
    r"|top\s+(?:\d+\s+)?(?:songs?|hits)|all\s+songs|full\s+album|playlist|collection"
    r"|compilation|medley|non-?stop|hits|songs|album|grandes\s+exitos|mejores\s+canciones"
    r"|maiores\s+sucessos|kumpulan\s+lagu|lagu\s+terbaik|meilleures\s+chansons)\b",
    re.IGNORECASE,
)
# A list of artists: "Antônio Carlos Jobim, Vinicius de Moraes", "IU x SUGA".
_MEMBERS_RE = re.compile(
    r"\s*(?:&|,|\+|;|/|\band\b|\bfeat\.?|\bft\.?|\bwith\b|" + chr(0xD7) + r")\s*|\s+x\s+",
    re.IGNORECASE,
)
# A title with no separator is cut into artist and song only when it has
# this many words at most: every cut is a name to look for.
_SPLIT_MAX_WORDS = 6


def _without_compilation(name: str) -> str:
    """``name`` less what a compilation adds to it, or "" when it adds
    nothing (or is all there is)."""
    stripped = _clean_part(_COMPILATION_RE.sub(" ", _CJK_COMPILATION_RE.sub(" ", name)))
    return stripped if stripped != name and song_key(stripped) else ""


def _names(first: list[str]) -> list[str]:
    names: list[str] = []
    for name in first:
        if not name:
            continue
        names.append(name)
        members = [m for m in _MEMBERS_RE.split(name) if m and m.strip()]
        if len(members) > 1:
            names += [m.strip() for m in members]
    names += [s for s in map(_without_compilation, names) if s]
    out: list[str] = []
    keys: set[str] = set()
    for name in names:
        key = name_key(name)
        if key and key not in keys:
            keys.add(key)
            out.append(name)
    return out


def artist_names(reading: TitleReading) -> list[str]:
    """Every name the reading's artist might go by on MusicBrainz: each name
    the title gives it, each one in a list ("A, B"), and each without what a
    compilation adds to it ("邓丽君经典金曲")."""
    return _names([reading.artist, *reading.artist_alts])


def name_alternatives(name: Any) -> list[str]:
    """artist_names for a bare artist name, a tag: "鄧麗君 Teresa Teng
    テレサ・テン" is three names, "Xutos & Pontapés" one band or two people."""
    text = _normalize(name).strip()
    return _names([text, *(alternatives(_mark_alternatives(text)) or [])])


def name_splits(title: Any) -> list[tuple[str, str]]:
    """(artist, song) for every place a title naming both with nothing
    between them could be cut ("美空ひばり 川の流れのように", "Herbert
    Grönemeyer Männer"), both ways round, the shortest artist first. Empty
    when the title has a separator, marks or quotes, which say where the cut
    is, or too many words to try them all, or too few (two, unless Chinese,
    Japanese or Korean)."""
    text, _found = _prepare(title)
    if _MARKED_RE.search(text) or _QUOTED_RE.search(text):
        return []
    parts, _separator = _split(text.replace(_ALT, " "))
    if len(parts) != 1:
        return []
    tokens = parts[0].split()
    # Two Latin words are as often one song's name ("Bohemian Rhapsody") as
    # an artist and a song, and cost a search either way: three at least.
    fewest = 2 if is_east_asian(parts[0]) else 3
    if not fewest <= len(tokens) <= _SPLIT_MAX_WORDS:
        return []
    cuts: list[tuple[str, str]] = []
    for size in range(1, len(tokens)):
        cuts.append((" ".join(tokens[:size]), " ".join(tokens[size:])))
        cuts.append((" ".join(tokens[-size:]), " ".join(tokens[:-size])))
    return cuts


def script_of(name: str) -> str:
    """The scripts a name is written in, as one string ("hankana",
    "latin", "hankana+latin"): two names in different scripts are two
    independent spellings of whatever they both name."""
    return "+".join(sorted({s for s in map(_script, name) if s}))


def title_names(title: Any) -> tuple[list[list[str]], bool]:
    """The names a title gives, for finding its artist when no recording was
    found: per part of the title, every name it gives (alternatives), each
    less what a compilation adds ("邓丽君经典金曲" is 邓丽君, "Teresa Teng's
    classic songs" is Teresa Teng); and whether any part had such words.

    Nothing here says which name is the artist: find_band_for takes one only
    when the title corroborates it (two scripts naming one artist, or a
    compilation naming nobody else), never a part that merely is a band's
    name ("Metropolis - Part I")."""
    text, _found = _prepare(title)
    parts, _separator = _split(_unmark(text))
    groups: list[list[str]] = []
    compilation = False
    for part in parts:
        names: list[str] = []
        for name in alternatives(part) or []:
            rest = _clean_part(_COMPILATION_RE.sub(" ", _CJK_COMPILATION_RE.sub(" ", name)))
            if rest != name:
                compilation = True
            if song_key(rest) and name_key(rest) not in {name_key(n) for n in names}:
                names.append(rest)
        if names:
            groups.append(names)
    return groups, compilation
