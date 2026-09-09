"""The last reachable branches: workspace staging, model caching, search
plumbing and audio scanning.

Small, scattered, and each one a fallback rather than an error path -- which is
why they were left: the code above them succeeds on a developer machine and the
fallback only runs on a filesystem, a machine or a network that behaves
differently.

_link_or_copy is the clearest example. It tries a hard link, then a symlink,
then a copy, and only the first is ever taken locally. The other two are what
runs on a Docker volume across devices, or on unelevated Windows where creating
a symlink needs a privilege StemDeck deliberately does not ask for.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

# --------------------------------------------------------------------------
# sections: staging stems into the work dir
# --------------------------------------------------------------------------


def test_a_stem_is_hard_linked_into_the_workspace_when_it_can_be(tmp_path):
    """The cheap path: no bytes copied for a file the worker only reads."""
    from app.pipeline.sections import _link_or_copy

    source = tmp_path / "vocals.wav"
    source.write_bytes(b"RIFF-data")
    target = tmp_path / "work" / "vocals.wav"
    target.parent.mkdir()

    _link_or_copy(source, target)

    assert target.read_bytes() == b"RIFF-data"
    assert target.stat().st_ino == source.stat().st_ino


def test_a_symlink_is_used_when_hard_linking_is_refused(tmp_path, monkeypatch):
    """Across devices -- a Docker volume, or a stems folder the user moved to
    another disk -- os.link fails with EXDEV."""
    from app.pipeline.sections import _link_or_copy

    source = tmp_path / "vocals.wav"
    source.write_bytes(b"RIFF-data")
    target = tmp_path / "work" / "vocals.wav"
    target.parent.mkdir()

    monkeypatch.setattr(os, "link", lambda a, b: (_ for _ in ()).throw(OSError(18, "EXDEV")))

    _link_or_copy(source, target)

    assert target.is_symlink()
    assert target.read_bytes() == b"RIFF-data"


def test_the_file_is_copied_when_neither_link_works(tmp_path, monkeypatch):
    """Unelevated Windows cannot create a symlink, and StemDeck does not ask for
    the privilege. Copying is slower but always works."""
    from app.pipeline.sections import _link_or_copy

    source = tmp_path / "vocals.wav"
    source.write_bytes(b"RIFF-data")
    target = tmp_path / "work" / "vocals.wav"
    target.parent.mkdir()

    monkeypatch.setattr(os, "link", lambda a, b: (_ for _ in ()).throw(OSError(18, "EXDEV")))
    monkeypatch.setattr(
        Path, "symlink_to", lambda self, t, **kw: (_ for _ in ()).throw(OSError(1314, "WinError"))
    )

    _link_or_copy(source, target)

    assert not target.is_symlink()
    assert target.read_bytes() == b"RIFF-data"


# --------------------------------------------------------------------------
# sections: what detect_sections gives up on
# --------------------------------------------------------------------------


@pytest.fixture
def sectionable(tmp_path, monkeypatch):
    from app.core.models import Job
    from app.pipeline import sections as sec

    stems = tmp_path / "stems"
    stems.mkdir()
    for name in ("bass", "drums", "vocals", "other", "guitar", "piano"):
        (stems / f"{name}.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(sec, "_mix_other_stems", lambda j, s, w: w / "other.wav")
    return Job(id="abcdefabcdef"), stems


def test_no_sections_when_the_stem_mix_could_not_be_built(sectionable, monkeypatch):
    """Without the combined "other" input the model has nothing to run on."""
    from app.pipeline import sections as sec

    job, stems = sectionable
    monkeypatch.setattr(sec, "_mix_other_stems", lambda j, s, w: None)

    assert sec.detect_sections(job, stems, duration=120.0) is None


def test_no_sections_when_the_worker_gave_nothing_back(sectionable, monkeypatch):
    from app.pipeline import sections as sec

    job, stems = sectionable
    monkeypatch.setattr(sec, "_run_worker", lambda j, w: None)

    assert sec.detect_sections(job, stems, duration=120.0) is None


def test_no_sections_when_the_output_normalises_to_nothing(sectionable, monkeypatch, caplog):
    """The model answered, but not with anything the editor can render. Saying
    so in the log is the only trace of a silently structure-less track."""
    from app.pipeline import sections as sec

    job, stems = sectionable
    monkeypatch.setattr(sec, "_run_worker", lambda j, w: {"segments": [{"bogus": 1}]})

    assert sec.detect_sections(job, stems, duration=120.0) is None
    assert "no valid structure" in caplog.text


def test_the_workspace_is_removed_whatever_happens(sectionable, monkeypatch):
    """It holds a full copy of four stems; leaking one per job fills the disk."""
    from app.pipeline import sections as sec

    job, stems = sectionable
    monkeypatch.setattr(sec, "_run_worker", lambda j, w: None)
    before = set(stems.iterdir())

    sec.detect_sections(job, stems, duration=120.0)

    assert set(stems.iterdir()) == before


def test_a_job_whose_stems_cannot_be_listed_is_skipped(tmp_path, monkeypatch):
    """One job folder on a disconnected volume must not leave every later job's
    workspace on disk."""
    from app.pipeline import sections as sec

    unreadable = tmp_path / "abcdefabcdef" / "stems"
    unreadable.mkdir(parents=True)
    readable = tmp_path / "abcdefabcdee" / "stems"
    readable.mkdir(parents=True)
    leftover = readable / f"{sec._WORK_PREFIX}orphan"
    leftover.mkdir()

    real_iterdir = Path.iterdir

    def _iterdir(self):
        if self == unreadable:
            raise OSError("volume went away")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)

    sec.sweep_orphaned_workspaces(tmp_path)  # must not raise

    monkeypatch.undo()
    assert not leftover.exists(), "the sweep stopped at the folder it could not read"


# --------------------------------------------------------------------------
# beat_detect: the model is built once, and a failure is remembered
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_beat_model():
    from app.pipeline import beat_detect as bd

    bd._model, bd._model_failed = None, False
    yield
    bd._model, bd._model_failed = None, False


def test_the_beat_model_is_built_once_and_reused(monkeypatch, caplog):
    """Loading the checkpoint costs about a second and 81 MB; doing it per job
    would put that on every import."""
    from app.pipeline import beat_detect as bd

    built = []

    class _Audio2Beats:
        def __init__(self, **kw):
            built.append(kw)

    monkeypatch.setitem(sys.modules, "beat_this", types.ModuleType("beat_this"))
    mod = types.ModuleType("beat_this.inference")
    mod.Audio2Beats = _Audio2Beats
    monkeypatch.setitem(sys.modules, "beat_this.inference", mod)
    monkeypatch.setattr("app.core.model_cache.load_or_heal", lambda load, stale: load())

    first = bd._get_model()
    second = bd._get_model()

    assert first is second
    assert len(built) == 1
    assert "beat model loaded" in caplog.text


def test_a_machine_without_beat_this_falls_back_to_librosa_once(monkeypatch, caplog):
    """Retrying per job would stall every import on the same failure."""
    from app.pipeline import beat_detect as bd

    attempts = {"n": 0}
    real_import = __import__

    def _no_beat_this(name, *a, **kw):
        if name.startswith("beat_this"):
            attempts["n"] += 1
            raise ImportError("no beat_this")
        return real_import(name, *a, **kw)

    monkeypatch.delitem(sys.modules, "beat_this", raising=False)
    monkeypatch.delitem(sys.modules, "beat_this.inference", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_beat_this)

    assert bd._get_model() is None
    assert bd._get_model() is None

    assert attempts["n"] == 1, "the missing import was retried"
    assert "using librosa beat tracking" in caplog.text


# --------------------------------------------------------------------------
# model_cache: removing a cached artifact
# --------------------------------------------------------------------------


def test_a_cached_artifact_that_cannot_be_removed_is_reported_not_raised(
    tmp_path, monkeypatch, caplog
):
    """A file held open by another process, or a read-only cache dir. The
    caller's own load error is the real one; this must not replace it."""
    from app.core import model_cache

    artifact = tmp_path / "model.ckpt"
    artifact.write_bytes(b"x")

    def _boom(self, **kw):
        raise OSError("in use")

    monkeypatch.setattr(Path, "unlink", _boom)

    assert model_cache._remove(artifact) is False
    assert "could not remove cached model artifact" in caplog.text


