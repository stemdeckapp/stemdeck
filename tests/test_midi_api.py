"""MIDI from a song's stems: the routes (app/api/midi.py), the run
(app/pipeline/midi.py), the settings, the zip option and the job lifecycle.

The worker is a stand-in script here (test_midi_worker.py runs the real one),
so each test says exactly how the run ends: a file, an error, a hang.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from app.core.config import YMT3_SECONDS_PER_MINUTE
from app.core.models import Job, clean_midi
from app.core.process import process_exists
from app.core.registry import _jobs, _procs
from app.pipeline import midi as midi_run

JOB = "abcdef123456"
OTHER = "fedcba654321"


@pytest.fixture(autouse=True)
def _fresh_runs():
    midi_run._current = None
    midi_run._engine_broken.clear()
    midi_run.forget_installed()
    yield
    run = midi_run._current
    if run is not None:
        run.cancel.set()
        deadline = time.monotonic() + 10
        while midi_run._current is not None and time.monotonic() < deadline:
            time.sleep(0.05)
    midi_run._current = None
    midi_run._engine_broken.clear()
    midi_run.forget_installed()


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

    def cmd(audio, out, stem, params, plan, engine="basic_pitch", device="cpu"):
        Stub.calls.append((stem, params, plan))
        return [sys.executable, str(script), Stub.mode, str(out), str(Stub.pid_file)]

    monkeypatch.setattr(midi_run, "_spawn_worker_cmd", cmd)
    # The stand-in plays Basic Pitch: the piano's and the bass's default is
    # YourMT3+, whose first run would download its model first.
    from app.core.settings import set_midi_setting

    set_midi_setting("transcribe_piano_engine", "basic_pitch")
    set_midi_setting("transcribe_bass_engine", "basic_pitch")
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


async def _saved_meta(job_dir: Path, stem: str, entry: dict) -> dict:
    """metadata.json once the run's state is saved in it. The page sees the end
    of a run first; the API task saves it to disk just after (app/api/midi.py
    _persist), so reading the file at once raced that save."""
    meta: dict = {}
    for _ in range(200):
        meta = json.loads((job_dir / "metadata.json").read_text(encoding="utf-8"))
        if (meta.get("midi") or {}).get(stem) == entry:
            return meta
        await asyncio.sleep(0.025)
    raise AssertionError(f"metadata.json never held the {stem} run: {meta.get('midi')}")


def _refused(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.json()["code"] == code, r.text
    assert isinstance(r.json()["detail"], str)


# -- status


async def test_status_describes_the_engines(client):
    async with client as c:
        r = await c.get("/api/transcribe/status")
    assert r.status_code == 200
    # The device a torch engine would ask for here (no CUDA in CI).
    device = midi_run.torch_engine_device()
    assert r.json() == {
        "available": True,
        "reason": None,
        "engines": [
            {
                "id": "basic_pitch",
                "label": "Basic Pitch",
                "version": "0.4.0",
                "licence": "Apache-2.0",
                "model_licence": "Apache-2.0",
                "stems": ["vocals", "bass", "guitar", "piano", "other"],
                "available": True,
                "reason": None,
                "source": "built_in",
                "model": "built_in",
                "download_bytes": None,
                "disk_bytes": None,
                "device": "cpu",
                "seconds_per_minute": None,
                "uses_detail": True,
            },
            {
                "id": "piano_hr",
                "label": "ByteDance Piano Transcription",
                "version": "0.0.6",
                "licence": "MIT",
                "model_licence": "CC BY 4.0",
                "stems": ["piano"],
                "available": True,
                "reason": None,
                "source": "built_in",
                # The test library's models folder never has it.
                "model": "not_downloaded",
                "download_bytes": 171_966_578,
                "disk_bytes": 171_966_578,
                "device": device,
                "seconds_per_minute": None,
                "uses_detail": True,
            },
            {
                "id": "ymt3",
                "label": "YourMT3+",
                "version": "5e66c1e",
                "licence": "Apache-2.0",
                "model_licence": "Apache-2.0",
                "stems": ["drums", "bass", "guitar", "piano", "other"],
                "available": True,
                "reason": None,
                "source": "built_in",
                "model": "not_downloaded",
                # The checkpoint as published; what stays is its weights.
                "download_bytes": 561_544_628,
                "disk_bytes": 188_412_704,
                "device": device,
                "seconds_per_minute": dict(YMT3_SECONDS_PER_MINUTE),
                "uses_detail": False,
            },
        ],
        "stem_engines": {
            "vocals": ["basic_pitch"],
            "drums": ["ymt3"],
            "bass": ["basic_pitch", "ymt3"],
            "guitar": ["basic_pitch", "ymt3"],
            "piano": ["basic_pitch", "piano_hr", "ymt3"],
            "other": ["basic_pitch", "ymt3"],
        },
        "default_engines": {
            "vocals": "basic_pitch",
            "drums": "ymt3",
            "bass": "ymt3",
            "guitar": "basic_pitch",
            "piano": "ymt3",
            "other": "ymt3",
        },
        "stems": ["vocals", "drums", "bass", "guitar", "piano", "other"],
        "max_seconds": 1200,
        "details": ["balanced", "detailed", "clean"],
    }


async def test_status_on_intel_macos_is_unsupported(client, monkeypatch):
    monkeypatch.setattr(midi_run.sys, "platform", "darwin")
    monkeypatch.setattr(midi_run.platform, "machine", lambda: "x86_64")
    midi_run.forget_installed()
    async with client as c:
        body = (await c.get("/api/transcribe/status")).json()
    assert (body["available"], body["reason"]) == (False, "unsupported_platform")
    # Apple Silicon is not Intel.
    monkeypatch.setattr(midi_run.platform, "machine", lambda: "arm64")
    midi_run.forget_installed()
    assert midi_run.availability() == (True, None)


async def test_status_without_onnxruntime_is_no_runtime(client, monkeypatch):
    real = midi_run.importlib.util.find_spec
    monkeypatch.setattr(
        midi_run.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "onnxruntime" else real(name, *a),
    )
    midi_run.forget_installed()
    async with client as c:
        body = (await c.get("/api/transcribe/status")).json()
    assert (body["available"], body["reason"]) == (False, "no_runtime")


def test_status_never_imports_the_engine_or_onnxruntime():
    before = set(sys.modules)
    midi_run.forget_installed()
    midi_run.status()
    assert not {
        "onnxruntime",
        "basic_pitch",
        "mido",
        "piano_transcription_inference",
        "torchlibrosa",
        "model.ymt3",
        "transformers",
        "pytorch_lightning",
    } & (set(sys.modules) - before)


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
    assert params["maximum_frequency"] == 4200.0 and params["minimum_note_length"] == 58.0
    assert params["one_voice"] is False
    info = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))
    record = info["stems"]["piano"]
    assert record["engine"] == "basic_pitch" and record["engine_version"] == "0.4.0"
    assert record["vendor_id"] and record["detail"] == "balanced" and record["notes"] == 7
    assert record["params"] == params and record["tempo_source"] == "default"
    assert record["bpm"] == 120.0 and record["seconds_audio"] == 3.0
    assert isinstance(record["seconds_run"], float) and record["created_at"].endswith("Z")
    # Kept where a restart and a registry rebuilt from the folder find it.
    meta = await _saved_meta(jobs_dir / JOB, "piano", state["midi"]["piano"])
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
    assert (params["onset_threshold"], params["frame_threshold"]) == (0.8, 0.5)
    # A bass line is one note at a time (midi_notes.one_voice).
    assert params["one_voice"] is True
    assert plan["bpm"] == pytest.approx(150.0) and plan["bar_start"] == pytest.approx(0.65)


async def test_the_detail_setting_is_the_default(jobs_dir, client, worker):
    _song(jobs_dir)
    async with client as c:
        await c.post("/api/settings", json={"transcribe_detail": "detailed"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert entry["detail"] == "detailed"
    assert worker.calls[-1][1]["frame_threshold"] == 0.2


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
        for stem in ("lead_backing", "mix", "Piano", "../piano", "piano.mid", "DRUMS"):
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
            "drums": {"state": "done", "engine": "ymt3"},
            "vocals": "nonsense",
            "lead_backing": {"state": "done"},
        },
    }  # fmt: skip
    job = Job.from_record(record)
    # Every stem of htdemucs_6s, and only those.
    assert set(job.midi) == {"piano", "bass", "drums"}
    assert job.midi["drums"]["engine"] == "ymt3"
    piano = job.midi["piano"]
    assert (piano["state"], piano["error_code"], piano["pct"]) == ("error", "interrupted", 0)
    assert piano["has_file"] is True and piano["notes"] == 9
    assert job.midi["bass"]["state"] == "done"
    assert not job.post_hoc_running()
    assert clean_midi({"piano": {"state": "running", "pct": float("nan"), "notes": True}}) == {
        "piano": {
            "state": "running", "pct": 0, "phase": None, "notes": None, "engine": "basic_pitch",
            "detail": None,
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
            f"/api/jobs/{JOB}/midi/lead_backing",
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
        _refused(
            await c.post(f"/api/jobs/{JOB}/midi/lead_backing/cancel"), 400, "stem_not_supported"
        )


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


async def test_an_audio_and_midi_download_of_one_stem_carries_its_own_file_only(
    jobs_dir, client, worker
):
    _song(jobs_dir)
    async with client as c:
        for stem in ("bass", "piano"):
            await c.post(f"/api/jobs/{JOB}/midi", json={"stem": stem})
            await _settled(c, stem)
        one = await c.get(f"/api/jobs/{JOB}/stems/all.zip?stems=bass&midi=1")
        every = await c.get(f"/api/jobs/{JOB}/stems/all.zip?midi=1")
    assert one.status_code == 200, one.text
    assert sorted(zipfile.ZipFile(io.BytesIO(one.content)).namelist()) == [
        "Song_bass.mid",
        "Song_bass.wav",
    ]
    names = set(zipfile.ZipFile(io.BytesIO(every.content)).namelist())
    assert {"Song_bass.mid", "Song_piano.mid"} <= names


# -- the notes of a file (the piano roll)


def _smf_notes() -> bytes:
    """A format 0 file at 120 BPM, 480 ticks per quarter: A2 from 0.5 s to 1 s
    at velocity 90, then C3 from 1 s to 1.25 s at velocity 40."""
    track = bytes(
        [0x00, 0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20]
        + [0x83, 0x60, 0x90, 45, 90]
        + [0x83, 0x60, 0x80, 45, 0]
        + [0x00, 0x90, 48, 40]
        + [0x81, 0x70, 0x80, 48, 0]
        + [0x00, 0xFF, 0x2F, 0x00]
    )
    head = b"MThd" + bytes([0, 0, 0, 6, 0, 0, 0, 1, 0x01, 0xE0])
    return head + b"MTrk" + len(track).to_bytes(4, "big") + track


async def test_the_notes_route_reads_the_file(jobs_dir, client):
    job = _song(jobs_dir)
    folder = jobs_dir / JOB / "midi"
    folder.mkdir(parents=True)
    (folder / "bass.mid").write_bytes(_smf_notes())
    job.midi = {
        "bass": {
            "state": "done",
            "pct": 100,
            "notes": 2,
            "engine": "basic_pitch",
            "detail": "balanced",
            "tempo_source": "song",
            "at": "2026-11-25T10:00:00Z",
            "error_code": None,
            "has_file": True,
        }
    }
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/midi/bass/notes")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-cache"
    body = r.json()
    assert body["notes"] == [[0.5, 1.0, 45, 90], [1.0, 1.25, 48, 40]]
    assert body["bpm"] == 120.0
    assert body["tempo_source"] == "song"


def _bass_midi(jobs_dir: Path, job: Job) -> None:
    folder = jobs_dir / JOB / "midi"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bass.mid").write_bytes(_smf_notes())
    job.midi = {
        "bass": {
            "state": "done",
            "pct": 100,
            "notes": 2,
            "engine": "basic_pitch",
            "detail": "balanced",
            "tempo_source": "song",
            "at": "2026-11-25T10:00:00Z",
            "error_code": None,
            "has_file": True,
        }
    }


def _rms0(path: Path) -> float:
    data, _ = sf.read(str(path), dtype="float32", always_2d=True)
    return float(np.sqrt(np.mean(data[:, 0].astype(np.float64) ** 2)))


@pytest.fixture
def scans(monkeypatch):
    """Every stem the level code measures, by file name."""
    from app.pipeline import stem_level

    seen: list[str] = []
    real = stem_level.scan_stem

    def counting(path, *a, **k):
        seen.append(Path(path).name)
        return real(path, *a, **k)

    monkeypatch.setattr(stem_level, "scan_stem", counting)
    return seen


async def test_the_notes_answer_the_stems_level_measured_once_and_kept(jobs_dir, client, scans):
    """A song separated before the level was kept: the stem is measured the
    first time (the chunked engine has no other way to match the MIDI sound
    to it), then kept in the record and in metadata.json."""
    job = _song(jobs_dir)
    _bass_midi(jobs_dir, job)
    want = _rms0(jobs_dir / JOB / "stems" / "bass.wav")
    async with client as c:
        first = (await c.get(f"/api/jobs/{JOB}/midi/bass/notes")).json()
        second = (await c.get(f"/api/jobs/{JOB}/midi/bass/notes")).json()
    assert first["stem_rms"] == pytest.approx(want, rel=1e-6)
    assert second["stem_rms"] == first["stem_rms"]
    assert scans == ["bass.wav"]
    assert job.stem_rms["bass"]["rms"] == first["stem_rms"]
    meta = json.loads((jobs_dir / JOB / "metadata.json").read_text(encoding="utf-8"))
    assert meta["stem_rms"] == job.stem_rms
    assert meta["title"] == "Song"


async def test_the_notes_use_the_kept_level_of_the_file_on_disk(jobs_dir, client, scans):
    from app.pipeline import stem_level

    job = _song(jobs_dir)
    _bass_midi(jobs_dir, job)
    path = jobs_dir / JOB / "stems" / "bass.wav"
    job.stem_rms = {"bass": stem_level.level_record(path, 0.0123)}
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/midi/bass/notes")
    assert r.json()["stem_rms"] == 0.0123
    assert scans == []


async def test_a_replaced_stem_is_measured_again(jobs_dir, client, scans):
    """The kept value belongs to the file it was measured on: a stem replaced
    since (re-extracted, undone, stored as MP3) is measured again, never
    matched to the old file's level."""
    from app.pipeline import stem_level

    job = _song(jobs_dir)
    _bass_midi(jobs_dir, job)
    path = jobs_dir / JOB / "stems" / "bass.wav"
    job.stem_rms = {
        "bass": stem_level.level_record(path, 0.0123),
        "piano": {"rms": "damaged"},
    }
    _wav(path, seconds=4.0)
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/midi/bass/notes")
    assert r.json()["stem_rms"] == pytest.approx(_rms0(path), rel=1e-6)
    assert scans == ["bass.wav"]
    # The damaged entry is dropped as the new one is kept.
    assert set(job.stem_rms) == {"bass"}


