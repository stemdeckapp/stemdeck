"""Upload tags written in a Windows codepage, or in UTF-8, but read as Latin-1.

An MP3's ID3v1 tag has no encoding, and an ID3v2 frame marked ISO-8859-1 is
as often filled in cp1250 (Central Europe) or cp1251 (Cyrillic). ffprobe
reads those as Latin-1, so "zapomniałem" arrived as "zapomnia³em" and
"Кино" as "Êèíî": identification searched for the garbage and found nothing,
and the page showed it. What matters here is that those are read again, and
that correct Latin-1 ("Björk", "Sigur Rós", "Mañana") never is.
"""

from __future__ import annotations

import struct
import subprocess

import pytest

from app.pipeline.audio_tags import probe_tags, tags_from_probe, tags_from_ytdlp
from tests.ffmpeg_probe import skip_without_ffmpeg


def misread(text: str, codepage: str) -> str:
    """``text`` as ffprobe hands it over from an ISO-8859-1 frame the tagger
    filled in ``codepage``."""
    return text.encode(codepage).decode("latin-1")


def raw_field(text: str, codepage: str) -> str:
    """``text`` as ffprobe hands over an ID3v1 field it cannot read as UTF-8:
    the bytes themselves, which probe_tags decodes to lone surrogates."""
    return text.encode(codepage).decode("utf-8", "surrogateescape")


def title_of(value: str) -> str | None:
    tags = tags_from_probe({"title": value})
    return tags["title"] if tags else None


# ── the examples from the bug ──


def test_the_reported_strings_are_read_again():
    assert title_of("zapomnia³em") == "zapomniałem"
    assert title_of("¯uraw") == "Żuraw"
    assert title_of("Ïðèâåò, ìèð") == "Привет, мир"


# ── Central European, cp1250 ──


@pytest.mark.parametrize(
    "text",
    [
        "Zapomniałem, że żuraw śpi",
        "Źródło Świętości",
        "Może jutro",
        "Był sobie król",
        "Ąę Łódź",
        "Wszystko się może zdarzyć",
    ],
)
def test_polish_in_cp1250(text):
    assert title_of(misread(text, "cp1250")) == text


@pytest.mark.parametrize(
    "text",
    [
        "Kryštof",
        "Žízeň řeka",
        "Příliš žluťoučký kůň úpěl ďábelské ódy",
        "Šťastný Žižkov",
    ],
)
def test_czech_in_cp1250(text):
    assert title_of(misread(text, "cp1250")) == text


# ── Cyrillic, cp1251 ──


@pytest.mark.parametrize(
    "text",
    [
        "Кино",
        "Группа крови",
        "Тёплое место, но улицы ждут",
        "Почему",
        "Океан Ельзи - Обійми",
    ],
)
def test_cyrillic_in_cp1251(text):
    assert title_of(misread(text, "cp1251")) == text


# ── UTF-8 read as Latin-1 ──


@pytest.mark.parametrize("text", ["Żuraw", "Café", "Sigur Rós", "Кино", "東京事変", "Ägypten"])
def test_utf8_read_as_latin1(text):
    assert title_of(misread(text, "utf-8")) == text


# ── what must come through untouched ──

LATIN1_NAMES = [
    "Café",
    "Björk",
    "Sigur Rós",
    "Motörhead",
    "Mañana",
    "Français",
    "Ágætis byrjun",
    "Guðrún Árnadóttir",
    "Þórður",
    "Ð",
    "Æ",
    "ÆØÅ",
    "Hüsker Dü",
    "Mötley Crüe",
    "Queensrÿche",
    "Blue Öyster Cult",
    "Die Ärzte",
    "Röyksopp",
    "Mø",
    "Ólafur Arnalds",
    "Émilie Simon",
    "Ça plane pour moi",
    "Déjà vu",
    "¿Quién Será?",
    "¡Ay Caramba!",
    "Rock´n´roll",
    "E=mc²",
    "Señor Coconut ©2001",
    "Tiësto",
    "Zoë Keating",
    "Kraftwerk « Radioaktivität »",
    "Chloë · Ñu",
]


