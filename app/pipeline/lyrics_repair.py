"""Letters given back to lyrics that lost them.

Some LRCLIB copies of a song lost every letter outside ASCII on their way in:
"Znowu biegne bo co cigle kae biec" for "Znowu biegnę bo coś ciągle każe
biec" (Natalia Kukulska, "W biegu"). Each such letter was dropped ("każe" as
"kae") or folded to its base ("biegnę" as "biegne"), and both happen in one
copy. The timing of such a copy is as good as any other's, so it is kept, and
only its words are mended, from a reference that has the letters: an intact
copy of the same song on LRCLIB (lyrics_lookup.py), or failing that what
Whisper heard in the vocals stem (transcribe.py).

A word is only ever replaced by a reference word that strips down to it, so a
reference that misheard a word, or is another song, cannot put a wrong word
in: at worst a word stays as it was. The two word lists are aligned in order
(a longest common subsequence under that match), and a pair counts only when
one of its neighbours is aligned too.

Whether a copy with no intact reference lost its letters cannot be read from
its text alone. looks_stripped() says whether it is worth asking the audio:
no letter outside ASCII over a real text, and not English. The rest of the
decision is the transcription's (transcribe.py).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core.config import (
    LYRICS_REPAIR_ALIGN_MAX_CELLS,
    LYRICS_REPAIR_MIN_ALIGNED,
    LYRICS_REPAIR_MIN_RESTORED,
    LYRICS_REPAIR_MIN_RESTORED_SHARE,
    LYRICS_REPAIR_VOCABULARY_MIN,
    LYRICS_STRIPPED_CHECK_MIN_WORDS,
    LYRICS_STRIPPED_ENGLISH_MAX_SHARE,
)

# LRC time stamps and header tags, which say nothing about the words: the
# same as lyrics_lookup._LRC_TAGS, which imports this module.
_LRC_TAGS = re.compile(r"\[[^\]\n]*\]|<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")

# Letters with no decomposition that a stripping tool may still write as a
# base letter ("ł" as "l"), besides dropping them.
_FOLD = {
    "ł": "l",
    "ø": "o",
    "đ": "d",
    "ħ": "h",
    "ı": "i",
    "ß": "ss",
    "æ": "ae",
    "œ": "oe",
    "þ": "th",
    "ð": "d",
    "ŀ": "l",
}

# Words common in English lyrics and not a word of any language whose
# spelling carries accents: a copy with enough of them is English, and
# English has nothing to lose. Short words many languages share ("a", "i",
# "to", "me", "no", "on", "die", "will", "for", "her", "was") are left out, so
# that a Polish or German copy never reads as English.
_ENGLISH = frozenset(
    [
        "the",
        "you",
        "your",
        "and",
        "that",
        "what",
        "with",
        "this",
        "when",
        "know",
        "just",
        "like",
        "but",
        "they",
        "she",
        "him",
        "his",
        "are",
        "have",
        "been",
        "can",
        "would",
        "there",
        "from",
        "into",
        "out",
        "get",
        "got",
        "never",
        "love",
        "yeah",
        "baby",
        "gonna",
        "wanna",
        "cause",
        "don",
        "it",
        "how",
        "now",
        "where",
        "who",
        "why",
        "could",
        "should",
        "every",
        "some",
        "thing",
        "things",
        "think",
        "feel",
        "make",
        "time",
        "night",
        "heart",
        "tonight",
        "away",
        "down",
        "again",
        "our",
        "their",
        "them",
        "then",
        "than",
        "only",
    ]
)


def _ascii_only(text: str) -> str:
    return "".join(ch for ch in text if ord(ch) < 128)


def _base(ch: str) -> str:
    """What a letter outside ASCII is folded to, or "" when it has no base."""
    return _FOLD.get(ch) or _ascii_only(unicodedata.normalize("NFD", ch))


def _letter(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LM"


def word_spans(text: str) -> list[tuple[int, int]]:
    """Where the words of ``text`` are, time stamps and tags left out: the
    runs of letters and their marks, as in lyrics_lookup._word_runs."""
    masked = _LRC_TAGS.sub(lambda m: " " * len(m.group()), text)
    spans: list[tuple[int, int]] = []
    start = -1
    for i, ch in enumerate(masked + " "):
        if _letter(ch):
            start = i if start < 0 else start
        elif start >= 0:
            spans.append((start, i))
            start = -1
    return spans


def words_of(text: str) -> list[str]:
    """The words of ``text``, NFC, in order, time stamps left out."""
    text = unicodedata.normalize("NFC", text or "")
    return [text[a:b] for a, b in word_spans(text)]


def looks_stripped(text: str) -> bool:
    """Whether a copy is worth checking against the audio for lost letters:
    at least LYRICS_STRIPPED_CHECK_MIN_WORDS words, not one letter outside
    ASCII among them, and not English (fewer than
    LYRICS_STRIPPED_ENGLISH_MAX_SHARE of them common English words).

    True says nothing about the language yet: a song in Indonesian, or
    Russian written in Latin letters, looks the same. Only the audio can tell,
    and transcribe.py asks it before anything is changed."""
    words = [w.lower() for w in words_of(text)]
    if len(words) < LYRICS_STRIPPED_CHECK_MIN_WORDS:
        return False
    if any(not w.isascii() for w in words):
        return False
    english = sum(w in _ENGLISH for w in words)
    return english < len(words) * LYRICS_STRIPPED_ENGLISH_MAX_SHARE


def _strips_to(reference: str, word: str) -> bool:
    """Whether ``reference`` gives ``word`` with each of its letters outside
    ASCII kept, folded to its base or dropped, and every other letter kept.

    Walked a letter at a time over the set of places in ``word`` reached so
    far, so the cost is at most the product of the two lengths. A regular
    expression of optional groups did the same by backtracking, which a word
    of forty accented letters in someone's LRCLIB upload turned into hours of
    matching with the interpreter locked."""
    reached = {0}
    for ch in reference:
        after: set[int] = set()
        base = "" if ch.isascii() else _base(ch)
        for at in reached:
            if word.startswith(ch, at):
                after.add(at + len(ch))
            if not ch.isascii():
                after.add(at)
                if base and word.startswith(base, at):
                    after.add(at + len(base))
        if not after:
            return False
        reached = after
    return len(word) in reached


class _Stripper:
    """Whether a reference word strips down to a word."""

    def restores(self, reference: str, word: str) -> bool:
        """Whether ``reference`` (lowercase) is ``word`` (lowercase) with the
        letters it lost: each of its letters outside ASCII kept, folded or
        dropped gives ``word``. Never for a word left with fewer than two
        letters, or half the reference's, unless it is all of it folded: a
        lone "e" is not "że"."""
        if reference == word or reference.isascii() or not _strips_to(reference, word):
            return False
        if len(word) >= max(2, math.ceil(len(reference) / 2)):
            return True
        return word == "".join(ch if ch.isascii() else _base(ch) for ch in reference)

    def restores_short(self, reference: str, word: str) -> bool:
        """Whether ``word`` is shorter than restores() allows yet strips
        down from ``reference``: "e" from "że". Taken only between two
        neighbours paired in order, where no sung vowel is heard as "że"."""
        return (
            reference != word
            and not reference.isascii()
            and _strips_to(reference, word)
            and not self.restores(reference, word)
        )

    def matches(self, reference: str, word: str) -> bool:
        return (
            reference == word
            or self.restores(reference, word)
            or self.restores_short(reference, word)
        )


def _align(words: list[str], reference: list[str], stripper: _Stripper) -> list[tuple[int, int]]:
    """The longest in-order pairing of ``words`` with ``reference`` (both
    lowercase) under _Stripper.matches, as (word index, reference index)."""
    n, m = len(words), len(reference)
    if not n or not m or n * m > LYRICS_REPAIR_ALIGN_MAX_CELLS:
        return []
    ids: dict[str, int] = {}
    wid = [ids.setdefault(w, len(ids)) for w in words]
    rid = [ids.setdefault(r, len(ids)) for r in reference]
    same: dict[tuple[int, int], bool] = {}

    def ok(i: int, j: int) -> bool:
        key = (wid[i], rid[j])
        if key not in same:
            same[key] = stripper.matches(reference[j], words[i])
        return same[key]

    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row, below = table[i], table[i + 1]
        for j in range(m - 1, -1, -1):
            if ok(i, j):
                row[j] = below[j + 1] + 1
            else:
                row[j] = max(below[j], row[j + 1])
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if ok(i, j) and table[i][j] == table[i + 1][j + 1] + 1:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _cased_like(reference: str, word: str) -> str:
    """``reference`` in the case ``word`` is written in."""
    lower = reference.lower()
    if len(word) > 1 and word.isupper():
        return reference.upper()
    if word[:1].isupper():
        return lower[:1].upper() + lower[1:]
    return lower


@dataclass(frozen=True)
class Restoration:
    """``text`` with letters given back to ``restored`` of its ``words``
    words, ``aligned`` of which were paired with a reference word at all."""

    text: str
    words: int
    aligned: int
    restored: int


def restore_letters(text: str, reference: list[str]) -> Restoration:
    """``text`` (LRC or plain) with the letters its words lost taken from
    ``reference``, the words of the same lyrics with them, in order.

    A word is replaced only by a reference word that strips down to it,
    paired with it in order and with a neighbour paired too. Any other word
    (the reference skipped its line, misheard a word beside it, or a chorus
    comes round again) takes the spelling those pairs gave it elsewhere, when
    they gave it one spelling, at least LYRICS_REPAIR_VOCABULARY_MIN times,
    and never kept it as it is: "sie" is "się" all through a song, while
    "co" that is sometimes "coś" is left alone. Everything else, time stamps
    included, stays as it was."""
    text = unicodedata.normalize("NFC", text or "")
    spans = word_spans(text)
    words = [text[a:b] for a, b in spans]
    lower = [w.lower() for w in words]
    ref = [unicodedata.normalize("NFC", r).lower() for r in reference]
    stripper = _Stripper()
    pairs = _align(lower, ref, stripper)
    paired = dict(pairs)

    replaced: dict[int, str] = {}
    # How each word is spelled where the reference is sure of it: a pair
    # with a neighbour paired too, or any pair that kept it as it is.
    spelled: dict[str, Counter[str]] = {}
    for i, j in pairs:
        anchored = paired.get(i - 1) == j - 1 or paired.get(i + 1) == j + 1
        if anchored or ref[j] == lower[i]:
            spelled.setdefault(lower[i], Counter())[ref[j]] += 1
        both = paired.get(i - 1) == j - 1 and paired.get(i + 1) == j + 1
        if (anchored and stripper.restores(ref[j], lower[i])) or (
            both and stripper.restores_short(ref[j], lower[i])
        ):
            replaced[i] = ref[j]
    for i, word in enumerate(lower):
        if i in replaced or not word.isascii():
            continue
        spellings = spelled.get(word)
        if not spellings or len(spellings) != 1:
            continue
        ((spelling, count),) = spellings.items()
        # A word paired on its own is filled only when its pair agrees.
        if i in paired and ref[paired[i]] != spelling:
            continue
        if count >= LYRICS_REPAIR_VOCABULARY_MIN and stripper.restores(spelling, word):
            replaced[i] = spelling

    out: list[str] = []
    last = 0
    for i, (a, b) in enumerate(spans):
        if i in replaced:
            out.append(text[last:a])
            out.append(_cased_like(replaced[i], words[i]))
            last = b
    out.append(text[last:])
    return Restoration("".join(out), len(words), len(pairs), len(replaced))


def _borne_out(restoration: Restoration) -> bool:
    """Whether a transcription agrees that a copy lost its letters: enough of
    its words paired with what was heard, and enough of those given letters
    back. A copy that was written without them pairs just as well and gets
    next to none, since what is sung has none either."""
    return (
        restoration.restored >= LYRICS_REPAIR_MIN_RESTORED
        and restoration.aligned >= restoration.words * LYRICS_REPAIR_MIN_ALIGNED
        and restoration.restored >= restoration.aligned * LYRICS_REPAIR_MIN_RESTORED_SHARE
    )


def mend_from_transcript(entry: dict[str, Any], heard: list[str]) -> dict[str, Any] | None:
    """lyrics.json ``entry`` with the letters its words lost taken from
    ``heard``, the words a transcription of the song heard, in order:
    "repaired": "whisper", its timing untouched. None when the transcription
    does not bear out that anything was lost."""
    synced = restore_letters(entry.get("synced") or "", heard)
    plain = restore_letters(entry.get("plain") or "", heard)
    if not _borne_out(synced if entry.get("synced") else plain):
        return None
    return {**entry, "synced": synced.text, "plain": plain.text, "repaired": "whisper"}
