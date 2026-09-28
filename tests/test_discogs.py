"""app/pipeline/discogs.py: the Discogs client, finding the right artist, and
what the artist box is given from it.

No network: conftest's _no_discogs_api keeps api.discogs.com offline and the
disk cache in a temporary directory; these tests stand in for _send with the
answers below, shaped as Discogs' API documentation and real answers give
them (https://www.discogs.com/developers). No real token is used anywhere:
TOKEN is made up.
"""

from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.pipeline import discogs, ratelimit

TOKEN = "FAKEtoken0123456789abcdefFAKEtoken012345"

# ── what Discogs answers ──

# GET /database/search?type=release&artist=NIHIL&track=Barro. Three artists
# called Nihil on Discogs; only one has a song called Barro.
SEARCH_BARRO = {
    "pagination": {"page": 1, "pages": 1, "per_page": 25, "items": 3, "urls": {}},
    "results": [
        {
            "id": 20001,
            "type": "release",
            "master_id": 3001,
            "title": "Nihil (5) - Lama",
            "year": "2021",
            "country": "Portugal",
            "format": ["Vinyl", "LP", "Album"],
            "uri": "/release/20001-Nihil-Lama",
            "resource_url": "https://api.discogs.com/releases/20001",
        },
        {
            "id": 20002,
            "type": "release",
            "master_id": 3001,
            "title": "Nihil (5) - Lama",
            "year": "2021",
            "format": ["CD", "Album"],
            "uri": "/release/20002-Nihil-Lama",
            "resource_url": "https://api.discogs.com/releases/20002",
        },
        {
            "id": 10001,
            "type": "release",
            "master_id": 0,
            "title": "Nihil (2) - Demo 1994",
            "year": "1994",
            "format": ["Cassette"],
            "uri": "/release/10001-Nihil-Demo-1994",
            "resource_url": "https://api.discogs.com/releases/10001",
        },
    ],
}

RELEASE_LAMA = {
    "id": 20001,
    "title": "Lama",
    "year": 2021,
    "master_id": 3001,
    "artists": [
        {
            "name": "Nihil (5)",
            "anv": "NIHIL",
            "join": "",
            "role": "",
            "id": 555501,
            "resource_url": "https://api.discogs.com/artists/555501",
        }
    ],
    "tracklist": [
        {"position": "A1", "type_": "track", "title": "Terra", "duration": "4:02"},
        {"position": "A2", "type_": "track", "title": "Barro", "duration": "3:28"},
        {"position": "B1", "type_": "track", "title": "Cinza", "duration": "5:10"},
    ],
}

# Another Nihil's demo, found by the same search, with no song called Barro.
RELEASE_DEMO = {
    "id": 10001,
    "title": "Demo 1994",
    "year": 1994,
    "artists": [{"name": "Nihil (2)", "anv": "", "id": 222202}],
    "tracklist": [
        {"position": "A", "type_": "track", "title": "Void"},
        {"position": "B", "type_": "track", "title": "Ashes"},
    ],
}

ARTIST_NIHIL = {
    "id": 555501,
    "name": "Nihil (5)",
    "realname": "",
    "resource_url": "https://api.discogs.com/artists/555501",
    "uri": "https://www.discogs.com/artist/555501-Nihil-5",
    "releases_url": "https://api.discogs.com/artists/555501/releases",
    "profile": (
        "Portuguese sludge band from Porto, formed in 2016 by [a=Rui Barros] "
        "and [a=Ana Lima (3)].\r\n\r\nTheir debut [m=3001] was released on "
        "[l=Raging Planet] in 2021. See [url=https://nihil.bandcamp.com]their "
        "Bandcamp[/url] for [b]more[/b].[a123456]"
    ),
    "urls": [
        "https://nihil.bandcamp.com/",
        "https://www.facebook.com/nihilporto",
        "https://www.instagram.com/nihil.porto/",
        "http://www.nihilband.pt",
        "https://www.youtube.com/@nihilporto",
        "https://en.wikipedia.org/wiki/Nihil",
        "javascript:alert(1)",
        "https://user:pw@evil.example/",
    ],
    "members": [
        {"id": 900001, "name": "Rui Barros", "active": True},
        {"id": 900002, "name": "Ana Lima (3)", "active": True},
        {"id": 900003, "name": "Pedro Sá", "active": False},
    ],
    "images": [{"type": "primary", "uri": "https://i.discogs.com/abc.jpg"}],
    "data_quality": "Needs Vote",
}

