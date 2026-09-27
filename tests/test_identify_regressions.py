"""Regression tests: song identification must never change what a job did
before it existed.

Identification (identify.py), the band and work lookups (artist_lookup.py,
work_lookup.py), the lyrics lookup (lyrics_lookup.py) and the Whisper stage
(transcribe.py) were added beside separation. These tests run the real
_run_common, the real lookup threads and the real transcription stage, with
only the heavy audio stages stubbed, and hold the pre-existing contract:

- a job still ends "done" with its stems, in the same stage order, whatever
  the services do: offline, timing out, erroring, answering garbage, hanging;
- Whisper missing or crashing costs nothing;
- cancel and delete while the lookups are still out leave nothing on disk and
  do not hold the queue;
- a re-split's inherited identity and lyrics survive every lookup failing.

No network: every service seam is replaced here, on top of conftest's own.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import app.api.jobs as jobs_mod
import app.pipeline.artist_lookup as al
import app.pipeline.identify as ident
import app.pipeline.lyrics_lookup as ll
import app.pipeline.musicbrainz as mb
import app.pipeline.transcribe as tr
from app.core import settings as _settings
from app.core.models import Job, JobCancelled
from app.core.registry import _jobs
from app.pipeline import jobqueue, runner
from app.pipeline.ratelimit import RateLimited
from app.pipeline.runner import run_local_pipeline

# A thread that dies of an exception is a lookup that stopped guarding itself.
pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")

TAGS = {"artist": "Queen", "title": "Bohemian Rhapsody", "album": "A Night at the Opera"}
TITLE = "Queen - Bohemian Rhapsody (Official Video)"
FOUND = ["bass", "drums", "vocals", "other"]
KEY = "Zx9Yw8Vu7T"
FINGERPRINT = "AQAA3UmUaEkSZSoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

# The stage_timings keys an upload recorded before identification, in order.
# identify_wait (formerly artist_wait), lyrics_wait and transcribe come after.
OLD_TIMINGS = ["prepare", "analyze", "separate", "post", "beatgrid", "sections"]
# The stage messages the runner itself set for an upload of a .wav, in order.
OLD_STAGES = ["Mixing tracks...", "Analyzing song structure...", "Done"]
# metadata.json keys written before identification (bc88527 _write_metadata).
OLD_METADATA_KEYS = {
    "title",
    "thumbnail",
    "audio_tags",
    "artist",
    "duration_sec",
    "bpm",
    "key",
    "scale",
    "key_confidence",
    "lufs",
    "peak_db",
    "dynamic_range",
    "tempo_stability",
    "stem_presence",
    "sections",
    "sections_source",
    "tags",
    "has_video",
    "video_status",
    "compute_device",
    "gpu_fallback",
    "stage_timings",
}


@pytest.fixture(autouse=True)
def _isolate_registry():
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()
    yield
    _jobs.clear()
    jobs_mod._TAG_LOOKUPS.clear()


@pytest.fixture
def jobs_dir() -> Path:
    """conftest's isolated JOBS_DIR, which the queue and the API both use."""
    return jobqueue.JOBS_DIR


@pytest.fixture
def stages(monkeypatch) -> list[str]:
    """Every stage message the runner and the transcription stage set."""
    seen: list[str] = []
    for module in (runner, tr):
        original = module._set

        def recording(job, _original=original, **fields):
            if "stage" in fields:
                seen.append(fields["stage"])
            _original(job, **fields)

        monkeypatch.setattr(module, "_set", recording)
    return seen


@pytest.fixture
def gate():
    """Holds every stubbed request until the test releases it. Released at
    teardown whatever happens, so no lookup thread outlives its test."""
    event = threading.Event()
    yield event
    event.set()
    for thread in threading.enumerate():
        if thread.name.startswith(("identify-", "lyrics-")):
            thread.join(5)


def _join_lookups(job_id: str) -> None:
    for thread in threading.enumerate():
        if thread.name in (f"identify-{job_id}", f"lyrics-{job_id}"):
            thread.join(5)
            assert not thread.is_alive(), f"{thread.name} never finished"


