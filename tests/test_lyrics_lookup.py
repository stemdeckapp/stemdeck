"""Lyrics found on LRCLIB while a job separates, kept as lyrics.json.

The ranking and title cleaning are ports of the page's (static/js/
lyricsLookup.js), so their cases are the ones in tests/js/lyrics-lookup.test.mjs.
The cascade, the file, and the thread beside the pipeline follow. No network:
conftest answers LRCLIB as offline, and tests that want an answer stand in
for it.
"""

from __future__ import annotations

import json
import threading
import time
import unicodedata
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import app.pipeline.lyrics_lookup as ll
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs, _recover_done_job
from app.pipeline.lyrics_lookup import (
    LyricsLookup,
    LyricsQuery,
    build_query,
    clean_lyrics,
    copy_lyrics,
    find_lyrics,
    has_synced_lines,
    keep_answer,
    lookup_lyrics,
    lyrics_settled,
    rank_matches,
    read_candidates,
    read_lyrics,
    song_from_title,
    write_candidates,
    write_lyrics,
)
from app.pipeline.runner import run_local_pipeline, run_pipeline

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_FETCH_JSON = ll._fetch_json

EN_DASH = chr(0x2013)


def row(
    lrclib_id, duration, kind="synced", *, artist="Dream Theater", track="Metropolis", album=""
):
    """An LRCLIB row, as tests/js/lyrics-lookup.test.mjs builds them."""
    return {
        "id": lrclib_id,
        "trackName": track,
        "artistName": artist,
        "albumName": album,
        "duration": duration,
        "instrumental": kind == "instrumental",
        "syncedLyrics": f"[00:01.00]line {lrclib_id}" if kind == "synced" else None,
        "plainLyrics": f"line {lrclib_id}" if kind in ("synced", "plain") else None,
    }


class Lrclib:
    """Stands in for _fetch_json: answers /get with ``exact`` and a search by
    its artist (or "q:<name>" for a name-only search) from ``searches``, and
    records what was asked."""

    def __init__(self, exact=None, searches=None, *, gate=None, fail=None):
        self.exact = exact
        self.searches = searches or {}
        self.gate = gate
        self.fail = fail
        self.asked: list[tuple[str, dict[str, str]]] = []
        self.called = threading.Event()

    def __call__(self, endpoint, params):
        self.asked.append((endpoint, dict(params)))
        self.called.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail is not None:
            raise self.fail
        if endpoint == "get":
            if isinstance(self.exact, Exception):
                raise self.exact
            return self.exact
        key = params.get("artist_name") or f"q:{params['q']}"
        return self.searches.get(key, [])

    @property
    def searched(self) -> list[str]:
        return [p.get("artist_name") or f"q:{p['q']}" for e, p in self.asked if e == "search"]


def kept(q: LyricsQuery, **kwargs):
    """The lyrics a lookup kept, or None."""
    answer = lookup_lyrics(q, **kwargs)
    return answer.lyrics if answer else None


def query(**fields) -> LyricsQuery:
    base = {"artist": "Dream Theater", "track": "Metropolis", "album": "", "duration": 572.0}
    return LyricsQuery(**{**base, **fields})


# ── ranking: the cases in tests/js/lyrics-lookup.test.mjs ──


def ids(matches):
    return [m["lrclib_id"] for m in matches]


def test_closest_length_first_synced_preferred_within_a_few_seconds_empty_rows_dropped():
    rows = [row(1, 769), row(2, 571, "plain"), row(3, 573), row(4, 572, "none"), row(5, 590)]
    assert ids(rank_matches(rows, 572)) == [3, 2, 5, 1]


def test_an_instrumental_row_is_kept():
    assert len(rank_matches([row(9, 100, "instrumental")], 100)) == 1


def test_with_no_length_known_lrclibs_order_synced_first():
    assert ids(rank_matches([row(1, 10, "plain"), row(2, 500)], 0)) == [2, 1]


def test_not_a_list_gives_nothing():
    assert rank_matches(None) == [] and rank_matches({"error": 1}) == []


def test_a_row_becomes_a_version_of_lyrics_json():
    [version] = rank_matches([row(7, 200.5, album="Images and Words")], 0)
    assert version == {
        "v": 1,
        "source": "lrclib",
        "track": "Metropolis",
        "artist": "Dream Theater",
        "album": "Images and Words",
        "duration": 200.5,
        "synced": "[00:01.00]line 7",
        "plain": "line 7",
        "instrumental": False,
        "lrclib_id": 7,
    }


@pytest.mark.parametrize(
    ("title", "artist", "want"),
    [
        ("Dream Theater - Pull Me Under (Official Video)", "Dream Theater", "Pull Me Under"),
        (f"Bohemian Rhapsody {EN_DASH} Queen [HD]", "Queen", "Bohemian Rhapsody"),
        (
            'Metropolis - Part I: "The Miracle and the Sleeper"',
            "Dream Theater",
            "Metropolis - Part I: The Miracle and the Sleeper",
        ),
        ("Jay-Z ft. Alicia Keys - Empire State of Mind", "Jay-Z", "Empire State of Mind"),
        ("Shadow Of The Day (Shadow Remix)", "", "Shadow Of The Day (Shadow Remix)"),
        ("Hello (Official Music Video) (Remastered 2015)", "", "Hello"),
    ],
)
def test_the_song_from_a_title(title, artist, want):
    assert song_from_title(title, artist) == want


def test_lrc_is_told_from_plain_text():
    assert has_synced_lines("[ar:Dream Theater]\n[01:58.63] Arrived early May")
    assert not has_synced_lines("[ar:Dream Theater]\nArrived early May")
    assert not has_synced_lines("")


