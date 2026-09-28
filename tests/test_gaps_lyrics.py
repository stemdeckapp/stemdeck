"""Lyrics branches the other lyrics suites leave unpinned.

The lookup's step (d), the artist's other names, and step (e), the song's
other titles (lyrics_lookup.lookup_lyrics): how many are tried, and what is
kept. Where the other names come from (name_aliases.artist_aliases): the
Wikidata cache and its expiry, and one source failing while the other
answers. Mending stripped lyrics from the vocals (transcribe.mend_lyrics):
the gates that keep the worker from starting, and the check that it never
writes over lyrics it was not made from.

No network: conftest answers LRCLIB, MusicBrainz and Wikidata as offline, and
tests that want an answer stand in for them.
"""

from __future__ import annotations

import pytest

import app.pipeline.name_aliases as name_aliases
from app.core import settings as settings_mod
from app.pipeline import musicbrainz
from app.pipeline import transcribe as tr
from app.pipeline.lyrics_lookup import LyricsQuery, lookup_lyrics, lyrics_path, read_lyrics
from tests.test_pipeline_transcribe import HYMN, _answering, _heard, _job, _stripped, _with_lyrics

JAY_MBID = "a223958d-5c56-4b2c-a30a-87e357bc121b"
IU_MBID = "b9545342-1e6d-4dae-84ac-013374ad8d7c"
THIRD_MBID = "5c5cb762-d95e-47af-a7bf-35171eeab8e6"


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
    """Stands in for _fetch_json: a search by (artist, track) or by "q:<name>"
    from ``searches``; /get finds nothing. Records every search."""

    def __init__(self, searches=None):
        self.searches = searches or {}
        self.asked: list[tuple[str, str]] = []

    def __call__(self, endpoint, params):
        if endpoint == "get":
            return None
        key = (
            (params["artist_name"], params["track_name"])
            if "q" not in params
            else ("q", params["q"])
        )
        self.asked.append(key)
        return self.searches.get(key, [])


def aliases_of(*names):
    return lambda query, cancelled: list(names)


# ── (d) the artist's other names ──


def test_at_most_three_other_names_are_searched_by():
    lrclib = Lrclib()
    q = LyricsQuery(artist="Jay Chou", track="晴天", album="", duration=269.0)
    others = ("周杰倫", "周杰伦", "ジェイ・チョウ", "Zhou Jielun", "Jay Chou Official")
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of(*others))
    by_other_name = [artist for artist, _track in lrclib.asked if artist in others]
    assert by_other_name == list(others[:3])
    assert answer.lyrics is None


def test_the_artists_own_name_from_its_aliases_is_not_searched_again():
    lrclib = Lrclib()
    q = LyricsQuery(artist="Jay Chou", track="晴天", album="", duration=269.0)
    lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of("jay chou", "周杰倫"))
    assert [a for a, _t in lrclib.asked if a != "q"] == ["Jay Chou", "周杰倫"]


def test_no_artist_asks_for_no_other_names():
    asked = []
    q = LyricsQuery(artist="", track="晴天", album="", duration=269.0, artist_ids=(JAY_MBID,))
    lookup_lyrics(q, fetch_json=Lrclib(), aliases=lambda query, c: asked.append(1) or [])
    assert asked == []


# ── (e) the song's other titles ──


def iu_query(**fields):
    base = {"artist": "IU", "track": "좋은 날", "album": "", "duration": 233.0}
    return LyricsQuery(**{**base, **fields})


def test_at_most_two_other_titles_are_searched_by():
    lrclib = Lrclib({("IU", "Third Title"): [row(3, 233, artist="IU", track="Third Title")]})
    q = iu_query(track_aliases=("Good Day", "Joeun Nal", "Third Title"))
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of())
    assert ("IU", "Third Title") not in lrclib.asked
    assert [t for a, t in lrclib.asked if a == "IU"] == ["좋은 날", "Good Day", "Joeun Nal"]
    assert answer.lyrics is None


def test_the_second_title_is_asked_only_when_the_first_found_nothing():
    lrclib = Lrclib({("IU", "Good Day"): [row(7, 233, artist="IU", track="Good Day")]})
    q = iu_query(track_aliases=("Good Day", "Joeun Nal"))
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of())
    assert answer.lyrics["lrclib_id"] == 7 and answer.lyrics["timing"] == "exact"
    assert ("IU", "Joeun Nal") not in lrclib.asked


def test_another_artists_song_under_the_other_title_is_neither_kept_nor_offered():
    """ "Good Day" is also somebody else's song: a title alias counts only by a
    name the track is known by."""
    lrclib = Lrclib({("IU", "Good Day"): [row(8, 233, artist="Surface", track="Good Day")]})
    answer = lookup_lyrics(
        iu_query(track_aliases=("Good Day",)), fetch_json=lrclib, aliases=aliases_of()
    )
    assert answer.lyrics is None and answer.others == []