def _collect(job, stems_root, job_dir):
    stems = job_dir / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    for name in FOUND:
        (stems / f"{name}.wav").write_bytes(b"RIFF" + bytes(40))
    return list(FOUND)


@contextlib.contextmanager
def _heavy_stages(separate=None):
    """Everything but the lookups and the transcription stage stubbed."""
    with (
        patch("app.pipeline.runner.analyze"),
        patch(
            "app.pipeline.runner.separate",
            side_effect=separate or (lambda job, source, job_dir: job_dir / "model"),
        ),
        patch("app.pipeline.runner.collect", side_effect=_collect),
        patch("app.pipeline.runner.make_original_track", return_value=None),
        patch("app.pipeline.runner.make_selected_mix", return_value=None),
        patch("app.pipeline.runner.compute_stem_peaks", return_value={}),
        patch("app.pipeline.runner.compute_beat_grid"),
        patch("app.pipeline.runner.detect_sections", return_value=[]),
    ):
        yield


def _upload(jobs_dir: Path, job_id: str, **fields) -> Job:
    fields.setdefault("audio_tags", dict(TAGS))
    fields.setdefault("status", "processing")
    job = Job(
        id=job_id,
        title=TITLE,
        duration_sec=355.0,
        source_url=f"local:{TITLE}",
        source_format="wav",
        auto_sections=True,
        **fields,
    )
    (jobs_dir / job_id).mkdir(parents=True, exist_ok=True)
    (jobs_dir / job_id / "source.wav").write_bytes(b"RIFF" + bytes(40))
    _jobs[job.id] = job
    return job


def _assert_done_as_before(job: Job, jobs_dir: Path) -> None:
    job_dir = jobs_dir / job.id
    assert job.status == "done", (job.status, job.error, job.error_detail)
    assert job.stage_message == "Done"
    assert job.progress == 1.0
    assert job.error is None
    assert [s["name"] for s in job.stems] == FOUND
    assert all((job_dir / "stems" / f"{n}.wav").is_file() for n in FOUND)
    # The upload is kept for a re-split, exactly as before.
    assert (job_dir / "source.wav").is_file()
    timings = list(job.stage_timings)
    assert [k for k in timings if k in OLD_TIMINGS] == OLD_TIMINGS
    assert timings[: len(OLD_TIMINGS)] == OLD_TIMINGS, "the lookups came before a stage"
    meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert set(meta) >= OLD_METADATA_KEYS
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert [r["status"] for r in registry["jobs"] if r["id"] == job.id] == ["done"]


# ── every service failing ──


def _raiser(exc: BaseException):
    def fail(*args, **kwargs):
        raise exc

    return fail


def _answer(value):
    return lambda *args, **kwargs: value


FAILURES = {
    "offline": _raiser(OSError("network is unreachable")),
    "timeout": _raiser(TimeoutError("timed out")),
    "http-500": _raiser(urllib.error.HTTPError("https://example.invalid", 500, "boom", None, None)),
    "invalid-json": _raiser(json.JSONDecodeError("Expecting value", "<html>", 0)),
    "rate-limited": _raiser(RateLimited("next turn too far off")),
    "null": _answer(None),
    "list": _answer([]),
    "string": _answer("<html>Service Unavailable</html>"),
    "number": _answer(42),
    "wrong-shape": _answer(
        {
            "status": "ok",
            "results": "none",
            "recordings": "none",
            "releases": {"x": 1},
            "search": 5,
            "entities": [],
            "artist-credit": "Queen",
            "syncedLyrics": 7,
        }
    ),
    "list-of-junk": _answer([None, 3, "x", {"id": None}, {"duration": "long"}]),
}


def _every_service(monkeypatch, fetch) -> None:
    monkeypatch.setattr(mb, "_fetch_json", fetch)
    monkeypatch.setattr(ident, "_acoustid_request", fetch)
    monkeypatch.setattr(ll, "_fetch_json", fetch)
    monkeypatch.setattr(al, "_fetch_json", fetch)