# ── what is looked up ──


def test_the_identity_is_looked_up_before_the_tags():
    job = Job(id="abcdefabc300", duration_sec=231.0, audio_tags={"artist": "x", "title": "y"})
    job.identity = {
        "title": "Popular",
        "artist": "Ariana Grande",
        "album": "Wicked: The Soundtrack",
    }
    q = build_query(job)
    assert (q.artist, q.track, q.album, q.duration) == (
        "Ariana Grande",
        "Popular",
        "Wicked: The Soundtrack",
        231.0,
    )
    assert q.album_artists == ("Wicked", "Wicked: The Soundtrack")


def test_the_tags_name_the_song_cleaned_of_video_noise():
    job = Job(
        id="abcdefabc301",
        audio_tags={"artist": "Dream Theater", "title": "Pull Me Under (Official Video)"},
    )
    q = build_query(job)
    assert (q.artist, q.track, q.album) == ("Dream Theater", "Pull Me Under", "")


def test_the_saved_band_and_the_title_when_there_are_no_tags():
    band = {"id": "Q162586", "name": "Dream Theater", "englishName": "Dream Theater"}
    job = Job(id="abcdefabc302", title="Dream Theater - Pull Me Under [HD]", artist=band)
    q = build_query(job)
    assert (q.artist, q.track) == ("Dream Theater", "Pull Me Under")


def test_nothing_known_is_nothing_to_look_up():
    assert build_query(Job(id="abcdefabc303", title="Some video")) is None


def test_the_work_a_cast_recording_belongs_to_is_tried_as_the_artist():
    job = Job(id="abcdefabc304", audio_tags={"artist": "Idina Menzel", "title": "Defying Gravity"})
    job.work = {"id": "Q1", "kind": "musical", "name": "Wicked", "englishName": "Wicked"}
    assert build_query(job).album_artists == ("Wicked",)


# ── the cascade ──


def test_an_exact_match_is_kept_and_the_artists_versions_offered_beside_it():
    lrclib = Lrclib(
        exact=row(10, 572, artist="Dream Theater"),
        searches={"Dream Theater": [row(11, 769), row(10, 572), row(12, 571)]},
    )
    found = kept(query(album="Images and Words"), fetch_json=lrclib)
    assert found["lrclib_id"] == 10
    assert ids(found["others"]) == [12, 11], "ranked, without the one kept"
    assert lrclib.asked[0] == (
        "get",
        {
            "artist_name": "Dream Theater",
            "track_name": "Metropolis",
            "album_name": "Images and Words",
            "duration": "572",
        },
    )
    assert lrclib.searched == ["Dream Theater"]


def test_no_exact_match_keeps_the_artists_version_closest_in_length():
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 769), row(3, 573), row(2, 571, "plain")]})
    found = kept(query(), fetch_json=lrclib)
    assert found["lrclib_id"] == 3
    assert ids(found["others"]) == [2, 1]
    assert lrclib.searched == ["Dream Theater"], "nothing asked past a found answer"


def test_an_exact_match_that_errors_still_searches():
    lrclib = Lrclib(
        exact=urllib.error.HTTPError("u", 500, "down", {}, None),
        searches={"Dream Theater": [row(3, 573)]},
    )
    assert kept(query(), fetch_json=lrclib)["lrclib_id"] == 3


def test_others_are_capped():
    lrclib = Lrclib(searches={"Dream Theater": [row(i, 572) for i in range(1, 30)]})
    found = kept(query(), fetch_json=lrclib)
    assert len(found["others"]) == 11


def test_a_cast_recording_is_found_under_the_shows_name():
    """LRCLIB files "Popular" under the artist "Wicked", not the singer."""
    lrclib = Lrclib(
        searches={
            "Wicked": [
                row(20, 300, artist="Wicked", track="Popular"),
                row(21, 225.7, artist="Wicked", track="Popular"),
            ]
        }
    )
    q = query(
        artist="Kristin Chenoweth",
        track="Popular",
        album="Wicked (Original Broadway Cast Recording)",
        duration=225.6,
        album_artists=("Wicked", "Wicked (Original Broadway Cast Recording)"),
    )
    found = kept(q, fetch_json=lrclib)
    assert found["lrclib_id"] == 21
    assert lrclib.searched == ["Kristin Chenoweth", "Wicked"]


def test_the_shows_name_is_held_to_the_tracks_length():
    lrclib = Lrclib(
        searches={
            "Wicked": [row(20, 300, artist="Wicked", track="Popular")],
            "q:Popular": [row(30, 190, track="Popular"), row(31, 226.5, track="Popular")],
        }
    )
    q = query(track="Popular", duration=225.6, album_artists=("Wicked",))
    found = kept(q, fetch_json=lrclib)
    assert found["lrclib_id"] == 31, "the name alone found the version the length of the track"
    assert lrclib.searched == ["Dream Theater", "Wicked", "q:Popular"]
    assert 20 in ids(found["others"])


def test_the_name_alone_is_kept_only_within_three_seconds():
    lrclib = Lrclib(searches={"q:Metropolis": [row(40, 575.5), row(41, 600)]})
    answer = lookup_lyrics(query(), fetch_json=lrclib)
    assert answer.lyrics is None
    assert ids(answer.others) == [40, 41], "kept to offer instead"
    lrclib = Lrclib(searches={"q:Metropolis": [row(40, 574.9), row(41, 600)]})
    assert kept(query(), fetch_json=lrclib)["lrclib_id"] == 40


