"""Songs in every language the app speaks identified as well as English ones
(app/pipeline/identify.py, musicbrainz.py): a title naming the artist in the
other script, a music video longer than the album cut, a compilation with no
recording to match. Offline: MusicBrainz and Wikidata answer from stand-ins
shaped like what they really answered for these uploads on 2026-09-27."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app.pipeline.artist_lookup as al
import app.pipeline.identify as ident
import app.pipeline.lyrics_lookup as ll
import app.pipeline.musicbrainz as mb
from app.pipeline.title_parse import TitleReading

JAY_CHOU = "a223958d-5c56-4b2c-a30a-87e357bc121b"
TERESA_TENG = "5c5cb762-d95e-47af-a7bf-35171eeab8e6"
IU = "b9545342-1e6d-4dae-84ac-013374ad8d7c"
BTS = "0d79fe8e-ba27-4859-bb8c-2f255f346853"
OTHER_BTS = "57729a9a-0238-414e-a3d4-b16b26c4a49d"
SUNNY_DAY = "11111111-1111-4111-8111-111111111111"
SUNNY_DAY_MV = "22222222-2222-4222-8222-222222222222"
SUNNY_DAY_INST = "33333333-3333-4333-8333-333333333333"
GOOD_DAY = "44444444-4444-4444-8444-444444444444"
RG = "55555555-5555-4555-8555-555555555555"

JAY_CHOU_ARTIST = {
    "id": JAY_CHOU,
    "score": 100,
    "name": "周杰倫",
    "sort-name": "Chou, Jay",
    "aliases": [{"name": "周杰伦"}, {"name": "ジェイ・チョウ"}, {"name": "Zhōu Jié Lún"}],
}
TERESA_TENG_ARTIST = {
    "id": TERESA_TENG,
    "score": 100,
    "name": "鄧麗君",
    "sort-name": "Teng, Teresa",
    "aliases": [{"name": "Teresa Teng"}, {"name": "テレサ・テン"}],
}
IU_ARTIST = {
    "id": IU,
    "score": 100,
    "name": "IU",
    "sort-name": "IU",
    "aliases": [{"name": "아이유"}, {"name": "이지은"}],
}


def _hit(rec, title, artist_id, artist_name, seconds, *, disambiguation="", score=100):
    return {
        "id": rec,
        "score": score,
        "title": title,
        "length": seconds * 1000,
        "disambiguation": disambiguation,
        "artist-credit": [{"name": artist_name, "artist": {"id": artist_id, "name": artist_name}}],
        "releases": [
            {
                "title": "葉惠美",
                "status": "Official",
                "release-group": {"id": RG, "title": "葉惠美", "primary-type": "Album"},
            }
        ],
    }


# 晴天 as MusicBrainz has it: the album cut (269 s), the music video's own
# recording (316 s, disambiguated) and an instrumental.
SUNNY_DAY_HITS = [
    _hit(SUNNY_DAY_INST, "晴天 (Instrumental)", JAY_CHOU, "周杰倫", 269),
    _hit(SUNNY_DAY, "晴天", JAY_CHOU, "周杰倫", 269),
    _hit(SUNNY_DAY_MV, "晴天", JAY_CHOU, "周杰倫", 316, disambiguation="music video"),
]


class MusicBrainz:
    """Stands in for musicbrainz._fetch_json: an artist search answers
    ``artists``, the first recording search ``first`` (the search by the
    title's words, which finds nothing for these) and later ones
    ``recordings``; an artist lookup answers its Wikidata link."""

    def __init__(self, *, artists=(), recordings=(), first=(), wikidata=None):
        self.artists = list(artists)
        self.recordings = list(recordings)
        self.first = list(first)
        self.wikidata = wikidata or {}
        self.works = {"relations": []}
        self.asked: list[tuple[str, dict]] = []

    def __call__(self, path, params, **_kwargs):
        self.asked.append((path, dict(params)))
        if path == "artist":
            return {"artists": self.artists}
        if path == "recording":
            searches = [p for p, _ in self.asked if p == "recording"]
            if len(searches) == 1 and "arid:" not in params["query"]:
                return {"recordings": self.first}
            return {"recordings": self.recordings}
        if path.startswith("recording/"):
            return self.works
        if path.startswith("work/"):
            return {"aliases": [{"name": "Joeun Nal"}]}
        if path.startswith("artist/"):
            qid = self.wikidata.get(path.removeprefix("artist/"))
            resource = f"https://www.wikidata.org/wiki/{qid}" if qid else ""
            return {"relations": [{"type": "wikidata", "url": {"resource": resource}}]}
        raise AssertionError(f"unexpected MusicBrainz request {path}")

    def queries(self, path):
        return [p["query"] for asked, p in self.asked if asked == path]


def _wikidata(qid, mbid, labels):
    def fetch(params):
        return {
            "entities": {
                qid: {
                    "id": qid,
                    "labels": {code: {"value": value} for code, value in labels.items()},
                    "claims": {"P434": [{"mainsnak": {"datavalue": {"value": mbid}}}]},
                }
            }
        }

    return fetch


@pytest.fixture(autouse=True)
def _fresh_artist_cache():
    mb._ARTISTS_FOUND.clear()
    yield
    mb._ARTISTS_FOUND.clear()


@pytest.fixture(autouse=True)
def _no_lrclib_answer(monkeypatch):
    monkeypatch.setattr(ll, "_fetch_json", lambda *a, **k: [])


# ── the search by the title's words, with its other names ──


def test_every_name_of_the_song_is_asked_for_in_one_search(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    reading = TitleReading(
        "晴天", artist="周杰倫", song_alts=("Sunny Day",), artist_alts=("Jay Chou",)
    )
    mb.search_by_title(reading, 318.0)
    query = musicbrainz.queries("recording")[0]
    assert query.startswith('recording:("晴天" OR "Sunny Day")')
    for word in ("周杰倫", "jay", "chou"):
        assert f'"{word}"' in query


def test_a_credit_in_one_script_covers_a_title_in_two():
    """周杰倫 alone covers "周杰倫 Jay Chou": the best of the names counts."""
    reading = TitleReading(
        "晴天", artist="周杰倫", song_alts=("Sunny Day",), artist_alts=("Jay Chou",)
    )
    answer = {"recordings": [_hit(SUNNY_DAY, "晴天", JAY_CHOU, "周杰倫", 269)]}
    identity = mb.best_title_match(answer, reading, 270.0)
    assert identity["recording_mbid"] == SUNNY_DAY
    assert identity["score"] == 1.0


def test_traditional_and_simplified_chinese_are_one_name():
    reading = TitleReading("甜蜜蜜", artist="邓丽君")
    answer = {"recordings": [_hit(SUNNY_DAY, "甜蜜蜜", TERESA_TENG, "鄧麗君", 215)]}
    assert mb.best_title_match(answer, reading, 215.0)["artist"] == "鄧麗君"


# ── the artist by name and alias ──


def test_an_artist_is_found_by_an_alias_in_another_script(monkeypatch):
    musicbrainz = MusicBrainz(artists=[JAY_CHOU_ARTIST])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    found = mb.search_artists(["周杰倫", "Jay Chou"])
    assert [(a["id"], a["matched"]) for a in found] == [(JAY_CHOU, "周杰倫")]
    query = musicbrainz.queries("artist")[0]
    assert 'alias:"Jay Chou"' in query and 'artist:"周杰倫"' in query
    # The sort name read both ways round, and simplified for traditional.
    mb._ARTISTS_FOUND.clear()
    assert mb.search_artists(["Jay Chou"])[0]["id"] == JAY_CHOU
    mb._ARTISTS_FOUND.clear()
    assert mb.search_artists(["周杰伦"])[0]["id"] == JAY_CHOU


def test_an_artist_by_another_name_is_not_kept(monkeypatch):
    """The search forgives spelling; the names have to agree exactly."""
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(artists=[JAY_CHOU_ARTIST]))
    assert mb.search_artists(["Jay Chu"]) == []


def test_an_artist_is_asked_for_once(monkeypatch):
    musicbrainz = MusicBrainz(artists=[IU_ARTIST])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    mb.search_artists(["IU", "아이유"])
    mb.search_artists(["IU", "아이유"])
    assert len(musicbrainz.queries("artist")) == 1


def test_a_name_cannot_change_the_query(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    mb.search_artists(['x" OR artist:"y'])
    assert musicbrainz.queries("artist") == [
        'artist:"x\\" OR artist:\\"y" OR alias:"x\\" OR artist:\\"y"'
    ]


# ── the song among the artist's recordings ──


def test_a_music_video_is_the_album_cut_it_is_longer_than():
    """周杰倫's 晴天 video runs 318 s, the album cut 269: the cut, not the
    instrumental, and not the video's own recording either, which is further
    from nothing but has a disambiguation."""
    identity = mb.best_artist_match({"recordings": SUNNY_DAY_HITS[:2]}, ["晴天"], [JAY_CHOU], 318.0)
    assert identity["recording_mbid"] == SUNNY_DAY
    assert identity["title"] == "晴天"


def test_the_recording_the_length_of_the_upload_comes_first():
    identity = mb.best_artist_match({"recordings": SUNNY_DAY_HITS}, ["晴天"], [JAY_CHOU], 316.0)
    assert identity["recording_mbid"] == SUNNY_DAY_MV


@pytest.mark.parametrize(
    ("seconds", "kept"),
    [(269, True), (318 - 150, True), (318 - 151, False), (323, False)],
    ids=["shorter", "at-the-bound", "too-short", "longer-than-the-upload"],
)
def test_a_recording_may_be_shorter_than_the_video_never_longer(seconds, kept):
    answer = {"recordings": [_hit(SUNNY_DAY, "晴天", JAY_CHOU, "周杰倫", seconds)]}
    assert (mb.best_artist_match(answer, ["晴天"], [JAY_CHOU], 318.0) is not None) is kept


def test_only_the_artists_asked_for_count():
    answer = {"recordings": [_hit(SUNNY_DAY, "晴天", OTHER_BTS, "Somebody", 269)]}
    assert mb.best_artist_match(answer, ["晴天"], [JAY_CHOU], 318.0) is None


def test_the_search_asks_for_every_name_by_those_artists(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    mb.search_by_artist(["Good Day", "좋은 날"], [IU, "not an mbid"], 357.0)
    assert musicbrainz.queries("recording") == [
        f'recording:("Good Day" OR "좋은 날") AND arid:({IU}) AND dur:[207000 TO 361000]'
        " AND status:official"
    ]


# ── the whole identification ──


def test_a_chinese_title_is_identified_through_its_artist(monkeypatch):
    musicbrainz = MusicBrainz(artists=[JAY_CHOU_ARTIST], recordings=SUNNY_DAY_HITS[:2])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None,
        title="周杰倫 Jay Chou【晴天 Sunny Day】-Official Music Video",
        duration=318.0,
        audio=[],
        api_key=None,
    )
    assert identity["source"] == "musicbrainz"
    assert identity["recording_mbid"] == SUNNY_DAY
    assert identity["artist_mbids"] == [JAY_CHOU]


def test_a_korean_title_is_identified_by_either_name(monkeypatch):
    good_day = _hit(GOOD_DAY, "좋은 날", IU, "IU", 233)
    musicbrainz = MusicBrainz(artists=[IU_ARTIST], recordings=[good_day])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None,
        title="IU (아이유) _ Good Day (좋은 날) _ MV",
        duration=357.0,
        audio=[],
        api_key=None,
    )
    assert identity["recording_mbid"] == GOOD_DAY


def test_a_title_with_no_separator_is_cut_where_an_artist_is_named(monkeypatch):
    hibari = "66666666-6666-4666-8666-666666666666"
    song = "77777777-7777-4777-8777-777777777777"
    artist = {"id": hibari, "score": 100, "name": "美空ひばり", "sort-name": "Misora, Hibari"}
    musicbrainz = MusicBrainz(
        artists=[artist], recordings=[_hit(song, "川の流れのように", hibari, "美空ひばり", 250)]
    )
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None, title="美空ひばり 川の流れのように", duration=252.0, audio=[], api_key=None
    )
    assert identity["recording_mbid"] == song
    assert musicbrainz.queries("recording")[0].startswith('recording:"川の流れのように" AND arid:')


def test_an_artist_search_that_fails_is_no_identity_and_no_error(monkeypatch):
    def fetch(path, params, **_kwargs):
        if path == "artist":
            raise OSError("down")
        return {"recordings": []}

    monkeypatch.setattr(mb, "_fetch_json", fetch)
    assert (
        ident.identify(
            tags=None, title="周杰倫 Jay Chou【晴天】", duration=318.0, audio=[], api_key=None
        )
        is None
    )


def test_english_titles_ask_what_they_asked_before(monkeypatch):
    """An English title found by its words never reaches the artist search."""
    queen = {"id": JAY_CHOU, "name": "Queen"}
    hit = _hit(SUNNY_DAY, "Bohemian Rhapsody", queen["id"], "Queen", 354)
    musicbrainz = MusicBrainz(first=[hit])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None,
        title="Queen - Bohemian Rhapsody (Official Video Remastered)",
        duration=356.0,
        audio=[],
        api_key=None,
    )
    assert identity["recording_mbid"] == SUNNY_DAY
    assert [path for path, _ in musicbrainz.asked] == ["recording"]


# ── the band ──


def test_a_compilation_finds_its_band_by_the_artist_it_names(monkeypatch):
    """邓丽君经典金曲 - Teresa Teng's classic songs: 59 minutes, no recording
    that long, and still the artist box shows Teresa Teng."""
    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(
        al, "_fetch_json", _wikidata("Q15971", TERESA_TENG, {"zh": "鄧麗君", "en": "Teresa Teng"})
    )
    band = ident.find_band_for(None, None, title="邓丽君经典金曲 - Teresa Teng's classic songs")
    assert band == {"id": "Q15971", "name": "鄧麗君", "englishName": "Teresa Teng"}
    query = musicbrainz.queries("artist")[0]
    assert '"邓丽君"' in query and '"Teresa Teng"' in query


def test_a_bilingual_artist_tag_finds_its_band(monkeypatch):
    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    wikidata = _wikidata("Q15971", TERESA_TENG, {"zh": "鄧麗君", "en": "Teresa Teng"})
    asked = []

    def fetch(params):
        asked.append(params["action"])
        # The whole bilingual name finds nothing by search.
        return {"search": []} if params["action"] == "wbsearchentities" else wikidata(params)

    monkeypatch.setattr(al, "_fetch_json", fetch)
    band = ident.find_band_for(None, "鄧麗君 Teresa Teng テレサ・テン")
    assert band["id"] == "Q15971"


def test_two_artists_as_likely_are_no_band(monkeypatch):
    """Two artists called BTS scoring alike: without a recording to say
    which, neither is taken."""
    twins = [
        {"id": BTS, "score": 100, "name": "BTS"},
        {"id": OTHER_BTS, "score": 95, "name": "BTS"},
    ]
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(artists=twins, wikidata={BTS: "Q13580495"}))
    monkeypatch.setattr(al, "_fetch_json", lambda params: {"search": []})
    assert ident.find_band_for(None, None, title="BTS (방탄소년단) - Live 2019") is None


def test_a_song_title_alone_names_no_band(monkeypatch):
    musicbrainz = MusicBrainz(artists=[{"id": BTS, "score": 100, "name": "Me Now"}])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    assert ident.find_band_for(None, None, title="Don't Stop Me Now") is None


METROPOLIS = "88888888-8888-4888-8888-888888888888"


@pytest.mark.parametrize(
    "title",
    [
        # Once made Metropolis the band: a part that merely is a band's name.
        "Metropolis - Part I",
        "Metropolis",
        # Nor is a compilation of someone else's songs by that name.
        "Metropolis - Queen Greatest Hits",
    ],
)
def test_a_title_that_merely_contains_a_bands_name_names_no_band(monkeypatch, title):
    artist = {"id": METROPOLIS, "score": 100, "name": "Metropolis"}
    musicbrainz = MusicBrainz(artists=[artist], wikidata={METROPOLIS: "Q1"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(al, "_fetch_json", _wikidata("Q1", METROPOLIS, {"en": "Metropolis"}))
    assert ident.find_band_for(None, None, title=title) is None


def test_one_script_compilation_names_its_band_when_nobody_else_is_named(monkeypatch):
    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(al, "_fetch_json", _wikidata("Q15971", TERESA_TENG, {"en": "Teresa Teng"}))
    assert ident.find_band_for(None, None, title="Teresa Teng - Greatest Hits")["id"] == "Q15971"


def test_a_compilation_of_a_name_two_artists_share_names_no_band(monkeypatch):
    twins = [
        {"id": BTS, "score": 100, "name": "Perfect"},
        {"id": OTHER_BTS, "score": 100, "name": "Perfect"},
    ]
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(artists=twins))
    assert ident.find_band_for(None, None, title="Perfect - Greatest Hits") is None


def test_a_bilingual_name_in_a_live_title_names_its_band(monkeypatch):
    musicbrainz = MusicBrainz(artists=[JAY_CHOU_ARTIST], wikidata={JAY_CHOU: "Q238819"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(
        al, "_fetch_json", _wikidata("Q238819", JAY_CHOU, {"zh": "周杰倫", "en": "Jay Chou"})
    )
    band = ident.find_band_for(None, None, title="周杰倫 Jay Chou - 晴天 (Live 2019)")
    assert band == {"id": "Q238819", "name": "周杰倫", "englishName": "Jay Chou"}


def test_a_wikidata_item_that_is_no_musician_is_no_band(monkeypatch):
    """The item has to name the MusicBrainz artist back (P434)."""
    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(al, "_fetch_json", _wikidata("Q15971", JAY_CHOU, {"en": "Teresa Teng"}))
    assert (
        ident.find_band_for(None, None, title="邓丽君经典金曲 - Teresa Teng's classic songs")
        is None
    )


COMPILATION_TITLE = "邓丽君经典金曲 - Teresa Teng's classic songs"


def test_an_older_compilation_gets_its_band_from_the_backfill(monkeypatch):
    """Job a17646838af6: 59 minutes, no tags, no identity. The artist box had
    to be searched by hand; the backfill now finds Teresa Teng itself."""
    from fastapi.testclient import TestClient

    import app.api.jobs as jobs_mod
    from app.core.models import Job
    from app.core.registry import _jobs

    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    wikidata = _wikidata("Q15971", TERESA_TENG, {"zh": "鄧麗君", "en": "Teresa Teng"})
    monkeypatch.setattr(
        al,
        "_fetch_json",
        lambda params: (
            {"search": []} if params["action"] == "wbsearchentities" else wikidata(params)
        ),
    )
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    job = Job(
        id="a17646838af6",
        status="done",
        title=COMPILATION_TITLE,
        duration_sec=3546.0,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    _jobs[job.id] = job
    (jobs_mod.JOBS_DIR / job.id / "stems").mkdir(parents=True)
    try:
        with (
            patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None),
            patch("app.api.jobs.fetch_audio_tags", return_value=None),
        ):
            from app.main import app

            with TestClient(app) as client:
                r = client.post(f"/api/jobs/{job.id}/audio-tags")
        assert r.status_code == 200
        assert r.json()["artist"] == {
            "id": "Q15971",
            "name": "鄧麗君",
            "englishName": "Teresa Teng",
        }
        assert job.artist == r.json()["artist"]
        assert job.identity is None
    finally:
        _jobs.clear()
        jobs_mod._TAG_LOOKUPS.clear()


def test_a_compilation_import_gets_its_band_while_it_separates(monkeypatch):
    from app.core.models import Job

    musicbrainz = MusicBrainz(artists=[TERESA_TENG_ARTIST], wikidata={TERESA_TENG: "Q15971"})
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(
        al, "_fetch_json", _wikidata("Q15971", TERESA_TENG, {"zh": "鄧麗君", "en": "Teresa Teng"})
    )
    for title in (COMPILATION_TITLE, "邓丽君经典金曲"):
        job = Job(id="abcdef00c001", title=title, duration_sec=3546.0)
        lookup = ident.IdentifyLookup.start(job, [])
        assert lookup is not None, title
        lookup.finish(job, 10)
        assert job.artist is not None and job.artist["id"] == "Q15971", title


# ── what only looks like the artist's recording ──


def _on(hit, album, secondary=()):
    group = {"id": RG, "title": album, "primary-type": "Album", "secondary-types": list(secondary)}
    return {**hit, "releases": [{"title": album, "status": "Official", "release-group": group}]}


def test_a_tribute_album_naming_the_artist_is_not_their_recording():
    """Alexandra's "Estranha forma de vida" on "Alexandra recorda Amália":
    the album's title names Amália, the credit does not."""
    reading = TitleReading("Estranha forma de vida", artist="Amália Rodrigues")
    hit = _on(
        _hit(SUNNY_DAY, "Estranha forma de vida", OTHER_BTS, "Alexandra", 222),
        "Alexandra recorda Amália",
    )
    assert mb.best_title_match({"recordings": [hit]}, reading, 222.0) is None


def test_a_cover_is_never_the_song():
    reading = TitleReading("Lemon", artist="米津玄師", artist_alts=("Kenshi Yonezu",))
    hit = _on(
        _hit(SUNNY_DAY, "Lemon (Kenshi Yonezu Cover)", OTHER_BTS, "KOBASOLO & Harutya", 274),
        "Lemon (Kenshi Yonezu Cover)",
    )
    assert mb.best_title_match({"recordings": [hit]}, reading, 274.0) is None
    assert mb.best_artist_match({"recordings": [hit]}, ["Lemon"], [OTHER_BTS], 274.0) is None


def test_a_show_named_where_the_artist_would_be_still_counts():
    """ "Defying Gravity | Wicked": the show's album names it, not the credit."""
    reading = TitleReading("Defying Gravity", artist="Wicked")
    hit = _on(
        _hit(SUNNY_DAY, "Defying Gravity", OTHER_BTS, "Idina Menzel", 354),
        "Wicked",
        secondary=("Soundtrack",),
    )
    assert mb.best_title_match({"recordings": [hit]}, reading, 354.0)["recording_mbid"] == SUNNY_DAY


def test_a_channels_title_is_searched_for_by_the_song_in_it(monkeypatch):
    """YOASOBI's channel, "YOASOBI「群青」Official Music Video": the tags say
    YOASOBI and 「群青」Official Music Video; the song searched for is 群青."""
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    ident.identify(
        tags={"artist": "YOASOBI", "title": "「群青」Official Music Video"},
        title="YOASOBI「群青」Official Music Video",
        duration=262.0,
        audio=[],
        api_key=None,
    )
    assert musicbrainz.queries("recording")[0].startswith('recording:"群青" AND artist:"YOASOBI"')


# ── the song's other titles, for the lyrics ──

WORK = "99999999-9999-4999-8999-999999999999"


def test_the_songs_other_titles_go_on_the_identity(monkeypatch):
    good_day = _hit(GOOD_DAY, "좋은 날", IU, "IU", 233)
    musicbrainz = MusicBrainz(artists=[IU_ARTIST], recordings=[good_day])
    musicbrainz.works = {
        "relations": [{"type": "performance", "work": {"id": WORK, "title": "좋은 날"}}]
    }
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None,
        title="IU (아이유) _ Good Day (좋은 날) _ MV",
        duration=357.0,
        audio=[],
        api_key=None,
    )
    # The upload's own, then the work's alias; never the title itself.
    assert identity["title_aliases"] == ["Good Day", "Joeun Nal"]


