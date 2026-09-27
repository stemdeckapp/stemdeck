"""The work a soundtrack or cast recording is from (app/pipeline/work_lookup.py).

No network: MusicBrainz is answered by a stub standing in for
musicbrainz._fetch_json, Wikidata by one for artist_lookup._fetch_json. The
items are trimmed from Wikidata's real ones: Wicked the 2003 musical
(Q616439) names its cast album (Q7998266) as its soundtrack release, and the
1939 film The Wizard of Oz (Q193695) is linked from its soundtrack's release
group directly.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_mod
import app.pipeline.artist_lookup as al
import app.pipeline.musicbrainz as mb
import app.pipeline.work_lookup as wl
from app.core.models import Job, JobCancelled, clean_identity, clean_work
from app.core.registry import _jobs, _recover_done_job
from app.pipeline.identify import IdentifyLookup
from app.pipeline.runner import run_pipeline

WICKED_RG = "76729419-70a3-30d0-b316-515bc24c61e2"
OZ_RG = "cda6d83d-628f-3879-8e2a-2750463db9ec"
WICKED = {"id": "Q616439", "kind": "musical", "name": "Wicked", "englishName": "Wicked"}
OZ = {
    "id": "Q193695",
    "kind": "film",
    "name": "The Wizard of Oz",
    "englishName": "The Wizard of Oz",
}


def _claims(prop, *values):
    return {
        prop: [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"id": v}}}} for v in values]
    }


def _item(qid, label, classes, *, year=None, performed=None):
    claims = _claims(wl.INSTANCE_OF, *classes)
    if year:
        claims[wl.PUBLICATION_DATE] = [
            {"mainsnak": {"datavalue": {"value": {"time": f"+{year}-01-01T00:00:00Z"}}}}
        ]
    if performed:
        claims[wl.FIRST_PERFORMANCE] = [
            {"mainsnak": {"datavalue": {"value": {"time": f"+{performed}-06-10T00:00:00Z"}}}}
        ]
    return {"id": qid, "labels": {"en": {"value": label}}, "claims": claims}


ITEMS = {
    "Q616439": _item("Q616439", "Wicked", ["Q58483083"], performed=2003),  # the musical
    "Q24797403": _item("Q24797403", "Wicked", ["Q11424"], year=2024),  # the film
    "Q1735754": _item("Q1735754", "Wicked", ["Q7725634"], year=1995),  # the novel
    "Q7998268": _item("Q7998268", "Wicked", ["Q7889"], year=1989),  # the video game
    "Q7998266": _item("Q7998266", "Wicked", ["Q482994"], year=2003),  # the cast album
    "Q193695": _item("Q193695", "The Wizard of Oz", ["Q11424"], year=1939),
    "Q16147325": _item("Q16147325", "Musical selections in The Wizard of Oz", ["Q98645843"]),
    "Q55603193": _item("Q55603193", "The Wizard of Oz", ["Q7889"], year=1985),
    # Typed by a class not in WORK_CLASSES, two steps below "film".
    "Q900001": _item("Q900001", "Cats", ["Q900100"], year=2019),
}
# Each class's "subclass of", for the walk up from a class not listed.
CLASSES = {
    "Q900100": ["Q900200"],
    "Q900200": ["Q11424"],
    "Q7725634": ["Q838948"],
}
SEARCH = {
    "Wicked": ["Q1735754", "Q7998268", "Q616439", "Q24797403"],
    "The Wizard of Oz": ["Q193695", "Q55603193"],
    "Cats": ["Q900001"],
}
STATEMENTS = {
    "haswbstatement:P406=Q7998266": ["Q616439"],
    "haswbstatement:P345=tt0032138": ["Q193695"],
}


class Wikidata:
    """Stands in for artist_lookup._fetch_json: answers by action, records asks."""

    def __init__(self, *, search=None, fail=False):
        self.asked: list[dict] = []
        self.search = SEARCH if search is None else search
        self.fail = fail

    def __call__(self, params):
        self.asked.append(dict(params))
        if self.fail:
            raise OSError("Wikidata down")
        action = params["action"]
        if action == "wbsearchentities":
            ids = self.search.get(params["search"], [])
            return {"search": [{"id": i, "label": ITEMS[i]["labels"]["en"]["value"]} for i in ids]}
        if action == "query":
            ids = STATEMENTS.get(params["srsearch"], [])
            return {"query": {"search": [{"title": i} for i in ids]}}
        if action == "wbgetentities" and params["props"] == "claims":
            ids = params["ids"].split("|")
            return {
                "entities": {
                    i: {"id": i, "claims": _claims(wl.SUBCLASS_OF, *CLASSES.get(i, []))}
                    for i in ids
                }
            }
        if action == "wbgetentities":
            ids = params["ids"].split("|")
            return {"entities": {i: ITEMS[i] for i in ids if i in ITEMS}}
        raise AssertionError(f"unexpected Wikidata request {params}")


def _group(mbid, date, *urls):
    return {
        "id": mbid,
        "first-release-date": date,
        "relations": [{"type": "x", "url": {"resource": u}} for u in urls],
    }


GROUPS = {
    WICKED_RG: _group(
        WICKED_RG,
        "2003-12-16",
        "https://www.discogs.com/master/178343",
        "https://www.wikidata.org/wiki/Q7998266",
    ),
    OZ_RG: _group(
        OZ_RG,
        "1956",
        "https://www.wikidata.org/wiki/Q16147325",
        "https://www.wikidata.org/wiki/Q193695",
        "https://www.imdb.com/title/tt0032138/",
    ),
}


class MusicBrainz:
    def __init__(self, *, fail=False):
        self.asked: list[str] = []
        self.fail = fail

    def __call__(self, path, params):
        self.asked.append(path)
        if self.fail:
            raise OSError("MusicBrainz down")
        mbid = path.removeprefix("release-group/")
        if mbid in GROUPS:
            return GROUPS[mbid]
        raise AssertionError(f"unexpected MusicBrainz request {path}")


@pytest.fixture(autouse=True)
def _fresh_class_cache():
    wl._CLASS_PARENTS.clear()
    yield
    wl._CLASS_PARENTS.clear()


@pytest.fixture
def services(monkeypatch):
    wikidata = Wikidata()
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(al, "_fetch_json", wikidata)
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    return wikidata, musicbrainz


def _identity(album, rg=None, types=("Soundtrack",), **over):
    return clean_identity(
        {
            "source": "musicbrainz",
            "score": 1.0,
            "title": "Popular",
            "artist": "Kristin Chenoweth",
            "album": album,
            "release_group_mbid": rg,
            "secondary_types": list(types),
            **over,
        }
    )


# ── what the album says ──


@pytest.mark.parametrize(
    ("album", "name"),
    [
        ("Wicked (Original Broadway Cast Recording)", "Wicked"),
        ("The Wizard of Oz (Original Motion Picture Soundtrack)", "The Wizard of Oz"),
        ("Wicked: The Soundtrack", "Wicked"),
        ("Les Misérables - Original London Cast", "Les Misérables"),
        ("Music from the Motion Picture Grease", "Grease"),
        ("Frozen OST", "Frozen"),
        ("Hamilton [Original Broadway Cast Recording] (Deluxe)", "Hamilton (Deluxe)"),
        ("Nevermind", "Nevermind"),
        ("Nevermind (Remastered)", "Nevermind (Remastered)"),
        ("Music from Big Pink", "Music from Big Pink"),
    ],
)
def test_the_qualifier_is_taken_off_the_album(album, name):
    assert wl.strip_album(album) == name


@pytest.mark.parametrize(
    ("album", "kind"),
    [
        ("Wicked (Original Broadway Cast Recording)", "musical"),
        ("Les Misérables (Original Motion Picture Soundtrack)", "film"),
        ("Stranger Things (Soundtrack from the Netflix Series)", "tv"),
        ("Wicked: The Soundtrack", "film"),
        ("Nevermind", None),
    ],
)
def test_the_album_hints_at_the_kind(album, kind):
    assert wl.kind_hint(album) == kind


def test_only_a_soundtrack_or_a_qualified_album_is_looked_up():
    assert wl.work_query(_identity("Wicked", WICKED_RG), None) == ("Wicked", None, True)
    assert wl.work_query(_identity("Nevermind", types=()), None) is None
    assert wl.work_query(None, {"album": "Nevermind"}) is None
    assert wl.work_query(None, {"album": "Wicked (Original Broadway Cast Recording)"}) == (
        "Wicked",
        "musical",
        False,
    )
    # Identified as a compilation it is also on: the tag names the soundtrack.
    compilation = _identity("Showstoppers", types=("Compilation",))
    assert wl.work_query(compilation, {"album": "Wicked (Original Broadway Cast Recording)"}) == (
        "Wicked",
        "musical",
        False,
    )
    assert wl.might_have_work(None, None) is False


# ── resolving it ──


def test_a_cast_album_is_followed_to_the_musical(services):
    wikidata, musicbrainz = services
    assert wl.lookup_work(_identity("Wicked", WICKED_RG), None) == WICKED
    assert musicbrainz.asked == [f"release-group/{WICKED_RG}"]
    assert not any(p["action"] == "wbsearchentities" for p in wikidata.asked), "no search needed"
    assert {"action": "query", "srsearch": "haswbstatement:P406=Q7998266"}.items() <= next(
        p for p in wikidata.asked if p["action"] == "query"
    ).items()


def test_a_soundtrack_linked_to_its_film_is_the_film(services):
    wikidata, _ = services
    assert wl.lookup_work(_identity("The Wizard of Oz", OZ_RG), None) == OZ
    assert [p["action"] for p in wikidata.asked] == ["wbgetentities"]


def test_the_release_group_is_asked_once_and_kept(services):
    _, musicbrainz = services
    wl.lookup_work(_identity("Wicked", WICKED_RG), None)
    wl.lookup_work(_identity("Wicked", WICKED_RG), None)
    assert musicbrainz.asked == [f"release-group/{WICKED_RG}"]


def test_an_album_tag_alone_finds_the_musical_by_search(services):
    wikidata, musicbrainz = services
    work = wl.lookup_work(None, {"album": "Wicked (Original Broadway Cast Recording)"})
    assert work == WICKED
    assert musicbrainz.asked == []
    assert wikidata.asked[0]["search"] == "Wicked"


def test_the_album_says_film_and_the_film_is_kept(services):
    work = wl.lookup_work(None, {"album": "Wicked: The Soundtrack"})
    assert work == {**WICKED, "id": "Q24797403", "kind": "film"}


def test_a_work_newer_than_the_recording_is_not_it(services, monkeypatch):
    # The release group links nothing, so the search decides, with its year.
    monkeypatch.setitem(GROUPS, WICKED_RG, _group(WICKED_RG, "2003-12-16"))
    wikidata, _ = services
    wikidata.search = {"Wicked": ["Q24797403", "Q616439"]}
    assert wl.lookup_work(_identity("Wicked", WICKED_RG), None) == WICKED


WICKED_VIDEO_TITLE = 'Dancing Through Life (From "Wicked" Original Broadway Cast Recording/2003)'


def test_a_title_saying_broadway_beats_a_release_group_linking_the_film(services, monkeypatch):
    """An ambiguous release group: "Wicked" linked to the 2024 film. The
    upload's title says Broadway cast, 2003, so the name search is asked and
    the musical kept; without the title the link stands."""
    monkeypatch.setitem(
        GROUPS, WICKED_RG, _group(WICKED_RG, "", "https://www.wikidata.org/wiki/Q24797403")
    )
    identity = _identity("Wicked", WICKED_RG, title="Dancing Through Life")
    assert wl.lookup_work(identity, None)["id"] == "Q24797403"
    assert wl.lookup_work(identity, None, title=WICKED_VIDEO_TITLE) == WICKED


def test_a_title_alone_finds_the_musical_of_its_year(services):
    """What LRCLIB names an untagged upload by: an album with no qualifier."""
    wikidata, musicbrainz = services
    wikidata.search = {"Wicked": ["Q24797403", "Q616439"]}
    identity = _identity("Wicked", types=(), source="lrclib", title="Dancing Through Life")
    assert wl.find_work(identity, None) is None
    assert wl.find_work(identity, None, title=WICKED_VIDEO_TITLE) == WICKED
    assert musicbrainz.asked == []


def test_a_novel_or_a_video_game_by_the_same_name_is_not_a_work(services):
    wikidata, _ = services
    wikidata.search = {"Wicked": ["Q1735754", "Q7998268", "Q7998266"]}
    assert wl.lookup_work(None, {"album": "Wicked (Original Broadway Cast Recording)"}) is None
    # Never walked up from: a literary work is not one, whatever it is under.
    assert not any(p.get("props") == "claims" for p in wikidata.asked)


def test_a_class_below_film_is_found_by_walking_up(services):
    wikidata, _ = services
    work = wl.lookup_work(None, {"album": "Cats (Original Motion Picture Soundtrack)"})
    assert work == {"id": "Q900001", "kind": "film", "name": "Cats", "englishName": "Cats"}
    walks = [p["ids"] for p in wikidata.asked if p.get("props") == "claims"]
    assert walks == ["Q900100", "Q900200"]
    # The classes are kept: the next lookup walks nothing.
    wikidata.asked.clear()
    wl.lookup_work(None, {"album": "Cats (Original Motion Picture Soundtrack)"})
    assert not any(p.get("props") == "claims" for p in wikidata.asked)


def test_a_name_that_is_not_the_label_is_not_kept(services):
    wikidata, _ = services
    wikidata.search = {"Wicke": ["Q616439"]}
    assert wl.lookup_work(None, {"album": "Wicke (Original Broadway Cast Recording)"}) is None


def test_musicbrainz_down_still_searches(services):
    _, musicbrainz = services
    musicbrainz.fail = True
    assert wl.find_work(_identity("Wicked", WICKED_RG), None) == WICKED


def test_no_connection_is_no_work(services):
    wikidata, musicbrainz = services
    wikidata.fail = musicbrainz.fail = True
    assert wl.find_work(_identity("Wicked", WICKED_RG), None) is None
    assert wl.find_work(None, {"album": "Wicked (Original Broadway Cast Recording)"}) is None


def test_offline_is_no_work():
    """conftest's offline Wikidata and MusicBrainz, which every other test sees."""
    assert wl.find_work(_identity("Wicked", WICKED_RG), None) is None