def test_the_name_alone_is_never_asked_without_the_tracks_length():
    lrclib = Lrclib(searches={"q:Metropolis": [row(40, 572)]})
    assert kept(query(duration=0.0), fetch_json=lrclib) is None
    assert "q:Metropolis" not in lrclib.searched


def test_a_track_known_by_no_name_gets_no_lyrics():
    """The name alone finds anybody's song: with nobody to hold it to, none
    is kept and LRCLIB is not asked."""
    lrclib = Lrclib(searches={"q:Metropolis": [row(40, 572)]})
    answer = lookup_lyrics(query(artist=""), fetch_json=lrclib)
    assert answer is not None and answer.lyrics is None and answer.others == []
    assert lrclib.asked == []


def test_another_artists_song_of_that_name_is_neither_kept_nor_offered():
    lrclib = Lrclib(
        exact=row(10, 572, artist="Metropolis Tribute Orchestra"),
        searches={
            "Dream Theater": [
                row(11, 572, track="Metropolis Part 2"),
                row(12, 572, artist="Kesha"),
            ],
            "q:Metropolis": [row(13, 572, artist="Someone Else")],
        },
    )
    answer = lookup_lyrics(query(), fetch_json=lrclib)
    assert answer.lyrics is None
    assert answer.others == [], "not offered either: the tab would say it is this song"


def test_a_credited_artist_or_a_song_tail_still_belongs():
    q = query(
        artist="Keala Settle & The Greatest Showman Ensemble", track="This Is Me", duration=235.0
    )
    tail = row(50, 234.9, artist="Keala Settle", track="This Is Me - From The Greatest Showman")
    found = kept(q, fetch_json=Lrclib(searches={q.artist: [tail]}))
    assert found["lrclib_id"] == 50


@pytest.mark.parametrize(
    ("found", "names", "same"),
    [
        ("The Beatles", ["Beatles"], True),
        ("Beyonce", ["Beyonc" + chr(0xE9)], True),
        ("Keala Settle", ["Keala Settle & The Greatest Showman Ensemble"], True),
        ("The Greatest Showman Cast", ["The Greatest Showman"], True),
        ("Pink", ["Pink Floyd"], False),
        ("DJ", ["DJ & Someone Else"], False),
        ("Kesha", ["Keala Settle"], False),
        ("", ["Keala Settle"], False),
        ("Keala Settle", [""], False),
    ],
)
def test_same_artist(found, names, same):
    assert ll.same_artist(found, names) is same


@pytest.mark.parametrize(
    ("found", "song", "same"),
    [
        ("This Is Me", "this is me", True),
        ("This Is Me (feat. Someone) [Live]", "This Is Me", True),
        ("This Is Me - From The Greatest Showman", "This Is Me", True),
        ("Part I - Dawn", "Part I - Dusk", False),
        ("This Is Not Me", "This Is Me", False),
        ("", "This Is Me", False),
    ],
)
def test_same_song(found, song, same):
    assert ll.same_song(found, song) is same


def test_lyrics_the_file_carried_win_and_ask_nothing():
    lrclib = Lrclib(searches={"Dream Theater": [row(3, 572)]})
    lrc = "[00:01.00]Our own words"
    found = kept(query(embedded=lrc), fetch_json=lrclib)
    assert (found["source"], found["synced"], found["plain"]) == ("file", lrc, "")
    assert found["others"] == [] and found["lrclib_id"] is None
    plain = kept(query(embedded="Just words"), fetch_json=lrclib)
    assert (plain["synced"], plain["plain"]) == ("", "Just words")
    assert lrclib.asked == []


def test_a_version_the_tracks_length_is_kept_with_exact_timing():
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 769), row(3, 573)]})
    assert kept(query(), fetch_json=lrclib)["timing"] == "exact"


def test_the_song_in_another_length_is_kept_with_timing_unverified():
    """The words from LRCLIB beat a transcription's: kept, to be moved onto
    the track once there is a vocals stem (lyrics_align.py)."""
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 769), row(2, 590)]})
    found = kept(query(), fetch_json=lrclib)
    assert (found["lrclib_id"], found["timing"]) == (2, "unverified")
    assert ids(found["others"]) == [1]
    assert lrclib.searched == ["Dream Theater", "q:Metropolis"], "the length was looked for first"


def test_a_version_the_tracks_length_under_another_name_wins_over_the_song_in_another():
    lrclib = Lrclib(searches={"Dream Theater": [row(2, 590)], "q:Metropolis": [row(40, 572)]})
    found = kept(query(), fetch_json=lrclib)
    assert (found["lrclib_id"], found["timing"]) == (40, "exact")


def test_a_synced_version_is_preferred_when_none_is_the_tracks_length():
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 590, "plain"), row(2, 700)]})
    found = kept(query(), fetch_json=lrclib)
    assert (found["lrclib_id"], found["timing"]) == (2, "unverified")
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 590, "plain")]})
    found = kept(query(), fetch_json=lrclib)
    assert (found["lrclib_id"], found["timing"]) == (1, "exact"), "plain text has no timing"


def test_the_shows_song_in_another_length_is_kept_too():
    lrclib = Lrclib(searches={"Wicked": [row(20, 300, artist="Wicked", track="Popular")]})
    q = query(artist="Kristin Chenoweth", track="Popular", duration=225.6,
              album_artists=("Wicked", "Wicked (Original Broadway Cast Recording)"))  # fmt: skip
    found = kept(q, fetch_json=lrclib)
    assert (found["lrclib_id"], found["timing"]) == (20, "unverified")
    assert "Wicked (Original Broadway Cast Recording)" not in lrclib.searched


