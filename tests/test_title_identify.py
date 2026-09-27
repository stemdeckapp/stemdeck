"""A track named only by its title: MusicBrainz, then LRCLIB, then the chain.

YouTube uploads of cast recordings and film songs carry no music metadata
(yt-dlp's artist, track and album are all None) and a channel that names no
artist, so their title is all there is. The search answers below are trimmed
from what MusicBrainz and LRCLIB really answered for three such uploads:
"Dancing Through Life" from Wicked (WickedVEVO, 458 s), "This Is Me" from The
Greatest Showman (Atlantic Records, 235 s) and "The Phantom of the Opera" (a
fan's upload, 303 s).

No network: musicbrainz._fetch_json, lyrics_lookup._fetch_json and
artist_lookup._fetch_json are stubbed; conftest keeps them offline otherwise.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.pipeline.artist_lookup as al
import app.pipeline.identify as ident
import app.pipeline.lyrics_lookup as ll
import app.pipeline.musicbrainz as mb
import app.pipeline.work_lookup as wl
from app.core.models import Job, clean_identity
from app.core.registry import _jobs
from app.pipeline.audio_tags import tags_from_ytdlp
from app.pipeline.runner import run_pipeline
from app.pipeline.title_parse import TitleReading

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_MB_FETCH = mb._fetch_json

WICKED_TITLE = 'Dancing Through Life (From "Wicked" Original Broadway Cast Recording/2003)'
SHOWMAN_TITLE = "The Greatest Showman Cast - This Is Me (Official Audio)"
PHANTOM_TITLE = "The Phantom of the Opera   Michael Crawford, Sarah Brightman"

WICKED_REC = "cff8fedd-a6fc-4575-a282-1b8e7504dafd"
WICKED_ATMOS = "11111111-1111-4111-8111-111111111111"
WICKED_FILM_REC = "22222222-2222-4222-8222-222222222222"
WICKED_RG = "76729419-70a3-30d0-b316-515bc24c61e2"
WICKED_FILM_RG = "33333333-3333-4333-8333-333333333333"
BUTZ = "2ed68ca3-ba76-4760-9b18-e7fa078de1a2"
KEALA_REC = "2b33e534-36c5-4c15-945d-907076e4c691"
SHOWMAN_RG = "c0639f1e-5be4-4442-9203-58b33324e784"
PHANTOM_REC = "e5bdc28a-f31a-4607-b913-d67a19574fc8"
PHANTOM_RG = "a5fd6ff9-0761-3427-befb-22b75a1c222b"
OTHER = "44444444-4444-4444-8444-444444444444"
WICKED = {"id": "Q616439", "kind": "musical", "name": "Wicked", "englishName": "Wicked"}


def _release(title, primary="Album", secondary=(), *, rg=OTHER, date="", status="Official"):
    return {
        "title": title,
        "status": status,
        "date": date,
        "release-group": {
            "id": rg,
            "title": title,
            "primary-type": primary,
            "secondary-types": list(secondary),
        },
    }


def _hit(rec, title, credit, length, releases, *, disambiguation="", score=100):
    names = [c.strip() for c in credit.split("|")]
    return {
        "id": rec,
        "score": score,
        "title": title,
        "length": length,
        "disambiguation": disambiguation,
        "artist-credit": [
            {"name": n, "joinphrase": "" if i == len(names) - 1 else " & ", "artist": {"id": OTHER}}
            for i, n in enumerate(names)
        ],
        "releases": releases,
    }


SOUNDTRACK = ("Soundtrack",)
WICKED_SEARCH = {
    "recordings": [
        _hit(
            WICKED_REC,
            "Dancing Through Life",
            "Norbert Leo Butz, Kristin Chenoweth, Christopher Fitzgerald, Michelle Federer,"
            " Idina Menzel and Students",
            457653,
            [
                _release("Wicked: Original Broadway Cast Recording", secondary=SOUNDTRACK),
                _release("Wicked", secondary=SOUNDTRACK, rg=WICKED_RG, date="2003-12-16"),
                _release("Wicked: A New Musical", secondary=SOUNDTRACK, date="2005"),
            ],
            disambiguation="2003 original Broadway cast",
        ),
        _hit(
            WICKED_ATMOS,
            "Dancing Through Life",
            "Norbert Leo Butz | Kristin Chenoweth",
            457000,
            [_release("Wicked", secondary=SOUNDTRACK, date="2021")],
            disambiguation="Dolby Atmos mix",
        ),
        _hit(
            WICKED_FILM_REC,
            "Dancing Through Life",
            "Jonathan Bailey | Ariana Grande",
            587000,
            [_release("Wicked: The Soundtrack", secondary=SOUNDTRACK, rg=WICKED_FILM_RG)],
        ),
    ]
}
WICKED_SEARCH["recordings"][0]["artist-credit"][0]["artist"]["id"] = BUTZ

SHOWMAN_SEARCH = {
    "recordings": [
        _hit(OTHER, "This Is Me", "Texas Lightning", 231000, [_release("Meanwhile")]),
        _hit(
            KEALA_REC,
            "This Is Me",
            "Keala Settle | The Greatest Showman Ensemble",
            234706,
            [
                _release("The Best Songs from Movies", secondary=("Compilation",), date="2019"),
                _release("Now That's What I Call Music! 102", secondary=("Compilation",)),
                _release(
                    "The Greatest Showman: Original Motion Picture Soundtrack",
                    secondary=SOUNDTRACK,
                    rg=SHOWMAN_RG,
                    date="2017-12-08",
                ),
            ]
            * 3,
        ),
        # The same singer credited with a cast, only ever on compilations.
        _hit(
            "55555555-5555-4555-8555-555555555555",
            "This Is Me",
            "Keala Settle | The Greatest Showman Cast",
            234320,
            [_release("Goldeneye: Soundtrack Classics", secondary=("Compilation",))],
        ),
        # A cover on an album named for the show.
        _hit(
            "66666666-6666-4666-8666-666666666666",
            "This Is Me",
            "Kesha",
            234947,
            [_release("The Greatest Showman: Reimagined", secondary=SOUNDTRACK)] * 11,
        ),
        _hit(
            "77777777-7777-4777-8777-777777777777",
            "This Is Me",
            "Andy Brown",
            234800,
            [_release("The Greatest Showman")],
        ),
    ]
}

PHANTOM_SEARCH = {
    "recordings": [
        _hit(
            OTHER,
            "The Phantom of the Opera – The Phantom of the Opera",
            "Michael Crawford | Sarah Brightman",
            305093,
            [_release("Musical Top 40")],
        ),
        _hit(
            "88888888-8888-4888-8888-888888888888",
            "The Phantom of the Opera",
            "Andrew Lloyd Webber",
            302533,
            [_release("The Phantom Of The Opera", secondary=SOUNDTRACK)],
        ),
        _hit(
            PHANTOM_REC,
            "The Phantom of the Opera",
            "Michael Crawford | Sarah Brightman",
            302533,
            [
                _release("Now & Forever", secondary=("Compilation",), date="2001"),
                _release(
                    "The Phantom of the Opera", secondary=SOUNDTRACK, rg=PHANTOM_RG, date="1987"
                ),
                _release("The Phantom of the Opera (Original London Cast)", secondary=SOUNDTRACK),
            ],
        ),
    ]
}


def _lrclib_row(lrclib_id, track, artist, album, duration, synced="[00:01.00]Dancing through life"):
    return {
        "id": lrclib_id,
        "trackName": track,
        "artistName": artist,
        "albumName": album,
        "duration": duration,
        "syncedLyrics": synced,
        "plainLyrics": "Dancing through life",
        "instrumental": False,
    }


WICKED_LRCLIB = [
    _lrclib_row(35211528, "Dancing Through Life", "Jonathan Bailey, Ariana Grande", "Wicked", 587),
    _lrclib_row(
        36586366,
        "Dancing Through Life",
        "Norbert Leo Butz, Kristin Chenoweth, Christopher Fitzgerald, Michelle Federer,"
        " Idina Menzel and Students",
        "Wicked",
        458,
    ),
    _lrclib_row(23341292, "Dancing Through Life", "Annapantsu", "Dancing Through Life", 457),
    _lrclib_row(1, "Dancing_Through_Life_From_Wicked", "wicked", "wicked", 457),
]


class MusicBrainz:
    """Stands in for musicbrainz._fetch_json: a search answers ``searches[i]``
    in turn (the last one again after that); records what was asked."""

    def __init__(self, *searches, fail=None, groups=None):
        self.searches = list(searches) or [{"recordings": []}]
        self.fail = fail
        self.groups = groups or {}
        self.asked: list[tuple[str, dict]] = []

    def __call__(self, path, params, **_kwargs):
        self.asked.append((path, dict(params)))
        if self.fail is not None:
            raise self.fail
        if path == "recording":
            index = min(len(self.asked_searches) - 1, len(self.searches) - 1)
            return self.searches[index]
        if path.startswith("release-group/") and path.removeprefix("release-group/") in self.groups:
            return self.groups[path.removeprefix("release-group/")]
        raise OSError(f"no stub for {path}")

    @property
    def asked_searches(self):
        return [p for path, p in self.asked if path == "recording"]


class Lrclib:
    def __init__(self, rows=None, *, fail=None, exact=None):
        self.rows = rows or []
        self.fail = fail
        self.exact = exact
        self.asked: list[tuple[str, dict]] = []

    def __call__(self, endpoint, params):
        self.asked.append((endpoint, dict(params)))
        if self.fail is not None:
            raise self.fail
        if endpoint == "get":
            return self.exact
        return self.rows


def _reading(title):
    return ident.title_parse.readings_for(None, title)[0]


# ── MusicBrainz: which recording the title is ──


def test_the_broadway_cast_recording_by_its_length():
    identity = mb.best_title_match(WICKED_SEARCH, _reading(WICKED_TITLE), 458.0)
    assert identity["source"] == "musicbrainz"
    assert identity["recording_mbid"] == WICKED_REC
    assert identity["score"] == 1.0
    assert identity["artist_mbids"][0] == BUTZ
    # The album named for the show, and its soundtrack type, for the work.
    assert identity["album"] == "Wicked"
    assert identity["release_group_mbid"] == WICKED_RG
    assert identity["secondary_types"] == ["Soundtrack"]


def test_the_film_version_by_its_length():
    identity = mb.best_title_match(WICKED_SEARCH, _reading(WICKED_TITLE), 587.0)
    assert identity["recording_mbid"] == WICKED_FILM_REC
    assert identity["album"] == "Wicked: The Soundtrack"


def test_the_cast_singer_on_the_soundtrack_not_a_cover_or_a_compilation():
    identity = mb.best_title_match(SHOWMAN_SEARCH, _reading(SHOWMAN_TITLE), 235.0)
    assert identity["recording_mbid"] == KEALA_REC
    assert identity["artist"] == "Keala Settle & The Greatest Showman Ensemble"
    assert identity["album"] == "The Greatest Showman: Original Motion Picture Soundtrack"
    assert identity["release_group_mbid"] == SHOWMAN_RG


def test_the_performers_the_title_lists():
    identity = mb.best_title_match(PHANTOM_SEARCH, _reading(PHANTOM_TITLE), 303.0)
    assert identity["recording_mbid"] == PHANTOM_REC
    assert identity["artist"] == "Michael Crawford & Sarah Brightman"
    assert identity["album"] == "The Phantom of the Opera"
    assert identity["release_group_mbid"] == PHANTOM_RG


@pytest.mark.parametrize(
    ("answer", "reading", "duration"),
    [
        # Nobody credited is anyone the title names.
        (SHOWMAN_SEARCH, TitleReading("This Is Me", artist="Nobody Known"), 235.0),
        # Named, but no version that length.
        (WICKED_SEARCH, TitleReading("Dancing Through Life", work="Wicked"), 500.0),
        # A song alone can be anybody's.
        (SHOWMAN_SEARCH, TitleReading("This Is Me"), 235.0),
        # Nothing to check the length by.
        (WICKED_SEARCH, TitleReading("Dancing Through Life", work="Wicked"), None),
        # Only half the performers is not enough when the rest is someone else.
        (PHANTOM_SEARCH, TitleReading("Defying Gravity", artist="Michael Crawford"), 303.0),
        ({"recordings": "junk"}, TitleReading("This Is Me", work="Wicked"), 235.0),
        (None, TitleReading("This Is Me", work="Wicked"), 235.0),
    ],
)
def test_nothing_short_of_confident_is_kept(answer, reading, duration):
    assert mb.best_title_match(answer, reading, duration) is None


def test_a_video_is_never_the_recording():
    answer = json.loads(json.dumps(WICKED_SEARCH))
    for hit in answer["recordings"]:
        hit["video"] = True
    assert mb.best_title_match(answer, _reading(WICKED_TITLE), 458.0) is None


def test_the_search_asks_for_the_song_its_length_and_the_other_words(monkeypatch):
    musicbrainz = MusicBrainz(SHOWMAN_SEARCH)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    assert mb.search_by_title(_reading(SHOWMAN_TITLE), 235.0)["recording_mbid"] == KEALA_REC
    [(path, params)] = musicbrainz.asked
    assert path == "recording"
    assert params["query"] == (
        'recording:"This Is Me" AND dur:[231000 TO 239000] AND status:official'
        ' AND (artist:("greatest" "showman") OR release:("greatest" "showman"))'
    )


def test_a_title_cannot_change_the_query(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    mb.search_by_title(TitleReading('Say "Hi" \\ OR', artist='OR q:"w" *'), 200.0)
    query = musicbrainz.asked[0][1]["query"]
    assert query.startswith('recording:"Say \\"Hi\\" \\\\ OR" AND dur:')
    # Each word its own quoted phrase, punctuation gone.
    assert query.endswith('(artist:("or" "q" "w") OR release:("or" "q" "w"))')


def test_no_search_without_a_length_or_other_words(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", lambda *a: pytest.fail("asked MusicBrainz"))
    assert mb.search_by_title(TitleReading("This Is Me"), 235.0) is None
    assert mb.search_by_title(TitleReading("This Is Me", artist="Keala"), None) is None
    assert (
        mb.search_by_title(
            TitleReading("This Is Me", artist="Keala"), 235.0, cancelled=lambda: True
        )
        is None
    )


# ── a live recording ranks below the studio and cast ones ──


def _popular(rec, releases, **over):
    return _hit(rec, "Popular", "Kristin Chenoweth", 224000, releases, **over)


POPULAR_SEARCH = {
    "recordings": [
        # Kristin Chenoweth's live "Coming Home", on more releases.
        _popular(
            OTHER,
            [_release("Coming Home", secondary=("Live",), date="2014")] * 6,
        ),
        _popular(
            WICKED_REC,
            [_release("Wicked", secondary=SOUNDTRACK, rg=WICKED_RG, date="2003-12-16")] * 3,
        ),
    ]
}


def test_a_live_recording_ranks_below_the_cast_album(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(POPULAR_SEARCH))
    identity = mb.search_recording("Kristin Chenoweth", "Popular", 224.0)
    assert identity["recording_mbid"] == WICKED_REC
    assert identity["album"] == "Wicked"
    reading = TitleReading("Popular", artist="Kristin Chenoweth")
    assert mb.best_title_match(POPULAR_SEARCH, reading, 224.0)["recording_mbid"] == WICKED_REC


def test_a_live_disambiguation_ranks_below_too():
    live = _popular(OTHER, [_release("Some Album")] * 9, disambiguation="Live at the Palladium")
    studio = _popular(WICKED_REC, [_release("Wicked", secondary=SOUNDTRACK)])
    assert mb.is_live(live) and not mb.is_live(studio)
    reading = TitleReading("Popular", artist="Kristin Chenoweth")
    answer = {"recordings": [live, studio]}
    assert mb.best_title_match(answer, reading, 224.0)["recording_mbid"] == WICKED_REC


def test_the_album_is_the_soundtrack_before_a_compilation_or_a_live_album():
    releases = [
        _release("Live Somewhere", secondary=("Live",), date="1990"),
        _release("Hits", secondary=("Compilation",), date="1995"),
        _release("Wicked", secondary=SOUNDTRACK, date="2003"),
    ]
    assert mb.best_release_group(releases)["title"] == "Wicked"
    assert mb.best_release_group(releases[:2])["title"] == "Hits"


# ── MusicBrainz shedding load ──


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(*codes, headers=None):
    """urlopen answering each of ``codes`` in turn: an HTTP error, or 200."""
    answers = list(codes)

    def urlopen(request, timeout, context):
        code = answers.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(
                request.full_url, code, "busy", headers or {}, io.BytesIO(b"")
            )
        return _Response(b'{"ok": true}')

    return urlopen


def test_a_busy_answer_is_asked_again_after_a_growing_wait(monkeypatch):
    """Four of 45 songs in the language benchmark lost their identity to a
    single 503 when one retry was all there was."""
    turns, slept = [], []
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda: turns.append(1))
    monkeypatch.setattr(mb, "_sleep", slept.append)
    monkeypatch.setattr(mb.urllib.request, "urlopen", _urlopen(503, 429, 200))
    assert _REAL_MB_FETCH("recording", {"query": "x"}) == {"ok": True}
    assert len(turns) == 3, "each attempt takes a turn of its own"
    assert sum(slept) == pytest.approx(1.0 + 2.0)


def test_retry_after_is_kept_to_within_a_bound(monkeypatch):
    slept = []
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda: None)
    monkeypatch.setattr(mb, "_sleep", slept.append)
    monkeypatch.setattr(
        mb.urllib.request, "urlopen", _urlopen(503, 200, headers={"Retry-After": "600"})
    )
    assert _REAL_MB_FETCH("recording", {"query": "x"}) == {"ok": True}
    assert sum(slept) == pytest.approx(mb.MUSICBRAINZ_RETRY_MAX_WAIT_SEC)


@pytest.mark.parametrize("codes", [(503,) * (mb.MUSICBRAINZ_RETRIES + 1), (400,), (404,)])
def test_a_refusal_past_the_retries_or_any_other_error_raises(monkeypatch, codes):
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda: None)
    monkeypatch.setattr(mb, "_sleep", lambda seconds: None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", _urlopen(*codes))
    with pytest.raises(urllib.error.HTTPError):
        _REAL_MB_FETCH("recording", {"query": "x"})


def test_a_cancel_stops_the_wait(monkeypatch):
    turns = []
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda: turns.append(1))
    monkeypatch.setattr(mb, "_sleep", lambda seconds: None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", _urlopen(503, 200))
    with pytest.raises(mb.ratelimit.RateLimited):
        _REAL_MB_FETCH("recording", {"query": "x"}, cancelled=lambda: True)
    assert len(turns) == 1


# ── LRCLIB as the second opinion ──


def test_lrclib_names_the_track_when_musicbrainz_cannot():
    lrclib = Lrclib(WICKED_LRCLIB)
    identity = ll.identify_on_lrclib(_reading(WICKED_TITLE), 458.0, fetch_json=lrclib)
    assert identity == clean_identity(
        {
            "source": "lrclib",
            "score": 1.0,
            "title": "Dancing Through Life",
            "artist": "Norbert Leo Butz, Kristin Chenoweth, Christopher Fitzgerald,"
            " Michelle Federer, Idina Menzel and Students",
            "album": "Wicked",
            "duration": 458.0,
        }
    )
    assert lrclib.asked == [("search", {"q": "Dancing Through Life Wicked"})]


@pytest.mark.parametrize(
    ("rows", "duration"),
    [
        (WICKED_LRCLIB, 500.0),  # no version that length
        (WICKED_LRCLIB[2:3], 457.0),  # that length, but nobody the title names
        (WICKED_LRCLIB[3:], 457.0),  # a name that is not the song's
        ([], 458.0),
        ("junk", 458.0),
    ],
)
def test_lrclib_is_only_kept_when_confident(rows, duration):
    reading = _reading(WICKED_TITLE)
    assert ll.identify_on_lrclib(reading, duration, fetch_json=Lrclib(rows)) is None


def test_lrclib_is_not_asked_without_something_to_check():
    lrclib = Lrclib(WICKED_LRCLIB)
    assert (
        ll.identify_on_lrclib(TitleReading("Dancing Through Life"), 458.0, fetch_json=lrclib)
        is None
    )
    assert ll.identify_on_lrclib(_reading(WICKED_TITLE), None, fetch_json=lrclib) is None
    assert lrclib.asked == []


# ── the whole answer ──


def test_a_title_alone_is_identified(monkeypatch):
    musicbrainz = MusicBrainz(WICKED_SEARCH)
    lrclib = Lrclib(WICKED_LRCLIB)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    identity = ident.identify(tags=None, title=WICKED_TITLE, duration=458.0, audio=[], api_key=None)
    assert identity["recording_mbid"] == WICKED_REC
    assert len(musicbrainz.asked) == 1
    assert lrclib.asked == [], "MusicBrainz was sure"


def test_each_reading_is_tried_until_one_is_confident(monkeypatch):
    # "Michael Crawford, Sarah Brightman" as the song finds nothing.
    musicbrainz = MusicBrainz(PHANTOM_SEARCH)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    title = "Michael Crawford, Sarah Brightman   The Phantom of the Opera"
    identity = ident.identify(tags=None, title=title, duration=303.0, audio=[], api_key=None)
    assert identity["recording_mbid"] == PHANTOM_REC
    songs = [p["query"].split(" AND ")[0] for p in musicbrainz.asked_searches]
    assert songs == [
        'recording:"Michael Crawford, Sarah Brightman"',
        'recording:"The Phantom of the Opera"',
    ]


@pytest.mark.parametrize("musicbrainz", [MusicBrainz(), MusicBrainz(fail=OSError("down"))])
def test_lrclib_decides_when_musicbrainz_is_unsure_or_down(monkeypatch, musicbrainz):
    lrclib = Lrclib(WICKED_LRCLIB)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    identity = ident.identify(tags=None, title=WICKED_TITLE, duration=458.0, audio=[], api_key=None)
    assert identity["source"] == "lrclib"
    assert identity["album"] == "Wicked"
    assert len(musicbrainz.asked) == 1, "one reading, and a failure is not asked again"


def test_nobody_confident_is_no_identity(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz())
    monkeypatch.setattr(ll, "_fetch_json", Lrclib())
    assert (
        ident.identify(tags=None, title=SHOWMAN_TITLE, duration=235.0, audio=[], api_key=None)
        is None
    )


def test_tags_naming_the_song_keep_lrclib_out_of_it(monkeypatch):
    """The title is a second opinion on MusicBrainz only: tags naming artist
    and song already give the lyrics lookup its names."""
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz())
    monkeypatch.setattr(ll, "_fetch_json", lambda *a: pytest.fail("asked LRCLIB"))
    identity = ident.identify(
        tags={"artist": "Somebody", "title": "Some Song"},
        title="Somebody - Some Song (Official Video)",
        duration=200.0,
        audio=[],
        api_key=None,
    )
    assert identity["source"] == "tags"


def test_tags_musicbrainz_does_not_know_get_a_second_opinion(monkeypatch):
    """yt-dlp's artist says "The Greatest Showman Cast", which no recording
    credits: the strict search finds nothing, the title's reading does."""
    musicbrainz = MusicBrainz({"recordings": []}, SHOWMAN_SEARCH)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags={"artist": "The Greatest Showman Cast", "title": "This Is Me"},
        title=SHOWMAN_TITLE,
        duration=235.0,
        audio=[],
        api_key=None,
    )
    assert identity["recording_mbid"] == KEALA_REC
    assert len(musicbrainz.asked_searches) == 2


