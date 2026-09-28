"""Identification branches the other identify suites leave unpinned
(app/pipeline/musicbrainz.py, identify.py).

MusicBrainz shedding load: what Retry-After says when it is not a number, or
is zero or negative, and a cancel that arrives in the middle of a wait. A
song's other titles from its MusicBrainz work (work_titles, title_aliases).
Which recordings are another take on a song (is_another_take). An artist
found by name (search_artists), and the band a title names only on evidence
it cannot give by chance (band_from_title).

No network: conftest has MusicBrainz, AcoustID and Wikidata offline, and each
test that wants an answer stands in for the one request it needs.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

import app.pipeline.identify as ident
import app.pipeline.musicbrainz as mb

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_MB_FETCH = mb._fetch_json

REC = "44444444-4444-4444-8444-444444444444"
WORK_A = "99999999-9999-4999-8999-999999999991"
WORK_B = "99999999-9999-4999-8999-999999999992"
WORK_C = "99999999-9999-4999-8999-999999999993"
ARTIST_A = "a223958d-5c56-4b2c-a30a-87e357bc121b"
ARTIST_B = "5c5cb762-d95e-47af-a7bf-35171eeab8e6"


@pytest.fixture(autouse=True)
def _fresh_artist_cache():
    mb._ARTISTS_FOUND.clear()
    yield
    mb._ARTISTS_FOUND.clear()


# ── MusicBrainz shedding load ──


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _busy(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError("https://musicbrainz.org/ws/2/x", code, "busy", headers, None)


def _urlopen(*answers):
    """urlopen answering each of ``answers`` in turn: an HTTPError is raised,
    anything else is the 200 body."""
    queue = list(answers)
    asked = []

    def urlopen(request, timeout, context):
        asked.append(request.full_url)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    urlopen.asked = asked
    return urlopen


@pytest.mark.parametrize(
    ("retry_after", "attempt", "want"),
    [
        # An HTTP date is allowed by the spec but not read: the backoff instead.
        ("Wed, 21 Oct 2026 07:28:00 GMT", 0, 1.0),
        ("Wed, 21 Oct 2026 07:28:00 GMT", 2, 4.0),
        ("0", 1, 0.0),
        ("-5", 0, 0.0),
        ("2.5", 0, 2.5),
        (None, 1, 2.0),
        # The backoff doubles, and is capped like a Retry-After is.
        (None, 5, mb.MUSICBRAINZ_RETRY_MAX_WAIT_SEC),
        ("", 0, 1.0),
    ],
)
def test_the_wait_before_asking_again(retry_after, attempt, want):
    assert mb._retry_wait(_busy(503, retry_after), attempt) == pytest.approx(want)


def test_a_retry_after_of_zero_asks_again_at_once(monkeypatch):
    slept = []
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda *a, **k: None)
    monkeypatch.setattr(mb, "_sleep", slept.append)
    urlopen = _urlopen(_busy(429, "0"), b'{"ok": true}')
    monkeypatch.setattr(mb.urllib.request, "urlopen", urlopen)
    assert _REAL_MB_FETCH("recording", {"query": "x"}) == {"ok": True}
    assert slept == [] and len(urlopen.asked) == 2


def test_a_cancel_during_the_wait_stops_it_between_steps(monkeypatch):
    """The wait is slept in short steps: a job cancelled a quarter second into
    an eight-second wait stops there, and asks nothing more."""
    slept = []
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda *a, **k: None)
    monkeypatch.setattr(mb, "_sleep", slept.append)
    urlopen = _urlopen(_busy(503, "8"), b'{"ok": true}')
    monkeypatch.setattr(mb.urllib.request, "urlopen", urlopen)
    with pytest.raises(mb.ratelimit.RateLimited):
        _REAL_MB_FETCH("recording", {"query": "x"}, cancelled=lambda: bool(slept))
    assert slept == [0.25]
    assert len(urlopen.asked) == 1


def test_an_answer_too_large_is_refused(monkeypatch):
    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", lambda *a, **k: None)
    monkeypatch.setattr(mb, "IDENTIFY_MAX_BYTES", 16)
    monkeypatch.setattr(mb.urllib.request, "urlopen", _urlopen(b'{"a": "' + b"x" * 64 + b'"}'))
    with pytest.raises(ValueError):
        _REAL_MB_FETCH("recording", {"query": "x"})


# ── a song's other titles, from its work ──


class Works:
    """Stands in for _fetch_json: a recording with ``relations``, and each
    work with the aliases given for it."""

    def __init__(self, relations, aliases=None):
        self.relations = relations
        self.aliases = aliases or {}
        self.asked: list[str] = []

    def __call__(self, path, params, **_kwargs):
        self.asked.append(path)
        if path.startswith("recording/"):
            assert params == {"inc": "work-rels"}
            return {"relations": self.relations}
        if path.startswith("work/"):
            names = self.aliases.get(path.removeprefix("work/"), [])
            return {"aliases": [{"name": n} for n in names]}
        raise AssertionError(f"unexpected request {path}")


def _performance(work_id, title):
    return {"type": "performance", "work": {"id": work_id, "title": title}}


def test_a_works_title_and_aliases_are_the_songs_other_titles(monkeypatch):
    works = Works([_performance(WORK_A, "좋은 날")], {WORK_A: ["Good Day", "Joeun Nal"]})
    monkeypatch.setattr(mb, "_fetch_json", works)
    assert mb.work_titles(REC) == ["좋은 날", "Good Day", "Joeun Nal"]


def test_only_a_performance_of_a_work_counts(monkeypatch):
    other = {"type": "based on", "work": {"id": WORK_B, "title": "Another Song"}}
    works = Works([other, _performance(WORK_A, "좋은 날")], {WORK_A: ["Good Day"]})
    monkeypatch.setattr(mb, "_fetch_json", works)
    assert mb.work_titles(REC) == ["좋은 날", "Good Day"]
    assert f"work/{WORK_B}" not in works.asked


def test_a_medley_asks_about_two_works_at_most(monkeypatch):
    works = Works(
        [_performance(w, t) for w, t in ((WORK_A, "A"), (WORK_B, "B"), (WORK_C, "C"))],
        {WORK_A: ["A2"], WORK_B: ["B2"], WORK_C: ["C2"]},
    )
    monkeypatch.setattr(mb, "_fetch_json", works)
    titles = mb.work_titles(REC)
    assert titles == ["A", "A2", "B", "B2"]
    assert f"work/{WORK_C}" not in works.asked


def test_work_titles_come_from_the_cache_the_second_time(monkeypatch):
    works = Works([_performance(WORK_A, "좋은 날")], {WORK_A: ["Good Day"]})
    monkeypatch.setattr(mb, "_fetch_json", works)
    first = mb.work_titles(REC)
    asked = len(works.asked)
    assert mb.work_titles(REC) == first
    assert len(works.asked) == asked == 2


@pytest.mark.parametrize("mbid", ["", "not-an-mbid", "../../etc/passwd", None])
def test_nothing_is_asked_for_what_is_not_a_recording_id(monkeypatch, mbid):
    monkeypatch.setattr(mb, "_fetch_json", lambda *a, **k: pytest.fail("asked"))
    assert mb.work_titles(mbid) == []


def test_a_cancelled_job_asks_for_no_work_titles(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", lambda *a, **k: pytest.fail("asked"))
    assert mb.work_titles(REC, cancelled=lambda: True) == []


def test_a_work_without_an_id_still_gives_its_title(monkeypatch):
    works = Works([{"type": "performance", "work": {"title": "좋은 날"}}])
    monkeypatch.setattr(mb, "_fetch_json", works)
    assert mb.work_titles(REC) == ["좋은 날"]
    assert works.asked == [f"recording/{REC}"]


def test_musicbrainz_failing_leaves_the_uploads_own_titles():
    """title_aliases never raises: MusicBrainz offline (conftest) is only the
    work's titles missing, not the upload's."""
    identity = {"title": "좋은 날", "artist": "IU", "recording_mbid": REC}
    aliases = ident.title_aliases(identity, None, "IU (아이유) _ Good Day (좋은 날) _ MV")
    assert aliases == ["Good Day"]


def test_a_title_alias_that_is_the_title_again_is_not_one(monkeypatch):
    works = Works([_performance(WORK_A, "좋은 날")], {WORK_A: ["좋은 날", " Good Day "]})
    monkeypatch.setattr(mb, "_fetch_json", works)
    identity = {"title": "좋은 날", "artist": "IU", "recording_mbid": REC}
    assert ident.title_aliases(identity, None, None) == ["Good Day"]


# ── another take on the song ──


@pytest.mark.parametrize(
    ("title", "disambiguation", "another"),
    [
        ("晴天 (Instrumental)", "", True),
        ("Lemon (Kenshi Yonezu Cover)", "", True),
        ("Palette [Inst.]", "", True),
        ("Song (Remix)", "", True),
        ("夜に駆ける (Off Vocal)", "", True),
        ("좋은 날 (MR)", "", True),
        ("群青 (カバー)", "", True),
        ("红豆 (伴奏)", "", True),
        ("Song", "karaoke version", True),
        ("Song", "a cappella", True),
        # The song itself, however its title is dressed.
        ("Song", "", False),
        ("Song (Live)", "", False),
        ("Song (feat. Someone)", "", False),
        ("Song (Mr. Brightside)", "", False),
        ("Cover Me", "", False),
        ("Song", "music video", False),
        ("Remixed Feelings", "", False),
    ],
)
def test_another_take_on_the_song(title, disambiguation, another):
    assert mb.is_another_take({"title": title, "disambiguation": disambiguation}) is another


# ── an artist by name ──


def _artist(mbid, name, *, sort_name="", aliases=(), score=100):
    return {
        "id": mbid,
        "name": name,
        "sort-name": sort_name or name,
        "score": score,
        "aliases": [{"name": a} for a in aliases],
    }


class Artists:
    """Stands in for _fetch_json: an artist search answers ``artists``, and
    an artist lookup links each of them to its own Wikidata item (Q1, Q2...),
    so that any artist the code settles on does become a band."""

    def __init__(self, artists):
        self.artists = artists
        self.asked: list[str] = []

    def qid(self, mbid):
        return f"Q{[a['id'] for a in self.artists].index(mbid) + 1}"

    def __call__(self, path, params, **_kwargs):
        if path.startswith("artist/"):
            resource = f"https://www.wikidata.org/wiki/{self.qid(path.removeprefix('artist/'))}"
            return {"relations": [{"type": "wikidata", "url": {"resource": resource}}]}
        assert path == "artist"
        self.asked.append(params["query"])
        return {"artists": self.artists}

    def wikidata(self, params):
        """Wikidata's entity for each item, naming its MusicBrainz artist back."""
        qid = params["ids"]
        mbid = next(a["id"] for a in self.artists if self.qid(a["id"]) == qid)
        entity = {
            "id": qid,
            "labels": {"en": {"value": f"Band {qid}"}},
            "claims": {"P434": [{"mainsnak": {"datavalue": {"value": mbid}}}]},
        }
        return {"entities": {qid: entity}}