ARTIST_NIHIL_RELEASES = {
    "pagination": {"page": 1, "pages": 1, "per_page": 100, "items": 5},
    "releases": [
        {
            "id": 3001,
            "type": "master",
            "main_release": 20001,
            "title": "Lama",
            "year": 2021,
            "role": "Main",
            "artist": "Nihil (5)",
        },
        {
            "id": 20001,
            "type": "release",
            "title": "Lama",
            "year": 2021,
            "role": "Main",
            "artist": "Nihil (5)",
        },
        {
            "id": 19000,
            "type": "release",
            "title": "Barro",
            "year": 2019,
            "role": "Main",
            "format": "File, Single",
            "artist": "Nihil (5)",
        },
        {
            "id": 30000,
            "type": "release",
            "title": "Porto Sludge Vol. 1",
            "year": 2020,
            "role": "Appearance",
            "artist": "Various",
        },
        {"id": 31000, "type": "release", "title": "Untitled", "role": "Main"},
    ],
}


class FakeDiscogs:
    """Stands in for discogs._send: answers by path and query, records every
    request as it would have gone out."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        parts = urllib.parse.urlsplit(request.full_url)
        path = parts.path.lstrip("/")
        query = dict(urllib.parse.parse_qsl(parts.query))
        for key, answer in self.answers.items():
            want_path, _, want_query = key.partition("?")
            if want_path != path:
                continue
            want = dict(urllib.parse.parse_qsl(want_query))
            if all(query.get(k) == v for k, v in want.items()):
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    return answer(request)
                return json.dumps(answer).encode()
        raise _http_error(request.full_url, 404)

    def paths(self) -> list[str]:
        return [urllib.parse.urlsplit(r.full_url).path.lstrip("/") for r in self.requests]


def _http_error(url: str, code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(url, code, "error", headers, io.BytesIO(b"{}"))


NIHIL_ANSWERS = {
    "database/search?track=Barro": SEARCH_BARRO,
    "releases/20001": RELEASE_LAMA,
    "releases/10001": RELEASE_DEMO,
    "artists/555501": ARTIST_NIHIL,
    "artists/555501/releases": ARTIST_NIHIL_RELEASES,
}

NIHIL_IDENTITY = {
    "source": "tags",
    "title": "Barro",
    "artist": "NIHIL",
    "artist_mbids": [],
    "album": None,
}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """No real sleeps: the limiter and the retry wait cost nothing."""
    ratelimit.DISCOGS.reset()
    monkeypatch.setattr(ratelimit.DISCOGS, "interval", 0.0)
    monkeypatch.setattr(discogs, "_sleep", lambda s: None)
    yield
    ratelimit.DISCOGS.reset()


def _fake(monkeypatch, answers) -> FakeDiscogs:
    fake = FakeDiscogs(answers)
    monkeypatch.setattr(discogs, "_send", fake)
    return fake


# ── the client ──


def test_a_request_names_the_app_and_carries_the_token_only_in_a_header(monkeypatch):
    fake = _fake(monkeypatch, {"artists/555501": ARTIST_NIHIL})
    session = discogs._Session(token=TOKEN)
    assert session.get("artists/555501")["id"] == 555501
    (request,) = fake.requests
    assert request.get_header("Authorization") == f"Discogs token={TOKEN}"
    assert request.get_header("User-agent").startswith("StemDeck/")
    assert request.full_url.startswith("https://api.discogs.com/artists/555501")
    assert TOKEN not in request.full_url


def test_every_request_takes_its_turn_from_the_shared_limiter(monkeypatch):
    _fake(monkeypatch, {"artists/555501": ARTIST_NIHIL})
    turns = []
    monkeypatch.setattr(ratelimit.DISCOGS, "wait", lambda **kw: turns.append(kw))
    discogs._Session(token=TOKEN).get("artists/555501")
    assert len(turns) == 1


def test_the_shared_limiter_keeps_to_sixty_a_minute():
    from app.core.config import DISCOGS_MIN_INTERVAL_SEC

    assert DISCOGS_MIN_INTERVAL_SEC >= 1.0


def test_a_429_is_asked_again_after_what_retry_after_says(monkeypatch):
    calls = {"n": 0}

    def busy_then_ok(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(request.full_url, 429, retry_after="3")
        return json.dumps(ARTIST_NIHIL).encode()

    slept = []
    monkeypatch.setattr(discogs, "_sleep", slept.append)
    _fake(monkeypatch, {"artists/555501": busy_then_ok})
    assert discogs._Session(token=TOKEN).get("artists/555501")["id"] == 555501
    assert calls["n"] == 2
    assert sum(slept) == pytest.approx(3.0)


def test_a_retry_after_past_the_budget_gives_up_rather_than_waiting(monkeypatch):
    monkeypatch.setattr(discogs, "DISCOGS_REQUEST_BUDGET_SEC", 1.0)
    fake = _fake(monkeypatch, {"artists/1": _http_error("x", 429, retry_after="60")})
    session = discogs._Session(token=TOKEN)
    assert session.get("artists/1") is None
    assert session.failed
    assert len(fake.requests) == 1


def test_a_429_every_time_stops_after_the_retries(monkeypatch):
    fake = _fake(monkeypatch, {"artists/1": _http_error("x", 429, retry_after="1")})
    session = discogs._Session(token=TOKEN)
    assert session.get("artists/1") is None
    assert len(fake.requests) == discogs.DISCOGS_RETRIES + 1


def test_a_cancelled_lookup_stops_waiting_to_retry(monkeypatch):
    fake = _fake(monkeypatch, {"artists/1": _http_error("x", 429, retry_after="5")})
    state = {"cancel": False}

    def sleep(step):
        state["cancel"] = True

    monkeypatch.setattr(discogs, "_sleep", sleep)
    session = discogs._Session(token=TOKEN, cancelled=lambda: state["cancel"])
    assert session.get("artists/1") is None
    assert len(fake.requests) == 1


def test_a_cancelled_lookup_asks_nothing(monkeypatch):
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    answer = discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN, cancelled=lambda: True)
    assert answer is None
    assert fake.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        OSError("offline"),
        TimeoutError("slow"),
        ValueError("junk"),
        _http_error("x", 401),
        _http_error("x", 500),
    ],
)
def test_any_failure_is_nothing_found_and_never_raises(monkeypatch, failure):
    _fake(monkeypatch, {"database/search": failure, "artists/555501": failure})
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None


def test_an_answer_that_is_not_json_is_nothing(monkeypatch):
    _fake(monkeypatch, {"artists/1": lambda r: b"<html>"})
    assert discogs._Session(token=TOKEN).get("artists/1") is None


def test_an_oversized_answer_is_not_read(monkeypatch):
    monkeypatch.setattr(discogs, "DISCOGS_MAX_BYTES", 10)
    _fake(monkeypatch, {"artists/1": lambda r: b'{"id": 1, "name": "long enough"}'})
    assert discogs._Session(token=TOKEN).get("artists/1") is None


def test_answers_are_kept_on_disk_and_not_asked_for_twice(monkeypatch):
    fake = _fake(monkeypatch, {"artists/555501": ARTIST_NIHIL})
    discogs._Session(token=TOKEN).get("artists/555501")
    discogs._Session(token="another" + TOKEN).get("artists/555501")
    assert len(fake.requests) == 1
    kept = list(discogs.CACHE_DIR.glob("*.json"))
    assert len(kept) == 1
    # Named by a hash of the request: no name, no token.
    assert TOKEN not in kept[0].name and TOKEN not in kept[0].read_text(encoding="utf-8")


def test_a_stale_cache_entry_is_asked_for_again(monkeypatch):
    fake = _fake(monkeypatch, {"artists/555501": ARTIST_NIHIL})
    discogs._Session(token=TOKEN).get("artists/555501")
    (kept,) = discogs.CACHE_DIR.glob("*.json")
    kept.write_text(json.dumps({"at": 0, "data": ARTIST_NIHIL}), encoding="utf-8")
    discogs._Session(token=TOKEN).get("artists/555501")
    assert len(fake.requests) == 2


def test_the_token_is_never_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _fake(monkeypatch, {"database/search": _http_error("x", 401), "artists/1": OSError("x")})
    discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN)
    discogs._Session(token=TOKEN).get("artists/1")
    assert TOKEN not in caplog.text


# ── markup and fields ──


def test_profile_markup_becomes_plain_text():
    paragraphs = discogs.profile_paragraphs(ARTIST_NIHIL["profile"])
    assert paragraphs == [
        "Portuguese sludge band from Porto, formed in 2016 by Rui Barros and Ana Lima.",
        "Their debut was released on Raging Planet in 2021. See their Bandcamp for more.",
    ]


@pytest.mark.parametrize(
    ("raw", "plain"),
    [
        ("[b]Bold[/b] and [i]italic[/i]", "Bold and italic"),
        ("Label: [l=Some Label (2)]", "Label: Some Label"),
        ("[url]https://x.example[/url]", "https://x.example"),
        ("Signed to [l12345].", "Signed to."),
        ("A [r=123456] B [t=5] C", "A B C"),
        ("<script>x</script>", "<script>x</script>"),
    ],
)
def test_profile_markup_cases(raw, plain):
    assert discogs.profile_paragraphs(raw) == [plain]


def test_a_long_profile_is_cut_at_a_word():
    paragraphs = discogs.profile_paragraphs("word " * 1000)
    assert len("".join(paragraphs)) <= discogs._PROFILE_MAX_CHARS + 1
    assert paragraphs[-1].endswith(chr(0x2026))


def test_the_profile_shape():
    profile = discogs.artist_profile(ARTIST_NIHIL, ARTIST_NIHIL_RELEASES)
    assert profile["id"] == 555501
    assert profile["name"] == "Nihil"
    assert profile["url"] == "https://www.discogs.com/artist/555501-Nihil-5"
    assert profile["members"] == {"current": ["Rui Barros", "Ana Lima"], "former": ["Pedro Sá"]}
    assert profile["links"] == [
        {"kind": "website", "url": "http://www.nihilband.pt"},
        {"kind": "bandcamp", "url": "https://nihil.bandcamp.com/"},
        {"kind": "instagram", "url": "https://www.instagram.com/nihil.porto/"},
        {"kind": "facebook", "url": "https://www.facebook.com/nihilporto"},
        {"kind": "youtube", "url": "https://www.youtube.com/@nihilporto"},
    ]
    # The master over its own release, the Various appearance left out, the
    # undated one last.
    assert profile["releases"] == [
        {"year": "2019", "title": "Barro"},
        {"year": "2021", "title": "Lama"},
        {"year": "", "title": "Untitled"},
    ]
    # No images: the box's photos come from Wikimedia.
    assert "image" not in profile and "images" not in profile


def test_a_page_url_that_is_not_discogs_is_rebuilt_from_the_id():
    artist = {**ARTIST_NIHIL, "uri": "https://evil.example/artist/1"}
    assert discogs.artist_profile(artist, None)["url"] == "https://www.discogs.com/artist/555501"


@pytest.mark.parametrize(
    ("value", "url"),
    [
        ("https://band.example/", "https://band.example/"),
        ("www.band.example", "https://www.band.example"),
        ("javascript:alert(1)", ""),
        ("data:text/html,x", ""),
        ("https://user:pw@band.example/", ""),
        ("https:///band.example", ""),
        ("https://band.example/a b", ""),
        ("ftp://band.example/", ""),
    ],
)
def test_only_plain_web_links_are_kept(value, url):
    assert discogs.safe_url(value) == url


def test_a_real_name_the_same_as_the_name_is_not_repeated():
    artist = {"id": 7, "name": "Ana Lima (3)", "realname": "Ana Lima"}
    assert discogs.artist_profile(artist, None)["real_name"] == ""
    artist = {"id": 7, "name": "DJ Ana", "realname": "Ana Lima", "groups": [{"name": "Nihil (5)"}]}
    profile = discogs.artist_profile(artist, None)
    assert profile["real_name"] == "Ana Lima"
    assert profile["groups"] == ["Nihil"]


# ── names ──


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Nihil (5)", "Nihil"),
        ("Nihil", "Nihil"),
        ("NIHIL*", "NIHIL"),
        ("Take (2) Me", "Take (2) Me"),
    ],
)
def test_the_number_discogs_adds_is_disambiguation(name, shown):
    assert discogs.display_name(name) == shown


def test_names_compare_without_case_accents_or_number():
    assert discogs.name_key("Nihil (5)") == discogs.name_key("NIHIL")
    assert discogs.name_key("Sigur Rós") == discogs.name_key("Sigur Ros")
    assert discogs.name_key("周杰倫") == discogs.name_key("周杰伦")


# ── finding the artist ──


def test_nihil_is_found_by_the_release_that_has_barro(monkeypatch):
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    answer = discogs.artist_for_track(
        NIHIL_IDENTITY, {"artist": "NIHIL", "title": "Barro"}, None, TOKEN
    )
    assert answer["id"] == 555501
    assert answer["name"] == "Nihil"
    search = fake.requests[0]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(search.full_url).query))
    assert query["type"] == "release"
    assert query["artist"] == "NIHIL"
    assert query["track"] == "Barro"
    # One pressing of the master is enough: 20002 is not fetched.
    assert "releases/20002" not in fake.paths()


def test_a_same_name_artist_without_the_track_is_refused(monkeypatch):
    """The search found Nihil (2)'s demo too, and it has no Barro."""
    answers = {
        **NIHIL_ANSWERS,
        "database/search?track=Barro": {"results": [SEARCH_BARRO["results"][2]]},
    }
    fake = _fake(monkeypatch, answers)
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None
    assert not any(p.startswith("artists/") for p in fake.paths())


