"""What a track's title says it is (app/pipeline/title_parse.py). Pure: no
network, no state. The first four titles are real YouTube uploads that carry
no music metadata at all, the case the parser exists for."""

from __future__ import annotations

import pytest

from app.pipeline.title_parse import (
    TitleReading,
    alternatives,
    cast_work,
    coverage,
    extra_words,
    kind_of,
    name_alternatives,
    name_key,
    name_splits,
    parse_title,
    readings_for,
    resolvable,
    song_key,
    title_names,
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


# ── titles from East Asia, and noise in every language ──


@pytest.mark.parametrize(
    ("title", "first"),
    [
        # 周杰倫 Jay Chou's channel: the song in lenticular brackets, both
        # names of each in two scripts, the noise after a dash with no space.
        (
            "周杰倫 Jay Chou【晴天 Sunny Day】-Official Music Video",
            R("晴天", artist="周杰倫", song_alts=("Sunny Day",), artist_alts=("Jay Chou",)),
        ),
        (
            "林俊傑 JJ Lin【江南 River South】官方完整版 MV",
            R("江南", artist="林俊傑", song_alts=("River South",), artist_alts=("JJ Lin",)),
        ),
        ("YOASOBI「夜に駆ける」 Official Music Video", R("夜に駆ける", artist="YOASOBI")),
        ("米津玄師 MV「Lemon」", R("Lemon", artist="米津玄師")),
        ("周杰倫《告白氣球》", R("告白氣球", artist="周杰倫")),
        # An anime's name in marks before the singer's.
        ("『鬼滅の刃』主題歌 LiSA「紅蓮華」", R("鬼滅の刃", artist="LiSA")),
        # 1theK: a name and its Hangul in brackets, "_" between.
        (
            "[MV] IU(아이유) _ Palette(팔레트) (Feat. G-DRAGON)",
            R("Palette", artist="IU", song_alts=("팔레트",), artist_alts=("아이유",)),
        ),
        (
            "IU (아이유) _ Good Day (좋은 날) _ MV",
            R("Good Day", artist="IU", song_alts=("좋은 날",), artist_alts=("아이유",)),
        ),
        # HYBE LABELS: the song in single quotes.
        (
            "BTS (방탄소년단) '봄날 (Spring Day)' Official MV",
            R("봄날", artist="BTS", song_alts=("Spring Day",), artist_alts=("방탄소년단",)),
        ),
        (
            "NewJeans (뉴진스) 'Ditto' Official MV (side A)",
            R("Ditto", artist="NewJeans", artist_alts=("뉴진스",)),
        ),
        # "Official" is part of the band's name, 【MV】 is noise.
        ("【MV】Official髭男dism - Pretender", R("Pretender", artist="Official髭男dism")),
        (
            "Official髭男dism - Pretender［Official Video］",
            R("Pretender", artist="Official髭男dism"),
        ),
        # Full-width letters and separators.
        ("Ｙｏａｓｏｂｉ ／ 群青", R("群青", artist="Yoasobi")),
        ("米津玄師 － Lemon", R("Lemon", artist="米津玄師")),
        # A dash touching the song.
        ("鄧麗君 -月亮代表我的心 (HD)", R("月亮代表我的心", artist="鄧麗君")),
        # The Japanese subtitle between wave dashes is the song's other name.
        (
            "YOASOBI - 夜に駆ける ~Racing Into The Night~",
            R("夜に駆ける", artist="YOASOBI", song_alts=("Racing Into The Night",)),
        ),
        # Noise in Chinese, Japanese and Korean, bracketed or not.
        ("五月天 - 倔強 (官方完整版MV)", R("倔強", artist="五月天")),
        ("King Gnu - 白日【高音質】", R("白日", artist="King Gnu")),
        ("BIGBANG - 거짓말 (뮤직비디오)", R("거짓말", artist="BIGBANG")),
        ("王菲 - 紅豆 (動態歌詞)", R("紅豆", artist="王菲")),
    ],
)
def test_an_east_asian_title_is_read_as_carefully(title, first):
    assert parse_title(title)[0] == first


def test_a_full_width_pipe_is_a_pipe():
    # "Song | Work" is read song first, as before; the other way next.
    assert R("夜に駆ける", artist="YOASOBI") in parse_title("YOASOBI｜夜に駆ける")


@pytest.mark.parametrize(
    "title",
    [
        "Don't Stop Me Now",
        "Guns N' Roses - Sweet Child O' Mine",
        "Rock 'n' Roll Train",
        "Queen - Bohemian Rhapsody (Official Video Remastered)",
        "Nirvana - Lithium (Official Music Video)",
    ],
)
def test_latin_titles_read_as_they_did(title):
    """No alternatives out of one script, apostrophes left alone."""
    for reading in parse_title(title):
        assert reading.song_alts == () and reading.artist_alts == ()
    assert parse_title("Don't Stop Me Now") == [R("Don't Stop Me Now")]
    assert parse_title("Rock 'n' Roll Train") == [R("Rock 'n' Roll Train")]


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("周杰倫 Jay Chou", ["周杰倫", "Jay Chou"]),
        ("鄧麗君 Teresa Teng テレサ・テン", ["鄧麗君", "Teresa Teng", "テレサ・テン"]),
        ("少女時代 소녀시대", ["少女時代", "소녀시대"]),
        # One word in two scripts is one name.
        ("Official髭男dism", ["Official髭男dism"]),
        ("宇多田ヒカル", ["宇多田ヒカル"]),
        ("Michael Crawford, Sarah Brightman", ["Michael Crawford, Sarah Brightman"]),
    ],
)
def test_a_name_in_two_scripts_is_two_names(text, names):
    assert alternatives(text) == names