async def test_the_notes_without_a_stem_file_have_no_level(jobs_dir, client, scans):
    job = _song(jobs_dir)
    _bass_midi(jobs_dir, job)
    (jobs_dir / JOB / "stems" / "bass.wav").unlink()
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/midi/bass/notes")
    assert r.status_code == 200
    assert r.json()["stem_rms"] is None
    assert scans == []
    assert job.stem_rms is None


async def test_the_notes_route_refuses_like_the_file_route(jobs_dir, client, caplog):
    _song(jobs_dir)
    (jobs_dir / "secret.mid").write_bytes(_smf_notes())
    async with client as c:
        _refused(await c.get(f"/api/jobs/{JOB}/midi/bass/notes"), 404, "no_midi")
        _refused(await c.get(f"/api/jobs/{JOB}/midi/vocals/notes"), 404, "no_midi")
        _refused(await c.get(f"/api/jobs/{OTHER}/midi/bass/notes"), 404, "job_not_found")
        r = await c.get(f"/api/jobs/{JOB}/midi/..%2F..%2Fsecret/notes")
        assert r.status_code == 404
        assert (await c.get("/api/jobs/..%2Fetc/midi/bass/notes")).status_code == 404
        folder = jobs_dir / JOB / "midi"
        folder.mkdir(parents=True)
        (folder / "piano.mid").write_bytes(b"MThd new file")
        _refused(await c.get(f"/api/jobs/{JOB}/midi/piano/notes"), 404, "no_midi")
    assert "could not read the piano MIDI file" in caplog.text


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
    meta = await _saved_meta(job_dir, "bass", entry)
    assert meta["midi"]["bass"]["tempo_source"] == source and meta["bpm"] == 152


