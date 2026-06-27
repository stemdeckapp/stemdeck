from __future__ import annotations

import pytest

from app.core import settings as _settings


@pytest.fixture(autouse=True)
def _isolate_network_settings(tmp_path, monkeypatch):
    """Isolate the runtime network gate for every test. Without this, a stray
    settings.json in the repo (written by a local dev server) could flip the
    gate off and 403 the whole suite, since TestClient's client host is not
    loopback. Each test starts from the env default (on, outside desktop mode)."""
    monkeypatch.setattr(_settings, "_SETTINGS_PATH", tmp_path / "settings.json")
    # Network access defaults OFF in production, which would 403 TestClient
    # (whose client host isn't loopback). Default the suite ON; gate tests set
    # it explicitly. Tests checking the real default clear this env var.
    monkeypatch.setenv("STEMDECK_ALLOW_NETWORK", "1")
    _settings._state = None  # force a fresh load from the isolated path
    yield
    _settings._state = None
