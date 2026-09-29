"""Synced lyrics timed line by line to the track's own singing.

lyrics_align.py moves a whole LRC by one amount, which is enough when the
copy is the same recording with more or less lead-in. It is not enough when
LRCLIB's copy was timed to another cut or arrangement: a verse the track
lacks, a chorus sung once more, a live take at another pace, or a copy whose
own stamps were never right. Here each line is found where it is sung.

How, for a vocals stem and the lyrics' lines:

1. Whisper hears the vocals with word timestamps, in the worker process that
   transcribe.py runs (transcribe_worker.py), in the lyrics' language when
   their text shows it (script_language, text_language). Its transcript is
   kept beside the lyrics (LYRICS_TRANSCRIPT_FILE), so timing them again, or
   after another version is picked, costs no second pass; a transcript
   mend_lyrics already made is used the same way. The lyrics are not given
   to Whisper as a prompt: measured on the benchmark, a prompt made it copy
   the lyrics' own misspellings and lose whole lines (Dawid Podsiadlo
   "Malomiasteczkowy": 99% of the lyrics matched without, 27% with), and it
   ran three to four times as long.
2. The lyrics' words and the heard words become tokens, script by script:
   a Latin (or Cyrillic, Greek...) word folded for case and accents, one
   token per Chinese character or kana (katakana as hiragana, traditional as
   simplified, zh_variants.py), one per Hangul syllable. Words in brackets
   in a line with words outside them are a backing vocal sung over the lead
   and count half: they take the echo Whisper hears after the lead's words,
   never the lead's own. Credits at the top of a copy ("作词 : ...") and
   pause marks ("♪") are not sung and are not timed.
3. A monotonic alignment (a dynamic program with free gaps on both sides and
   a bonus for runs of consecutive matches) pairs lyric tokens with heard
   ones: exact tokens score 1, Latin words close by edit distance and Hangul
   syllables one letter off (a misheard word) a little less. Free gaps are
   what let a verse the track lacks, or a chorus sung once more than the text
   has, go unmatched rather than pull the rest out of place; monotonic order
   keeps a repeated chorus with its own occurrence, and a small cost for
   each heard word skipped between matches keeps it with its first one.
4. A line is anchored where its first matched word starts, when enough of it
   matched in a run and the voice is really there (the vocal envelope,
   lyrics_align._envelope): a word heard in a silence anchors nothing. Whisper
   often starts the first word of a line on the held end of the line before;
   the start is moved to where the voice comes back after a pause, or rises
   (or, for a word held long, rises again twice as sharply), within that
   word. When Whisper missed the first words of a phrase and heard nothing
   else in it, the line starts where the phrase does.
5. Anchors the LRC's own stamps show are out of place (a phrase matched where
   it is sung again) are dropped, and when the stamps follow the anchors, the
   alignment runs again with each line's words looked for only near where
   the stamps and the anchors around it put it.
6. The lines between anchors are placed with the song's own timing: by the
   LRC's stamps mapped between the two anchors when those agree with them,
   else spread over the singing between (not linearly across an instrumental
   gap), each start put on the nearest onset of the voice. A line stamped the
   same as the one before it (a translation) is shown with that one.
7. A run of lines still not found where there is singing (a chorus sung
   once more at the end, which Whisper skips) is heard again on its own
   (the worker's --clips), and the lines it then finds are kept when they
   sit where the copy's stamps and the lines found around them say.
8. Words inside a line are stamped where they were heard, and the rest
   spread between those by their syllables.

Measured on the benchmark and left out: the lead vocal
stem of the lead/backing split (better on 4 of 16 songs, worse on 7, and 2
to 3 minutes of CPU a song), a band-pass and levelled stem (within noise,
and fewer words matched), and a spectral-flux onset cue for legato line
starts (worse in 5 of 10 languages).

The result is kept in lyrics.json as ``aligned``, only when it passes the
quality gate (retime_gate, no_worse): {"synced": enhanced LRC, "method":
"whisper", "matched": the share of the lyrics' syllables matched to heard
words, "lines_matched", "lines", "at": unix time}. The source ``synced`` is
never rewritten here.

What the page shows, first present wins:

1. ``user_synced``: a full manual timing from the sync editor (PUT
   .../lyrics/user-synced, validated by clean_user_synced: its lines are the
   lyrics' own, one to one).
2. ``aligned.synced``: this module's result.
3. ``synced`` moved by ``offset_sec``: the source, with the Align panel's
   whole-song offset.

When it runs: after separation for LRCLIB lyrics whose timing the voice
does not confirm line for line (a copy of another length, shifted or not,
or one of the track's length no whole-song shift confirms), under the transcribe_lyrics setting's terms (auto
means a CUDA job only), inside the pipeline's lock (retime_after_separation);
and on demand, from POST .../lyrics/retime (app/api/jobs.py), behind the same
lock.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import (
    LYRICS_RETIME_MAX_CELLS,
    LYRICS_RETIME_MIN_LINES_SHARE,
    LYRICS_RETIME_MIN_MATCHED,
    LYRICS_TIMED_MAX_CHARS,
    LYRICS_TRANSCRIPT_FILE,
    LYRICS_USER_SYNCED_MAX_LINES,
)
from app.pipeline.lyrics_align import (
    _HIT_RISE_DB,
    _LOUD_PERCENTILE,
    _RISE_FRAMES,
    _SILENT_DB,
    _VOICE_FLOOR_DB,
    _VOICE_RANGE_DB,
    _offset,
    onset_strength,
)
from app.pipeline.zh_variants import to_simplified

logger = logging.getLogger("stemdeck.lyrics")

# ── tokens ──

_APOSTROPHES = frozenset("'’`ʼ")
# Latin letters Unicode does not build from a base letter and an accent.
_PLAIN_LETTERS = str.maketrans(
    {"ł": "l", "ø": "o", "đ": "d", "ð": "d", "ß": "ss", "æ": "ae", "œ": "oe", "ı": "i", "þ": "th"}
)
_VOWELS = re.compile(r"[aeiouy]+")
# A small kana or the long-vowel mark belongs to the syllable before it.
_SMALL_KANA = frozenset("ゃゅょャュョっッーぁぃぅぇぉァィゥェォ")


def _han_or_kana(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x20000 <= cp <= 0x3FFFF
        or 0x3040 <= cp <= 0x30FF
        or 0x31F0 <= cp <= 0x31FF
        or 0xFF66 <= cp <= 0xFF9F
    )


def _hangul(ch: str) -> bool:
    return 0xAC00 <= ord(ch) <= 0xD7AF


def _cjk(ch: str) -> bool:
    return _han_or_kana(ch) or _hangul(ch)


def _word_char(ch: str) -> bool:
    return ch.isalnum() and not _cjk(ch)


def _latin_key(word: str) -> str:
    text = unicodedata.normalize("NFKC", word).casefold().translate(_PLAIN_LETTERS)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _char_key(ch: str) -> str:
    ch = to_simplified(unicodedata.normalize("NFKC", ch))
    cp = ord(ch[0]) if ch else 0
    # Katakana as hiragana: the same sound written either way.
    if 0x30A1 <= cp <= 0x30F6:
        return chr(cp - 0x60)
    return ch


def tokens_of(text: str) -> list[tuple[str, int, int, float]]:
    """The tokens of ``text`` as (key, start, end, syllables), in order, with
    ``start`` and ``end`` its character offsets: a word of letters and digits
    (apostrophes inside it dropped, "don't" is "dont"), else one Chinese
    character, kana or Hangul syllable each."""
    out: list[tuple[str, int, int, float]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _cjk(ch):
            out.append((_char_key(ch), i, i + 1, 0.3 if ch in _SMALL_KANA else 1.0))
            i += 1
            continue
        if _word_char(ch):
            j = i
            while j < n and (
                _word_char(text[j])
                or (text[j] in _APOSTROPHES and j + 1 < n and _word_char(text[j + 1]))
            ):
                j += 1
            key = _latin_key("".join(c for c in text[i:j] if c not in _APOSTROPHES))
            if key:
                syllables = len(_VOWELS.findall(key)) + sum(c.isdigit() for c in key)
                out.append((key, i, j, float(max(1, syllables))))
            i = j
            continue
        i += 1
    return out


def _has_word(text: str) -> bool:
    return any(_word_char(c) or _cjk(c) for c in text)


def pieces_of(text: str) -> list[tuple[int, int]]:
    """Where the word stamps of a line go: (start, end) character offsets of
    each piece, in order. A piece is a word as the text spaces it, except
    that each Chinese character or kana is one of its own (those lines do not
    space their words), and a Latin word after one starts another."""
    pieces: list[tuple[int, int]] = []
    start: int | None = None
    prev_cjk = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if start is not None:
                pieces.append((start, i))
                start = None
            prev_cjk = False
            continue
        cjk = _han_or_kana(ch)
        if start is None:
            start = i
        elif cjk or (prev_cjk and _word_char(ch)):
            pieces.append((start, i))
            start = i
        if cjk or _word_char(ch) or _hangul(ch):
            prev_cjk = cjk
    if start is not None:
        pieces.append((start, len(text)))
    # A piece of punctuation alone joins the one before it (or after, first).
    merged: list[tuple[int, int]] = []
    for a, b in pieces:
        if merged and not _has_word(text[a:b]):
            merged[-1] = (merged[-1][0], b)
            continue
        merged.append((a, b))
    if len(merged) > 1 and not _has_word(text[merged[0][0] : merged[0][1]]):
        merged[1] = (merged[0][0], merged[1][1])
        merged.pop(0)
    return merged


# ── lines ──

_LINE_STAMPS = re.compile(r"^(?:\s*\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+")
_LINE_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_STAMP = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_OPEN, _CLOSE = "([（【", ")]）】"


def line_text(raw: str) -> str:
    """A line's text as the page shows it: word stamps out, spaces collapsed,
    composed (parseLrc in static/js/lyricsLookup.js)."""
    return " ".join(unicodedata.normalize("NFC", _WORD_STAMP.sub("", raw)).split())


@dataclass
class Line:
    text: str
    prior: float | None
    pieces: list[tuple[int, int]] = field(default_factory=list)
    # Indices into the lyric's token list.
    tokens: list[int] = field(default_factory=list)


def synced_lines(synced: str) -> list[tuple[float, str]]:
    """Every sung line of an LRC as (seconds, text), earliest first: a line
    stamped more than once (a chorus written once) is each of its times, the
    [offset:] tag is applied, and lines with no words are left out."""
    offset = _offset(synced)
    out: list[tuple[float, str]] = []
    for raw in synced.splitlines():
        stamps = _LINE_STAMPS.match(raw)
        if not stamps:
            continue
        text = line_text(raw[stamps.end() :])
        if not text:
            continue
        for m in _LINE_STAMP.finditer(stamps.group(0)):
            seconds = int(m.group(1)) * 60 + float(m.group(2).replace(":", "."))
            out.append((max(0.0, seconds - offset), text))
    out.sort(key=lambda pair: pair[0])
    return out


def plain_lines(plain: str) -> list[str]:
    return [t for t in (line_text(raw) for raw in plain.splitlines()) if t]


@dataclass
class Lyric:
    lines: list[Line]
    keys: list[str]
    weights: list[float]
    token_line: list[int]
    token_piece: list[int]
    # Tokens in brackets in a line with words outside them: a backing vocal
    # sung over the lead, which Whisper hears little of.
    aside: list[bool]


def _bracketed(text: str) -> list[bool]:
    """For each character of ``text``, whether it is inside brackets."""
    depth = 0
    out: list[bool] = []
    for ch in text:
        if ch in _OPEN:
            depth += 1
        out.append(depth > 0)
        if ch in _CLOSE and depth:
            depth -= 1
    return out


def build_lyric(pairs: list[tuple[float | None, str]]) -> Lyric:
    """The lines (stamp or None, text) as tokens."""
    lyric = Lyric([], [], [], [], [], [])
    for prior, text in pairs:
        line = Line(text=text, prior=prior, pieces=pieces_of(text))
        starts = [a for a, _b in line.pieces]
        toks = tokens_of(text)
        bracketed = _bracketed(text)
        inside = [bracketed[a] for _k, a, _b, _s in toks]
        if all(inside):
            inside = [False] * len(toks)
        for (key, a, _b, syllables), side in zip(toks, inside, strict=True):
            line.tokens.append(len(lyric.keys))
            lyric.keys.append(key)
            lyric.weights.append(syllables)
            lyric.token_line.append(len(lyric.lines))
            lyric.token_piece.append(max(0, bisect.bisect_right(starts, a) - 1))
            lyric.aside.append(side)
        lyric.lines.append(line)
    return lyric


# ── the voice ──


@dataclass
class Voice:
    """The vocals stem's envelope: where it is singing and where it starts."""

    hop: float
    voiced: np.ndarray
    strength: np.ndarray
    cumulative: np.ndarray

    @classmethod
    def of(cls, hop: float, db: Any) -> Voice:
        """From an envelope, a level in dB every ``hop`` seconds. Singing is
        within _VOICE_RANGE_DB of the stem's loud parts, as the page's wipe
        has it (lyrics_align)."""
        levels = np.asarray(db, dtype=float)
        voiced = np.zeros(levels.size, dtype=bool)
        if levels.size:
            loud = float(np.percentile(levels, _LOUD_PERCENTILE))
            if loud >= _SILENT_DB:
                voiced = levels >= max(loud - _VOICE_RANGE_DB, _VOICE_FLOOR_DB)
        cumulative = np.concatenate(([0.0], np.cumsum(voiced) * hop))
        return cls(hop, voiced, onset_strength(levels), cumulative)

    def frame(self, t: float) -> int:
        return int(min(max(0, round(t / self.hop)), max(0, self.voiced.size - 1)))

    def voiced_near(self, t: float, within: float) -> bool:
        if not self.voiced.size:
            return True
        return bool(self.voiced[self.frame(t - within) : self.frame(t + within) + 1].any())

    def voiced_between(self, a: float, b: float) -> float:
        if not self.voiced.size or b <= a:
            return 0.0
        return float(self.cumulative[self.frame(b)] - self.cumulative[self.frame(a)])

    def at_voiced(self, a: float, amount: float) -> float:
        """The sung moment by which ``amount`` seconds of singing follow
        ``a``: with 0, where the singing next starts."""
        target = self.cumulative[self.frame(a)] + amount
        k = int(np.searchsorted(self.cumulative, target, side="right")) - 1
        if k >= self.voiced.size:
            return a
        return max(a, k * self.hop)

    def end_of_singing(self, t: float, within: float) -> float:
        """Where the singing going on at ``t`` stops, at most ``within``
        seconds on: the held end of a line belongs to it."""
        k, last = self.frame(t), self.frame(t + within)
        while k < last and self.voiced[k]:
            k += 1
        return max(t, k * self.hop)

    def onset_near(self, t: float, within: float) -> float | None:
        """Where the voice rises most within ``within`` seconds of ``t``,
        as the time the singing starts, or None when it does not clearly."""
        if not self.strength.size:
            return None
        lo, hi = self.frame(t - within), self.frame(t + within)
        window = self.strength[lo : hi + 1]
        if not window.size or window.max() < _HIT_RISE_DB:
            return None
        return self._rise_time(lo + int(np.argmax(window)))

    def _rise_time(self, peak: int) -> float:
        # The rise is measured over _RISE_FRAMES of the level smoothed over
        # three: it peaks a frame after a step in the level.
        return max(0.0, (peak - (_RISE_FRAMES - 1) / 2) * self.hop)

    def rise_start(self, start: float, end: float) -> float:
        """When a heard word that starts a line really starts. Whisper often
        starts it on the held end of the line before: when the voice does not
        rise at ``start`` but does within the word (or just past its end),
        the word starts at that rise."""
        if not self.strength.size:
            return start
        at = self.frame(start)
        here = float(self.strength[max(0, at - 2) : at + 3].max(initial=0))
        if here >= _HIT_RISE_DB:
            return self._stronger_rise(start, end, here)
        span = min(
            _RISE_SEARCH_MAX_SEC, max(_RISE_SEARCH_MIN_SEC, end - start + _RISE_PAST_END_SEC)
        )
        window = self.strength[at : self.frame(start + span) + 1]
        above = np.flatnonzero(window >= _HIT_RISE_DB)
        if not above.size:
            return start
        k = int(above[0])
        while k + 1 < window.size and window[k + 1] >= window[k]:
            k += 1
        return max(start, self._rise_time(at + k))

    def _stronger_rise(self, start: float, end: float, here: float) -> float:
        """A word held longer than _HELD_WORD_SEC whose voice rises again,
        _STRONGER times as sharply as at its start, before its end: Whisper
        started it on the tail of the note before, and it starts at the
        stronger rise."""
        if end - start < _HELD_WORD_SEC:
            return start
        lo, hi = self.frame(start + 0.15), self.frame(end - 0.05)
        window = self.strength[lo : hi + 1]
        if not window.size:
            return start
        k = int(np.argmax(window))
        if window[k] < max(_STRONGER * here, 2 * _HIT_RISE_DB):
            return start
        return self._rise_time(lo + k)

    def phrase_start(self, t: float, gap: float) -> float | None:
        """Where the singing going on at ``t`` started: back through the
        voice until a silence of ``gap`` seconds. None when there is no
        singing at ``t``."""
        if not self.voiced.size:
            return None
        k = self.frame(t)
        # The word may start a frame before the voice is loud enough.
        while k < self.voiced.size - 1 and not self.voiced[k] and k < self.frame(t + 0.1):
            k += 1
        if not self.voiced[k]:
            return None
        need = max(1, round(gap / self.hop))
        quiet = 0
        while k > 0:
            if self.voiced[k - 1]:
                quiet = 0
            else:
                quiet += 1
                if quiet >= need:
                    return (k - 1 + quiet) * self.hop
            k -= 1
        return 0.0

    def first_voiced(self) -> float | None:
        idx = np.flatnonzero(self.voiced)
        return float(idx[0] * self.hop) if idx.size else None

    def last_voiced(self) -> float | None:
        idx = np.flatnonzero(self.voiced)
        return float((idx[-1] + 1) * self.hop) if idx.size else None