# -- the second piano engine (piano_hr): its setting, its checkpoint, its run

CKPT = b"a stand-in for the piano_hr checkpoint " * 32
CKPT_SHA = hashlib.sha256(CKPT).hexdigest()

FETCH_STUB = textwrap.dedent(
    """
    import json, sys, time
    mode, dst, body = sys.argv[1], sys.argv[2], sys.argv[3]
    def emit(**e):
        print(json.dumps(e), flush=True)
    emit(event="progress", pct=0)
    if mode == "ok":
        emit(event="progress", pct=60)
        open(dst, "wb").write(open(body, "rb").read())
        emit(event="progress", pct=100)
        emit(event="done")
    elif mode == "wrong":
        open(dst, "wb").write(b"not the pinned bytes at all")
        emit(event="done")
    elif mode == "fail":
        print("model download failed: no route to host", file=sys.stderr, flush=True)
        emit(event="error", code="download")
        sys.exit(3)
    elif mode == "sleep":
        time.sleep(60)
    """
)


@pytest.fixture
def pinned(monkeypatch):
    """The pinned checkpoint is CKPT: its size and SHA-256 are what a run
    checks, and what midi.json records."""
    from app.pipeline import piano_hr

    monkeypatch.setattr(piano_hr, "PIANO_HR_CHECKPOINT_SIZE", len(CKPT))
    monkeypatch.setattr(piano_hr, "PIANO_HR_CHECKPOINT_SHA256", CKPT_SHA)
    monkeypatch.setattr(midi_run, "PIANO_HR_CHECKPOINT_SHA256", CKPT_SHA)
    return piano_hr.checkpoint_path()


