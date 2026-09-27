"""What a track's title says it is (app/pipeline/title_parse.py). Pure: no
network, no state. The first four titles are real YouTube uploads that carry
no music metadata at all, the case the parser exists for."""

from __future__ import annotations

import pytest

from app.pipeline.title_parse import (
    TitleReading,
    cast_work,
    coverage,
    extra_words,
    kind_of,
    parse_title,
    readings_for,
    resolvable,
    song_key,
    words,
    work_hint,
)

R = TitleReading


@pytest.mark.parametrize(
    ("title", "readings"),
    [
        # WickedVEVO, 458 s.
        (
            'Dancing Through Life (From "Wicked" Original Broadway Cast Recording/2003)',
            [R("Dancing Through Life", work="Wicked", kind="musical", year=2003)],
        ),
        # Atlantic Records, 235 s.
        (
            "The Greatest Showman Cast - This Is Me (Official Audio)",
            [R("This Is Me", artist="The Greatest Showman Cast", work="The Greatest Showman")],
        ),
        # A fan's upload, 303 s: the performers after a run of spaces.
        (
            "The Phantom of the Opera   Michael Crawford, Sarah Brightman",
            [
                R("The Phantom of the Opera", artist="Michael Crawford, Sarah Brightman"),
                R("Michael Crawford, Sarah Brightman", artist="The Phantom of the Opera"),
            ],
        ),
        (
            "Nirvana - Lithium (Official Music Video)",
            [R("Lithium", artist="Nirvana"), R("Nirvana", artist="Lithium")],
        ),
        (
            "Queen - Bohemian Rhapsody (Official Video Remastered)",
            [R("Bohemian Rhapsody", artist="Queen"), R("Queen", artist="Bohemian Rhapsody")],
        ),
        # The noise unbracketed, or after a pipe.
        (
            "Queen - Bohemian Rhapsody | Official Video",
            [R("Bohemian Rhapsody", artist="Queen"), R("Queen", artist="Bohemian Rhapsody")],
        ),
        (
            "Hamilton Original Broadway Cast - My Shot [Lyric Video] HD",
            [R("My Shot", artist="Hamilton Original Broadway Cast", work="Hamilton")],
        ),
        ("Cast of Hamilton - My Shot", [R("My Shot", artist="Cast of Hamilton", work="Hamilton")]),
        ("Popular - Wicked The Musical", [R("Popular", work="Wicked", kind="musical")]),
        ("Frozen Soundtrack - Let It Go", [R("Let It Go", work="Frozen", kind="film")]),
        (
            'Let It Go (From "Frozen"/Soundtrack Version)',
            [R("Let It Go", work="Frozen", kind="film")],
        ),
        (
            "This Is Me (from The Greatest Showman)",
            [R("This Is Me", work="The Greatest Showman")],
        ),
        # "Song | Work": the song first.
        (
            "Defying Gravity | Wicked",
            [R("Defying Gravity", artist="Wicked"), R("Wicked", artist="Defying Gravity")],
        ),
        # "Work: Song".
        ("Wicked: Popular", [R("Popular", artist="Wicked"), R("Wicked", artist="Popular")]),
        (
            "Kristin Chenoweth - Popular - Wicked",
            [
                R("Popular", artist="Kristin Chenoweth, Wicked"),
                R("Kristin Chenoweth", artist="Popular, Wicked"),
                R("Wicked", artist="Kristin Chenoweth, Popular"),
            ],
        ),
        ('Keala Settle "This Is Me"', [R("This Is Me", artist="Keala Settle")]),
        # A featured artist in brackets is not part of the song's name.
        (
            "Queen - Under Pressure (feat. David Bowie)",
            [R("Under Pressure", artist="Queen"), R("Queen", artist="Under Pressure")],
        ),
        ("Bohemian Rhapsody", [R("Bohemian Rhapsody")]),
        ("", []),
        (None, []),
        ("(Official Video)", []),
    ],
)
def test_a_title_is_read_every_way_it_can_be(title, readings):
    assert parse_title(title) == readings