@pytest.mark.parametrize("text", LATIN1_NAMES)
def test_correct_latin1_is_never_read_again(text):
    assert title_of(text) == text


def test_correct_latin1_is_left_alone_as_a_whole_file_too():
    tags = {f"field{i}": text for i, text in enumerate(LATIN1_NAMES)}
    tags.update(artist="Björk", title="Jóga", album="Homogenic")
    assert tags_from_probe(tags) == {"artist": "Björk", "title": "Jóga", "album": "Homogenic"}


@pytest.mark.parametrize(
    "text", ["Dream Theater", "Pull Me Under", "AC/DC", "Guns N' Roses", "(What's the Story)"]
)
def test_ascii_is_left_alone(text):
    assert title_of(text) == text


@pytest.mark.parametrize("text", ["Żuraw", "Кино", "東京事変", "Sigur Rós – Hoppípolla"])
def test_text_beyond_latin1_came_from_a_unicode_frame_and_is_left_alone(text):
    assert title_of(text) == text


def test_ytdlp_metadata_is_real_unicode_and_is_not_read_again():
    # yt-dlp's fields come from JSON APIs, never from a file's bytes.
    assert tags_from_ytdlp({"artist": "Êèíî", "track": "Mañana"}) == {
        "artist": "Êèíî",
        "title": "Mañana",
    }


# ── the file as a whole ──


def test_one_field_that_gives_the_codepage_away_settles_the_others():
    # "Pięć" misread is "Piêæ": plausible Latin-1 letters, nothing to go on.
    assert title_of(misread("Pięć", "cp1250")) == "Piêæ"
    tags = tags_from_probe(
        {
            "artist": misread("Pięć", "cp1250"),
            "title": misread("Zapomniałem", "cp1250"),
            # Byte 0xF1 is "ñ" in Latin-1 and "ń" in cp1250. In a file whose
            # tagger wrote cp1250 it was "ń".
            "album": "Mañana",
        }
    )
    assert tags == {"artist": "Pięć", "title": "Zapomniałem", "album": "Mańana"}


def test_a_short_cyrillic_name_follows_the_rest_of_the_file():
    assert title_of(misread("ДДТ", "cp1251")) == "ÄÄÒ"
    tags = tags_from_probe(
        {"artist": misread("ДДТ", "cp1251"), "title": misread("Осенняя", "cp1251")}
    )
    assert tags == {"artist": "ДДТ", "title": "Осенняя"}


def test_lyrics_are_read_again_with_the_rest():
    lyrics = "Тёплое место, но улицы ждут\nОтпечатков наших ног"
    tags = tags_from_probe(
        {"artist": misread("Кино", "cp1251"), "lyrics-eng": misread(lyrics, "cp1251")}
    )
    assert tags == {"artist": "Кино", "lyrics": lyrics}


def test_a_cp1252_apostrophe_is_read_as_one():
    assert title_of("Don" + chr(0x92) + "t Stop") == "Don’t Stop"


def test_codepages_that_disagree_equally_settle_nothing():
    # A lone byte 0x9C is "ś" in cp1250 and "њ" in cp1251: no telling. The
    # control character it is in Latin-1 is dropped, as it always was.
    assert title_of("Song " + chr(0x9C)) == "Song"


# ── an ID3v1 field ffprobe passes through as bytes ──


def test_raw_cp1250_bytes():
    assert title_of(raw_field("Żuraw Pięć", "cp1250")) == "Żuraw Pięć"


def test_raw_cp1251_bytes():
    assert title_of(raw_field("Привет", "cp1251")) == "Привет"


def test_raw_latin1_bytes_read_as_the_spec_says():
    assert title_of(raw_field("Björk", "latin-1")) == "Björk"


def test_raw_utf8_cut_at_the_field_length_keeps_what_is_whole():
    cut = "Źródło".encode()[:-1]
    assert title_of(cut.decode("utf-8", "surrogateescape")) == "Źródł"


def test_no_raw_byte_ever_reaches_the_page():
    tags = tags_from_probe({"title": bytes(range(0x80, 0x100)).decode("utf-8", "surrogateescape")})
    assert tags is not None
    assert not any(0xD800 <= ord(ch) <= 0xDFFF or 0x80 <= ord(ch) <= 0x9F for ch in tags["title"])
    tags["title"].encode("utf-8")


