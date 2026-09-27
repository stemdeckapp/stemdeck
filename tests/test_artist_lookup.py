"""The band a job's artist tag names, found on Wikidata while the job runs (#699).

No network: Wikidata is answered by a stub standing in for _fetch_json, and
conftest makes every other test see it as offline. The matching cases mirror
tests/js/artist-lookup.test.mjs, since this is a port of that module and the
two must agree on what counts as the same name.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import app.pipeline.artist_lookup as al
from app.core.models import Job, clean_artist
from app.core.registry import _jobs, _recover_done_job
from app.pipeline.artist_lookup import (
    BandLookup,
    artist_matches_name,
    find_band,
    lookup_band,
    pick_artist,
    search_language,
    tagged_artist_name,
)
from app.pipeline.runner import run_local_pipeline, run_pipeline

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_FETCH_JSON = al._fetch_json

DT = {"id": "Q162586", "name": "Dream Theater", "englishName": "Dream Theater"}

# Trimmed from Wikidata's real answers for "Dream Theater": the album of the
# same name ranks first and is skipped for having no MusicBrainz artist id.
SEARCH = {"search": [{"id": "Q13420662"}, {"id": "Q162586"}, {"id": "not-an-id"}]}
ENTITIES = {
    "entities": {
        "Q13420662": {
            "id": "Q13420662",
            "labels": {"en": {"value": "Dream Theater"}},
            "claims": {},
        },
        "Q162586": {
            "id": "Q162586",
            "labels": {"en": {"value": "Dream Theater"}},
            "claims": {"P434": [{}], "P31": [{}]},
        },
    }
}


class Wikidata:
    """Stands in for _fetch_json: answers by action, records what was asked."""

    def __init__(self, search=SEARCH, entities=ENTITIES, *, gate=None, fail=None):
        self.search = search
        self.entities = entities
        self.gate = gate
        self.fail = fail
        self.asked: list[dict[str, str]] = []
        self.called = threading.Event()

    def __call__(self, params):
        self.asked.append(dict(params))
        self.called.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail is not None:
            raise self.fail
        return self.search if params["action"] == "wbsearchentities" else self.entities


# ── matching: the cases in tests/js/artist-lookup.test.mjs ──


@pytest.mark.parametrize(
    ("artist", "name"),
    [
        (DT, "Dream Theater"),
        (DT, "  dream  THEATER "),
        ({"name": "AC/DC", "englishName": "AC/DC"}, "ACDC"),
        ({"name": "Beyoncé", "englishName": "Beyoncé"}, "Beyonce"),
        ({"name": "ドリーム・シアター", "englishName": "Dream Theater"}, "Dream Theater"),
    ],
)
def test_names_that_match(artist, name):
    assert artist_matches_name(artist, name)


@pytest.mark.parametrize(
    ("artist", "name"),
    [
        (DT, "Dream Theatre"),
        (DT, "Theater"),
        ({"name": "!!!", "englishName": ""}, "!!!"),
        (None, "Dream Theater"),
    ],
)
def test_names_that_do_not(artist, name):
    assert not artist_matches_name(artist, name)


def test_the_marks_in_a_japanese_name_are_part_of_it():
    # バ and ハ differ only by a combining mark once decomposed.
    assert not artist_matches_name({"name": "バンド", "englishName": ""}, "ハンド")


@pytest.mark.parametrize(
    ("tag", "name"),
    [
        ("Dream Theater feat. Someone", "Dream Theater"),
        ("Dream Theater (ft. Someone)", "Dream Theater"),
        ("Dream Theater [featuring Someone]", "Dream Theater"),
        ("  Dream Theater ", "Dream Theater"),
        (None, ""),
    ],
)
def test_a_featured_artist_is_left_off(tag, name):
    assert tagged_artist_name(tag) == name


def test_the_first_hit_that_is_an_artist_is_the_band():
    entities = ENTITIES["entities"]
    assert pick_artist(entities, ["Q13420662", "Q162586"])["id"] == "Q162586"
    assert pick_artist({"Q13420662": entities["Q13420662"]}, ["Q13420662"]) is None
    assert pick_artist(None, ["Q162586"]) is None


@pytest.mark.parametrize(
    ("name", "lang"),
    [
        ("Dream Theater", "en"),
        ("Beyoncé", "en"),
        ("ドリーム・シアター", "ja"),
        ("東京事変", "zh"),
        ("아이유", "ko"),
        ("Кино", "ru"),
    ],
)
def test_a_name_is_searched_in_its_own_script(name, lang):
    assert search_language(name) == lang


def test_clean_artist_keeps_only_a_wikidata_band():
    assert clean_artist(DT) == DT
    assert clean_artist({**DT, "extra": "x"}) == DT
    assert clean_artist({**DT, "id": "Q1 OR 1=1"}) is None
    assert clean_artist({**DT, "id": "P434"}) is None
    assert clean_artist({"id": "Q1", "name": 3, "englishName": None}) is None
    assert clean_artist("Q162586") is None


# ── the lookup ──


def test_a_band_by_exactly_that_name_is_found():
    wikidata = Wikidata()
    assert lookup_band("Dream Theater", fetch_json=wikidata) == DT
    search, entities = wikidata.asked
    # The name, and nothing else about the track.
    assert search["action"] == "wbsearchentities"
    assert search["search"] == "Dream Theater"
    assert search["language"] == "en"
    assert entities["action"] == "wbgetentities"
    assert entities["ids"] == "Q13420662|Q162586", "ids that are not items are dropped"
    assert "claims" in entities["props"]


def test_a_band_by_another_name_is_not_kept():
    # Wikidata's search forgives the spelling and answers Dream Theater.
    assert lookup_band("Dream Theatre", fetch_json=Wikidata()) is None


def test_a_name_in_another_script_matches_its_own_label():
    entities = {
        "entities": {
            "Q162586": {
                "id": "Q162586",
                "labels": {
                    "ja": {"value": "ドリーム・シアター"},
                    "en": {"value": "Dream Theater"},
                },
                "claims": {"P434": [{}]},
            }
        }
    }
    wikidata = Wikidata(search={"search": [{"id": "Q162586"}]}, entities=entities)
    band = lookup_band("ドリーム・シアター", fetch_json=wikidata)
    assert band == {"id": "Q162586", "name": "ドリーム・シアター", "englishName": "Dream Theater"}
    assert wikidata.asked[1]["languages"] == "ja|en"


def test_no_artist_among_the_hits_is_no_band():
    assert lookup_band("Dream Theater", fetch_json=Wikidata(search={"search": []})) is None
    only_album = {"entities": {"Q13420662": ENTITIES["entities"]["Q13420662"]}}
    assert lookup_band("Dream Theater", fetch_json=Wikidata(entities=only_album)) is None


def test_an_id_that_is_not_an_item_is_never_kept():
    entities = {
        "entities": {
            "Q162586": {**ENTITIES["entities"]["Q162586"], "id": "Q162586<script>"},
        }
    }
    wikidata = Wikidata(search={"search": [{"id": "Q162586"}]}, entities=entities)
    assert lookup_band("Dream Theater", fetch_json=wikidata) is None


def test_no_connection_is_no_band_and_no_error():
    assert find_band("Dream Theater", fetch_json=Wikidata(fail=OSError("offline"))) is None
    # conftest's stand-in for the network is offline too.
    assert find_band("Dream Theater") is None


def test_nonsense_from_wikidata_is_no_band():
    assert find_band("Dream Theater", fetch_json=lambda params: ["not", "an", "object"]) is None


def test_no_artist_tag_asks_nothing():
    wikidata = Wikidata()
    assert find_band("", fetch_json=wikidata) is None
    assert find_band(None, fetch_json=wikidata) is None
    assert wikidata.asked == []


def test_a_featured_artist_is_not_searched_for():
    wikidata = Wikidata()
    assert find_band("Dream Theater feat. Someone", fetch_json=wikidata) == DT
    assert wikidata.asked[0]["search"] == "Dream Theater"


def test_a_cancel_between_requests_stops_the_second():
    calls = {"n": 0}

    def cancelled():
        calls["n"] += 1
        return calls["n"] > 1

    wikidata = Wikidata()
    assert lookup_band("Dream Theater", fetch_json=wikidata, cancelled=cancelled) is None
    assert [p["action"] for p in wikidata.asked] == ["wbsearchentities"]


def test_the_request_names_stemdeck(monkeypatch):
    """Wikimedia's policy: a client says who it is and how to reach its maker."""
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return b'{"search": []}'

    def urlopen(request, timeout, context):
        seen["url"] = request.full_url
        seen["agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(al.urllib.request, "urlopen", urlopen)
    assert _REAL_FETCH_JSON({"action": "wbsearchentities", "search": "Dream Theater"}) == {
        "search": []
    }
    assert seen["url"].startswith("https://www.wikidata.org/w/api.php?")
    assert "search=Dream+Theater" in seen["url"]
    assert "StemDeck" in seen["agent"] and "github.com" in seen["agent"]
    assert seen["timeout"] == al.TIMEOUT_ARTIST_LOOKUP


# ── in a thread beside the pipeline ──


def _tagged_job(job_id: str, **fields) -> Job:
    return Job(
        id=job_id, audio_tags={"artist": "Dream Theater", "title": "Pull Me Under"}, **fields
    )


def test_the_answer_is_kept_once_the_pipeline_finishes(monkeypatch):
    monkeypatch.setattr(al, "_fetch_json", Wikidata())
    job = _tagged_job("abcdefabc200")
    lookup = BandLookup.start(job)
    assert lookup is not None
    lookup.finish(job, 5)
    assert job.artist == DT


def test_the_thread_never_writes_to_the_job_itself(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(al, "_fetch_json", Wikidata(gate=gate))
    job = _tagged_job("abcdefabc201")
    lookup = BandLookup.start(job)
    gate.set()
    lookup._thread.join(5)
    assert job.artist is None, "only finish() writes, from the pipeline's thread"
    lookup.finish(job, 5)
    assert job.artist == DT


def test_a_cancelled_job_is_never_written_to(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(al, "_fetch_json", Wikidata(gate=gate))
    job = _tagged_job("abcdefabc202")
    lookup = BandLookup.start(job)
    job.cancel_requested = True
    gate.set()
    lookup.finish(job, 5)
    assert job.artist is None


def test_an_answer_too_late_is_dropped(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(al, "_fetch_json", Wikidata(gate=gate))
    job = _tagged_job("abcdefabc203")
    lookup = BandLookup.start(job)
    started = time.monotonic()
    lookup.finish(job, 0.1)
    assert time.monotonic() - started < 1
    gate.set()
    lookup._thread.join(5)
    assert job.artist is None


def test_nothing_to_look_for_starts_nothing(monkeypatch):
    wikidata = Wikidata()
    monkeypatch.setattr(al, "_fetch_json", wikidata)
    assert BandLookup.start(Job(id="abcdefabc204")) is None
    assert BandLookup.start(Job(id="abcdefabc205", audio_tags={"title": "Song"})) is None
    # A re-split inherits its source's band.
    assert BandLookup.start(_tagged_job("abcdefabc206", artist=DT)) is None
    assert wikidata.asked == []


# ── the pipeline ──

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _download_with_tags(job, url, job_dir):
    job.audio_tags = {"artist": "Dream Theater", "title": "Pull Me Under"}
    return job_dir / "source.wav"


def _separation(seconds: float):
    def run_common(job, source, job_dir):
        (job_dir / "stems").mkdir(parents=True, exist_ok=True)
        time.sleep(seconds)

    return run_common


async def test_a_link_finishes_with_its_band_on_the_job_and_on_disk(tmp_path: Path, monkeypatch):
    """The lookup starts once the download has the tags and runs while the
    job separates, so the job is done with its band, which is in the state,
    the registry record and metadata.json, and waiting for it cost nothing."""
    slow = Wikidata()

    def answer_slowly(params):
        time.sleep(0.2)
        return slow(params)

    monkeypatch.setattr(al, "_fetch_json", answer_slowly)
    job = Job(id="abcdefabc210")
    _jobs[job.id] = job
    try:
        with (
            patch("app.pipeline.runner.download", side_effect=_download_with_tags),
            # Longer than both requests together: separation always is.
            patch("app.pipeline.runner._run_common", side_effect=_separation(0.8)),
        ):
            await run_pipeline(job, URL, tmp_path)
    finally:
        _jobs.pop(job.id, None)

    assert job.status == "done"
    assert job.artist == DT
    assert job.to_state()["artist"] == DT
    assert job.stage_timings["artist_wait"] < 0.1, "the answer was already waiting"
    meta = json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["artist"] == DT
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    [record] = [r for r in registry["jobs"] if r["id"] == job.id]
    assert record["artist"] == DT


async def test_a_lookup_that_hangs_does_not_hold_the_job(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(al, "_fetch_json", Wikidata(gate=gate))
    monkeypatch.setattr("app.pipeline.runner.ARTIST_LOOKUP_GRACE_SEC", 0.2)
    job = Job(id="abcdefabc211")
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
    assert job.artist is None
    meta = json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["artist"] is None


async def test_no_connection_finishes_the_job_without_a_band(tmp_path: Path):
    # conftest's stand-in for the network is offline.
    job = Job(id="abcdefabc212")
    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=_separation(0)),
    ):
        await run_pipeline(job, URL, tmp_path)
    assert job.status == "done"
    assert job.artist is None


async def test_an_upload_looks_its_band_up_from_the_tags_it_arrived_with(
    tmp_path: Path, monkeypatch
):
    wikidata = Wikidata()
    monkeypatch.setattr(al, "_fetch_json", wikidata)
    job = _tagged_job("abcdefabc213", source_url="local:Pull Me Under")
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
    assert job.artist == DT
    assert wikidata.asked[0]["search"] == "Dream Theater"


async def test_a_job_cancelled_while_the_lookup_is_out_gets_no_band(tmp_path: Path, monkeypatch):
    gate = threading.Event()
    wikidata = Wikidata(gate=gate)
    monkeypatch.setattr(al, "_fetch_json", wikidata)
    job = Job(id="abcdefabc214")

    def cancelled_mid_separation(job, source, job_dir):
        # Cancelled while the search is out, not before it was sent.
        assert wikidata.called.wait(5)
        job.cancel_requested = True
        gate.set()
        from app.core.models import JobCancelled

        raise JobCancelled()

    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=cancelled_mid_separation),
    ):
        await run_pipeline(job, URL, tmp_path)
    # Let the lookup thread run out: it must still write nothing.
    for thread in threading.enumerate():
        if thread.name == f"artist-{job.id}":
            thread.join(5)
    assert job.status == "cancelled"
    assert job.artist is None
    # The search was the last thing asked: the cancel stopped the second one.
    assert [p["action"] for p in wikidata.asked] == ["wbsearchentities"]


# ── after a restart ──


def test_the_band_comes_back_from_metadata_and_a_bad_one_does_not(tmp_path: Path):
    for job_id, artist, expected in (
        ("abcdefabc220", DT, DT),
        ("abcdefabc221", {"id": "../etc", "name": "x"}, None),
    ):
        job_dir = tmp_path / job_id
        (job_dir / "stems").mkdir(parents=True)
        (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
        (job_dir / "metadata.json").write_text(
            json.dumps({"title": "Pull Me Under", "artist": artist}), encoding="utf-8"
        )
        job = _recover_done_job(job_dir)
        assert job is not None
        assert job.artist == expected


def test_a_registry_record_with_a_bad_band_loads_without_it():
    job = Job.from_record({"id": "abcdefabc222", "artist": {"id": "nope", "name": "x"}})
    assert job.artist is None
    job = Job.from_record({"id": "abcdefabc223", "artist": DT})
    assert job.artist == DT