def test_a_title_naming_more_than_a_song_is_enough_to_start():
    job = Job(id="abcdef00a001", title=WICKED_TITLE, duration_sec=458.0)
    assert ident.can_identify(None, None, [], job.title)
    assert not ident.can_identify(None, None, [], "Bohemian Rhapsody")
    lookup = ident.IdentifyLookup.start(job, [Path("source.wav")])
    assert lookup is not None
    lookup.finish(job, 5)


# ── the work, through the title ──


def test_the_title_names_the_work_when_nothing_else_does():
    identity = clean_identity(
        {"source": "lrclib", "title": "Dancing Through Life", "artist": "x", "album": "Wicked"}
    )
    # "Wicked" alone carries no qualifier, so only the title says it is a show.
    assert wl.work_query(identity, None) is None
    assert wl.work_query(identity, None, WICKED_TITLE) == ("Wicked", "musical", False)
    assert wl.work_query(None, None, SHOWMAN_TITLE) == ("The Greatest Showman", None, False)


def test_a_studio_album_ignores_a_work_in_the_title():
    identity = clean_identity(
        {
            "source": "musicbrainz",
            "title": "Lithium",
            "artist": "Nirvana",
            "album": "Nevermind",
            "release_group_mbid": OTHER,
            "secondary_types": [],
        }
    )
    assert wl.work_query(identity, None, 'Lithium (From "Some Film")') is None


