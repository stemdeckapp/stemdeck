from __future__ import annotations

import sys

import pytest

from app.core import settings as _settings
from app.pipeline import jobqueue as _jobqueue


@pytest.fixture(autouse=True)
def _isolate_jobs_dir(tmp_path, monkeypatch):
    """Point every JOBS_DIR at a temp dir, for every test, no exceptions.

    Several modules do `from app.core.config import JOBS_DIR`, which binds the
    value at import time, so patching config alone is not enough. Individual
    fixtures used to patch it one module at a time and any test that forgot ran
    against the developer's real jobs/ directory. That is not a hypothetical:
    the suite writes the registry and starts the TTL sweep through TestClient's
    lifespan, and together those wiped a local library.
    """
    jobs = tmp_path / "_jobs_root"
    jobs.mkdir(parents=True, exist_ok=True)
    import app.core.config as cfg

    monkeypatch.setattr(cfg, "JOBS_DIR", jobs)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and getattr(module, "JOBS_DIR", None) is not None:
            monkeypatch.setattr(module, "JOBS_DIR", jobs, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_job_queue():
    """The queue is module-global, so a job left waiting by one test would be
    picked up by the next test's worker."""
    from app.core import registry as _registry

    _jobqueue._queue.clear()
    _jobqueue._running_id = None
    _jobqueue._paused = False
    # restore() populates this at import time from whatever registry is on disk;
    # the app lifespan drains it into the queue, which would otherwise leak a
    # real job into a test's queue.
    _registry._pending_resume.clear()
    # Deletion records are module-global too, so one test's deleted id would
    # suppress another test's orphan recovery (#521).
    _registry._deleted.clear()
    yield
    _jobqueue._queue.clear()
    _jobqueue._running_id = None
    _jobqueue._paused = False
    _registry._pending_resume.clear()
    _registry._deleted.clear()


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
    # Song-structure extraction is env-seeded too. A developer who exported
    # STEMDECK_AUTO_SECTIONS=0 in their shell must not change what the pipeline
    # tests assert; the tests that care set it themselves.
    monkeypatch.delenv("STEMDECK_AUTO_SECTIONS", raising=False)
    _settings._state = None  # force a fresh load from the isolated path
    yield
    _settings._state = None