# ── real files through ffprobe ──


def _id3v23(frames: list[tuple[str, bytes]]) -> bytes:
    body = b"".join(
        fid.encode("ascii") + struct.pack(">I", len(data)) + bytes(2) + data for fid, data in frames
    )
    size = len(body)
    syncsafe = bytes((size >> shift) & 0x7F for shift in (21, 14, 7, 0))
    return b"ID3" + bytes([3, 0, 0]) + syncsafe + body


def _latin1_frame(text: str, codepage: str) -> bytes:
    # Encoding byte 0 declares ISO-8859-1; the tagger wrote ``codepage``.
    return bytes(1) + text.encode(codepage)


def _id3v1(title: str, artist: str, album: str, codepage: str) -> bytes:
    def field(text: str) -> bytes:
        return text.encode(codepage)[:30].ljust(30, bytes(1))

    return b"TAG" + field(title) + field(artist) + field(album) + b"2001" + bytes(30) + b"\xff"


@pytest.fixture
def untagged_mp3(tmp_path):
    skip_without_ffmpeg()
    from app.core.config import ffmpeg_executable

    path = tmp_path / "untagged.mp3"
    subprocess.run(
        [
            ffmpeg_executable(),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-map_metadata",
            "-1",
            "-id3v2_version",
            "0",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return path.read_bytes()


def test_a_real_mp3_with_cp1250_id3v2_frames(tmp_path, untagged_mp3):
    path = tmp_path / "pl.mp3"
    tag = _id3v23(
        [
            ("TPE1", _latin1_frame("Kult", "cp1250")),
            ("TIT2", _latin1_frame("Zapomniałem, że żuraw śpi", "cp1250")),
            ("TALB", _latin1_frame("Źródło Świętości", "cp1250")),
        ]
    )
    path.write_bytes(tag + untagged_mp3)
    assert probe_tags(path) == {
        "artist": "Kult",
        "title": "Zapomniałem, że żuraw śpi",
        "album": "Źródło Świętości",
    }


def test_a_real_mp3_with_cp1251_id3v2_frames_and_lyrics(tmp_path, untagged_mp3):
    path = tmp_path / "ru.mp3"
    lyrics = "Тёплое место, но улицы ждут"
    tag = _id3v23(
        [
            ("TPE1", _latin1_frame("Кино", "cp1251")),
            ("TIT2", _latin1_frame("Группа крови", "cp1251")),
            ("USLT", bytes(1) + b"eng" + bytes(1) + lyrics.encode("cp1251")),
        ]
    )
    path.write_bytes(tag + untagged_mp3)
    assert probe_tags(path) == {"artist": "Кино", "title": "Группа крови", "lyrics": lyrics}


@pytest.mark.parametrize(
    ("codepage", "title", "artist", "album"),
    [
        ("cp1250", "Żuraw Pięć", "Łzy", "Mogę być"),
        ("cp1250", "Žízeň řeka", "Kryštof", "Šťastný"),
        ("cp1251", "Привет", "ДДТ", "Осень"),
    ],
)
def test_a_real_mp3_with_an_id3v1_tag(tmp_path, untagged_mp3, codepage, title, artist, album):
    path = tmp_path / "v1.mp3"
    path.write_bytes(untagged_mp3 + _id3v1(title, artist, album, codepage))
    assert probe_tags(path) == {"artist": artist, "title": title, "album": album}


def test_a_real_mp3_with_correct_latin1_frames(tmp_path, untagged_mp3):
    path = tmp_path / "latin1.mp3"
    tag = _id3v23(
        [
            ("TPE1", _latin1_frame("Sigur Rós", "latin-1")),
            ("TIT2", _latin1_frame("Mañana Français", "latin-1")),
            ("TALB", _latin1_frame("Ágætis byrjun", "latin-1")),
        ]
    )
    path.write_bytes(tag + untagged_mp3)
    assert probe_tags(path) == {
        "artist": "Sigur Rós",
        "title": "Mañana Français",
        "album": "Ágætis byrjun",
    }