@pytest.mark.parametrize("with_key", [False, True], ids=["no-key", "acoustid-key"])
@pytest.mark.parametrize("failure", sorted(FAILURES))
async def test_a_job_completes_as_before_whatever_the_services_do(
    monkeypatch, jobs_dir, stages, failure, with_key
):
    """Every lookup fails the same way. The job is done, with its stems, the
    old stage order and the old metadata, and no lyrics file claims to know
    anything. With a key, the fingerprint is taken and AcoustID fails too."""
    _every_service(monkeypatch, FAILURES[failure])
    if with_key:
        _settings.set_acoustid_api_key(KEY)
        monkeypatch.setattr(ident, "fingerprint", lambda audio, **kw: FINGERPRINT)
    job = _upload(jobs_dir, "abcdef00a001")
    started = time.monotonic()
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    elapsed = time.monotonic() - started
    _join_lookups(job.id)

    _assert_done_as_before(job, jobs_dir)
    assert stages == OLD_STAGES
    assert elapsed < 10
    assert job.artist is None
    assert job.work is None
    # Nothing checked it: at most the tags, as the file says.
    assert job.identity is None or job.identity["source"] == "tags"
    assert job.has_lyrics is False
    assert not (jobs_dir / job.id / "lyrics.json").exists()


async def test_a_job_with_nothing_to_identify_by_completes_as_before(jobs_dir, stages):
    """No tags, no key, a title naming nothing: no lookup starts at all."""
    job = _upload(jobs_dir, "abcdef00a002", audio_tags=None)
    job.title = "track 01"
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    _assert_done_as_before(job, jobs_dir)
    assert stages == OLD_STAGES
    assert (job.identity, job.artist, job.work, job.has_lyrics) == (None, None, None, False)


async def test_services_that_hang_do_not_hold_the_job_or_write_late(
    monkeypatch, jobs_dir, stages, gate
):
    """Every request hangs past the grace. The job finishes on time, and the
    answers that arrive afterwards are never written: not to the job, not
    beside its stems."""
    entered = threading.Event()

    def hang(*args, **kwargs):
        entered.set()
        gate.wait(10)
        return None

    _every_service(monkeypatch, hang)
    monkeypatch.setattr(runner, "IDENTIFY_GRACE_SEC", 0.2)
    monkeypatch.setattr(runner, "LYRICS_LOOKUP_GRACE_SEC", 0.2)
    monkeypatch.setattr(runner, "LYRICS_IDENTITY_WAIT_SEC", 0.2)
    job = _upload(jobs_dir, "abcdef00a003")
    started = time.monotonic()
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    elapsed = time.monotonic() - started
    assert entered.is_set(), "no lookup was out, so this proved nothing"

    _assert_done_as_before(job, jobs_dir)
    assert stages == OLD_STAGES
    assert elapsed < 3
    version = job.version
    meta = (jobs_dir / job.id / "metadata.json").read_bytes()
    listing = sorted(p.name for p in (jobs_dir / job.id).iterdir())

    gate.set()
    _join_lookups(job.id)
    assert job.version == version, "a late answer changed the job"
    assert (job.identity, job.artist, job.work, job.has_lyrics) == (None, None, None, False)
    assert (jobs_dir / job.id / "metadata.json").read_bytes() == meta
    assert sorted(p.name for p in (jobs_dir / job.id).iterdir()) == listing


# ── Whisper missing or crashing ──


def _worker(tmp_path: Path, script: str):
    path = tmp_path / "fake_whisper.py"
    path.write_text(script, encoding="utf-8")
    return lambda vocals, device: [sys.executable, str(path)]


WHISPER = {
    "exits": "raise SystemExit(3)",
    "crashes": "raise RuntimeError('CUDA error: out of memory')",
    "prints-junk": "print('not json at all')",
    "prints-two-answers": "print('{}')\nprint('{}')",
    "wrong-shape": "import json\nprint(json.dumps({'segments': 'nope', 'language': 5}))",
    "segments-of-junk": (
        "import json\nprint(json.dumps({'segments': [None, 3, {'words': 'x'},"
        " {'words': [{'word': 7, 'start': 'a', 'end': None}]}]}))"
    ),
}


