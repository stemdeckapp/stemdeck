"""Letters given back to lyrics that lost them (app/pipeline/lyrics_repair.py).

The Polish anthem stands in for a song, being in the public domain; the
English lines are written for these tests.
"""

from __future__ import annotations

import unicodedata

import pytest

from app.pipeline.lyrics_repair import (
    looks_stripped,
    mend_from_transcript,
    restore_letters,
    words_of,
)

ANTHEM = (
    "[00:01.00]Jeszcze Polska nie zginęła,\n"
    "[00:04.00]Kiedy my żyjemy.\n"
    "[00:07.00]Co nam obca przemoc wzięła,\n"
    "[00:10.00]Szablą odbierzemy.\n"
    "[00:13.00]Marsz, marsz, Dąbrowski,\n"
    "[00:16.00]Z ziemi włoskiej do Polski.\n"
    "[00:19.00]Za twoim przewodem\n"
    "[00:22.00]Złączym się z narodem.\n"
)
# Twice through, with the stamps of a second verse: over forty words.
SONG = ANTHEM + ANTHEM.replace("[00:", "[01:")

ENGLISH = (
    "[00:01.00]I know the night is long and you are far away from home\n"
    "[00:05.00]But every time I think of you I feel the city glow\n"
    "[00:09.00]So hold me close and tell me that the morning will be kind\n"
    "[00:13.00]Cause all the words we never said are running through my mind\n"
    "[00:17.00]Yeah we could leave it all behind and never look again\n"
)


def _drop(text: str) -> str:
    """Every letter outside ASCII dropped: "Szablą" as "Szabl"."""
    return "".join(ch for ch in text if ord(ch) < 128)


def _fold(text: str) -> str:
    """Folded to the base letter where there is one: "Szablą" as "Szabla"."""
    return _drop(unicodedata.normalize("NFD", text))


def _as_lrclib_drops_them(text: str) -> str:
    """What LRCLIB's damaged copies hold: "ę" folded, the rest dropped."""
    return _drop(text.replace("ę", "e"))


def _stamps(text: str) -> list[str]:
    return [line[:10] for line in text.splitlines()]


# ── restore_letters ──


@pytest.mark.parametrize("strip", [_drop, _fold, _as_lrclib_drops_them])
def test_a_stripped_copy_gets_every_letter_back_and_keeps_its_stamps(strip):
    mended = restore_letters(strip(SONG), words_of(SONG))
    assert mended.text == SONG
    assert mended.restored > 0 and mended.aligned == mended.words


def test_enhanced_word_stamps_and_header_tags_are_left_as_they_are():
    text = "[ar:Zespol]\n[00:01.00]<00:01.00>Kiedy <00:01.40>my <00:01.70>yjemy<00:02.20>\n"
    mended = restore_letters(text, ["Kiedy", "my", "żyjemy"])
    assert mended.text == (
        "[ar:Zespol]\n[00:01.00]<00:01.00>Kiedy <00:01.40>my <00:01.70>żyjemy<00:02.20>\n"
    )


def test_a_word_takes_the_case_it_was_written_in():
    text = "[00:01.00]SZABL odbierzemy\n[00:02.00]Szabl odbierzemy\n[00:03.00]szabl odbierzemy\n"
    heard = ["szablą", "odbierzemy", "SZABLĄ", "odbierzemy", "Szablą", "odbierzemy"]
    assert restore_letters(text, heard).text == (
        "[00:01.00]SZABLĄ odbierzemy\n[00:02.00]Szablą odbierzemy\n[00:03.00]szablą odbierzemy\n"
    )


def test_a_misheard_word_is_never_put_in():
    heard = words_of(ANTHEM)
    heard[heard.index("Szablą")] = "Sablą"  # misheard
    heard[heard.index("zginęła")] = "zgubiła"  # another word
    mended = restore_letters(_as_lrclib_drops_them(ANTHEM), heard).text
    assert "[00:10.00]Szabl odbierzemy." in mended
    assert "[00:01.00]Jeszcze Polska nie zginea," in mended
    assert "[00:04.00]Kiedy my żyjemy." in mended, "the rest is mended all the same"