def test_the_lyrics_try_the_show_the_title_names_as_the_artist():
    job = Job(id="abcdef00a002", title='Popular (From "Wicked")', duration_sec=224.0)
    identity = clean_identity(
        {
            "source": "musicbrainz",
            "title": "Popular",
            "artist": "Kristin Chenoweth",
            "album": "Some Compilation",
        }
    )
    query = ll.build_query(job, identity=identity)
    assert query.album_artists == ("Wicked", "Some Compilation")


# ── the whole chain, with no clicks ──


def _wikidata(asked):
    musical = {
        "id": "Q616439",
        "labels": {"en": {"value": "Wicked"}},
        "claims": {
            "P31": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": "Q58483083"}}}}]
        },
    }

    def fetch(params):
        asked.append(dict(params))
        if params["action"] == "wbgetentities":
            return {"entities": {"Q616439": musical}}
        return {"search": []}

    return fetch


WICKED_GROUP = {
    "id": WICKED_RG,
    "first-release-date": "2003-12-16",
    "relations": [{"url": {"resource": "https://www.wikidata.org/wiki/Q616439"}}],
}


@pytest.fixture
def services(monkeypatch):
    musicbrainz = MusicBrainz(WICKED_SEARCH, groups={WICKED_RG: WICKED_GROUP})
    lrclib = Lrclib(WICKED_LRCLIB)
    wikidata_asked: list[dict] = []
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(ll, "_fetch_json", lrclib)
    monkeypatch.setattr(al, "_fetch_json", _wikidata(wikidata_asked))
    return musicbrainz, lrclib, wikidata_asked