def test_a_cancelled_lookup_asks_nothing(services):
    wikidata, musicbrainz = services
    assert wl.find_work(_identity("Wicked", WICKED_RG), None, cancelled=lambda: True) is None
    assert wikidata.asked == [] and musicbrainz.asked == []


# ── in the pipeline ──


def test_the_work_is_kept_once_the_pipeline_finishes(services):
    job = Job(
        id="abcdef000040",
        audio_tags={"title": "Popular", "album": "Wicked (Original Broadway Cast Recording)"},
        identity=_identity("Wicked", WICKED_RG),
    )
    lookup = IdentifyLookup.start(job, [Path("source.wav")])
    assert lookup is not None, "a known soundtrack is enough to start"
    lookup._thread.join(5)
    assert job.work is None, "only finish() writes, from the pipeline's thread"
    lookup.finish(job, 5)
    assert job.work == WICKED


def test_a_cancelled_job_gets_no_work(services, monkeypatch):
    gate = threading.Event()
    real = al._fetch_json

    def slow(params):
        gate.wait(5)
        return real(params)

    monkeypatch.setattr(al, "_fetch_json", slow)
    job = Job(id="abcdef000041", identity=_identity("Wicked", WICKED_RG))
    lookup = IdentifyLookup.start(job, [])
    job.cancel_requested = True
    gate.set()
    lookup.finish(job, 5)
    assert job.work is None