def test_the_name_alone_in_another_length_is_never_kept():
    lrclib = Lrclib(searches={"q:Metropolis": [row(40, 600)]})
    answer = lookup_lyrics(query(), fetch_json=lrclib)
    assert answer.lyrics is None and ids(answer.others) == [40]


def test_an_exact_match_of_another_length_is_not_preferred():
    lrclib = Lrclib(exact=row(10, 600), searches={"Dream Theater": [row(3, 571)]})
    assert kept(query(), fetch_json=lrclib)["lrclib_id"] == 3


def test_with_the_length_unknown_the_artists_best_is_kept():
    lrclib = Lrclib(searches={"Dream Theater": [row(1, 769), row(2, 590)]})
    assert kept(query(duration=0.0), fetch_json=lrclib)["lrclib_id"] == 1


def test_nothing_on_lrclib_is_an_answer_with_nothing_to_offer():
    answer = lookup_lyrics(query(), fetch_json=Lrclib())
    assert answer.lyrics is None and answer.others == []


def test_out_of_time_with_nothing_kept_is_no_answer():
    """Not "LRCLIB has nothing": it is asked again next time."""

    def slow(endpoint, params):
        time.sleep(0.2)

    assert lookup_lyrics(query(), fetch_json=slow, budget=0.1) is None


def test_no_connection_is_no_lyrics_and_no_error():
    # conftest's stand-in for the network is offline.
    assert find_lyrics(query()) is None


def test_a_cancel_between_requests_stops_the_next():
    lrclib = Lrclib()
    asked_once = lambda: len(lrclib.asked) >= 1  # noqa: E731
    assert lookup_lyrics(query(), fetch_json=lrclib, cancelled=asked_once) is None
    assert len(lrclib.asked) == 1


def test_out_of_time_asks_nothing_more_and_keeps_what_it_found():
    asked = []

    def slow(endpoint, params):
        asked.append(endpoint)
        time.sleep(0.2)
        return row(10, 572)

    found = kept(query(), fetch_json=slow, budget=0.1)
    assert found["lrclib_id"] == 10 and found["others"] == []
    assert asked == ["get"], "no search started past the budget"
    assert lookup_lyrics(query(), fetch_json=Lrclib(), budget=-1) is None


