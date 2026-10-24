from __future__ import annotations

import sys

import pytest

from app.core import settings as _settings
from app.pipeline import jobqueue as _jobqueue


@pytest.fixture(autouse=True)
def _no_wikidata(monkeypatch):
    """No test reaches Wikidata. A job with an artist tag looks its band up
    while it runs (#699), so any pipeline or endpoint test with tags would
    otherwise make a real request. Offline is what this answers, which the
    lookup treats as "no band"; tests that want an answer stub it themselves."""

    def offline(params):
        raise OSError("no network in tests")

    monkeypatch.setattr("app.pipeline.artist_lookup._fetch_json", offline)


@pytest.fixture(autouse=True)
def _no_lrclib(monkeypatch):
    """No test reaches LRCLIB. A job with tags looks its lyrics up while it
    runs, and so does the tag backfill; offline is what this answers, which
    the lookup treats as "no lyrics". Tests that want an answer stub it."""

    def offline(endpoint, params):
        raise OSError("no network in tests")

    monkeypatch.setattr("app.pipeline.lyrics_lookup._fetch_json", offline)


@pytest.fixture(autouse=True)
def _no_whisper(monkeypatch):
    """No test runs Whisper. A job separated on a GPU with no lyrics found
    transcribes its vocals at the end (app/pipeline/transcribe.py), and the
    real worker would download a 1.6 GB model. The worker this points at
    exits at once, which the stage treats as "no transcript". Tests that want
    one point _spawn_worker_cmd at a stub of their own."""
    monkeypatch.setattr(
        "app.pipeline.transcribe._spawn_worker_cmd",
        lambda vocals, device: [sys.executable, "-c", "raise SystemExit(3)"],
    )


@pytest.fixture(autouse=True)
def _no_acoustid_or_musicbrainz(monkeypatch, tmp_path):
    """No test reaches api.acoustid.org or musicbrainz.org. Every job with an
    artist tag is identified while it runs (app/pipeline/identify.py), and so
    is the tag backfill; offline is what this answers, which identification
    treats as "not found" and falls back to the tags. The MusicBrainz cache
    goes to a temporary directory, so no test reads or writes the real one.
    Tests that want an answer stub the two requests themselves."""

    def offline(*args, **kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr("app.pipeline.musicbrainz._fetch_json", offline)
    monkeypatch.setattr("app.pipeline.identify._acoustid_request", offline)
    monkeypatch.setattr("app.pipeline.musicbrainz.CACHE_DIR", tmp_path / "_musicbrainz_cache")


@pytest.fixture(autouse=True)
def _no_discogs(monkeypatch):
    """No test reaches api.discogs.com. A Discogs token is tried once when it
    is saved (app/pipeline/discogs_auth.py); offline is what this answers,
    which keeps the token on trust. Tests that want an answer stub it."""

    def offline(*args, **kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr("app.pipeline.discogs_auth._ask_identity", offline)


@pytest.fixture(autouse=True)
def _no_discogs_api(monkeypatch, tmp_path):
    """No test reaches api.discogs.com for a band's profile either
    (app/pipeline/discogs.py, GET /api/jobs/{id}/artist-extra). Offline is
    what this answers, which the lookup treats as "nothing found". Its disk
    cache goes to a temporary directory and its memory is emptied, so no test
    reads another's answers. Tests that want an answer stub _send."""
    from app.pipeline import discogs as _discogs

    def offline(*args, **kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(_discogs, "_send", offline)
    monkeypatch.setattr(_discogs, "CACHE_DIR", tmp_path / "_discogs_cache")
    _discogs.forget()
    yield
    _discogs.forget()


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
def _relax_secure_origin_gate(monkeypatch):
    """Server mode refuses a plaintext request from a non-local client, and
    TestClient's peer is "testclient" -- not loopback -- so the gate would 403
    the entire suite for a reason none of those tests are about. The gate has
    its own file, tests/test_secure_origin_gate.py, which puts it back."""
    import app.main as _main

    monkeypatch.setattr(_main, "_secure_origin_required", lambda: False)
    yield


@pytest.fixture(autouse=True)
def _test_hosts_are_own_names(monkeypatch):
    """The cross-site guard refuses a Host it does not know while network access
    is off, and the test clients address the app as "testserver", "test" or "t".
    Treated here as the machine's own names, so the gate tests that switch
    network access off fail for their own reason. tests/test_csrf_guard.py
    checks the real rule."""
    from app.core import csrf as _csrf

    real = _csrf.own_hostnames()
    monkeypatch.setattr(
        _csrf, "own_hostnames", lambda: real | frozenset({"testserver", "test", "t"})
    )
    yield


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


@pytest.fixture(autouse=True)
def _isolate_vocal_split_status(tmp_path, monkeypatch):
    """Every split a test runs records its outcome (#832): never in the real
    data folder, and never carried from one test into the next."""
    from app.pipeline import vocal_split_status as _vss

    monkeypatch.setattr(_vss, "_status_path", tmp_path / "vocal-split-status.json")
    _vss.reset_state()
    yield
    _vss.reset_state()


@pytest.fixture(autouse=True)
def _isolate_video_encoder(tmp_path, monkeypatch):
    """No test touches the machine's GPU encoders or the real probe cache.
    The suite defaults to the CPU kill switch, so every existing render runs
    libx264 exactly as before; tests/test_video_encoder.py clears it."""
    from app.pipeline import video_encoder as _ve

    monkeypatch.setattr(_ve, "_cache_path", tmp_path / "video_encoders.json")
    monkeypatch.setattr(_ve, "VIDEO_ENCODER_OVERRIDE", "cpu")
    _ve.reset_state()
    yield
    _ve.reset_state()
