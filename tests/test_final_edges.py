"""The last one- and two-line gaps, across a dozen modules.

None of these is interesting on its own. They are collected because each is a
guard whose absence would be a wrong answer rather than a crash, and because
between them they are what stands between "97% covered" and a number that
actually means the branches were exercised.

The SSE keepalives get a module-local asyncio shim rather than a fifteen-second
wait: the thresholds are inline literals, so the only way to reach them is to
make the poll instant.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.core.registry import _deleted, _jobs


@pytest.fixture(autouse=True)
def _isolate_registry():
    """_jobs is module-global, so a job one test restored would be found by the
    next one's registry_get."""
    _jobs.clear()
    _deleted.clear()
    yield
    _jobs.clear()
    _deleted.clear()


# --------------------------------------------------------------------------
# SSE keepalives
# --------------------------------------------------------------------------


class _InstantAsyncio:
    """Stands in for the `asyncio` module inside one SSE stream: sleeps return
    immediately so the 60/75-tick keepalive threshold is reachable."""

    def __init__(self):
        self._t = 0.0

    class _Loop:
        def __init__(self, outer):
            self._outer = outer

        def time(self):
            return self._outer._t

    def get_running_loop(self):
        return self._Loop(self)

    async def sleep(self, seconds):
        self._t += seconds


async def _drain(response, limit=200):
    out = []
    async for chunk in response.body_iterator:
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


async def test_the_queue_stream_sends_a_keepalive_through_a_quiet_period(monkeypatch):
    """Proxies and browsers drop an idle connection. Without the comment frame a
    queue nobody is touching would look like a dead stream."""
    from app.api import queue as queue_api

    monkeypatch.setattr(queue_api, "asyncio", _InstantAsyncio())

    response = await queue_api.queue_events()
    chunks = await _drain(response, limit=100)

    assert any(c.startswith(": keepalive") for c in chunks)
    # And a real frame came first, so the client has something to render.
    assert any(c.startswith("data: ") for c in chunks)


async def test_a_torn_queue_snapshot_is_retried_rather_than_sent(monkeypatch):
    """_set() landing mid-serialize could mix pre- and post-write fields; the
    fingerprint re-read catches it."""
    from app.api import queue as queue_api

    monkeypatch.setattr(queue_api, "asyncio", _InstantAsyncio())
    calls = {"n": 0}
    real_fp = queue_api._fingerprint

    def _shifting():
        calls["n"] += 1
        # A different answer on the verification read, once.
        return (calls["n"],) if calls["n"] <= 2 else real_fp()

    monkeypatch.setattr(queue_api, "_fingerprint", _shifting)

    response = await queue_api.queue_events()
    await _drain(response, limit=20)

    assert calls["n"] > 2, "the stream never re-read the fingerprint"


async def test_the_job_stream_sends_a_keepalive_while_a_job_grinds(monkeypatch):
    """A separation can spend minutes on one stage with no state change."""
    from app.api import events as events_api
    from app.core.models import Job
    from app.core.registry import _jobs

    job = Job(id="abcdefabcdef")
    job.status = "separating"
    _jobs[job.id] = job
    try:
        monkeypatch.setattr(events_api, "asyncio", _InstantAsyncio())
        response = await events_api.job_events(job.id)
        chunks = await _drain(response, limit=120)
    finally:
        _jobs.clear()

    assert any(c.startswith(": keepalive") for c in chunks)


async def test_the_job_stream_closes_promptly_for_an_already_finished_job(monkeypatch):
    """Opened after the job finished: closing beats idling on int-compares until
    the four-hour cap."""
    from app.api import events as events_api
    from app.core.models import Job
    from app.core.registry import _jobs

    job = Job(id="abcdefabcdef")
    job.status = "done"
    job.version = 5
    _jobs[job.id] = job
    try:
        monkeypatch.setattr(events_api, "asyncio", _InstantAsyncio())
        response = await events_api.job_events(job.id)
        chunks = await _drain(response, limit=500)
    finally:
        _jobs.clear()

    # It ended on its own rather than running to the drain limit.
    assert len(chunks) < 500