def test_a_bare_name_is_never_enough(monkeypatch):
    """No song and no album: nothing is searched and nothing is taken."""
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    identity = {**NIHIL_IDENTITY, "title": ""}
    assert discogs.artist_for_track(identity, {"artist": "NIHIL"}, None, TOKEN) is None
    assert fake.requests == []


def test_a_song_title_that_is_a_band_name_does_not_become_the_band(monkeypatch):
    """The release has "Barro" by a band called Barro: not NIHIL's."""
    release = {**RELEASE_LAMA, "artists": [{"name": "Barro", "id": 777}]}
    _fake(monkeypatch, {**NIHIL_ANSWERS, "releases/20001": release})
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None


def test_two_artists_with_the_name_and_the_song_are_no_answer(monkeypatch):
    other = {
        **RELEASE_DEMO,
        "tracklist": [{"type_": "track", "title": "Barro"}],
    }
    _fake(monkeypatch, {**NIHIL_ANSWERS, "releases/10001": other})
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None


def test_a_compilation_credits_the_track_artist(monkeypatch):
    compilation = {
        "id": 40001,
        "title": "Porto Sludge Vol. 1",
        "artists": [{"name": "Various", "id": 194}],
        "tracklist": [
            {
                "type_": "track",
                "title": "Barro (Demo)",
                "artists": [{"name": "Nihil (5)", "id": 555501}],
            },
            {"type_": "track", "title": "Other", "artists": [{"name": "Someone", "id": 1}]},
        ],
    }
    search = {"results": [{"id": 40001, "master_id": 0, "title": "Various - Porto Sludge Vol. 1"}]}
    _fake(
        monkeypatch,
        {**NIHIL_ANSWERS, "database/search?track=Barro": search, "releases/40001": compilation},
    )
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN)["id"] == 555501


