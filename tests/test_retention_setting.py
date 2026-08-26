"""Automatic deletion of finished jobs, and the setting that controls it (#459).

Deleting a finished separation destroys work that cannot be recovered. The
behaviour used to be on by default and switched off by an environment variable,
which meant every documented way of starting StemDeck set that variable and
anyone who started the backend directly lost their library within a day. These
tests hold the direction: off unless asked, and the user's stored choice beats
the environment.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_settings_report_deletion_off_with_bounds(client):
    body = client.get("/api/settings").json()
    assert body["auto_delete_jobs"] is False
    # Published even when off, so the field the toggle reveals opens with a
    # value rather than empty.
    assert body["auto_delete_days"] == 30
    assert body["auto_delete_days_min"] == 1
    assert body["auto_delete_days_max"] == 365


def test_toggle_and_days_round_trip(client):
    r = client.post("/api/settings", json={"auto_delete_jobs": True, "auto_delete_days": 7})
    assert r.status_code == 200
    body = client.get("/api/settings").json()
    assert body["auto_delete_jobs"] is True
    assert body["auto_delete_days"] == 7


def test_days_are_clamped_not_rejected(client):
    from app.core.settings import AUTO_DELETE_DAYS_MAX, AUTO_DELETE_DAYS_MIN

    client.post("/api/settings", json={"auto_delete_days": 0})
    assert client.get("/api/settings").json()["auto_delete_days"] == AUTO_DELETE_DAYS_MIN
    client.post("/api/settings", json={"auto_delete_days": 99_999})
    assert client.get("/api/settings").json()["auto_delete_days"] == AUTO_DELETE_DAYS_MAX


def test_days_reject_nonsense(client):
    r = client.post("/api/settings", json={"auto_delete_days": "soon"})
    assert r.status_code == 422


def test_turning_it_off_again_sticks_over_the_environment(client, monkeypatch):
    """The stored choice has to survive an env var that says otherwise, or the
    setting is decorative on exactly the deployments that need it most."""
    monkeypatch.setenv("STEMDECK_PERSIST_LIBRARY", "0")
    client.post("/api/settings", json={"auto_delete_jobs": False})
    assert client.get("/api/settings").json()["auto_delete_jobs"] is False


def test_job_ttl_seconds_seeds_the_default_days(monkeypatch):
    """A deployment that already tuned STEMDECK_JOB_TTL_SECONDS keeps its
    intent when it opts in, rather than silently jumping to 30 days."""
    from app.core import settings as _settings

    monkeypatch.setenv("STEMDECK_JOB_TTL_SECONDS", str(3 * 86400))
    _settings._state = None
    assert _settings.get_auto_delete_days() == 3

    # Sub-day TTLs round to one day, not to none. Rounding a deliberately
    # aggressive sweep down to zero would turn it into a much slower one.
    monkeypatch.setenv("STEMDECK_JOB_TTL_SECONDS", "3600")
    _settings._state = None
    assert _settings.get_auto_delete_days() == 1

    monkeypatch.setenv("STEMDECK_JOB_TTL_SECONDS", "not-a-number")
    _settings._state = None
    assert _settings.get_auto_delete_days() == _settings.DEFAULT_AUTO_DELETE_DAYS