def test_a_lone_letter_is_not_given_any():
    # "E" could be "Że", "Ę" or a sung vowel; "ze" is only ever given letters
    # by a heard word it strips down to.
    text = "[00:01.00]E tak niewiele jest\n"
    assert restore_letters(text, ["Że", "tak", "niewiele", "jest"]).text == text


def test_a_pair_with_no_neighbour_paired_does_not_count_on_its_own():
    # Two words that merely strip down alike, far apart and alone, are not
    # the same word sung.
    text = "[00:01.00]Nam cie dzis\n"
    assert restore_letters(text, ["Zupełnie", "inna", "cię", "piosenka"]).text == text


def test_a_repeat_the_reference_skipped_takes_the_spelling_it_had_elsewhere():
    stripped = _as_lrclib_drops_them(SONG)
    # The transcription heard the first time through only.
    mended = restore_letters(stripped, words_of(ANTHEM))
    assert mended.text == SONG


def test_a_word_kept_as_it_is_somewhere_is_not_filled_anywhere():
    # "co" is heard once as "coś" and once as "co": the third, unheard, stays.
    text = "[00:01.00]bo co ciagle\n[00:02.00]wszystko co jeszcze\n[00:03.00]a co dalej\n"
    heard = ["bo", "coś", "ciągle", "wszystko", "co", "jeszcze"]
    mended = restore_letters(text, heard).text
    assert mended.splitlines() == [
        "[00:01.00]bo coś ciągle",
        "[00:02.00]wszystko co jeszcze",
        "[00:03.00]a co dalej",
    ]


def test_nothing_to_pair_with_changes_nothing():
    assert restore_letters(_drop(ANTHEM), []).text == _drop(ANTHEM)
    assert restore_letters("", words_of(ANTHEM)).text == ""


# ── looks_stripped ──


@pytest.mark.parametrize("strip", [_drop, _fold, _as_lrclib_drops_them])
def test_a_long_copy_with_no_accents_that_is_not_english_looks_stripped(strip):
    assert looks_stripped(strip(SONG))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(ENGLISH, id="english"),
        pytest.param(ENGLISH.upper(), id="english in capitals"),
        pytest.param(SONG, id="correctly accented"),
        pytest.param(_drop(SONG) + "[02:00.00]i jeszcze raz, już\n", id="one accented word"),
        pytest.param(_drop(ANTHEM), id="too short to tell"),
        pytest.param("[00:01.00]夢ならばどれほどよかったでしょう\n" * 20, id="another script"),
        pytest.param("[00:01.00]Группа крови на рукаве\n" * 20, id="cyrillic"),
    ],
)
def test_english_accented_and_short_copies_do_not_look_stripped(text):
    assert not looks_stripped(text)


def test_a_transliteration_looks_stripped_to_the_text_alone():
    # Russian in Latin letters cannot be told from Polish without accents by
    # its text: the language Whisper hears is what rules it out (transcribe.py,
    # LYRICS_REPAIR_LANGUAGES has no "ru").
    translit = "[00:01.00]Gruppa krovi na rukave, moy poryadkovyy nomer na rukave\n" * 5
    assert looks_stripped(translit)


# ── mend_from_transcript ──


def _entry(synced: str) -> dict:
    return {"source": "lrclib", "synced": synced, "plain": synced, "lrclib_id": 7}


def test_a_transcript_that_bears_it_out_mends_both_texts():
    stripped = _as_lrclib_drops_them(SONG)
    mended = mend_from_transcript(_entry(stripped), words_of(SONG))
    assert mended["synced"] == SONG and mended["plain"] == SONG
    assert mended["repaired"] == "whisper"
    assert _stamps(mended["synced"]) == _stamps(stripped)
    assert mended["lrclib_id"] == 7


def test_a_song_written_without_accents_is_not_mended():
    # The singer sings what is written; a word or two misheard with accents
    # does not make a stripped copy.
    words = _drop(SONG)
    heard = words_of(words)
    heard[0], heard[5] = "Jeszczę", "żyjemy"
    assert mend_from_transcript(_entry(words), heard) is None


def test_a_transcript_of_another_song_is_not_used():
    heard = words_of("Zupełnie inna piosenka, śpiewana gdzie indziej, o czymś innym. " * 5)
    heard += words_of(ANTHEM)[:6]
    assert mend_from_transcript(_entry(_as_lrclib_drops_them(SONG)), heard) is None