# How far into a line's first heard word its start is looked for.
_RISE_SEARCH_MIN_SEC = 0.4
_RISE_SEARCH_MAX_SEC = 1.5
# Whisper can end that word where the rise is: looked for a little past it.
_RISE_PAST_END_SEC = 0.1
# A first word held this long, whose voice rises again this many times as
# sharply as at its start, starts at that rise.
_HELD_WORD_SEC = 0.7
_STRONGER = 2.0


# ── what was heard ──


@dataclass
class Heard:
    keys: list[str]
    starts: np.ndarray
    ends: np.ndarray
    # The end of the whole heard word each token is part of.
    word_ends: np.ndarray


# A pause in the voice at least this long inside a heard word is before it...
_PAUSE_SEC = 0.15
# ...when it starts in this first share of the word.
_PAUSE_WITHIN = 0.8


def voiced_start(start: float, end: float, voice: Voice | None) -> float:
    """When a heard word really starts. Whisper often starts the first word
    after a pause at the end of the word before, a second or so early, and
    stretches it over the pause: when the voice pauses for _PAUSE_SEC or more
    in the first _PAUSE_WITHIN of the word, it starts where the voice comes
    back after the last such pause."""
    if voice is None or not voice.voiced.size or end - start <= _PAUSE_SEC:
        return start
    lo, hi = voice.frame(start), voice.frame(end)
    latest = voice.frame(start + (end - start) * _PAUSE_WITHIN)
    quiet = ~voice.voiced[lo:hi]
    need = max(1, round(_PAUSE_SEC / voice.hop))
    back = None
    k = 0
    while k < quiet.size:
        if not quiet[k]:
            k += 1
            continue
        first = k
        while k < quiet.size and quiet[k]:
            k += 1
        if k - first >= need and lo + first <= latest and k < quiet.size:
            back = k
    return (lo + back) * voice.hop if back is not None else start