@pytest.mark.parametrize("behaviour", sorted(WHISPER) + ["missing"])
async def test_whisper_failing_costs_the_job_nothing(
    monkeypatch, jobs_dir, stages, tmp_path, behaviour
):
    """Transcription on, a vocals stem and no lyrics anywhere, so the worker
    runs, and fails. The job is done as before, with no lyrics file."""
    _settings.set_transcribe_lyrics("on")
    if behaviour == "missing":
        missing = str(tmp_path / "no-such-whisper-worker")
        monkeypatch.setattr(tr, "_spawn_worker_cmd", lambda vocals, device: [missing])
    else:
        monkeypatch.setattr(tr, "_spawn_worker_cmd", _worker(tmp_path, WHISPER[behaviour]))
    job = _upload(jobs_dir, "abcdef00a004")
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    _join_lookups(job.id)

    _assert_done_as_before(job, jobs_dir)
    assert stages == [*OLD_STAGES[:-1], "Transcribing lyrics...", "Done"]
    assert job.has_lyrics is False
    assert not (jobs_dir / job.id / "lyrics.json").exists()


async def test_whisper_is_not_run_under_auto_on_the_cpu(monkeypatch, jobs_dir, stages):
    """The default: a CPU job never spawns a worker and sees no new stage."""
    spawned = []
    monkeypatch.setattr(
        tr, "_spawn_worker_cmd", lambda vocals, device: spawned.append(device) or ["x"]
    )
    job = _upload(jobs_dir, "abcdef00a005", compute_device="cpu")
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    _assert_done_as_before(job, jobs_dir)
    assert spawned == []
    assert stages == OLD_STAGES


# ── a re-split keeps what it inherited ──


async def test_a_resplit_keeps_its_inherited_identity_and_lyrics_when_every_lookup_fails(
    monkeypatch, jobs_dir, stages
):
    """A re-split arrives with its source's identity, band, work and lyrics.
    Nothing is asked, every service is broken anyway, Whisper would crash:
    the job is done and keeps all of it, byte for byte."""
    asked = []

    def broken(*args, **kwargs):
        asked.append(args)
        raise OSError("offline")

    _every_service(monkeypatch, broken)
    _settings.set_acoustid_api_key(KEY)
    _settings.set_transcribe_lyrics("on")
    spawned = []
    monkeypatch.setattr(tr, "_spawn_worker_cmd", lambda vocals, device: spawned.append(1) or ["x"])
    identity = {
        "source": "acoustid",
        "score": 0.97,
        "recording_mbid": "b1a9c0e9-d987-4042-ae91-78d6a3267d69",
        "title": "Bohemian Rhapsody",
        "artist": "Queen",
        "artist_mbids": [],
        "album": "A Night at the Opera",
        "release_group_mbid": None,
        "release_group_type": "Album",
        "secondary_types": [],
        "year": 1975,
        "duration": 355.0,
    }
    band = {"id": "Q15862", "name": "Queen", "englishName": "Queen"}
    work = {"id": "Q1", "kind": "film", "name": "Wayne's World", "englishName": "Wayne's World"}
    job = _upload(
        jobs_dir, "abcdef00a006", identity=identity, artist=band, work=work, has_lyrics=True
    )
    lyrics = jobs_dir / job.id / "lyrics.json"
    lyrics.write_text('{"v": 1, "source": "lrclib"}\n', encoding="utf-8")
    before = lyrics.read_bytes()
    with _heavy_stages():
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    _join_lookups(job.id)

    _assert_done_as_before(job, jobs_dir)
    assert stages == OLD_STAGES
    assert job.identity == identity
    assert job.artist == band
    assert job.work == work
    assert job.has_lyrics is True
    assert lyrics.read_bytes() == before
    assert spawned == []
    assert asked == [], "an inherited answer was asked for again"


# ── cancel and delete while the lookups are out ──