@pytest.fixture
def fetch(tmp_path, monkeypatch, pinned):
    """The checkpoint's download step as a stand-in. Set .mode before a run;
    .calls counts the downloads started."""
    script = tmp_path / "stub_fetch.py"
    script.write_text(FETCH_STUB, encoding="utf-8")
    body = tmp_path / "ckpt.bin"
    body.write_bytes(CKPT)

    class Stub:
        mode = "ok"
        calls: list = []

    def cmd(dst, *pinned):
        Stub.calls.append(dst)
        return [sys.executable, str(script), Stub.mode, str(dst), str(body)]

    monkeypatch.setattr(midi_run, "_spawn_fetch_cmd", cmd)
    return Stub


@pytest.fixture
def hr_worker(tmp_path, monkeypatch):
    """A stand-in worker that records (stem, engine, device) per spawn and
    ends as .modes says for that device: "ok", "engine" (fails), "cpu_engine"
    (says it chose the CPU, then fails)."""
    script = tmp_path / "stub_hr_worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            mode, out, device = sys.argv[1], sys.argv[2], sys.argv[3]
            def emit(**e):
                print(json.dumps(e), flush=True)
            if mode == "cpu_engine":
                emit(event="progress", pct=2, device="cpu")
                emit(event="error", code="engine")
                sys.exit(4)
            emit(event="progress", pct=10)
            if mode == "engine":
                print("CUDA error: an illegal memory access", file=sys.stderr, flush=True)
                emit(event="error", code="engine")
                sys.exit(4)
            open(out, "wb").write(b"MThd piano_hr")
            emit(event="done", notes=11, seconds_audio=3.0, device=device)
            """
        ),
        encoding="utf-8",
    )

    class Stub:
        modes = {"cpu": "ok", "cuda": "ok"}
        calls: list = []

    def cmd(audio, out, stem, params, plan, engine="basic_pitch", device="cpu"):
        Stub.calls.append((stem, engine, device, params))
        return [sys.executable, str(script), Stub.modes[device], str(out), device]

    monkeypatch.setattr(midi_run, "_spawn_worker_cmd", cmd)
    monkeypatch.setattr(midi_run, "torch_engine_device", lambda: "cpu")
    return Stub


async def test_the_piano_engine_setting(client):
    async with client as c:
        # YourMT3+ until the person picks another (config.MIDI_DEFAULT_ENGINES).
        assert (await c.get("/api/settings")).json()["transcribe_piano_engine"] == "ymt3"
        r = await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        assert r.status_code == 200 and r.json()["transcribe_piano_engine"] == "piano_hr"
        for bad in (
            {"transcribe_piano_engine": "auto"},
            {"transcribe_piano_engine": "bytedance"},
            {"transcribe_piano_engine": None},
            {"transcribe_piano_engine": 1},
            # One bad value and nothing in the request is written.
            {"transcribe_piano_engine": "basic_pitch", "transcribe_detail": "nope"},
        ):
            r = await c.post("/api/settings", json=bad)
            assert r.status_code == 422, bad
            assert "must be one of" in r.json()["detail"]
        body = (await c.get("/api/settings")).json()
    assert body["transcribe_piano_engine"] == "piano_hr"


async def test_the_status_says_whether_the_checkpoint_is_there(client, pinned):
    async with client as c:
        engines = (await c.get("/api/transcribe/status")).json()["engines"]
        assert engines[1]["model"] == "not_downloaded"
        pinned.parent.mkdir(parents=True)
        pinned.write_bytes(CKPT[:10])
        engines = (await c.get("/api/transcribe/status")).json()["engines"]
        assert engines[1]["model"] == "partial"
        pinned.write_bytes(CKPT)
        engines = (await c.get("/api/transcribe/status")).json()["engines"]
    assert engines[1]["model"] == "downloaded"


async def test_piano_hr_downloads_its_checkpoint_then_makes_the_file(
    jobs_dir, client, hr_worker, fetch, pinned, monkeypatch
):
    _song(jobs_dir)
    phases: list = []
    real = midi_run._update

    def watch(job, stem, **fields):
        if "phase" in fields:
            phases.append(fields["phase"])
        real(job, stem, **fields)

    monkeypatch.setattr(midi_run, "_update", watch)
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano", "detail": "clean"})
        assert r.status_code == 202, r.text
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["engine"], entry["notes"], entry["phase"]) == (
        "done",
        "piano_hr",
        11,
        None,
    )
    assert phases == ["download", "transcribe", None]
    assert len(fetch.calls) == 1 and pinned.read_bytes() == CKPT
    stem, engine, device, params = hr_worker.calls[-1]
    assert (stem, engine, device) == ("piano", "piano_hr", "cpu")
    assert params == midi_run.MIDI_PIANO_HR_PRESETS["clean"]
    record = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))["stems"]
    piano = record["piano"]
    assert (piano["engine"], piano["engine_version"], piano["device"]) == (
        "piano_hr",
        "0.0.6",
        "cpu",
    )
    assert piano["vendor_id"] == midi_run.piano_hr.vendor_id() != midi_run.vendor_id()
    assert piano["model_sha256"] == CKPT_SHA and piano["gpu_fallback"] is False


async def test_a_checked_checkpoint_is_not_fetched_again(jobs_dir, client, hr_worker, fetch):
    _song(jobs_dir)
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        for _ in range(2):
            await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
            assert (await _settled(c))["midi"]["piano"]["state"] == "done"
    assert len(fetch.calls) == 1 and len(hr_worker.calls) == 2


async def test_a_checkpoint_that_is_not_the_pinned_file_is_never_used(
    jobs_dir, client, hr_worker, fetch, pinned
):
    _song(jobs_dir)
    # The right size, the wrong bytes: removed and fetched again.
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(b"x" * len(CKPT))
    fetch.mode = "wrong"
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["error_code"], entry["has_file"]) == ("error", "download", False)
    assert len(fetch.calls) == 1
    # What the download wrote did not match either: it is not left there.
    assert not pinned.exists()
    assert hr_worker.calls == []


async def test_a_failed_download_keeps_the_old_file_and_says_so(
    jobs_dir, client, hr_worker, fetch, caplog
):
    _song(jobs_dir)
    old = jobs_dir / JOB / "midi" / "piano.mid"
    old.parent.mkdir()
    old.write_bytes(b"MThd old")
    fetch.mode = "fail"
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["error_code"], entry["has_file"]) == ("error", "download", True)
    assert old.read_bytes() == b"MThd old" and hr_worker.calls == []
    assert "no route to host" in caplog.text


async def test_a_cancel_stops_the_download(jobs_dir, client, hr_worker, fetch, pinned):
    _song(jobs_dir)
    fetch.mode = "sleep"
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        procs: list = []
        for _ in range(200):
            entry = (await c.get(f"/api/jobs/{JOB}")).json()["midi"]["piano"]
            procs = [p for key, p in list(_procs.items()) if key[0] == JOB]
            if entry["phase"] == "download" and procs:
                break
            await asyncio.sleep(0.05)
        assert entry["phase"] == "download" and len(procs) == 1
        proc = procs[0]
        r = await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")
        assert r.json() == {"state": "cancelled"}
    assert proc.poll() is not None
    assert not pinned.exists() and hr_worker.calls == []


async def test_a_gpu_failure_is_run_again_whole_on_the_cpu(
    jobs_dir, client, hr_worker, fetch, pinned, monkeypatch
):
    _song(jobs_dir)
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(CKPT)
    monkeypatch.setattr(midi_run, "torch_engine_device", lambda: "cuda")
    hr_worker.modes = {"cuda": "engine", "cpu": "ok"}
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert entry["state"] == "done"
    assert [call[2] for call in hr_worker.calls] == ["cuda", "cpu"]
    piano = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))["stems"]["piano"]
    assert (piano["device"], piano["gpu_fallback"]) == ("cpu", True)
    assert fetch.calls == []


async def test_a_failure_on_the_cpu_the_worker_chose_is_not_run_again(
    jobs_dir, client, hr_worker, pinned, monkeypatch
):
    _song(jobs_dir)
    pinned.parent.mkdir(parents=True)
    pinned.write_bytes(CKPT)
    monkeypatch.setattr(midi_run, "torch_engine_device", lambda: "cuda")
    hr_worker.modes = {"cuda": "cpu_engine", "cpu": "ok"}
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["error_code"]) == ("error", "engine")
    assert [call[2] for call in hr_worker.calls] == ["cuda"]


async def test_each_stem_runs_its_own_engine(jobs_dir, client, hr_worker, fetch):
    _song(jobs_dir)
    async with client as c:
        await c.post(
            "/api/settings",
            json={"transcribe_piano_engine": "piano_hr", "transcribe_bass_engine": "basic_pitch"},
        )
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})
        entry = (await _settled(c, "bass"))["midi"]["bass"]
    assert entry["engine"] == "basic_pitch"
    assert hr_worker.calls[-1][:3] == ("bass", "basic_pitch", "cpu")
    assert fetch.calls == []


async def test_piano_hr_unavailable_refuses_the_piano_only(
    jobs_dir, client, hr_worker, monkeypatch
):
    _song(jobs_dir)
    monkeypatch.setattr(midi_run, "_PIANO_HR_MODULES", ("torch", "no_such_module_for_this_test"))
    midi_run.forget_installed()
    async with client as c:
        await c.post("/api/settings", json={"transcribe_piano_engine": "piano_hr"})
        status = (await c.get("/api/transcribe/status")).json()
        assert status["available"] is True
        assert (status["engines"][1]["available"], status["engines"][1]["reason"]) == (
            False,
            "no_runtime",
        )
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        _refused(r, 503, "engine_unavailable")
        await c.post("/api/settings", json={"transcribe_bass_engine": "basic_pitch"})
        assert (await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})).status_code == 202
        await _settled(c, "bass")


def test_the_device_piano_hr_asks_for(monkeypatch):
    for resolved, asked in (("cuda", "cuda"), ("mps", "cpu"), ("cpu", "cpu")):
        monkeypatch.setattr(midi_run, "get_demucs_device", lambda r=resolved: r)
        assert midi_run.torch_engine_device() == asked

    def broken():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(midi_run, "get_demucs_device", broken)
    assert midi_run.torch_engine_device() == "cpu"


def test_an_entry_names_its_engine_and_its_phase_only_while_running():
    entry = {"state": "running", "pct": 5, "phase": "download", "engine": "piano_hr"}
    assert clean_midi({"piano": entry})["piano"]["phase"] == "download"
    assert clean_midi({"piano": {**entry, "phase": "bogus"}})["piano"]["phase"] is None
    done = clean_midi({"piano": {**entry, "state": "done"}})["piano"]
    assert (done["phase"], done["engine"]) == (None, "piano_hr")
    # Saved before there was a second engine, or damaged: Basic Pitch's.
    for engine in (None, "whisper", 3):
        assert clean_midi({"piano": {"state": "done", "engine": engine}})["piano"]["engine"] == (
            "basic_pitch"
        )


# -- the third engine (ymt3): every stem's setting, its weights, its run

YMT3_CKPT = b"a stand-in for YourMT3's published checkpoint " * 16
YMT3_WEIGHTS = b"the weights-only file made from it " * 8

CONVERT_STUB = textwrap.dedent(
    """
    import json, sys, time
    mode, out, body = sys.argv[1], sys.argv[2], sys.argv[3]
    def emit(**e):
        print(json.dumps(e), flush=True)
    emit(event="progress", pct=0)
    if mode == "ok":
        open(out, "wb").write(open(body, "rb").read())
        emit(event="progress", pct=100)
        emit(event="done", tensors=655)
    elif mode == "wrong":
        open(out, "wb").write(b"x" * 7)
        emit(event="done", tensors=1)
    elif mode == "fail":
        print("conversion failed: not this model", file=sys.stderr, flush=True)
        emit(event="error", code="convert")
        sys.exit(3)
    elif mode == "sleep":
        time.sleep(60)
    """
)


@pytest.fixture
def ymt3_files(tmp_path, monkeypatch):
    """The pinned checkpoint is YMT3_CKPT and the pinned weights YMT3_WEIGHTS
    (their sizes and SHA-256 are what a run checks); the download and the
    conversion are stand-ins. Set .fetch and .convert before a run; .calls
    has ("fetch" | "convert", path) per step started."""
    from app.core.pinned_file import PinnedFile
    from app.pipeline import ymt3

    checkpoint = PinnedFile(
        ymt3.CHECKPOINT.url,
        hashlib.sha256(YMT3_CKPT).hexdigest(),
        len(YMT3_CKPT),
        ymt3.CHECKPOINT.relpath,
    )
    weights = PinnedFile(
        None, hashlib.sha256(YMT3_WEIGHTS).hexdigest(), len(YMT3_WEIGHTS), ymt3.WEIGHTS.relpath
    )
    monkeypatch.setattr(ymt3, "CHECKPOINT", checkpoint)
    monkeypatch.setattr(ymt3, "WEIGHTS", weights)
    monkeypatch.setattr(midi_run, "YMT3_CHECKPOINT_SHA256", checkpoint.sha256)
    monkeypatch.setattr(midi_run, "YMT3_CHECKPOINT_SIZE", checkpoint.size)
    monkeypatch.setattr(midi_run, "YMT3_WEIGHTS_SHA256", weights.sha256)
    fetch_script = tmp_path / "stub_fetch.py"
    fetch_script.write_text(FETCH_STUB, encoding="utf-8")
    convert_script = tmp_path / "stub_convert.py"
    convert_script.write_text(CONVERT_STUB, encoding="utf-8")
    ckpt_body = tmp_path / "ckpt.bin"
    ckpt_body.write_bytes(YMT3_CKPT)
    weights_body = tmp_path / "weights.bin"
    weights_body.write_bytes(YMT3_WEIGHTS)

    class Stub:
        fetch = "ok"
        convert = "ok"
        calls: list = []

    def fetch_cmd(dst, url, sha256, size):
        Stub.calls.append(("fetch", dst, url, sha256, size))
        return [sys.executable, str(fetch_script), Stub.fetch, str(dst), str(ckpt_body)]

    def convert_cmd(ckpt, out):
        Stub.calls.append(("convert", ckpt))
        return [sys.executable, str(convert_script), Stub.convert, str(out), str(weights_body)]

    monkeypatch.setattr(midi_run, "_spawn_fetch_cmd", fetch_cmd)
    monkeypatch.setattr(midi_run, "_spawn_convert_cmd", convert_cmd)
    Stub.checkpoint = checkpoint
    Stub.weights = weights
    return Stub


async def test_every_stem_has_its_setting_and_its_engine(client):
    async with client as c:
        body = (await c.get("/api/settings")).json()
        assert {s: body[f"transcribe_{s}"] for s in midi_run.MIDI_STEMS} == dict.fromkeys(
            midi_run.MIDI_STEMS, "auto"
        )
        # Each stem's measured winner (config.MIDI_DEFAULT_ENGINES).
        assert {s: body[f"transcribe_{s}_engine"] for s in midi_run.MIDI_STEMS} == {
            "vocals": "basic_pitch",
            "drums": "ymt3",
            "bass": "ymt3",
            "guitar": "basic_pitch",
            "piano": "ymt3",
            "other": "ymt3",
        }
        r = await c.post(
            "/api/settings",
            json={"transcribe_guitar_engine": "ymt3", "transcribe_drums": "off"},
        )
        assert r.status_code == 200
        assert (r.json()["transcribe_guitar_engine"], r.json()["transcribe_drums"]) == (
            "ymt3",
            "off",
        )
        for bad in (
            # An engine that cannot take the stem.
            {"transcribe_drums_engine": "basic_pitch"},
            {"transcribe_vocals_engine": "ymt3"},
            {"transcribe_bass_engine": "piano_hr"},
            {"transcribe_other_engine": "whisper"},
            {"transcribe_vocals": "on"},
            # One bad value and nothing in the request is written.
            {"transcribe_guitar_engine": "basic_pitch", "transcribe_drums_engine": "nope"},
        ):
            r = await c.post("/api/settings", json=bad)
            assert r.status_code == 422, bad
            assert "must be one of" in r.json()["detail"]
        body = (await c.get("/api/settings")).json()
    assert body["transcribe_guitar_engine"] == "ymt3"


def test_a_stored_engine_that_cannot_take_the_stem_reads_as_its_default():
    """A settings file written by hand, or naming an engine that is gone."""
    from app.core import settings

    with settings._LOCK:
        settings._ensure()["transcribe_drums_engine"] = "basic_pitch"
        settings._ensure()["transcribe_piano_engine"] = "piano_hr"
    assert settings.get_transcribe_engine("drums") == "ymt3"
    # The piano's key predates the others and keeps its stored choice.
    assert settings.get_transcribe_engine("piano") == "piano_hr"


async def test_a_first_ymt3_run_downloads_prepares_then_transcribes(
    jobs_dir, client, hr_worker, ymt3_files, monkeypatch
):
    _song(jobs_dir)
    phases: list = []
    real = midi_run._update

    def watch(job, stem, **fields):
        if "phase" in fields:
            phases.append(fields["phase"])
        real(job, stem, **fields)

    monkeypatch.setattr(midi_run, "_update", watch)
    async with client as c:
        r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        assert r.status_code == 202, r.text
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["engine"], entry["notes"]) == ("done", "ymt3", 11)
    assert phases == ["download", "prepare", "transcribe", None]
    kinds = [call[0] for call in ymt3_files.calls]
    assert kinds == ["fetch", "convert"]
    assert ymt3_files.calls[0][3:] == (ymt3_files.checkpoint.sha256, ymt3_files.checkpoint.size)
    # The weights stay; the checkpoint they were made from is gone.
    assert ymt3_files.weights.path().read_bytes() == YMT3_WEIGHTS
    assert not ymt3_files.checkpoint.path().exists()
    stem, engine, device, params = hr_worker.calls[-1]
    assert (stem, engine, device, params) == ("piano", "ymt3", "cpu", {"class_filter": "piano"})
    piano = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))["stems"]["piano"]
    assert (piano["engine"], piano["engine_version"]) == ("ymt3", "5e66c1e")
    assert piano["model_sha256"] == ymt3_files.weights.sha256
    assert piano["vendor_id"] == midi_run.ymt3.vendor_id()


async def test_the_weights_are_made_once(jobs_dir, client, hr_worker, ymt3_files):
    _song(jobs_dir)
    async with client as c:
        for stem in ("piano", "bass"):
            await c.post(f"/api/jobs/{JOB}/midi", json={"stem": stem})
            assert (await _settled(c, stem))["midi"][stem]["state"] == "done"
    assert [call[0] for call in ymt3_files.calls] == ["fetch", "convert"]
    assert [call[:2] for call in hr_worker.calls] == [("piano", "ymt3"), ("bass", "ymt3")]


@pytest.mark.parametrize("mode", ["fail", "wrong"])
async def test_weights_that_cannot_be_made_are_never_used(
    jobs_dir, client, hr_worker, ymt3_files, mode, caplog
):
    _song(jobs_dir)
    ymt3_files.convert = mode
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        entry = (await _settled(c))["midi"]["piano"]
    assert (entry["state"], entry["error_code"], entry["has_file"]) == ("error", "prepare", False)
    assert not ymt3_files.weights.path().exists() and hr_worker.calls == []
    # The checkpoint stays for the next try: only made weights replace it.
    assert ymt3_files.checkpoint.path().read_bytes() == YMT3_CKPT
    if mode == "fail":
        assert "not this model" in caplog.text


async def test_a_cancel_stops_the_conversion(jobs_dir, client, hr_worker, ymt3_files):
    _song(jobs_dir)
    ymt3_files.convert = "sleep"
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "piano"})
        procs: list = []
        for _ in range(200):
            entry = (await c.get(f"/api/jobs/{JOB}")).json()["midi"]["piano"]
            procs = [p for key, p in list(_procs.items()) if key[0] == JOB]
            if entry["phase"] == "prepare" and procs:
                break
            await asyncio.sleep(0.05)
        assert entry["phase"] == "prepare" and len(procs) == 1
        proc = procs[0]
        r = await c.post(f"/api/jobs/{JOB}/midi/piano/cancel")
        assert r.json() == {"state": "cancelled"}
    assert proc.poll() is not None
    assert not ymt3_files.weights.path().exists() and hr_worker.calls == []


async def test_a_ymt3_gpu_failure_is_run_again_whole_on_the_cpu(
    jobs_dir, client, hr_worker, ymt3_files, monkeypatch
):
    _song(jobs_dir)
    ymt3_files.weights.path().parent.mkdir(parents=True)
    ymt3_files.weights.path().write_bytes(YMT3_WEIGHTS)
    monkeypatch.setattr(midi_run, "torch_engine_device", lambda: "cuda")
    hr_worker.modes = {"cuda": "engine", "cpu": "ok"}
    async with client as c:
        await c.post(f"/api/jobs/{JOB}/midi", json={"stem": "bass"})
        entry = (await _settled(c, "bass"))["midi"]["bass"]
    assert entry["state"] == "done"
    assert [call[2] for call in hr_worker.calls] == ["cuda", "cpu"]
    bass = json.loads((jobs_dir / JOB / "midi.json").read_text(encoding="utf-8"))["stems"]["bass"]
    assert (bass["device"], bass["gpu_fallback"]) == ("cpu", True)
    assert ymt3_files.calls == []


async def test_the_drums_and_every_other_stem_can_be_transcribed(
    jobs_dir, client, hr_worker, monkeypatch
):
    """Every stem of htdemucs_6s, each with its own engine: the drums with
    YourMT3+ (its weights taken as made here), the vocals with Basic Pitch."""
    job = _song(jobs_dir)
    for stem in ("drums", "guitar", "other"):
        _wav(jobs_dir / JOB / "stems" / f"{stem}.wav")
    job.stems = [{"name": n, "url": ""} for n in midi_run.MIDI_STEMS]
    monkeypatch.setattr(midi_run._Ymt3, "prepare", lambda self, job, run: None)
    async with client as c:
        for stem in midi_run.MIDI_STEMS:
            r = await c.post(f"/api/jobs/{JOB}/midi", json={"stem": stem})
            assert r.status_code == 202, (stem, r.text)
            entry = (await _settled(c, stem))["midi"][stem]
            assert entry["state"] == "done", stem
            assert (jobs_dir / JOB / "midi" / f"{stem}.mid").is_file()
    engines = {call[0]: call[1] for call in hr_worker.calls}
    assert engines == {
        "vocals": "basic_pitch",
        "drums": "ymt3",
        "bass": "ymt3",
        "guitar": "basic_pitch",
        "piano": "ymt3",
        "other": "ymt3",
    }


async def test_the_zip_carries_the_midi_of_every_stem(jobs_dir, client, worker):
    _song(jobs_dir)
    midi = jobs_dir / JOB / "midi"
    midi.mkdir()
    for stem in ("drums", "vocals", "piano"):
        (midi / f"{stem}.mid").write_bytes(b"MThd " + stem.encode())
    async with client as c:
        r = await c.get(f"/api/jobs/{JOB}/stems/all.zip?format=wav&midi=1")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    mids = [n for n in names if n.endswith(".mid")]
    assert len(mids) == 3
    for stem in ("drums", "vocals", "piano"):
        assert any(stem in n for n in mids), (stem, mids)