def test_a_work_still_out_keeps_the_band(services, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(wl, "lookup_work", lambda *a, **k: gate.wait(5) and WICKED)
    band = {"id": "Q229379", "name": "Kristin Chenoweth", "englishName": "Kristin Chenoweth"}
    monkeypatch.setattr("app.pipeline.identify.find_band_for", lambda *a, **k: band)
    job = Job(
        id="abcdef000042",
        audio_tags={"artist": "Kristin Chenoweth"},
        identity=_identity("Wicked", WICKED_RG),
    )
    lookup = IdentifyLookup.start(job, [])
    time.sleep(0.2)
    lookup.finish(job, 0.1)
    gate.set()
    lookup._thread.join(5)
    assert job.artist == band
    assert job.work is None, "an answer after finish() goes nowhere"


def test_nothing_to_find_starts_nothing_for_a_plain_album():
    band = {"id": "Q11649", "name": "Nirvana", "englishName": "Nirvana"}
    job = Job(id="abcdef000043", identity=_identity("Nevermind", types=()), artist=band)
    assert IdentifyLookup.start(job, []) is None
    # A re-split inherits its work, and asks for nothing.
    job = Job(id="abcdef000044", identity=_identity("Wicked", WICKED_RG), artist=band, work=WICKED)
    assert IdentifyLookup.start(job, []) is None


URL = "https://www.youtube.com/watch?v=6Nn8rBmhDjE"


def _download_with_tags(job, url, job_dir):
    job.audio_tags = {
        "artist": "Kristin Chenoweth",
        "title": "Popular",
        "album": "Wicked (Original Broadway Cast Recording)",
    }
    job.duration_sec = 224.0
    return job_dir / "source.wav"


async def test_a_cast_recording_finishes_with_its_work(services, tmp_path):
    """No AcoustID key and MusicBrainz's search offline: the tags are the
    identity, the album tag names the cast recording, and the musical is on
    the job when it is done, in its state, the registry and metadata.json."""

    def separation(job, source, job_dir):
        (job_dir / "stems").mkdir(parents=True, exist_ok=True)
        time.sleep(0.3)

    job = Job(id="abcdef000045")
    _jobs[job.id] = job
    try:
        with (
            patch("app.pipeline.runner.download", side_effect=_download_with_tags),
            patch("app.pipeline.runner._run_common", side_effect=separation),
        ):
            await run_pipeline(job, URL, tmp_path)
    finally:
        _jobs.pop(job.id, None)
    assert job.status == "done"
    assert job.work == WICKED
    assert job.artist is None, "Wikidata knows no band by that name here"
    assert job.to_state()["work"] == WICKED
    meta = json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["work"] == WICKED
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    [record] = [r for r in registry["jobs"] if r["id"] == job.id]
    assert record["work"] == WICKED


async def test_a_job_cancelled_while_finding_its_work_is_never_written(
    services, tmp_path, monkeypatch
):
    called = threading.Event()
    gate = threading.Event()

    def slow(identity, tags, **kw):
        called.set()
        gate.wait(5)
        return WICKED

    monkeypatch.setattr("app.pipeline.identify.find_work", slow)

    def cancelled_mid_separation(job, source, job_dir):
        assert called.wait(5)
        job.cancel_requested = True
        gate.set()
        raise JobCancelled()

    job = Job(id="abcdef000046")
    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=cancelled_mid_separation),
    ):
        await run_pipeline(job, URL, tmp_path)
    for thread in threading.enumerate():
        if thread.name == f"identify-{job.id}":
            thread.join(5)
    assert job.status == "cancelled"
    assert job.work is None


