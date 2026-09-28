"""The Discogs token setting (GET/POST /api/settings, discogs_auth).

The token is the user's: the API says only whether one is set and its last
two characters, a rejected one is never echoed back, and none of it reaches
the log or a URL. It is tried once against Discogs when it is saved: a 401
refuses it, anything that is not an answer keeps it on trust, and the Save
is never held past a bound. Nothing reaches api.discogs.com: conftest answers
every check as offline, and these tests stub the request or urlopen.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.pipeline.discogs_auth as da
from app.core import settings as _settings

# The real request, kept before conftest swaps it for an offline stand-in.
_REAL_ASK = da._ask_identity

TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789XyQz"  # 40, the real length
OTHER = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0PpOoNnMmLl"


@pytest.fixture
def client():
    with patch("app.api.jobs.jobqueue.enqueue", lambda job_id: None):
        from app.main import app

        with TestClient(app) as c:
            yield c


def _discogs_answers(monkeypatch, status):
    sent = []

    def answer(token):
        sent.append(token)
        return status

    monkeypatch.setattr(da, "_ask_identity", answer)
    return sent


# ── the setting ──


def test_unset_by_default(client):
    data = client.get("/api/settings").json()
    assert data["discogs_token_set"] is False
    assert data["discogs_token_tail"] is None
    assert "discogs_token" not in data


def test_the_token_is_saved_and_never_handed_back(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = client.post("/api/settings", json={"discogs_token": f"  {TOKEN}  "})
    assert r.status_code == 200
    assert r.json()["discogs_token_set"] is True
    assert r.json()["discogs_token_tail"] == "Qz"
    assert TOKEN not in r.text
    got = client.get("/api/settings")
    assert TOKEN not in got.text
    assert got.json()["discogs_token_tail"] == TOKEN[-2:]
    assert _settings.get_discogs_token() == TOKEN
    assert TOKEN not in caplog.text


def test_the_token_survives_a_restart():
    _settings.set_discogs_token(TOKEN)
    _settings._state = None  # the next read comes from settings.json
    assert _settings.get_discogs_token() == TOKEN


def test_the_acoustid_key_is_untouched_by_the_token(client):
    _settings.set_acoustid_api_key("Zx9Yw8Vu7T")
    client.post("/api/settings", json={"discogs_token": TOKEN})
    client.post("/api/settings", json={"discogs_token": ""})
    assert _settings.get_acoustid_api_key() == "Zx9Yw8Vu7T"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_clearing_the_token(client, value):
    _settings.set_discogs_token(TOKEN)
    r = client.post("/api/settings", json={"discogs_token": value})
    assert r.status_code == 200
    assert r.json()["discogs_token_set"] is False
    assert _settings.get_discogs_token() is None


@pytest.mark.parametrize(
    "value",
    [
        "tooShort123",
        "x" * 81,
        "has a space in the middle of it abcdefgh",
        "dash-is-not-in-a-discogs-token-abcdefgh",
        "<script>alert(1)</script>abcdefghijklmn",
        12345,
        ["a"],
        {"t": TOKEN},
    ],
)
def test_a_token_that_cannot_be_one_is_refused_without_echoing_it(client, monkeypatch, value):
    sent = _discogs_answers(monkeypatch, 200)
    _settings.set_discogs_token(TOKEN)
    r = client.post("/api/settings", json={"discogs_token": value})
    assert r.status_code == 422
    assert r.json()["detail"] == "invalid Discogs token"
    if isinstance(value, str):
        assert value not in r.text
    assert _settings.get_discogs_token() == TOKEN, "the old token stays"
    assert sent == [], "a malformed token is never sent to Discogs"


# ── the token is tried when it is saved ──


def test_a_token_discogs_refuses_is_not_saved(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _settings.set_discogs_token(TOKEN)
    sent = _discogs_answers(monkeypatch, 401)
    r = client.post("/api/settings", json={"discogs_token": OTHER})
    assert r.status_code == 422
    assert r.json()["detail"] == "Discogs does not accept this token"
    assert OTHER not in r.text and OTHER not in caplog.text
    assert _settings.get_discogs_token() == TOKEN, "the old token stays"
    assert sent == [OTHER], "asked once"


def test_a_token_discogs_knows_is_saved(client, monkeypatch):
    _discogs_answers(monkeypatch, 200)
    r = client.post("/api/settings", json={"discogs_token": TOKEN})
    assert r.status_code == 200
    assert _settings.get_discogs_token() == TOKEN


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_discogs_down_or_busy_keeps_the_token_on_trust(client, monkeypatch, status):
    _discogs_answers(monkeypatch, status)
    r = client.post("/api/settings", json={"discogs_token": TOKEN})
    assert r.status_code == 200
    assert _settings.get_discogs_token() == TOKEN


def test_a_token_that_cannot_be_checked_is_kept_on_trust(client):
    # conftest answers every Discogs request as offline.
    r = client.post("/api/settings", json={"discogs_token": TOKEN})
    assert r.status_code == 200
    assert _settings.get_discogs_token() == TOKEN


def test_a_slow_discogs_does_not_hold_the_save(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    release = threading.Event()

    def hang(token):
        release.wait(30)
        return 401

    monkeypatch.setattr(da, "_ask_identity", hang)
    monkeypatch.setattr(da, "TIMEOUT_DISCOGS_CHECK", 0.3)
    started = time.monotonic()
    try:
        r = client.post("/api/settings", json={"discogs_token": TOKEN})
    finally:
        release.set()
    elapsed = time.monotonic() - started
    assert r.status_code == 200, "saved on trust"
    assert elapsed < 5, f"the Save waited {elapsed:.1f}s"
    assert _settings.get_discogs_token() == TOKEN
    assert TOKEN not in caplog.text


def test_the_check_can_be_turned_off(client, monkeypatch):
    sent = _discogs_answers(monkeypatch, 401)
    monkeypatch.setattr("app.main.DISCOGS_CHECK_TOKEN", False)
    r = client.post("/api/settings", json={"discogs_token": TOKEN})
    assert r.status_code == 200 and sent == []
    assert _settings.get_discogs_token() == TOKEN


# ── the request itself ──


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return b'{"id": 1, "username": "someone"}'


def test_the_token_goes_only_in_the_authorization_header(monkeypatch):
    monkeypatch.setattr(da, "_ask_identity", _REAL_ASK)
    seen = []

    def urlopen(request, timeout, context):
        seen.append(request)
        return _Response()

    monkeypatch.setattr(da.urllib.request, "urlopen", urlopen)
    assert da.discogs_token_works(TOKEN) is True
    (request,) = seen
    assert request.full_url == "https://api.discogs.com/oauth/identity"
    assert TOKEN not in request.full_url
    assert request.get_header("Authorization") == f"Discogs token={TOKEN}"
    assert request.get_header("User-agent", "").startswith("StemDeck/")


def test_a_401_from_the_real_request_is_a_refusal(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(da, "_ask_identity", _REAL_ASK)

    def urlopen(request, timeout, context):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(da.urllib.request, "urlopen", urlopen)
    assert da.discogs_token_works(TOKEN) is False
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("error", [TimeoutError("timed out"), urllib.error.URLError("offline")])
def test_no_answer_from_the_real_request_is_trust(monkeypatch, caplog, error):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(da, "_ask_identity", _REAL_ASK)

    def urlopen(request, timeout, context):
        raise error

    monkeypatch.setattr(da.urllib.request, "urlopen", urlopen)
    assert da.discogs_token_works(TOKEN) is None
    assert TOKEN not in caplog.text