def test_an_english_song_asks_nothing_more_for_its_titles(monkeypatch):
    hit = _hit(SUNNY_DAY, "Bohemian Rhapsody", JAY_CHOU, "Queen", 354)
    musicbrainz = MusicBrainz(first=[hit])
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags=None, title="Queen - Bohemian Rhapsody", duration=356.0, audio=[], api_key=None
    )
    assert "title_aliases" not in identity
    assert [path for path, _ in musicbrainz.asked] == ["recording"]


def test_the_lyrics_are_looked_for_under_the_songs_other_titles():
    from app.core.models import Job, clean_identity

    identity = clean_identity(
        {
            "source": "musicbrainz",
            "title": "좋은 날",
            "artist": "IU",
            "duration": 233.0,
            "title_aliases": ["Good Day", "", 3],
        }
    )
    assert identity["title_aliases"] == ["Good Day"]
    job = Job(id="abcdef00c002", title="IU _ Good Day", duration_sec=233.0)
    q = ll.build_query(job, identity=identity)
    assert q.track_aliases == ("Good Day",)
    row = {
        "id": 7,
        "trackName": "Good Day",
        "artistName": "IU",
        "albumName": "Real",
        "duration": 233.0,
        "instrumental": False,
        "syncedLyrics": "[00:01.00]words",
        "plainLyrics": "words",
    }
    asked = []

    def lrclib(endpoint, params):
        asked.append(params.get("track_name") or params.get("q"))
        if endpoint == "search" and params.get("track_name") == "Good Day":
            return [row]
        return None if endpoint == "get" else []

    answer = ll.lookup_lyrics(q, fetch_json=lrclib, aliases=lambda query, cancelled: [])
    assert answer.lyrics["lrclib_id"] == 7
    assert asked[-1] == "Good Day"
