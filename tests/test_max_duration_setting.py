"""The longest track accepted for processing (max_duration_sec)."""

from __future__ import annotations

from app.core import settings as _settings


def test_the_duration_ceiling_is_published_not_duplicated():
    """The UI used to keep its own copy of the max-track-length ceiling. It
    went stale at 20 minutes against a real ceiling of 60, so typing 60 was
    silently clamped to 20 and the field snapped back with no explanation.

    The cure was to stop having a second copy, so the bound has to stay in the
    payload for the client to read.
    """
    from fastapi.testclient import TestClient

    from app.core.settings import DURATION_MAX_SEC, DURATION_MIN_SEC
    from app.main import app

    with TestClient(app) as c:
        body = c.get("/api/settings").json()
    assert body["max_duration_max_sec"] == DURATION_MAX_SEC
    assert body["max_duration_min_sec"] == DURATION_MIN_SEC


def test_a_value_at_the_ceiling_is_kept(tmp_path, monkeypatch):
    """60 minutes is a legal setting. It was reachable through the API all
    along; only the client refused to send it."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(_settings, "_SETTINGS_PATH", path)
    monkeypatch.setattr(_settings, "_state", None)
    assert _settings.set_max_duration_sec(3600) == 3600
    assert _settings.get_max_duration_sec() == 3600


def test_beyond_the_ceiling_still_clamps(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(_settings, "_SETTINGS_PATH", path)
    monkeypatch.setattr(_settings, "_state", None)
    assert _settings.set_max_duration_sec(99 * 60) == 3600
    assert _settings.set_max_duration_sec(1) == 60