def test_the_album_is_used_when_the_song_finds_nothing(monkeypatch):
    fake = _fake(
        monkeypatch,
        {
            "database/search?track=Barro": {"results": []},
            "database/search?release_title=Lama": SEARCH_BARRO,
            "releases/20001": RELEASE_LAMA,
            "releases/10001": RELEASE_DEMO,
            "artists/555501": ARTIST_NIHIL,
            "artists/555501/releases": ARTIST_NIHIL_RELEASES,
        },
    )
    identity = {**NIHIL_IDENTITY, "album": "Lama"}
    assert discogs.artist_for_track(identity, None, None, TOKEN)["id"] == 555501
    assert fake.paths().count("database/search") == 2


def test_the_musicbrainz_link_is_followed_without_searching(monkeypatch):
    mbid = "0b6e9d9e-1111-4222-8333-444455556666"
    asked = []

    def musicbrainz(path, params, *, cancelled=lambda: False):
        asked.append(path)
        return {
            "id": mbid,
            "name": "Nihil",
            "aliases": [],
            "relations": [
                {"type": "wikipedia", "url": {"resource": "https://pt.wikipedia.org/wiki/X"}},
                {
                    "type": "discogs",
                    "url": {"resource": "https://www.discogs.com/artist/555501-Nihil-5"},
                },
            ],
        }

    monkeypatch.setattr("app.pipeline.musicbrainz._fetch_json", musicbrainz)
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    identity = {**NIHIL_IDENTITY, "source": "acoustid", "artist_mbids": [mbid]}
    assert discogs.artist_for_track(identity, None, None, TOKEN)["id"] == 555501
    assert asked == [f"artist/{mbid}"]
    assert "database/search" not in fake.paths()
    # The MusicBrainz answer is cached, so the next lookup asks it nothing.
    discogs.forget()
    discogs.artist_for_track(identity, None, None, TOKEN)
    assert asked == [f"artist/{mbid}"]


