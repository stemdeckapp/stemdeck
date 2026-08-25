"""The optional cookies.txt handed to yt-dlp (#432).

YouTube's bot check has no other remedy in-tree: yt-dlp ships no PO token
generator, so an IP YouTube has flagged cannot import anything without
credentials. This is deliberately a file path rather than `cookiesfrombrowser`,
and deliberately empty by default -- supplying cookies makes yt-dlp skip every
client that does not support them, which removes the unauthenticated fallback
that works for most people.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import settings as _settings


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def cookies_txt(tmp_path):
    p = tmp_path / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return p


def test_unset_by_default():
    assert _settings.get_cookies_file() is None


def test_set_and_read_back(cookies_txt):
    stored = _settings.set_cookies_file(str(cookies_txt))
    assert stored == str(cookies_txt.resolve())
    assert _settings.get_cookies_file() == str(cookies_txt.resolve())


def test_path_is_resolved(tmp_path, cookies_txt):
    """Stored absolute and resolved, so a later cwd change can't move it."""
    relative = f"{tmp_path}/.//cookies.txt"
    assert _settings.set_cookies_file(relative) == str(cookies_txt.resolve())


def test_empty_clears(cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    assert _settings.set_cookies_file("") is None
    assert _settings.get_cookies_file() is None


def test_none_clears(cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    assert _settings.set_cookies_file(None) is None
    assert _settings.get_cookies_file() is None


def test_missing_file_is_rejected(tmp_path):
    """Told at the point of setting, not discovered as a failed import later."""
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(tmp_path / "nope.txt"))
    assert _settings.get_cookies_file() is None


def test_directory_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(tmp_path))


def test_a_rejected_value_does_not_replace_a_good_one(tmp_path, cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(tmp_path / "nope.txt"))
    assert _settings.get_cookies_file() == str(cookies_txt.resolve())


def test_api_exposes_the_path(client, cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    body = client.get("/api/settings").json()
    assert body["cookies_file"] == str(cookies_txt.resolve())


def test_api_never_exposes_the_contents(client, cookies_txt):
    """The file is the user's YouTube session. Only the path is public."""
    cookies_txt.write_text("# Netscape HTTP Cookie File\nSECRETVALUE\n", encoding="utf-8")
    _settings.set_cookies_file(str(cookies_txt))
    assert "SECRETVALUE" not in client.get("/api/settings").text


def test_api_sets_and_clears(client, cookies_txt):
    assert client.post("/api/settings", json={"cookies_file": str(cookies_txt)}).status_code == 200
    assert _settings.get_cookies_file() == str(cookies_txt.resolve())
    assert client.post("/api/settings", json={"cookies_file": ""}).status_code == 200
    assert _settings.get_cookies_file() is None


def test_api_rejects_a_missing_file(client, tmp_path):
    resp = client.post("/api/settings", json={"cookies_file": str(tmp_path / "nope.txt")})
    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"]


def test_api_rejects_a_non_string(client):
    assert client.post("/api/settings", json={"cookies_file": 17}).status_code == 422