def test_the_other_title_in_another_length_is_kept_unverified():
    lrclib = Lrclib({("IU", "Good Day"): [row(9, 200, artist="IU", track="Good Day")]})
    answer = lookup_lyrics(
        iu_query(track_aliases=("Good Day",)), fetch_json=lrclib, aliases=aliases_of()
    )
    assert answer.lyrics["lrclib_id"] == 9
    assert answer.lyrics["timing"] == "unverified"


def test_other_titles_are_not_asked_when_the_song_was_found_in_another_length():
    """(e) is for a song not found at all: the artist's own version of it,
    whatever its length, is the song."""
    lrclib = Lrclib({("IU", "좋은 날"): [row(10, 200, artist="IU", track="좋은 날")]})
    answer = lookup_lyrics(
        iu_query(track_aliases=("Good Day",)), fetch_json=lrclib, aliases=aliases_of()
    )
    assert answer.lyrics["lrclib_id"] == 10
    assert ("IU", "Good Day") not in lrclib.asked


def test_other_titles_are_searched_by_the_artist_only():
    lrclib = Lrclib()
    q = LyricsQuery(
        artist="", track="좋은 날", album="", duration=233.0, track_aliases=("Good Day",)
    )
    lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of())
    assert all(t != "Good Day" for _a, t in lrclib.asked)


def test_an_other_title_found_under_an_other_name_is_kept():
    """(d) widened the names before (e) asks: 아이유's "Good Day" is IU's."""
    lrclib = Lrclib({("IU", "Good Day"): [row(11, 233, artist="아이유", track="Good Day")]})
    q = iu_query(track_aliases=("Good Day",))
    answer = lookup_lyrics(q, fetch_json=lrclib, aliases=aliases_of("아이유"))
    assert answer.lyrics["lrclib_id"] == 11


# ── where the other names come from ──

JAY = {
    "id": JAY_MBID,
    "name": "周杰倫",
    "aliases": [
        {"name": "Jay Chou", "locale": "en", "type": "Artist name", "primary": True},
        {"name": "周杰", "type": "Legal name", "primary": True},
    ],
}
IU = {"id": IU_MBID, "name": "IU", "aliases": [{"name": "아이유", "type": "Artist name"}]}


class MusicBrainz:
    def __init__(self, artists, failing=()):
        self.artists = artists
        self.failing = set(failing)
        self.asked: list[str] = []

    def __call__(self, path, params, **_kwargs):
        mbid = path.removeprefix("artist/")
        self.asked.append(mbid)
        if mbid in self.failing:
            raise OSError("no connection")
        return self.artists[mbid]


class Wikidata:
    def __init__(self, labels, failing=0):
        self.labels = labels
        self.failing = failing
        self.asked = 0

    def __call__(self, params):
        self.asked += 1
        if self.failing:
            self.failing -= 1
            raise OSError("no connection")
        return {
            "entities": {
                params["ids"]: {"labels": {k: {"value": v} for k, v in self.labels.items()}}
            }
        }


def test_a_primary_legal_name_is_a_name():
    """A legal name is left out only when it is not primary."""
    assert "周杰" in name_aliases.musicbrainz_names(JAY)
    not_primary = {**JAY, "aliases": [{"name": "周杰", "type": "Legal name", "primary": False}]}
    assert "周杰" not in name_aliases.musicbrainz_names(not_primary)


def test_one_credited_artist_failing_leaves_the_others_names(monkeypatch):
    mb = MusicBrainz({IU_MBID: IU}, failing={JAY_MBID})
    monkeypatch.setattr(musicbrainz, "_fetch_json", mb)
    wikidata = Wikidata({"en": "IU", "ko": "아이유"})
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", wikidata)
    names = name_aliases.artist_aliases([JAY_MBID, IU_MBID], "Q20145")
    assert names == ["IU", "아이유"]
    assert mb.asked == [JAY_MBID, IU_MBID] and wikidata.asked == 1


def test_only_the_first_two_credited_artists_are_asked(monkeypatch):
    mb = MusicBrainz({JAY_MBID: JAY, IU_MBID: IU, THIRD_MBID: {"name": "Third"}})
    monkeypatch.setattr(musicbrainz, "_fetch_json", mb)
    names = name_aliases.artist_aliases([JAY_MBID, IU_MBID, THIRD_MBID])
    assert mb.asked == [JAY_MBID, IU_MBID]
    assert "Third" not in names