def heard_tokens(words: list[dict[str, Any]], voice: Voice | None = None) -> Heard:
    """Whisper's words as tokens, each word's time split between its tokens
    by their syllables, each word's start moved past a pause in the voice
    it begins with (voiced_start)."""
    keys: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    word_ends: list[float] = []
    for word in words:
        toks = tokens_of(str(word.get("text", "")))
        if not toks:
            continue
        start, end = float(word["start"]), max(float(word["end"]), float(word["start"]))
        start = voiced_start(start, end, voice)
        total = sum(t[3] for t in toks)
        at = start
        for key, _a, _b, syllables in toks:
            span = (end - start) * syllables / total if total else 0.0
            keys.append(key)
            starts.append(at)
            ends.append(at + span)
            word_ends.append(end)
            at += span
    return Heard(
        keys,
        np.asarray(starts, dtype=float),
        np.asarray(ends, dtype=float),
        np.asarray(word_ends, dtype=float),
    )


# ── matching ──

# What a match of a backing vocal's word (an aside) is worth: it takes the
# echo Whisper hears after the lead's words, never the lead's own.
_ASIDE_SCALE = 0.5
# A misheard Latin word still counts, a little less, when it is this close.
_FUZZY_MIN = 0.67
_FUZZY_SCALE = 0.9
# A word of two letters or fewer only counts as itself.
_FUZZY_MIN_LEN = 3
# A Hangul syllable that shares two of its three letters with the heard one.
_HANGUL_NEAR = 0.6
# What a match right after a match adds: runs anchor, stray words do not.
_RUN_BONUS = 0.5
# What each heard token skipped between two matches costs.
_GAP_COST = 0.1
_NEG = -1e9


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """How alike two Latin tokens are, 0 to 1: 1 - edit distance / length."""
    if a == b:
        return 1.0
    if len(a) < _FUZZY_MIN_LEN or len(b) < _FUZZY_MIN_LEN:
        return 0.0
    longest = max(len(a), len(b))
    if abs(len(a) - len(b)) > longest * (1 - _FUZZY_MIN):
        return 0.0
    return 1.0 - _levenshtein(a, b) / longest


def _jamo(ch: str) -> tuple[int, int, int]:
    """A Hangul syllable's lead consonant, vowel and final consonant."""
    code = ord(ch) - 0xAC00
    return code // 588, (code % 588) // 28, code % 28


def _jamo_alike(a: str, b: str) -> bool:
    return sum(x == y for x, y in zip(_jamo(a), _jamo(b), strict=True)) >= 2


def score_table(lyric_keys: list[str], heard_keys: list[str]) -> tuple:
    """(table, lyric ids, heard ids): table[u, v] is the score of lyric key
    u against heard key v, _NEG when they do not match at all."""
    u_keys = sorted(set(lyric_keys))
    v_keys = sorted(set(heard_keys))
    u_index = {k: i for i, k in enumerate(u_keys)}
    v_index = {k: i for i, k in enumerate(v_keys)}
    table = np.full((len(u_keys), len(v_keys)), _NEG, dtype=np.float64)
    for k, i in u_index.items():
        j = v_index.get(k)
        if j is not None:
            table[i, j] = 1.0
    latin_v = [(k, j) for k, j in v_index.items() if not _cjk(k[0])]
    hangul_v = [(k, j) for k, j in v_index.items() if _hangul(k[0])]
    for k, i in u_index.items():
        if _hangul(k[0]):
            for hk, j in hangul_v:
                if hk != k and _jamo_alike(k, hk):
                    table[i, j] = _HANGUL_NEAR
            continue
        if _cjk(k[0]) or len(k) < _FUZZY_MIN_LEN:
            continue
        for hk, j in latin_v:
            if hk == k or abs(len(hk) - len(k)) > 3:
                continue
            sim = similarity(k, hk)
            if sim >= _FUZZY_MIN:
                table[i, j] = sim * _FUZZY_SCALE
    lyric_ids = np.asarray([u_index[k] for k in lyric_keys], dtype=np.int64)
    heard_ids = np.asarray([v_index[k] for k in heard_keys], dtype=np.int64)
    return table, lyric_ids, heard_ids


def align_tokens(
    lyric_keys: list[str],
    heard_keys: list[str],
    heard_starts: np.ndarray | None = None,
    windows: tuple[np.ndarray, np.ndarray] | None = None,
    scale: np.ndarray | None = None,
) -> list[tuple[int, int, float]]:
    """The best monotonic pairing of lyric tokens with heard tokens, as
    (lyric index, heard index, score), in order. Lyric tokens unheard are
    free to skip, and heard tokens before the first match and after the
    last; each match scores its similarity, plus _RUN_BONUS when it follows
    the match of the tokens just before on both sides, less _GAP_COST for
    each heard token skipped between matches. One row of the
    dynamic program at a time, in numpy; only the back pointers are kept, two
    bytes a cell. Empty when there is nothing to pair or the two are too
    long to pair (LYRICS_RETIME_MAX_CELLS). With ``windows`` (per lyric
    token, the earliest and latest start) and ``heard_starts``, a token only
    pairs with heard tokens starting within its window."""
    n, m = len(lyric_keys), len(heard_keys)
    if not n or not m or n * m > LYRICS_RETIME_MAX_CELLS:
        return []
    table, lyric_ids, heard_ids = score_table(lyric_keys, heard_keys)
    d_prev = np.zeros(m + 1)
    m_prev = np.full(m + 1, _NEG)
    # from_run[i, j]: the match at (i, j) follows a match. how[i, j]: what
    # the best score up to (i, j) ends with: 0 a match here, 1 a lyric token
    # skipped, 2 a heard token skipped.
    from_run = np.zeros((n + 1, m + 1), dtype=np.bool_)
    how = np.zeros((n + 1, m + 1), dtype=np.int8)
    how[1:, 0] = 1
    cost = _GAP_COST * np.arange(m + 1)
    for i in range(1, n + 1):
        scores = table[lyric_ids[i - 1]][heard_ids]
        if scale is not None:
            scores = np.where(scores > 0, scores * scale[i - 1], scores)
        if windows is not None and heard_starts is not None:
            lo, hi = windows[0][i - 1], windows[1][i - 1]
            scores = np.where((heard_starts >= lo) & (heard_starts <= hi), scores, _NEG)
        run = m_prev[:-1] + _RUN_BONUS
        chain = run > d_prev[:-1]
        m_row = np.full(m + 1, _NEG)
        m_row[1:] = np.where(scores > 0, scores + np.where(chain, run, d_prev[:-1]), _NEG)
        from_run[i, 1:] = chain
        here = np.maximum(m_row, d_prev)
        # Heard tokens skipped between matches cost _GAP_COST each: of two
        # pairings that hear the lines about as well, the one that sings them
        # without a long wait between (a chorus written once is its first
        # time, not one a minute on). Kept in the running maximum as
        # here[k] + cost * k.
        lifted = here + cost
        best = np.maximum.accumulate(lifted)
        d_row = best - cost
        earlier = np.concatenate(([False], best[1:] == best[:-1]))
        how[i] = np.where((best > lifted) | earlier, 2, np.where(m_row >= d_prev, 0, 1)).astype(
            np.int8
        )
        how[i, 0] = 1
        d_prev, m_prev = d_row, m_row
    pairs: list[tuple[int, int, float]] = []
    # Heard tokens after the last match cost nothing: the best end, earliest.
    i, j, in_match = n, int(np.argmax(d_prev)), False
    while i > 0 and j > 0:
        if not in_match:
            step = how[i, j]
            if step == 0:
                in_match = True
            elif step == 1:
                i -= 1
            else:
                j -= 1
            continue
        pairs.append((i - 1, j - 1, float(table[lyric_ids[i - 1], heard_ids[j - 1]])))
        in_match = bool(from_run[i, j])
        i, j = i - 1, j - 1
    pairs.reverse()
    return pairs


# ── retiming ──

# A line is anchored when at least this share of its syllables matched...
_ANCHOR_MIN_SHARE = 0.34
# ...with at least two of its tokens matched in a run (one-token lines: one).
_ANCHOR_MIN_RUN = 2
# The seconds a syllable takes when nothing better is known.
_DEFAULT_RATE = 0.28
_MIN_RATE, _MAX_RATE = 0.08, 0.9
# An anchor with no singing this close is a word heard in a silence.
_VOICE_CHECK_SEC = 0.35
# The singing going on when a line's last word ends is that line's for at
# most this long.
_HELD_SEC = 1.0
# A placed line starts on an onset of the voice this close to its estimate.
_SNAP_SEC = 0.6
# LRC stamps between two anchors are trusted when their spacing agrees with
# the anchors' within this ratio.
_PRIOR_AGREE = 0.12
# Lines this close apart are one after the other, not at once.
_MIN_GAP = 0.05
# Two matched words of a line are sung together when no more than this many
# seconds apart, plus this many a syllable between them.
_CLUSTER_GAP_SEC = 1.5
_CLUSTER_SEC_PER_SYLLABLE = 0.8
# ...or any distance apart, when next to each other in what was heard and
# the voice sounds for this share of the time between them: a held note.
_HELD_VOICED = 0.8
# An anchor this many seconds off its neighbours' shift from the stamps, on
# both sides, while those agree among themselves within _AGREE_SEC, is a
# phrase matched where it is sung again, not this line.
_OUTLIER_SEC = 3.0
_AGREE_SEC = 1.0
# A second pass looks for each line's words within this many seconds of
# where its neighbours' shifts from the stamps put it, given at least this
# many anchors, and is kept when it matches at least this share of what the
# first matched.
_WINDOW_SEC = 8.0
_WINDOW_MIN_ANCHORS = 4
_WINDOW_KEEP = 0.9

Anchor = tuple[float, float, float]  # (start, end, seconds a syllable or 0)


@dataclass
class Timed:
    start: float
    end: float
    words: list[float]
    anchored: bool