# ── on disk ──


def test_the_work_comes_back_from_metadata_and_a_bad_one_does_not(tmp_path: Path):
    for job_id, work, expected in (
        ("abcdef000050", WICKED, WICKED),
        ("abcdef000051", {**WICKED, "kind": "novel"}, None),
        ("abcdef000052", {**WICKED, "id": "wicked"}, None),
    ):
        job_dir = tmp_path / job_id
        (job_dir / "stems").mkdir(parents=True)
        (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
        (job_dir / "metadata.json").write_text(
            json.dumps({"title": "x", "work": work}), encoding="utf-8"
        )
        job = _recover_done_job(job_dir)
        assert job is not None
        assert job.work == expected


def test_a_registry_record_is_cleaned():
    assert Job.from_record({"id": "abcdef000053", "work": WICKED}).work == WICKED
    assert Job.from_record({"id": "abcdef000054", "work": "junk"}).work is None
    assert clean_work({**WICKED, "name": "", "englishName": ""}) is None


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


def _done_job(job_id, **fields) -> Job:
    job = Job(id=job_id, status="done", title="Popular", source_url=URL, **fields)
    _jobs[job.id] = job
    (jobs_mod.JOBS_DIR / job.id / "stems").mkdir(parents=True)
    (jobs_mod.JOBS_DIR / job.id / "metadata.json").write_text(
        '{"title": "Popular"}', encoding="utf-8"
    )
    return job


def test_an_older_soundtrack_job_gets_its_work(client, services):
    tags = {"artist": "Kristin Chenoweth", "title": "Popular"}
    job = _done_job("abcdef000060", audio_tags=tags, identity=_identity("Wicked", WICKED_RG))
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json()["work"] == WICKED
    assert job.work == WICKED
    meta = json.loads((jobs_mod.JOBS_DIR / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["work"] == WICKED
    registry = json.loads((jobs_mod.JOBS_DIR / "registry.json").read_text(encoding="utf-8"))
    [record] = [j for j in registry["jobs"] if j["id"] == job.id]
    assert record["work"] == WICKED


def test_a_job_that_is_no_soundtrack_asks_for_no_work(client):
    tags = {"artist": "Nirvana", "title": "Lithium", "album": "Nevermind"}
    job = _done_job("abcdef000061", audio_tags=tags)
    with patch("app.api.jobs.find_work") as find:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json()["work"] is None
    find.assert_not_called()


def test_a_work_already_there_is_answered_without_asking(client):
    tags = {"title": "Popular", "album": "Wicked (Original Broadway Cast Recording)"}
    job = _done_job("abcdef000062", audio_tags=tags, work=WICKED)
    with patch("app.api.jobs.find_work") as find:
        r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.json()["work"] == WICKED
    find.assert_not_called()


def test_a_failed_work_lookup_is_nothing_found(client):
    tags = {"title": "Popular", "album": "Wicked (Original Broadway Cast Recording)"}
    job = _done_job("abcdef000063", audio_tags=tags)
    r = client.post(f"/api/jobs/{job.id}/audio-tags")
    assert r.status_code == 200
    assert r.json()["work"] is None
    assert job.work is None


def test_a_resplit_keeps_the_work(client):
    job = _done_job("abcdef000064", identity=_identity("Wicked", WICKED_RG), work=WICKED)
    job.source_url = "local:Popular"
    (jobs_mod.JOBS_DIR / job.id / "source.wav").write_bytes(b"RIFF")
    with patch(
        "app.api.jobs._retained_source", return_value=jobs_mod.JOBS_DIR / job.id / "source.wav"
    ):
        r = client.post(f"/api/jobs/{job.id}/resplit", json={"stems": []})
    assert r.status_code == 200, r.text
    assert _jobs[r.json()["job_id"]].work == WICKED