def test_a_cancel_stops_before_the_next_source(monkeypatch):
    mb = MusicBrainz({JAY_MBID: JAY, IU_MBID: IU})
    monkeypatch.setattr(musicbrainz, "_fetch_json", mb)
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", lambda p: pytest.fail("asked"))
    names = name_aliases.artist_aliases([JAY_MBID, IU_MBID], "Q1", cancelled=lambda: bool(mb.asked))
    assert mb.asked == [JAY_MBID]
    assert "Jay Chou" in names, "what was found before the cancel is kept"


def test_wikidata_is_kept_for_a_day_then_asked_again(monkeypatch):
    wikidata = Wikidata({"en": "IU", "ko": "아이유"})
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", wikidata)
    now = [1_000_000.0]
    monkeypatch.setattr(name_aliases.time, "time", lambda: now[0])
    assert name_aliases.artist_aliases([], "Q20145") == ["IU", "아이유"]
    now[0] += name_aliases._WIKIDATA_KEEP_SEC - 1
    name_aliases.artist_aliases([], "Q20145")
    assert wikidata.asked == 1
    now[0] += 2
    name_aliases.artist_aliases([], "Q20145")
    assert wikidata.asked == 2


def test_wikidata_failing_is_not_remembered(monkeypatch):
    wikidata = Wikidata({"en": "IU"}, failing=1)
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", wikidata)
    assert name_aliases.artist_aliases([], "Q20145") == []
    assert name_aliases.artist_aliases([], "Q20145") == ["IU"]
    assert wikidata.asked == 2


def test_the_wikidata_cache_is_bounded(monkeypatch):
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", Wikidata({"en": "X"}))
    for i in range(name_aliases._WIKIDATA_KEEP_MAX + 5):
        name_aliases.artist_aliases([], f"Q{i + 1}")
    assert len(name_aliases._wikidata_kept) <= name_aliases._WIKIDATA_KEEP_MAX


def test_an_item_wikidata_does_not_have_gives_no_names(monkeypatch):
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", lambda p: {"entities": {}})
    assert name_aliases.artist_aliases([], "Q20145") == []


# ── mending stripped lyrics from the vocals ──


@pytest.mark.parametrize(("choice", "enabled"), [("off", False), ("auto", True), ("on", True)])
def test_mending_is_on_unless_the_setting_is_off(choice, enabled):
    settings_mod.set_transcribe_lyrics(choice)
    assert settings_mod.lyrics_mending_enabled() is enabled


def test_mending_with_no_vocals_stem_never_starts_the_worker(tmp_path, monkeypatch):
    seen: list = []
    _answering(tmp_path, monkeypatch, _heard(HYMN), seen)
    job_dir = _with_lyrics(tmp_path, _stripped(HYMN))
    (job_dir / "stems" / "vocals.wav").unlink()
    assert tr.transcribe_lyrics(_job(has_lyrics=True), job_dir) is False
    assert seen == []


def test_mending_with_near_silent_vocals_never_starts_the_worker(tmp_path, monkeypatch):
    seen: list = []
    _answering(tmp_path, monkeypatch, _heard(HYMN), seen)
    job_dir = _with_lyrics(tmp_path, _stripped(HYMN))
    job = _job(has_lyrics=True, stem_presence={"vocals": 0})
    assert tr.transcribe_lyrics(job, job_dir) is False
    assert seen == []


def test_a_mend_never_lands_on_lyrics_it_was_not_made_from(tmp_path, monkeypatch):
    """lyrics.json changed while the worker ran: the mend is dropped rather
    than written over them."""
    _answering(tmp_path, monkeypatch, _heard(HYMN))
    job_dir = _with_lyrics(tmp_path, _stripped(HYMN))
    reads = []

    def read_changed(path):
        entry = read_lyrics(path)
        reads.append(1)
        if len(reads) > 1 and entry:
            entry = {**entry, "lrclib_id": 43}
        return entry

    monkeypatch.setattr(tr, "read_lyrics", read_changed)
    before = lyrics_path(job_dir).read_bytes()
    assert tr.transcribe_lyrics(_job(has_lyrics=True), job_dir) is False
    assert lyrics_path(job_dir).read_bytes() == before
    assert len(reads) == 2


def test_a_worker_answer_without_segments_mends_nothing(tmp_path, monkeypatch):
    _answering(tmp_path, monkeypatch, _heard("", "pl"))
    job_dir = _with_lyrics(tmp_path, _stripped(HYMN))
    before = lyrics_path(job_dir).read_bytes()
    assert tr.transcribe_lyrics(_job(has_lyrics=True), job_dir) is False
    assert lyrics_path(job_dir).read_bytes() == before