@dataclass
class Retimed:
    lines: list[Line]
    timed: list[Timed]
    matched: float
    lines_matched: int
    # The lines that were timed: all but those shown with another
    # (retime_lines), which have nothing of their own to be found by.
    counted: int = -1

    def __post_init__(self) -> None:
        if self.counted < 0:
            self.counted = len(self.lines)


def _line_rate(points: list[tuple[float, float]]) -> float | None:
    """Seconds a syllable from (cumulative syllables, time) points."""
    if len(points) < 2:
        return None
    (s0, t0), (s1, t1) = points[0], points[-1]
    if s1 <= s0 or t1 <= t0:
        return None
    return min(_MAX_RATE, max(_MIN_RATE, (t1 - t0) / (s1 - s0)))


def _companions(pairs: list[tuple[float | None, str]]) -> list[int | None]:
    """For each line, the line it is shown with, or None: a line stamped the
    same as the one before it is its translation or romanisation (bilingual
    LRC), never sung apart from it."""
    out: list[int | None] = [None] * len(pairs)
    for i in range(1, len(pairs)):
        here, before = pairs[i][0], pairs[i - 1][0]
        if here is not None and before is not None and abs(here - before) < 0.005:
            leader = out[i - 1]
            out[i] = leader if leader is not None else i - 1
    return out


# A credit a copy lists before the singing: "作词 : 米津玄師", "Lyrics: ...".
_CREDIT = re.compile(
    r"^\s*(?:作?[词詞]|作?曲|[编編]曲|制作人?|製作人?|[监監]製|[监監]制|演唱|原唱|和声|和聲|"
    r"混音|母带|母帶|吉他|贝斯|貝斯|鼓|键盘|鍵盤|弦乐|弦樂|出品|发行|發行|"
    r"lyrics?|music|composer|composed|written|arrange[dr]?|producer|produced)"
    r"\s*(?:by)?\s*[:：]",
    re.IGNORECASE,
)


def _credits(pairs: list[tuple[float | None, str]]) -> set[int]:
    """The lines at the top of a copy that are its credits, not sung: those
    matching _CREDIT, and a "Title - Artist" line just before them."""
    out: set[int] = set()
    for i, (_t, text) in enumerate(pairs):
        if _CREDIT.match(text):
            out.add(i)
        elif i > len(out) + 1:
            break
    if out and min(out) == 1 and " - " in pairs[0][1]:
        out.add(0)
    return out


def retime_lines(
    pairs: list[tuple[float | None, str]],
    words: list[dict[str, Any]],
    voice: Voice | None,
    duration: float,
) -> Retimed:
    """The lyrics' lines as (stamp or None, text) timed to the heard words:
    retime() of all but the lines shown with another (_companions), which
    take that one's timing, and the credits at the top (_credits), which
    are shown before the singing."""
    companions = _companions(pairs)
    credits = _credits(pairs)
    # A line with no word ("♪", "...") marks a pause: it keeps its place in
    # the copy's own timing, moved with the line before it.
    marks = {i for i, (_t, text) in enumerate(pairs) if not tokens_of(text)}
    main = [
        i for i, c in enumerate(companions) if c is None and i not in credits and i not in marks
    ]
    heard = heard_tokens(words, voice)
    result = retime(build_lyric([pairs[i] for i in main]), heard, voice, duration)
    if len(main) == len(pairs):
        return result
    full = build_lyric(pairs)
    placed = dict(zip(main, result.timed, strict=True))
    first = min((t.start for t in result.timed), default=0.0)
    timed: list[Timed] = []
    for i, line in enumerate(full.lines):
        if i in placed:
            timed.append(placed[i])
            continue
        if i in marks and i not in credits and companions[i] is None:
            timed.append(_mark_timing(i, pairs, placed, line))
            continue
        if i in credits or companions[i] not in placed:
            # Shown before the singing, as briefly as the copy has them.
            at = max(0.0, min(pairs[i][0] or 0.0, first - _MIN_GAP * (len(credits) - i)))
            timed.append(Timed(at, at, [at] * len(line.pieces), anchored=True))
            continue
        leader = placed[companions[i]]  # type: ignore[index]
        step = (leader.end - leader.start) / max(1, len(line.pieces))
        words_at = [leader.start + step * k for k in range(len(line.pieces))]
        timed.append(Timed(leader.start, leader.end, words_at, anchored=leader.anchored))
    return Retimed(full.lines, timed, result.matched, result.lines_matched, len(main))


def _mark_timing(
    i: int, pairs: list[tuple[float | None, str]], placed: dict[int, Timed], line: Line
) -> Timed:
    """A pause mark's timing: its stamp moved as the line before it was
    (after it, when it leads), or just after that line's end without one."""
    before = [k for k in placed if k < i]
    after = [k for k in placed if k > i]
    prior = pairs[i][0]
    at = 0.0
    if before:
        lead = placed[max(before)]
        at = lead.end + _MIN_GAP
        mark_prior, lead_prior = prior, pairs[max(before)][0]
        if mark_prior is not None and lead_prior is not None:
            at = max(at, lead.start + (mark_prior - lead_prior))
    elif after and prior is not None and pairs[min(after)][0] is not None:
        at = max(0.0, placed[min(after)].start - (pairs[min(after)][0] - prior))  # type: ignore[operator]
    if after:
        at = min(at, placed[min(after)].start - _MIN_GAP)
    at = max(0.0, at)
    return Timed(at, at, [at] * len(line.pieces), anchored=True)


def retime(lyric: Lyric, heard: Heard, voice: Voice | None, duration: float) -> Retimed:
    """Each line's start and end, and each piece's start, on the track."""
    lines = lyric.lines
    # An aside's words count for less: they are sung over the lead's.
    dp_keys = lyric.keys
    scale = np.where(np.asarray(lyric.aside, dtype=bool), _ASIDE_SCALE, 1.0)
    total_w = sum(w for w, side in zip(lyric.weights, lyric.aside, strict=True) if not side)
    pairs = align_tokens(dp_keys, heard.keys, scale=scale)
    matched_at = _compact(lyric, heard, voice, {i: j for i, j, _s in pairs})
    anchors, rates = _anchor(lyric, heard, voice, matched_at)
    anchors = _drop_outliers(lines, anchors)
    # Again, each line's words looked for only near where its stamp and the
    # anchors around it put it, when those agree: a phrase sung again
    # elsewhere then cannot take a line away from its place.
    windows = _windows(lyric, anchors)
    if windows is not None:
        pairs = align_tokens(dp_keys, heard.keys, heard.starts, windows, scale)
        near = _compact(lyric, heard, voice, {i: j for i, j, _s in pairs})
        # Kept unless it loses much of what the first pass heard: stamps
        # that say nothing about where lines are sung would.
        if _weight(lyric, near) >= _WINDOW_KEEP * _weight(lyric, matched_at):
            matched_at = near
            anchors, rates = _anchor(lyric, heard, voice, matched_at)
            anchors = _drop_outliers(lines, anchors)
    matched_w = _weight(lyric, matched_at)
    default_rate = float(np.median(rates)) if rates else _DEFAULT_RATE
    if voice is not None:
        anchors = _phrase_starts(heard, voice, anchors)

    # Anchors must run forward; the alignment makes them, but the back-off
    # for unmatched first words can cross the line before.
    kept: dict[int, Anchor] = {}
    last_start = -1.0
    for li in sorted(anchors):
        s, e, r = anchors[li]
        if s <= last_start:
            s = last_start + _MIN_GAP
        kept[li] = (max(0.0, s), max(e, s), r)
        last_start = kept[li][0]
    anchors = kept

    starts: list[float | None] = [
        anchors[li][0] if li in anchors else None for li in range(len(lines))
    ]
    song_end = duration
    if voice is not None:
        last = voice.last_voiced()
        if last is not None:
            song_end = min(duration, last + 1.0) if duration else last + 1.0
    _place_unanchored(lines, lyric, starts, anchors, voice, song_end, default_rate)
    for li in range(1, len(starts)):
        starts[li] = max(starts[li] or 0.0, starts[li - 1] or 0.0)

    timed: list[Timed] = []
    for li, line in enumerate(lines):
        start = float(starts[li] or 0.0)
        following = next((s for s in starts[li + 1 :] if s is not None and s > start), None)
        limit = following - _MIN_GAP if following is not None else max(song_end, start + 1.0)
        timed.append(
            _words_of_line(
                line, lyric, heard, matched_at, start, limit, anchors.get(li), default_rate
            )
        )
    return Retimed(lines, timed, matched_w / (total_w or 1.0), len(anchors))


# A line starts where its phrase does, up to this long before its first heard
# word, when nothing else was heard in between.
_PHRASE_BACK_SEC = 2.5
# Silence this long ends a phrase; a gap shorter than this is within one.
_PHRASE_GAP_SEC = 0.25


def _phrase_starts(
    heard: Heard,
    voice: Voice,
    anchors: dict[int, Anchor],
) -> dict[int, Anchor]:
    """The anchors, each moved back to the start of the phrase its first
    heard word is sung in, when Whisper heard nothing of that phrase before
    the word. Whisper drops the first words of a phrase often, a repeated
    word most of all ("Bailando, bailando": it hears the second), and the
    line then starts where the voice does, after a silence. Never back past
    the end of the line before, nor further than _PHRASE_BACK_SEC."""
    out: dict[int, Anchor] = {}
    floor = 0.0
    everything = np.sort(heard.starts)
    for li in sorted(anchors):
        start, end, rate = anchors[li]
        phrase = voice.phrase_start(start, _PHRASE_GAP_SEC)
        if (
            phrase is not None
            and floor <= phrase < start - _PHRASE_GAP_SEC
            and start - phrase <= _PHRASE_BACK_SEC
        ):
            between = np.searchsorted(everything, [phrase, start - 0.05])
            if between[1] == between[0]:
                start = phrase
        out[li] = (start, end, rate)
        floor = voice.end_of_singing(end, _HELD_SEC)
    return out


def _weight(lyric: Lyric, matched_at: dict[int, int]) -> float:
    return sum(lyric.weights[i] for i in matched_at if not lyric.aside[i])