def test_readings_are_capped():
    assert len(parse_title("a - b - c - d - e")) == 3


def test_a_cast_names_its_show():
    assert cast_work("The Greatest Showman Cast") == "The Greatest Showman"
    assert cast_work("Original Broadway Cast of Hamilton") == "Hamilton"
    assert cast_work("Wicked Original Broadway Cast Recording") == "Wicked"
    # Performers in front of the cast: the show is the last of the list.
    assert cast_work("Keala Settle & The Greatest Showman Cast") == "The Greatest Showman"
    assert cast_work("Nirvana") == ""
    assert cast_work("Original Broadway Cast") == ""


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Original Broadway Cast Recording", "musical"),
        ("Original London Cast", "musical"),
        ("Original Motion Picture Cast Recording", "film"),
        ("Soundtrack Version", "film"),
        ("Soundtrack from the Netflix Series", "tv"),
        # A cast alone could be a film's or a show's.
        ("The Greatest Showman Cast", None),
        ("Official Video", None),
    ],
)
def test_what_kind_of_work_a_qualifier_says(text, kind):
    assert kind_of(text) == kind


def test_the_words_a_recording_must_account_for():
    reading = R("This Is Me", artist="The Greatest Showman Cast", work="The Greatest Showman")
    assert extra_words(reading) == {"greatest", "showman"}
    assert words("Les Misérables (Original London Cast)") == {"les", "miserables"}
    assert words("Don't Stop Me Now") == {"dont", "stop", "me", "now"}
    assert coverage(reading, "Keala Settle & The Greatest Showman Ensemble") == 1.0
    assert coverage(reading, "Kesha", "The Greatest Showman: Reimagined") == 1.0
    assert coverage(reading, "Kesha") == 0.0
    assert coverage(R("Song"), "anything") == 0.0


def test_a_song_alone_is_not_enough():
    assert not resolvable(R("Bohemian Rhapsody"))
    assert not resolvable(R("", artist="Queen"))
    # The show's qualifier words alone name nothing either.
    assert not resolvable(R("Popular", artist="Original Broadway Cast"))
    assert resolvable(R("Popular", work="Wicked"))


def test_song_keys_ignore_brackets_case_and_punctuation():
    assert song_key('This Is Me (From "The Greatest Showman")') == song_key("this is me")
    assert song_key("Bohemian Rhapsody - Remastered 2011") != song_key("Bohemian Rhapsody")


def test_the_tags_reading_comes_first_and_duplicates_go():
    tags = {"artist": "The Greatest Showman Cast", "title": "This Is Me (Official Audio)"}
    readings = readings_for(tags, "The Greatest Showman Cast - This Is Me (Official Audio)")
    assert readings[0] == R(
        "This Is Me", artist="The Greatest Showman Cast", work="The Greatest Showman"
    )
    # The title's own first reading is the same one, and is not asked twice.
    assert (
        R("This Is Me", artist="The Greatest Showman Cast", work="The Greatest Showman")
        not in (readings[1:])
    )


def test_only_readings_naming_more_than_a_song_are_offered():
    assert readings_for(None, "Bohemian Rhapsody") == []
    assert readings_for(None, "x") == []
    assert readings_for({"title": "Song"}, None) == []
    assert readings_for(None, 'Dancing Through Life (From "Wicked")') == [
        R("Dancing Through Life", work="Wicked")
    ]


def test_the_work_hint_follows_the_identified_song():
    title = "Popular (Original Broadway Cast Recording) - Kristin Chenoweth"
    # Either end could be the song; the one identified decides.
    assert work_hint(title, "Popular").work == "Kristin Chenoweth"
    assert work_hint("Popular - Wicked The Musical", "Popular").work == "Wicked"
    assert work_hint("Popular - Wicked The Musical").kind == "musical"
    assert work_hint("Nirvana - Lithium (Official Music Video)", "Lithium") is None
    assert work_hint('Dancing Through Life (From "Wicked")', "Another Song") is None