def test_the_request_names_stemdeck(monkeypatch):
    """LRCLIB asks clients to name themselves and their homepage."""
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return b"[]"

    def urlopen(request, timeout, context):
        seen["url"] = request.full_url
        seen["agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ll.urllib.request, "urlopen", urlopen)
    assert _REAL_FETCH_JSON("search", {"artist_name": "Dream Theater", "track_name": "X"}) == []
    assert seen["url"].startswith("https://lrclib.net/api/search?")
    assert "artist_name=Dream+Theater" in seen["url"]
    assert "StemDeck" in seen["agent"] and "github.com" in seen["agent"]
    assert seen["timeout"] == ll.TIMEOUT_LYRICS_LOOKUP


def test_lrclibs_404_is_no_such_track(monkeypatch):
    def urlopen(request, timeout, context):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(ll.urllib.request, "urlopen", urlopen)
    assert _REAL_FETCH_JSON("get", {"artist_name": "A", "track_name": "B"}) is None


# ── lyrics.json ──

KEYS = {
    "v",
    "source",
    "track",
    "artist",
    "album",
    "duration",
    "synced",
    "plain",
    "instrumental",
    "timing",
    "others",
    "lrclib_id",
}


def _found() -> dict:
    lrclib = Lrclib(searches={"Dream Theater": [row(3, 573), row(1, 769)]})
    return kept(query(), fetch_json=lrclib)


def test_the_file_has_the_designs_shape(tmp_path: Path):
    job = Job(id="abcdefabc310")
    assert write_lyrics(job, tmp_path, _found())
    data = json.loads((tmp_path / "lyrics.json").read_text(encoding="utf-8"))
    assert set(data) == KEYS
    assert data["v"] == 1 and data["source"] == "lrclib" and data["lrclib_id"] == 3
    assert [set(o) for o in data["others"]] == [KEYS - {"others", "timing"}]
    assert data["timing"] == "exact"
    assert job.has_lyrics is True
    assert read_lyrics(tmp_path) == data
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"


def test_a_file_that_cannot_be_written_leaves_the_job_without_lyrics(tmp_path: Path):
    job = Job(id="abcdefabc311")
    assert not write_lyrics(job, tmp_path / "gone", _found())
    assert job.has_lyrics is False


@pytest.mark.parametrize(
    "damage",
    [
        {"v": 2},
        {"timing": "roughly"},
        {"source": "somewhere"},
        {"duration": float("nan")},
        {"lrclib_id": "7"},
        {"synced": "", "plain": "", "instrumental": False},
    ],
)
def test_a_damaged_file_is_not_lyrics(damage):
    assert clean_lyrics({**_found(), **damage}) is None


def test_nothing_kept_leaves_the_versions_and_when_lrclib_was_asked(tmp_path: Path):
    job = Job(id="abcdefabc312")
    answer = lookup_lyrics(query(), fetch_json=Lrclib(searches={"q:Metropolis": [row(1, 769)]}))
    before = time.time()
    assert keep_answer(job, tmp_path, answer) is False
    data = json.loads((tmp_path / "lyrics_candidates.json").read_text(encoding="utf-8"))
    assert set(data) == {"v", "searched_at", "others"}
    assert data["searched_at"] >= before
    assert [o["lrclib_id"] for o in data["others"]] == [1]
    assert [set(o) for o in data["others"]] == [KEYS - {"others", "timing"}]
    assert not (tmp_path / "lyrics.json").exists() and job.has_lyrics is False


def test_lrclibs_nothing_is_not_asked_again_for_thirty_days(tmp_path: Path):
    assert not lyrics_settled(tmp_path)
    write_candidates(Job(id="abcdefabc313"), tmp_path, [])
    now = time.time()
    assert lyrics_settled(tmp_path, now=now)
    assert lyrics_settled(tmp_path, now=now + 29 * 86400)
    assert not lyrics_settled(tmp_path, now=now + 31 * 86400)


def test_lyrics_found_later_replace_the_candidates_and_a_transcription_keeps_them(
    tmp_path: Path,
):
    write_candidates(Job(id="abcdefabc314"), tmp_path, [_found()])
    transcribed = {**_found(), "source": "whisper", "lrclib_id": None, "others": []}
    assert write_lyrics(Job(id="abcdefabc314"), tmp_path, transcribed)
    assert read_candidates(tmp_path)["others"], "still offered beside a transcription"
    assert write_lyrics(Job(id="abcdefabc314"), tmp_path, _found())
    assert read_candidates(tmp_path) is None
    assert lyrics_settled(tmp_path, now=time.time() + 365 * 86400), "lyrics settle it for good"


def test_a_resplit_copies_the_lyrics_and_the_marker(tmp_path: Path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    assert copy_lyrics(src, dest) is False
    write_candidates(Job(id="abcdefabc315"), src, [])
    assert copy_lyrics(src, dest) is False
    assert lyrics_settled(dest)
    write_lyrics(Job(id="abcdefabc315"), src, _found())
    assert copy_lyrics(src, dest) is True
    assert read_lyrics(dest) == read_lyrics(src)


# ── in a thread beside the pipeline ──


def _tagged_job(job_id: str, **fields) -> Job:
    return Job(
        id=job_id,
        duration_sec=572.0,
        audio_tags={"artist": "Dream Theater", "title": "Metropolis"},
        **fields,
    )


def _answer():
    return Lrclib(searches={"Dream Theater": [row(3, 573), row(1, 769)]})


def test_the_answer_is_kept_once_the_pipeline_finishes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ll, "_fetch_json", _answer())
    job = _tagged_job("abcdefabc320")
    lookup = LyricsLookup.start(job, tmp_path)
    assert lookup is not None
    lookup.finish(job, tmp_path, 5)
    assert read_lyrics(tmp_path)["lrclib_id"] == 3
    assert job.has_lyrics


def test_the_thread_never_writes_itself(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    lrclib = Lrclib(searches={"Dream Theater": [row(3, 573)]}, gate=gate)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _tagged_job("abcdefabc321")
    lookup = LyricsLookup.start(job, tmp_path)
    gate.set()
    lookup._thread.join(5)
    assert not (tmp_path / "lyrics.json").exists(), "only finish() writes"
    assert job.has_lyrics is False
    lookup.finish(job, tmp_path, 5)
    assert (tmp_path / "lyrics.json").is_file()


def test_a_cancelled_job_is_never_written_to(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(searches={"Dream Theater": []}, gate=gate))
    job = _tagged_job("abcdefabc322")
    lookup = LyricsLookup.start(job, tmp_path)
    job.cancel_requested = True
    gate.set()
    lookup.finish(job, tmp_path, 5)
    assert not (tmp_path / "lyrics.json").exists()
    assert job.has_lyrics is False


def test_an_answer_too_late_is_dropped(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(exact=row(3, 572), gate=gate))
    job = _tagged_job("abcdefabc323")
    lookup = LyricsLookup.start(job, tmp_path)
    started = time.monotonic()
    lookup.finish(job, tmp_path, 0.1)
    assert time.monotonic() - started < 1
    gate.set()
    lookup._thread.join(5)
    assert not (tmp_path / "lyrics.json").exists()


def test_a_lookup_that_keeps_nothing_leaves_the_candidates(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(searches={"q:Metropolis": [row(1, 769)]}))
    job = _tagged_job("abcdefabc328")
    LyricsLookup.start(job, tmp_path).finish(job, tmp_path, 5)
    assert not (tmp_path / "lyrics.json").exists() and job.has_lyrics is False
    assert ids(read_candidates(tmp_path)["others"]) == [1]


def test_nothing_to_look_for_starts_nothing(tmp_path: Path, monkeypatch):
    lrclib = _answer()
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    assert LyricsLookup.start(Job(id="abcdefabc324", title="A video"), tmp_path) is None
    write_candidates(Job(id="abcdefabc325"), tmp_path, [])
    assert LyricsLookup.start(_tagged_job("abcdefabc325"), tmp_path) is None, "asked lately"
    (tmp_path / "lyrics.json").write_text("{}", encoding="utf-8")
    assert LyricsLookup.start(_tagged_job("abcdefabc325"), tmp_path) is None, "already has some"
    assert lrclib.asked == []


def test_the_identity_still_being_found_is_waited_for(tmp_path: Path, monkeypatch):
    """The identification runs beside the lookup and puts its answer on the
    job only when the pipeline is done, so the lookup asks it directly, in
    its own thread, and looks up what it says rather than the tags."""
    lrclib = Lrclib(
        searches={"Ariana Grande": [row(5, 231, artist="Ariana Grande", track="Popular")]}
    )
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = Job(id="abcdefabc326", duration_sec=231.0, audio_tags={"title": "popular (hd)"})
    identified = threading.Event()

    def wait_identity():
        assert identified.wait(5)
        return {"title": "Popular", "artist": "Ariana Grande", "album": "Wicked: The Soundtrack"}

    lookup = LyricsLookup.start(job, tmp_path, identity=wait_identity)
    assert lookup is not None
    identified.set()
    lookup.finish(job, tmp_path, 5)
    assert read_lyrics(tmp_path)["lrclib_id"] == 5
    assert lrclib.asked[0][1]["artist_name"] == "Ariana Grande"


def test_no_identity_falls_back_to_the_tags(tmp_path: Path, monkeypatch):
    lrclib = _answer()
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _tagged_job("abcdefabc327")
    LyricsLookup.start(job, tmp_path, identity=lambda: None).finish(job, tmp_path, 5)
    assert job.has_lyrics
    assert lrclib.searched[0] == "Dream Theater"


def test_the_flag_comes_back_from_the_file_after_a_restart(tmp_path: Path):
    for job_id, has in (("abcdefabc330", True), ("abcdefabc331", False)):
        job_dir = tmp_path / job_id
        (job_dir / "stems").mkdir(parents=True)
        (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
        (job_dir / "metadata.json").write_text(json.dumps({"title": "x"}), encoding="utf-8")
        if has:
            write_lyrics(Job(id=job_id), job_dir, _found())
        assert _recover_done_job(job_dir).has_lyrics is has


# ── the pipeline ──

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _download_with_tags(job, url, job_dir):
    job.duration_sec = 572.0
    job.audio_tags = {"artist": "Dream Theater", "title": "Metropolis"}
    return job_dir / "source.wav"


def _separation(seconds: float):
    def run_common(job, source, job_dir):
        (job_dir / "stems").mkdir(parents=True, exist_ok=True)
        time.sleep(seconds)

    return run_common


async def test_a_link_finishes_with_its_lyrics_on_disk_and_costs_no_time(
    tmp_path: Path, monkeypatch
):
    """The lookup starts once the download has the tags and runs while the job
    separates, so the job is done with lyrics.json written, the flag in its
    state and registry record, the text in neither, and waiting cost nothing."""
    lrclib = _answer()

    def answer_slowly(endpoint, params):
        time.sleep(0.1)
        return lrclib(endpoint, params)

    monkeypatch.setattr(ll, "_fetch_json", answer_slowly)
    job = Job(id="abcdefabc340")
    _jobs[job.id] = job
    try:
        with (
            patch("app.pipeline.runner.download", side_effect=_download_with_tags),
            patch("app.pipeline.runner._run_common", side_effect=_separation(0.8)),
        ):
            await run_pipeline(job, URL, tmp_path)
    finally:
        _jobs.pop(job.id, None)

    assert job.status == "done"
    assert read_lyrics(tmp_path / job.id)["lrclib_id"] == 3
    state = job.to_state()
    assert state["has_lyrics"] is True
    assert "line 3" not in json.dumps(state), "the text stays out of the state"
    assert job.stage_timings["lyrics_wait"] < 0.1, "the answer was already waiting"
    registry = (tmp_path / "registry.json").read_text(encoding="utf-8")
    [record] = [r for r in json.loads(registry)["jobs"] if r["id"] == job.id]
    assert record["has_lyrics"] is True
    assert "line 3" not in registry


async def test_a_lookup_that_hangs_does_not_hold_the_job(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ll, "_fetch_json", Lrclib(gate=gate))
    monkeypatch.setattr("app.pipeline.runner.LYRICS_LOOKUP_GRACE_SEC", 0.2)
    job = Job(id="abcdefabc341")
    started = time.monotonic()
    try:
        with (
            patch("app.pipeline.runner.download", side_effect=_download_with_tags),
            patch("app.pipeline.runner._run_common", side_effect=_separation(0)),
        ):
            await run_pipeline(job, URL, tmp_path)
    finally:
        gate.set()
    assert job.status == "done"
    assert time.monotonic() - started < 2
    assert not (tmp_path / job.id / "lyrics.json").exists()
    assert job.has_lyrics is False


async def test_no_connection_finishes_the_job_without_lyrics(tmp_path: Path):
    job = Job(id="abcdefabc342")
    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=_separation(0)),
    ):
        await run_pipeline(job, URL, tmp_path)
    assert job.status == "done"
    assert job.has_lyrics is False


async def test_an_upload_keeps_the_lyrics_it_arrived_with(tmp_path: Path, monkeypatch):
    lrclib = _answer()
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = _tagged_job("abcdefabc343", source_url="local:Metropolis")
    job.audio_tags = {**job.audio_tags, "lyrics": "[00:01.00]Our own words"}
    (tmp_path / job.id).mkdir()
    with (
        patch(
            "app.pipeline.runner._prepare_local_source",
            side_effect=lambda job, source, job_dir: job_dir / "source.wav",
        ),
        patch("app.pipeline.runner._run_common", side_effect=_separation(0.1)),
    ):
        await run_local_pipeline(job, tmp_path / job.id / "upload.flac", tmp_path)
    assert job.status == "done"
    found = read_lyrics(tmp_path / job.id)
    assert (found["source"], found["synced"]) == ("file", "[00:01.00]Our own words")
    assert lrclib.asked == []


async def test_a_job_cancelled_while_the_lookup_is_out_gets_no_lyrics(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    lrclib = Lrclib(exact=row(3, 572), gate=gate)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    job = Job(id="abcdefabc344")

    def cancelled_mid_separation(job, source, job_dir):
        assert lrclib.called.wait(5)
        job.cancel_requested = True
        gate.set()
        raise JobCancelled()

    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=cancelled_mid_separation),
    ):
        await run_pipeline(job, URL, tmp_path)
    for thread in threading.enumerate():
        if thread.name == f"lyrics-{job.id}":
            thread.join(5)
    assert job.status == "cancelled"
    assert job.has_lyrics is False
    assert not (tmp_path / job.id).exists()
    assert [e for e, _ in lrclib.asked] == ["get"], "the cancel stopped the search"