def _compact(
    lyric: Lyric, heard: Heard, voice: Voice | None, matched_at: dict[int, int]
) -> dict[int, int]:
    """The matches, less those of a line's words heard far from the rest of
    it: a line whose last words were matched to a chorus sung again later
    keeps only the group of its matches that weighs most. Two words heard
    one after the other with the voice going on between them are together
    however long the first is held: Whisper ended Piaf's long "Non" two
    seconds before "rien", and the line lost its first word."""
    by_line: dict[int, list[int]] = {}
    for t in sorted(matched_at):
        by_line.setdefault(lyric.token_line[t], []).append(t)
    kept: dict[int, int] = {}
    for toks in by_line.values():
        groups: list[list[int]] = [[toks[0]]]
        for a, b in zip(toks, toks[1:], strict=False):
            between = sum(lyric.weights[x] for x in range(a + 1, b))
            gap = float(heard.starts[matched_at[b]] - heard.ends[matched_at[a]])
            held = (
                voice is not None
                and matched_at[b] == matched_at[a] + 1
                and voice.voiced_between(
                    float(heard.ends[matched_at[a]]), float(heard.starts[matched_at[b]])
                )
                >= _HELD_VOICED * gap
            )
            if not held and gap > _CLUSTER_GAP_SEC + _CLUSTER_SEC_PER_SYLLABLE * between:
                groups.append([])
            groups[-1].append(b)
        best = max(groups, key=lambda g: sum(lyric.weights[t] for t in g))
        kept.update({t: matched_at[t] for t in best})
    return kept


def _anchor(
    lyric: Lyric, heard: Heard, voice: Voice | None, matched_at: dict[int, int]
) -> tuple[dict[int, Anchor], list[float]]:
    """The lines found where they are sung, {line: (start, end, pace)}, and
    each one's pace (seconds a syllable)."""
    anchors: dict[int, Anchor] = {}
    rates: list[float] = []
    for li, line in enumerate(lyric.lines):
        toks = [t for t in line.tokens if not lyric.aside[t]]
        if not toks:
            continue
        cum = 0.0
        points: list[tuple[float, float, int]] = []  # (syllables before, time, token)
        best_run = run = 0
        prev_j: int | None = None
        for t in toks:
            j = matched_at.get(t)
            if j is not None:
                points.append((cum, float(heard.starts[j]), t))
                run = run + 1 if prev_j is not None and j - prev_j <= 2 else 1
                best_run = max(best_run, run)
                prev_j = j
            else:
                run, prev_j = 0, None
            cum += lyric.weights[t]
        share = sum(lyric.weights[p[2]] for p in points) / cum if cum else 0.0
        if share < _ANCHOR_MIN_SHARE or best_run < min(_ANCHOR_MIN_RUN, len(toks)):
            continue
        first_t = points[0][1]
        if voice is not None:
            if not voice.voiced_near(first_t, _VOICE_CHECK_SEC):
                continue
            first_t = voice.rise_start(first_t, float(heard.word_ends[matched_at[points[0][2]]]))
        rate = _line_rate([(p[0], p[1]) for p in points])
        if rate is not None:
            rates.append(rate)
        end = float(heard.ends[matched_at[points[-1][2]]])
        start = _line_start(lyric, heard, matched_at, toks, points, first_t, rate, voice)
        anchors[li] = (start, end, rate or 0.0)
    return anchors, rates


def _line_start(
    lyric: Lyric,
    heard: Heard,
    matched_at: dict[int, int],
    toks: list[int],
    points: list[tuple[float, float, int]],
    first_t: float,
    rate: float | None,
    voice: Voice | None,
) -> float:
    """When a line starts whose first matched token is not its first: as
    many heard tokens before that one as the line has unmatched tokens
    before it, when those heard tokens matched nothing and are sung with it
    (misheard words, one for one), else back from the first match by the
    line's pace."""
    before = toks.index(points[0][2])
    if before == 0:
        return first_t
    j = matched_at[points[0][2]]
    used = set(matched_at.values())
    k = j - before
    near = _CLUSTER_GAP_SEC + _CLUSTER_SEC_PER_SYLLABLE * points[0][0]
    if (
        k >= 0
        and not any(x in used for x in range(k, j))
        and first_t - float(heard.starts[k]) <= near
    ):
        start = float(heard.starts[k])
        if voice is not None:
            start = voice.rise_start(start, float(heard.word_ends[k]))
        return min(start, first_t)
    return first_t - points[0][0] * (rate or _DEFAULT_RATE)


def _shifts(lines: list[Line], anchors: dict[int, Anchor]) -> tuple[list[int], list[float]]:
    """The anchored lines with stamps, in order, and how far each moved."""
    order = [li for li in sorted(anchors) if lines[li].prior is not None]
    return order, [anchors[li][0] - lines[li].prior for li in order]  # type: ignore[operator]


def _drop_outliers(lines: list[Line], anchors: dict[int, Anchor]) -> dict[int, Anchor]:
    """The anchors, less those the LRC's own stamps show are out of place.
    A song timed to another cut moves by a step where the cut is, and each
    side of the step agrees with itself; a line matched to a repeat of its
    words elsewhere agrees with neither side. Stamps that agree with nothing
    (a copy never timed right) drop nothing."""
    order, shift = _shifts(lines, anchors)
    dropped: set[int] = set()
    for k, li in enumerate(order):
        sides = [s for s in (shift[max(0, k - 2) : k], shift[k + 1 : k + 3]) if len(s) == 2]
        if not sides or any(max(s) - min(s) > _AGREE_SEC for s in sides):
            continue
        if all(abs(shift[k] - float(np.median(s))) > _OUTLIER_SEC for s in sides):
            dropped.add(li)
    return {li: v for li, v in anchors.items() if li not in dropped}


def _windows(lyric: Lyric, anchors: dict[int, Anchor]) -> tuple[np.ndarray, np.ndarray] | None:
    """For each lyric token, the earliest and latest time its heard match may
    start: its line's stamp moved by the shifts of the anchors either side
    (as far as the nearer of them disagree), _WINDOW_SEC wider. None when
    there are too few anchors with stamps to tell."""
    lines = lyric.lines
    order, shift = _shifts(lines, anchors)
    if len(order) < _WINDOW_MIN_ANCHORS:
        return None
    lo = np.full(len(lyric.keys), -np.inf)
    hi = np.full(len(lyric.keys), np.inf)
    for li, line in enumerate(lines):
        if line.prior is None:
            continue
        k = bisect.bisect_left(order, li)
        near = shift[max(0, k - 2) : k + 2]
        for t in line.tokens:
            lo[t] = line.prior + min(near) - _WINDOW_SEC
            hi[t] = line.prior + max(near) + _WINDOW_SEC
    return lo, hi


def _syllables(lyric: Lyric, line: Line) -> float:
    return sum(lyric.weights[t] for t in line.tokens) or 1.0


def _place_unanchored(
    lines: list[Line],
    lyric: Lyric,
    starts: list[float | None],
    anchors: dict[int, Anchor],
    voice: Voice | None,
    song_end: float,
    rate: float,
) -> None:
    """Fill ``starts`` for the lines between anchors, in place."""
    n = len(lines)
    li = 0
    while li < n:
        if starts[li] is not None:
            li += 1
            continue
        a = li - 1  # the anchored line before the run, or -1
        b = li
        while b < n and starts[b] is None:
            b += 1
        run = list(range(li, b))
        lo = anchors[a][1] if a >= 0 else 0.0
        if a >= 0 and voice is not None and voice.voiced.size:
            lo = voice.end_of_singing(lo, _HELD_SEC)
        hi = float(starts[b]) if b < n else song_end  # type: ignore[arg-type]
        if a < 0 and voice is not None:
            first = voice.first_voiced()
            if first is not None and first < hi:
                lo = max(lo, first - 0.2)
        lo = min(lo, hi)
        placed = _by_prior(lines, run, a, b, starts, lo, hi)
        if placed is None:
            placed = _by_voice(lines, lyric, run, lo, hi, voice, rate)
        prev = starts[a] if a >= 0 else -_MIN_GAP
        for k, t in zip(run, placed, strict=True):
            if voice is not None:
                snapped = voice.onset_near(t, _SNAP_SEC)
                if snapped is not None and lo - _MIN_GAP <= snapped <= hi - _MIN_GAP:
                    t = snapped
            t = max(t, (prev or 0.0) + _MIN_GAP)
            if b < n:
                # Never past the line after the run: the order is the text's.
                t = min(t, hi)
            starts[k] = t
            prev = t
        li = b


def _by_prior(
    lines: list[Line],
    run: list[int],
    a: int,
    b: int,
    starts: list[float | None],
    lo: float,
    hi: float,
) -> list[float] | None:
    """The run's starts from the LRC's own stamps, mapped onto the anchors
    around it, when those stamps agree with the anchors: None when they do
    not, or there are no stamps."""
    if any(lines[k].prior is None for k in run):
        return None
    pa = lines[a].prior if a >= 0 else None
    pb = lines[b].prior if b < len(lines) else None
    sa = starts[a] if a >= 0 else None
    sb = starts[b] if b < len(lines) else None
    if pa is not None and pb is not None and sa is not None and sb is not None:
        if pb <= pa or sb <= sa:
            return None
        ratio = (sb - sa) / (pb - pa)
        if abs(ratio - 1) > _PRIOR_AGREE:
            return None
        return [sa + (lines[k].prior - pa) * ratio for k in run]  # type: ignore[operator]
    # One anchor only (the run leads or ends the song): moved with it, when
    # that keeps the whole run inside the time it has.
    if pa is not None and sa is not None:
        placed = [sa + (lines[k].prior - pa) for k in run]  # type: ignore[operator]
    elif pb is not None and sb is not None:
        placed = [sb - (pb - lines[k].prior) for k in run]  # type: ignore[operator]
    else:
        return None
    if placed[0] < lo - 1.0 or placed[-1] > hi:
        return None
    return placed


