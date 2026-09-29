"""Lyrics for an artist whatever name, script or spelling LRCLIB has them under.

LRCLIB files a song under the name its uploader wrote: 周杰倫, 周杰伦 or Jay
Chou; 아이유 or IU. The lookup keeps a version only when its artist is one the
track is known by (belongs_to), so these are the ways two names are one: the
same name folded (traditional and simplified Chinese, full and half width, a
Latin letter typed plain), a name given in two scripts at once, and the other
names MusicBrainz and Wikidata record for the artist (name_aliases.py). And the
way they are not: another artist's song of the same name is never kept.

No network: conftest answers LRCLIB, MusicBrainz and Wikidata as offline, and
tests that want an answer stand in for them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.pipeline.lyrics_lookup as ll
from app.core.models import Job
from app.pipeline import musicbrainz, name_aliases, zh_variants
from app.pipeline.lyrics_lookup import LyricsQuery, build_query, lookup_lyrics

JAY_MBID = "a223958d-5c56-4b2c-a30a-87e357bc121b"
IU_MBID = "b9545342-1e6d-4dae-84ac-013374ad8d7c"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _forget_wikidata():
    name_aliases._wikidata_kept.clear()
    yield
    name_aliases._wikidata_kept.clear()


def row(lrclib_id, duration, *, artist, track):
    return {
        "id": lrclib_id,
        "trackName": track,
        "artistName": artist,
        "albumName": "",
        "duration": duration,
        "instrumental": False,
        "syncedLyrics": f"[00:01.00]line {lrclib_id}",
        "plainLyrics": f"line {lrclib_id}",
    }


class Lrclib:
    """Stands in for _fetch_json: a search by artist, or "q:<name>" for a
    name-only search, from ``searches``; /get finds nothing."""

    def __init__(self, searches):
        self.searches = searches
        self.asked: list[str] = []

    def __call__(self, endpoint, params):
        if endpoint == "get":
            return None
        key = params.get("artist_name") or f"q:{params['q']}"
        self.asked.append(key)
        return self.searches.get(key, [])


def aliases_of(*names):
    calls = []

    def aliases(query, cancelled):
        calls.append(query)
        return list(names)

    aliases.calls = calls
    return aliases


# ── the table ──


def test_traditional_chinese_folds_to_simplified_and_nothing_else_changes():
    assert zh_variants.to_simplified("鄧麗君 紅豆 周杰倫") == "邓丽君 红豆 周杰伦"
    assert zh_variants.to_simplified("宇多田ヒカル, 아이유, Café") == "宇多田ヒカル, 아이유, Café"
    assert len(zh_variants.pairs()) > 3000


def test_the_page_holds_the_same_table():
    source = (ROOT / "static" / "js" / "zhVariants.js").read_text(encoding="utf-8")
    joined = re.search(r'export const PAIRS =\s*"([^"]*)";', source).group(1)
    chars = list(joined)
    page = dict(zip(chars[0::2], chars[1::2], strict=True))
    assert page == zh_variants.pairs()


def test_the_table_carries_its_attribution():
    head = (ROOT / "app" / "_vendor" / "opencc" / "zh_t2s.txt").read_text(encoding="utf-8")[:1000]
    assert "OpenCC" in head and "Apache License, Version 2.0" in head


# ── two names, one artist ──


@pytest.mark.parametrize(
    ("found", "names", "same"),
    [
        # Traditional and simplified, full and half width, a letter typed plain.
        ("周杰伦", ["周杰倫"], True),
        ("邓丽君", ["鄧麗君"], True),
        ("ＹＯＡＳＯＢＩ", ["YOASOBI"], True),
        ("ｷﾝｸﾞﾇｰ", ["キングヌー"], True),
        ("Dawid Podsiadlo", ["Dawid Podsiadło"], True),
        # A name in two scripts at once is either of them.
        ("鄧麗君 (Teresa Teng)", ["Teresa Teng"], True),
        ("五月天 (Mayday)", ["五月天"], True),
        ("IU", ["IU (아이유)"], True),
        ("周杰倫", ["周杰倫 Jay Chou"], True),
        ("Jay Chou", ["周杰倫 Jay Chou"], True),
        # A duet credits a Chinese name of three characters, which is a name.
        ("周杰倫 & 費玉清", ["周杰倫"], True),
        # Never guessed: a romanisation is not the name without a source for it.
        ("Jay Chou", ["周杰倫"], False),
        ("아이유", ["IU"], False),
        # Another artist, however close.
        ("张信哲", ["周杰倫"], False),
        ("五月天 阿信", ["五月天"], False),
        ("告五人", ["五月天"], False),
        ("Official", ["Official髭男dism"], False),
        ("林", ["林 & 周杰倫"], False),
    ],
)
def test_same_artist_across_scripts(found, names, same):
    assert ll.same_artist(found, names) is same


@pytest.mark.parametrize(
    ("found", "song", "same"),
    [
        ("红豆", "紅豆", True),
        ("月亮代表我的心", "月亮代表我的心", True),
        ("晴天 (Sunny Day)", "晴天", True),
        ("晴天（Sunny Day）", "晴天", True),
        ("「白日」", "白日", True),
        ("【白日】", "白日", True),
        ("밤편지 (Through the Night)", "밤편지", True),
        ("月亮代表我的心 - 劇集 “黃金有罪” 插曲", "月亮代表我的心", True),
        ("Malomiasteczkowy", "Małomiasteczkowy", True),
        ("晴れの日(晴天)", "晴天", False),
        ("雨天", "晴天", False),
        ("The Moon Represents My Heart - 月亮代表我的心", "月亮代表我的心", False),
    ],
)
def test_same_song_across_scripts(found, song, same):
    assert ll.same_song(found, song) is same


def test_script_names():
    assert ll.script_names("周杰倫 Jay Chou") == ["周杰伦", "Jay Chou"]
    assert ll.script_names("IU(아이유)") == ["IU", "아이유"]
    assert ll.script_names("鄧麗君 Teresa Teng テレサ・テン") == [
        "邓丽君",
        "Teresa Teng",
        "テレサ・テン",
    ]
    assert ll.script_names("Official髭男dism") == []
    assert ll.script_names("五月天 阿信") == []
    assert ll.script_names("Queen") == []


# ── what MusicBrainz and Wikidata say ──

# Jay Chou's MusicBrainz artist as it answers, trimmed.
JAY = {
    "id": JAY_MBID,
    "name": "周杰倫",
    "aliases": [
        {"name": "Jay", "locale": "en", "type": "Artist name", "primary": False},
        {"name": "Jay Chou", "locale": "en", "type": "Artist name", "primary": False},
        {"name": "Zhou Jielun", "locale": "zh_Latn", "type": "Artist name", "primary": None},
        {"name": "周杰伦", "locale": "zh_Hans", "type": "Artist name", "primary": True},
        {"name": "ジェイ・チョウ", "locale": "ja", "type": "Artist name", "primary": None},
        {"name": "µËÀö¾ý", "locale": None, "type": "Search hint", "primary": None},
        {"name": "周杰", "locale": None, "type": "Legal name", "primary": False},
        {"name": "てん", "locale": None, "type": None, "primary": None},
    ],
    "relations": [
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q238819"}}
    ],
}


def test_musicbrainz_names_leave_out_what_is_not_a_name():
    names = name_aliases.musicbrainz_names(JAY)
    assert names[:2] == ["周杰倫", "周杰伦"], "its own name, then the primary ones"
    assert {"Jay Chou", "Zhou Jielun", "ジェイ・チョウ"} <= set(names)
    # Too short to say who ("Jay", "てん"), a search hint's mojibake, a legal name.
    assert not {"Jay", "てん", "µËÀö¾ý", "周杰"} & set(names)


def test_wikidata_labels_count_and_only_distinctive_aliases():
    entity = {
        "labels": {"en": {"value": "BIGBANG"}, "ko": {"value": "빅뱅"}},
        "aliases": {
            "en": [{"value": "VI"}, {"value": "Big Bang"}],
            "ja": [{"value": "ビッグ・バン"}],
        },
    }
    names = name_aliases.wikidata_names(entity)
    assert names[:2] == ["BIGBANG", "빅뱅"]
    assert "Big Bang" in names and "ビッグ・バン" in names
    assert "VI" not in names


def test_aliases_are_asked_of_musicbrainz_once_and_kept(monkeypatch):
    asked = []

    def fetch(path, params, **kwargs):
        asked.append((path, params))
        return JAY

    monkeypatch.setattr(musicbrainz, "_fetch_json", fetch)
    first = name_aliases.artist_aliases([JAY_MBID])
    again = name_aliases.artist_aliases([JAY_MBID])
    assert "Jay Chou" in first and again == first
    assert asked == [(f"artist/{JAY_MBID}", {"inc": "aliases+url-rels"})]
    # The same answer serves the band lookup's question, with no request.
    assert musicbrainz.artist_wikidata_id(JAY_MBID) == "Q238819"
    assert len(asked) == 1


def test_a_cached_artist_without_aliases_is_asked_again(monkeypatch):
    musicbrainz.cache_put("artist", JAY_MBID, {"id": JAY_MBID, "name": "周杰倫", "relations": []})
    monkeypatch.setattr(musicbrainz, "_fetch_json", lambda path, params, **kw: JAY)
    assert "Jay Chou" in name_aliases.artist_aliases([JAY_MBID])


def test_wikidata_names_the_band(monkeypatch):
    asked = []

    def fetch(params):
        asked.append(params)
        return {
            "entities": {"Q20145": {"labels": {"ko": {"value": "아이유"}, "en": {"value": "IU"}}}}
        }

    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", fetch)
    assert name_aliases.artist_aliases([], "Q20145") == ["아이유", "IU"]
    assert name_aliases.artist_aliases([], "Q20145") == ["아이유", "IU"]
    assert len(asked) == 1 and asked[0]["ids"] == "Q20145"


def test_no_connection_is_no_other_names():
    # conftest has MusicBrainz and Wikidata offline.
    assert name_aliases.artist_aliases([JAY_MBID], "Q238819") == []


def test_nothing_is_asked_for_what_is_not_an_id(monkeypatch):
    monkeypatch.setattr(musicbrainz, "_fetch_json", lambda *a, **k: pytest.fail("asked"))
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", lambda *a: pytest.fail("asked"))
    assert name_aliases.artist_aliases(["../../etc"], "Q1 OR 1") == []


# ── the lookup ──


def jay_query(**fields):
    base = {
        "artist": "Jay Chou",
        "track": "晴天",
        "album": "",
        "duration": 269.0,
        "artist_ids": (JAY_MBID,),
    }
    return LyricsQuery(**{**base, **fields})


# The name alone answers with anybody's 晴天: Jay Chou's, filed under his
# Chinese name in both scripts, a cover by another singer, and a song whose
# artist and title were swapped.
Q_SUNNY_DAY = [
    row(1, 250, artist="张信哲", track="晴天"),
    row(2, 269, artist="周杰伦", track="晴天"),
    row(3, 269, artist="晴天", track="周杰伦"),
    row(4, 300, artist="告五人", track="晴れの日(晴天)"),
]


def test_a_latin_name_finds_the_song_lrclib_has_under_the_chinese_one():
    lrclib = Lrclib({"q:晴天": Q_SUNNY_DAY})
    aliases = aliases_of("周杰倫", "周杰伦")
    answer = lookup_lyrics(jay_query(), fetch_json=lrclib, aliases=aliases)
    assert answer.lyrics["lrclib_id"] == 2
    assert answer.lyrics["timing"] == "exact"
    assert [o["artist"] for o in answer.others] == []
    assert len(aliases.calls) == 1


def test_without_its_other_names_the_same_song_is_not_taken():
    lrclib = Lrclib({"q:晴天": Q_SUNNY_DAY})
    answer = lookup_lyrics(jay_query(artist_ids=()), fetch_json=lrclib)
    assert answer.lyrics is None and answer.others == []


def test_its_other_names_are_searched_by_when_nothing_else_found_it():
    lrclib = Lrclib({"周杰倫": [row(5, 269, artist="周杰倫", track="晴天")]})
    answer = lookup_lyrics(jay_query(), fetch_json=lrclib, aliases=aliases_of("周杰倫", "周杰伦"))
    assert answer.lyrics["lrclib_id"] == 5
    assert lrclib.asked == ["Jay Chou", "q:晴天", "周杰倫"], "stops at the first one found"


def test_lyrics_found_under_another_name_carry_the_mark_and_stand_when_read_back():
    from app.pipeline.lyrics_lookup import clean_lyrics, saved_lyrics_belong

    lrclib = Lrclib({"周杰倫": [row(5, 269, artist="周杰倫", track="晴天")]})
    answer = lookup_lyrics(jay_query(), fetch_json=lrclib, aliases=aliases_of("周杰倫", "周杰伦"))
    assert answer.lyrics["by_alias"] is True
    kept = clean_lyrics({**answer.lyrics, "v": 1})
    assert kept["by_alias"] is True
    # Same script, other name: the mark is what lets it stand.
    latin = {**kept, "artist": "Chou Jie-lun"}
    assert saved_lyrics_belong(latin, jay_query())
    assert not saved_lyrics_belong({**latin, "by_alias": False}, jay_query())


def test_a_korean_name_finds_the_song_filed_under_the_latin_one():
    q = LyricsQuery(
        artist="아이유", track="밤편지", album="", duration=253.0, artist_ids=(IU_MBID,)
    )
    iu = row(6, 253, artist="IU", track="밤편지 (Through the Night)")
    lrclib = Lrclib({"IU": [iu]})
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of("IU", "아이유"))
    assert answer.lyrics["lrclib_id"] == 6


def test_a_name_in_two_scripts_is_searched_by_each_with_nothing_else_known():
    q = jay_query(artist="周杰倫 Jay Chou", artist_ids=())
    lrclib = Lrclib({"周杰伦": [row(7, 269, artist="周杰伦", track="晴天")]})
    answer = lookup_lyrics(q, fetch_json=lrclib)
    assert answer.lyrics["lrclib_id"] == 7


def test_another_artists_song_of_that_name_is_still_neither_kept_nor_offered():
    # Teresa Teng never sang 晴天: none of her names makes Jay Chou's hers.
    q = jay_query(artist="Teresa Teng")
    lrclib = Lrclib({"q:晴天": Q_SUNNY_DAY, "鄧麗君": [], "邓丽君": []})
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of("鄧麗君", "邓丽君"))
    assert answer.lyrics is None and answer.others == []


def test_other_names_are_not_asked_for_when_the_song_was_found():
    lrclib = Lrclib({"Jay Chou": [row(8, 269, artist="Jay Chou", track="晴天")]})
    aliases = aliases_of("周杰倫")
    answer = lookup_lyrics(jay_query(), fetch_json=lrclib, aliases=aliases)
    assert answer.lyrics["lrclib_id"] == 8
    assert aliases.calls == []
    assert lrclib.asked == ["Jay Chou"]


def test_a_version_of_another_length_under_another_name_is_offered_not_kept():
    lrclib = Lrclib({"周杰倫": [row(9, 180, artist="周杰倫", track="晴天")]})
    answer = lookup_lyrics(jay_query(), fetch_json=lrclib, aliases=aliases_of("周杰倫"))
    assert answer.lyrics["lrclib_id"] == 9
    assert answer.lyrics["timing"] == "unverified"


def test_the_identity_and_the_band_say_who_the_artist_is():
    job = Job(id="abcdef000042", title="x", duration_sec=269.0)
    identity = {
        "source": "musicbrainz",
        "title": "晴天",
        "artist": "周杰倫",
        "artist_mbids": [JAY_MBID],
    }
    band = {"id": "Q238819", "name": "周杰倫", "englishName": "Jay Chou"}
    q = build_query(job, identity=identity, band=band)
    assert q.artist_ids == (JAY_MBID,) and q.band_id == "Q238819"
    tagged = build_query(job, audio_tags={"artist": "Jay Chou", "title": "晴天"})
    assert tagged.artist_ids == () and tagged.band_id == ""