def test_the_wikidata_band_names_its_discogs_artist(monkeypatch):
    def wikidata(params):
        assert params["ids"] == "Q999"
        return {
            "entities": {
                "Q999": {
                    "claims": {
                        "P1953": [
                            {"rank": "normal", "mainsnak": {"datavalue": {"value": "555501"}}}
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", wikidata)
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    band = {"id": "Q999", "name": "Nihil", "englishName": "Nihil"}
    assert (
        discogs.artist_for_track({**NIHIL_IDENTITY, "title": ""}, None, band, TOKEN)["id"] == 555501
    )
    assert "database/search" not in fake.paths()


def test_nothing_found_is_kept_for_a_while_and_a_failure_is_not(monkeypatch):
    fake = _fake(monkeypatch, {"database/search": {"results": []}})
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN) is None
    assert len(fake.requests) == 1

    discogs.forget()
    for path in list(discogs.CACHE_DIR.glob("*.json")):
        path.unlink()
    fake = _fake(monkeypatch, {"database/search": OSError("offline")})
    discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN)
    discogs.artist_for_track(NIHIL_IDENTITY, None, None, TOKEN)
    assert len(fake.requests) == 2


def test_no_token_asks_nothing(monkeypatch):
    fake = _fake(monkeypatch, NIHIL_ANSWERS)
    assert discogs.artist_for_track(NIHIL_IDENTITY, None, None, "") is None
    assert fake.requests == []
