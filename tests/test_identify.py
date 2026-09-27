"""Which recording a job is: AcoustID fingerprint, MusicBrainz, or the tags.

No network: AcoustID and MusicBrainz are answered by stubs standing in for
identify._acoustid_request and musicbrainz._fetch_json, Wikidata by one for
artist_lookup._fetch_json, and conftest makes every other test see all three
as offline. The one test that runs FFmpeg for real skips on a build without
the chromaprint muxer.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

import app.pipeline.artist_lookup as al
import app.pipeline.identify as ident
import app.pipeline.musicbrainz as mb
from app.core import settings as _settings
from app.core.models import Job, JobCancelled, clean_identity
from app.core.registry import _jobs, _recover_done_job
from app.pipeline.ratelimit import RateLimited, RateLimiter
from app.pipeline.runner import run_local_pipeline, run_pipeline
from tests.ffmpeg_probe import ffmpeg_available

# The real requests, kept before conftest swaps them for offline stand-ins.
_REAL_MB_FETCH = mb._fetch_json
_REAL_ACOUSTID = ident._acoustid_request

KEY = "Ab12Cd34Ef"
REC = "b1a9c0e9-d987-4042-ae91-78d6a3267d69"
REC_OTHER = "0b9e3a44-1a4f-4b8e-8f59-2e7a0b3f8d11"
QUEEN_MBID = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"
BOWIE_MBID = "5441c29d-3602-4898-b1a1-b77fa23b8e50"
OPERA_RG = "f89d8e8a-3c7f-3d4c-9a6a-0d0f0a1f2b3c"
GREATEST_RG = "2b2c4b6e-4f0e-3b0a-8e0d-9c3b5a1a7e21"
SINGLE_RG = "7e3b0e7d-6a1b-4b62-8c0e-11c2e3f4a5b6"
QUEEN = {"id": "Q15862", "name": "Queen", "englishName": "Queen"}
FINGERPRINT = "AQAA3UmUaEkSZSoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# Trimmed from MusicBrainz's real answer for Bohemian Rhapsody, with a
# compilation and a single beside the album it is from.
RECORDING = {
    "id": REC,
    "title": "Bohemian Rhapsody",
    "length": 354320,
    "artist-credit": [
        {"name": "Queen", "joinphrase": "", "artist": {"id": QUEEN_MBID, "name": "Queen"}}
    ],
    "releases": [
        {
            "id": "r1",
            "status": "Official",
            "date": "1981-10-26",
            "release-group": {
                "id": GREATEST_RG,
                "title": "Greatest Hits",
                "primary-type": "Album",
                "secondary-types": ["Compilation"],
            },
        },
        {
            "id": "r2",
            "status": "Official",
            "date": "1975-10-31",
            "release-group": {
                "id": SINGLE_RG,
                "title": "Bohemian Rhapsody",
                "primary-type": "Single",
                "secondary-types": [],
            },
        },
        {
            "id": "r3",
            "status": "Official",
            "date": "1975-11-21",
            "release-group": {
                "id": OPERA_RG,
                "title": "A Night at the Opera",
                "primary-type": "Album",
                "secondary-types": [],
            },
        },
    ],
}
ACOUSTID_ANSWER = {
    "status": "ok",
    "results": [
        {"id": "low", "score": 0.42, "recordings": [{"id": REC_OTHER, "title": "Other"}]},
        {
            "id": "a1",
            "score": 0.97,
            "recordings": [
                {"id": REC_OTHER, "duration": 300},
                {
                    "id": REC,
                    "title": "Bohemian Rhapsody",
                    "duration": 354,
                    "artists": [{"id": QUEEN_MBID, "name": "Queen"}],
                    "releasegroups": [
                        {
                            "id": OPERA_RG,
                            "title": "A Night at the Opera",
                            "type": "Album",
                        }
                    ],
                },
            ],
        },
    ],
}
ARTIST_RELS = {
    "id": QUEEN_MBID,
    "relations": [
        {"type": "discogs", "url": {"resource": "https://www.discogs.com/artist/81013"}},
        {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q15862"}},
    ],
}


def _wikidata_entity(mbid=QUEEN_MBID):
    return {
        "entities": {
            "Q15862": {
                "id": "Q15862",
                "labels": {"en": {"value": "Queen"}},
                "claims": {"P434": [{"mainsnak": {"datavalue": {"value": mbid}}}]},
            }
        }
    }


class MusicBrainz:
    """Stands in for musicbrainz._fetch_json: answers by path, records asks."""

    def __init__(self, *, search=None, fail=False, delay=0.0):
        self.asked: list[tuple[str, dict]] = []
        self.search = search if search is not None else {"recordings": []}
        self.fail = fail
        self.delay = delay

    def __call__(self, path, params, **_kwargs):
        self.asked.append((path, dict(params)))
        time.sleep(self.delay)
        if self.fail:
            raise OSError("MusicBrainz down")
        if path == f"recording/{REC}":
            return RECORDING
        if path == f"artist/{QUEEN_MBID}":
            return ARTIST_RELS
        if path == "recording":
            return self.search
        raise AssertionError(f"unexpected MusicBrainz request {path}")


class AcoustID:
    def __init__(self, answer=ACOUSTID_ANSWER, delay=0.0):
        self.answer = answer
        self.asked: list[dict] = []
        self.delay = delay

    def __call__(self, form):
        self.asked.append(dict(form))
        time.sleep(self.delay)
        return self.answer


@pytest.fixture
def services(monkeypatch):
    """Every service answering, a key set, and a fingerprint that needs no
    FFmpeg: the parts tested elsewhere in this file."""
    musicbrainz = MusicBrainz()
    acoustid = AcoustID()
    wikidata_asked: list[dict] = []

    def wikidata(params):
        wikidata_asked.append(params)
        return _wikidata_entity()

    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    monkeypatch.setattr(ident, "_acoustid_request", acoustid)
    monkeypatch.setattr(al, "_fetch_json", wikidata)
    monkeypatch.setattr(ident, "fingerprint", lambda audio, **kw: FINGERPRINT)
    _settings.set_acoustid_api_key(KEY)
    return musicbrainz, acoustid, wikidata_asked


# ── the fingerprint ──


def test_the_fingerprint_command_reads_two_minutes_and_prints_fpcalcs_format():
    cmd = ident.fingerprint_command("ffmpeg", [Path("source.wav")], 120)
    assert cmd[:5] == ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    # -t before -i: an input option, so the rest of the file is never decoded.
    assert cmd[cmd.index("-i") - 2 : cmd.index("-i") + 2] == ["-t", "120", "-i", "source.wav"]
    assert cmd[-7:] == [
        "-f",
        "chromaprint",
        "-algorithm",
        "1",
        "-fp_format",
        "base64",
        "-",
    ]
    assert "-filter_complex" not in cmd


def test_stems_are_summed_back_into_the_mix():
    stems = [Path(f"{n}.wav") for n in ("vocals", "drums", "bass")]
    cmd = ident.fingerprint_command("ffmpeg", stems, 120)
    assert cmd.count("-t") == 3 and cmd.count("-i") == 3
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == "[0:a][1:a][2:a]amix=inputs=3:normalize=0[mix]"
    assert cmd[cmd.index("-map") + 1] == "[mix]"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (b"AQAA3UmUaEkSZSoAAAAAAAAAAAAAAAAA\n", "AQAA3UmUaEkSZSoAAAAAAAAAAAAAAAAA"),
        ("AQAB_-z9AAAAAAAAAAAAAAAAAAAA", "AQAB_-z9AAAAAAAAAAAAAAAAAAAA"),
        (b"", None),
        (b"AQ", None),
        (b"not a fingerprint at all", None),
        (None, None),
    ],
)
def test_parse_fingerprint(stdout, expected):
    assert ident.parse_fingerprint(stdout) == expected


def _chromaprint_available() -> bool:
    if not ffmpeg_available():
        return False
    from app.core.config import ffmpeg_executable

    out = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-h", "muxer=chromaprint"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return "chromaprint" in (out.stdout + out.stderr).lower() and "unknown" not in (
        out.stdout.lower()
    )


def _tone(path: Path, seconds: int, freq: int) -> None:
    from app.core.config import ffmpeg_executable

    subprocess.run(
        [
            ffmpeg_executable(),
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=d={seconds}:a=0.05:seed=7",
            "-filter_complex",
            "amix=inputs=2",
            "-ac",
            "2",
            "-y",
            str(path),
        ],
        check=True,
        timeout=60,
    )


def test_a_real_fingerprint_from_ffmpeg(tmp_path):
    """The bundled Windows build and Debian's (the Docker image) carry the
    muxer; the static Linux and macOS builds do not, and skip here."""
    if not _chromaprint_available():
        pytest.skip("this FFmpeg has no chromaprint muxer")
    one, two = tmp_path / "vocals.wav", tmp_path / "drums.wav"
    _tone(one, 8, 440)
    _tone(two, 8, 220)
    single = ident.fingerprint([one])
    summed = ident.fingerprint([one, two])
    # "AQ": algorithm 1, fpcalc's default, the one AcoustID's database uses.
    assert single and single.startswith("AQ")
    assert summed and summed.startswith("AQ")


class FakeProc:
    """A Popen that runs until killed, or answers at once when given output."""

    def __init__(self, *, returncode=None, stdout=b"", stderr=b""):
        self.killed = threading.Event()
        self.returncode = returncode
        self.stdout_data, self.stderr_data = stdout, stderr

    def communicate(self, timeout=None):
        if self.returncode is not None:
            return self.stdout_data, self.stderr_data
        if self.killed.wait(timeout if timeout is not None else 5):
            self.returncode = -9
            return b"", b""
        raise subprocess.TimeoutExpired("ffmpeg", timeout)

    def kill(self):
        self.killed.set()


def test_an_ffmpeg_without_chromaprint_is_asked_once(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    spawned = []

    def popen(cmd, **kwargs):
        spawned.append(cmd)
        return FakeProc(
            returncode=1, stderr=b"[out#0] Requested output format 'chromaprint' is not known."
        )

    monkeypatch.setattr(ident, "_NO_CHROMAPRINT", set())
    monkeypatch.setattr(ident.subprocess, "Popen", popen)
    # No fpcalc to fall back to (test_fpcalc.py covers the fallback).
    monkeypatch.setattr(ident.fpcalc, "fpcalc_executable", lambda: None)
    assert ident.fingerprint([source]) is None
    assert ident.fingerprint([source]) is None
    assert len(spawned) == 1, "the missing muxer is remembered"


def test_a_missing_source_is_not_fingerprinted(tmp_path, monkeypatch):
    monkeypatch.setattr(ident.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert ident.fingerprint([tmp_path / "gone.wav"]) is None


def test_release_source_stops_a_fingerprint_still_reading(tmp_path, monkeypatch):
    """The pipeline deletes a link's source once its stems exist; on Windows a
    file FFmpeg still has open cannot be deleted, which would fail the job."""
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    proc = FakeProc()
    monkeypatch.setattr(ident.subprocess, "Popen", lambda *a, **k: proc)
    answer = {}
    thread = threading.Thread(
        target=lambda: answer.setdefault("fp", ident.fingerprint([source], job_id="abcdef000001"))
    )
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    ident.release_source("abcdef000001")
    assert time.monotonic() - started < 2
    thread.join(5)
    assert proc.killed.is_set()
    assert answer["fp"] is None
    assert "abcdef000001" not in ident._RUNNING
    # Nothing running: nothing to do.
    ident.release_source("abcdef000001")


def test_a_cancelled_job_stops_its_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFF")
    proc = FakeProc()
    monkeypatch.setattr(ident.subprocess, "Popen", lambda *a, **k: proc)
    started = time.monotonic()
    # Cancelled once FFmpeg is running, not before it starts.
    assert ident.fingerprint([source], cancelled=lambda: time.monotonic() > started + 0.3) is None
    assert time.monotonic() - started < 2
    assert proc.killed.is_set()


# ── AcoustID ──


def test_the_best_match_is_the_named_recording_nearest_the_length():
    score, rec = ident.best_acoustid_match(ACOUSTID_ANSWER, 355.0)
    assert score == 0.97
    assert rec["id"] == REC


def test_a_match_below_the_threshold_is_no_match():
    answer = json.loads(json.dumps(ACOUSTID_ANSWER))
    answer["results"][1]["score"] = 0.79
    assert ident.best_acoustid_match(answer, 355.0) is None
    assert ident.best_acoustid_match(answer, 355.0, min_score=0.5) is not None


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "error", "error": {"code": 4, "message": "invalid API key"}},
        {"status": "ok", "results": []},
        # Known fingerprint, linked to no recording.
        {"status": "ok", "results": [{"id": "x", "score": 0.99}]},
        {"status": "ok", "results": [{"id": "x", "score": 0.99, "recordings": [{"id": "../x"}]}]},
        None,
        "nonsense",
    ],
)
def test_answers_that_name_nothing(answer):
    assert ident.best_acoustid_match(answer, 355.0) is None


def test_the_acoustid_request_posts_the_key_and_never_puts_it_in_a_url(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout, context):
        seen["url"] = request.full_url
        seen["body"] = request.data.decode("ascii")
        seen["agent"] = request.get_header("User-agent")
        return Response(b'{"status": "ok", "results": []}')

    monkeypatch.setattr(ident.ratelimit.ACOUSTID, "wait", lambda: None)
    monkeypatch.setattr(ident.urllib.request, "urlopen", urlopen)
    answer = _REAL_ACOUSTID({"client": KEY, "fingerprint": FINGERPRINT, "duration": "354"})
    assert answer == {"status": "ok", "results": []}
    assert seen["url"] == "https://api.acoustid.org/v2/lookup"
    assert KEY not in seen["url"]
    assert f"client={KEY}" in seen["body"]
    assert "StemDeck" in seen["agent"]


def test_a_refused_acoustid_lookup_says_why_without_the_key(monkeypatch):
    def urlopen(request, timeout, context):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"status": "error", "error": {"code": 4, "message": "invalid API key"}}'),
        )

    monkeypatch.setattr(ident.ratelimit.ACOUSTID, "wait", lambda: None)
    monkeypatch.setattr(ident.urllib.request, "urlopen", urlopen)
    with pytest.raises(OSError) as err:
        _REAL_ACOUSTID({"client": KEY, "fingerprint": FINGERPRINT})
    assert "invalid API key" in str(err.value)
    assert KEY not in str(err.value)


# ── MusicBrainz ──


def test_a_recording_becomes_an_identity_from_its_album():
    identity = mb.identity_from_recording(RECORDING, source="acoustid", score=0.97)
    assert identity == {
        "source": "acoustid",
        "score": 0.97,
        "recording_mbid": REC,
        "title": "Bohemian Rhapsody",
        "artist": "Queen",
        "artist_mbids": [QUEEN_MBID],
        # The studio album, not the earlier single or the compilation.
        "album": "A Night at the Opera",
        "release_group_mbid": OPERA_RG,
        "release_group_type": "Album",
        "secondary_types": [],
        # A search answer has no first-release-date: the album's own release.
        "year": 1975,
        "duration": 354.32,
    }


def test_the_year_is_the_release_groups_first_release():
    group = {"id": OPERA_RG, "title": "Wicked", "first-release-date": "2003-12-16"}
    # A lookup carries the group's own date, which wins over a later reissue.
    releases = [{"date": "2013-10-29", "release-group": group}]
    assert mb.release_group_year(releases, group) == 2003
    # A search leaves it out: the earliest of that group's releases, and only
    # that group's, whatever form the date takes.
    bare = {"id": OPERA_RG}
    releases = [
        {"date": "1981", "release-group": {"id": GREATEST_RG}},
        {"date": "2004-03", "release-group": bare},
        {"date": "2003-12-16", "release-group": bare},
        {"date": "", "release-group": bare},
        {"release-group": bare},
        "junk",
    ]
    assert mb.release_group_year(releases, bare) == 2003
    # Nothing dated, no group, or no releases at all: no year.
    assert mb.release_group_year([{"release-group": bare}], bare) is None
    assert mb.release_group_year(releases, {}) is None
    assert mb.release_group_year(None, {"id": OPERA_RG, "first-release-date": "?"}) is None


def test_an_identity_year_is_kept_only_as_a_plausible_year():
    base = {"source": "musicbrainz", "title": "Song", "artist": "Queen"}
    assert clean_identity({**base, "year": 1975})["year"] == 1975
    for bad in ("1975", True, 1200, 12345, 1975.0, None):
        assert clean_identity({**base, "year": bad})["year"] is None
    # A record from before years were kept reads back with none.
    assert clean_identity(base)["year"] is None


def test_a_recording_only_on_a_soundtrack_keeps_the_soundtrack():
    recording = {
        "id": REC,
        "title": "Defying Gravity",
        "artist-credit": [
            {"name": "Idina Menzel", "joinphrase": " & ", "artist": {"id": QUEEN_MBID}},
            {"name": "Kristin Chenoweth", "artist": {"id": BOWIE_MBID}},
        ],
        "releases": [
            {
                "status": "Official",
                "release-group": {
                    "id": OPERA_RG,
                    "title": "Wicked (Original Broadway Cast Recording)",
                    "primary-type": "Album",
                    "secondary-types": ["Soundtrack"],
                },
            }
        ],
    }
    identity = mb.identity_from_recording(
        recording, source="musicbrainz", score=1.0, fallback_duration=342.0
    )
    assert identity["artist"] == "Idina Menzel & Kristin Chenoweth"
    assert identity["artist_mbids"] == [QUEEN_MBID, BOWIE_MBID]
    assert identity["secondary_types"] == ["Soundtrack"]
    assert identity["release_group_type"] == "Album"
    assert identity["duration"] == 342.0


def test_the_real_request_names_stemdeck_and_waits_its_turn(monkeypatch):
    seen = {"waited": 0}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout, context):
        seen["url"] = request.full_url
        seen["agent"] = request.get_header("User-agent")
        return Response(b"{}")

    def wait():
        seen["waited"] += 1

    monkeypatch.setattr(mb.ratelimit.MUSICBRAINZ, "wait", wait)
    monkeypatch.setattr(mb.urllib.request, "urlopen", urlopen)
    assert _REAL_MB_FETCH(f"recording/{REC}", {"inc": "artist-credits"}) == {}
    assert seen["waited"] == 1
    assert seen["url"].startswith(f"https://musicbrainz.org/ws/2/recording/{REC}?")
    assert "fmt=json" in seen["url"]
    assert seen["agent"].startswith("StemDeck/") and "github.com" in seen["agent"]


def test_lookups_are_cached_on_disk_per_mbid(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    assert mb.lookup_recording(REC) == RECORDING
    assert mb.lookup_recording(REC) == RECORDING
    assert mb.artist_wikidata_id(QUEEN_MBID) == "Q15862"
    assert mb.artist_wikidata_id(QUEEN_MBID) == "Q15862"
    assert [path for path, _ in musicbrainz.asked] == [f"recording/{REC}", f"artist/{QUEEN_MBID}"]
    assert (mb.CACHE_DIR / f"recording-{REC}.json").is_file()


def test_a_stale_cache_entry_is_asked_again(monkeypatch):
    musicbrainz = MusicBrainz()
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    mb.cache_put("recording", REC, {"id": REC, "title": "old"})
    kept = json.loads((mb.CACHE_DIR / f"recording-{REC}.json").read_text(encoding="utf-8"))
    kept["at"] -= mb.MUSICBRAINZ_CACHE_TTL_SEC + 1
    (mb.CACHE_DIR / f"recording-{REC}.json").write_text(json.dumps(kept), encoding="utf-8")
    assert mb.lookup_recording(REC)["title"] == "Bohemian Rhapsody"
    assert len(musicbrainz.asked) == 1


def test_the_cache_never_leaves_its_directory():
    assert mb._cache_path("recording", "../../etc/passwd") is None
    assert mb._cache_path("anything", REC) is None
    mb.cache_put("recording", "../escape", {"x": 1})
    assert mb.cache_get("recording", "../escape") is None


def test_a_wikidata_link_that_is_not_one_is_ignored(monkeypatch):
    rels = {"relations": [{"type": "wikidata", "url": {"resource": "https://evil.example/Q1"}}]}
    monkeypatch.setattr(mb, "_fetch_json", lambda path, params: rels)
    assert mb.artist_wikidata_id(QUEEN_MBID) is None


def _search_hit(**over):
    hit = {
        "id": REC,
        "score": 100,
        "title": "Bohemian Rhapsody",
        "length": 354320,
        "artist-credit": [{"name": "Queen", "artist": {"id": QUEEN_MBID, "name": "Queen"}}],
        "releases": RECORDING["releases"],
    }
    hit.update(over)
    return {"recordings": [hit]}


def test_a_confident_search_is_kept(monkeypatch):
    musicbrainz = MusicBrainz(search=_search_hit())
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = mb.search_recording("Queen", "Bohemian Rhapsody", 356.0)
    assert identity["source"] == "musicbrainz"
    assert identity["score"] == 1.0
    assert identity["recording_mbid"] == REC
    assert identity["album"] == "A Night at the Opera"
    [(path, params)] = musicbrainz.asked
    assert path == "recording"
    assert params["query"] == (
        'recording:"Bohemian Rhapsody" AND artist:"Queen"'
        " AND dur:[352000 TO 360000] AND status:official"
    )


def test_of_many_recordings_of_the_song_the_studio_one_is_kept(monkeypatch):
    """What MusicBrainz really answers for a popular song: every hit scores
    100 and matches by name and length, and most are live cuts, karaoke or
    compilation tracks. The original is the one with no disambiguation, on
    an album of its own, released most often."""

    def hit(rec_id, disambiguation, secondary, releases, length):
        group = {"id": OPERA_RG, "title": "An Album", "primary-type": "Album"}
        group["secondary-types"] = secondary
        return {
            "id": rec_id,
            "score": 100 if releases < 50 else 99,
            "title": "Bohemian Rhapsody",
            "length": length,
            "disambiguation": disambiguation,
            "artist-credit": [{"name": "Queen", "artist": {"id": QUEEN_MBID, "name": "Queen"}}],
            "releases": [{"status": "Official", "release-group": group}] * releases,
        }

    canonical = "11111111-1111-4111-8111-111111111111"
    hits = [
        hit("22222222-2222-4222-8222-222222222222", "karaoke", ["Compilation"], 1, 355000),
        hit("33333333-3333-4333-8333-333333333333", "", ["Compilation"], 1, 355000),
        hit("44444444-4444-4444-8444-444444444444", "live, 1986", [], 3, 355000),
        hit("55555555-5555-4555-8555-555555555555", "", [], 6, 354000),
        hit(canonical, "", [], 353, 355106),
    ]
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(search={"recordings": hits}))
    identity = mb.search_recording("Queen", "Bohemian Rhapsody", 355.0)
    assert identity["recording_mbid"] == canonical
    assert identity["score"] == 0.99


@pytest.mark.parametrize(
    ("hit", "duration"),
    [
        (_search_hit(score=80), 354.0),  # MusicBrainz is not sure
        (_search_hit(), 340.0),  # a different cut: 14 s off
        (_search_hit(length=None), 354.0),  # nothing to check the length by
        (_search_hit(title="Bohemian Rhapsody Live"), 354.0),
        (
            _search_hit(
                **{"artist-credit": [{"name": "Queen Tribute", "artist": {"id": QUEEN_MBID}}]}
            ),
            354.0,
        ),
        (_search_hit(), None),  # the track's own length unknown
    ],
)
def test_a_search_that_is_not_unmistakable_is_not_kept(monkeypatch, hit, duration):
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(search=hit))
    assert mb.search_recording("Queen", "Bohemian Rhapsody", duration) is None


def test_a_title_cannot_change_the_query():
    assert mb._lucene_phrase('Say "Hi" \\ AND artist:x') == '"Say \\"Hi\\" \\\\ AND artist:x"'


# ── the rate limit ──


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_one_request_a_second():
    clock = Clock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(4):
        limiter.wait()
        times.append(clock.now)
    assert [round(t - times[0], 3) for t in times] == [0, 1, 2, 3]


def test_a_turn_too_far_off_is_not_taken():
    clock = Clock()
    limiter = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    limiter.wait()
    limiter._next = clock.now + 30
    with pytest.raises(RateLimited):
        limiter.wait(max_wait=5)
    assert limiter._next == clock.now + 30, "a refused turn reserves nothing"


def test_a_cancelled_wait_stops_waiting():
    clock = Clock()
    limiter = RateLimiter(10.0, clock=clock, sleep=clock.sleep)
    limiter.wait()
    with pytest.raises(RateLimited):
        limiter.wait(cancelled=lambda: True)


def test_threads_take_turns():
    limiter = RateLimiter(0.05)
    stamps: list[float] = []
    lock = threading.Lock()

    def ask():
        limiter.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=ask) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert len(stamps) == 5
    assert min(gaps) >= 0.04


def test_every_musicbrainz_caller_shares_one_limiter():
    from app.pipeline import ratelimit

    assert ratelimit.MUSICBRAINZ.interval == 1.0
    assert ratelimit.ACOUSTID.interval <= 1 / 3 + 0.01
    assert mb.ratelimit is ratelimit


# ── the whole answer ──


def test_a_fingerprint_match_is_described_by_musicbrainz(services):
    musicbrainz, acoustid, _ = services
    identity = ident.identify(
        tags={"artist": "Queen"},
        title="Queen - Bohemian Rhapsody (Official Video)",
        duration=355.0,
        audio=[Path("source.wav")],
        api_key=KEY,
    )
    assert identity["source"] == "acoustid"
    assert identity["score"] == 0.97
    assert identity["album"] == "A Night at the Opera"
    [form] = acoustid.asked
    assert form == {
        "client": KEY,
        "format": "json",
        "meta": "recordings releasegroups compress",
        "duration": "355",
        "fingerprint": FINGERPRINT,
    }
    assert [p for p, _ in musicbrainz.asked] == [f"recording/{REC}"]


def test_musicbrainz_down_keeps_acoustids_own_description(services, monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz(fail=True))
    identity = ident.identify(
        tags=None, title="x", duration=355.0, audio=[Path("s.wav")], api_key=KEY
    )
    assert identity["source"] == "acoustid"
    assert identity["recording_mbid"] == REC
    assert identity["artist_mbids"] == [QUEEN_MBID]
    assert identity["album"] == "A Night at the Opera"
    assert identity["duration"] == 354.0


def test_no_key_searches_by_the_tags_and_never_fingerprints(monkeypatch):
    monkeypatch.setattr(ident, "fingerprint", lambda *a, **k: pytest.fail("fingerprinted"))
    monkeypatch.setattr(ident, "_acoustid_request", lambda f: pytest.fail("asked AcoustID"))
    musicbrainz = MusicBrainz(search=_search_hit())
    monkeypatch.setattr(mb, "_fetch_json", musicbrainz)
    identity = ident.identify(
        tags={"artist": "Queen"},
        title="Queen - Bohemian Rhapsody (Official Video)",
        duration=356.0,
        audio=[Path("source.wav")],
        api_key=None,
    )
    assert identity["source"] == "musicbrainz"
    # The song's name cleaned out of the video title, the artist off its front.
    assert musicbrainz.asked[0][1]["query"].startswith(
        'recording:"Bohemian Rhapsody" AND artist:"Queen" AND dur:'
    )


def test_no_match_anywhere_leaves_the_tags(monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", MusicBrainz())
    identity = ident.identify(
        tags={"artist": "Queen feat. Nobody", "title": "Some Song", "album": "Some Album"},
        title="x",
        duration=200.0,
        audio=[],
        api_key=None,
    )
    assert identity == clean_identity(
        {
            "source": "tags",
            "score": 0.0,
            "title": "Some Song",
            "artist": "Queen feat. Nobody",
            "album": "Some Album",
            "duration": 200.0,
        }
    )


def test_nothing_to_go_on_is_no_identity():
    assert ident.identify(tags=None, title="x", duration=1.0, audio=[], api_key=None) is None
    assert (
        ident.identify(tags={"title": "Song"}, title="x", duration=1.0, audio=[], api_key=None)
        is None
    )


# ── the band, through the identity ──


def test_the_band_is_found_through_the_artist_mbid(services):
    musicbrainz, _, wikidata_asked = services
    identity = mb.identity_from_recording(RECORDING, source="acoustid", score=0.97)
    assert ident.find_band_for(identity, "Queen") == QUEEN
    assert [p for p, _ in musicbrainz.asked] == [f"artist/{QUEEN_MBID}"]
    # One entity fetch by id, no name search.
    assert [p["action"] for p in wikidata_asked] == ["wbgetentities"]
    assert wikidata_asked[0]["ids"] == "Q15862"


def test_a_wikidata_item_naming_another_artist_falls_back_to_the_name(services, monkeypatch):
    asked = []

    def wikidata(params):
        asked.append(params["action"])
        if params["action"] == "wbgetentities" and params["ids"] == "Q15862":
            return _wikidata_entity(mbid=BOWIE_MBID)  # a stale link
        if params["action"] == "wbsearchentities":
            return {"search": [{"id": "Q15862"}]}
        return {"entities": {"Q15862": _wikidata_entity()["entities"]["Q15862"]}}

    monkeypatch.setattr(al, "_fetch_json", wikidata)
    identity = mb.identity_from_recording(RECORDING, source="acoustid", score=0.97)
    assert ident.find_band_for(identity, "Queen") == QUEEN
    assert asked == ["wbgetentities", "wbsearchentities", "wbgetentities"]


def test_a_tags_identity_uses_the_name_search(services):
    _, _, wikidata_asked = services
    identity = clean_identity({"source": "tags", "title": "Song", "artist": "Queen"})
    ident.find_band_for(identity, None)
    assert wikidata_asked[0]["action"] == "wbsearchentities"


# ── in a thread beside the pipeline ──


def test_identity_and_band_are_kept_once_the_pipeline_finishes(services):
    job = Job(id="abcdef000010", audio_tags={"artist": "Queen"}, duration_sec=355.0)
    lookup = ident.IdentifyLookup.start(job, [Path("source.wav")])
    assert lookup is not None
    assert lookup.wait_identity(5)["recording_mbid"] == REC
    assert job.identity is None, "only finish() writes, from the pipeline's thread"
    lookup.finish(job, 5)
    assert job.identity["source"] == "acoustid"
    assert job.artist == QUEEN


def test_a_cancelled_job_gets_nothing(services, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ident, "fingerprint", lambda audio, **kw: gate.wait(5) and FINGERPRINT)
    job = Job(id="abcdef000011", audio_tags={"artist": "Queen"}, duration_sec=355.0)
    lookup = ident.IdentifyLookup.start(job, [Path("source.wav")])
    job.cancel_requested = True
    gate.set()
    lookup.finish(job, 5)
    assert job.identity is None
    assert job.artist is None


def test_an_identity_in_time_is_kept_while_the_band_is_still_out(services, monkeypatch):
    gate = threading.Event()

    def slow_wikidata(params):
        gate.wait(5)
        return _wikidata_entity()

    monkeypatch.setattr(al, "_fetch_json", slow_wikidata)
    job = Job(id="abcdef000012", audio_tags={"artist": "Queen"}, duration_sec=355.0)
    lookup = ident.IdentifyLookup.start(job, [Path("source.wav")])
    assert lookup.wait_identity(5) is not None
    lookup.finish(job, 0.1)
    gate.set()
    lookup._thread.join(5)
    assert job.identity["recording_mbid"] == REC
    assert job.artist is None, "an answer after finish() goes nowhere"


def test_nothing_to_find_starts_nothing():
    # No key and no artist tag.
    assert ident.IdentifyLookup.start(Job(id="abcdef000013"), [Path("s.wav")]) is None
    assert (
        ident.IdentifyLookup.start(
            Job(id="abcdef000014", audio_tags={"title": "Song"}), [Path("s.wav")]
        )
        is None
    )
    # A re-split inherits both.
    known = clean_identity({"source": "tags", "title": "Song", "artist": "Queen"})
    job = Job(id="abcdef000015", audio_tags={"artist": "Queen"}, identity=known, artist=QUEEN)
    assert ident.IdentifyLookup.start(job, [Path("s.wav")]) is None


def test_a_key_alone_is_enough_to_start(services):
    job = Job(id="abcdef000016", duration_sec=355.0)
    lookup = ident.IdentifyLookup.start(job, [Path("s.wav")])
    assert lookup is not None
    lookup.finish(job, 5)
    assert job.identity["title"] == "Bohemian Rhapsody"


# ── the pipeline ──

URL = "https://www.youtube.com/watch?v=fJ9rUzIMcZQ"


def _download_with_tags(job, url, job_dir):
    job.audio_tags = {"artist": "Queen", "title": "Bohemian Rhapsody"}
    job.duration_sec = 355.0
    return job_dir / "source.wav"


def _separation(seconds: float):
    def run_common(job, source, job_dir):
        (job_dir / "stems").mkdir(parents=True, exist_ok=True)
        time.sleep(seconds)

    return run_common


async def test_a_link_is_identified_beside_separation_at_no_cost(services, tmp_path, monkeypatch):
    """Every service answers slowly, and all of it is done while the job
    separates: the job finishes with its identity and band, in its state, the
    registry and metadata.json, and waiting for them cost nothing."""
    musicbrainz, acoustid, _ = services
    musicbrainz.delay = acoustid.delay = 0.1
    fingerprinted = []

    def slow_fingerprint(audio, **kw):
        fingerprinted.append(audio)
        time.sleep(0.1)
        return FINGERPRINT

    monkeypatch.setattr(ident, "fingerprint", slow_fingerprint)
    job = Job(id="abcdef000020")
    _jobs[job.id] = job
    started = time.monotonic()
    try:
        with (
            patch("app.pipeline.runner.download", side_effect=_download_with_tags),
            patch("app.pipeline.runner._run_common", side_effect=_separation(1.0)),
        ):
            await run_pipeline(job, URL, tmp_path)
    finally:
        _jobs.pop(job.id, None)
    elapsed = time.monotonic() - started

    assert job.status == "done"
    assert fingerprinted == [[tmp_path / job.id / "source.wav"]]
    assert job.identity["recording_mbid"] == REC
    assert job.artist == QUEEN
    assert job.stage_timings["identify_wait"] < 0.1, "the answer was already waiting"
    assert elapsed < 1.9, "identification ran beside separation, not after it"
    assert job.to_state()["identity"] == job.identity
    meta = json.loads((tmp_path / job.id / "metadata.json").read_text(encoding="utf-8"))
    assert meta["identity"] == job.identity
    registry = json.loads((tmp_path / "registry.json").read_text(encoding="utf-8"))
    [record] = [r for r in registry["jobs"] if r["id"] == job.id]
    assert record["identity"] == job.identity


async def test_an_upload_is_fingerprinted_from_its_prepared_source(services, tmp_path):
    job = Job(
        id="abcdef000021",
        audio_tags={"artist": "Queen"},
        duration_sec=355.0,
        source_url="local:Queen - Bohemian Rhapsody",
    )
    (tmp_path / job.id).mkdir()
    with (
        patch("app.pipeline.runner._prepare_local_source", side_effect=lambda j, s, d: s),
        patch("app.pipeline.runner._run_common", side_effect=_separation(0.3)),
    ):
        await run_local_pipeline(job, tmp_path / job.id / "source.wav", tmp_path)
    assert job.status == "done"
    assert job.identity["source"] == "acoustid"
    assert job.artist == QUEEN


async def test_identification_that_hangs_does_not_hold_the_job(services, tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(ident, "fingerprint", lambda audio, **kw: gate.wait(10) and FINGERPRINT)
    monkeypatch.setattr("app.pipeline.runner.IDENTIFY_GRACE_SEC", 0.2)
    # The lyrics lookup waits for the identity too, on its own grace.
    monkeypatch.setattr("app.pipeline.runner.LYRICS_LOOKUP_GRACE_SEC", 0.2)
    job = Job(id="abcdef000022")
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
    assert job.identity is None
    assert job.artist is None


async def test_a_job_cancelled_while_identifying_is_never_written(services, tmp_path, monkeypatch):
    gate = threading.Event()
    called = threading.Event()

    def fingerprint(audio, **kw):
        called.set()
        gate.wait(5)
        return FINGERPRINT

    monkeypatch.setattr(ident, "fingerprint", fingerprint)
    job = Job(id="abcdef000023")

    def cancelled_mid_separation(job, source, job_dir):
        assert called.wait(5)
        job.cancel_requested = True
        gate.set()
        raise JobCancelled()

    with (
        patch("app.pipeline.runner.download", side_effect=_download_with_tags),
        patch("app.pipeline.runner._run_common", side_effect=cancelled_mid_separation),
    ):
        await run_pipeline(job, URL, tmp_path)
    for thread in threading.enumerate():
        if thread.name == f"identify-{job.id}":
            thread.join(5)
    assert job.status == "cancelled"
    assert job.identity is None
    assert job.artist is None


# ── on disk ──


def test_identity_comes_back_from_metadata_and_a_bad_one_does_not(tmp_path: Path):
    good = mb.identity_from_recording(RECORDING, source="acoustid", score=0.97)
    for job_id, identity, expected in (
        ("abcdef000030", good, good),
        ("abcdef000031", {"source": "made-up", "title": "x", "artist": "y"}, None),
        ("abcdef000032", {"source": "tags", "title": "x"}, None),
    ):
        job_dir = tmp_path / job_id
        (job_dir / "stems").mkdir(parents=True)
        (job_dir / "stems" / "vocals.wav").write_bytes(b"RIFF")
        (job_dir / "metadata.json").write_text(
            json.dumps({"title": "x", "identity": identity}), encoding="utf-8"
        )
        job = _recover_done_job(job_dir)
        assert job is not None
        assert job.identity == expected


def test_a_registry_record_is_cleaned():
    bad = {
        "source": "acoustid",
        "score": 7,
        "recording_mbid": "../../etc",
        "title": " Song ",
        "artist": "Queen",
        "artist_mbids": [QUEEN_MBID, "nope", 3],
        "secondary_types": ["Soundtrack", 5],
        "duration": float("nan"),
    }
    job = Job.from_record({"id": "abcdef000033", "identity": bad})
    assert job.identity["score"] == 1.0
    assert job.identity["recording_mbid"] is None
    assert job.identity["title"] == "Song"
    assert job.identity["artist_mbids"] == [QUEEN_MBID]
    assert job.identity["secondary_types"] == ["Soundtrack"]
    assert job.identity["duration"] is None
    assert Job.from_record({"id": "abcdef000034", "identity": "junk"}).identity is None