@contextlib.asynccontextmanager
async def _queue_and_api():
    from app.main import app

    task = jobqueue.start_worker()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            yield c
    finally:
        jobqueue.request_stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting")


def _hanging_services(monkeypatch, gate, entered):
    def hang(*args, **kwargs):
        entered.set()
        gate.wait(10)
        raise OSError("offline")

    _every_service(monkeypatch, hang)
    monkeypatch.setattr(runner, "IDENTIFY_GRACE_SEC", 0.2)
    monkeypatch.setattr(runner, "LYRICS_LOOKUP_GRACE_SEC", 0.2)
    monkeypatch.setattr(runner, "LYRICS_IDENTITY_WAIT_SEC", 0.2)


async def test_cancel_while_the_lookups_are_out_frees_the_queue(monkeypatch, jobs_dir, gate):
    """A is cancelled mid-separation with its lookups hanging. A ends
    cancelled with its directory gone, B starts and finishes, and once the
    lookups come back nothing reappears on disk or in the registry."""
    entered = threading.Event()
    _hanging_services(monkeypatch, gate, entered)
    separating = threading.Event()

    def separate(job, source, job_dir):
        if job.id == "abcdef00b001":
            separating.set()
            deadline = time.monotonic() + 10
            while not job.cancel_requested and time.monotonic() < deadline:
                time.sleep(0.01)
            raise JobCancelled()
        return job_dir / "model"

    a = _upload(jobs_dir, "abcdef00b001", status="queued")
    b = _upload(jobs_dir, "abcdef00b002", status="queued")
    with _heavy_stages(separate=separate):
        async with _queue_and_api() as client:
            jobqueue.enqueue(a.id)
            jobqueue.enqueue(b.id)
            await _until(lambda: separating.is_set() and entered.is_set())
            assert b.status == "queued"

            r = await client.post(f"/api/jobs/{a.id}/cancel")
            assert r.status_code == 200
            await _until(lambda: a.status == "cancelled")
            assert not (jobs_dir / a.id).exists()

            await _until(lambda: b.status == "done")
            state = (await client.get(f"/api/jobs/{a.id}")).json()
            assert state["status"] == "cancelled"
            r = await client.delete(f"/api/jobs/{a.id}")
            assert r.status_code == 200

            gate.set()
            _join_lookups(a.id)
            _join_lookups(b.id)
            assert (await client.get(f"/api/jobs/{a.id}")).status_code == 404

    assert not (jobs_dir / a.id).exists(), "a late lookup recreated the deleted job"
    assert a.identity is None and a.has_lyrics is False
    _assert_done_as_before(b, jobs_dir)
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert a.id not in {r["id"] for r in registry["jobs"]}


async def test_delete_a_done_job_while_its_lookups_are_still_out(monkeypatch, jobs_dir, gate):
    """Done past the grace with every lookup still hanging, then deleted.
    Nothing the lookups do afterwards brings a file or a record back."""
    entered = threading.Event()
    _hanging_services(monkeypatch, gate, entered)
    job = _upload(jobs_dir, "abcdef00b003", status="queued")
    with _heavy_stages():
        async with _queue_and_api() as client:
            jobqueue.enqueue(job.id)
            await _until(lambda: job.status == "done")
            assert entered.is_set()
            assert any(t.name == f"identify-{job.id}" for t in threading.enumerate())

            r = await client.delete(f"/api/jobs/{job.id}")
            assert r.status_code == 200
            assert not (jobs_dir / job.id).exists()

            gate.set()
            _join_lookups(job.id)
            assert (await client.get(f"/api/jobs/{job.id}")).status_code == 404
            listed = (await client.get("/api/jobs")).json()
            assert job.id not in {j["job_id"] for j in listed}

    assert not (jobs_dir / job.id).exists()
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert job.id not in {r["id"] for r in registry["jobs"]}