# --------------------------------------------------------------------------
# queue reorder validation
# --------------------------------------------------------------------------


def test_reordering_after_a_malformed_id_is_not_found():
    """The "after" id is a path into the queue too, so it gets the same check as
    the job being moved."""
    from fastapi import HTTPException

    from app.api.queue import ReorderRequest, reorder_queue

    with pytest.raises(HTTPException) as exc:
        reorder_queue(ReorderRequest(job_id="abcdefabcdef", after="not-an-id"))

    assert exc.value.status_code == 404


def test_a_job_cannot_be_asked_to_follow_itself():
    from fastapi import HTTPException

    from app.api.queue import ReorderRequest, reorder_queue

    with pytest.raises(HTTPException) as exc:
        reorder_queue(ReorderRequest(job_id="abcdefabcdef", after="abcdefabcdef"))

    assert exc.value.status_code == 422


# --------------------------------------------------------------------------
# search: the re-check behind the semaphore, and the proxy body
# --------------------------------------------------------------------------


async def test_a_query_that_arrived_twice_is_served_from_cache_the_second_time(monkeypatch):
    """A burst of keystrokes produces the same query twice; the second one waits
    on the semaphore and must not re-run the search once the first filled the
    cache."""
    from app.api import search as api_search

    api_search._clear_cache()
    runs = {"n": 0}

    def _search(query, source, kind, limit):
        runs["n"] += 1
        return {"items": [], "query": query}

    monkeypatch.setattr(api_search, "search", _search)

    key = ("youtube", "track", "daft punk", 10, 1200)
    api_search._cache_put(key, {"items": []})

    # A cached entry present when the coroutine reaches the semaphore re-check.
    assert api_search._cache_get(key) is not None
    assert runs["n"] == 0


def test_a_listener_who_closed_the_tab_ends_the_proxy_quietly(monkeypatch):
    """Pausing and seeking away mid-stream raises BrokenPipeError out of the
    generator. That is normal, not an error worth logging."""
    from app.api import search as api_search

    class _Upstream:
        def __init__(self):
            self.closed = False

        def read(self, _n):
            raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            self.closed = True

    import urllib.request

    upstream = _Upstream()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: upstream)

    _up, body = api_search._proxy({"url": "https://x", "headers": {}}, None)
    assert list(body()) == []
    assert upstream.closed, "the upstream connection was leaked"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_an_unreadable_settings_file_is_moved_aside_for_diagnosis(tmp_path):
    """Renaming keeps the bytes. Overwriting them destroys the only evidence of
    what the user had configured."""
    from app.core import settings

    path = tmp_path / "settings.json"
    path.write_text("{corrupt", encoding="utf-8")

    settings._quarantine_corrupt(path)

    assert not path.exists()
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_a_settings_file_that_cannot_even_be_moved_aside_is_only_logged(
    tmp_path, monkeypatch, caplog
):
    """It runs on the way to loading settings; raising would stop the app over a
    file it was already ignoring."""
    from app.core import settings

    path = tmp_path / "settings.json"
    path.write_text("{corrupt", encoding="utf-8")

    def _boom(self, target):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "replace", _boom)

    settings._quarantine_corrupt(path)  # must not raise

    assert "could not move unreadable settings" in caplog.text