def _by_voice(
    lines: list[Line],
    lyric: Lyric,
    run: list[int],
    lo: float,
    hi: float,
    voice: Voice | None,
    rate: float,
) -> list[float]:
    """The run's starts spread over the singing between ``lo`` and ``hi``
    by their syllables; evenly over the time when there is no singing there
    (lines the track does not sing: they pass quickly)."""
    weights = [_syllables(lyric, lines[k]) for k in run]
    total = sum(weights)
    sung = voice.voiced_between(lo, hi) if voice is not None else 0.0
    out: list[float] = []
    before = 0.0
    if voice is not None and sung >= 0.4 * len(run):
        # No more singing than the lines need at the usual pace: the rest of
        # the window is somebody else's (a chorus the text does not repeat).
        need = min(sung, total * rate * 1.5)
        for w in weights:
            out.append(voice.at_voiced(lo, need * before / total))
            before += w
        return out
    span = max(0.0, hi - lo)
    for w in weights:
        out.append(lo + span * before / total)
        before += w
    return out


def _words_of_line(
    line: Line,
    lyric: Lyric,
    heard: Heard,
    matched_at: dict[int, int],
    start: float,
    limit: float,
    anchor: Anchor | None,
    default_rate: float,
) -> Timed:
    """Each piece's start in a line starting at ``start`` and over by
    ``limit``, and the line's end: where its words were heard, the rest
    spread between those by syllables."""
    n_pieces = len(line.pieces)
    limit = max(limit, start)
    rate = (anchor[2] if anchor and anchor[2] else 0.0) or default_rate
    piece_w = [0.0] * n_pieces
    known: dict[int, float] = {}
    for t in line.tokens:
        p = lyric.token_piece[t]
        j = matched_at.get(t) if anchor is not None else None
        if j is not None and p not in known:
            # The piece starts when its first matched token is heard, less
            # the syllables of the piece before that token.
            known[p] = float(heard.starts[j]) - piece_w[p] * rate
        piece_w[p] += lyric.weights[t]
    piece_w = [w or 0.5 for w in piece_w]
    cum = [0.0]
    for w in piece_w:
        cum.append(cum[-1] + w)
    points = [(0, start)]
    for p in sorted(known):
        if p == 0:
            continue
        points.append((p, min(max(known[p], points[-1][1]), limit)))
    if anchor is not None:
        last_known = points[-1][0]
        own = cum[last_known + 1] - cum[last_known] if last_known < n_pieces else 0.0
        tail = cum[n_pieces] - cum[last_known + 1] if last_known + 1 <= n_pieces else 0.0
        end = max(anchor[1], points[-1][1] + own * rate) + tail * rate
    else:
        end = start + cum[n_pieces] * rate
    end = max(min(end, limit), points[-1][1])
    points.append((n_pieces, end))
    words: list[float] = []
    k = 0
    for p in range(n_pieces):
        while k + 1 < len(points) and points[k + 1][0] <= p:
            k += 1
        p0, t0 = points[k]
        if p0 == p:
            words.append(t0)
            continue
        p1, t1 = points[k + 1] if k + 1 < len(points) else (n_pieces, end)
        span_w = cum[p1] - cum[p0]
        words.append(t0 + (t1 - t0) * ((cum[p] - cum[p0]) / span_w if span_w > 0 else 0.0))
    for p in range(1, n_pieces):
        words[p] = max(words[p], words[p - 1])
    if words:
        words[0] = start
    return Timed(start=start, end=max(end, start), words=words, anchored=anchor is not None)


# ── LRC out ──

# A pause this long after a line clears it (an empty stamped line).
_CLEAR_AFTER_SEC = 4.0