def test_a_bilingual_tag_gives_each_name():
    assert name_alternatives("鄧麗君 Teresa Teng テレサ・テン")[1:] == [
        "鄧麗君",
        "Teresa Teng",
        "テレサ・テン",
    ]
    assert name_alternatives("IU(아이유)")[1:] == ["IU", "아이유"]
    assert name_alternatives("Xutos & Pontapés") == ["Xutos & Pontapés", "Xutos", "Pontapés"]


def test_the_best_of_an_artists_names_covers_a_reading():
    reading = R("晴天", artist="周杰倫", artist_alts=("Jay Chou",))
    assert extra_words(reading) == {"周杰伦", "jay", "chou"}
    assert coverage(reading, "周杰倫") == 1.0
    assert coverage(reading, "Jay Chou") == 1.0
    assert coverage(reading, "Somebody") == 0.0


def test_names_compare_across_chinese_scripts_and_latin_letters():
    assert name_key("周杰倫") == name_key("周杰伦")
    assert name_key("Dawid Podsiadło") == name_key("Dawid Podsiadlo")
    assert song_key("紅豆") == song_key("红豆")
    # Kana keep their voicing: が is not か.
    assert words("ずっと") != words("すっと")


def test_a_bilingual_tag_reading_carries_both_names():
    tags = {"artist": "鄧麗君 Teresa Teng テレサ・テン", "title": "甜蜜蜜 (Official Music Video)"}
    assert readings_for(tags)[0] == R(
        "甜蜜蜜", artist="鄧麗君", artist_alts=("Teresa Teng", "テレサ・テン")
    )


@pytest.mark.parametrize(
    ("title", "cuts"),
    [
        (
            "美空ひばり 川の流れのように",
            [("美空ひばり", "川の流れのように"), ("川の流れのように", "美空ひばり")],
        ),
        (
            "Herbert Grönemeyer Männer",
            [("Herbert", "Grönemeyer Männer"), ("Männer", "Herbert Grönemeyer")],
        ),
        # Two Latin words are as often one song's name.
        ("Bohemian Rhapsody", []),
        # A separator, marks or quotes say where the cut is.
        ("Queen - Bohemian Rhapsody", []),
        ("YOASOBI「群青」", []),
    ],
)
def test_a_title_with_nothing_between_artist_and_song_is_cut_every_way(title, cuts):
    assert name_splits(title)[: len(cuts)] == cuts
    assert bool(name_splits(title)) is bool(cuts)


def test_a_compilation_names_its_artist_without_what_makes_it_one():
    assert title_names("邓丽君经典金曲 - Teresa Teng's classic songs") == (
        [["邓丽君"], ["Teresa Teng"]],
        True,
    )
    assert title_names("邓丽君经典歌曲，Teresa Teng's classic songs. #music")[0][0][0] == "邓丽君"
    assert title_names("Teresa Teng Greatest Hits 20首") == ([["Teresa Teng"]], True)
    # A title that is no compilation says so.
    assert title_names("Metropolis - Part I") == ([["Metropolis"], ["Part I"]], False)
