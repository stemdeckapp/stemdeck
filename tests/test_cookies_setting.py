"""The optional cookies.txt handed to yt-dlp (#432).

YouTube's bot check has no other remedy in-tree: yt-dlp ships no PO token
generator, so an IP YouTube has flagged cannot import anything without
credentials. This is deliberately a file path rather than `cookiesfrombrowser`,
and deliberately empty by default -- supplying cookies makes yt-dlp skip every
client that does not support them, which removes the unauthenticated fallback
that works for most people.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import settings as _settings


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(_settings, "DATA_DIR", d)
    return d


@pytest.fixture
def cookies_txt(data_dir):
    p = data_dir / "cookies.txt"
    p.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    return p


def test_unset_by_default():
    assert _settings.get_cookies_file() is None


def test_set_and_read_back(cookies_txt):
    stored = _settings.set_cookies_file(str(cookies_txt))
    assert stored == str(cookies_txt.resolve())
    assert _settings.get_cookies_file() == str(cookies_txt.resolve())


def test_path_is_resolved(data_dir, cookies_txt):
    """Stored absolute and resolved, so a later cwd change can't move it."""
    relative = f"{data_dir}/.//cookies.txt"
    assert _settings.set_cookies_file(relative) == str(cookies_txt.resolve())


def test_a_bare_name_is_found_in_the_data_folder(cookies_txt):
    assert _settings.set_cookies_file("cookies.txt") == str(cookies_txt.resolve())


def test_a_file_outside_the_data_folder_is_refused(tmp_path, cookies_txt):
    """Anyone who can reach Settings could otherwise point yt-dlp at any file
    on the machine, which echoes the lines it cannot parse into the log."""
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    for path in (str(outside), f"{cookies_txt.parent}/../elsewhere.txt"):
        with pytest.raises(ValueError):
            _settings.set_cookies_file(path)
    assert _settings.get_cookies_file() is None


def test_a_network_share_is_refused_without_being_opened(monkeypatch):
    """Resolving \\\\host\\share opens it, and Windows answers the share with
    the user's NTLM hash: the name alone must be enough to refuse it."""
    opened = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda p, *a, **k: opened.append(p) or real_resolve(p))
    with pytest.raises(ValueError):
        _settings.set_cookies_file("//10.0.0.5/share/cookies.txt")
    assert not [p for p in opened if "10.0.0.5" in str(p)]


def test_api_answers_an_outside_file_and_a_missing_one_alike(client, tmp_path):
    """Not an oracle for whether a path exists on the machine."""
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    present = client.post("/api/settings", json={"cookies_file": str(outside)})
    missing = client.post("/api/settings", json={"cookies_file": str(tmp_path / "nope.txt")})
    garbled = client.post("/api/settings", json={"cookies_file": "bad\x00name.txt"})
    assert present.status_code == missing.status_code == garbled.status_code == 422
    assert present.json() == missing.json() == garbled.json()


def test_empty_clears(cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    assert _settings.set_cookies_file("") is None
    assert _settings.get_cookies_file() is None


def test_none_clears(cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    assert _settings.set_cookies_file(None) is None
    assert _settings.get_cookies_file() is None


def test_missing_file_is_rejected(data_dir):
    """Told at the point of setting, not discovered as a failed import later."""
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(data_dir / "nope.txt"))
    assert _settings.get_cookies_file() is None


def test_directory_is_rejected(data_dir):
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(data_dir))


def test_a_rejected_value_does_not_replace_a_good_one(data_dir, cookies_txt):
    _settings.set_cookies_file(str(cookies_txt))
    with pytest.raises(ValueError):
        _settings.set_cookies_file(str(data_dir / "nope.txt"))
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


def test_api_rejects_a_missing_file(client, data_dir):
    resp = client.post("/api/settings", json={"cookies_file": str(data_dir / "nope.txt")})
    assert resp.status_code == 422
    assert "data folder" in resp.json()["detail"]


def test_api_rejects_a_non_string(client):
    assert client.post("/api/settings", json={"cookies_file": 17}).status_code == 422