def _answering(monkeypatch, artists):
    stand_in = Artists(artists)
    monkeypatch.setattr(mb, "_fetch_json", stand_in)
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", stand_in.wikidata)
    return stand_in


def test_a_sort_name_turned_round_is_a_name(monkeypatch):
    """周杰倫's sort name is "Chou, Jay": the name "Jay Chou" is his even with
    no alias that says so."""
    monkeypatch.setattr(
        mb, "_fetch_json", Artists([_artist(ARTIST_A, "周杰倫", sort_name="Chou, Jay")])
    )
    found = mb.search_artists(["Jay Chou"])
    assert [(a["id"], a["matched"]) for a in found] == [(ARTIST_A, "Jay Chou")]


def test_a_near_miss_is_not_kept(monkeypatch):
    """The search forgives spelling; the answer is held to the name exactly."""
    monkeypatch.setattr(mb, "_fetch_json", Artists([_artist(ARTIST_A, "Jay Chow")]))
    assert mb.search_artists(["Jay Chou"]) == []


def test_an_artist_with_no_valid_id_is_dropped(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", Artists([_artist("../x", "Jay Chou")]))
    assert mb.search_artists(["Jay Chou"]) == []


def test_artists_come_in_the_order_of_the_names_then_by_score(monkeypatch):
    artists = [
        _artist(ARTIST_B, "Teresa Teng", score=100),
        _artist(ARTIST_A, "周杰倫", aliases=("Jay Chou",), score=90),
    ]
    monkeypatch.setattr(mb, "_fetch_json", Artists(artists))
    found = mb.search_artists(["Jay Chou", "Teresa Teng"])
    assert [a["id"] for a in found] == [ARTIST_A, ARTIST_B]


def test_the_same_names_are_searched_once(monkeypatch):
    artists = Artists([_artist(ARTIST_A, "周杰倫", aliases=("Jay Chou",))])
    monkeypatch.setattr(mb, "_fetch_json", artists)
    first = mb.search_artists(["Jay Chou", "周杰倫"])
    # The same names, spelled and ordered as the key sees them, ask nothing.
    assert mb.search_artists(["jay chou", "周杰倫"]) == first
    assert len(artists.asked) == 1


def test_no_name_asks_nothing(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", lambda *a, **k: pytest.fail("asked"))
    assert mb.search_artists(["", "  ", "!!!"]) == []
    assert mb.search_artists(["Jay Chou"], cancelled=lambda: True) == []


# ── the band a title names ──


def test_the_stand_in_does_give_a_band_for_one_artist_in_two_scripts(monkeypatch):
    """The control for the three below: with the same stand-ins, one artist
    named in two scripts is the band, so a None below is the rule saying no."""
    _answering(monkeypatch, [_artist(ARTIST_A, "周杰倫", aliases=("Jay Chou",))])
    assert ident.band_from_title("周杰倫 Jay Chou - 晴天 (Live 2019)")["id"] == "Q1"


def test_the_stand_in_does_give_a_band_for_a_compilation_naming_one_artist(monkeypatch):
    _answering(monkeypatch, [_artist(ARTIST_B, "Teresa Teng")])
    assert ident.band_from_title("Teresa Teng - Greatest Hits")["id"] == "Q1"


def test_two_scripts_naming_two_different_artists_name_no_band(monkeypatch):
    """周杰倫 is one artist and "Jay Chou" another here: neither is named in
    two scripts, so neither is the band."""
    _answering(monkeypatch, [_artist(ARTIST_A, "周杰倫"), _artist(ARTIST_B, "Jay Chou")])
    assert ident.band_from_title("周杰倫 Jay Chou - 晴天 (Live 2019)") is None


def test_two_artists_each_named_in_two_scripts_name_no_band(monkeypatch):
    artists = [
        _artist(ARTIST_A, "周杰倫", aliases=("Jay Chou",)),
        _artist(ARTIST_B, "周杰倫", aliases=("Jay Chou",), score=99),
    ]
    _answering(monkeypatch, artists)
    assert ident.band_from_title("周杰倫 Jay Chou - 晴天 (Live 2019)") is None


def test_a_compilation_with_a_part_naming_nobody_names_no_band(monkeypatch):
    """Every part of a compilation's title has to name the artist: a part
    that names nobody is a song, and the title a playlist of anybody's."""
    _answering(monkeypatch, [_artist(ARTIST_B, "Teresa Teng")])
    assert ident.band_from_title("Teresa Teng - Greatest Hits - Metropolis") is None


def test_musicbrainz_offline_names_no_band_and_asks_wikidata_nothing(monkeypatch):
    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", lambda p: pytest.fail("asked"))
    title = "邓丽君经典金曲 - Teresa Teng's classic songs"
    assert ident.band_from_title(title) is None
    assert ident.find_band_for(None, None, title=title) is None


def test_a_cancelled_job_names_no_band_from_its_title(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", lambda *a, **k: pytest.fail("asked"))
    title = "邓丽君经典金曲 - Teresa Teng's classic songs"
    assert ident.band_from_title(title, cancelled=lambda: True) is None


# ── the identity as it is kept ──


def test_title_aliases_are_capped_deduplicated_and_never_the_title():
    from app.core.models import clean_identity

    identity = clean_identity(
        {
            "source": "musicbrainz",
            "title": "좋은 날",
            "artist": "IU",
            "title_aliases": [
                "좋은 날",
                "Good Day",
                "Good Day",
                *[f"Alias {i}" for i in range(10)],
            ],
        }
    )
    assert identity["title_aliases"][0] == "Good Day"
    assert "좋은 날" not in identity["title_aliases"]
    assert len(identity["title_aliases"]) == 6
    assert len(set(identity["title_aliases"])) == 6


@pytest.mark.parametrize("aliases", ["Good Day", {"a": 1}, 7, [], [None, "", "  "]])
def test_title_aliases_that_are_not_a_list_of_names_leave_the_old_shape(aliases):
    from app.core.models import clean_identity

    identity = clean_identity(
        {"source": "musicbrainz", "title": "좋은 날", "artist": "IU", "title_aliases": aliases}
    )
    assert "title_aliases" not in identity


@pytest.mark.parametrize("score", [float("inf"), float("-inf"), float("nan")])
def test_a_score_that_is_not_a_finite_number_is_zero(score):
    from app.core.models import clean_identity

    identity = clean_identity({"source": "acoustid", "title": "T", "artist": "A", "score": score})
    assert identity["score"] == 0.0
