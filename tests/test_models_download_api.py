"""Settings > Models' Download button: the routes (app/api/config.py), the one
run (app/core/model_get.py) and the worker (app/pipeline/model_get_worker.py).

The worker the routes start is a stand-in script here, so each test says how
a run ends: the file, an error, a hang. The worker's own logic is run in
this process with each family's download replaced by a writer of known
bytes, so nothing is fetched and nothing is written outside tmp_path.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from app.core import config, model_get, model_inventory, stem_models
from app.core.model_download import CHECKSUM_REASON, ModelDownloadError
from app.pipeline import model_get_worker as worker_mod

BODY = b"model bytes " * 1000
SHA = hashlib.sha256(BODY).hexdigest()


def _plan(
    tmp_path: Path, model_id: str = "beat_this", family="pipeline", files=1
) -> model_get.Plan:
    out = []
    for i in range(files):
        out.append(
            model_get.PlanFile(
                url=f"https://example.org/{model_id}-{i}.bin",
                sha256=SHA,
                size=len(BODY),
                dest=tmp_path / "models" / model_id / f"file{i}.bin",
                via="verified",
            )
        )
    return model_get.Plan(model_id, family, tuple(out))


STUB = textwrap.dedent(
    """
    import json, os, sys, time
    mode, dest, token = sys.argv[1], sys.argv[2], sys.argv[3]
    def emit(**e):
        print(json.dumps(e), flush=True)
    stage = os.path.join(os.path.dirname(dest), ".stemdeck-get-" + token)
    os.makedirs(stage, exist_ok=True)
    emit(event="progress", bytes=0, phase="download")
    with open(os.path.join(stage, "half.partial"), "wb") as f:
        f.write(b"x" * 100)
    emit(event="progress", bytes=100, phase="download")
    if mode == "ok":
        with open(dest, "wb") as f:
            f.write(b"done")
        emit(event="done")
    elif mode == "checksum":
        emit(event="error", code="checksum")
        sys.exit(3)
    elif mode == "weird":
        emit(event="error", code="C:/Users/someone/secret")
        sys.exit(3)
    elif mode == "hang":
        time.sleep(60)
    elif mode == "silent":
        time.sleep(60)
    """
)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """The routes' worker is the stand-in, the plan a tmp_path one, and the
    model counts as one this install can download."""
    script = tmp_path / "stub_worker.py"
    script.write_text(STUB, encoding="utf-8")

    class Stub:
        mode = "ok"
        plans: dict[str, model_get.Plan] = {
            "beat_this": _plan(tmp_path, "beat_this"),
            "piano_hr": _plan(tmp_path, "piano_hr", family="midi"),
        }

    def cmd(model_id, token):
        dest = Stub.plans[model_id].files[0].dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        return [sys.executable, str(script), Stub.mode, str(dest), token]

    monkeypatch.setattr(model_get, "_spawn_cmd", cmd)
    monkeypatch.setattr(model_get, "plan_for", lambda model_id: Stub.plans.get(model_id))
    from app.api import config as config_api

    monkeypatch.setattr(config_api, "_available", lambda model_id: model_id in Stub.plans)
    return Stub


@pytest.fixture
def client():
    from app.main import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _settled(c: httpx.AsyncClient) -> dict:
    for _ in range(400):
        snap = (await c.get("/api/models/download")).json()
        if snap["state"] != "running":
            return snap
        await asyncio.sleep(0.05)
    raise AssertionError("the download never ended")


def _refused(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["code"] == code, r.text
    assert (
        isinstance(body["detail"], str) and "/" not in body["detail"] and "\\" not in body["detail"]
    )


# ── The routes ──


async def test_a_download_runs_and_ends_done(client, stub, tmp_path):
    from app.pipeline.runner import _pipeline_lock

    async with client as c:
        r = await c.post("/api/models/beat_this/download")
        assert r.status_code == 202 and r.json() == {"state": "running"}
        # A pipeline model holds the pipeline lock while it runs.
        assert _pipeline_lock.locked()
        snap = await _settled(c)
    assert snap == {
        "state": "done",
        "model": "beat_this",
        "phase": None,
        "bytes": len(BODY),
        "total": len(BODY),
        "error_code": None,
    }
    assert not _pipeline_lock.locked()
    dest = stub.plans["beat_this"].files[0].dest
    assert dest.read_bytes() == b"done"
    # Nothing of the run is left beside it.
    assert [p.name for p in dest.parent.iterdir()] == ["file0.bin"]


async def test_the_list_carries_the_run(client, stub, monkeypatch):
    async with client as c:
        body = (await c.get("/api/models")).json()
    assert body["download"]["state"] == "idle"
    assert all("downloadable" in m for m in body["models"])


async def test_unknown_and_malformed_ids_are_404(client, stub):
    async with client as c:
        for model_id in ("nope", "BEAT_THIS", "basic_pitch", "a" * 41, "beat-this", "x.y"):
            _refused(await c.post(f"/api/models/{model_id}/download"), 404, "not_found")
        # Traversal never reaches a path: the router or the id rule stops it.
        for path in (
            "/api/models/..%2F..%2Fetc%2Fpasswd/download",
            "/api/models/%2e%2e/download",
            "/api/models/../../etc/download",
        ):
            assert (await c.post(path)).status_code in (404, 405)
        assert (await c.get("/api/models/download")).json()["state"] == "idle"


async def test_a_model_already_there_is_409(client, stub):
    dest = stub.plans["beat_this"].files[0].dest
    dest.parent.mkdir(parents=True)
    dest.write_bytes(BODY)
    async with client as c:
        _refused(await c.post("/api/models/beat_this/download"), 409, "downloaded")


async def test_one_download_at_a_time(client, stub):
    stub.mode = "hang"
    async with client as c:
        assert (await c.post("/api/models/beat_this/download")).status_code == 202
        _refused(await c.post("/api/models/piano_hr/download"), 409, "busy")
        _refused(await c.post("/api/models/beat_this/download"), 409, "busy")
        snap = (await c.post("/api/models/download/cancel")).json()
    assert snap["state"] == "cancelled"


async def test_a_pipeline_model_waits_for_an_idle_pipeline(client, stub):
    from app.pipeline.runner import _pipeline_lock

    await _pipeline_lock.acquire()
    try:
        async with client as c:
            _refused(await c.post("/api/models/beat_this/download"), 409, "busy")
            # A MIDI engine's weights are no pipeline file: that one may run.
            assert (await c.post("/api/models/piano_hr/download")).status_code == 202
            await _settled(c)
    finally:
        _pipeline_lock.release()


async def test_a_midi_engine_waits_for_no_midi_run(client, stub, monkeypatch):
    from app.pipeline import midi as midi_run

    monkeypatch.setattr(midi_run, "current", lambda: object())
    async with client as c:
        _refused(await c.post("/api/models/piano_hr/download"), 409, "busy")
        # Separation models do not care about MIDI runs.
        assert (await c.post("/api/models/beat_this/download")).status_code == 202
        await _settled(c)


async def test_a_queued_song_makes_it_busy(client, stub, monkeypatch):
    from app.pipeline import jobqueue

    monkeypatch.setattr(jobqueue, "snapshot", lambda: ("abcdef123456", []))
    async with client as c:
        _refused(await c.post("/api/models/beat_this/download"), 409, "busy")


async def test_cancel_stops_it_and_leaves_nothing(client, stub):
    stub.mode = "hang"
    dest = stub.plans["beat_this"].files[0].dest
    async with client as c:
        assert (await c.post("/api/models/beat_this/download")).status_code == 202
        for _ in range(200):
            snap = (await c.get("/api/models/download")).json()
            if snap["bytes"] == 100:
                break
            await asyncio.sleep(0.05)
        assert snap["bytes"] == 100 and snap["phase"] == "download"
        assert any(p.name.startswith(".stemdeck-get-") for p in dest.parent.iterdir())
        snap = (await c.post("/api/models/download/cancel")).json()
    assert snap["state"] == "cancelled" and snap["error_code"] is None
    # The run's folder went with it, and nothing took the final name.
    assert list(dest.parent.iterdir()) == []
    from app.pipeline.runner import _pipeline_lock

    assert not _pipeline_lock.locked()


async def test_cancel_with_nothing_running(client, stub):
    async with client as c:
        r = await c.post("/api/models/download/cancel")
    assert r.status_code == 200 and r.json()["state"] == "idle"


@pytest.mark.parametrize(("mode", "code"), [("checksum", "checksum"), ("weird", "failed")])
async def test_a_failure_is_a_fixed_code(client, stub, mode, code):
    stub.mode = mode
    dest = stub.plans["beat_this"].files[0].dest
    async with client as c:
        assert (await c.post("/api/models/beat_this/download")).status_code == 202
        snap = await _settled(c)
        text = (await c.get("/api/models/download")).text
    assert snap["state"] == "failed" and snap["error_code"] == code
    # Never a path or a word of the worker's in the answer.
    assert "Users" not in text and "secret" not in text
    assert not dest.exists() and list(dest.parent.iterdir()) == []


async def test_a_silent_worker_is_stopped_as_a_network_failure(client, stub, monkeypatch):
    stub.mode = "silent"
    monkeypatch.setattr(config, "TIMEOUT_MODEL_GET_STALL", 0.5)
    async with client as c:
        assert (await c.post("/api/models/beat_this/download")).status_code == 202
        snap = await _settled(c)
    assert (snap["state"], snap["error_code"]) == ("failed", "network")


async def test_a_midi_run_of_that_engine_is_refused_while_it_downloads(client, stub):
    """app/api/midi.py: a run would fetch the weights a second time beside
    the download, or read them before they are whole."""
    stub.mode = "hang"
    async with client as c:
        assert (await c.post("/api/models/piano_hr/download")).status_code == 202
        assert model_get.running_model() == "piano_hr"
        await c.post("/api/models/download/cancel")
    assert model_get.running_model() is None


# ── The plan: each family's own address and file ──


@pytest.fixture
def folders(tmp_path, monkeypatch):
    hub = tmp_path / "torch" / "hub" / "checkpoints"
    monkeypatch.setattr(stem_models, "hub_checkpoints_dir", lambda: hub)
    monkeypatch.setattr(stem_models, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    from app.core import model_cache

    monkeypatch.setattr(
        model_cache, "beat_this_artifacts", lambda name: [hub / f"beat_this-{name}.ckpt"]
    )
    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "torch"))
    return tmp_path


def test_every_downloadable_model_has_a_plan_under_its_own_folder(folders):
    ids = [
        "htdemucs_6s",
        "htdemucs_ft",
        "kim_mel_roformer",
        "uvr_karaoke_2",
        "piano_hr",
        "ymt3",
        "whisper_turbo",
        "whisper_small",
        "beat_this",
    ]
    for model_id in ids:
        plan = model_get.plan_for(model_id)
        assert plan is not None, model_id
        assert plan.files and plan.total > 0, model_id
        for f in plan.files:
            assert f.url.startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", f.sha256)
    six = model_get.plan_for("htdemucs_6s")
    assert [f.via for f in six.files] == ["hub"]
    assert six.files[0].dest == folders / "torch" / "hub" / "checkpoints" / "5c90dfd2-34c22ccb.th"
    assert len(model_get.plan_for("htdemucs_ft").files) == 4
    kim = model_get.plan_for("kim_mel_roformer")
    assert kim.files[0].via == "kim"
    assert kim.files[0].dest == folders / "models" / "roformer" / "MelBandRoformer.ckpt"
    karaoke = model_get.plan_for("uvr_karaoke_2")
    assert (
        karaoke.files[0].dest == folders / "models" / "audio-separator" / "UVR_MDXNET_KARA_2.onnx"
    )
    turbo = model_get.plan_for("whisper_turbo")
    assert turbo.files[0].dest == folders / "torch" / "whisper" / "large-v3-turbo.pt"
    ymt3_plan = model_get.plan_for("ymt3")
    assert ymt3_plan.family == "midi" and ymt3_plan.convert_to is not None
    assert model_get.plan_for("piano_hr").family == "midi"
    assert model_get.plan_for("beat_this").files[0].size == config.BEAT_THIS_FINAL0_SIZE


def test_no_plan_where_the_code_has_no_pin(folders, monkeypatch):
    assert model_get.plan_for("basic_pitch") is None
    assert model_get.plan_for("whisper_medium") is None
    assert model_get.plan_for("../etc") is None
    assert model_get.plan_for("") is None
    monkeypatch.setattr(config, "VOCAL_SPLIT_MODEL", "other.ckpt")
    assert model_get.plan_for("uvr_karaoke_2") is None
    monkeypatch.setattr(config, "BEAT_MODEL_CHECKPOINT", "final1")
    assert model_get.plan_for("beat_this") is None


def test_a_folder_that_cannot_be_worked_out_has_no_plan(folders, monkeypatch):
    def broken():
        raise OSError("torch failed to load")

    monkeypatch.setattr(stem_models, "hub_checkpoints_dir", broken)
    assert model_get.plan_for("htdemucs_6s") is None


def _source_of(module: str) -> str:
    import importlib.util

    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        pytest.skip(f"{module} is not installed")
    return Path(spec.origin).read_text(encoding="utf-8")


def test_the_pins_are_the_addresses_the_libraries_fetch():
    """Read from each library's source, never imported (whisper loads numba):
    the button fetches the same file the library would."""
    whisper = _source_of("whisper")
    for name, (url, sha256, _size) in config.WHISPER_FILES.items():
        assert f'"{name}": "{url}"' in whisper, name
        assert f"/{sha256}/" in url
    beat = _source_of("beat_this.inference")
    m = re.search(r'CHECKPOINT_URL = "([^"]+)"', beat)
    assert m and m.group(1) + "/final0.ckpt" == config.BEAT_THIS_FINAL0_URL
    separator = _source_of("audio_separator.separator.separator")
    m = re.search(r'public_model_repo_url_prefix = "([^"]+)"', separator)
    assert m and m.group(1) + "/UVR_MDXNET_KARA_2.onnx" == config.KARAOKE_2_URL


def test_the_inventory_and_the_plan_agree_on_sizes(folders):
    for model_id, size in (
        ("uvr_karaoke_2", config.KARAOKE_2_SIZE),
        ("beat_this", config.BEAT_THIS_FINAL0_SIZE),
        ("whisper_turbo", config.WHISPER_FILES["turbo"][2]),
        ("whisper_small", config.WHISPER_FILES["small"][2]),
    ):
        assert model_get.plan_for(model_id).total == size, model_id
    assert model_inventory._KARAOKE_BYTES == config.KARAOKE_2_SIZE


# ── The worker, in this process ──


def _events():
    said: list[tuple[str, dict]] = []

    def emit(event, **fields):
        said.append((event, fields))

    return said, emit


@pytest.fixture
def no_net(monkeypatch):
    """No socket timeout games and no torch: bounded_downloads is a no-op."""
    import contextlib

    from app.core import model_download

    monkeypatch.setattr(
        model_download, "bounded_downloads", lambda *a, **k: contextlib.nullcontext()
    )


def _writer(content: bytes = BODY, calls: list | None = None):
    def fetch(f, dst, on_bytes):
        if calls is not None:
            calls.append(f.url)
        dst = Path(dst)
        dst.write_bytes(content[: len(content) // 2])
        on_bytes(len(content) // 2)
        dst.write_bytes(content)
        on_bytes(len(content))

    return fetch


TOKEN = "0" * 32


def test_the_worker_places_each_file_after_its_check(tmp_path, monkeypatch, no_net):
    plan = _plan(tmp_path, files=2)
    calls: list = []
    monkeypatch.setattr(worker_mod, "_fetch", _writer(calls=calls))
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 0
    assert said[-1] == ("done", {})
    for f in plan.files:
        assert f.dest.read_bytes() == BODY
    assert len(calls) == 2
    got = [e[1]["bytes"] for e in said if e[0] == "progress"]
    assert got[0] == 0 and got[-1] == plan.total and got == sorted(got)


def test_a_file_already_pinned_is_left_alone(tmp_path, monkeypatch, no_net):
    plan = _plan(tmp_path, files=2)
    plan.files[0].dest.parent.mkdir(parents=True)
    plan.files[0].dest.write_bytes(BODY)
    before = plan.files[0].dest.stat().st_mtime_ns
    calls: list = []
    monkeypatch.setattr(worker_mod, "_fetch", _writer(calls=calls))
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 0
    assert calls == [plan.files[1].url]
    assert plan.files[0].dest.stat().st_mtime_ns == before
    # The progress starts at what is there already.
    assert said[0] == ("progress", {"bytes": len(BODY), "phase": "download"})


def test_a_wrong_file_never_takes_the_final_name(tmp_path, monkeypatch, no_net):
    plan = _plan(tmp_path)
    monkeypatch.setattr(worker_mod, "_fetch", _writer(b"something else entirely"))
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 3
    assert said[-1] == ("error", {"code": "checksum"})
    assert not plan.files[0].dest.exists()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ModelDownloadError("https://x", "/f", "DNS lookup failed"), "network"),
        (ModelDownloadError("https://x", "/f", CHECKSUM_REASON), "checksum"),
        (OSError(errno.ENOSPC, "No space left on device"), "disk"),
        (OSError(errno.EACCES, "Permission denied"), "io"),
        (RuntimeError("anything else"), "failed"),
    ],
)
def test_each_failure_is_its_code(tmp_path, monkeypatch, no_net, exc, code):
    plan = _plan(tmp_path)

    def fetch(f, dst, on_bytes):
        Path(dst).write_bytes(b"half")
        raise exc

    monkeypatch.setattr(worker_mod, "_fetch", fetch)
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) != 0
    assert said[-1] == ("error", {"code": code})
    assert not plan.files[0].dest.exists()


def test_not_enough_space_fetches_nothing(tmp_path, monkeypatch, no_net):
    plan = _plan(tmp_path)
    calls: list = []
    monkeypatch.setattr(worker_mod, "_fetch", _writer(calls=calls))

    class Usage:
        free = 10

    monkeypatch.setattr(worker_mod.shutil, "disk_usage", lambda path: Usage)
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 3
    assert said[-1] == ("error", {"code": "disk"}) and calls == []


def test_yourmt3_is_converted_and_its_checkpoint_removed(tmp_path, monkeypatch, no_net):
    from app.pipeline import ymt3, ymt3_convert

    base = _plan(tmp_path, "ymt3", family="midi")
    weights = tmp_path / "models" / "ymt3" / "weights.safetensors"
    plan = model_get.Plan("ymt3", "midi", base.files, convert_to=weights)
    monkeypatch.setattr(worker_mod, "_fetch", _writer())
    monkeypatch.setattr(ymt3, "weights_ok", lambda path=None: False)
    seen = {}

    def convert(ckpt, out, sha256, size, emit):
        seen["ckpt"] = ckpt.read_bytes()
        # Into the run's own folder, never the final name.
        assert out.parent.name == model_get.STAGING_PREFIX + TOKEN
        out.write_bytes(b"weights")
        return 0

    monkeypatch.setattr(ymt3_convert, "convert", convert)
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 0
    assert seen["ckpt"] == BODY
    assert weights.read_bytes() == b"weights"
    assert not base.files[0].dest.exists()
    assert ("progress", {"bytes": len(BODY), "phase": "prepare"}) in said


def test_a_conversion_that_fails_is_prepare(tmp_path, monkeypatch, no_net):
    from app.pipeline import ymt3, ymt3_convert

    base = _plan(tmp_path, "ymt3", family="midi")
    weights = tmp_path / "models" / "ymt3" / "weights.safetensors"
    plan = model_get.Plan("ymt3", "midi", base.files, convert_to=weights)
    monkeypatch.setattr(worker_mod, "_fetch", _writer())
    monkeypatch.setattr(ymt3, "weights_ok", lambda path=None: False)

    def convert(ckpt, out, sha256, size, emit):
        emit("error", code="convert")
        return 3

    monkeypatch.setattr(ymt3_convert, "convert", convert)
    said, emit = _events()
    assert worker_mod.run(plan, TOKEN, emit) == 3
    assert said[-1] == ("error", {"code": "prepare"})
    assert not weights.exists()


def test_a_crash_s_leftover_folder_is_swept_once_old(tmp_path):
    plan = _plan(tmp_path)
    folder = plan.files[0].dest.parent
    old = folder / (model_get.STAGING_PREFIX + "a" * 32)
    young = folder / (model_get.STAGING_PREFIX + "b" * 32)
    other = folder / ".stemdeck-get-notatoken"
    for d in (old, young, other):
        d.mkdir(parents=True)
        (d / "x.partial").write_bytes(b"x")
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))
    assert model_get.sweep_stale_staging(plan) == [old.name]
    assert not old.exists() and young.exists() and other.exists()


def test_a_token_is_32_hex():
    with pytest.raises(ValueError):
        model_get.staging_dir(Path("."), "../../x")
    assert re.fullmatch(r"[0-9a-f]{32}", model_get.new_token())


def test_the_worker_protocol_is_json_lines():
    assert model_get.parse_event('{"event":"progress","bytes":3}') == {
        "event": "progress",
        "bytes": 3,
    }
    assert model_get.parse_event("Downloading: 50%") is None
    assert model_get.parse_event(json.dumps({"event": "other"})) is None