def test_removing_a_directory_artifact_works(tmp_path):
    from app.core import model_cache

    d = tmp_path / "cache-dir"
    (d / "inner").mkdir(parents=True)
    (d / "inner" / "f").write_bytes(b"x")

    assert model_cache._remove(d) is True
    assert not d.exists()


def test_removing_something_that_is_not_there_reports_nothing_removed(tmp_path):
    """The retry is conditional on having actually invalidated something; a
    load that failed for another reason must not pay the download twice."""
    from app.core import model_cache

    assert model_cache._remove(tmp_path / "absent.ckpt") is False


# --------------------------------------------------------------------------
# audio_stats: scanning a stem
# --------------------------------------------------------------------------


def test_an_empty_stem_scans_to_nothing(tmp_path):
    """A zero-frame WAV would make the bucket arithmetic divide by zero."""
    from app.pipeline.audio_stats import scan_stem

    path = tmp_path / "silent.wav"
    sf.write(str(path), np.zeros((0, 2), dtype=np.float32), 44100)

    peaks, rms = scan_stem(path)

    assert peaks == []
    assert rms == 0.0


def test_a_stem_scans_to_bounded_buckets_and_its_rms(tmp_path):
    from app.pipeline.audio_stats import scan_stem

    path = tmp_path / "tone.wav"
    t = np.linspace(0, 1.0, 44100, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.column_stack([tone, tone]), 44100)

    peaks, rms = scan_stem(path, buckets=100)

    assert 0 < len(peaks) <= 100
    assert all(lo <= hi for lo, hi in peaks)
    # A 0.5-amplitude sine has an RMS of 0.5/sqrt(2).
    assert rms == pytest.approx(0.5 / np.sqrt(2), abs=0.01)