# ── letters outside ASCII ──
#
# LRCLIB holds some songs as copies that lost those letters on the way in
# (Kayah's "Nie ma, nie ma ciebie": eleven copies read "Niewinnoci biaym
# niegiem", one "Niewinnością białym śniegiem"). The anthem stands in for a
# song here, being in the public domain.

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


def _drop(text: str) -> str:
    """Every letter outside ASCII dropped: "Szablą" as "Szabl"."""
    return "".join(ch for ch in text if ord(ch) < 128)


def _fold(text: str) -> str:
    """Folded to the base letter where there is one: "Szablą" as "Szabla"."""
    return _drop(unicodedata.normalize("NFD", text))


def _as_lrclib_drops_them(text: str) -> str:
    """What LRCLIB's damaged copies hold: "ę" folded, the rest dropped."""
    return _drop(text.replace("ę", "e"))


def text_row(lrclib_id, duration, text, *, synced=True):
    return {
        **row(lrclib_id, duration),
        "syncedLyrics": text if synced else None,
        "plainLyrics": ll._LRC_TAGS.sub("", text),
    }


@pytest.mark.parametrize("strip", [_drop, _fold, _as_lrclib_drops_them])
def test_a_copy_that_lost_its_polish_letters_is_told_from_the_one_it_came_from(strip):
    intact = ll.normalise(text_row(1, 200, ANTHEM))
    stripped = ll.normalise(text_row(2, 200, strip(ANTHEM)))
    assert ll.stripped_copy(stripped, intact)
    assert not ll.stripped_copy(intact, stripped)
    assert not ll.stripped_copy(intact, intact)