def _download_untagged(job, url, job_dir):
    # What yt-dlp really gave: no music fields, a channel that names nobody.
    job.title = WICKED_TITLE
    job.duration_sec = 458.0
    job.audio_tags = tags_from_ytdlp(
        {"title": WICKED_TITLE, "channel": "WickedVEVO", "artist": None, "track": None}
    )
    return job_dir / "source.wav"


def _separation(job, source, job_dir):
    (job_dir / "stems").mkdir(parents=True, exist_ok=True)
    time.sleep(0.3)


async def test_an_untagged_upload_gets_identity_work_and_lyrics(services, tmp_path):
    musicbrainz, lrclib, _ = services
    job = Job(id="abcdef00a010")
    with (
        patch("app.pipeline.runner.download", side_effect=_download_untagged),
        patch("app.pipeline.runner._run_common", side_effect=_separation),
    ):
        await run_pipeline(job, "https://www.youtube.com/watch?v=CnIjnMDY5-c", tmp_path)
    assert job.status == "done"
    assert job.audio_tags is None
    assert job.identity["recording_mbid"] == WICKED_REC
    assert job.work == WICKED
    lyrics = json.loads((tmp_path / job.id / "lyrics.json").read_text(encoding="utf-8"))
    assert lyrics["lrclib_id"] == 36586366
    assert lyrics["timing"] == "exact"
    assert job.has_lyrics
    meta = json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["identity"] == job.identity
    assert meta["work"] == WICKED


# ── the tag backfill ──


@pytest.fixture
def client():
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()


def test_an_older_untagged_job_is_identified_by_its_title(client, services):
    job = Job(
        id="abcdef00a020",
        status="done",
        title=WICKED_TITLE,
        duration_sec=458.0,
        source_url="https://www.youtube.com/watch?v=CnIjnMDY5-c",
    )
    _jobs[job.id] = job
    (jobs_mod.JOBS_DIR / job.id / "stems").mkdir(parents=True)
    with patch("app.api.jobs.fetch_audio_tags", return_value=None):
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    body = r.json()
    assert body["audio_tags"] is None
    assert body["identity"]["recording_mbid"] == WICKED_REC
    assert body["work"] == WICKED
    assert body["has_lyrics"] is True
    assert job.identity == body["identity"]
    assert job.work == WICKED