async def test_delete_while_the_tag_backfill_is_identifying(monkeypatch, jobs_dir, gate):
    """An older track is being identified by the backfill when it is deleted.
    The backfill answers "nothing", and writes nothing into the deleted job."""
    entered = threading.Event()
    _hanging_services(monkeypatch, gate, entered)
    job = Job(
        id="abcdef00b004",
        status="done",
        title=TITLE,
        duration_sec=355.0,
        source_url="local:Queen - Bohemian Rhapsody",
        audio_tags=dict(TAGS),
    )
    _jobs[job.id] = job
    (jobs_dir / job.id / "stems").mkdir(parents=True)
    (jobs_dir / job.id / "stems" / "vocals.wav").write_bytes(b"RIFF")
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        backfill = asyncio.create_task(client.post(f"/api/jobs/{job.id}/audio-tags"))
        await _until(entered.is_set)
        r = await client.delete(f"/api/jobs/{job.id}")
        assert r.status_code == 200
        gate.set()
        answer = await asyncio.wait_for(backfill, 30)

    assert answer.status_code == 200
    body = answer.json()
    assert body["audio_tags"] is None and body["artist"] is None
    assert not (jobs_dir / job.id).exists(), "the backfill wrote into a deleted job"
    assert job.id not in _jobs


async def test_cancel_during_the_lookup_wait_is_a_cancel_not_a_done(monkeypatch, jobs_dir, gate):
    """A cancel that lands once separation is over, while the job waits out
    the lookups' grace: the job ends cancelled with its directory gone, as a
    cancel at the last stage always did, not done with its stems."""
    entered = threading.Event()
    _hanging_services(monkeypatch, gate, entered)
    monkeypatch.setattr(runner, "IDENTIFY_GRACE_SEC", 1.0)
    job = _upload(jobs_dir, "abcdef00b005")

    def last_stage(job, stems_dir, duration):
        job.cancel_requested = True
        return []

    with (
        _heavy_stages(),
        patch("app.pipeline.runner.detect_sections", side_effect=last_stage),
    ):
        await run_local_pipeline(job, jobs_dir / job.id / "source.wav", jobs_dir)
    assert entered.is_set()
    gate.set()
    _join_lookups(job.id)

    assert job.status == "cancelled"
    assert job.stage_message == "Cancelled"
    assert not (jobs_dir / job.id).exists()
    assert (job.identity, job.artist, job.work, job.has_lyrics) == (None, None, None, False)


async def test_cancel_during_transcription_frees_the_queue(monkeypatch, jobs_dir, tmp_path):
    """A is transcribing, the new last stage, when it is cancelled. The
    worker is stopped, A ends cancelled with its directory gone, and B runs."""
    _settings.set_transcribe_lyrics("on")
    monkeypatch.setattr(tr, "_spawn_worker_cmd", _worker(tmp_path, "import time\ntime.sleep(60)"))
    procs = []
    real_set_proc = tr.set_proc

    def set_proc(job_id, proc):
        if proc is not None:
            procs.append(proc)
        real_set_proc(job_id, proc)

    monkeypatch.setattr(tr, "set_proc", set_proc)
    a = _upload(jobs_dir, "abcdef00b006", status="queued")
    b = _upload(jobs_dir, "abcdef00b007", status="queued", audio_tags=None)
    b.title = "track 02"
    with _heavy_stages():
        async with _queue_and_api() as client:
            jobqueue.enqueue(a.id)
            jobqueue.enqueue(b.id)
            await _until(lambda: a.stage_message == "Transcribing lyrics..." and procs)
            started = time.monotonic()
            r = await client.post(f"/api/jobs/{a.id}/cancel")
            assert r.status_code == 200
            await _until(lambda: a.status == "cancelled")
            assert time.monotonic() - started < 10
            # B has no lyrics anywhere either, so it transcribes too, and
            # that worker would sleep; B is only asked to have started.
            await _until(lambda: b.status == "processing" and b.stage_timings is not None)
            await client.post(f"/api/jobs/{b.id}/cancel")
            await _until(lambda: b.status == "cancelled")

    assert procs[0].poll() is not None, "the Whisper worker outlived the cancel"
    assert not (jobs_dir / a.id).exists()
    assert "separate" in b.stage_timings, "the next job never ran"