@pytest.mark.parametrize(
    "intact, other",
    [
        # Another script, or its transliteration, is not a stripped copy.
        (
            "[00:01.00]Группа крови на рукаве, мой порядковый номер на рукаве",
            "[00:01.00]Gruppa krovi na rukave, moy poryadkovyy nomer na rukave",
        ),
        (
            "[00:01.00]夢ならばどれほどよかったでしょう 未だにあなたのことを夢にみる",
            "[00:01.00]Yume naraba dore hodo yokatta deshou imada ni anata no koto wo yume ni miru",
        ),
        # A stray accent proves nothing.
        (
            "[00:01.00]A café, a naïve smile, and the rest in plain English words",
            "[00:01.00]A cafe, a naive smile, and the rest in plain English words",
        ),
        # Another song entirely, written without accents.
        (ANTHEM, "[00:01.00]Jeszcze nic nie jest stracone, moja mila, gdy jestem z toba"),
    ],
)
def test_other_scripts_and_other_songs_are_not_stripped_copies(intact, other):
    a, b = ll.normalise(text_row(1, 200, intact)), ll.normalise(text_row(2, 200, other))
    assert not ll.stripped_copy(b, a)
    assert not ll.stripped_copy(a, b)


def test_ranking_puts_the_intact_copy_before_one_lrclib_ranked_first():
    rows = [
        text_row(5470091, 230.0, _as_lrclib_drops_them(ANTHEM)),
        text_row(28700266, 230.0, _as_lrclib_drops_them(ANTHEM)),
        text_row(10910419, 229.93, ANTHEM),
        text_row(4291789, 230.0, ANTHEM, synced=False),
    ]
    assert ids(rank_matches(rows, 230.0)) == [10910419, 5470091, 28700266, 4291789]
    assert ids(rank_matches(rows)) == [10910419, 5470091, 28700266, 4291789]


def test_synced_still_beats_plain_and_length_still_beats_both():
    stripped = text_row(1, 230.0, _as_lrclib_drops_them(ANTHEM))
    assert ids(rank_matches([stripped, text_row(2, 230.0, ANTHEM, synced=False)], 230.0)) == [
        1,
        2,
    ], "no synced copy is intact: the words in time beat the words alone"
    assert ids(rank_matches([stripped, text_row(3, 250.0, ANTHEM)], 230.0)) == [1, 3]