def _stamp(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    minutes, rest = divmod(centis, 6000)
    return f"{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def to_lrc(result: Retimed) -> str:
    """The retimed lines as enhanced LRC: each line its start, each piece its
    word stamp, the last its end; an empty line where a long pause follows."""
    rows = sorted(zip(result.lines, result.timed, strict=True), key=lambda r: r[1].start)
    out: list[str] = []
    for k, (line, timed) in enumerate(rows):
        text = line.text
        parts = [f"[{_stamp(timed.start)}]"]
        cursor = 0
        for (a, _b), t in zip(line.pieces, timed.words, strict=True):
            parts.append(text[cursor:a])
            parts.append(f"<{_stamp(t)}>")
            cursor = a
        parts.append(text[cursor:])
        if line.pieces:
            parts.append(f"<{_stamp(timed.end)}>")
        out.append("".join(parts))
        following = rows[k + 1][1].start if k + 1 < len(rows) else None
        if following is None or following - timed.end >= _CLEAR_AFTER_SEC:
            out.append(f"[{_stamp(timed.end + 0.5)}]")
    return "\n".join(out)


# ── the gate ──


def onset_fit(starts: list[float], voice: Voice) -> float:
    """The share of line starts with the voice rising within 0.25 s."""
    if not starts or not voice.strength.size:
        return 0.0
    return sum(voice.onset_near(t, 0.25) is not None for t in starts) / len(starts)


def retime_gate(result: Retimed) -> bool:
    """Whether a result is good enough to show: enough of the lyrics heard,
    and enough lines found where they are sung."""
    lines = result.counted
    return (
        lines > 0
        and result.matched >= LYRICS_RETIME_MIN_MATCHED
        and result.lines_matched / lines >= LYRICS_RETIME_MIN_LINES_SHARE
    )


# ── the transcript ──


def transcript_words(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The timed words of a worker answer: [{"text", "start", "end"}].

    Looser than the segments transcribe.py keeps as lyrics: a chorus sung
    four times compresses as well as a loop Whisper made up, so the
    compression ratio drops nothing here. Only a window Whisper itself took
    for silence goes; a word made up over an instrumental anchors nothing,
    since an anchor needs the voice (Voice.voiced_near)."""
    from app.pipeline.transcribe import _LOGPROB_FLOOR, _NO_SPEECH_PROB, _number, _words

    words: list[dict[str, Any]] = []
    segments = result.get("segments")
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict):
            continue
        no_speech = _number(segment.get("no_speech_prob")) or 0.0
        logprob = _number(segment.get("avg_logprob")) or 0.0
        if no_speech > _NO_SPEECH_PROB and logprob < _LOGPROB_FLOOR:
            continue
        words.extend(_words(segment, words[-1]["start"] if words else 0.0))
    return words


def transcript_path(job_dir: Path) -> Path:
    return job_dir / LYRICS_TRANSCRIPT_FILE


def _vocals_stamp(job_dir: Path) -> list[int] | None:
    """The vocals stem's size and modification time: what a kept transcript
    was heard from. A stem made again (a re-separation writing it in place)
    no longer matches, and its transcript is heard afresh."""
    try:
        st = (job_dir / "stems" / "vocals.wav").stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def save_transcript(
    job_dir: Path,
    result: dict[str, Any] | None = None,
    *,
    words: list[dict[str, Any]] | None = None,
    language: str | None = None,
    refined: bool = False,
) -> None:
    """Keep heard words beside the lyrics, for timing them again without a
    second pass: a worker answer's, or ``words`` already taken from one
    (``refined``: with the lines a first timing missed heard again). Best
    effort."""
    from app.pipeline.lyrics_lookup import _write_json_atomic

    if result is not None:
        words = transcript_words(result)
        language = result.get("language") if isinstance(result.get("language"), str) else None
    data = {
        "v": 1,
        "vocals": _vocals_stamp(job_dir),
        "language": language,
        "refined": refined,
        "words": [[round(w["start"], 3), round(w["end"], 3), w["text"]] for w in words or []],
    }
    try:
        _write_json_atomic(transcript_path(job_dir), data)
    except OSError:
        logger.info("could not keep the transcript in %s", job_dir.name, exc_info=True)


def read_transcript(job_dir: Path) -> dict[str, Any] | None:
    """The kept transcript: {"language", "refined", "words": [{"text",
    "start", "end"}]}, or None when there is none or it is damaged."""
    try:
        data = json.loads(transcript_path(job_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("words"), list):
        return None
    # Heard from another vocals stem than the one there now. A transcript kept
    # before this was recorded has no stamp and is taken as it is.
    kept = data.get("vocals")
    if kept is not None and kept != _vocals_stamp(job_dir):
        return None
    words = []
    for row in data["words"]:
        if (
            isinstance(row, list)
            and len(row) == 3
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in row[:2])
            and isinstance(row[2], str)
            and math.isfinite(row[0])
            and math.isfinite(row[1])
        ):
            words.append({"start": float(row[0]), "end": float(row[1]), "text": row[2]})
    language = data.get("language")
    return {
        "language": language if isinstance(language, str) else None,
        "refined": data.get("refined") is True,
        "words": words,
    }


# ── hearing the missed lines again ──

# A run of at least this many lines not found is heard again when there is
# at least this much singing where it goes, heard this much wider on each
# side (only the words inside the run's own time replace the first ones).
_CLIP_MIN_LINES = 2
_CLIP_MIN_SUNG_SEC = 3.0
_CLIP_PAD_SEC = 0.4


def missed_clips(
    result: Retimed, voice: Voice | None, duration: float
) -> list[tuple[float, float]]:
    """Where the runs of lines not found go, as (start, end) seconds of the
    stem, when there is singing there: between the end of the line found
    before and the start of the one found after."""
    if voice is None:
        return []
    timed = sorted(result.timed, key=lambda t: t.start)
    clips: list[tuple[float, float]] = []
    k = 0
    while k < len(timed):
        if timed[k].anchored:
            k += 1
            continue
        j = k
        while j < len(timed) and not timed[j].anchored:
            j += 1
        lo = timed[k - 1].end if k > 0 else 0.0
        hi = timed[j].start if j < len(timed) else (duration or voice.hop * voice.voiced.size)
        if j - k >= _CLIP_MIN_LINES and voice.voiced_between(lo, hi) >= _CLIP_MIN_SUNG_SEC:
            clips.append((lo, hi))
        k = j
    return clips


def merge_words(
    words: list[dict[str, Any]], extra: list[dict[str, Any]], clips: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """``words`` with those inside ``clips`` replaced by ``extra``, heard
    again there, in time order."""

    def inside(w: dict[str, Any]) -> bool:
        return any(a <= w["start"] < b for a, b in clips)

    kept = [w for w in words if not inside(w)]
    return sorted(kept + [w for w in extra if inside(w)], key=lambda w: w["start"])


# A line found only when heard again is kept where the copy's own stamps,
# moved as the lines found around it were, put it within this much.
_CLIP_AGREE_SEC = 1.5


def better(new: Retimed, old: Retimed, pairs: list[tuple[float | None, str]]) -> bool:
    """Whether a timing with missed lines heard again is the better one: it
    finds more lines, and every line it finds that the first did not sits
    where the copy's stamps and the lines both found in the same place
    around it say. A chorus
    sung more often than the copy has it is heard again too, and its later
    times must not take the copy's lines."""
    if (new.lines_matched, new.matched) <= (old.lines_matched, old.matched):
        return False
    # The lines both found in the same place: what the others go by.
    shifts = [
        (k, t.start - p)
        for k, (t, n, (p, _x)) in enumerate(zip(old.timed, new.timed, pairs, strict=True))
        if t.anchored and n.anchored and abs(t.start - n.start) < 0.3 and p is not None
    ]
    for k, (t, (prior, _x)) in enumerate(zip(new.timed, pairs, strict=True)):
        if not t.anchored or old.timed[k].anchored:
            continue
        if prior is None or not shifts:
            return False
        near = sorted(shifts, key=lambda ks: abs(ks[0] - k))[:4]
        expected = float(np.median([sh for _k, sh in near]))
        if abs(t.start - prior - expected) > _CLIP_AGREE_SEC:
            return False
    return True


# ── the language ──

# The commonest words of each language in Latin letters StemDeck's lyrics
# come in, only those no other of them shares: a text in one of them uses a
# handful a line. Whisper decides the language from 30 s of the vocals, and
# on a song with an English-sounding hook it can decide wrong (Nena
# "Leuchtturm": English, a transcription that matched 15% of the lyrics; in
# German, 89%).
_COMMON_WORDS = {
    "en": "the you and i'm don't your that with what this love know just can't",
    "de": "ich und nicht ist die der das mich dich mir dir wir auch noch wenn",
    "fr": "je tu et le les est pas qui une moi toi dans pour c'est mon ne j'ai",
    "es": "yo y el los las pero más como está eres tú mí porque",
    "pt": "eu você e não é meu minha uma com mais tem sou quando",
    "pl": "nie się że jak mnie już tylko jest ci dla czy",
    "id": "aku kau kamu dan yang ini itu untuk dengan cinta hati akan",
}
_COMMON = {lang: frozenset(words.split()) for lang, words in _COMMON_WORDS.items()}
# At least this many common words, and this many times the next language's.
_LANGUAGE_MIN_HITS = 8
_LANGUAGE_MIN_LEAD = 1.5
_WORDS = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")


def text_language(text: str) -> str | None:
    """The language of lyrics in Latin letters, from their commonest words,
    or None when they do not show it clearly."""
    words = _WORDS.findall(text.casefold())
    scores = sorted(
        ((sum(w in common for w in words), lang) for lang, common in _COMMON.items()),
        reverse=True,
    )
    (best, lang), (second, _other) = scores[0], scores[1]
    if best >= _LANGUAGE_MIN_HITS and best >= _LANGUAGE_MIN_LEAD * second:
        return lang
    return None


def script_language(text: str) -> str | None:
    """The language a text's script alone shows: Korean for Hangul,
    Japanese for kana, Chinese for Chinese characters with neither. None for
    anything else."""
    hangul = kana = han = 0
    for ch in text:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7AF:
            hangul += 1
        elif 0x3040 <= cp <= 0x30FF:
            kana += 1
        elif _han_or_kana(ch):
            han += 1
    letters = sum(ch.isalpha() for ch in text) or 1
    if hangul / letters > 0.3:
        return "ko"
    if kana / letters > 0.1:
        return "ja"
    if han / letters > 0.3:
        return "zh"
    return None


# ── the formats ──


def lyric_pairs(entry: dict[str, Any]) -> list[tuple[float | None, str]]:
    """The lyrics' lines as (stamp, text), in time order, one per stamp;
    (None, text) for text-only lyrics."""
    if entry.get("synced"):
        return synced_lines(entry["synced"])
    return [(None, text) for text in plain_lines(entry.get("plain") or "")]


def lyric_line_texts(entry: dict[str, Any]) -> list[str]:
    """The lyrics' own lines, as any timing of them must give them."""
    return [text for _t, text in lyric_pairs(entry)]


_USER_ROW = re.compile(r"\s*\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\](.*)")
_USER_WORD = re.compile(r"<(\d{1,3}):(\d{2}(?:\.\d{1,3})?)>")


def clean_user_synced(value: Any, lines: list[str]) -> str | None:
    """A manual timing as kept, or None when it is not one of these lyrics:
    LRC whose every non-empty line is one [mm:ss.xx] stamp and text (enhanced
    <mm:ss.xx> word stamps allowed, no other tag), the texts exactly
    ``lines`` in order (lyric_line_texts), and the line stamps never going
    back in time. Empty stamped lines (a pause) are allowed anywhere."""
    if not isinstance(value, str) or not lines or len(value) > LYRICS_TIMED_MAX_CHARS:
        return None
    rows = [raw for raw in value.splitlines() if raw.strip()]
    if len(rows) > LYRICS_USER_SYNCED_MAX_LINES:
        return None
    texts: list[str] = []
    last = -1.0
    for raw in rows:
        m = _USER_ROW.fullmatch(raw)
        if not m or float(m.group(2)) >= 60:
            return None
        seconds = int(m.group(1)) * 60 + float(m.group(2))
        if seconds < last:
            return None
        last = seconds
        rest = m.group(3)
        if any(float(w.group(2)) >= 60 for w in _USER_WORD.finditer(rest)):
            return None
        # Word stamps only: no other tag, and no stamp of another shape.
        stripped = _WORD_STAMP.sub("", rest)
        if re.search(r"\[\d|<\d", stripped):
            return None
        text = line_text(rest)
        if text:
            texts.append(text)
    if texts != lines:
        return None
    return "\n".join(raw.strip() for raw in rows)


ALIGN_METHODS = frozenset(("whisper",))


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def clean_aligned(value: Any, lines: list[str]) -> dict[str, Any] | None:
    """The "aligned" of lyrics.json as kept, or None when it is malformed or
    not a timing of ``lines`` (the lyrics' own, lyric_line_texts)."""
    if not isinstance(value, dict) or value.get("method") not in ALIGN_METHODS:
        return None
    synced = value.get("synced")
    if not isinstance(synced, str) or not synced or len(synced) > LYRICS_TIMED_MAX_CHARS:
        return None
    matched, at = value.get("matched"), value.get("at")
    for number in (matched, at):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
    if not (math.isfinite(matched) and 0 <= matched <= 1 and math.isfinite(at) and at >= 0):
        return None
    lines_matched, count = _count(value.get("lines_matched")), _count(value.get("lines"))
    if lines_matched is None or count is None or lines_matched > count:
        return None
    if not lines or [text for _t, text in synced_lines(synced)] != lines:
        return None
    return {
        "synced": synced,
        "method": value["method"],
        "matched": float(matched),
        "lines_matched": lines_matched,
        "lines": count,
        "at": float(at),
    }


def aligned_entry(result: Retimed) -> dict[str, Any]:
    return {
        "synced": to_lrc(result),
        "method": "whisper",
        "matched": round(result.matched, 3),
        "lines_matched": result.lines_matched,
        "lines": len(result.lines),
        "at": round(time.time(), 3),
    }


# ── running it ──

_STAGE = "Timing lyrics to the vocals..."
# The share of a run's progress the transcription takes; the rest is quick.
_TRANSCRIBE_SHARE = 0.9
# A new timing is kept over the current one only when it puts at most this
# much smaller a share of its line starts on a rise of the voice.
_NO_WORSE_MARGIN = 0.05
# A current timing counts as confirmed when a whole-song shift of at most
# this much fits it.
_CONFIRMED_SHIFT_SEC = 0.3


class RetimeFailed(Exception):
    """There is nothing to time, or the transcription failed."""


@dataclass
class Outcome:
    state: str  # "done" or "unsure"
    matched: float
    lines_matched: int
    lines: int
    # The timing itself, for lyrics the browser keeps (nothing is written).
    aligned: dict[str, Any] | None = None


def _language(entry: dict[str, Any], text: str) -> str:
    """The language to transcribe in: the lyrics' own when known, else what
    their script or their words show, else "" for Whisper to decide."""
    language = entry.get("language")
    if isinstance(language, str) and re.fullmatch(r"[a-z]{2,3}", language):
        return language
    return script_language(text) or text_language(text) or ""


def _hear(
    job: Any,
    job_dir: Path,
    language: str,
    cancelled: Callable[[], bool],
    report: Callable[[str, int | None], None] | None,
    clips: list[tuple[float, float]] | None = None,
) -> dict[str, Any] | None:
    """A worker answer for the job's vocals, or only ``clips`` of them; None
    when the worker failed. Raises RetimeFailed without vocals, JobCancelled."""
    from app.pipeline.transcribe import _device, _run_worker, _spawn_worker_cmd

    vocals = job_dir / "stems" / "vocals.wav"
    if not vocals.is_file():
        raise RetimeFailed("no vocals stem")
    cmd = _spawn_worker_cmd(vocals, _device(job))
    if language:
        cmd += ["--language", language]
    if clips:
        # Heard a little wider than the run, for the words at its edges.
        padded = [(max(0.0, a - _CLIP_PAD_SEC), b + _CLIP_PAD_SEC) for a, b in clips]
        cmd += ["--clips", ",".join(f"{a:.2f}:{b:.2f}" for a, b in padded)]
    return _run_worker(job, cmd, cancelled=cancelled, report=report)


def _transcribe(
    job: Any,
    job_dir: Path,
    language: str,
    cancelled: Callable[[], bool],
    report: Callable[[str, int | None], None],
) -> tuple[list[dict[str, Any]], bool]:
    """The heard words, and whether missed lines were heard again already:
    the kept transcript, else a new one (kept too). Raises RetimeFailed, or
    JobCancelled."""
    kept = read_transcript(job_dir)
    if kept is not None and kept["words"]:
        return kept["words"], kept["refined"]
    result = _hear(job, job_dir, language, cancelled, report)
    if result is None:
        raise RetimeFailed("transcription failed")
    save_transcript(job_dir, result)
    return transcript_words(result), False


def _current_starts(entry: dict[str, Any], voice: Voice) -> list[float] | None:
    """The line starts the lyrics show now, when the voice confirms them:
    their own stamps with the user's offset, moved by a whole-song shift of
    at most _CONFIRMED_SHIFT_SEC. None when nothing confirms them."""
    from app.pipeline.lyrics_align import estimate_offset

    if not entry.get("synced"):
        return None
    offset = float(entry.get("offset_sec") or 0.0)
    starts = sorted(t + offset for t, _text in synced_lines(entry["synced"]))
    shift = estimate_offset(starts, voice.strength, voice.hop)
    if shift is None or abs(shift) > _CONFIRMED_SHIFT_SEC:
        return None
    return [t + shift for t in starts]


def no_worse(entry: dict[str, Any], result: Retimed, voice: Voice | None) -> bool:
    """Whether ``result`` is at least as good as the timing the lyrics have,
    when the voice confirms that timing: judged by the share of line starts
    on a rise of the voice. True when nothing confirms the current one."""
    if voice is None:
        return True
    current = _current_starts(entry, voice)
    if current is None:
        return True
    new = onset_fit([t.start for t in result.timed], voice)
    return new >= onset_fit(current, voice) - _NO_WORSE_MARGIN


def retime_job(
    job: Any,
    job_dir: Path,
    *,
    cancelled: Callable[[], bool],
    report: Callable[[float], None] | None = None,
    lyrics: dict[str, Any] | None = None,
) -> Outcome:
    """Time the job's lyrics to its vocals and keep the result as their
    "aligned" when it passes the gate (retime_gate, no_worse). Blocking:
    may run a transcription of minutes. Raises RetimeFailed when there is
    nothing to time or the transcription failed, JobCancelled when
    ``cancelled()`` turned true; the lyrics are then left as they were.

    ``lyrics``, {"synced", "plain"}, are lyrics the browser keeps rather
    than lyrics.json: timed the same way, and the result is only answered
    (Outcome.aligned), never written."""
    from app.core.models import JobCancelled
    from app.pipeline.lyrics_align import _envelope
    from app.pipeline.lyrics_lookup import _OFFSET_LOCK, read_lyrics, write_lyrics

    entry = read_lyrics(job_dir) if lyrics is None else lyrics
    if entry is None:
        raise RetimeFailed("no lyrics")
    pairs = lyric_pairs(entry)
    if not pairs:
        raise RetimeFailed("no lines")

    def progress(phase: str, pct: int | None) -> None:
        if report is not None and phase == "transcribe" and pct is not None:
            report(_TRANSCRIBE_SHARE * pct / 100)

    language = _language(entry, " ".join(text for _t, text in pairs))
    words, refined = _transcribe(job, job_dir, language, cancelled, progress)
    if cancelled():
        raise JobCancelled()
    envelope = _envelope(job_dir / "stems")
    voice = Voice.of(*envelope) if envelope is not None else None
    duration = float(getattr(job, "duration_sec", 0) or entry.get("duration") or 0)
    result = retime_lines(pairs, words, voice, duration)
    clips = [] if refined else missed_clips(result, voice, duration)
    if clips:
        # The lines not found, heard again on their own: Whisper often
        # skips a chorus sung once more at the end, and hears it alone.
        extra = _hear(job, job_dir, language, cancelled, None, clips)
        if extra is not None:
            merged = merge_words(words, transcript_words(extra), clips)
            again = retime_lines(pairs, merged, voice, duration)
            if better(again, result, pairs):
                result, words = again, merged
            save_transcript(job_dir, words=words, language=language or None, refined=True)
        if cancelled():
            raise JobCancelled()
    outcome = Outcome("unsure", round(result.matched, 3), result.lines_matched, len(result.lines))
    if not retime_gate(result) or not no_worse(entry, result, voice):
        logger.info(
            "[%s] lyrics timing unsure: %.0f%% heard, %d of %d lines found",
            job.id,
            result.matched * 100,
            result.lines_matched,
            result.counted,
        )
        return outcome
    aligned = aligned_entry(result)
    if lyrics is not None:
        outcome.state, outcome.aligned = "done", aligned
        return outcome
    with _OFFSET_LOCK:
        if cancelled():
            raise JobCancelled()
        now = read_lyrics(job_dir)
        # Other lyrics since (another version, letters mended): this timing
        # is not theirs.
        if now is None or lyric_line_texts(now) != [text for _t, text in pairs]:
            raise RetimeFailed("the lyrics changed meanwhile")
        if not write_lyrics(job, job_dir, {**now, "aligned": aligned}):
            raise RetimeFailed("could not write lyrics")
    logger.info(
        "[%s] lyrics timed line by line: %.0f%% heard, %d of %d lines found",
        job.id,
        result.matched * 100,
        result.lines_matched,
        result.counted,
    )
    outcome.state = "done"
    return outcome


def wants_retime(job: Any, job_dir: Path) -> bool:
    """Whether a job just separated gets its lyrics timed line by line: LRCLIB
    lyrics that nothing has confirmed the timing of line by line (a version
    of another length, moved by a whole-song shift or not, or one the
    track's length the voice does not confirm), not aligned by hand nor
    timed already, under the transcribe_lyrics setting (auto: a CUDA job
    only)."""
    from app.core.settings import transcribe_lyrics_enabled
    from app.pipeline.lyrics_align import detect_offset
    from app.pipeline.lyrics_lookup import read_lyrics

    if not transcribe_lyrics_enabled(getattr(job, "compute_device", None)):
        return False
    entry = read_lyrics(job_dir)
    if entry is None or entry["source"] != "lrclib" or not entry["synced"]:
        return False
    if any(key in entry for key in ("aligned", "user_synced", "offset_sec")):
        return False
    # "shifted": a copy of another cut moved by one amount, which a cut
    # inside the song (a verse more or less) leaves wrong from there on.
    if entry["timing"] in ("unverified", "shifted"):
        return True
    return detect_offset(entry["synced"], job_dir / "stems") is None


def retime_after_separation(job: Any, job_dir: Path) -> bool:
    """The pipeline's last step: time the lyrics line by line when
    wants_retime. True when "aligned" was written. Never raises but
    JobCancelled: lyrics are optional, and a failure here must not cost the
    user a separation that has already succeeded."""
    from app.core.models import JobCancelled, _set

    try:
        if job.cancel_requested:
            raise JobCancelled()
        if not wants_retime(job, job_dir):
            return False
        _set(job, stage=_STAGE)
        outcome = retime_job(job, job_dir, cancelled=lambda: job.cancel_requested)
        return outcome.state == "done"
    except JobCancelled:
        raise
    except RetimeFailed as exc:
        logger.info("[%s] lyrics not timed: %s", job.id, exc)
        return False
    except Exception:
        logger.exception("[%s] lyrics timing failed", job.id)
        return False


# ── on demand ──


@dataclass
class RetimeRun:
    """One on-demand run of a job, for GET .../lyrics/retime: "running"
    until it ends "done", "unsure", "failed" or, cancelled, "idle"."""

    state: str = "running"
    progress: float = 0.0
    outcome: Outcome | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    # Set once it holds the pipeline lock: a run cancelled while waiting for
    # it is over at once.
    started: bool = False
    # Set when its task has finished, whatever the outcome: from then on it
    # holds no file of the job open (wait_run_ended).
    ended: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state}
        if self.state == "running":
            out["progress"] = round(self.progress, 3)
        if self.outcome is not None:
            out["matched"] = self.outcome.matched
            out["lines_matched"] = self.outcome.lines_matched
            out["lines"] = self.outcome.lines
            if self.outcome.aligned is not None:
                out["aligned"] = self.outcome.aligned
        return out


_RUNS: dict[str, RetimeRun] = {}
_RUNS_LOCK = threading.Lock()


def claim_run(job_id: str) -> RetimeRun | None:
    """A new run for ``job_id``, or None while one is running already."""
    with _RUNS_LOCK:
        current = _RUNS.get(job_id)
        if current is not None and current.state == "running":
            return None
        run = RetimeRun()
        _RUNS[job_id] = run
        return run


def current_run(job_id: str) -> RetimeRun | None:
    with _RUNS_LOCK:
        return _RUNS.get(job_id)


def cancel_run(job_id: str) -> bool:
    """Ask a running run of ``job_id`` to stop. True when one was running."""
    with _RUNS_LOCK:
        run = _RUNS.get(job_id)
        if run is None or run.state != "running":
            return False
        run.cancel.set()
        if not run.started:
            run.state = "idle"
        return True


def wait_run_ended(job_id: str, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for a run of ``job_id`` that got as far
    as the vocals to let go of them. True when none holds them now. Deleting
    a job waits on this: on Windows a file the transcription still has open
    cannot be removed, and the delete would fail and leave the folder."""
    with _RUNS_LOCK:
        run = _RUNS.get(job_id)
    if run is None or not run.started:
        return True
    return run.ended.wait(timeout)


def forget_run(job_id: str) -> None:
    with _RUNS_LOCK:
        _RUNS.pop(job_id, None)


def saved_view(entry: dict[str, Any] | None) -> dict[str, Any]:
    """The state of a job with no run since the server started: "done" when
    its lyrics carry a line-by-line timing, else "idle"."""
    aligned = (entry or {}).get("aligned")
    if not aligned:
        return {"state": "idle"}
    return {
        "state": "done",
        "matched": aligned["matched"],
        "lines_matched": aligned["lines_matched"],
        "lines": aligned["lines"],
    }
