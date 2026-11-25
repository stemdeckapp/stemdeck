"""MIDI from the piano and bass stems: the routes (app/api/midi.py), the run
(app/pipeline/midi.py), the settings, the zip option and the job lifecycle.

The worker is a stand-in script here (test_midi_worker.py runs the real one),
so each test says exactly how the run ends: a file, an error, a hang.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
import soundfile as sf

from app.api import midi as midi_api  # noqa: F401 -- imported before JOBS_DIR is patched
from app.core.models import Job, clean_midi
from app.core.process import process_exists
from app.core.registry import _jobs, _procs
from app.pipeline import midi as midi_run

JOB = "abcdef123456"
OTHER = "fedcba654321"


@pytest.fixture(autouse=True)
def _fresh_runs():
    midi_run._current = None
    midi_run._engine_broken = False
    midi_run._installed.cache_clear()
    yield
    run = midi_run._current
    if run is not None:
        run.cancel.set()
        deadline = time.monotonic() + 10
        while midi_run._current is not None and time.monotonic() < deadline:
            time.sleep(0.05)
    midi_run._current = None
    midi_run._engine_broken = False
    midi_run._installed.cache_clear()


@pytest.fixture
def jobs_dir():
    from app.core import config

    _procs.clear()
    yield config.JOBS_DIR
    _jobs.pop(JOB, None)
    _jobs.pop(OTHER, None)
    _procs.clear()


@pytest.fixture
def client(jobs_dir):
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


STUB = textwrap.dedent(
    """
    import json, subprocess, sys, time
    mode, out = sys.argv[1], sys.argv[2]
    def emit(**e):
        print(json.dumps(e), flush=True)
    emit(event="progress", pct=10)
    if mode == "ok":
        emit(event="progress", pct=50)
        with open(out, "wb") as f:
            f.write(b"MThd new file")
        emit(event="done", notes=7, seconds_audio=3.0)
    elif mode == "sleep":
        time.sleep(60)
    elif mode == "tree":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(sys.argv[3], "w").write(str(child.pid))
        time.sleep(60)
    elif mode == "disk":
        with open(out, "wb") as f:
            f.write(b"half")
        emit(event="error", code="disk")
        sys.exit(5)
    elif mode == "garbage":
        print("not a protocol line", flush=True)
    elif mode == "import":
        emit(event="error", code="engine", phase="import")
        sys.exit(4)
    elif mode == "no_file":
        emit(event="done", notes=3, seconds_audio=3.0)
    """
)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """Point the run at the stand-in. Set .mode before a POST; .calls has the
    (params, plan) of each run."""
    script = tmp_path / "stub_worker.py"
    script.write_text(STUB, encoding="utf-8")

    class Stub:
        mode = "ok"
        pid_file = tmp_path / "child.pid"
        calls: list = []

    def cmd(audio, out, stem, params, plan):
        Stub.calls.append((stem, params, plan))
        return [sys.executable, str(script), Stub.mode, str(out), str(Stub.pid_file)]

    monkeypatch.setattr(midi_run, "_spawn_worker_cmd", cmd)
    return Stub


def _wav(path: Path, seconds: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    sf.write(str(path), (rng.standard_normal(int(44100 * seconds)) * 0.05).astype("float32"), 44100)


def _song(jobs_dir: Path, job_id: str = JOB, **fields) -> Job:
    job_dir = jobs_dir / job_id
    for stem in ("piano", "bass", "vocals"):
        _wav(job_dir / "stems" / f"{stem}.wav")
    (job_dir / "metadata.json").write_text(json.dumps({"title": "Song"}), encoding="utf-8")
    job = Job(id=job_id, status="done", title="Song", source_url="local:Song", **fields)
    job.stems = [
        {"name": n, "url": f"/api/jobs/{job_id}/stems/{n}.wav"} for n in ("piano", "bass", "vocals")
    ]
    _jobs[job_id] = job
    return job


async def _settled(c: httpx.AsyncClient, stem: str = "piano", job_id: str = JOB) -> dict:
    for _ in range(400):
        state = (await c.get(f"/api/jobs/{job_id}")).json()
        entry = state["midi"].get(stem)
        if entry and entry["state"] != "running":
            return state
        await asyncio.sleep(0.05)
    raise AssertionError("the MIDI run never ended")


def _refused(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["code"] == code, r.text
    assert isinstance(r.json()["detail"], str)


# -- status


async def test_status_describes_the_engine(client):
    async with client as c:
        r = await c.get("/api/transcribe/status")
    assert r.status_code == 200
    assert r.json() == {
        "available": True,
        "reason": None,
        "engines": [
            {
                "id": "basic_pitch",
                "label": "Basic Pitch",
                "version": "0.4.0",
                "licence": "Apache-2.0",
            }
        ],
        "stems": ["piano", "bass"],
        "max_seconds": 1200,
        "details": ["balanced", "detailed", "clean"],
    }


async def test_status_on_intel_macos_is_unsupported(client, monkeypatch):
    monkeypatch.setattr(midi_run.sys, "platform", "darwin")
    monkeypatch.setattr(midi_run.platform, "machine", lambda: "x86_64")
    midi_run._installed.cache_clear()
    async with client as c:
        body = (await c.get("/api/transcribe/status")).json()
    assert (body["available"], body["reason"]) == (False, "unsupported_platform")
    # Apple Silicon is not Intel.
    monkeypatch.setattr(midi_run.platform, "machine", lambda: "arm64")
    midi_run._installed.cache_clear()
    assert midi_run.availability() == (True, None)


async def test_status_without_onnxruntime_is_no_runtime(client, monkeypatch):
    real = midi_run.importlib.util.find_spec
    monkeypatch.setattr(
        midi_run.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "onnxruntime" else real(name, *a),
    )
    midi_run._installed.cache_clear()
    async with client as c:
        body = (await c.get("/api/transcribe/status")).json()
    assert (body["available"], body["reason"]) == (False, "no_runtime")


def test_status_never_imports_the_engine_or_onnxruntime():
    before = set(sys.modules)
    midi_run._installed.cache_clear()
    midi_run.status()
    assert not {"onnxruntime", "basic_pitch", "mido"} & (set(sys.modules) - before)


# -- settings


async def test_settings_defaults_and_validation(client):
    async with client as c:
        body = (await c.get("/api/settings")).json()
        assert (body["transcribe_piano"], body["transcribe_bass"], body["transcribe_detail"]) == (
            "auto",
            "auto",
            "balanced",
        )
        r = await c.post(
            "/api/settings", json={"transcribe_bass": "off", "transcribe_detail": "detailed"}
        )
        assert r.status_code == 200
        assert (r.json()["transcribe_bass"], r.json()["transcribe_detail"]) == ("off", "detailed")
        for bad in (
            {"transcribe_piano": "on"},
            {"transcribe_piano": 1},
            {"transcribe_detail": "max"},
            {"transcribe_detail": None},
            # One bad value and nothing in the request is written.
            {"transcribe_piano": "off", "transcribe_detail": "nope"},
        ):
            r = await c.post("/api/settings", json=bad)
            assert r.status_code == 422, bad
            assert "must be one of" in r.json()["detail"]
        body = (await c.get("/api/settings")).json()
    assert (body["transcribe_piano"], body["transcribe_bass"], body["transcribe_detail"]) == (
        "auto",
        "off",
        "detailed",
    )


# -- a run


async def test_a_run_writes_the_file_and_serves_its_state(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        assert r.status_code == 202, r.text
        assert r.json() == {"state": "running"}
        state = await _settled(c)
        entry = state["midi"]["piano"]
        assert entry["state"] == "done" and entry["pct"] == 100 and entry["notes"] == 7
        assert entry["engine"] == "basic_pitch" and entry["detail"] == "balanced"
        assert entry["tempo_source"] == "default" and entry["error_code"] is None
        assert entry["has_file"] is True and entry["at"].endswith("Z")
        assert set(state["midi"]) == {"piano"}
        r = await c.get(f"/api/jobs/{JOB}/stems/piano.mid")
    assert r.status_code == 200 and r.content == b"MThd new file"
    assert r.headers["content-type"] == "audio/midi"
    assert 'filename="Song_piano.mid"' in r.headers["content-disposition"]
    stem, params, plan = worker.calls[-1]
    assert stem == "piano" and params["minimum_frequency"] == 27.5
    assert params["maximum_frequency"] == 4200.0 and params["minimum_note_length"] == 80.0
    info = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))
    record = info["stems"]["piano"]
    assert record["engine"] == "basic_pitch" and record["engine_version"] == "0.4.0"
    assert record["vendor_id"] and record["detail"] == "balanced" and record["notes"] == 7
    assert record["params"] == params and record["tempo_source"] == "default"
    assert record["bpm"] == 120.0 and record["seconds_audio"] == 3.0
    assert isinstance(record["seconds_run"], float) and record["created_at"].endswith("Z")
    # Kept where a restart and a registry rebuilt from the folder find it.
    meta = json.loads((jobs_dir / JOB / "metadata.json").read_text(encoding="utf-8"))
    assert meta["midi"]["piano"]["state"] == "done"
    registry = json.loads((jobs_dir / "registry.json").read_text(encoding="utf-8"))
    assert next(j for j in registry["jobs"] if j["id"] == JOB)["midi"]["piano"]["notes"] == 7
    assert not list((jobs_dir / JOB / "midi").glob(".*"))
    assert midi_run.current() is None


async def test_bass_uses_its_range_the_detail_asked_and_the_song_tempo(jobs_dir, client, worker):
    _song(jobs_dir)
    beats = [0.25 + 0.4 * i for i in range(50)]  # 150 BPM
    grid = {"beats": beats, "bars": [{"beat": 1, "beats_per_bar": 4}], "bpm": 150.0,
            "irregularity": 0.02, "confidence": 88}  # fmt: skip
    (jobs_dir / JOB / "stems" / "beats.json").write_text(json.dumps(grid), encoding="utf-8")
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass", "detail": "clean"})
        assert r.status_code == 202
        entry = (await _settled(c, "bass"))["midi"]["bass"]
    assert entry["tempo_source"] == "song" and entry["detail"] == "clean"
    _stem, params, plan = worker.calls[-1]
    assert (params["minimum_frequency"], params["maximum_frequency"]) == (30.0, 400.0)
    assert (params["onset_threshold"], params["frame_threshold"]) == (0.7, 0.5)
    assert plan["bpm"] == pytest.approx(150.0) and plan["bar_start"] == pytest.approx(0.65)


async def test_the_detail_setting_is_the_default(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        await c.post("/api/settings", json={"transcribe_detail": "detailed"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert entry["detail"] == "detailed"
    assert worker.calls[-1][1]["minimum_note_length"] == 58.0


# -- refusals


async def test_refusals(jobs_dir, client, worker, monkeypatch):
    job = _song(jobs_dir)
    async with client as c:
        for job_id in ("ffffffffffff", "not-a-job", "ABCDEF123456"):
            _refused(
                await c.post(f"/api/jobs/{job_id}/midi", json={"stem": "piano"}),
                404,
                "job_not_found",
            )
        for stem in ("vocals", "drums", "Piano", "../piano", "piano.mid"):
            _refused(
                await c.post(f"/api/jobs/{JOB}/midi", json={"stem": stem}),
                400,
                "stem_not_supported",
            )
        for detail in ("max", "BALANCED", "x" * 32):
            r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano", "detail": detail})
            _refused(r, 400, "bad_detail")
        for body in (
            {},
            {"stem": 1},
            {"stem": ""},
            {"stem": "p" * 33},
            {"stem": "piano", "detail": 3},
        ):
            assert (await c.post(f"/api/jobs/{JOB}/midi", json=body)).status_code == 422, body

        await c.post("/api/settings", json={"transcribe_bass": "off"})
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"}), 403, "disabled")

        job.duration_sec = 5000.0
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"}), 413, "too_long")
        job.duration_sec = None
        monkeypatch.setattr(midi_api, "TRANSCRIBE_MAX_SECONDS", 2)
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"}), 413, "too_long")
        monkeypatch.setattr(midi_api, "TRANSCRIBE_MAX_SECONDS", 1200)

        (jobs_dir / JOB / "stems" / "piano.wav").unlink()
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"}), 409, "no_stem")
        job.status = "error"
        await c.post("/api/settings", json={"transcribe_bass": "auto"})
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"}), 409, "no_stem")

        monkeypatch.setattr(midi_run, "availability", lambda: (False, "no_runtime"))
        _refused(
            await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"}), 503, "engine_unavailable"
        )
    assert worker.calls == []


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "separating"},
        {"status": "queued"},
        {"vocal_split": "running"},
        {"stem_fill": "running"},
        {"stem_reextract": {"stem": "guitar", "state": "running"}},
    ],
)
async def test_busy_while_the_song_is_worked_on(jobs_dir, client, worker, fields):
    job = _song(jobs_dir)
    for key, value in fields.items():
        setattr(job, key, value)
    async with client as c:
        _refused(await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"}), 409, "busy")
    assert worker.calls == []


async def test_one_run_at_a_time_across_the_server(jobs_dir, client, worker):
    _song(jobs_dir)
    _song(jobs_dir, OTHER)
    worker.mode = "sleep"
    async with client as c:
        assert (await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})).status_code == 202
        for job_id, stem in ((JOB, "piano"), (JOB, "bass"), (OTHER, "bass")):
            r = await c.post(f"/api/jobs/{job_id}/midi", json={"stem": stem})
            _refused(r, 409, "busy")
        # The song is busy for everything else that writes into it.
        assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 409
        _refused(await c.delete(f"/api/jobs/{JOB}/midi/piano"), 409, "busy")
        assert (await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")).json() == {"state": "cancelled"}
        assert (await c.post(f"/api/jobs/{OTHER}/midi", json={"stem": "bass"})).status_code == 202
        await c.post(f"/api/jobs/{OTHER}/midi/bass/cancel")


# -- endings that make no new file


async def _with_old_file(jobs_dir: Path, client, worker, mode: str) -> dict:
    """A song whose piano already has a MIDI file, and a second run that
    ends by ``mode``."""
    _song(jobs_dir)
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        await _settled(c)
        (jobs_dir / JOB / "midi" / "piano.mid").write_bytes(b"OLD")
        worker.mode = mode
        assert (await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})).status_code == 202
        if mode in ("sleep", "tree"):
            for _ in range(200):
                if (jobs_dir / JOB).exists() and ("abcdef123456", "midi") in _procs:
                    break
                await asyncio.sleep(0.05)
            if mode == "tree":
                for _ in range(200):
                    if worker.pid_file.is_file() and worker.pid_file.read_text():
                        break
                    await asyncio.sleep(0.05)
            r = await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")
            assert r.status_code == 200 and r.json() == {"state": "cancelled"}
        return await _settled(c)


async def test_cancel_keeps_the_previous_file_and_kills_the_tree(jobs_dir, client, worker):
    state = await _with_old_file(jobs_dir, client, worker, "tree")
    entry = state["midi"]["piano"]
    assert entry["state"] == "cancelled" and entry["error_code"] is None
    # The file and what it was written with: the first run's.
    assert entry["has_file"] is True and entry["notes"] == 7
    assert (jobs_dir / JOB / "midi" / "piano.mid").read_bytes() == b"OLD"
    assert not list((jobs_dir / JOB / "midi").glob(".*"))
    assert (JOB, "midi") not in _procs
    child = int(worker.pid_file.read_text())
    for _ in range(100):
        if not process_exists(child):
            break
        time.sleep(0.1)
    assert not process_exists(child), "the worker's child outlived the cancel"


@pytest.mark.parametrize(
    "mode,code", [("disk", "disk"), ("garbage", "engine"), ("no_file", "engine")]
)
async def test_a_failed_run_keeps_the_previous_file(jobs_dir, client, worker, mode, code):
    state = await _with_old_file(jobs_dir, client, worker, mode)
    entry = state["midi"]["piano"]
    assert entry["state"] == "error" and entry["error_code"] == code
    assert entry["has_file"] is True and entry["pct"] == 0
    assert (jobs_dir / JOB / "midi" / "piano.mid").read_bytes() == b"OLD"
    assert not list((jobs_dir / JOB / "midi").glob(".*"))


async def test_a_first_run_that_fails_has_no_file(jobs_dir, client, worker):
    _song(jobs_dir)
    worker.mode = "disk"
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})
        entry = (await _settled(c, "bass"))["midi"]["bass"]
        assert (entry["has_file"], entry["notes"], entry["error_code"]) == (False, None, "disk")
        _refused(await c.get(f"/api/jobs/{JOB}/stems/bass.mid"), 404, "no_midi")


async def test_an_engine_that_cannot_import_makes_the_status_say_so(jobs_dir, client, worker):
    _song(jobs_dir)
    worker.mode = "import"
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        assert (await _settled(c))["midi"]["piano"]["error_code"] == "engine"
        body = (await c.get("/api/transcribe/status")).json()
        assert (body["available"], body["reason"]) == (False, "engine_error")
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        _refused(r, 503, "engine_unavailable")


async def test_a_stall_is_stopped(jobs_dir, client, worker, monkeypatch):
    monkeypatch.setattr(midi_run, "TIMEOUT_MIDI_STALL", 0.5)
    _song(jobs_dir)
    worker.mode = "sleep"
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        assert (await _settled(c))["midi"]["piano"]["error_code"] == "stalled"


def test_a_run_saved_while_running_comes_back_interrupted():
    record = {
        "id": JOB,
        "status": "done",
        "midi": {
            "piano": {"state": "running", "pct": 40, "notes": 9, "detail": "balanced",
                      "tempo_source": "song", "at": "2026-11-25T10:00:00Z", "has_file": True},
            "bass": {"state": "done", "pct": 100, "notes": 3, "has_file": True},
            "drums": {"state": "done"},
            "vocals": "nonsense",
        },
    }  # fmt: skip
    job = Job.from_record(record)
    assert set(job.midi) == {"piano", "bass"}
    piano = job.midi["piano"]
    assert (piano["state"], piano["error_code"], piano["pct"]) == ("error", "interrupted", 0)
    assert piano["has_file"] is True and piano["notes"] == 9
    assert job.midi["bass"]["state"] == "done"
    assert not job.post_hoc_running()
    assert clean_midi({"piano": {"state": "running", "pct": float("nan"), "notes": True}}) == {
        "piano": {
            "state": "running", "pct": 0, "notes": None, "engine": "basic_pitch", "detail": None,
            "tempo_source": None, "at": None, "error_code": None, "has_file": False,
        }
    }  # fmt: skip


# -- removing it


async def test_delete_removes_the_file_and_its_record(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        for stem in ("piano", "bass"):
            await c.post(f"/api/jobs/{JOB}/midi", json={"stem": stem})
            await _settled(c, stem)
        r = await c.delete(f"/api/jobs/{JOB}/midi/piano")
        assert r.status_code == 200 and r.json() == {"deleted": "piano"}
        assert not (jobs_dir / JOB / "midi" / "piano.mid").exists()
        info = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))
        assert set(info["stems"]) == {"bass"}
        assert set((await c.get(f"/api/jobs/{JOB}")).json()["midi"]) == {"bass"}
        _refused(await c.delete(f"/api/jobs/{JOB}/midi/piano"), 404, "no_midi")
        _refused(await c.get(f"/api/jobs/{JOB}/stems/piano.mid"), 404, "no_midi")
        await c.delete(f"/api/jobs/{JOB}/midi/bass")
        assert not (jobs_dir / JOB / "midi").exists()
        assert not (jobs_dir / JOB / "midi.json").exists()
        assert (await c.get(f"/api/jobs/{JOB}")).json()["midi"] == {}


async def test_deleting_the_song_takes_its_midi(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        await _settled(c)
        assert (await c.delete(f"/api/jobs/{JOB}")).status_code == 200
    assert not (jobs_dir / JOB).exists()


async def test_traversal_and_unknown_names(jobs_dir, client, worker):
    _song(jobs_dir)
    (jobs_dir / "secret.mid").write_bytes(b"secret")
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        await _settled(c)
        for url in (
            f"/api/jobs/{JOB}/stems/..%2Fsecret.mid",
            f"/api/jobs/{JOB}/stems/..%2F..%2Fsecret.mid",
            f"/api/jobs/{JOB}/stems/vocals.mid",
            "/api/jobs/..%2F..%2Fetc/stems/piano.mid",
            "/api/jobs/" + "%2E%2E" + "/stems/piano.mid",
        ):
            r = await c.get(url)
            assert r.status_code == 404, url
            assert b"secret" not in r.content
        for url in (
            f"/api/jobs/{JOB}/midi/..%2Fpiano",
            f"/api/jobs/{JOB}/midi/vocals",
        ):
            # Refused, by the stem whitelist or by no route matching at all
            # (405 when the decoded path lands on another route's shape).
            assert (await c.delete(url)).status_code in (400, 404, 405), url
            assert (await c.post(url + "/cancel")).status_code in (400, 404, 405), url
        assert (jobs_dir / JOB / "midi" / "piano.mid").is_file()


async def test_cancel_with_nothing_running(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        assert (await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")).json() == {"state": None}
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        await _settled(c)
        assert (await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")).json() == {"state": "done"}
        _refused(await c.post("/api/jobs/ffffffffffff/midi/piano/cancel"), 404, "job_not_found")
        _refused(await c.post(f"/api/jobs/{JOB}/midi/drums/cancel"), 400, "stem_not_supported")


# -- the stems zip


async def test_the_zip_carries_midi_only_when_asked(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})
        await _settled(c, "bass")
        plain = await c.get(f"/api/jobs/{JOB}/stems/all.zip")
        with_midi = await c.get(f"/api/jobs/{JOB}/stems/all.zip?midi=true")
    assert plain.status_code == 200 and with_midi.status_code == 200, with_midi.text
    names = zipfile.ZipFile(io.BytesIO(plain.content)).namelist()
    assert not [n for n in names if n.endswith(".mid")]
    archive = zipfile.ZipFile(io.BytesIO(with_midi.content))
    assert set(archive.namelist()) == set(names) | {"Song_bass.mid"}
    assert "Song_bass.wav" in names
    assert archive.read("Song_bass.mid") == b"MThd new file"


# -- the tempo of a real song folder


# metadata.json as the pipeline writes it, every key of a real song in the
# repository's test library (data/fe-test/jobs/4d17b7d6d9ee), with its own
# values where the tempo could be read from: bpm 152 and tempo_stability 95
# are librosa's reading of the first 180 s, with no beat times.
REAL_METADATA = {
    "active_version": None,
    "artist": {"id": "Q151892", "name": "Ariana Grande", "englishName": "Ariana Grande"},
    "audio_tags": None,
    "auto_lyrics": False,
    "bpm": 152,
    "compute_device": "cuda",
    "created_at": 1795540442.0772781,
    "duration_sec": 229,
    "dynamic_range": 12.6,
    "favorite": None,
    "gpu_fallback": False,
    "has_video": False,
    "identity": None,
    "key": "C min",
    "key_confidence": 100,
    "loudness_remeasured": None,
    "loudness_version": 2,
    "lufs": -11.0,
    "peak_db": 1.6,
    "scale": "Natural Minor",
    "sections": None,
    "sections_source": None,
    "selected_stems": ["vocals", "guitar"],
    "separation_model": "htdemucs_6s",
    "separation_quality": "standard",
    "source_format": None,
    "source_url": "local:Song",
    "stage_timings": {"analyze": 25.5, "separate": 67.4, "beatgrid": 9.2},
    "stem_format": "mp3",
    "stem_gain": None,
    "stem_model_fallbacks": None,
    "stem_models": {
        "vocals": "kim_mel_roformer",
        "drums": "htdemucs_ft",
        "bass": "htdemucs_ft",
        "guitar": "htdemucs_6s",
        "piano": "htdemucs_6s",
    },
    "stem_mp3_kbps": 128,
    "stem_presence": {"vocals": 100, "drums": 46, "bass": 18, "guitar": 7, "piano": 7, "other": 72},
    "stem_revisions": None,
    "stem_undo": None,
    "tags": ["republic records"],
    "tempo_stability": 95,
    "thumbnail": None,
    "timeline_tags": None,
    "title": "Song",
    "trashed_at": None,
    "versions": None,
    "video_status": None,
    "vocal_split_wanted": True,
    "work": None,
}

# beats.json's fields as stems/beats.json holds them for three real songs in
# the test libraries, their own statistics: What Is This Feeling (tempo and
# meter change throughout, the grid starts at 44 s), Bohemian Rhapsody (eleven
# bar marks), and a steady 100.55 BPM song with one meter.
_IRREGULAR = {
    "bpm": 103.593, "confidence": 68, "irregularity": 0.6836, "interval_cv": 3.2662,
    "bars": [{"beat": 4, "beats_per_bar": 4}, {"beat": 9, "beats_per_bar": 2},
             {"beat": 21, "beats_per_bar": 4}, {"beat": 87, "beats_per_bar": 3},
             {"beat": 98, "beats_per_bar": 2}, {"beat": 102, "beats_per_bar": 4}],
    "beats": [44.087704, 44.683399, 45.856867, 47.84666, 48.242789, 48.631635,
              49.020481, 49.417018, 50.475, 51.396547, 52.181204, 52.978051],
}  # fmt: skip
_CHANGING = {
    "bpm": 139.246, "confidence": 62, "irregularity": 0.12, "interval_cv": 0.3639,
    "bars": [{"beat": 188, "beats_per_bar": 4}, {"beat": 319, "beats_per_bar": 2},
             {"beat": 376, "beats_per_bar": 3}],
    "beats": [0.031564 + 0.430891 * i for i in range(400)],
}  # fmt: skip
_STEADY = {
    "bpm": 100.552, "confidence": 95, "irregularity": 0.0209, "interval_cv": 0.02,
    "bars": [{"beat": 2, "beats_per_bar": 4}],
    "beats": [0.21 + 60 / 100.552 * i for i in range(380)],
}  # fmt: skip


@pytest.mark.parametrize(
    "grid,source,bpm",
    [(_IRREGULAR, "default", 120.0), (_CHANGING, "default", 120.0), (_STEADY, "song", 100.552)],
)
async def test_the_tempo_of_a_song_loaded_from_its_folder(
    jobs_dir, client, worker, grid, source, bpm
):
    """A job read back from a real-shaped metadata.json, as restore() reads
    it: Job.bpm is there, but only the beat grid decides the file's tempo."""
    _song(jobs_dir)
    job_dir = jobs_dir / JOB
    (job_dir / "metadata.json").write_text(json.dumps(REAL_METADATA), encoding="utf-8")
    job = Job.from_record({**REAL_METADATA, "id": JOB, "status": "done"})
    assert job.bpm == 152 and job.midi is None
    _jobs[JOB] = job
    full = {"version": 1, "source": "drums", "detector": "beat_this", "duration": 229.0,
            "refined": 200, "corrected": 0, "detected": 239, "extrapolated_head": 0,
            "extrapolated_tail": 0, "gap_filled": 0, "phase_shifted": False, "onsets": [],
            **grid}  # fmt: skip
    (job_dir / "stems" / "beats.json").write_text(json.dumps(full), encoding="utf-8")
    async with client as c:
        assert (await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})).status_code == 202
        entry = (await _settled(c, "bass"))["midi"]["bass"]
    assert entry["tempo_source"] == source
    plan = worker.calls[-1][2]
    assert plan["source"] == source and plan["bpm"] == pytest.approx(bpm, abs=0.01)
    info = json.loads((job_dir / "midi.json").read_text(encoding="utf-8"))["stems"]["bass"]
    assert info["tempo_source"] == source and info["bpm"] == pytest.approx(bpm, abs=0.01)
    if source == "song":
        assert plan["beats_per_bar"] == 4
        assert plan["bar_start"] == pytest.approx(0.21 + 2 * 60 / 100.552, abs=1e-5)
    # The run's state is kept in that metadata.json, beside what was there.
    meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["midi"]["bass"]["tempo_source"] == source and meta["bpm"] == 152