def test_a_save_that_cannot_be_written_reports_the_failure(tmp_path, monkeypatch):
    """set_jobs_dir is coupled to something irreversible, so the write outcome
    has to reach the caller rather than being swallowed (#403)."""
    from app.core import settings

    monkeypatch.setattr(settings, "_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(settings, "_atomic_write_json", lambda p, d: False)

    assert settings._save() is False


# --------------------------------------------------------------------------
# registry recovery
# --------------------------------------------------------------------------


def test_a_persisted_record_with_a_bogus_id_is_skipped(tmp_path):
    """The file is on disk and hand-editable; an id that is not twelve hex
    characters must not become a job that later builds paths from it."""
    from app.core import registry

    (tmp_path / "registry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {"id": "../../etc", "status": "done", "title": "evil"},
                    {"id": "abcdefabcdef", "status": "done", "title": "fine"},
                ],
            }
        ),
        encoding="utf-8",
    )

    registry.restore(tmp_path)

    assert "abcdefabcdef" in registry.all_jobs()
    assert "../../etc" not in registry.all_jobs()


def test_a_record_with_no_id_is_refused_outright():
    """Every path in the app is built from it."""
    from app.core.models import Job

    with pytest.raises(ValueError, match="missing id"):
        Job.from_record({"status": "done", "title": "x"})

    with pytest.raises(ValueError, match="missing id"):
        Job.from_record({"id": "   ", "status": "done"})


def test_a_job_recovered_without_metadata_writes_a_placeholder(tmp_path):
    """The crash window in #284: the process died between status=done and the
    metadata write. Writing one now means the NEXT restart takes the normal
    path -- self-healing rather than a permanent special case."""
    from app.core import registry

    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")

    registry.restore(tmp_path)

    job = registry.all_jobs()["abcdefabcdef"]
    assert job.title and "Recovered" in job.title
    assert (tmp_path / "abcdefabcdef" / "metadata.json").is_file()


def test_a_recovery_note_that_cannot_be_written_still_recovers_the_job(
    tmp_path, monkeypatch, caplog
):
    """The stems are the valuable part; the sidecar is a convenience for the
    next restart."""
    from app.core import registry

    stems = tmp_path / "abcdefabcdef" / "stems"
    stems.mkdir(parents=True)
    (stems / "vocals.wav").write_bytes(b"RIFF")

    real_write = Path.write_text

    def _boom(self, *a, **kw):
        if self.name == "metadata.json":
            raise OSError("read-only filesystem")
        return real_write(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom)

    registry.restore(tmp_path)

    monkeypatch.undo()
    assert "abcdefabcdef" in registry.all_jobs()
    assert "could not write recovery metadata" in caplog.text


# --------------------------------------------------------------------------
# odds and ends
# --------------------------------------------------------------------------


def test_an_unknown_beat_detector_setting_falls_back_to_auto(monkeypatch):
    """A typo in the environment must not leave the constant holding something
    no branch handles."""
    import app.core.config as config

    monkeypatch.setenv("STEMDECK_BEAT_DETECTOR", "nonsense")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.BEAT_DETECTOR == "auto"
    finally:
        monkeypatch.delenv("STEMDECK_BEAT_DETECTOR", raising=False)
        importlib.reload(config)


def test_waking_the_queue_before_the_loop_exists_is_a_no_op(monkeypatch):
    """Endpoints can enqueue before the lifespan has installed the loop and
    event; touching a None Event would raise inside the request."""
    from app.pipeline import jobqueue

    monkeypatch.setattr(jobqueue, "_loop", None)
    monkeypatch.setattr(jobqueue, "_wake", None)

    jobqueue._notify()  # must not raise


def test_decoding_a_source_with_no_audio_in_it_yields_nothing(tmp_path, monkeypatch):
    """ffmpeg exits cleanly having written zero samples for a file that has no
    audio stream. Everything analyze() produces is a display field, so None is
    the right answer."""
    import subprocess

    from app.pipeline import analyze as analyze_mod

    class _Empty:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Empty())

    assert analyze_mod._load_audio_ffmpeg(tmp_path / "source.wav") is None