# --------------------------------------------------------------------------
# search: cache and error plumbing
# --------------------------------------------------------------------------


def test_an_expired_search_result_is_dropped_from_the_cache(monkeypatch):
    """The too_long verdict on each row depends on a live setting, so a stale
    entry would grey out rows the user has since made importable."""
    import time

    from app.api import search as api_search

    api_search._clear_cache()
    key = ("youtube", "track", "q", 10, 600)
    api_search._cache_put(key, {"items": []})
    assert api_search._cache_get(key) is not None

    later = time.monotonic() + api_search._CACHE_TTL_SEC + 60
    monkeypatch.setattr(time, "monotonic", lambda: later)

    assert api_search._cache_get(key) is None
    assert key not in api_search._cache


def test_an_unsupported_search_reaching_the_backend_is_a_422(monkeypatch):
    """The endpoint checks the pair up front, but the backend can refuse a
    combination the table thought was fine -- a client bug, not a server one."""
    from fastapi.testclient import TestClient

    from app.api import search as api_search
    from app.main import app
    from app.pipeline.search import UnsupportedSearch

    api_search._clear_cache()

    def _boom(query, source, kind, limit):
        raise UnsupportedSearch("soundcloud has no playlist search")

    monkeypatch.setattr(api_search, "search", _boom)

    with TestClient(app) as c:
        r = c.post("/api/search", json={"query": "daft punk"})

    assert r.status_code == 422
    assert "playlist search" in r.json()["detail"]


def test_a_preview_whose_signed_url_expired_is_a_502(monkeypatch):
    """Between resolving a stream and proxying it the signature can lapse; the
    listener gets "could not load", not a stack trace."""
    import urllib.error

    from fastapi.testclient import TestClient

    from app.api import search as api_search
    from app.main import app

    monkeypatch.setattr(
        api_search, "resolve_preview", lambda url: {"url": "https://x/stream", "headers": {}}
    )

    def _boom(stream, range_header):
        raise urllib.error.HTTPError("https://x/stream", 403, "Forbidden", {}, None)

    monkeypatch.setattr(api_search, "_proxy", _boom)

    with TestClient(app) as c:
        r = c.get("/api/search/preview", params={"url": "https://youtu.be/dQw4w9WgXcQ"})

    assert r.status_code == 502
    assert r.json()["detail"] == "Could not load a preview"