def test_an_exact_match_that_lost_its_letters_gives_way_to_its_intact_copy():
    stripped = {**text_row(5470091, 230.0, _as_lrclib_drops_them(ANTHEM)), "artistName": "Kayah"}
    intact = {**text_row(10910419, 229.93, ANTHEM), "artistName": "Kayah"}
    lrclib = Lrclib(exact=stripped, searches={"Kayah": [stripped, intact]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert found["lrclib_id"] == 10910419
    assert "Złączym się z narodem." in found["synced"]
    assert found["timing"] == "exact"
    assert ids(found["others"]) == [5470091]


def test_an_intact_copy_of_another_length_does_not_replace_one_the_tracks_length():
    stripped = {**text_row(1, 230.0, _as_lrclib_drops_them(ANTHEM)), "artistName": "Kayah"}
    intact = {**text_row(2, 260.0, ANTHEM), "artistName": "Kayah"}
    lrclib = Lrclib(exact=stripped, searches={"Kayah": [stripped, intact]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert found["lrclib_id"] == 1, "its timing is the track's; the other's is not"


@pytest.mark.parametrize(
    "text",
    [
        ANTHEM,
        "[00:01.00]Группа крови на рукаве",
        "[00:01.00]夢ならばどれほどよかったでしょう",
        "[00:01.00]Ngày ấy, anh đã đi xa",
        "[00:01.00]Ich weiß, dass Mädchen über Grüße lächeln",
        "[00:01.00]Coração, não há razão",
        "[00:01.00]Çok güzel şarkı söylüyorsun",
    ],
)
def test_every_script_survives_the_file_byte_for_byte(tmp_path: Path, text):
    job = Job(id="abcdefabc399")
    entry = {**_found(), "synced": text, "plain": ll._LRC_TAGS.sub("", text), "others": []}
    line = text.splitlines()[0].split("]", 1)[1]
    entry["track"] = entry["artist"] = line
    assert write_lyrics(job, tmp_path, entry)
    raw = (tmp_path / "lyrics.json").read_bytes()
    assert line.encode("utf-8") in raw, "UTF-8, never the locale's codepage"
    back = read_lyrics(tmp_path)
    assert back["synced"] == text and back["track"] == entry["track"]


def test_a_stripped_copy_with_an_intact_plain_one_gets_its_letters_back():
    # No synced copy is intact, so the damaged one's timing is kept and its
    # words are mended from the plain copy, word by word.
    stripped = {**text_row(1, 230.0, _as_lrclib_drops_them(ANTHEM)), "artistName": "Kayah"}
    plain = {**text_row(2, 230.0, ANTHEM, synced=False), "artistName": "Kayah"}
    lrclib = Lrclib(exact=stripped, searches={"Kayah": [stripped, plain]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert found["lrclib_id"] == 1
    assert found["synced"] == ANTHEM, "every letter back, every stamp where it was"
    assert found["plain"] == ll._LRC_TAGS.sub("", ANTHEM)
    assert found["repaired"] == "lrclib"
    assert found["timing"] == "exact"
    assert ids(found["others"]) == [2]


def test_an_intact_copy_of_another_length_mends_the_one_the_tracks_length():
    stripped = {**text_row(1, 230.0, _as_lrclib_drops_them(ANTHEM)), "artistName": "Kayah"}
    intact = {**text_row(2, 260.0, ANTHEM), "artistName": "Kayah"}
    lrclib = Lrclib(exact=stripped, searches={"Kayah": [stripped, intact]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert (found["lrclib_id"], found["repaired"]) == (1, "lrclib")
    assert found["synced"] == ANTHEM, "its own stamps, the other's letters"


def test_a_copy_nothing_intact_stands_beside_is_left_as_it_is():
    stripped = {**text_row(1, 230.0, _as_lrclib_drops_them(ANTHEM)), "artistName": "Kayah"}
    lrclib = Lrclib(exact=stripped, searches={"Kayah": [stripped]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert found["synced"] == _as_lrclib_drops_them(ANTHEM)
    assert "repaired" not in found


def test_an_intact_copy_is_never_marked_repaired():
    intact = {**text_row(1, 230.0, ANTHEM), "artistName": "Kayah"}
    lrclib = Lrclib(exact=intact, searches={"Kayah": [intact]})
    found = kept(query(artist="Kayah", duration=230.0), fetch_json=lrclib)
    assert found["synced"] == ANTHEM and "repaired" not in found
    assert set(found) == KEYS


@pytest.mark.parametrize(("repaired", "kept_as"), [("lrclib", "lrclib"), ("whisper", "whisper")])
def test_where_the_letters_came_from_survives_the_file(tmp_path: Path, repaired, kept_as):
    job = Job(id="abcdefabc398")
    assert write_lyrics(job, tmp_path, {**_found(), "repaired": repaired})
    assert read_lyrics(tmp_path)["repaired"] == kept_as


@pytest.mark.parametrize("repaired", ["yes", True, 1, None, ""])
def test_a_repair_mark_that_is_not_one_is_dropped_not_fatal(repaired):
    entry = clean_lyrics({**_found(), "repaired": repaired})
    assert entry is not None and "repaired" not in entry


# ── LRCLIB busy ──


class _Busy:
    """LRCLIB answering ``code`` for the first ``times`` requests, then the
    artist's version of the song."""

    def __init__(self, times, code=503):
        self.times = times
        self.code = code
        self.calls = 0

    def __call__(self, endpoint, params):
        self.calls += 1
        if self.calls <= self.times:
            raise urllib.error.HTTPError("u", self.code, "busy", {}, None)
        return row(1, 572) if endpoint == "get" else [row(1, 572)]


def test_a_busy_moment_is_asked_again_and_the_lyrics_kept(monkeypatch):
    waits = []
    monkeypatch.setattr(ll.time, "sleep", waits.append)
    lrclib = _Busy(2)
    assert kept(query(), fetch_json=lrclib)["lrclib_id"] == 1
    assert waits == [1.0, 2.0], "twice, a second and then two"


def test_a_server_that_stays_busy_is_given_up_on(monkeypatch):
    monkeypatch.setattr(ll.time, "sleep", lambda s: None)
    lrclib = _Busy(100, code=429)
    with pytest.raises(urllib.error.HTTPError):
        lookup_lyrics(query(), fetch_json=lrclib)
    # The exact match, three times, gives way to the search, three times,
    # whose failure is LRCLIB's: raised, so the lookup is tried again later.
    assert lrclib.calls == 3 * 2


def test_other_failures_are_not_asked_again(monkeypatch):
    monkeypatch.setattr(ll.time, "sleep", lambda s: pytest.fail("no wait"))
    lrclib = _Busy(1, code=500)
    found = kept(query(), fetch_json=lrclib)
    assert found["lrclib_id"] == 1, "the exact match failed; the search found it"
    assert lrclib.calls == 2