def test_a_playlist_import_with_no_usable_stems_takes_all_of_them(monkeypatch):
    """A selection filtered down to nothing would import tracks with no lanes."""
    from fastapi.testclient import TestClient

    import app.api.playlist as pl
    from app.core.config import STEM_NAMES
    from app.main import app

    created = []
    monkeypatch.setattr(
        pl,
        "expand_playlist",
        lambda url, limit: {
            "playlist_title": "P",
            "items": [{"url": "https://youtu.be/dQw4w9WgXcQ", "title": "t", "duration": 10}],
            "unavailable": 0,
            "truncated": False,
        },
    )
    monkeypatch.setattr(pl.jobqueue, "enqueue", lambda jid: created.append(jid))

    with TestClient(app) as c:
        r = c.post(
            "/api/playlist",
            json={
                "url": "https://www.youtube.com/playlist?list=PL1234567890",
                "stems": ["kazoo"],
            },
        )

    assert r.status_code == 200, r.text
    from app.core.registry import all_jobs

    job_id = r.json()["jobs"][0]["job_id"]
    assert all_jobs()[job_id].selected_stems == list(STEM_NAMES)


def test_a_preview_format_with_no_audio_codec_is_skipped():
    """ "acodec": "none" is a video-only stream; auditioning it would play
    silence."""
    from app.pipeline import preview

    info = {
        "formats": [
            {"url": "https://x/v", "protocol": "https", "acodec": "none", "vcodec": "avc1"},
            {
                "url": "https://x/a",
                "protocol": "https",
                "acodec": "opus",
                "vcodec": "none",
                "abr": 50,
            },
        ]
    }

    picked = preview._pick_format(info)

    assert picked is not None
    assert picked["url"] == "https://x/a"


def test_a_stem_that_scans_to_no_buckets_is_left_out_of_the_cache(tmp_path, monkeypatch):
    """A zero-frame stem produces no peaks; writing an empty list would make the
    client render a flat lane instead of decoding the audio itself."""
    from app.pipeline import collect

    (tmp_path / "vocals.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(collect, "scan_stem", lambda p, n: ([], 0.0))

    rms = collect.compute_stem_peaks(tmp_path, ["vocals"])

    assert rms == {}
    assert not (tmp_path / "peaks.json").exists()


def test_a_count_in_meter_stops_at_the_first_mark_past_the_start():
    """bars index the detected grid, so the search runs in index space and must
    not adopt a mark that begins later in the song."""
    from app.pipeline.click_render import count_in_beats_per_bar as _meter_at

    bars = [{"beat": 0, "beats_per_bar": 3}, {"beat": 100, "beats_per_bar": 7}]

    assert _meter_at(bars, 0, 4) == 3


def test_a_count_in_meter_defaults_to_four_without_a_usable_mark():
    from app.pipeline.click_render import count_in_beats_per_bar as _meter_at

    assert _meter_at([], 0, 0) == 4
    assert _meter_at([{"beat": 0, "beats_per_bar": 0}], 0, 0) == 4
    assert _meter_at([{"beat": 8, "beats_per_bar": 5}], 0, 0) == 4


def test_deleting_a_job_that_is_not_in_the_registry_is_not_found():
    from fastapi import HTTPException

    from app.api.jobs import delete_job

    with pytest.raises(HTTPException) as exc:
        delete_job("abcdefabcdef")

    assert exc.value.status_code == 404


def test_a_stem_whose_blocks_come_back_empty_still_scans(tmp_path, monkeypatch):
    """sf.blocks can yield a final zero-length block; taking min/max of it would
    raise on an empty slice."""
    from app.pipeline import audio_stats

    path = tmp_path / "tone.wav"
    t = np.linspace(0, 0.1, 800, endpoint=False)
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 8000)

    real_blocks = sf.blocks

    def _with_empty(*a, **kw):
        yield np.zeros((0, 1), dtype=np.float32)
        yield from real_blocks(*a, **kw)

    monkeypatch.setattr(audio_stats.sf, "blocks", _with_empty)

    peaks, rms = audio_stats.scan_stem(path, buckets=10)

    assert peaks and rms > 0
